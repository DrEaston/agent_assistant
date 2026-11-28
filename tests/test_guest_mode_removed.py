"""Guest mode is gone (docs/demo_plan.md, step 1): no guest sign-in, and old guest accounts count as signed out."""

import secrets
import unittest
import uuid
import warnings
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient


class GuestModeRemovedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        cls.api, cls.db = api, api.db
        cls.password = "guest-password"
        cls.guest_email = f"guest-{uuid.uuid4().hex[:8]}@example.com"
        cls.guest_id = cls.db.create_user(cls.guest_email, "Guest", api.hash_password(cls.password), "guest")
        conn = cls.db.connect()
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (cls.guest_id,))
        conn.commit()

    def client(self, as_guest=False):
        client = TestClient(self.api.app)
        if as_guest:
            session = secrets.token_urlsafe(16)
            self.db.create_session(self.guest_id, self.api.hash_session_token(session),
                                   (datetime.utcnow() + timedelta(days=1)).isoformat())
            client.cookies.set(self.api.SESSION_COOKIE_NAME, session)
        return client

    def test_no_guest_or_demo_routes(self):
        paths = {getattr(route, "path", "") for route in self.api.app.routes}
        self.assertNotIn("/guest-login", paths)
        self.assertNotIn("/demo", paths)
        self.assertFalse(hasattr(self.api, "DEMO_MODE"))

    def test_sign_in_page_offers_only_real_sign_in(self):
        page = self.client().get("/login").text
        self.assertNotIn("/guest-login", page)
        self.assertNotIn("Guest", page)
        self.assertIn('action="/login"', page)

    def test_an_old_guest_session_counts_as_signed_out(self):
        response = self.client(as_guest=True).get("/apps/recipes", follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertTrue(response.headers["location"].startswith("/login"))

    def test_a_guest_account_cant_sign_in(self):
        response = self.client().post("/login", data={"email": self.guest_email, "password": self.password},
                                      follow_redirects=False)
        self.assertNotEqual(303, response.status_code)
        self.assertIn("not active", response.text)

    def test_no_guest_banner(self):
        template = self.api.jinja_env.get_template("base.html")
        request = SimpleNamespace(url=SimpleNamespace(path="/apps"), query_params={},
                                  state=SimpleNamespace(current_user=SimpleNamespace(role="guest")))
        html = template.render(request=request)
        self.assertNotIn("Guest Mode", html)
        self.assertNotIn("data-guest-session", html)

    def test_signed_out_home_goes_to_the_trainer_landing_page(self):
        home = self.client().get("/", follow_redirects=False)
        self.assertEqual((303, "/trainer"), (home.status_code, home.headers["location"]))
        page = self.client().get("/trainer")
        self.assertEqual(200, page.status_code)
        self.assertIn("Dieter Trainer", page.text)
        self.assertIn('href="/login?next=/apps/trainer"', page.text)
        self.assertIn("New sign-ups aren't open right now.", page.text)
        self.assertIn('class="share-card"', page.text)          # the example run, drawn with the real card
        self.assertIn("route-heat heat-7", page.text)
        self.assertNotIn("Report a problem", page.text)

    def test_sign_ups_are_closed(self):
        self.assertNotIn('href="/register"', self.client().get("/login").text)
        self.assertIn("aren't open", self.client().get("/register").text)
        before = self.db.get_user_count()
        answer = self.client().post("/register", data={"email": f"new-{uuid.uuid4().hex[:6]}@example.com",
                                                       "password": "a-long-password"}, follow_redirects=False)
        self.assertIn("aren't open", answer.text)
        self.assertEqual(before, self.db.get_user_count())

    def test_studio_routes_classify_as_studio_area(self):
        self.assertEqual("Studio", self.api.app_area_from_url("/apps/issues"))


if __name__ == "__main__":
    unittest.main()
