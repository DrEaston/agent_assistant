"""Thanksgiving as its own service: signed user header, the forwarder, and the main app's internal endpoints."""

import importlib
import os
import tempfile
import time
import unittest
import warnings
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core import internal, proxy

KEY = "test-internal-key-0123456789"


class SigningTests(unittest.TestCase):
    def test_round_trip_and_tampering(self):
        header = internal.sign({"id": 7, "email": "a@b.c", "name": "Cook"}, secret=KEY)
        self.assertEqual(internal.verify(header, secret=KEY), {"id": 7, "email": "a@b.c", "name": "Cook"})
        self.assertIsNone(internal.verify(header, secret="other-key"))
        payload, mac = header.rsplit(".", 1)
        forged = internal.sign({"id": 8}, secret="attacker").rsplit(".", 1)[0] + "." + mac
        self.assertIsNone(internal.verify(forged, secret=KEY))
        self.assertIsNone(internal.verify("", secret=KEY))
        self.assertIsNone(internal.verify(header, secret=""))

    def test_stale_headers_are_refused(self):
        old = internal.sign({"id": 7}, secret=KEY, now=time.time() - 3600)
        self.assertIsNone(internal.verify(old, secret=KEY))

    def test_signing_needs_a_key(self):
        with self.assertRaises(RuntimeError):
            internal.sign({"id": 1}, secret="")


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.env = {"DB_PATH": str(Path(cls.tmp.name) / "tg.db"), "GCS_BUCKET": "", "DIETER_GCS_BUCKET": "",
                   "DIETER_INTERNAL_KEY": KEY, "PLATFORM_URL": ""}
        cls.saved = {k: os.environ.get(k) for k in cls.env}
        os.environ.update(cls.env)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from apps.kitchen.thanksgiving import service
            cls.service = importlib.reload(service)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if cls.service.db.conn:
            cls.service.db.conn.close()
        cls.tmp.cleanup()

    def signed(self, user_id=1):
        return {internal.HEADER: internal.sign({"id": user_id, "email": "cook@example.com", "name": "Curtis"})}

    def test_refuses_requests_without_a_signed_user(self):
        client = TestClient(self.service.app)
        self.assertEqual(client.get("/thanksgiving").status_code, 401)
        self.assertEqual(client.get("/thanksgiving", headers={internal.HEADER: "x.y"}).status_code, 401)
        self.assertEqual(client.get("/healthz").json(), {"ok": True})

    def test_signed_user_gets_their_own_plan(self):
        client = TestClient(self.service.app)
        page = client.get("/thanksgiving", headers=self.signed(41))
        self.assertEqual(page.status_code, 200)
        self.assertIn("Up next", page.text)
        dishes = client.get("/thanksgiving/api/internal/dishes", headers=self.signed(41)).json()
        self.assertTrue(dishes["plan"])
        self.assertIn("Mac and cheese", [d["name"] for d in dishes["dishes"]])
        # Another user doesn't see that plan.
        other = client.get("/thanksgiving/api/internal/dishes", headers=self.signed(42)).json()
        self.assertIsNone(other["plan"])

    def test_forwarding_from_the_main_app(self):
        # A main-app stand-in that forwards /thanksgiving to the service, in-process.
        main = FastAPI()

        @main.middleware("http")
        async def signed_in(request: Request, call_next):
            request.state.current_user = {"id": 43, "email": "cook@example.com", "display_name": "Curtis Cook"}
            return await call_next(request)

        @main.api_route("/thanksgiving/{rest:path}", methods=["GET", "POST"])
        async def forward(request: Request, rest: str = ""):
            return await proxy.forward(request, "http://thanksgiving.internal", request.state.current_user)

        saved = proxy._client
        proxy._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.service.app))
        try:
            client = TestClient(main)
            menu = client.get("/thanksgiving/menu", headers={"cookie": "dieter_session=secret"})
            self.assertEqual(menu.status_code, 200)
            self.assertIn("The Menu</h2>", menu.text)
            dishes = client.get("/thanksgiving/api/internal/dishes").json()
            key = f"{dishes['dishes'][0]['id']}:{dishes['dishes'][0]['steps'][0]['id']}"
            r = client.post("/thanksgiving/steps/toggle", data={"key": key, "done": "1"}, follow_redirects=False,
                            headers={"referer": "https://askdieter.ai/thanksgiving/timeline"})
            self.assertEqual(r.status_code, 303)
            self.assertEqual(r.headers["location"], "/thanksgiving/timeline")
            state = client.get("/thanksgiving/api/state").json()
            self.assertIn(key, [s["key"] for d in state["dishes"] for s in d["steps"] if s["done"]])
        finally:
            proxy._client = saved


class PlatformEndpointTests(unittest.TestCase):
    def setUp(self):
        self.saved_key = os.environ.get("DIETER_INTERNAL_KEY")
        os.environ["DIETER_INTERNAL_KEY"] = KEY
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        self.api = api
        email = "tg-service@example.com"
        row = api.db.get_user_by_email(email)
        self.user_id = row["id"] if row else api.db.create_user(email, "Service Test", "x", "member")
        api.db.connect().execute("UPDATE users SET status = 'active' WHERE id = ?", (self.user_id,))
        api.db.connect().commit()
        self.client = TestClient(api.app)

    def tearDown(self):
        if self.saved_key is None:
            os.environ.pop("DIETER_INTERNAL_KEY", None)
        else:
            os.environ["DIETER_INTERNAL_KEY"] = self.saved_key

    def test_internal_endpoints_need_a_signed_user(self):
        self.assertEqual(self.client.get("/internal/kitchen/library").status_code, 401)
        headers = {internal.HEADER: internal.sign({"id": self.user_id})}
        library = self.client.get("/internal/kitchen/library", headers=headers)
        self.assertEqual(library.status_code, 200)
        self.assertIsInstance(library.json(), list)
        self.assertEqual(self.client.get("/internal/kitchen/library/999999", headers=headers).status_code, 404)

    def test_problem_reports_reach_studio(self):
        headers = {internal.HEADER: internal.sign({"id": self.user_id})}
        before = len(self.api.db.get_app_feedback_reports() if hasattr(self.api.db, "get_app_feedback_reports") else [])
        r = self.client.post("/internal/studio/reports", headers=headers, json={
            "title": "[Thanksgiving] test", "area": "Kitchen / Recipes", "page_url": "/thanksgiving",
            "page_title": "Thanksgiving planner", "reporter_name": "Service Test",
            "reporter_email": "tg-service@example.com", "raw_feedback": "The timer didn't ring."})
        self.assertEqual(r.json(), {"ok": True})
        row = self.api.db.connect().execute(
            "SELECT title FROM app_feedback_reports WHERE raw_feedback = ? ORDER BY id DESC", ("The timer didn't ring.",)).fetchone()
        self.assertEqual(row["title"], "[Thanksgiving] test")
        self.assertEqual(self.client.post("/internal/studio/reports", headers=headers, json={}).status_code, 400)
        del before


if __name__ == "__main__":
    unittest.main()
