import unittest
from types import SimpleNamespace
from unittest.mock import patch

import api
from database import reset_current_user_id, set_current_user_id


class DemoGuestLoginTests(unittest.TestCase):
    def test_demo_guest_login_defaults_to_app_launcher(self):
        with (
            patch.object(api, "DEMO_MODE", True),
            patch.object(api, "get_or_create_guest_user_id", return_value=42),
            patch.object(api.db, "create_session", return_value=None),
        ):
            response = api.guest_login_form(next="")

        self.assertEqual("/apps", response.headers["location"])

    def test_private_guest_login_defaults_to_recipes(self):
        with (
            patch.object(api, "DEMO_MODE", False),
            patch.object(api, "get_or_create_guest_user_id", return_value=42),
            patch.object(api.db, "create_session", return_value=None),
        ):
            response = api.guest_login_form(next="")

        self.assertEqual("/apps/recipes", response.headers["location"])

    def test_demo_home_landing_only_for_logged_out_visitors(self):
        request = SimpleNamespace(
            url=SimpleNamespace(path="/"),
            query_params={},
            state=SimpleNamespace(current_user=None),
        )

        with (
            patch.object(api, "DEMO_MODE", True),
            patch.object(api, "render_demo_landing", return_value="landing") as landing,
            patch.object(api, "render_apps_page", return_value="apps") as apps,
        ):
            self.assertEqual("landing", api.home_default(request))
            landing.assert_called_once()
            apps.assert_not_called()

        request.state.current_user = {"id": 42, "role": "guest"}
        with (
            patch.object(api, "DEMO_MODE", True),
            patch.object(api, "render_demo_landing", return_value="landing") as landing,
            patch.object(api, "render_apps_page", return_value="apps") as apps,
        ):
            self.assertEqual("apps", api.home_default(request))
            apps.assert_called_once()
            landing.assert_not_called()

    def test_guest_ask_dieter_answers_about_repository_without_writes(self):
        message = api.DieterActionMessage(
            content="What does Studio do?",
            page_url="/apps",
            page_title="Dieter",
        )
        with patch.object(api, "current_user_is_guest_session", return_value=True):
            response = api.api_dieter_action(message)

        self.assertTrue(response["guest_context"])
        self.assertEqual([], response["changed_fields"])
        self.assertEqual("local-guest-repository", response["model"])
        self.assertIn("Studio captures feedback", response["assistant_message"])

    def test_studio_routes_classify_as_studio_area(self):
        self.assertEqual("Studio", api.app_area_from_url("/apps/issues"))

    def test_guest_studio_uses_read_only_demo_issues(self):
        request = SimpleNamespace(
            url=SimpleNamespace(path="/apps/issues"),
            query_params={},
            state=SimpleNamespace(current_user={"id": 42, "role": "guest", "status": "active"}),
        )

        response = api.render_issues_app(request, status="active", area="", issues_view="list")
        html = response.body.decode()

        self.assertIn("Guest Studio Preview", html)
        self.assertIn("Guest demo should land on the Dieter homepage", html)
        self.assertNotIn('href="/apps/issues/new', html)
        self.assertNotIn('action="/apps/issues/audit-next"', html)

    def test_guest_can_open_demo_codex_report(self):
        token = set_current_user_id(42)
        try:
            with patch.object(api, "current_user_is_guest_session", return_value=True):
                report = api.feedback_report_by_id(910001)
        finally:
            reset_current_user_id(token)

        self.assertEqual("Guest demo should land on the Dieter homepage", report["title"])
        self.assertIn("# Codex Plan", report["audit_plan"])

    def test_guest_demo_codex_report_controls_are_disabled(self):
        request = SimpleNamespace(
            url=SimpleNamespace(path="/apps/issues/910001"),
            query_params={},
            state=SimpleNamespace(current_user={"id": 42, "role": "guest", "status": "active"}),
        )
        report = api.prepare_app_feedback_reports_for_display([api.demo_studio_report_by_id(910001)])[0]

        response = api.render_issues_app(
            request,
            status="active",
            area="",
            issues_view="pipeline",
            active_feedback_report=report,
            feedback_plan=report["audit_plan"],
        )
        html = response.body.decode()

        self.assertIn("Codex Controls Disabled", html)
        self.assertIn("# Codex Plan", html)
        self.assertNotIn("Approve and Send to Codex", html)
        self.assertNotIn('action="/apps/issues/910001/run-codex"', html)

    def test_guest_issue_create_page_is_demo_only(self):
        request = SimpleNamespace(
            url=SimpleNamespace(path="/apps/issues/new"),
            query_params={},
            state=SimpleNamespace(current_user={"id": 42, "role": "guest", "status": "active"}),
        )

        response = api.render_issues_app(request, status="active", area="Kitchen / Recipes", issues_view="create")
        html = response.body.decode()

        self.assertIn("Demo-only intake preview", html)
        self.assertIn("Nothing entered here is saved or sent to Codex", html)
        self.assertIn("Demo Only", html)
        self.assertIn('action="#"', html)
        self.assertNotIn('action="/apps/issues/create"', html)


if __name__ == "__main__":
    unittest.main()
