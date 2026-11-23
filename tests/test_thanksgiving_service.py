"""Thanksgiving as its own service: signed user header, the forwarder, and the main app's internal endpoints."""

import importlib
import json
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

    def test_serves_claude_tools_to_the_main_app(self):
        client = TestClient(self.service.app)
        self.assertEqual(client.get("/thanksgiving/api/connector/tools").status_code, 401)
        names = [t["name"] for t in client.get("/thanksgiving/api/connector/tools", headers=self.signed(52)).json()["tools"]]
        self.assertIn("thanksgiving_finish_trip", names)
        client.get("/thanksgiving", headers=self.signed(52))
        plan = client.post("/thanksgiving/api/connector/tools/thanksgiving_get_plan", headers=self.signed(52), json={})
        self.assertIn("Pumpkin pie", plan.json()["result"])
        nope = client.post("/thanksgiving/api/connector/tools/thanksgiving_fly", headers=self.signed(52), json={})
        self.assertEqual(nope.status_code, 422)

    def test_without_platform_url_next_trip_lists_are_refused_not_lost(self):
        client = TestClient(self.service.app)
        client.get("/thanksgiving", headers=self.signed(53))
        tool = "/thanksgiving/api/connector/tools/"
        listing = client.post(tool + "thanksgiving_get_shopping_list", headers=self.signed(53), json={}).json()
        key = listing["result"]["aisles"][0]["items"][0]["key"]
        made = client.post(tool + "thanksgiving_finish_trip", headers=self.signed(53), json={"arguments": {"next_trip": [key]}}).json()
        self.assertIn("Couldn't make the next-trip list", json.dumps(made))
        listing = client.post(tool + "thanksgiving_get_shopping_list", headers=self.signed(53), json={}).json()
        self.assertEqual(listing["result"]["aisles"][0]["items"][0]["state"], "to buy")

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

    def test_menu_opens_signed_out_but_nothing_else_does(self):
        client = TestClient(self.service.app)
        client.get("/thanksgiving", headers=self.signed(1))  # the household's plan
        anon = {internal.HEADER: internal.sign(proxy.signer_user(None))}
        self.assertIsNone(internal.verify(anon[internal.HEADER]))  # other services still require a user
        menu = client.get("/thanksgiving/menu", headers=anon)
        self.assertEqual(menu.status_code, 200)
        self.assertIn("The Menu</h2>", menu.text)
        self.assertIn("Pumpkin pie", menu.text)
        for private in ("Parrisa", "Janice", "Add a dish", "Report a problem", 'id="voice-'):
            self.assertNotIn(private, menu.text)
        self.assertIn("Dinner at four o&#39;clock", menu.text)
        self.assertIn("Guests are welcome from three o&#39;clock", menu.text)
        # Dishes with a recipe or a named cook link to their page; house prep doesn't.
        from apps.kitchen.thanksgiving import router as tg_router
        household = tg_router.store().household_plan()  # the shared menu is the first plan made
        dishes = {d["name"]: d for d in tg_router.store().get_dishes(household["id"])}
        self.assertIn(f'href="/thanksgiving/menu/{dishes["Pumpkin pie"]["id"]}"', menu.text)
        self.assertIn(f'href="/thanksgiving/menu/{dishes["Green bean casserole"]["id"]}"', menu.text)
        pie = client.get(f'/thanksgiving/menu/{dishes["Pumpkin pie"]["id"]}', headers=anon)
        self.assertEqual(pie.status_code, 200)
        self.assertIn("pumpkin puree", pie.text)
        self.assertIn("1 can (15 oz)", pie.text)  # the recipe as written: one pie, though we're making two
        for private in ("Parrisa", "Janice", "Batch size", "Edit dish", "Report a problem", "Notes"):
            self.assertNotIn(private, pie.text)  # nobody's named on our own dishes
        mac = client.get(f'/thanksgiving/menu/{dishes["Mac and cheese"]["id"]}', headers=anon)
        self.assertEqual(mac.status_code, 200)
        self.assertIn("From Parrisa", mac.text)
        beans = client.get(f'/thanksgiving/menu/{dishes["Green bean casserole"]["id"]}', headers=anon)
        self.assertEqual(beans.status_code, 200)
        self.assertIn("From Janice", beans.text)
        self.assertNotIn("Janice is bringing it", beans.text)  # the private note stays private
        self.assertEqual(client.get(f'/thanksgiving/menu/{dishes["House prep"]["id"]}', headers=anon).status_code, 404)
        self.assertEqual(client.get("/thanksgiving/menu/99999", headers=anon).status_code, 404)
        self.assertEqual(client.post(f'/thanksgiving/menu/{dishes["Pumpkin pie"]["id"]}', headers=anon).status_code, 401)
        self.assertIn('property="og:title"', menu.text)
        self.assertEqual(client.get("/thanksgiving/menu").status_code, 401)  # still only through askdieter.ai
        self.assertEqual(client.get("/thanksgiving", headers=anon).status_code, 401)
        self.assertEqual(client.get("/thanksgiving/shopping", headers=anon).status_code, 401)
        self.assertEqual(client.post("/thanksgiving/menu", headers=anon).status_code, 401)
        self.assertEqual(client.get("/thanksgiving/api/state", headers=anon).status_code, 401)
        # Signed in, the same address is the full Menu tab.
        self.assertIn("Add a dish", client.get("/thanksgiving/menu", headers=self.signed(1)).text)

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


