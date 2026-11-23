"""The main app on Cloud Run: load the database on the first request and save each change before answering, so a
deploy never loses changes (the new version starts minutes before it takes over)."""

import os
import unittest
import warnings

from fastapi.testclient import TestClient

from core import internal

KEY = "test-internal-key-0123456789"


class FakeStorage:
    def __init__(self, fail=False):
        self.enabled, self.fail, self.pending, self.events = True, fail, False, []

    def restore_database(self):
        self.events.append("restore")
        if self.fail:
            raise RuntimeError("storage unreachable")
        return True

    def schedule_backup(self):
        self.pending = True

    def save_pending(self):
        if self.pending:
            self.pending = False
            self.events.append("save")

    def seed_if_empty(self):
        return False

    def flush(self):
        self.events.append("flush")


class MainDatabaseReadyTests(unittest.TestCase):
    def setUp(self):
        self.saved_key = os.environ.get("DIETER_INTERNAL_KEY")
        os.environ["DIETER_INTERNAL_KEY"] = KEY
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        self.api = api
        self.saved = (api.cloud_persistence, api._db_ready["done"], api.db.after_commit)
        self.client = TestClient(api.app)

    def tearDown(self):
        self.api.cloud_persistence, self.api._db_ready["done"], self.api.db.after_commit = self.saved
        if self.saved_key is None:
            os.environ.pop("DIETER_INTERNAL_KEY", None)
        else:
            os.environ["DIETER_INTERNAL_KEY"] = self.saved_key

    def cloud(self, storage):
        self.api.cloud_persistence = storage
        self.api._db_ready["done"] = False  # as if this instance just started on Cloud Run
        return storage

    def test_loads_on_the_first_request_and_saves_before_answering(self):
        storage = self.cloud(FakeStorage())
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(storage.events, ["restore"])
        self.assertIs(self.api.db.after_commit.__self__, storage)  # changes are tracked for upload
        self.client.get("/login")
        self.assertEqual(storage.events.count("restore"), 1)  # once per instance
        email = "persist@example.com"
        row = self.api.db.get_user_by_email(email)
        user_id = row["id"] if row else self.api.db.create_user(email, "Persist", "x", "member")
        storage.events.clear()
        r = self.client.post("/internal/kitchen/lists", headers={internal.HEADER: internal.sign({"id": user_id})},
                             json={"title": "Persist", "lines": ["milk"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(storage.events, ["save"])  # uploaded before the answer came back

    def test_a_failed_load_answers_try_again_and_never_uploads(self):
        storage = self.cloud(FakeStorage(fail=True))
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 503)
        self.assertFalse(self.api._db_ready["done"])
        self.api.flush_cloud_backup_on_shutdown()
        self.assertNotIn("flush", storage.events)
        storage.fail = False
        self.assertEqual(self.client.get("/login").status_code, 200)  # the next request tries again


if __name__ == "__main__":
    unittest.main()
