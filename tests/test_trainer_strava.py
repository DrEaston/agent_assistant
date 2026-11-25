"""Strava, live (apps/trainer/strava.py): the webhook, handling events, the whole-history import, and his own log."""

import tempfile
import unittest
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
        self.calls, self.activities, self.subscriptions, self.fail = [], {}, [], None

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
        self.assertTrue(self.sync.status()["history_done"])
        self.assertEqual(sum(1 for e in self.entries() if not e["has_detail"]), 4)
        self.assertEqual(self.sync.fill_details(self.user, limit=3), 3)
        self.assertEqual(sum(1 for e in self.entries() if not e["has_detail"]), 1)
        self.assertEqual(next(e for e in self.entries() if e["date"] == "2026-11-20")["title"], "Already here")

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
                                                       "title": "Reformer class", "minutes": "50"})
            page = client.get("/apps/trainer/log")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Reformer class", page.text)
            exported = client.get("/apps/trainer/log.csv")
            self.assertIn("Reformer class", exported.text)
            self.assertTrue(exported.text.startswith("date,time,source,type"))
            self.assertEqual(client.post("/apps/trainer/sync/live").status_code, 403)  # admin only
        finally:
            reset_current_user_id(token)


if __name__ == "__main__":
    unittest.main()
