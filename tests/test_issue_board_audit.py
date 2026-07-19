import unittest

from scripts.audit_issue_board import Issue, issue_recommendation, likely_duplicate_groups


def issue(**overrides):
    values = {
        "id": 1,
        "title": "Demo mode issue",
        "area": "Dieter",
        "status": "in_progress",
        "raw_feedback": "Demo mode needs its own issue type.",
        "audit_plan": "# Plan",
        "audit_plan_approved_at": "2026-07-19",
        "implementation_note": "",
        "run_id": 10,
        "run_status": "",
        "run_note": "",
        "run_count": 1,
    }
    values.update(overrides)
    return Issue(**values)


class IssueBoardAuditTests(unittest.TestCase):
    def test_running_issue_is_top_priority_hold(self):
        priority, recommendation = issue_recommendation(issue(run_status="running"))

        self.assertEqual("P0", priority)
        self.assertIn("Leave running", recommendation)

    def test_worker_path_failure_is_retryable_but_not_top_priority(self):
        priority, recommendation = issue_recommendation(
            issue(
                title="Generic worker failure",
                raw_feedback="The worker failed before implementation.",
                run_status="failed",
                run_note="Codex crashed: [WinError 2] The system cannot find the file specified",
            )
        )

        self.assertEqual("P2", priority)
        self.assertIn("Retry later", recommendation)

    def test_demo_mode_items_are_condense_candidates(self):
        group = likely_duplicate_groups(
            [
                issue(id=31, title="Add separate issue type for demo mode"),
                issue(id=30, title="Hide demo mode bar when not in demo mode", raw_feedback="The demo mode bar is visible."),
            ]
        )

        self.assertEqual([[30, 31]], [[item.id for item in sorted(items, key=lambda candidate: candidate.id)] for items in group])


if __name__ == "__main__":
    unittest.main()