class SaveBeforeReplyTests(unittest.TestCase):
    """A deploy must not lose changes: load on the first request (after the new version takes over) and save each
    change before answering."""

    class FakeStorage:
        def __init__(self, fail_restore=False):
            self.enabled, self.events, self.fail_restore, self.pending = True, [], fail_restore, False

        def restore(self):
            self.events.append("restore")
            if self.fail_restore:
                raise RuntimeError("storage unreachable")
            return {"enabled": True, "database_restored": False, "uploads_restored": 0}

        def schedule_backup(self):
            self.pending = True

        def save_pending(self):
            if self.pending:
                self.pending = False
                self.events.append("save")

        def flush(self):
            self.events.append("flush")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        env = {"DB_PATH": str(Path(self.tmp.name) / "tg.db"), "GCS_BUCKET": "", "DIETER_GCS_BUCKET": "",
               "DIETER_INTERNAL_KEY": KEY, "PLATFORM_URL": ""}
        self.saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from apps.kitchen.thanksgiving import service
            self.service = importlib.reload(service)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self.service.db.conn:
            self.service.db.conn.close()
        self.tmp.cleanup()

    def use(self, storage):
        self.service.persistence = storage
        self.service.db.after_commit = storage.schedule_backup
        return storage

    def signed(self):
        return {internal.HEADER: internal.sign({"id": 61, "email": "cook@example.com", "name": "Curtis"})}

    def test_loads_on_the_first_request_and_saves_before_answering(self):
        storage = self.use(self.FakeStorage())
        self.assertIsNone(self.service.db.conn)  # starting up opens nothing
        client = TestClient(self.service.app)
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(storage.events, [])  # the health check doesn't load anything either
        client.get("/thanksgiving", headers=self.signed())
        self.assertEqual(storage.events[0], "restore")
        self.assertEqual(storage.events.count("restore"), 1)
        page = client.get("/thanksgiving/shopping", headers=self.signed()).text
        key = page.split('name="key" value="')[1].split('"')[0]
        storage.events.clear()
        client.post("/thanksgiving/groceries/toggle", headers=self.signed(), data={"key": key, "done": "1"},
                    follow_redirects=False)
        self.assertEqual(storage.events, ["save"])  # saved before the reply came back
        self.assertNotIn("restore", storage.events)

    def test_a_failed_load_answers_try_again_and_never_saves(self):
        storage = self.use(self.FakeStorage(fail_restore=True))
        client = TestClient(self.service.app)
        r = client.get("/thanksgiving", headers=self.signed())
        self.assertEqual(r.status_code, 503)
        self.assertIsNone(self.service.db.conn)
        self.service.flush_backup()
        self.assertNotIn("flush", storage.events)  # an unloaded database is never uploaded
        storage.fail_restore = False
        self.assertEqual(client.get("/thanksgiving", headers=self.signed()).status_code, 200)  # next try loads


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

    def test_next_trip_lists_are_made_for_the_signed_user(self):
        lines = {"title": "Next trip · Sun Nov 22", "lines": ["Whole milk (1 quart)", " "]}
        self.assertEqual(self.client.post("/internal/kitchen/lists", json=lines).status_code, 401)
        headers = {internal.HEADER: internal.sign({"id": self.user_id})}
        made = self.client.post("/internal/kitchen/lists", headers=headers, json=lines).json()
        self.assertEqual(made["url"], f"/apps/recipes/grocery-lists/{made['id']}")
        row = self.api.db.connect().execute("SELECT title, user_id FROM recipe_grocery_lists WHERE id = ?",
                                            (made["id"],)).fetchone()
        self.assertEqual((row["title"], row["user_id"]), ("Next trip · Sun Nov 22", self.user_id))
        extras = self.api.db.connect().execute("SELECT text FROM recipe_grocery_extras WHERE list_id = ?",
                                               (made["id"],)).fetchall()
        self.assertEqual([e["text"] for e in extras], ["Whole milk (1 quart)"])

    def test_portable_items_can_be_followed_checked_and_taken_back(self):
        headers = {internal.HEADER: internal.sign({"id": self.user_id})}
        source = {"app": "thanksgiving", "label": "Thanksgiving"}
        items = [{"key": "item-milk", "text": "Whole milk", "amount": "1 quart (4 cups)", "aisle": "Dairy & eggs",
                  "for": ["Mac and cheese"]},
                 {"key": "extra-4", "text": "Paper plates"}]
        self.assertEqual(self.client.post("/internal/kitchen/lists", headers=headers,
                                          json={"title": "Next trip", "items": items}).status_code, 400)  # no source
        made = self.client.post("/internal/kitchen/lists", headers=headers,
                                json={"title": "Next trip", "items": items, "source": source}).json()
        base = f"/internal/kitchen/lists/{made['id']}/linked"
        self.assertEqual(self.client.get(base + "?app=thanksgiving").status_code, 401)
        state = self.client.get(base + "?app=thanksgiving", headers=headers).json()
        self.assertTrue(state["found"])
        self.assertEqual(set(state["items"]), {"item-milk", "extra-4"})
        self.assertFalse(state["items"]["item-milk"]["checked"])
        ok = self.client.post(base + "/check", headers=headers, json={"app": "thanksgiving", "key": "item-milk",
                                                                      "done": True}).json()
        self.assertTrue(ok["ok"])
        checks = self.api.db.connect().execute("SELECT item_key FROM recipe_grocery_checks WHERE list_id = ?",
                                               (made["id"],)).fetchall()
        self.assertEqual([c["item_key"] for c in checks], ["item-milk"])  # Kitchen's key for it is the same
        self.assertTrue(self.client.get(base + "?app=thanksgiving", headers=headers).json()["items"]["item-milk"]["checked"])
        self.client.post(base + "/remove", headers=headers, json={"app": "thanksgiving", "key": "extra-4"})
        self.assertEqual(set(self.client.get(base + "?app=thanksgiving", headers=headers).json()["items"]),
                         {"item-milk"})
        email = "tg-someone-else@example.com"
        row = self.api.db.get_user_by_email(email)
        other = row["id"] if row else self.api.db.create_user(email, "Someone Else", "x", "member")
        self.api.db.connect().execute("UPDATE users SET status = 'active' WHERE id = ?", (other,))
        self.api.db.connect().commit()
        someone_else = {internal.HEADER: internal.sign({"id": other})}
        self.assertEqual(self.client.get(base + "?app=thanksgiving", headers=someone_else).json(),
                         {"found": False, "items": {}})

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
