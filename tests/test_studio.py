"""Dieter Studio: an inbox of problem reports that Claude reads and closes through the connector."""

import json
import tempfile
import unittest
import warnings
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.issues import connector, routes
from apps.issues.store import StudioStore
from core.connector.tools import ToolError
from database import Database, reset_current_user_id, set_current_user_id


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(str(Path(self.tmp.name) / "studio.db"))
        self.db.init()
        self.saved = dict(routes._state)
        routes.configure(self.db, require_admin=lambda request: None,
                         summarize=lambda text, page_url="", page_title="": {"title": "Drafted title", "area": "Kitchen / Recipes"},
                         areas=("Kitchen / Recipes", "Trainer"))
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def tearDown(self):
        routes._state.clear()
        routes._state.update(self.saved)
        if self.db.conn:
            self.db.conn.close()
        self.tmp.cleanup()

    def add_report(self, text="The timer didn't ring.", **fields):
        return self.db.add_app_feedback_report(title=fields.get("title", "Timer"), area="Kitchen / Recipes",
                                               page_url="/apps/recipes", page_title="Kitchen", reporter_name="Curtis",
                                               reporter_email="", raw_feedback=text)

    def test_old_codex_runs_are_copied_onto_their_report_once(self):
        report_id = self.add_report()
        conn = self.db.connect()
        for status, note in (("failed", "Codex not found"), ("ready_for_testing", "Added the timer chime.")):
            conn.execute("INSERT INTO app_feedback_codex_runs (report_id, status, plan, result_note) VALUES (?, ?, 'plan', ?)",
                         (report_id, status, note))
        conn.commit()
        store = StudioStore(self.db)
        self.assertEqual(store.archive_codex_runs(), 1)
        self.assertEqual(store.archive_codex_runs(), 0)
        history = store.report(report_id)["codex_history"]
        self.assertEqual([r["status"] for r in history], ["failed", "ready_for_testing"])
        page = self.client.get(f"/apps/issues/{report_id}")
        self.assertIn("Earlier Codex attempts (2)", page.text)
        self.assertIn("Added the timer chime.", page.text)

    def test_heartbeat_table_is_gone(self):
        row = self.db.connect().execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'app_feedback_codex_worker_heartbeats'").fetchone()
        self.assertIsNone(row)

    def test_inbox_create_status_and_delete(self):
        r = self.client.post("/apps/issues/create", data={"text": "Make the grocery list bigger", "page": "/apps/recipes"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        report_id = int(r.headers["location"].rsplit("/", 1)[-1])
        report = StudioStore(self.db).report(report_id)
        self.assertEqual((report["title"], report["area"]), ("Drafted title", "Kitchen / Recipes"))
        self.assertIn("Drafted title", self.client.get("/apps/issues").text)
        page = self.client.get(f"/apps/issues/{report_id}")
        self.assertIn("Studio reports", page.text)  # how to ask Claude about it
        self.assertNotIn("/send", page.text)
        self.client.post(f"/apps/issues/{report_id}/status", data={"status": "done"})
        self.assertEqual(StudioStore(self.db).report(report_id)["status"], "done")
        self.assertIn("Drafted title", self.client.get("/apps/issues?show=done").text)
        self.assertEqual(self.client.post(f"/apps/issues/{report_id}/status", data={"status": "bogus"}).status_code, 400)
        self.client.post(f"/apps/issues/{report_id}/delete")
        self.assertIsNone(StudioStore(self.db).report(report_id))

    def test_old_github_link_still_shows(self):
        report_id = self.add_report()
        conn = self.db.connect()
        conn.execute("UPDATE app_feedback_reports SET github_issue_url = ?, status = 'sent' WHERE id = ?",
                     ("https://github.com/DrEaston/agent_assistant/issues/9", report_id))
        conn.commit()
        page = self.client.get(f"/apps/issues/{report_id}").text
        self.assertIn("Old GitHub issue", page)
        self.assertIn("With Claude", page)

    def test_claude_reads_and_closes_reports_as_admin_only(self):
        report_id = self.add_report()
        admin = self.db.create_user("curtis@example.com", "Curtis", "x", "admin")
        token = set_current_user_id(admin)
        try:
            listed = connector.list_reports({})["reports"]
            self.assertEqual([(r["id"], r["title"], r["page"]) for r in listed], [(report_id, "Timer", "/apps/recipes")])
            report = connector.get_report({"report_id": report_id})
            self.assertEqual(report["text"], "The timer didn't ring.")
            done = connector.set_status({"report_id": report_id, "status": "done", "note": "Timer chimes now."})
            self.assertEqual(done["status"], "Done")
            self.assertEqual(connector.list_reports({})["reports"], [])
            self.assertEqual(StudioStore(self.db).report(report_id)["history"][-1]["summary"], "Timer chimes now.")
            with self.assertRaises(ToolError):
                connector.set_status({"report_id": report_id, "status": "sent"})
            with self.assertRaises(ToolError):
                connector.get_report({"report_id": 9999})
        finally:
            reset_current_user_id(token)
        member = self.db.create_user("guest@example.com", "Guest", "x", "member")
        token = set_current_user_id(member)
        try:
            with self.assertRaises(ToolError):
                connector.list_reports({})
        finally:
            reset_current_user_id(token)

    def test_old_addresses_redirect(self):
        for old, new in (("/apps/assistant/feedback", "/apps/issues"), ("/apps/assistant/feedback/7", "/apps/issues/7"),
                         ("/apps/issues/workers", "/apps/issues")):
            r = self.client.get(old, follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (303, new))


class CodexRetiredTests(unittest.TestCase):
    def test_main_app_has_no_codex_worker_routes(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        paths = {getattr(r, "path", "") for r in api.app.routes}
        self.assertFalse([p for p in paths if p.startswith("/api/app-feedback")])
        self.assertNotIn("/apps/issues/{report_id}/send", paths)  # GitHub sending is retired too
        self.assertFalse(Path(api.__file__).with_name("scripts").joinpath("run_codex_feedback_worker.py").exists())


if __name__ == "__main__":
    unittest.main()
