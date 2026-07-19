import unittest
from types import SimpleNamespace
from unittest.mock import patch

import api


def admin_request(path="/apps/issues"):
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        query_params={},
        state=SimpleNamespace(current_user={"id": 1, "role": "admin", "status": "active"}),
    )


class IssueBoardOverviewTests(unittest.TestCase):
    def test_board_overview_counts_and_active_issue(self):
        reports = [
            {"id": 1, "title": "Draft issue", "status": "open", "area": "Dieter", "codex_run_status": ""},
            {"id": 2, "title": "Running issue", "status": "in_progress", "area": "Issues", "codex_run_status": "running"},
            {"id": 3, "title": "Ready issue", "status": "ready_for_review", "area": "Trainer", "codex_run_status": "ready_for_testing"},
            {"id": 4, "title": "Failed issue", "status": "in_progress", "area": "Kitchen / Recipes", "codex_run_status": "failed"},
        ]

        overview = api.app_feedback_board_overview(reports, {"worker_listener_recent": True})

        self.assertEqual(1, overview["counts"]["open"])
        self.assertEqual(2, overview["counts"]["in_progress"])
        self.assertEqual(1, overview["counts"]["running"])
        self.assertEqual(1, overview["counts"]["failed"])
        self.assertEqual("Running issue", overview["active_issue"]["title"])
        self.assertEqual("Ready issue", overview["ready_issue"]["title"])
        self.assertEqual("Failed issue", overview["failed_issue"]["title"])

    def test_worker_claim_selector_filters_by_project_lane(self):
        rows = [
            {"id": 11, "report_area": "Zombie Game / Gameplay", "report_title": "Tune zombie movement"},
            {"id": 12, "report_area": "Kitchen / Recipes", "report_title": "Fix bake mode"},
        ]

        self.assertEqual(12, api.select_next_codex_run_for_project(rows, "dieter")["id"])
        self.assertEqual(11, api.select_next_codex_run_for_project(rows, "zombie_game")["id"])
        self.assertIsNone(api.select_next_codex_run_for_project(rows, "eeg"))

    def test_lane_overview_groups_counts_by_project(self):
        reports = [
            {"id": 1, "title": "Dieter queued", "area": "Dieter", "codex_run_status": "queued"},
            {"id": 2, "title": "Zombie running", "area": "Zombie Game / Gameplay", "codex_run_status": "running"},
            {"id": 3, "title": "EEG failed", "area": "EEG / Firmware", "codex_run_status": "failed"},
        ]

        lanes = {lane["id"]: lane for lane in api.app_feedback_lane_overview(reports, {})}

        self.assertEqual(1, lanes["dieter"]["counts"]["queued"])
        self.assertEqual(1, lanes["zombie_game"]["counts"]["running"])
        self.assertEqual("Zombie running", lanes["zombie_game"]["active_issue"]["title"])
        self.assertEqual(1, lanes["eeg"]["counts"]["failed"])

    def test_studio_list_renders_now_visualization(self):
        reports = [
            {
                "id": 32,
                "title": "Add home page prompts for starting AI projects",
                "area": "Assistant / Planner",
                "status": "in_progress",
                "raw_feedback": "Add prompts to start AI projects.",
                "created_at": "2026-07-19",
                "audit_plan": "# Plan",
                "audit_plan_approved_at": "2026-07-19",
                "codex_run_status": "running",
            },
            {
                "id": 28,
                "title": "Remove issues banner from planner app",
                "area": "Assistant / Planner",
                "status": "ready_for_review",
                "raw_feedback": "Issues should be on the issues page only.",
                "created_at": "2026-07-18",
                "audit_plan": "# Plan",
                "audit_plan_approved_at": "2026-07-18",
                "codex_run_status": "ready_for_testing",
            },
        ]

        with (
            patch.object(api, "get_app_feedback_reports_for_status", return_value=reports),
            patch.object(
                api,
                "app_feedback_worker_dashboard_context",
                return_value={
                    "worker_listener_recent": True,
                    "worker_heartbeats": [{"worker_name": "curtis-workstation-codex", "age_label": "14s ago"}],
                    "feedback_worker_runs": [],
                    "feedback_worker_recent_runs": [],
                },
            ),
            patch.object(api, "scheduler_due_context", return_value={"due_items": [], "upcoming_items": []}),
        ):
            response = api.render_issues_app(admin_request(), status="active", area="", issues_view="list")

        html = response.body.decode("utf-8")
        self.assertIn("Studio Now", html)
        self.assertIn("Running #32 Add home page prompts for starting AI projects", html)
        self.assertIn("Worker Listening", html)
        self.assertIn("Lanes", html)
        self.assertIn("Zombie Game", html)
        self.assertIn("Planning", html)
        self.assertIn("Test", html)
        self.assertIn("Worker detail", html)


if __name__ == "__main__":
    unittest.main()
