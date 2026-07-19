import tempfile
import unittest
from pathlib import Path

from database import Database, reset_current_user_id, set_current_user_id


class MemberApprovalTests(unittest.TestCase):
    def close_db(self, db):
        if db.conn is not None:
            db.conn.close()
            db.conn = None

    def test_new_user_can_be_created_pending_and_approved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            try:
                db.init()

                user_id = db.create_user(
                    "new@example.com",
                    "New Member",
                    "hash",
                    role="user",
                    status="pending",
                    requested_trainer_mode="coach",
                )
                user = dict(db.get_user_by_id(user_id))
                self.assertEqual(user["status"], "pending")
                self.assertEqual(user["requested_trainer_mode"], "coach")

                self.assertTrue(db.update_user_status(user_id, "active"))
                self.assertTrue(db.update_trainer_mode_for_user(user_id, user["requested_trainer_mode"]))
                approved = dict(db.get_user_by_id(user_id))
                self.assertEqual(approved["status"], "active")
                profile = dict(db.get_trainer_profile(user_id))
                self.assertEqual(profile["mode"], "coach")
            finally:
                self.close_db(db)

    def test_admin_member_list_puts_pending_users_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            try:
                db.init()

                db.create_user("active@example.com", "Active", "hash", status="active")
                pending_id = db.create_user("pending@example.com", "Pending", "hash", status="pending")

                users = [dict(user) for user in db.get_users_for_admin()]
                self.assertEqual(users[0]["id"], pending_id)
                self.assertEqual(users[0]["status"], "pending")
            finally:
                self.close_db(db)

    def test_user_role_can_be_promoted_and_demoted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            try:
                db.init()

                user_id = db.create_user("member@example.com", "Member", "hash", role="user", status="active")
                self.assertTrue(db.update_user_role(user_id, "admin"))
                admin = dict(db.get_user_by_id(user_id))
                self.assertEqual(admin["role"], "admin")

                self.assertTrue(db.update_user_role(user_id, "user"))
                member = dict(db.get_user_by_id(user_id))
                self.assertEqual(member["role"], "user")
            finally:
                self.close_db(db)

    def test_admin_can_override_requested_coach_to_athlete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            try:
                db.init()

                user_id = db.create_user(
                    "coach-request@example.com",
                    "Coach Request",
                    "hash",
                    status="pending",
                    requested_trainer_mode="coach",
                )
                self.assertTrue(db.update_trainer_mode_for_user(user_id, "athlete"))
                profile = dict(db.get_trainer_profile(user_id))
                self.assertEqual(profile["mode"], "athlete")
                self.assertEqual(dict(db.get_user_by_id(user_id))["requested_trainer_mode"], "coach")
            finally:
                self.close_db(db)

    def test_active_coaches_are_searchable_by_name_or_email(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            try:
                db.init()

                athlete_id = db.create_user("athlete@example.com", "Active Athlete", "hash", status="active")
                coach_id = db.create_user(
                    "jane.coach@example.com",
                    "Jane Fast",
                    "hash",
                    status="active",
                    requested_trainer_mode="coach",
                )
                db.update_trainer_mode_for_user(coach_id, "coach")

                noncoach_id = db.create_user("jane.athlete@example.com", "Jane Athlete", "hash", status="active")
                db.update_trainer_mode_for_user(noncoach_id, "athlete")

                pending_coach_id = db.create_user(
                    "pending.coach@example.com",
                    "Pending Coach",
                    "hash",
                    status="pending",
                    requested_trainer_mode="coach",
                )
                db.update_trainer_mode_for_user(pending_coach_id, "coach")

                token = set_current_user_id(athlete_id)
                try:
                    name_matches = [dict(user) for user in db.search_trainer_coaches("jane")]
                    email_matches = [dict(user) for user in db.search_trainer_coaches("COACH@EXAMPLE")]
                finally:
                    reset_current_user_id(token)

                self.assertEqual([coach_id], [user["id"] for user in name_matches])
                self.assertEqual([coach_id], [user["id"] for user in email_matches])
                self.assertEqual("Jane Fast", name_matches[0]["display_name"])
                self.assertEqual("jane.coach@example.com", name_matches[0]["email"])
            finally:
                self.close_db(db)


if __name__ == "__main__":
    unittest.main()
