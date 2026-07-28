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
        self.assertIn("Interview Questions", template)
        self.assertIn("is_cct_project", template)
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

        self.assertIn("Regenerate Summary", template)
        self.assertIn('data-working-message="Codex is regenerating the summary..."', template)
        self.assertIn("Ask Dieter About This Summary", template)
        self.assertIn("Interview Questions", template)
        self.assertIn("Report History", template)
        self.assertIn("No research summary yet", template)

    def test_cct_interview_questions_page_exists(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "project_interview_questions.html").read_text(encoding="utf-8")
        api_source = (Path(__file__).resolve().parents[1] / "api.py").read_text(encoding="utf-8")
        base_template = (Path(__file__).resolve().parents[1] / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn('@app.get("/projects/{project_id}/interview-questions")', api_source)
        self.assertIn("CCT_INTERVIEW_QUESTION_SECTIONS", api_source)
        self.assertIn("Cloud migration status", api_source)
        self.assertIn("Data platform architecture", api_source)
        self.assertIn("AI strategy", api_source)
        self.assertIn("Role expectations", api_source)
        self.assertIn("What percentage of my time would be spent on infrastructure", api_source)
        self.assertIn("CCT Interview Questions", template)
        self.assertIn("CTO Share Version", template)
        self.assertIn("/cct-roadmap", template)
        self.assertIn("question_sections", template)
        self.assertIn("interview-question-section", template)
        self.assertIn("Listen for", template)
        self.assertIn(".interview-focus-grid", base_template)
        self.assertIn(".interview-question-section", base_template)

    def test_public_cct_interview_questions_page_exists(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "public_cct_interview_questions.html").read_text(encoding="utf-8")
        api_source = (Path(__file__).resolve().parents[1] / "api.py").read_text(encoding="utf-8")
        base_template = (Path(__file__).resolve().parents[1] / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn('public_exact_paths = {"/cct-roadmap"}', api_source)
        self.assertIn('"/api/cct-roadmap"', api_source)
        self.assertIn('@app.get("/cct-roadmap")', api_source)
        self.assertIn('@app.get("/public/cct-interview-questions")', api_source)
        self.assertIn('@app.post("/api/cct-roadmap/summary-update")', api_source)
        self.assertIn('@app.post("/api/cct-roadmap/fit-answer")', api_source)
        self.assertIn("CURTIS_CCT_FIT_BACKGROUND", api_source)
        self.assertIn("about twenty minutes", api_source)
        self.assertIn("Do not overstate casino-domain experience", api_source)
        self.assertIn("public_cct_interview_questions.html", api_source)
        self.assertIn("CCT technical brief", template)
        self.assertIn("CCT Data &amp; AI Roadmap", template)
        self.assertIn("Working understanding", template)
        self.assertIn("<ul>", template)
        self.assertIn("Likely migration status", template)
        self.assertIn("Likely need from the role", template)
        self.assertIn("Likely technical problems", template)
        self.assertIn("Moving from customer-specific on-prem SQL environments toward AWS.", template)
        self.assertIn("Technical areas to explore", template)
        self.assertIn("Questions for CCT leadership", template)
        self.assertIn("What strong answers would clarify", template)
        self.assertIn("question_sections", template)
        self.assertIn("data-roadmap-update-form", template)
        self.assertIn("/api/cct-roadmap/summary-update", template)
        self.assertIn("Ask the fit agent", template)
        self.assertIn("Why would Curtis be a good fit for this job?", template)
        self.assertIn("data-cct-fit-form", template)
        self.assertIn("/api/cct-roadmap/fit-answer", template)
        self.assertIn(".public-interview-hero", base_template)
        self.assertIn(".cto-brief-grid", base_template)
        self.assertIn(".cto-brief-area-list", base_template)
        self.assertIn(".roadmap-update-checkin", base_template)
        self.assertIn(".fit-agent-panel", base_template)
        self.assertIn(".roadmap-agent-answer", base_template)

    def test_research_summary_page_uses_product_cards_not_raw_packet(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "project_research_results.html").read_text(encoding="utf-8")

        self.assertIn("research-summary-view", template)
        self.assertIn("research-domain-nav", template)
        self.assertIn("research-domain-section", template)
        self.assertIn("research-section-checkin", template)
        self.assertIn("Regenerate This Section", template)
        self.assertIn('name="section_title"', template)
        self.assertIn("research-topic-list", template)
        self.assertIn("research-topic-card", template)
        self.assertIn("research-bullet-kind", template)
        self.assertIn("<details class=\"research-topic-card\">", template)
        self.assertNotIn("research-product-card", template)
        self.assertNotIn('<pre class="work-packet">{{ active_review.content_markdown }}</pre>', template)

    def test_research_summary_parser_exposes_clickable_domains(self):
        api_source = (Path(__file__).resolve().parents[1] / "api.py").read_text(encoding="utf-8")

        self.assertIn('"domains": domains', api_source)
        self.assertIn("project_summary_domain_for_title", api_source)
        self.assertIn("project_summary_anchor", api_source)

    def test_project_summary_prompt_targets_products_data_and_models(self):
        api_source = (Path(__file__).resolve().parents[1] / "api.py").read_text(encoding="utf-8")

        self.assertIn("Machine Learning Strategy Research", api_source)
        self.assertIn("Customer Return Prediction", api_source)
        self.assertIn("Customer Analytics", api_source)
        self.assertIn("Machine Analytics", api_source)
        self.assertIn("Forecasting", api_source)
        self.assertIn("Data challenge:", api_source)
        self.assertIn("Model approach:", api_source)
        self.assertIn("Do not invent specific CCT products", api_source)
        self.assertIn("Produce a usable summary document, not a plan", api_source)
        self.assertIn("build_project_research_summary_packet", api_source)
        self.assertIn("default_research_workspace_guidance", api_source)
        self.assertIn("Default Research Workspace Template", api_source)
        self.assertIn("Use the Default Research Workspace Template as the standard format", api_source)
        self.assertIn("Components / Product Areas", api_source)
        self.assertIn("default app template", api_source)
        self.assertIn("CCT_SUMMARY_GUIDANCE_NOTE_PREFIX", api_source)
        self.assertIn("Use '## Customer Analytics'", api_source)

    def test_research_results_page_improves_summary_without_question_loop(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "project_research_results.html").read_text(encoding="utf-8")

        self.assertIn("Improve This Summary", template)
        self.assertIn("improvement_request", template)
        self.assertIn("Regenerate Summary With This Request", template)
        self.assertNotIn("Answer Open Questions", template)
        self.assertNotIn("review_questions", template)

    def test_project_summary_improvement_route_persists_request_and_reruns(self):
        api_source = (Path(__file__).resolve().parents[1] / "api.py").read_text(encoding="utf-8")

        self.assertIn('@app.post("/projects/{project_id}/research-results/improve")', api_source)
        self.assertIn("format_project_summary_improvement_note", api_source)
        self.assertIn("section_title: str = Form(\"\")", api_source)
        self.assertIn("Research summary improvement request for section", api_source)
        self.assertIn("review_result = run_project_codex_review(markdown)", api_source)

    def test_base_template_has_generic_working_indicator(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn("form-working-status", template)
        self.assertIn("form[data-working-message]", template)
        self.assertIn("data-form-working-message", template)

    def test_action_bars_wrap_inside_page_width(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn("flex-wrap: wrap", template)
        self.assertIn(".project-heading .app-shell-actions", template)
        self.assertIn("overflow-wrap: anywhere", template)
        self.assertIn(".app-shell-actions details", template)

    def test_research_bullet_labels_stack_above_text(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertIn(".research-topic-card li", template)
        self.assertIn("display: block", template)
        self.assertIn("margin-bottom: 0.35rem", template)
        self.assertNotIn("grid-template-columns: 4.2rem minmax(0, 1fr)", template)

    def test_assistant_landing_puts_projects_before_project_creation(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text(encoding="utf-8")

        self.assertIn("assistant-projects-heading", template)
        self.assertLess(template.index("assistant-projects-heading"), template.index("project-starter-heading"))
        self.assertIn("Open a major project directly.", template)
        self.assertNotIn("Priority Task", template)
        self.assertNotIn("Priority Project", template)


if __name__ == "__main__":
    unittest.main()
