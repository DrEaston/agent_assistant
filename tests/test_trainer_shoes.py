"""Shoes and the after-run check-in (apps/trainer/shoes.py)."""

import tempfile
import unittest
import warnings
from datetime import date, timedelta
from pathlib import Path

from database import Database, reset_current_user_id, set_current_user_id

from apps.trainer import shoes


class ShoeStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(str(Path(self.tmp.name) / "shoes.db"))
        self.db.init()
        self.user = self.db.create_user("runner@example.com", "Runner", "x", "member")
        self.other = self.db.create_user("other@example.com", "Other", "x", "member")
        self.token = set_current_user_id(self.user)
        self.store = shoes.ShoeStore(self.db)

    def tearDown(self):
        reset_current_user_id(self.token)
        if self.db.conn is not None:
            self.db.conn.close()
        self.tmp.cleanup()

    def run_on(self, days_ago, metres=10000, external_id=None, user=None, kind="Run"):
        day = (date.today() - timedelta(days=days_ago)).isoformat()
        conn = self.db.connect()
        cur = conn.execute("INSERT INTO trainer_imported_workouts (source, external_id, user_id, activity_type, title, "
                           "started_at, distance_meters) VALUES ('strava', ?, ?, ?, ?, ?, ?)",
                           (external_id or f"{days_ago}{metres}{kind}", user or self.user, kind, f"Run {days_ago}",
                            f"{day}T07:00:00", metres))
        conn.commit()
        return cur.lastrowid

    def test_new_runs_wait_for_the_check_in_until_answered_or_skipped(self):
        old = self.run_on(30)
        ride = self.run_on(1, kind="Ride")
        a, b = self.run_on(2), self.run_on(0, metres=1609.344 * 6)
        self.run_on(0, user=self.other, external_id="theirs")
        self.assertEqual([r["workout_id"] for r in self.store.waiting()], [b, a])   # newest first; not old, not rides
        self.assertNotIn(old, [r["workout_id"] for r in self.store.waiting()])
        self.assertNotIn(ride, [r["workout_id"] for r in self.store.waiting()])
        self.assertEqual(self.store.waiting()[0]["miles"], 6.0)
        peg = self.store.add("Pegasus 41", "Nike", "Pegasus", "100")
        fly = self.store.add("Vaporfly")
        self.store.save(b, peg, "7", "Calf a bit tight")
        self.store.skip(a)
        self.assertEqual(self.store.waiting(), [])
        self.assertEqual(self.store.count_waiting(), 0)
        saved = self.store.for_run(b)
        self.assertEqual((saved["shoe"], saved["feel"], saved["feel_label"], saved["note"]),
                         ("Pegasus 41", 7, "hard", "Calf a bit tight"))
        # Saving again moves the run to the other pair, keeps how it felt
        self.store.save(b, fly, "", "")
        self.assertEqual(self.store.for_run(b)["shoe"], "Vaporfly")
        self.assertEqual(self.store.for_run(b)["feel"], 7)
        miles = {s["name"]: s["miles"] for s in self.store.shoes()}
        self.assertEqual(miles, {"Vaporfly": 6.0, "Pegasus 41": 100.0})
        self.assertEqual(self.store.shoes()[0]["name"], "Vaporfly")          # the pair worn last comes first

    def test_shoe_rules(self):
        peg = self.store.add("Pegasus 41", start_miles="395")
        with self.assertRaises(shoes.Invalid):
            self.store.add("Pegasus 41")
        with self.assertRaises(shoes.Invalid):
            self.store.add("")
        run = self.run_on(0, metres=1609.344 * 6)
        self.store.save(run, peg)
        self.assertTrue(self.store.shoes()[0]["replace"])                     # past REPLACE_MILES
        self.store.retire(peg)
        self.assertEqual(self.store.shoes(include_retired=False), [])
        self.assertFalse(self.store.shoes()[0]["replace"])
        with self.assertRaises(shoes.Invalid):
            self.store.save(run, peg, "11")
        theirs = self.run_on(0, user=self.other, external_id="theirs")
        with self.assertRaises(shoes.Invalid):
            self.store.save(theirs, peg)                                      # not his run
        token = set_current_user_id(self.other)
        try:
            with self.assertRaises(shoes.Invalid):
                self.store.retire(peg)                                        # not their pair
        finally:
            reset_current_user_id(token)


class ShoePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        cls.api = api

    def test_the_shoes_page_and_a_one_tap_check_in(self):
        import secrets
        import uuid
        from datetime import datetime
        from fastapi.testclient import TestClient
        db = self.api.db
        user = db.create_user(f"shoes-{uuid.uuid4().hex[:8]}@example.com", "Shoe Runner", "x", "member")
        conn = db.connect()
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (user,))
        cur = conn.execute("INSERT INTO trainer_imported_workouts (source, external_id, user_id, activity_type, title, "
                           "started_at, distance_meters) VALUES ('strava', ?, ?, 'Run', 'Easy loop', ?, 8000)",
                           (uuid.uuid4().hex[:10], user, date.today().isoformat() + "T07:00:00"))
        conn.commit()
        workout = cur.lastrowid
        session = secrets.token_urlsafe(16)
        db.create_session(user, self.api.hash_session_token(session), (datetime.utcnow() + timedelta(days=1)).isoformat())
        client = TestClient(self.api.app)
        client.cookies.set(self.api.SESSION_COOKIE_NAME, session)
        client.post("/apps/trainer/shoes/add", data={"name": "Ghost 16", "start_miles": "20"})
        page = client.get("/apps/trainer/shoes").text
        self.assertIn("Ghost 16", page)
        self.assertIn("Easy loop", page)                                       # waiting for the check-in
        self.assertIn("After your run", client.get("/apps/trainer").text)
        shoe_id = page.split('name="shoe_id" value="')[1].split('"')[0]
        client.post(f"/apps/trainer/after-run/{workout}", data={"shoe_id": shoe_id, "feel": "3", "next": "/apps/trainer/shoes"})
        page = client.get("/apps/trainer/shoes").text
        self.assertNotIn("Easy loop", page)
        self.assertIn("25.0 mi", page)                                         # 20 + 5.0 (8 km)
        self.assertEqual(client.get("/apps/trainer/history").status_code, 404)   # the old pages are gone
        self.assertEqual(client.get("/apps/trainer/imports").status_code, 404)


if __name__ == "__main__":
    unittest.main()
