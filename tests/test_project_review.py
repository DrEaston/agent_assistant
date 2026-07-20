import tempfile
import unittest
from pathlib import Path

from database import Database


class ProjectMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "projects.db"))
        self.db.init()

    def tearDown(self):
        if self.db.conn:
            self.db.conn.close()
            self.db.conn = None
        self.temp_dir.cleanup()

    def test_merge_projects_moves_planner_content_and_removes_source(self):
        source_id = self.db.add_project("CCT Research", "Research brief", 3, "research")
        destination_id = self.db.add_project("CCT Technical Scope", "Technical brief", 3, "technical")
        self.db.add_note(source_id, "Research context")
        self.db.add_recommended_action(source_id, "Confirm CCT", "medium")
        self.db.add_recommended_action(destination_id, "Map the platform", "high")

        merged_id = self.db.merge_projects(
            source_id,
            destination_id,
            destination_name="CCT",
            destination_description="Combined brief",
        )

        self.assertEqual(destination_id, merged_id)
        self.assertIsNone(self.db.get_project_by_id(source_id))
        project = dict(self.db.get_project_by_id(destination_id))
        self.assertEqual("CCT", project["name"])
        self.assertEqual("Combined brief", project["description"])
        self.assertEqual(2, len(self.db.get_recommended_actions(destination_id)))
        self.assertEqual("Research context", self.db.get_notes(destination_id)[0]["content"])

    def test_research_review_artifacts_preserve_run_history(self):
        project_id = self.db.add_project("Research", "Brief", 3, "research")
        self.db.upsert_project_artifact(
            project_id,
            "Research Results — First",
            "research-review-first",
            "First findings",
            artifact_type="research_review",
            status="complete",
        )
        self.db.upsert_project_artifact(
            project_id,
            "Research Results — Second",
            "research-review-second",
            "Updated findings",
            artifact_type="research_review",
            status="complete",
        )

        reviews = [dict(row) for row in self.db.get_project_artifacts(project_id)]

        self.assertEqual(2, len(reviews))
        self.assertEqual({"First findings", "Updated findings"}, {item["content_markdown"] for item in reviews})
        self.assertTrue(all(item["artifact_type"] == "research_review" for item in reviews))


class ProjectReviewTemplateTests(unittest.TestCase):
    def test_project_page_exposes_review_actions_and_guide(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "project_detail.html").read_text(encoding="utf-8")

        self.assertIn("Start Codex Review", template)
        self.assertIn("Open Next Task", template)
        self.assertIn("How to move this project forward", template)
        self.assertIn("Edit Project", template)

    def test_research_results_page_has_rerun_and_history_controls(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "project_research_results.html").read_text(encoding="utf-8")

        self.assertIn("Run Updated Review", template)
        self.assertIn("Report History", template)
        self.assertIn("No research results yet", template)

    def test_research_results_page_can_answer_open_questions(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "project_research_results.html").read_text(encoding="utf-8")

        self.assertIn("review_questions", template)
        self.assertIn("Answer Open Questions", template)
        self.assertIn("Answer &amp; Continue Review", template)
        self.assertIn('/research-results/{{ active_review.slug }}/answer', template)

    def test_project_review_answer_route_persists_answers_and_reruns(self):
        api_source = (Path(__file__).resolve().parents[1] / "api.py").read_text(encoding="utf-8")

        self.assertIn('@app.post("/projects/{project_id}/research-results/{slug}/answer")', api_source)
        self.assertIn("db.add_note(project_id, answer_note)", api_source)
        self.assertIn("review_result = run_project_codex_review(markdown)", api_source)


if __name__ == "__main__":
    unittest.main()
