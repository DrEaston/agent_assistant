"""Trainer's plan (apps/trainer): the drafted week, sessions, habits, the Sunday check-in and the calendar feed."""

import re
import secrets
import tempfile
import unittest
import uuid
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from apps.trainer import plan
from database import Database, reset_current_user_id, set_current_user_id

MONDAY = date(2026, 11, 30)


class DraftTests(unittest.TestCase):
    def test_his_usual_week(self):
        week = plan.draft_week(plan.DEFAULTS, MONDAY)
        runs = [s for s in week if s["kind"] in plan.RUN_KINDS]
        self.assertEqual(len(runs), 7)
        self.assertAlmostEqual(sum(s["miles"] for s in runs), 40, delta=1)
        workout = [s for s in week if s["kind"] == "workout"]
        self.assertEqual([(s["day"], s["slot"]) for s in workout], [("2026-12-01", "16:00")])  # Tuesday afternoon
        self.assertEqual(sum(1 for s in week if s["kind"] == "weights"), 2)
        self.assertEqual(sum(1 for s in week if s["kind"] == "pilates"), 2)
        stride_days = {s["day"] for s in week if s["strides"]}
        self.assertEqual(len(stride_days), 2)
        self.assertNotIn("2026-11-30", stride_days)  # not the day before the hard workout
        gym_days = {s["day"] for s in week if s["kind"] in ("weights", "pilates")}
        self.assertFalse(gym_days & {"2026-12-01", "2026-12-05"})  # not the workout or long-run day

    def test_it_follows_his_settings(self):
        week = plan.draft_week(dict(plan.DEFAULTS, miles=30, workout_day="wed", long_day="sun", rest_day="fri",
                                    weights=1, pilates=1, strides=1), MONDAY)
        self.assertEqual([s["day"] for s in week if s["kind"] == "workout"], ["2026-12-02"])
        self.assertEqual([s["day"] for s in week if s["kind"] == "rest"], ["2026-12-04"])
        self.assertEqual(sum(1 for s in week if s["kind"] in ("weights", "pilates")), 2)
        self.assertAlmostEqual(sum(s["miles"] or 0 for s in week), 30, delta=1)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(str(Path(self.tmp.name) / "trainer.db"))
        self.db.init()
        self.user = self.db.create_user("runner@example.com", "Runner", "x", "member")
        self.token = set_current_user_id(self.user)
        self.store = plan.PlanStore(self.db)

    def tearDown(self):
        reset_current_user_id(self.token)
        if self.db.conn is not None:
            self.db.conn.close()
        self.tmp.cleanup()

    def test_fill_check_off_and_count(self):
        sessions = self.store.fill_week(MONDAY)
        self.assertEqual(self.store.fill_week(MONDAY), sessions)  # never doubles a planned week
        workout = next(s for s in sessions if s["kind"] == "workout")
        self.store.update_session(workout["id"], status="done")
        easy = next(s for s in sessions if s["strides"])
        self.store.update_session(easy["id"], strides_done=1)
        self.store.set_habit("2026-12-01", add_water=8, fueled=True)
        totals = self.store.week_view(MONDAY, today=date(2026, 12, 1))["totals"]
        self.assertEqual(totals["workout"], (1, 1))
        self.assertEqual(totals["strides"], (1, 2))
        self.assertEqual(totals["water_days"], 1)

    def test_redraft_keeps_what_is_done(self):
        sessions = self.store.fill_week(MONDAY)
        self.store.update_session(sessions[0]["id"], status="done")
        again = self.store.fill_week(MONDAY, replace=True)
        self.assertEqual(sum(1 for s in again if s["done"]), 1)
        self.assertEqual(len(again), len(sessions) + 1)  # the done one stays; the rest are drafted again

    def test_strava_runs_count_toward_the_week(self):
        conn = self.db.connect()
        conn.execute("""CREATE TABLE IF NOT EXISTS trainer_imported_workouts (id INTEGER PRIMARY KEY, source TEXT,
                        external_id TEXT, user_id INTEGER, activity_type TEXT, title TEXT, started_at TEXT,
                        distance_meters REAL, moving_time_seconds INTEGER)""")
        conn.execute("INSERT INTO trainer_imported_workouts (source, external_id, user_id, activity_type, title, "
                     "started_at, distance_meters, moving_time_seconds) VALUES ('strava', '1', ?, 'Run', "
                     "'Morning Run', '2026-12-01T13:00:00Z', 16093.44, 3600)", (self.user,))
        conn.commit()
        view = self.store.week_view(MONDAY)
        self.assertEqual(view["totals"]["miles_run"], 10.0)
        self.assertEqual(view["days"][1]["strava"][0]["title"], "Morning Run")

    def test_sunday_check_in(self):
        self.store.save_week(MONDAY, miles_target="42", goals=["strides twice", "", "fuel before Tuesday"],
                             checked_in=True)
        week = self.store.week(MONDAY)
        self.assertEqual((week["miles_target"], week["goals"]), (42.0, ["strides twice", "fuel before Tuesday"]))
        self.assertTrue(week["checked_in_at"])

    def test_settings_are_checked(self):
        self.store.save_settings(miles="45", weights="1")
        self.assertEqual((self.store.settings()["miles"], self.store.settings()["weights"]), (45.0, 1))
        with self.assertRaises(plan.Invalid):
            self.store.save_settings(long_day="tue")  # the hard-workout day

    def test_other_people_dont_see_my_sessions(self):
        mine = self.store.fill_week(MONDAY)
        other = self.db.create_user("someone@example.com", "Someone", "x", "member")
        token = set_current_user_id(other)
        try:
            self.assertEqual(self.store.sessions(MONDAY), [])
            with self.assertRaises(plan.Invalid):
                self.store.update_session(mine[0]["id"], status="done")
        finally:
            reset_current_user_id(token)

    def test_calendar_feed(self):
        self.store.fill_week(MONDAY)
        ics = plan.calendar(self.store.sessions(MONDAY), weeks=[MONDAY.isoformat()], host="askdieter.ai",
                            now=datetime(2026, 11, 25, 7, 0))
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n"))
        self.assertIn("DTSTART:20261201T160000", ics)  # Tuesday's workout at 4 pm, no time zone
        self.assertIn("TRIGGER:-PT30M", ics)
        self.assertIn("SUMMARY:Set the week (Dieter Trainer)", ics)
        self.assertEqual(ics.count("BEGIN:VEVENT"), 12)  # 7 runs + 2 weights + 2 Pilates + the check-in
        self.assertTrue(all(len(line.encode()) <= 75 for line in ics.split("\r\n")))


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        cls.api, cls.db = api, api.db
        cls.user_id = cls.db.create_user(f"trainer-{uuid.uuid4().hex[:8]}@example.com", "Trainer User", "x", "member")
        conn = cls.db.connect()
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (cls.user_id,))
        conn.commit()

    def setUp(self):
        self.token = set_current_user_id(self.user_id)
        self.client = TestClient(self.api.app)
        session = secrets.token_urlsafe(16)
        self.db.create_session(self.user_id, self.api.hash_session_token(session),
                               (datetime.utcnow() + timedelta(days=1)).isoformat())
        self.client.cookies.set(self.api.SESSION_COOKIE_NAME, session)

    def tearDown(self):
        reset_current_user_id(self.token)

    def test_this_week_draft_check_off_and_change(self):
        week = (MONDAY + timedelta(days=7 * 52)).isoformat()  # a week nothing else touches
        page = self.client.get(f"/apps/trainer?week={week}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Draft it from my usual week", page.text)
        self.client.post("/apps/trainer/week/fill", data={"week": week})
        page = self.client.get(f"/apps/trainer?week={week}").text
        self.assertIn("Hard workout", page)
        session_id = re.search(r'/apps/trainer/sessions/(\d+)"', page).group(1)
        self.client.post("/apps/trainer/check", data={"key": f"s-{session_id}", "done": "1", "week": week})
        self.client.post(f"/apps/trainer/sessions/{session_id}", data={
            "day": week, "kind": "easy", "title": "Shakeout", "miles": "5", "slot": "07:00", "status": "done"})
        page = self.client.get(f"/apps/trainer/sessions/{session_id}")
        self.assertIn("Shakeout", page.text)

    def test_overview_is_the_landing_page(self):
        page = self.client.get("/apps/trainer")
        self.assertEqual(page.status_code, 200)
        for part in ("Aerobic fitness", "Speed fitness", "Miles per week", "This week"):
            self.assertIn(part, page.text)
        self.assertEqual(self.client.get("/apps/trainer/week").status_code, 200)

    def test_check_in_and_usual_week_pages(self):
        self.assertEqual(self.client.get("/apps/trainer/checkin").status_code, 200)
        self.assertEqual(self.client.get("/apps/trainer/usual").status_code, 200)
        self.client.post("/apps/trainer/usual", data={"miles": "40", "workout_day": "tue", "workout_time": "16:00",
                                                      "long_day": "sat", "weights": "2", "pilates": "2",
                                                      "strides": "2", "water_goal": "8"})
        self.assertEqual(self.client.get("/apps/trainer/history").status_code, 404)  # the older pages are gone

    def test_calendar_feed_needs_only_the_link(self):
        self.client.get("/apps/trainer/usual")
        token = plan.PlanStore(self.db).calendar_token()
        signed_out = TestClient(self.api.app)
        feed = signed_out.get(f"/trainer-calendar/{token}.ics")
        self.assertEqual(feed.status_code, 200)
        self.assertTrue(feed.headers["content-type"].startswith("text/calendar"))
        self.assertEqual(signed_out.get("/trainer-calendar/" + "x" * 30 + ".ics").status_code, 404)
        self.assertNotEqual(signed_out.get("/apps/trainer", follow_redirects=False).status_code, 200)  # pages need sign-in


if __name__ == "__main__":
    unittest.main()
