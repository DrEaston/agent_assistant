import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import api
from apps.kitchen.stalled_issue_evaluation import (
    build_evaluation_plan,
    existing_plan_matches,
    stalled_eligibility,
)


def report(**overrides):
    values = {
        "id": 33,
        "title": "Recipe save flow should work",
        "area": "Kitchen / Recipes",
        "status": "ready_for_review",
        "raw_feedback": "Saving a recipe should keep the page usable.",
        "page_url": "/apps/recipes",
        "updated_at": "2026-07-10 10:00:00",
        "user_last_active_at": "2026-07-10 09:00:00",
        "audit_plan": "Verify recipe save flow works.",
        "audit_plan_updated_at": "2026-07-10 10:10:00",
        "implementation_note_updated_at": "",
        "audit_action_history_json": "[]",
        "codex_run_status": "ready_for_testing",
        "auto_evaluation_plan": "",
        "auto_evaluation_plan_approved_at": "",
    }
    values.update(overrides)
    return values


class KitchenStalledIssueEvaluationTests(unittest.TestCase):
    def test_inactive_kitchen_issue_is_eligible(self):
        result = stalled_eligibility(report(), now=datetime(2026, 7, 18, 12, 0, 0))

        self.assertTrue(result["eligible"])
        self.assertIn("inactive", result["reason"])
        self.assertIn("ready_for_review", result["reason"])

    def test_recent_user_activity_excludes_issue(self):
        result = stalled_eligibility(
            report(user_last_active_at="2026-07-18 09:00:00"),
            now=datetime(2026, 7, 18, 12, 0, 0),
        )

        self.assertFalse(result["eligible"])
        self.assertIn("Owning user was active", result["reason"])

    def test_plan_fingerprint_prevents_duplicate_generation(self):
        source = report()
        plan = build_evaluation_plan(source, stalled_eligibility(source, now=datetime(2026, 7, 18, 12, 0, 0)))
        duplicate = report(auto_evaluation_plan=plan["plan"], updated_at="2026-07-18 12:00:00")

        self.assertTrue(existing_plan_matches(duplicate))

    def test_scan_skips_matching_existing_plan(self):
        source = report()
        plan = build_evaluation_plan(source, stalled_eligibility(source, now=datetime(2026, 7, 18, 12, 0, 0)))
        existing = report(auto_evaluation_plan=plan["plan"])

        with patch.object(api, "get_app_feedback_reports_for_status", return_value=[existing]):
            result = api.scan_kitchen_stalled_issues(limit=1)

        self.assertEqual([], result["generated"])
        self.assertEqual([{"id": 33, "reason": "Existing plan matches current issue history."}], result["skipped"])

    def test_evaluation_run_requires_approved_plan(self):
        with patch.object(api, "feedback_report_by_id", return_value=report(auto_evaluation_plan="# Plan")):
            with self.assertRaises(api.HTTPException) as raised:
                api.run_stalled_issue_evaluation_form(33)

        self.assertEqual(400, raised.exception.status_code)
        self.assertIn("Approve", raised.exception.detail)

    def test_approved_evaluation_records_observation_without_status_change(self):
        db = Mock()
        with (
            patch.object(api, "feedback_report_by_id", return_value=report(auto_evaluation_plan="# Plan", auto_evaluation_plan_approved_at="2026-07-18 12:00:00")),
            patch.object(api, "kitchen_observation_check", return_value={"status_code": 200, "body": "Dieter Kitchen recipe save flow", "summary": "Dieter Kitchen recipe save flow"}),
            patch.object(api, "db", db),
        ):
            response = api.run_stalled_issue_evaluation_form(33, next="/apps/issues/33")

        self.assertEqual(303, response.status_code)
        db.update_app_feedback_auto_evaluation_result.assert_called_once()
        db.update_app_feedback_report_status.assert_not_called()
        db.append_app_feedback_report_action.assert_called_once()


if __name__ == "__main__":
    unittest.main()
