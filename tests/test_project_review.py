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

        self.assertIn("Create Research Summary", template)
        self.assertIn("Open Summary Page", template)
        self.assertIn('data-working-message="Codex is creating the research summary..."', template)
        self.assertIn("Open Next Task", template)
        self.assertIn("How to move this project forward", template)
        self.assertIn("Edit Project", template)

    def test_projects_home_is_a_simple_clickable_index(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "projects.html").read_text(encoding="utf-8")

        self.assertIn("Choose a major project to work on.", template)
        self.assertIn("project-index-list", template)
        self.assertIn("project-index-item", template)
        self.assertIn("New Project", template)
        self.assertNotIn("Quick Starts", template)
        self.assertNotIn("project-starter-card", template)

    def test_research_results_page_has_rerun_and_history_controls(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "project_research_results.html").read_text(encoding="utf-8")

        self.assertIn("Create Updated Summary", template)
        self.assertIn('data-working-message="Codex is creating the updated summary..."', template)
        self.assertIn("Report History", template)
        self.assertIn("No research summary yet", template)

    def test_research_summary_page_uses_product_cards_not_raw_packet(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "project_research_results.html").read_text(encoding="utf-8")

        self.assertIn("research-summary-view", template)
        self.assertIn("research-product-card", template)
        self.assertIn("research-bullet-kind", template)
        self.assertNotIn('<pre class="work-packet">{{ active_review.content_markdown }}</pre>', template)

    def test_project_summary_prompt_targets_products_data_and_models(self):
        api_source = (Path(__file__).resolve().parents[1] / "api.py").read_text(encoding="utf-8")

        self.assertIn("product-by-product map for CCT", api_source)
        self.assertIn("Data challenge:", api_source)
        self.assertIn("Model approach:", api_source)
        self.assertIn("Do not invent specific CCT products", api_source)

    def test_research_results_page_can_answer_open_questions(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "project_research_results.html").read_text(encoding="utf-8")

        self.assertIn("review_questions", template)
        self.assertIn("Answer Open Questions", template)
        self.assertIn("Answer &amp; Create Revised Summary", template)
        self.assertIn('data-working-message="Codex is creating a revised summary..."', template)
        self.assertIn('/research-results/{{ active_review.slug }}/answer', template)

    def test_project_review_answer_route_persists_answers_and_reruns(self):
        api_source = (Path(__file__).resolve().parents[1] / "api.py").read_text(encoding="utf-8")

        self.assertIn('@app.post("/projects/{project_id}/research-results/{slug}/answer")', api_source)
        self.assertIn("db.add_note(project_id, answer_note)", api_source)
        self.assertIn("review_result = run_project_codex_review(markdown)", api_source)

    def test_base_template_has_generic_working_indicator(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn("form-working-status", template)
        self.assertIn("form[data-working-message]", template)
        self.assertIn("data-form-working-message", template)

    def test_assistant_landing_puts_projects_before_project_creation(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text(encoding="utf-8")

        self.assertIn("assistant-projects-heading", template)
        self.assertLess(template.index("assistant-projects-heading"), template.index("project-starter-heading"))
        self.assertIn("Open a major project directly.", template)
        self.assertNotIn("Priority Task", template)
        self.assertNotIn("Priority Project", template)


if __name__ == "__main__":
    unittest.main()
