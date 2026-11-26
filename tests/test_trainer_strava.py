"""Strava, live (apps/trainer/strava.py): the webhook, handling events, the whole-history import, and his own log."""

import tempfile
import unittest
import unittest.mock
import uuid
import warnings
from pathlib import Path

from fastapi.testclient import TestClient

from apps.trainer import strava
from database import Database, reset_current_user_id, set_current_user_id

ATHLETE = "777"


def activity(activity_id, kind="Run", name="Morning Run", day="2026-11-24", miles=6.0, with_detail=False):
    data = {"id": activity_id, "type": kind, "sport_type": kind, "name": name, "start_date": f"{day}T13:00:00Z",
            "start_date_local": f"{day}T06:00:00Z", "distance": miles * strava.METERS_PER_MILE, "moving_time": 2700,
            "average_heartrate": 140.0}
    if with_detail:
        data["laps"] = [{"lap_index": 1, "distance": 1609.0}]
    return data


class FakeStrava:
    """Strava's API, in memory: records calls, serves activities, and can be told to fail."""

    def __init__(self):
        self.calls, self.activities, self.subscriptions, self.fail, self.streams = [], {}, [], None, {}
        self.no_streams = set()

    def __call__(self, url, method="GET", data=None, token=""):
        self.calls.append((method, url.split("?")[0], token))
        if self.fail:
            raise strava.StravaError(self.fail)
        if url.startswith(strava.TOKEN_URL):
            return {"access_token": "fresh", "refresh_token": "r2", "expires_at": 9999999999}
        if "/push_subscriptions" in url:
            if method == "POST":
                self.subscriptions = [{"id": 42, "callback_url": data["callback_url"]}]
                return {"id": 42}
            if method == "DELETE":
                self.subscriptions = []
                return {}
            return list(self.subscriptions)
        if "/athlete/activities" in url:
            after = int(url.split("after=")[1].split("&")[0])
            from datetime import datetime
            items = [a for a in self.activities.values()
                     if datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")).timestamp() > after]
            return sorted(items, key=lambda a: a["start_date"])[:2]  # small pages, so paging is exercised
        if "/streams" in url:
            activity_id = int(url.split("/activities/")[1].split("/")[0])
            if activity_id in self.no_streams:
                raise strava.StravaError("Strava said 404: Resource Not Found")
            return self.streams.get(activity_id, {})
        if "/activities/" in url:
            found = self.activities.get(int(url.rsplit("/", 1)[1].split("?")[0]))
            if not found:
                raise strava.StravaError("Strava said 404: not found")
            return dict(found, laps=[{"lap_index": 1}])
        raise AssertionError(url)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(str(Path(self.tmp.name) / "strava.db"))
        self.db.init()
        self.user = self.db.create_user("runner@example.com", "Runner", "x", "member")
        self.token = set_current_user_id(self.user)
        self.db.update_trainer_strava_tokens(ATHLETE, "old", "r1", 0, "activity:read_all")
        self.fake = FakeStrava()
        self.sync = strava.Sync(self.db, "client", "secret", call=self.fake)

    def tearDown(self):
        reset_current_user_id(self.token)
        if self.db.conn is not None:
            self.db.conn.close()
        self.tmp.cleanup()

    def entries(self, **kw):
        return strava.log(self.db, self.user, **kw)

    def test_subscribe_and_the_callback_check(self):
        self.assertEqual(self.sync.subscribe("https://askdieter.ai/strava/webhook"), 42)
        self.assertTrue(self.sync.status()["live"])
        token = self.sync.verify_token()
        self.assertEqual(self.sync.challenge("subscribe", token, "abc"), {"hub.challenge": "abc"})
        self.assertIsNone(self.sync.challenge("subscribe", "wrong", "abc"))
        self.assertEqual(self.sync.subscribe("https://askdieter.ai/strava/webhook"), 42)  # already there: reused

    def test_a_new_run_arrives_by_itself(self):
        self.sync.subscribe("https://askdieter.ai/strava/webhook")
        self.fake.activities[1001] = activity(1001, name="Threshold 4x1600")
        self.assertFalse(self.sync.receive({"subscription_id": 99, "object_type": "activity"}))  # not ours
        self.assertTrue(self.sync.receive({"subscription_id": 42, "object_type": "activity", "object_id": 1001,
                                           "aspect_type": "create", "owner_id": int(ATHLETE)}))
        self.assertEqual(self.sync.process(), 1)
        entry = self.entries()[0]
        self.assertEqual((entry["title"], entry["category"], entry["miles"], entry["time"]),
                         ("Threshold 4x1600", "run_threshold", 6.0, "06:00"))
        self.assertTrue(entry["has_detail"])
        self.assertIn(("POST", strava.TOKEN_URL, ""), self.fake.calls)  # expired token refreshed first

    def test_every_activity_type_is_kept_and_deletes_are_marked_not_lost(self):
        self.sync.subscribe("https://askdieter.ai/strava/webhook")
        self.fake.activities[2] = activity(2, kind="Pilates", name="Reformer", miles=0)
        self.fake.activities[3] = activity(3, kind="WeightTraining", name="Glute bridge and hips", miles=0)
        for n in (2, 3):
            self.sync.receive({"subscription_id": 42, "object_type": "activity", "object_id": n,
                               "aspect_type": "create", "owner_id": ATHLETE})
        self.sync.receive({"subscription_id": 42, "object_type": "activity", "object_id": 2, "aspect_type": "delete",
                           "owner_id": ATHLETE})
        self.sync.process()
        self.assertEqual([e["category"] for e in self.entries()], ["strength_glutes"])
        kept = self.entries(include_deleted=True)
        self.assertEqual({e["category"]: e["deleted_on_strava"] for e in kept},
                         {"pilates": True, "strength_glutes": False})

    def test_failures_are_retried_then_given_up(self):
        self.sync.subscribe("https://askdieter.ai/strava/webhook")
        self.sync.receive({"subscription_id": 42, "object_type": "activity", "object_id": 5, "aspect_type": "create",
                           "owner_id": ATHLETE})
        for _ in range(5):
            self.sync.process()  # activity 5 doesn't exist: fails each time
        self.assertEqual(self.sync.status()["pending"], 0)
        status = self.db.connect().execute("SELECT status, tries FROM tr_strava_events").fetchone()
        self.assertEqual((status["status"], status["tries"]), ("failed", 5))

    def test_unknown_athletes_are_ignored(self):
        self.sync.subscribe("https://askdieter.ai/strava/webhook")
        self.sync.receive({"subscription_id": 42, "object_type": "activity", "object_id": 9, "aspect_type": "create",
                           "owner_id": 123456})
        self.sync.process()
        self.assertEqual(self.entries(), [])

    def test_whole_history_in_pages_then_details(self):
        for n, day in enumerate(("2019-03-01", "2021-06-02", "2023-01-03", "2025-05-04", "2026-11-20"), start=10):
            self.fake.activities[n] = activity(n, day=day)
        self.sync._store(self.user, activity(14, day="2026-11-20", name="Already here"),
                         activity(14, day="2026-11-20", name="Already here", with_detail=True))
        stored = self.sync.import_history(self.user, pages=5, per_page=2)
        self.assertEqual(stored, 4)  # the one already in the log isn't replaced by a summary
        self.assertTrue(self.sync.status(self.user)["history_done"])
        self.assertEqual(self.sync.status(self.user)["history_count"], 4)
        # Someone else connecting later starts their own import (it used to be one flag for the whole site).
        other = self.db.create_user("friend@example.com", "Friend", "x", "member")
        self.assertEqual(self.sync.history_state(other), (False, 0))
        self.assertEqual(sum(1 for e in self.entries() if not e["has_detail"]), 4)
        self.assertEqual(self.sync.fill_details(self.user, limit=3), 3)
        self.assertEqual(sum(1 for e in self.entries() if not e["has_detail"]), 1)
        self.assertEqual(next(e for e in self.entries() if e["date"] == "2026-11-20")["title"], "Already here")

    def test_weekly_and_monthly_totals(self):
        from datetime import date
        strava.add_manual(self.db, self.user, "2026-11-23", "Pilates", "Reformer", minutes=60)
        strava.add_manual(self.db, self.user, "2026-11-24", "Weight Training", "Hips", minutes=30)
        self.sync._store(self.user, activity(1, day="2026-11-24", miles=6.0), None)
        self.sync._store(self.user, activity(2, day="2026-11-25", miles=8.0), None)
        totals = strava.summary(self.db, self.user, today=date(2026, 11, 26))
        week = totals["weeks"][0]
        self.assertEqual((week["key"], week["run_miles"], week["runs"], week["strength"], week["pilates"]),
                         ("2026-11-23", 14.0, 2, 1, 1))
        self.assertEqual(totals["weeks"][1]["runs"], 0)
        self.assertEqual(totals["months"][0]["key"], "2026-11")
        self.assertEqual(len(totals["weeks"]), 12)

    def test_manual_entries_and_filters(self):
        strava.add_manual(self.db, self.user, "2026-11-23", "Pilates", "Reformer class", minutes=50, notes="core")
        self.sync._store(self.user, activity(1, day="2026-11-24"), None)
        self.assertEqual([e["source"] for e in self.entries()], ["strava", "dieter"])
        self.assertEqual([e["title"] for e in self.entries(kind="pilates")], ["Reformer class"])
        self.assertEqual(self.entries(year=2025), [])


class WebhookPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        cls.api = api

    def test_strava_can_reach_the_webhook_without_signing_in(self):
        client = TestClient(self.api.app)
        token = strava.Sync(self.api.db).verify_token()
        answer = client.get("/strava/webhook", params={"hub.mode": "subscribe", "hub.verify_token": token,
                                                       "hub.challenge": "xyz"})
        self.assertEqual(answer.json(), {"hub.challenge": "xyz"})
        self.assertEqual(client.get("/strava/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no",
                                                               "hub.challenge": "xyz"}).status_code, 403)
        posted = client.post("/strava/webhook", json={"object_type": "athlete", "object_id": 1,
                                                      "aspect_type": "update", "owner_id": uuid.uuid4().int % 10**6})
        self.assertEqual(posted.status_code, 200)

    def test_log_page_and_export(self):
        import secrets
        from datetime import datetime, timedelta
        db = self.api.db
        user = db.create_user(f"log-{uuid.uuid4().hex[:8]}@example.com", "Log User", "x", "member")
        conn = db.connect()
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (user,))
        conn.commit()
        session = secrets.token_urlsafe(16)
        db.create_session(user, self.api.hash_session_token(session), (datetime.utcnow() + timedelta(days=1)).isoformat())
        client = TestClient(self.api.app)
        client.cookies.set(self.api.SESSION_COOKIE_NAME, session)
        token = set_current_user_id(user)
        try:
            client.post("/apps/trainer/log/add", data={"day": "2026-11-23", "activity_type": "Pilates",
                                                       "title": "Barre fusion 77", "minutes": "50"})
            page = client.get("/apps/trainer/log")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Last 12 weeks", page.text)
            self.assertNotIn("Barre fusion 77", page.text)  # totals, not a list of every session
            exported = client.get("/apps/trainer/log.csv")
            self.assertIn("Barre fusion 77", exported.text)
            self.assertTrue(exported.text.startswith("date,time,source,type"))
            self.assertEqual(client.post("/apps/trainer/sync/live").status_code, 403)  # admin only
            from apps.trainer import connector
            status = connector.sync_status({})
            self.assertEqual((status["strava_connected"], status["sessions_in_log"]), (False, 1))
            self.assertEqual(status["newest_session"]["title"], "Barre fusion 77")
            self.assertEqual(len(connector.this_week({})["days"]), 7)
            self.assertEqual(connector.log_totals({"weeks": 4})["total"], 1)
        finally:
            reset_current_user_id(token)


