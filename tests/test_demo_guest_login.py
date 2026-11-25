import unittest
from pathlib import Path
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

    def test_signed_out_home_goes_to_sign_in(self):
        request = SimpleNamespace(
            url=SimpleNamespace(path="/"),
            query_params={},
            state=SimpleNamespace(current_user=None),
        )

        with (
            patch.object(api, "DEMO_MODE", True),
            patch.object(api, "render_demo_landing", return_value="landing") as landing,
            patch.object(api, "render_today_page", return_value="apps") as apps,
        ):
            answer = api.home_default(request)
            self.assertEqual(303, answer.status_code)
            self.assertEqual("/login", answer.headers["location"])
            landing.assert_not_called()
            apps.assert_not_called()

        request.state.current_user = {"id": 42, "role": "guest"}
        with (
            patch.object(api, "DEMO_MODE", True),
            patch.object(api, "render_demo_landing", return_value="landing") as landing,
            patch.object(api, "render_today_page", return_value="apps") as apps,
        ):
            self.assertEqual("apps", api.home_default(request))
            apps.assert_called_once()
            landing.assert_not_called()

    def test_demo_banner_only_uses_guest_session_state(self):
        template = api.jinja_env.get_template("base.html")
        anonymous_request = SimpleNamespace(
            url=SimpleNamespace(path="/cct-roadmap"),
            query_params={},
            state=SimpleNamespace(current_user=None),
        )
        guest_request = SimpleNamespace(
            url=SimpleNamespace(path="/apps"),
            query_params={},
            state=SimpleNamespace(current_user=SimpleNamespace(role="guest")),
        )

        anonymous_html = template.render(request=anonymous_request, demo_mode=True)
        guest_html = template.render(request=guest_request, demo_mode=True)
        base_source = (Path(__file__).resolve().parents[1] / "templates" / "base.html").read_text(encoding="utf-8")

        self.assertNotIn("demo_mode or is_guest_session", base_source)
        self.assertNotIn('<div class="demo-mode-banner">', anonymous_html)
        self.assertNotIn("Demo Mode", anonymous_html)
        self.assertIn('<div class="demo-mode-banner">', guest_html)
        self.assertIn("Guest Mode", guest_html)

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


if __name__ == "__main__":
    unittest.main()
