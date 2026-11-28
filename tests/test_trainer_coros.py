"""COROS (apps/trainer/coros.py): connect with OAuth (discovery, registration, PKCE), list activities and save FIT
files through COROS's MCP server, against a fake COROS."""

import base64
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from apps.trainer import coros
from database import Database

MCP = "https://mcpus.coros.com/mcp"
AS = "https://mcpus.coros.com"
FIT = b"\x0e\x10fake-fit-bytes.FIT"


class FakeCoros:
    def __init__(self):
        self.calls, self.registered, self.codes, self.tokens, self.sessions = [], [], {}, {}, 0
        self.expired = False

    def __call__(self, url, method="GET", form=None, body=None, headers=None, raw=False):
        self.calls.append((method, url, form, body))
        if url == "https://mcpus.coros.com/.well-known/oauth-protected-resource":
            return 200, {}, json.dumps({"resource": AS, "authorization_servers": [AS],
                                        "scopes_supported": ["openid", "mcp.tools", "offline_access"]})
        if url == AS + "/.well-known/oauth-authorization-server":
            return 200, {}, json.dumps({"issuer": AS, "authorization_endpoint": AS + "/oauth2/authorize",
                                        "token_endpoint": AS + "/oauth2/token",
                                        "registration_endpoint": AS + "/connect/register"})
        if url == AS + "/connect/register":
            self.registered.append(body)
            return 201, {}, json.dumps({"client_id": "dieter-client"})
        if url == AS + "/oauth2/token":
            if form["grant_type"] == "authorization_code":
                challenge = self.codes.pop(form["code"])
                digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
                assert digest.decode() == challenge, "PKCE verifier doesn't match"
                return 200, {}, json.dumps({"access_token": "a1", "refresh_token": "r1", "expires_in": 3600})
            if form["grant_type"] == "refresh_token":
                assert form["refresh_token"] == "r1"
                self.expired = False
                return 200, {}, json.dumps({"access_token": "a2", "refresh_token": "r1", "expires_in": 3600})
        if url == MCP:
            if self.expired:
                return 401, {}, "expired"
            message = body
            if message["method"] == "initialize":
                self.sessions += 1
                return 200, {"Mcp-Session-Id": f"s{self.sessions}"}, json.dumps(
                    {"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": coros.PROTOCOL}})
            if message["method"] == "notifications/initialized":
                return 202, {}, ""
            assert headers.get("Mcp-Session-Id"), "tools/call without a session"
            name, args = message["params"]["name"], message["params"]["arguments"]
            if name == "querySportRecords":
                rows = [] if not args["startDate"] <= "20261126" <= args["endDate"] else [
                    {"labelId": "L1", "sportType": 100, "name": "Gilbert turkey trot", "date": 20261126,
                     "distance": 4800, "totalTime": 1025},
                    {"labelId": "L2", "sportType": 402, "name": "Weights", "date": 20261125}]
                text = json.dumps({"records": rows})
                return 200, {}, "event: message\ndata: " + json.dumps(
                    {"jsonrpc": "2.0", "id": message["id"],
                     "result": {"content": [{"type": "text", "text": text}]}}) + "\n\n"
            if name == "downloadActivityFitFiles":
                return 200, {}, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"content": [
                    {"type": "resource", "resource": {"uri": f"file://{args['labelId']}.fit",
                                                      "mimeType": "application/octet-stream",
                                                      "blob": base64.b64encode(FIT).decode()}}]}})
        raise AssertionError(f"unexpected call {method} {url}")


class MemoryBlobs:
    def __init__(self):
        self.files = {}

    def put(self, key, data):
        self.files[key] = data

    def get(self, key):
        return self.files.get(key)


class CorosTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(str(Path(tempfile.mkdtemp()) / "t.db"))
        self.db.init()
        self.fake, self.blobs = FakeCoros(), MemoryBlobs()
        self.c = coros.Coros(self.db, self.blobs, call=self.fake, mcp_url=MCP)

    def connect(self):
        address = self.c.start(7, "https://askdieter.ai/apps/trainer/coros/callback")
        query = parse_qs(urlsplit(address).query)
        self.assertTrue(address.startswith(AS + "/oauth2/authorize?"))
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["scope"], [coros.SCOPES])
        self.fake.codes["code-1"] = query["code_challenge"][0]
        self.c.finish(7, query["state"][0], "code-1")

    def test_connect_list_and_save_fit_files(self):
        self.assertFalse(self.c.status(7)["connected"])
        self.connect()
        self.assertEqual(self.fake.registered[0]["redirect_uris"], ["https://askdieter.ai/apps/trainer/coros/callback"])
        self.assertEqual(self.fake.registered[0]["token_endpoint_auth_method"], "none")
        self.c.catch_up_list(7, today=date(2026, 11, 28))
        self.c.fetch_fits(7)
        status = self.c.status(7)
        self.assertTrue(status["connected"])
        self.assertEqual((status["found"], status["runs"], status["saved"]), (2, 1, 2))
        self.assertEqual(status["newest"], "2026-11-26")
        self.assertEqual(self.blobs.files["coros/7/L1.fit"], FIT)
        # registering happens once per callback address
        self.c.start(7, "https://askdieter.ai/apps/trainer/coros/callback")
        self.assertEqual(len(self.fake.registered), 1)

    def test_an_expired_sign_in_is_refreshed(self):
        self.connect()
        self.fake.expired = True
        self.c.list_window(7, date(2026, 11, 1), date(2026, 11, 28))
        self.assertEqual(self.c.connected(7)["access_token"], "a2")

    def test_the_daily_allowance_is_kept(self):
        self.connect()
        self.c.catch_up_list(7, today=date(2026, 11, 28))
        conn = self.db.connect()
        for i in range(coros.FIT_PER_DAY):
            conn.execute("INSERT INTO tr_coros_activities (user_id, label_id, fit_at, fit_key) VALUES (7, ?, "
                         "datetime('now'), 'k')", (f"old{i}",))
        conn.commit()
        self.assertEqual(self.c.fetch_fits(7), 0)
        self.assertEqual(self.blobs.files, {})

    def test_a_stale_state_is_refused(self):
        self.c.start(7, "https://askdieter.ai/cb")
        with self.assertRaises(coros.CorosError):
            self.c.finish(7, "not-the-state", "code")

    def test_reading_coros_answers(self):
        self.assertEqual(coros.sse_messages('event: message\ndata: {"id": 2}\n\n'), [{"id": 2}])
        self.assertEqual(coros._start_text(20261126), ("2026-11-26", ""))
        self.assertEqual(coros._start_text("1764150000")[0], "2025-11-26")


if __name__ == "__main__":
    unittest.main()
