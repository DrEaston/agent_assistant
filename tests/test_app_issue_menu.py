import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, pass_context

from apps.registry import app_shell_for_path, global_nav_apps, launcher_cards


ROOT = Path(__file__).resolve().parents[1]


class AppMenuParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.links = []
        self.current_link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        parent_in_app_menu = self.stack[-1]["in_app_menu"] if self.stack else False
        in_app_menu = parent_in_app_menu or (tag == "details" and "app-menu" in classes)
        node = {"tag": tag, "in_app_menu": in_app_menu}
        self.stack.append(node)
        if tag == "a":
            self.current_link = {
                "href": attrs.get("href", ""),
                "text": "",
                "in_app_menu": in_app_menu,
            }

    def handle_data(self, data):
        if self.current_link is not None:
            self.current_link["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current_link is not None:
            self.current_link["text"] = " ".join(self.current_link["text"].split())
            self.links.append(self.current_link)
            self.current_link = None
        if self.stack:
            self.stack.pop()


class AppIssueMenuTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
        self.env.globals["current_app_shell"] = self.current_app_shell
        self.env.globals["global_nav_apps"] = global_nav_apps
        self.template = self.env.get_template("base.html")
        self.feedback_template = self.env.get_template("app_feedback.html")

    @pass_context
    def current_app_shell(self, template_context):
        request = template_context.get("request")
        if not request:
            return None
        return app_shell_for_path(request.url.path, template_context)

    def render_path(self, path, user_role="admin", **extra_context):
        request = SimpleNamespace(
            url=SimpleNamespace(path=path),
            query_params={},
            state=SimpleNamespace(current_user=SimpleNamespace(name="Curtis", role=user_role, status="active")),
        )
        context = {"request": request, **extra_context}
        return self.template.render(context)

    def render_issue_create_page(self):
        request = SimpleNamespace(
            url=SimpleNamespace(path="/apps/issues/new"),
            query_params={},
            state=SimpleNamespace(current_user=SimpleNamespace(name="Curtis", role="admin", status="active")),
        )
        return self.feedback_template.render(
            request=request,
            feedback_area="",
            feedback_areas=[
                "Kitchen / Recipes",
                "Scheduler",
                "Assistant / Planner",
                "Issues",
                "Trainer",
                "Music",
                "Auth",
                "Dieter",
            ],
            show_issue_create=True,
            show_issue_list=False,
            show_worker_panel=False,
            show_issue_pipeline=False,
        )

    def parse_links(self, html):
        parser = AppMenuParser()
        parser.feed(html)
        return parser.links

    def issue_menu_links(self, html):
        links = self.parse_links(html)
        return [
            link
            for link in links
            if link["href"].startswith("/apps/issues/new") and link["text"] == "Issues"
        ]

    def studio_home_menu_links(self, html):
        links = self.parse_links(html)
        return [
            link
            for link in links
            if link["in_app_menu"] and link["href"] == "/apps/issues" and link["text"] == "Studio"
        ]

    def issues_app_menu_links(self, html):
        links = self.parse_links(html)
        return [
            link
            for link in links
            if link["in_app_menu"] and link["href"].startswith("/apps/issues")
        ]

    def assert_issue_menu_link(self, html, expected_href="/apps/issues/new"):
        issue_links = self.issue_menu_links(html)
        self.assertEqual(1, len(issue_links))
        self.assertTrue(issue_links[0]["in_app_menu"])
        self.assertEqual(expected_href, issue_links[0]["href"])

    def test_app_hamburgers_link_to_shared_issue_route(self):
        cases = [
            ("/apps/recipes", {"recipe_app": SimpleNamespace(import_url=None)}, "/apps/issues/new?area=Kitchen%20/%20Recipes"),
            ("/apps/trainer", {"trainer_mode": "athlete"}, "/apps/issues/new?area=Trainer"),
            ("/apps/music/playlists", {}, "/apps/issues/new?area=Music"),
            ("/apps", {}, "/apps/issues/new"),
        ]

        for path, context, expected_href in cases:
            with self.subTest(path=path):
                self.assert_issue_menu_link(self.render_path(path, **context), expected_href)

    def test_planner_ask_dieter_button_uses_app_shell_action_style(self):
        html = self.render_path("/apps/assistant/planner")

        self.assertIn('class="app-shell-chat-button" data-recipe-chat-open>Ask Dieter</button>', html)
        self.assertIn('class="app-menu"', html)
        self.assertIn("assistant-shell-actions", html)
        self.assertIn(".app-themed-page .app-shell-actions button", html)
        self.assertIn(".assistant-shell-actions .app-menu summary", html)
        self.assertIn(".planner-shell-page .container button:not(.recipe-chat-close)", html)
        self.assertNotIn(".planner-shell-page button:not(.recipe-chat-close) {", html)
        self.assertIn('class="app-shell app-shell-planner"', html)
        self.assertNotIn('planner-app-heading"', html)

    def test_assistant_hamburgers_link_to_issue_form_for_admin_and_guest(self):
        for role in ("admin", "guest"):
            for path, expected_area in (
                ("/apps/assistant/planner", "Assistant%20/%20Planner"),
                ("/apps/assistant/scheduler", "Assistant%20/%20Planner"),
            ):
                with self.subTest(role=role, path=path):
                    self.assert_issue_menu_link(
                        self.render_path(path, user_role=role),
                        f"/apps/issues/new?area={expected_area}",
                    )

    def test_non_admin_app_hamburgers_do_not_link_to_issue_creation(self):
        html = self.render_path(
            "/apps/recipes",
            user_role="user",
            recipe_app=SimpleNamespace(import_url=None),
        )

        self.assertEqual([], self.issue_menu_links(html))

    def test_studio_hamburger_has_single_studio_home_entry_across_subpages(self):
        for path in ("/apps/issues", "/apps/issues/new", "/apps/issues/workers", "/apps/issues/42"):
            with self.subTest(path=path):
                html = self.render_path(path)

                self.assertEqual(1, len(self.studio_home_menu_links(html)))
                self.assertEqual(
                    [{"href": "/apps/issues", "text": "Studio", "in_app_menu": True}],
                    self.issues_app_menu_links(html),
                )

    def test_dieter_home_launcher_menu_has_single_issue_link(self):
        for path in ("/", "/apps"):
            with self.subTest(path=path):
                html = self.render_path(path)
                app_menu_links = [
                    link
                    for link in self.parse_links(html)
                    if link["in_app_menu"]
                ]
                issue_menu_links = [
                    link
                    for link in app_menu_links
                    if link["href"].startswith("/apps/issues")
                ]
                member_menu_links = [
                    link
                    for link in app_menu_links
                    if link["href"] == "/admin/members" and link["text"] == "Members"
                ]

                self.assertEqual(1, len(issue_menu_links))
                self.assertEqual("/apps/issues/new", issue_menu_links[0]["href"])
                self.assertEqual("Issues", issue_menu_links[0]["text"])
                self.assertEqual(1, len(member_menu_links))

    def test_studio_is_not_a_regular_launcher_card(self):
        cards = launcher_cards({"project": True}, {})

        self.assertNotIn("Dieter Studio", [card["title"] for card in cards])

    def test_studio_bubble_only_appears_on_launcher_shell(self):
        self.assertIn('class="studio-home-bubble" href="/apps/issues">Studio</a>', self.render_path("/apps"))
        self.assertIn('class="studio-home-bubble" href="/apps/issues">Studio</a>', self.render_path("/"))
        self.assertNotIn('class="studio-home-bubble" href="/apps/issues">Studio</a>', self.render_path("/apps/recipes", recipe_app=SimpleNamespace(import_url=None)))
        self.assertNotIn('class="studio-home-bubble" href="/apps/issues">Studio</a>', self.render_path("/apps/trainer", trainer_mode="athlete"))

    def test_guest_app_hamburger_links_to_demo_issues_form(self):
        html = self.render_path(
            "/apps/recipes",
            user_role="guest",
            recipe_app=SimpleNamespace(import_url=None),
        )

        self.assert_issue_menu_link(html, "/apps/issues/new?area=Kitchen%20/%20Recipes")

    def test_members_page_uses_launcher_shell_menu(self):
        html = self.render_path("/admin/members")
        links = self.parse_links(html)
        member_menu_links = [
            link
            for link in links
            if link["in_app_menu"] and link["href"] == "/admin/members" and link["text"] == "Members"
        ]

        self.assertIn("launcher-shell-page", html)
        self.assertEqual(1, len(member_menu_links))

    def test_issues_shell_band_does_not_render_members_shortcut(self):
        html = self.render_path("/apps/issues")
        links = self.parse_links(html)

        self.assertEqual(
            [],
            [
                link
                for link in links
                if link["href"] == "/admin/members" and link["text"] == "Members"
            ],
        )

    def test_known_regressions_do_not_render_standalone_report_issue_links(self):
        cases = [
            ("/apps/recipes", {"recipe_app": SimpleNamespace(import_url=None)}),
            ("/apps/music/playlists", {}),
        ]

        for path, context in cases:
            with self.subTest(path=path):
                html = self.render_path(path, **context)
                self.assertNotIn("Report Issue", html)
                self.assertEqual(1, len(self.issue_menu_links(html)))
                issue_list_links = [
                    link
                    for link in self.parse_links(html)
                    if link["in_app_menu"] and link["href"].startswith("/apps/issues?area=")
                ]
                self.assertEqual([], issue_list_links)

    def test_issue_create_hamburger_uses_red_theme_parameters(self):
        html = self.render_path("/apps/issues/new")

        self.assertIn("issues-shell-page", html)
        self.assertIn("--page-theme-color: #be123c", html)
        self.assertIn("--page-theme-header: #881337", html)
        self.assertIn(".recipe-app-menu-icon span", html)
        icon_rule = html.split(".recipe-app-menu-icon span", 1)[1].split("}", 1)[0]
        self.assertIn("background: var(--shell-control-text);", icon_rule)
        self.assertNotIn("background: #4c1d95;", icon_rule)

    def test_issue_create_page_has_single_studio_home_navigation_link(self):
        html = self.render_issue_create_page()
        links = self.parse_links(html)
        studio_home_links = [
            link
            for link in links
            if link["in_app_menu"] and link["href"] == "/apps/issues" and link["text"] == "Studio"
        ]
        issue_app_menu_links = [
            link
            for link in links
            if link["in_app_menu"] and link["href"].startswith("/apps/issues")
        ]

        self.assertEqual(1, len(studio_home_links))
        self.assertEqual(studio_home_links, issue_app_menu_links)
        self.assertNotIn("Issue", [link["text"] for link in issue_app_menu_links])
        self.assertNotIn("Make Issue", [link["text"] for link in links])
        self.assertEqual(1, html.count('<option value="Issues"'))

    def test_issue_create_header_actions_are_compact_links_without_synthesize(self):
        html = self.render_issue_create_page()

        self.assertIn('class="feedback-header-actions"', html)
        self.assertIn('class="feedback-header-action" href="/apps/issues"', html)
        self.assertIn('class="feedback-header-action" href="/apps/issues/workers"', html)
        self.assertNotIn("button-link secondary compact", html)
        self.assertNotIn("/apps/issues/synthesize", html)
        self.assertNotIn("Synthesize", html)

    def test_issue_create_title_field_appears_after_description(self):
        html = self.render_issue_create_page()

        self.assertIn('id="issue-description"', html)
        self.assertIn('id="issue-title"', html)
        self.assertLess(html.index('id="issue-description"'), html.index('id="issue-title"'))


if __name__ == "__main__":
    unittest.main()
