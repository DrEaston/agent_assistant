"""Claude's connector: OAuth sign-in, the /mcp endpoint, and the Kitchen and Thanksgiving tools."""

import base64
import hashlib
import json
import re
import secrets
import unittest
import uuid
import warnings
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from core.connector import oauth
from core.connector.tools import RemoteToolSet, Tool, ToolError, ToolSet, serve_tools

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class PieceTests(unittest.TestCase):
    def test_redirects_only_go_back_to_claude(self):
        self.assertTrue(oauth.redirect_allowed(CALLBACK))
        self.assertTrue(oauth.redirect_allowed("http://localhost:4312/callback"))
        self.assertTrue(oauth.redirect_allowed("http://127.0.0.1/callback"))
        self.assertTrue(oauth.redirect_allowed("https://chatgpt.com/connector_platform_oauth_redirect"))
        self.assertTrue(oauth.redirect_allowed("https://chatgpt.com/connector/oauth/6mt_L_yi2KyC"))
        self.assertEqual(oauth.app_name("https://chatgpt.com/connector_platform_oauth_redirect"), "ChatGPT")
        self.assertEqual(oauth.app_name(CALLBACK), "Claude")
        for bad in ("https://evil.example/cb", "http://localhost.evil.example/cb", "https://claude.ai/other",
                    "http://localhost:80@evil.example/", "https://chatgpt.com/other",
                    "https://chatgpt.com/connector/oauth/x/../../evil", "https://chatgpt.com.evil.example/connector_platform_oauth_redirect"):
            self.assertFalse(oauth.redirect_allowed(bad), bad)
        self.assertTrue(oauth.redirect_matches(["http://localhost/callback"], "http://localhost:5555/callback"))
        self.assertFalse(oauth.redirect_matches(["http://localhost/callback"], "http://localhost:5555/other"))
        self.assertFalse(oauth.redirect_matches([CALLBACK], "https://claude.com/api/mcp/auth_callback"))

    def test_pkce(self):
        verifier, challenge = pkce()
        self.assertTrue(oauth.pkce_ok(verifier, challenge))
        self.assertFalse(oauth.pkce_ok(verifier + "x", challenge))
        self.assertFalse(oauth.pkce_ok("short", challenge))

    def test_toolset_names_and_remote_calls(self):
        with self.assertRaises(ValueError):
            ToolSet("kitchen", [Tool("music_play", "Play", "", lambda a: None)])

        def boom(args):
            raise ToolError("Nope: " + args["why"])

        tools = ToolSet("demo", [Tool("demo_echo", "Echo", "Echo it", lambda a: {"echo": a.get("text")},
                                      {"text": {"type": "string"}}, read_only=True),
                                 Tool("demo_fail", "Fail", "", boom)])
        router = APIRouter()
        serve_tools(router, "/api/tools", tools)
        service = FastAPI()
        service.include_router(router)
        client = TestClient(service)

        def call(base_url, method, path, user, payload=None, timeout=20):
            response = client.request(method, path, json=payload)
            response.raise_for_status()
            return response.json()

        remote = RemoteToolSet("demo", "http://service", "/api/tools", call=call)
        listed = remote.list({"id": 1})
        self.assertEqual([t["name"] for t in listed], ["demo_echo", "demo_fail"])
        self.assertTrue(listed[0]["annotations"]["readOnlyHint"])
        self.assertEqual(remote.call("demo_echo", {"text": "hi"}, {"id": 1}), {"echo": "hi"})
        with self.assertRaisesRegex(ToolError, "Nope: because"):
            remote.call("demo_fail", {"why": "because"}, {"id": 1})


