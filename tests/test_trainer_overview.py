"""Trainer's Overview (apps/trainer/analysis.py): weekly miles, fitness/fatigue/form, speed, and the week explained."""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from apps.trainer import analysis, plan, strava
from database import Database, reset_current_user_id, set_current_user_id

TODAY = date(2026, 11, 25)  # a Wednesday


def run(n, day, miles=6.0, minutes=48, hr=145.0, max_speed=6.0, name="Run"):
    return {"id": n, "type": "Run", "sport_type": "Run", "name": name, "start_date": f"{day}T13:00:00Z",
            "start_date_local": f"{day}T06:00:00Z", "distance": miles * strava.METERS_PER_MILE,
            "moving_time": minutes * 60, "average_heartrate": hr, "max_heartrate": 180.0, "max_speed": max_speed,
            "average_speed": miles * strava.METERS_PER_MILE / (minutes * 60)}


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(str(Path(self.tmp.name) / "o.db"))
        self.db.init()
        self.user = self.db.create_user("runner@example.com", "Runner", "x", "member")
        self.token = set_current_user_id(self.user)
        self.sync = strava.Sync(self.db)

    def tearDown(self):
        reset_current_user_id(self.token)
        if self.db.conn is not None:
            self.db.conn.close()
        self.tmp.cleanup()

    def test_weekly_miles_fitness_and_speed(self):
        n = 0
        for back in range(0, 140):
            day = TODAY - timedelta(days=back)
            if day.weekday() == 4:
                continue  # Fridays off
            n += 1
            faster = day >= plan.week_start(TODAY) - timedelta(weeks=3)  # the last 4 weeks: quicker, more efficient
            self.sync._store(self.user, run(n, day, max_speed=7.0 if faster else 6.0,
                                            hr=140.0 if faster else 150.0), None)
        o = analysis.overview(self.db, self.user, miles_target=40, today=TODAY)
        self.assertEqual(len(o["miles"]), 16)
        self.assertEqual(o["miles"][-2]["miles"], 36.0)  # last full week: 6 runs of 6 mi
        self.assertTrue(o["miles"][-1]["current"])
        self.assertGreater(o["fitness"]["fitness"], 0)
        self.assertIn(o["fitness"]["form_label"], ("fresh", "balanced", "building (tired)", "very fatigued"))
        self.assertTrue(o["speed"]["faster"])
        self.assertTrue(o["speed"]["efficiency_up"])
        self.assertEqual(o["speed"]["top_pace_recent"], "3:50")  # 7 m/s

    def test_load_with_and_without_heart_rate(self):
        hard = analysis.load({"moving_time_seconds": 3600, "average_heartrate": 170}, 190)
        easy = analysis.load({"moving_time_seconds": 3600, "average_heartrate": 130}, 190)
        self.assertGreater(hard, easy)
        self.assertGreater(analysis.load({"moving_time_seconds": 3600, "workout_category": "run_threshold"}, 190),
                           analysis.load({"moving_time_seconds": 3600, "workout_category": "pilates"}, 190))

    def test_the_week_explained(self):
        store = plan.PlanStore(self.db)
        start = plan.week_start(TODAY)
        store.fill_week(start)
        self.sync._store(self.user, run(1, start + timedelta(days=1), miles=8.0), None)  # Tuesday
        lines = analysis.week_guidance(store.week_view(start, today=TODAY), today=TODAY)
        self.assertTrue(lines[0].startswith("8.0 of 40 miles so far"))
        self.assertTrue(any("Tuesday's hard workout isn't checked off (Strava has a run that day)" in l for l in lines))
        self.assertTrue(any(l.startswith("Strides 0 of 2") for l in lines))


if __name__ == "__main__":
    unittest.main()
