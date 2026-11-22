"""Dieter Studio after the Codex pipeline: an inbox that sends requests to Claude Code via GitHub issues."""

import json
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.issues import github, routes
from apps.issues.store import StudioStore
from database import Database


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
        with patch.object(github, "token", return_value=""):
            r = self.client.post("/apps/issues/create", data={"text": "Make the grocery list bigger", "page": "/apps/recipes"},
                                 follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            report_id = int(r.headers["location"].rsplit("/", 1)[-1])
            report = StudioStore(self.db).report(report_id)
            self.assertEqual((report["title"], report["area"]), ("Drafted title", "Kitchen / Recipes"))
            home = self.client.get("/apps/issues")
            self.assertIn("Drafted title", home.text)
            self.assertIn("GitHub isn't connected yet", home.text)
            page = self.client.get(f"/apps/issues/{report_id}")
            self.assertNotIn('action="/apps/issues/%d/send"' % report_id, page.text)
        self.client.post(f"/apps/issues/{report_id}/status", data={"status": "done"})
        self.assertEqual(StudioStore(self.db).report(report_id)["status"], "done")
        self.assertIn("Drafted title", self.client.get("/apps/issues?show=done").text)
        self.assertEqual(self.client.post(f"/apps/issues/{report_id}/status", data={"status": "bogus"}).status_code, 400)
        self.client.post(f"/apps/issues/{report_id}/delete")
        self.assertIsNone(StudioStore(self.db).report(report_id))

    def test_send_to_claude_opens_one_github_issue(self):
        report_id = self.add_report()
        opened = []

        def fake_create(report, client=None):
            opened.append(github.issue_body(report))
            return "https://github.com/DrEaston/agent_assistant/issues/101"

        with patch.object(github, "token", return_value="t"), patch.object(github, "create_issue", fake_create), \
                patch.object(github, "issue_state", return_value={"state": "open", "comments": 2}):
            r = self.client.post(f"/apps/issues/{report_id}/send", follow_redirects=False)
            self.assertEqual(r.headers["location"], f"/apps/issues/{report_id}?sent=1")
            self.client.post(f"/apps/issues/{report_id}/send")  # a second tap doesn't open another issue
            page = self.client.get(f"/apps/issues/{report_id}?sent=1")
        self.assertEqual(len(opened), 1)
        self.assertIn("@claude", opened[0])
        self.assertIn("The timer didn't ring.", opened[0])
        self.assertIn("https://askdieter.ai/apps/recipes", opened[0])
        report = StudioStore(self.db).report(report_id)
        self.assertEqual(report["status"], "sent")
        self.assertEqual(report["history"][-1]["action"], "sent_to_claude")
        self.assertIn("Open on GitHub", page.text)
        self.assertIn("2 replies", page.text)

    def test_github_failure_is_shown_not_raised(self):
        report_id = self.add_report()

        def broken(report, client=None):
            raise RuntimeError("401")

        with patch.object(github, "token", return_value="t"), patch.object(github, "create_issue", broken):
            r = self.client.post(f"/apps/issues/{report_id}/send")
        self.assertIn("GitHub didn", r.text)
        self.assertEqual(StudioStore(self.db).report(report_id)["github_issue_url"], "")

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
        self.assertIn("/apps/issues/{report_id}/send", paths)
        self.assertFalse(Path(api.__file__).with_name("scripts").joinpath("run_codex_feedback_worker.py").exists())


if __name__ == "__main__":
    unittest.main()