class PageTests(unittest.TestCase):
    def test_pages_build_safe_addresses(self):
        from apps.kitchen import nav as kitchen_nav
        from apps.kitchen.thanksgiving import nav
        from core.connector.pages import open_page_tool

        pages = {p.name: p for p in nav.PAGES + kitchen_nav.PAGES}
        self.assertEqual(pages["thanksgiving.dish"].url({"dish_id": 7}), "/thanksgiving/menu/7")
        timeline = pages["thanksgiving.timeline"]
        self.assertEqual(timeline.url({}), "/thanksgiving/timeline")
        self.assertEqual(timeline.url({"day": "2026-11-25"}), "/thanksgiving/timeline#day-2026-11-25")
        self.assertEqual(timeline.url({"dish_id": 7, "step_id": "dough", "day": "2026-11-23"}),
                         "/thanksgiving/timeline#task-7-dough")  # the step wins over the day
        with self.assertRaisesRegex(ToolError, "needs dish_id"):
            pages["thanksgiving.dish"].url({})
        for bad in ({"dish_id": "7/../../admin"}, {"dish_id": "x"}):
            with self.assertRaises(ToolError):
                pages["thanksgiving.dish"].url(bad)
        with self.assertRaisesRegex(ToolError, "doesn't take"):
            pages["kitchen.home"].url({"evil": "1"})
        tool = open_page_tool([nav.PAGES, kitchen_nav.PAGES])
        self.assertEqual(tool.handler({"page": "kitchen.recipe", "params": {"recipe_id": 12}}),
                         {"title": "Recipe", "link": "https://askdieter.ai/apps/recipes/meals/12"})
        with self.assertRaisesRegex(ToolError, "no page called"):
            tool.handler({"page": "trainer.week"})
        self.assertIn("thanksgiving.timeline", tool.schema()["description"])


class ProposalTests(unittest.TestCase):
    def test_diff_and_plain_save(self):
        from core.connector.proposals import line_diff, said_save
        lines = line_diff("a\nb\nc\nd\ne\nf", "a\nb\nc\nD\ne\nf")
        self.assertEqual([(l["op"], l["text"]) for l in lines],
                         [("gap", "2 unchanged lines"), ("same", "c"), ("removed", "d"), ("added", "D"), ("same", "e")])
        for yes in ("save the recipe change", "yes, save it", "make the change", "Save that please"):
            self.assertTrue(said_save(yes), yes)
        for no in ("yes", "ok", "sure", "yeah do it", "don't save it", "save", "wait, don't make that change"):
            self.assertFalse(said_save(no), no)

    def test_proposals_expire_and_belong_to_their_owner(self):
        import tempfile, time as _time
        from pathlib import Path as _P
        from core.connector import proposals
        from database import Database
        db = Database(str(_P(tempfile.mkdtemp()) / "p.db"))
        book = proposals.ProposalStore(db, "test_proposals")
        card = book.create("plan:1", "recipe", {"title": "T", "save_url": "/x/{id}/save"}, {"a": 1})
        self.assertEqual(card["save_url"], f"/x/{card['id']}/save")
        self.assertEqual(book.get("plan:2", card["id"]), (None, None))
        self.assertEqual(book.get("plan:1", card["id"])[0]["status"], "pending")
        self.assertEqual(len(book.pending("plan:1", "recipe")), 1)
        db.connect().execute("UPDATE test_proposals SET created_at = ?", (_time.time() - proposals.EXPIRES_SECONDS - 5,))
        self.assertEqual(book.get("plan:1", card["id"])[0]["status"], "expired")
        self.assertEqual(book.pending("plan:1"), [])
        db.conn.close()


class AgentLoopTests(unittest.TestCase):
    def test_tool_errors_go_back_to_claude_and_the_loop_ends(self):
        from types import SimpleNamespace as NS
        from core.connector import agent

        turns = [NS(stop_reason="tool_use", content=[NS(type="tool_use", id="a", name="demo_fail", input={}),
                                                     NS(type="tool_use", id="b", name="demo_ok", input={"x": 1})]),
                 NS(stop_reason="end_turn", content=[NS(type="text", text="All set.")])]
        sent = []

        def model_call(system, messages, tools, effort):
            sent.append([dict(m) for m in messages])
            return turns.pop(0)

        def call_tool(name, args):
            if name == "demo_fail":
                raise ToolError("Not on the list.")
            return {"ok": args["x"]}

        self.assertEqual(agent.run("sys", [{"role": "user", "content": "hi"}], [], call_tool, model_call=model_call),
                         "All set.")
        results = sent[1][-1]["content"]
        self.assertEqual([(r["tool_use_id"], r.get("is_error", False)) for r in results], [("a", True), ("b", False)])
        self.assertEqual(results[1]["content"], '{"ok": 1}')

    def test_refusals_and_runaway_loops_get_a_plain_answer(self):
        from types import SimpleNamespace as NS
        from core.connector import agent

        refused = agent.run("s", [], [], lambda n, a: None,
                            model_call=lambda **k: NS(stop_reason="refusal", content=[]))
        self.assertEqual(refused, "I can't help with that one.")
        looping = agent.run("s", [], [], lambda n, a: "ok", max_rounds=2, model_call=lambda **k: NS(
            stop_reason="tool_use", content=[NS(type="tool_use", id="x", name="demo", input={})]))
        self.assertIn("more steps", looping)


class ConnectorFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        cls.api = api
        cls.db = api.db
        email = f"claude-{uuid.uuid4().hex[:8]}@example.com"
        cls.user_id = cls.db.create_user(email, "Connector Cook", "x", "member")
        conn = cls.db.connect()
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (cls.user_id,))
        conn.commit()

    def setUp(self):
        self.client = TestClient(self.api.app)

    def sign_in(self, client=None):
        token = secrets.token_urlsafe(16)
        self.db.create_session(self.user_id, self.api.hash_session_token(token),
                               (datetime.utcnow() + timedelta(days=1)).isoformat())
        (client or self.client).cookies.set(self.api.SESSION_COOKIE_NAME, token)

    def connect(self):
        """Run Claude's whole sign-in. Returns (client_id, token response)."""
        client_id = self.client.post("/oauth/register", json={"client_name": "Claude",
                                                              "redirect_uris": [CALLBACK]}).json()["client_id"]
        verifier, challenge = pkce()
        query = {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "s1",
                 "code_challenge": challenge, "code_challenge_method": "S256", "scope": "dieter"}
        self.sign_in()
        decision = self.client.post("/oauth/authorize", data=dict(query, decision="allow"), follow_redirects=False)
        code = parse_qs(urlsplit(decision.headers["location"]).query)["code"][0]
        tokens = self.client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                                        "client_id": client_id, "redirect_uri": CALLBACK,
                                                        "code_verifier": verifier}).json()
        self.client.cookies.clear()  # from here on Claude only has its token
        return client_id, tokens

    def test_chatgpt_connects_the_same_way(self):
        chatgpt = "https://chatgpt.com/connector_platform_oauth_redirect"
        registered = self.client.post("/oauth/register", json={"redirect_uris": [chatgpt]}).json()
        self.assertEqual(registered["client_name"], "ChatGPT")
        meta = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertIn("offline_access", meta["scopes_supported"])
        self.assertTrue(meta["authorization_response_iss_parameter_supported"])
        verifier, challenge = pkce()
        query = {"response_type": "code", "client_id": registered["client_id"], "redirect_uri": chatgpt,
                 "state": "s1", "code_challenge": challenge, "code_challenge_method": "S256",
                 "scope": "dieter offline_access", "resource": "http://testserver/mcp"}
        self.sign_in()
        page = self.client.get("/oauth/authorize", params=query)
        self.assertIn("Let ChatGPT use your Dieter?", page.text)
        decision = self.client.post("/oauth/authorize", data=dict(query, decision="allow"), follow_redirects=False)
        location = decision.headers["location"]
        self.assertTrue(location.startswith(chatgpt + "?"))
        answer = parse_qs(urlsplit(location).query)
        self.assertEqual(answer["iss"], ["http://testserver"])
        tokens = self.client.post("/oauth/token", data={"grant_type": "authorization_code", "code": answer["code"][0],
                                                        "client_id": registered["client_id"], "redirect_uri": chatgpt,
                                                        "code_verifier": verifier,
                                                        "resource": "http://testserver/mcp"}).json()
        self.assertIn("refresh_token", tokens)
        self.client.cookies.clear()
        names = {t["name"] for t in self.rpc(tokens["access_token"], "tools/list").json()["result"]["tools"]}
        self.assertIn("assistant_get_project", names)

    def rpc(self, token, method, params=None, msg_id=1):
        return self.client.post("/mcp", headers={"Authorization": f"Bearer {token}"},
                                json={"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})

    def tool(self, token, name, **arguments):
        answer = self.rpc(token, "tools/call", {"name": name, "arguments": arguments}).json()["result"]
        text = answer["content"][0]["text"]
        if answer["isError"]:
            return {"error": text}
        try:
            return json.loads(text)
        except ValueError:
            return text

    # ---------------------------------------------------------------- sign-in

    def test_unsigned_requests_are_pointed_at_sign_in(self):
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers["www-authenticate"],
                         'Bearer resource_metadata="http://testserver/.well-known/oauth-protected-resource"')
        bad = self.client.post("/mcp", headers={"Authorization": "Bearer nope"}, json={})
        self.assertIn('error="invalid_token"', bad.headers["www-authenticate"])
        resource = self.client.get("/.well-known/oauth-protected-resource").json()
        self.assertEqual(resource["resource"], "http://testserver/mcp")
        server = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(server["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(server["registration_endpoint"], "http://testserver/oauth/register")
        behind_proxy = self.client.get("/.well-known/oauth-protected-resource",
                                       headers={"host": "askdieter.ai"}).json()
        self.assertEqual(behind_proxy["resource"], "https://askdieter.ai/mcp")

    def test_registration_only_accepts_claude(self):
        self.assertEqual(self.client.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]})
                         .json()["error"], "invalid_redirect_uri")
        self.assertEqual(self.client.post("/oauth/register", json={}).status_code, 400)
        made = self.client.post("/oauth/register", json={"redirect_uris": [CALLBACK]})
        self.assertEqual(made.status_code, 201)
        self.assertEqual(made.json()["token_endpoint_auth_method"], "none")

    def test_sign_in_needs_dieter_login_then_allow(self):
        client_id = self.client.post("/oauth/register", json={"redirect_uris": [CALLBACK]}).json()["client_id"]
        verifier, challenge = pkce()
        query = {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "xyz",
                 "code_challenge": challenge, "code_challenge_method": "S256"}
        r = self.client.get("/oauth/authorize", params=query, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/login?next=%2Foauth%2Fauthorize%3F"))
        self.sign_in()
        page = self.client.get("/oauth/authorize", params=query)
        self.assertIn("Let Claude use your Dieter?", page.text)
        self.assertIn("claude.ai", page.text)
        self.assertIn("Connector Cook", page.text)
        # Wrong redirect or no PKCE: a problem page, nothing sent anywhere
        self.assertEqual(self.client.get("/oauth/authorize", params=dict(query, redirect_uri="http://localhost/x"),
                                         follow_redirects=False).status_code, 400)
        self.assertEqual(self.client.get("/oauth/authorize", params=dict(query, code_challenge=""),
                                         follow_redirects=False).status_code, 400)
        denied = self.client.post("/oauth/authorize", data=dict(query, decision="deny"), follow_redirects=False)
        self.assertEqual(parse_qs(urlsplit(denied.headers["location"]).query)["error"], ["access_denied"])
        allowed = self.client.post("/oauth/authorize", data=dict(query, decision="allow"), follow_redirects=False)
        back = urlsplit(allowed.headers["location"])
        self.assertEqual(f"{back.scheme}://{back.netloc}{back.path}", CALLBACK)
        got = parse_qs(back.query)
        self.assertEqual((got["state"], got["iss"]), (["xyz"], ["http://testserver"]))
        exchange = {"grant_type": "authorization_code", "code": got["code"][0], "client_id": client_id,
                    "redirect_uri": CALLBACK, "code_verifier": verifier + "x"}
        self.assertEqual(self.client.post("/oauth/token", data=exchange).json()["error"], "invalid_grant")
        exchange["code_verifier"] = verifier
        tokens = self.client.post("/oauth/token", data=exchange)
        self.assertEqual(tokens.headers["cache-control"], "no-store")
        self.assertEqual(tokens.json()["token_type"], "Bearer")
        self.assertEqual(self.client.post("/oauth/token", data=exchange).json()["error"], "invalid_grant")  # used

    def test_refresh_tokens_rotate_and_reuse_ends_the_sign_in(self):
        client_id, tokens = self.connect()
        first = self.client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                       "refresh_token": tokens["refresh_token"]}).json()
        self.assertEqual(self.rpc(tokens["access_token"], "ping").status_code, 401)  # the old access token is done
        self.assertEqual(self.rpc(first["access_token"], "ping").json()["result"], {})
        reused = self.client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                        "refresh_token": tokens["refresh_token"]})
        self.assertEqual(reused.json()["error"], "invalid_grant")
        self.assertEqual(self.rpc(first["access_token"], "ping").status_code, 401)  # possible theft: all revoked

    def test_guest_accounts_cant_connect(self):
        self.assertIsNone(oauth.usable({"id": 3, "role": "guest", "status": "active"}))
        self.assertIsNone(oauth.usable({"id": 3, "role": "member", "status": "pending"}))
        self.assertTrue(oauth.usable({"id": 3, "role": "member", "status": "active"}))

    # ---------------------------------------------------------------- MCP

    def test_initialize_and_list_tools(self):
        _, tokens = self.connect()
        token = tokens["access_token"]
        init = self.rpc(token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "test", "version": "1"}}).json()["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["capabilities"])
        self.assertIn("Dieter", init["instructions"])
        note = self.client.post("/mcp", headers={"Authorization": f"Bearer {token}"},
                                json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(note.status_code, 202)
        names = {t["name"] for t in self.rpc(token, "tools/list").json()["result"]["tools"]}
        self.assertTrue({"kitchen_get_shopping_list", "kitchen_finish_shopping_list", "thanksgiving_get_plan",
                         "thanksgiving_finish_trip", "thanksgiving_change_plan", "dieter_open_page"} <= names)
        proposal = self.tool(token, "thanksgiving_propose_recipe_change", dish="Gravy", reason="Richer",
                             notes="Make it the day before.")
        # This user has no Thanksgiving plan yet (test_thanksgiving_tools covers one that does)
        self.assertIn("no Thanksgiving plan", proposal.get("error", ""))
        link = self.tool(token, "dieter_open_page", page="thanksgiving.shopping")
        self.assertTrue(link["link"].endswith("/thanksgiving/shopping"))
        self.assertEqual(self.rpc(token, "tools/call", {"name": "music_play"}).json()["error"]["code"], -32602)
        self.assertEqual(self.rpc(token, "nope/nope").json()["error"]["code"], -32601)
        self.assertEqual(self.client.get("/mcp").status_code, 405)

    def test_tools_added_after_connecting_are_still_reachable(self):
        # Clients keep the tool list from when the person connected; these two always reach every tool there is now.
        _, tokens = self.connect()
        token = tokens["access_token"]
        names = [t["name"] for t in self.rpc(token, "tools/list").json()["result"]["tools"]]
        self.assertEqual(names[:2], ["dieter_find_tools", "dieter_use_tool"])
        found = self.tool(token, "dieter_find_tools", query="shopping list create")["tools"]
        self.assertIn("kitchen_create_shopping_list", [t["name"] for t in found])
        self.assertIn("properties", found[0]["inputSchema"])
        self.assertGreater(len(self.tool(token, "dieter_find_tools")["tools"]), 20)
        def use(**arguments):  # self.tool's own first argument is called name
            answer = self.rpc(token, "tools/call", {"name": "dieter_use_tool", "arguments": arguments}).json()["result"]
            return answer["content"][0]["text"] if answer["isError"] else json.loads(answer["content"][0]["text"])

        made = use(name="kitchen_create_shopping_list", arguments={"title": "Via the back door", "items": ["foil"]})
        self.assertEqual(made["title"], "Via the back door")
        self.assertIn("no tool", use(name="music_play"))
        self.assertIn("no tool", use(name="dieter_use_tool", arguments={"name": "dieter_find_tools"}))

    def test_file_tools_must_be_called_directly(self):
        # ChatGPT attaches files only to tools in its own list (it reads their openai/fileParams), so the
        # dieter_use_tool wrapper refuses them with instructions instead of failing obscurely.
        _, tokens = self.connect()
        token = tokens["access_token"]
        order = [t["name"] for t in self.rpc(token, "tools/list").json()["result"]["tools"]]
        self.assertEqual(order[:3], ["dieter_find_tools", "dieter_use_tool", "media_test_image"])  # can't fall off the end
        listed = {t["name"]: t for t in self.rpc(token, "tools/list").json()["result"]["tools"]}
        self.assertEqual(listed["media_test_image"]["_meta"], {"openai/fileParams": ["image"]})
        found = {t["name"]: t for t in self.tool(token, "dieter_find_tools", query="image")["tools"]}
        self.assertEqual(found["media_test_image"]["takes_files"], ["image"])
        self.assertNotIn("takes_files", found.get("media_test_results", {}))
        answer = self.rpc(token, "tools/call", {"name": "dieter_use_tool", "arguments": {
            "name": "media_test_image", "arguments": {"image": {"file_id": "file_1"}}}}).json()["result"]
        self.assertTrue(answer["isError"])
        self.assertEqual(json.loads(answer["content"][0]["text"])["error"], "needs_direct_call")

    def test_kitchen_shopping_lists(self):
        _, tokens = self.connect()
        token = tokens["access_token"]
        made = self.tool(token, "kitchen_create_shopping_list", title="Costco run", items=["2 lemons", "foil"])
        self.assertEqual(made["title"], "Costco run")
        self.assertTrue(made["url"].endswith(f"/apps/recipes/grocery-lists/{made['id']}"))
        listed = self.tool(token, "kitchen_list_shopping_lists")
        self.assertIn(made["id"], [g["id"] for g in listed["lists"]])
        view = self.tool(token, "kitchen_add_items", list_id=made["id"], items="whole milk")
        keys = {i["text"]: i["key"] for a in view["aisles"] for i in a["items"]}
        self.assertEqual(set(keys), {"2 lemons", "foil", "whole milk"})
        view = self.tool(token, "kitchen_check_items", list_id=made["id"], keys=[keys["foil"]])
        self.assertEqual(view["items_left"], 2)
        self.assertIn("aren't on list", self.tool(token, "kitchen_check_items", list_id=made["id"],
                                                  keys=["item-unicorn"])["error"])
        view = self.tool(token, "kitchen_remove_items", list_id=made["id"], keys=[keys["2 lemons"]])
        self.assertEqual(view["items_total"], 2)
        done = self.tool(token, "kitchen_finish_shopping_list", list_id=made["id"], carry_over=[keys["whole milk"]])
        self.assertEqual(done["finished"]["status"], "done")
        self.assertTrue(done["next_trip"]["title"].startswith("Next trip"))
        self.assertEqual([i["text"] for a in done["next_trip"]["aisles"] for i in a["items"]], ["whole milk"])
        self.assertIn("no shopping list", self.tool(token, "kitchen_get_shopping_list", list_id=999999)["error"])

    def test_other_people_cant_reach_my_lists(self):
        _, tokens = self.connect()
        mine = self.tool(tokens["access_token"], "kitchen_create_shopping_list", items=["secret sauce"])
        other = self.db.create_user(f"other-{uuid.uuid4().hex[:8]}@example.com", "Other", "x", "member")
        conn = self.db.connect()
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (other,))
        conn.commit()
        theirs = oauth.OAuth(self.db, lambda uid: dict(self.db.get_user_by_id(uid) or {}) or None) \
            .issue("someone", other, "dieter")["access_token"]
        self.assertIn("no shopping list", self.tool(theirs, "kitchen_get_shopping_list", list_id=mine["id"])["error"])

    def test_thanksgiving_tools(self):
        self.sign_in()
        self.client.get("/thanksgiving")  # first visit makes the plan
        _, tokens = self.connect()
        token = tokens["access_token"]
        plan = self.tool(token, "thanksgiving_get_plan")
        self.assertIn("Dinner:", plan)
        self.assertIn("Pumpkin pie", plan)
        pie = self.tool(token, "thanksgiving_get_dish", dish="pumpkin")
        self.assertEqual(pie["name"], "Pumpkin pie")
        self.assertTrue(pie["steps"])
        listing = self.tool(token, "thanksgiving_add_to_shopping_list", items=["1 pint heavy whipping cream"])
        items = {i["text"]: i for a in listing["aisles"] for i in a["items"]}
        cream = items["1 pint heavy whipping cream"]
        self.assertEqual(cream["state"], "to buy")
        others = [i["key"] for i in items.values() if i["key"] != cream["key"]]
        after = self.tool(token, "thanksgiving_finish_trip", next_trip=[cream["key"]], got=others[:1],
                          dont_need=others[1:2])
        states = {i["key"]: i["state"] for a in after["aisles"] for i in a["items"]}
        self.assertEqual((states[cream["key"]], states[others[0]], states[others[1]]),
                         ("on a next-trip list", "in the cart", "don't need"))
        self.assertEqual(after["next_trip_list"]["items"], 1)
        # The cream is on the Kitchen list as Thanksgiving's item; buying it there puts it in the cart here.
        cream_after = {i["key"]: i for a in after["aisles"] for i in a["items"]}[cream["key"]]
        self.assertTrue(cream_after["kitchen_list_url"].endswith(f"/apps/recipes/grocery-lists/{after['next_trip_list']['id']}"))
        kitchen = self.tool(token, "kitchen_get_shopping_list", list_id=after["next_trip_list"]["id"])
        on_kitchen = [i for a in kitchen["aisles"] for i in a["items"]]
        self.assertEqual([(i["text"], i["from_app"]) for i in on_kitchen],
                         [("1 pint heavy whipping cream", ["Thanksgiving"])])
        self.tool(token, "kitchen_check_items", list_id=after["next_trip_list"]["id"], keys=[on_kitchen[0]["key"]])
        states = {i["key"]: i["state"] for a in self.tool(token, "thanksgiving_get_shopping_list")["aisles"]
                  for i in a["items"]}
        self.assertEqual(states[cream["key"]], "in the cart")
        # Sent back from the Kitchen list: to buy here again.
        self.tool(token, "kitchen_check_items", list_id=after["next_trip_list"]["id"], keys=[on_kitchen[0]["key"]],
                  checked=False)
        self.tool(token, "kitchen_remove_items", list_id=after["next_trip_list"]["id"], keys=[on_kitchen[0]["key"]])
        states = {i["key"]: i["state"] for a in self.tool(token, "thanksgiving_get_shopping_list")["aisles"]
                  for i in a["items"]}
        self.assertEqual(states[cream["key"]], "to buy")
        back = self.tool(token, "thanksgiving_put_back", keys=[others[1]])
        self.assertEqual({i["key"]: i["state"] for a in back["aisles"] for i in a["items"]}[others[1]], "to buy")
        preview = self.tool(token, "thanksgiving_change_plan", preview=True,
                            actions=[{"type": "dinner", "date": "2026-11-26", "time": "17:00"}])
        self.assertFalse(preview["applied"])
        self.assertIn("Thu 5:00 PM", preview["changes"][0])
        saved = self.tool(token, "thanksgiving_change_plan",
                          actions=[{"type": "dinner", "date": "2026-11-26", "time": "17:00"}, {"type": "assign"}])
        self.assertTrue(saved["applied"])
        self.assertEqual(len(saved["problems"]), 1)
        self.assertIn("Dinner: Thursday 2026-11-26 17:00", self.tool(token, "thanksgiving_get_plan"))
        self.assertEqual(self.tool(token, "thanksgiving_undo_last_change"), {"undone": True})
        self.assertIn("Dinner: Thursday 2026-11-26 16:00", self.tool(token, "thanksgiving_get_plan"))
        self.assertIn("no recent change", self.tool(token, "thanksgiving_undo_last_change")["error"])
        proposal = self.tool(token, "thanksgiving_propose_recipe_change", dish="Gravy", notes="Make it the day before.",
                             reason="Less to do on the day")
        self.assertTrue(proposal["review_link"].endswith(f"/thanksgiving/menu/{proposal['dish_id']}"))
        self.assertEqual(proposal["status"], "pending")
        self.assertNotEqual(self.tool(token, "thanksgiving_get_dish", dish="Gravy")["notes"], "Make it the day before.")
        self.assertTrue(re.search(r"Not on the Thanksgiving list", self.tool(token, "thanksgiving_check_items",
                                                                            keys=["item-nope"])["error"]))


if __name__ == "__main__":
    unittest.main()