if __name__ == "__main__":
    unittest.main()


def stream(segments):
    """Strava streams (time, speed, distance) from [(seconds, m/s), ...], one sample a second, with a little wobble."""
    times, speeds, dists, t, d = [], [], [], 0, 0.0
    for seconds, mps in segments:
        for i in range(int(seconds)):
            v = mps + (0.1 if i % 7 < 3 else -0.1) if mps < 4 else mps
            times.append(t); speeds.append(v); dists.append(round(d, 1)); t += 1; d += v
    return {"time": {"data": times}, "velocity_smooth": {"data": speeds}, "distance": {"data": dists}}


def easy_run_with_strides(strides=5, easy=2.8, fast=5.3, minutes=50):
    """An easy run that ends with strides: 20 s fast, 60 s easy jog between."""
    return stream([(minutes * 60, easy)] + [(20, fast), (60, 2.2)] * strides)


def session_of(reps, rep_seconds, rep_mps, jog=90):
    """Warm up 15 min, reps with jogs, cool down 10 min."""
    return stream([(900, 2.9)] + [(rep_seconds, rep_mps), (jog, 2.2)] * reps + [(600, 2.8)])


class StrideTests(SyncTests):
    """Fast running found in each run's streams, ticked on the plan, and the speed measures week by week."""

    def parts(self, st):
        return st["time"]["data"], st["velocity_smooth"]["data"], st["distance"]["data"]

    def test_finding_strides_and_reps(self):
        from apps.trainer import speed
        found, usual = speed.find_efforts(*self.parts(easy_run_with_strides()))
        self.assertEqual([e["kind"] for e in found], ["stride"] * 5)
        self.assertAlmostEqual(usual, 2.8, delta=0.2)
        self.assertAlmostEqual(found[0]["top_mps"], 5.3, delta=0.05)
        self.assertEqual(speed.summary(found), "5 strides (5:04/mi)")
        # 200s and 400s are named; a plain easy run, one GPS spike and a long tempo stretch aren't fast running.
        two = speed.find_efforts(*self.parts(session_of(8, 28, 200 / 28)))[0]
        self.assertEqual([e["kind"] for e in two], ["200"] * 8)
        four = speed.find_efforts(*self.parts(session_of(8, 79, 400 / 79)))[0]
        self.assertEqual([e["kind"] for e in four], ["400"] * 8)
        self.assertEqual(speed.summary(four), "8 × 400 avg 1:19, 90 s rest")
        plain = easy_run_with_strides(strides=0)
        self.assertEqual(speed.find_efforts(*self.parts(plain))[0], [])
        t, v, d = self.parts(plain)
        spiky = list(v); spiky[500] = 9.0
        self.assertEqual(speed.find_efforts(t, spiky, d)[0], [])
        tempo = stream([(600, 2.8), (1200, 4.3), (600, 2.8)])          # a 20-minute tempo inside a longer run
        self.assertEqual([e["kind"] for e in speed.find_efforts(*self.parts(tempo), easy=2.85)[0]], ["tempo"])
        steady = stream([(5400, 3.38)])                                  # a steady long run isn't fast running
        self.assertEqual(speed.find_efforts(*self.parts(steady), easy=2.85)[0], [])

    def test_the_two_sessions_by_the_critical_speed_model(self):
        """8 × 200 in 28 against 8 × 400 in 79, for a runner with a 5:18 mile and a 17:39 5K."""
        from apps.trainer import speed
        cs, d_prime = speed.fit_critical_speed({"1609": 318, "5000": 1059})
        self.assertAlmostEqual(cs, 4.576, delta=0.01)
        self.assertAlmostEqual(d_prime, 154, delta=3)
        self.assertEqual(speed.pace(cs), "5:52")
        used = {}
        for name, (reps, secs, mps) in {"200s": (8, 28, 200 / 28), "400s": (8, 79, 400 / 79)}.items():
            t, v, _ = self.parts(session_of(reps, secs, mps))
            used[name] = speed.d_prime_used(speed.histogram(t, v), cs)
        self.assertAlmostEqual(used["200s"], 570, delta=40)
        self.assertAlmostEqual(used["400s"], 310, delta=40)
        self.assertEqual(speed.fit_critical_speed({"400": 60}), (None, None))  # one short effort isn't enough

    def test_best_efforts_in_a_run(self):
        from apps.trainer import speed
        t, v, d = self.parts(stream([(600, 2.8), (318, 1609.344 / 318), (600, 2.8)]))
        bests = speed.best_efforts(t, d)
        self.assertAlmostEqual(bests["1609"], 318, delta=2)
        self.assertLess(bests["400"], 80)
        self.assertNotIn("5000", bests)

    def test_a_new_run_with_strides_ticks_the_plan(self):
        from apps.trainer import plan, speed
        store = plan.PlanStore(self.db)
        session = store.add_session("2026-11-24", "easy", "Easy run", 6, strides=6)
        self.fake.activities[501] = activity(501)
        self.fake.streams[501] = easy_run_with_strides()
        self.sync.receive({"object_type": "activity", "object_id": 501, "aspect_type": "create", "owner_id": ATHLETE})
        self.sync.process()
        self.assertTrue(store.session(session["id"])["strides_done"])
        week = store.week_view(__import__("datetime").date(2026, 11, 24))
        run = next(a for d in week["days"] for a in d["strava"] if a["is_run"])
        self.assertEqual((run["strides"], run["speed"]), (5, "5 strides (5:04/mi)"))

    def test_older_runs_are_read_a_few_at_a_time_and_the_weekly_measures(self):
        from datetime import date, timedelta
        from apps.trainer import analysis, speed
        today = date.today()
        runs = [(84, stream([(600, 2.8), (318, 1609.344 / 318), (600, 2.8)])),          # a hard mile
                (77, stream([(600, 2.8), (1059, 5000 / 1059), (600, 2.8)])),            # a hard 5K
                (70, easy_run_with_strides(fast=4.9)), (63, easy_run_with_strides(fast=4.9)),
                (21, easy_run_with_strides(fast=5.3)), (14, easy_run_with_strides(fast=5.35)),
                (7, session_of(8, 79, 400 / 79)), (0, session_of(8, 28, 200 / 28))]
        for i, (days_ago, st) in enumerate(runs):
            day = (today - timedelta(days=days_ago)).isoformat()
            self.fake.activities[600 + i] = activity(600 + i, day=day)
            self.fake.streams[600 + i] = st
            self.sync._store(self.user, self.fake.activities[600 + i])
        self.assertEqual(speed.waiting_count(self.db, self.user), 8)
        self.assertEqual(speed.fill(self.sync, self.user, limit=3), 3)
        self.assertEqual(speed.fill(self.sync, self.user, limit=10), 5)
        self.assertEqual(speed.fill(self.sync, self.user, limit=10), 0)  # each run is read once
        o = analysis.overview(self.db, self.user)["speed"]
        self.assertEqual(o["waiting"], 0)
        self.assertAlmostEqual(o["weeks"][-1]["cs"], 4.58, delta=0.08)    # about 5:52/mi, from all the bests found
        self.assertTrue(80 <= o["d_prime"] <= 250, o["d_prime"])
        self.assertAlmostEqual(o["speed_work"], 570, delta=60)         # this week: the 200s
        self.assertGreater(o["near_top"], 1000)                         # 200s at near top speed
        self.assertTrue(o["stride_pace_recent"] < o["stride_pace_before"], o)
        self.assertIn("200", o["latest"]["summary"])

    def test_a_table_from_an_earlier_version_gets_the_new_columns(self):
        from apps.trainer import speed
        conn = self.db.connect()
        conn.execute("CREATE TABLE tr_run_speed (user_id INTEGER NOT NULL, external_id TEXT NOT NULL, day TEXT NOT NULL, "
                     "strides INTEGER DEFAULT 0, strides_json TEXT DEFAULT '[]', stride_mps REAL, usual_mps REAL, "
                     "checked_at TEXT, PRIMARY KEY (user_id, external_id))")
        conn.execute("INSERT INTO tr_run_speed (user_id, external_id, day) VALUES (?, 'old', '2026-11-20')", (self.user,))
        conn.commit()
        speed.ensure(self.db)
        row = conn.execute("SELECT version, efforts_json FROM tr_run_speed WHERE external_id = 'old'").fetchone()
        self.assertEqual((row["version"], row["efforts_json"]), (0, "[]"))  # read again (version 0 < VERSION)

    def load_weeks(self, weekly_runs):
        """weekly_runs: {weeks_ago: [stream, ...]}, plus a hard mile and 5K 10 weeks ago for critical speed."""
        from datetime import date, timedelta
        monday = date.today() - timedelta(days=date.today().weekday())
        weekly_runs = dict(weekly_runs)
        weekly_runs.setdefault(10, []).extend([stream([(600, 2.8), (318, 1609.344 / 318), (600, 2.8)]),
                                               stream([(600, 2.8), (1059, 5000 / 1059), (600, 2.8)])])
        n = 700
        for weeks_ago, streams in weekly_runs.items():
            for i, st in enumerate(streams):
                day = (monday - timedelta(weeks=weeks_ago) + timedelta(days=i)).isoformat()
                self.fake.activities[n] = activity(n, day=day)
                self.fake.streams[n] = st
                self.sync._store(self.user, self.fake.activities[n])
                n += 1
        from apps.trainer import speed
        speed.fill(self.sync, self.user, limit=100)

    def test_a_sudden_block_of_400s_goes_over_the_line(self):
        from apps.trainer import analysis
        quiet = {w: [easy_run_with_strides(strides=4, fast=5.0)] for w in (2, 3, 4, 5)}
        quiet[1] = [session_of(18, 82, 400 / 82, jog=30), easy_run_with_strides(strides=4, fast=5.0)]
        self.load_weeks(quiet)
        o = analysis.overview(self.db, self.user)["speed"]
        last = o["weeks"][-2]
        self.assertTrue(last["work_over"], last)
        self.assertGreater(last["speed_work"], last["work_line"])
        self.assertIn(last["label"], [w["label"] for w in o["over_weeks"]])
        self.assertFalse(any(w["work_over"] for w in o["weeks"][-6:-2]))  # the strides weeks stay under

    def test_building_up_to_it_stays_under(self):
        from apps.trainer import analysis
        build = {6: [session_of(8, 82, 400 / 82, jog=60)], 5: [session_of(10, 82, 400 / 82, jog=60)],
                 4: [session_of(12, 82, 400 / 82, jog=45)], 3: [session_of(14, 82, 400 / 82, jog=45)],
                 2: [session_of(16, 82, 400 / 82, jog=30)], 1: [session_of(18, 82, 400 / 82, jog=30)]}
        self.load_weeks(build)
        o = analysis.overview(self.db, self.user)["speed"]
        self.assertFalse(o["weeks"][-2]["work_over"], o["weeks"][-2])

    def test_a_few_seconds_slower_per_rep_counts_much_less(self):
        """18 x 400: at 82 s it's about twice the speed work of 85 s; at critical speed (~87 s) almost none."""
        from apps.trainer import speed
        used = {}
        for secs in (82, 83, 85, 87):
            st = session_of(18, secs, 400 / secs, jog=30)
            used[secs] = speed.d_prime_used(speed.histogram(st["time"]["data"], st["velocity_smooth"]["data"]), 4.576)
        self.assertGreater(used[82], used[83])
        self.assertGreater(used[82], 2 * used[85])
        self.assertLess(used[87], 60)

    def test_reps_with_jogs_are_not_a_best_mile(self):
        """The 400s session's 'best mile' included its 30 s jogs and set critical speed at 6:47. Steady stretches only."""
        from apps.trainer import speed
        st = session_of(18, 82, 400 / 82, jog=30)
        t, v, d = self.parts(st)
        bests = speed.best_efforts(t, d, v)
        self.assertIn("400", bests)                      # one rep is a steady 400
        self.assertGreater(bests["1609"], 480)           # the only steady mile is the warm-up, not reps + jogs (~6:28)
        self.assertNotIn("5000", bests)
        steady = stream([(600, 2.8), (318, 1609.344 / 318), (600, 2.8)])
        self.assertAlmostEqual(speed.best_efforts(*[steady[k]["data"] for k in ("time", "distance", "velocity_smooth")])["1609"], 318, delta=2)

    def test_race_times_set_critical_speed_and_the_line_waits_for_the_weeks_before(self):
        from apps.trainer import analysis, plan, speed
        self.assertEqual(plan.race_seconds("17:39"), 1059)
        self.assertIsNone(plan.race_seconds("fast"))
        store = plan.PlanStore(self.db)
        store.save_settings(race_mile="5:18", race_5k="17:39")
        with self.assertRaises(plan.Invalid):
            store.save_settings(race_5k="soon")
        races = plan.race_bests(store.settings())
        # Only this week's 400s are read (older runs still waiting): critical speed comes from the races, no line yet.
        from datetime import date, timedelta
        monday = date.today() - timedelta(days=date.today().weekday())
        for n, (days_back, st) in enumerate(((0, session_of(18, 82, 400 / 82, jog=30)),
                                             (10, easy_run_with_strides()), (17, easy_run_with_strides()))):
            self.fake.activities[900 + n] = activity(900 + n, day=(monday - timedelta(days=days_back)).isoformat())
            self.fake.streams[900 + n] = st
            self.sync._store(self.user, self.fake.activities[900 + n])
        speed.fill(self.sync, self.user, limit=1)
        o = analysis.overview(self.db, self.user, races=races)["speed"]
        self.assertEqual(o["cs_pace"], "5:52")
        self.assertTrue(o["from_races"])
        self.assertAlmostEqual(o["speed_work"], 450, delta=60)
        self.assertIsNone(o["work_line"])
        self.assertFalse(o["work_over"])
        speed.fill(self.sync, self.user, limit=10)
        o = analysis.overview(self.db, self.user, races=races)["speed"]
        self.assertIsNotNone(o["work_line"])           # all read: the line is back

    def test_critical_speed_works_itself_out_from_a_gps_short_5k(self):
        """Oct 25: a 5K race GPS measured as 4,997 m in 17:52, warm-up and cool-down as separate runs."""
        from datetime import date, timedelta
        from apps.trainer import analysis, speed
        race = stream([(5, 3.0), (1067, 4997.4 / 1067)])
        t, v, d = self.parts(race)
        self.assertAlmostEqual(speed.best_efforts(t, d, v)["5000"], 1073, delta=5)
        monday = date.today() - timedelta(days=date.today().weekday())
        for n, (days_back, st) in enumerate(((30, race), (2, session_of(18, 82, 400 / 82, jog=30)))):
            self.fake.activities[950 + n] = activity(950 + n, day=(monday - timedelta(days=days_back)).isoformat())
            self.fake.streams[950 + n] = st
            self.sync._store(self.user, self.fake.activities[950 + n])
        speed.fill(self.sync, self.user, limit=10)
        o = analysis.overview(self.db, self.user)["speed"]
        self.assertIn(o["cs_pace"], ("5:55", "5:56", "5:57"))
        self.assertEqual(o["d_prime"], 150)
        self.assertIn("from your 5K on", o["cs_how"])

    def real_like_race(self):
        """Like his Oct 25 5K: standing start with a GPS spike, a slow turn, a kick; 4,997 m in 1,076 s."""
        segments = [(1, 0.0), (1, 0.6), (1, 1.3), (1, 2.1), (1, 3.0), (3, 6.9), (10, 5.5), (660, 4.65), (8, 3.7),
                    (380, 4.62), (10, 5.6)]
        st = stream(segments)
        scale = 4997.4 / (st["distance"]["data"][-1] + st["velocity_smooth"]["data"][-1])
        st["distance"]["data"] = [round(d * scale, 1) for d in st["distance"]["data"]]
        st["velocity_smooth"]["data"] = [v * scale for v in st["velocity_smooth"]["data"]]
        return st

    def test_a_real_race_counts_and_an_easy_long_run_does_not(self):
        from datetime import date, timedelta
        from apps.trainer import analysis, speed
        t, v, d = self.parts(self.real_like_race())
        self.assertAlmostEqual(speed.best_efforts(t, d, v)["5000"], 1077, delta=6)   # the slow turn doesn't rule it out
        monday = date.today() - timedelta(days=date.today().weekday())
        long_run = stream([(5400, 3.38)])                                               # Nov 15: a steady 7:56/mi run
        for n, (days_back, st) in enumerate(((10, long_run), (12, easy_run_with_strides()), (5, easy_run_with_strides()))):
            self.fake.activities[980 + n] = activity(980 + n, day=(monday - timedelta(days=days_back)).isoformat())
            self.fake.streams[980 + n] = st
            self.sync._store(self.user, self.fake.activities[980 + n])
        speed.fill(self.sync, self.user, limit=10)
        o = analysis.overview(self.db, self.user)["speed"]
        self.assertEqual(o["cs_pace"], "")                       # no hard effort yet: no number, not a wrong one
        self.fake.activities[990] = activity(990, day=(monday - timedelta(days=31)).isoformat())
        self.fake.streams[990] = self.real_like_race()
        self.sync._store(self.user, self.fake.activities[990])
        speed.fill(self.sync, self.user, limit=10)
        o = analysis.overview(self.db, self.user)["speed"]
        self.assertIn(o["cs_pace"], ("5:56", "5:57", "5:58"))


    def test_a_run_with_no_streams_is_marked_read_not_retried(self):
        """The 0.5 mi trail run on Nov 16 had no GPS: Strava says 404 for its streams. It mustn't hold up the line."""
        from apps.trainer import speed
        self.fake.activities[1001] = activity(1001, day="2026-11-16")
        self.fake.no_streams.add(1001)
        self.sync._store(self.user, self.fake.activities[1001])
        self.assertEqual(speed.waiting_count(self.db, self.user), 1)
        speed.fill(self.sync, self.user, limit=5)
        self.assertEqual(speed.waiting_count(self.db, self.user), 0)
        calls = len(self.fake.calls)
        speed.fill(self.sync, self.user, limit=5)
        self.assertEqual(len(self.fake.calls), calls)      # not asked for again

    def test_the_run_page_and_how_workouts_are_described(self):
        """18 × 400 is named with its rest; its page has the route, the speed chart and the reps."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from apps.trainer import routes, runpage, speed
        st = session_of(18, 82, 400 / 82, jog=30)
        st["heartrate"] = {"data": [150] * len(st["time"]["data"])}
        st["latlng"] = {"data": [[33.3 + i * 1e-5, -111.8 + (i % 400) * 1e-5] for i in range(len(st["time"]["data"]))]}
        self.fake.activities[1100] = activity(1100, day="2026-11-18", name="Threshold")
        self.fake.streams[1100] = st
        self.sync._store(self.user, self.fake.activities[1100])
        speed.fill(self.sync, self.user, limit=5)
        self.assertEqual(speed.recent(self.db, self.user)[0]["summary"], "18 × 400 avg 1:22, 30 s rest")
        r = runpage.detail(self.db, self.user, "1100")
        self.assertEqual(len(r["efforts"]), 18)
        self.assertEqual(r["efforts"][0]["rest_text"], "30 s")
        self.assertTrue(r["map"]["fast"] and r["chart"]["marks"])
        self.assertEqual(r["max_hr"], 150)
        saved = routes._state.get("db")
        routes.configure(self.db)
        self.addCleanup(lambda: routes._state.update(db=saved))
        app = FastAPI()
        app.include_router(routes.router)
        page = TestClient(app).get("/apps/trainer/run/1100")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn("18 × 400 avg 1:22, 30 s rest", page.text)
        self.assertIn("route-fast", page.text)
        self.assertEqual(TestClient(app).get("/apps/trainer/run/999999").status_code, 404)
        # Explore: the map and the graph together, with every moment's time, distance, pace, HR and map spot
        self.assertIn('href="/apps/trainer/run/1100/explore"', page.text)
        ex = runpage.explore(self.db, self.user, "1100")
        samples = ex["samples"]
        self.assertTrue(len(samples["t"]) == len(samples["v"]) == len(samples["x"]) == len(samples["hr"]))
        self.assertLessEqual(len(samples["t"]), runpage.EXPLORE_POINTS)
        rep = max(range(len(samples["t"])), key=lambda i: samples["v"][i])
        self.assertAlmostEqual(samples["v"][rep], 400 / 82, delta=0.4)       # 5-second pace in a rep
        self.assertEqual(len(ex["stretches"]), 18)
        explore_page = TestClient(app).get("/apps/trainer/run/1100/explore")
        self.assertEqual(explore_page.status_code, 200, explore_page.text[:300])
        self.assertIn('id="ex-dot"', explore_page.text)
        self.assertIn("Zoom in", explore_page.text)
        self.assertEqual(TestClient(app).get("/apps/trainer/run/999999/explore").status_code, 404)
        # kept streams: a summary change is worked out again without asking Strava
        calls = len(self.fake.calls)
        self.db.connect().execute("UPDATE tr_run_speed SET version = 0")
        self.assertEqual(speed.refresh(self.db, self.user), 1)
        self.assertEqual(len(self.fake.calls), calls)


    def test_getting_someone_started_one_step_at_a_time(self):
        """A new person: the Overview's background steps import the list first, then read each run's details."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from apps.trainer import routes
        for n, day in enumerate(("2026-11-10", "2026-11-12", "2026-11-14"), start=1200):
            self.fake.activities[n] = activity(n, day=day)
            self.fake.streams[n] = easy_run_with_strides()
        saved = routes._state.get("db")
        routes.configure(self.db)
        self.addCleanup(lambda: routes._state.update(db=saved))
        with unittest.mock.patch.object(routes, "sync", lambda: self.sync):
            app = FastAPI()
            app.include_router(routes.router)
            client = TestClient(app)
            stages = []
            for _ in range(10):
                r = client.post("/apps/trainer/sync/step").json()
                stages.append(r["stage"])
                if r["stage"] == "runs" and r["left"] == 0:
                    break
            self.assertEqual(stages[0], "history")
            self.assertEqual(stages[-1], "runs")
            self.assertEqual(r["left"], 0)
            page = client.get("/apps/trainer").text
            self.assertNotIn('id="getting"', page)  # all in: no background reading


    def test_easy_run_surges_are_not_strides(self):
        """Surges just over the fast mark in an easy run (6:55-6:59/mi) aren't strides; real ones (5:00/mi) are."""
        from apps.trainer import speed
        surgy = stream([(600, 2.85), (20, 3.88), (300, 2.85), (20, 3.9), (600, 2.85)])
        self.assertEqual(speed.find_efforts(*self.parts(surgy), easy=2.85)[0], [])
        real = easy_run_with_strides(fast=5.3)
        self.assertEqual(len(speed.find_efforts(*self.parts(real), easy=2.85)[0]), 5)

    def test_a_week_page_lists_its_runs_with_speed_points(self):
        from datetime import date, timedelta
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from apps.trainer import routes
        monday = date.today() - timedelta(days=date.today().weekday())
        self.load_weeks({1: [session_of(18, 82, 400 / 82, jog=30), easy_run_with_strides(fast=5.3)]})
        saved = routes._state.get("db")
        routes.configure(self.db)
        self.addCleanup(lambda: routes._state.update(db=saved))
        app = FastAPI()
        app.include_router(routes.router)
        client = TestClient(app)
        last = (monday - timedelta(weeks=1)).isoformat()
        page = client.get(f"/apps/trainer/speed/{last}").text
        self.assertIn("18 × 400", page)
        self.assertIn("points", page)
        self.assertIn("Week before", page)
        overview = client.get("/apps/trainer").text
        self.assertIn("Speed points this week", overview)
        self.assertIn(f'href="/apps/trainer/speed/{last}"', overview)
        self.assertNotIn("tanks", overview)
        self.assertEqual(client.get("/apps/trainer/speed/not-a-date").status_code, 404)
        self.assertIn('href="/apps/trainer/speed/how"', overview)
        self.assertIn('href="/apps/trainer/speed/how"', page)

    def test_how_these_numbers_work(self):
        from apps.trainer import speed
        self.load_weeks({1: [easy_run_with_strides(fast=5.3)]})
        e = speed.explain(self.db, self.user, races={"5000": 17 * 60 + 10})
        # A 17:10 5K: threshold pace near 5:40/mi; a 400 a little faster scores a few dozen points, a fast one ~140.
        self.assertTrue(e["cs_pace"])
        steady, fast = e["examples"]["steady"], e["examples"]["fast"]
        self.assertEqual(steady["points"], 400 - steady["pacer"])
        self.assertGreater(fast["points"], 3 * steady["points"])
        self.assertTrue(2 <= e["examples"]["fast_reps"] <= 6)

    def test_hard_stretches_of_any_length(self):
        from datetime import date
        from apps.trainer import speed
        # A steady 25 minutes at 4.6 m/s inside an easy run: its best 24 minutes is about 4.6 x 1440 m.
        t, v, d = self.parts(stream([(600, 2.9), (1500, 4.6), (600, 2.9)]))
        best = speed.best_by_time(t, d)
        self.assertAlmostEqual(best["1440"], 4.6 * 1440, delta=15)
        self.assertNotIn("7200", best)                                   # the run is only 45 minutes
        # 400s with jogs aren't one 20-minute effort; each rep is too short for 2 minutes
        t, v, d = self.parts(session_of(16, 80, 5.0, jog=60))
        reps = speed.best_by_time(t, d)
        self.assertLess(reps.get("1200", 0) / 1200, 4.0)
        # Holding nearly the same pace longer means a faster threshold pace (Curtis's 3 mi at 5:50 vs 4.5 mi at 5:52)
        mi = speed.METERS_PER_MILE
        three = {"m": 3 * mi, "t": 3 * 350, "day": date(2026, 11, 1), "src": "run"}
        longer = {"m": 4.5 * mi, "t": 4.5 * 352, "day": date(2026, 11, 8), "src": "run"}
        cs, _, how = speed.estimate([three, longer])
        self.assertGreater(cs, speed.estimate([three])[0])
        self.assertEqual(how, "from your 4.5 miles at 5:52/mi on Nov 8")
        # Over 40 minutes (a half) counts through the race-time formula, a little slower than its own pace
        half = {"m": 21097.5, "t": 4800, "day": date(2026, 3, 1), "src": "run"}
        cs, _, how = speed.estimate([half])
        self.assertTrue(21097.5 / 4800 < cs < 4.7)
        self.assertIn("13.1 miles", how)

    def test_a_short_course_race_keeps_its_measured_distance(self):
        from apps.trainer import speed
        # 4,855 m in 17:10, like his turkey trot: not "5K in 17:39"
        self.assertEqual(speed.label(4855, 1030, 4855), "hard run")
        self.assertEqual(speed.label(4997, 1076, 5000), "5K")
        text = speed.describe({"kind": "hard run", "items": [{"metres": 4840, "seconds": 1025, "avg_mps": 4.72}],
                               "rest": None})
        self.assertEqual(text, "3 miles in 17:05 (5:41/mi)")

    def test_the_trainer_tools_for_dieters_chat(self):
        from datetime import date, timedelta
        from apps.trainer import connector, routes
        monday = date.today() - timedelta(days=date.today().weekday())
        self.load_weeks({1: [session_of(18, 82, 400 / 82, jog=30), easy_run_with_strides(fast=5.3)]})
        saved = routes._state.get("db")
        routes.configure(self.db)
        self.addCleanup(lambda: routes._state.update(db=saved))
        picture = connector.speed_picture({"weeks": 8})
        self.assertEqual(len(picture["weeks"]), 8)
        self.assertTrue(picture["recent_runs"])
        run_id = picture["recent_runs"][0]["run_id"]
        one = connector.run({"run_id": run_id})
        self.assertEqual(one["runs"][0]["run_id"], run_id)
        self.assertIn("speed_points", one["that_week"])
        day = one["runs"][0]["date"]
        self.assertTrue(connector.run({"date": day})["runs"])
        self.assertEqual(connector.run({"date": (monday + timedelta(days=30)).isoformat()})["runs"], [])
