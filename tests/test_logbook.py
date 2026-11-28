"""The Logbook (core/logbook): types, versions, proposals, the pages, and the tools ChatGPT and Claude use."""

import secrets
import tempfile
import unittest
import uuid
import warnings
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from core.connector import oauth
from core.logbook import types
from core.logbook.report import summarize
from core.logbook.store import LogbookStore
from database import Database, reset_current_user_id, set_current_user_id

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "examples"
LEGS = (EXAMPLES / "ragnar_trail_arizona_2026_legs.csv").read_text(encoding="utf-8")
LOOPS = (EXAMPLES / "ragnar_trail_arizona_2026_loops.csv").read_text(encoding="utf-8")


class TypeTests(unittest.TestCase):
    def test_values(self):
        self.assertEqual(types.to_value("duration", "30:35"), 1835)
        self.assertEqual(types.to_value("duration", "1:02:45"), 3765)
        self.assertEqual(types.to_value("duration", "75:10"), 4510)  # minutes past an hour, written m:ss
        self.assertEqual(types.show("duration", 3765), "1:02:45")
        self.assertEqual(types.to_value("number", "4.95"), 4.95)
        self.assertEqual(types.to_value("integer", "1,200"), 1200)
        self.assertEqual(types.to_value("date", "2026-11-06"), "2026-11-06")
        self.assertIs(types.to_value("boolean", "Yes"), True)
        self.assertIsNone(types.to_value("number", "  "))
        for kind, bad in (("duration", "1:75:00"), ("duration", "fast"), ("date", "Nov 6"), ("integer", "4.5"),
                          ("number", "four"), ("boolean", "maybe"), ("number", True)):
            with self.assertRaises(types.Invalid, msg=(kind, bad)):
                types.to_value(kind, bad)

    def test_ragnar_csv_types_are_guessed(self):
        columns, rows = types.read_csv(LEGS, 5000)
        self.assertEqual([(c["name"], c["type"]) for c in columns],
                         [("leg", "integer"), ("loop", "text"), ("runner", "text"), ("distance_mi", "number"),
                          ("time", "duration"), ("pace", "duration"), ("day_night", "text")])
        self.assertEqual(columns[3]["label"], "Distance mi")
        self.assertEqual(len(rows), 24)

    def test_every_bad_value_is_listed_and_nothing_is_kept(self):
        columns = types.check_columns([{"name": "leg", "type": "integer"}, {"name": "time", "type": "duration"}])
        with self.assertRaises(types.Invalid) as caught:
            types.check_rows(columns, [{"leg": 1, "time": "30:35"}, {"leg": "two", "time": "soon"},
                                       {"leg": 3, "pace": "8:00"}])
        message = str(caught.exception)
        self.assertTrue(message.startswith("Nothing was saved"))
        for bit in ("row 2, leg", "row 2, time", "row 3: no column called 'pace'"):
            self.assertIn(bit, message)

    def test_column_rules(self):
        for bad in ([], [{"name": "Leg", "type": "integer"}], [{"name": "leg", "type": "float"}],
                    [{"name": "leg", "type": "text"}, {"name": "leg", "type": "text"}]):
            with self.assertRaises(types.Invalid):
                types.check_columns(bad)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(str(Path(tempfile.mkdtemp()) / "logbook.db"))
        self.store = LogbookStore(self.db)
        self.token = set_current_user_id(1)

    def tearDown(self):
        reset_current_user_id(self.token)
        self.db.conn.close()

    def legs(self):
        columns, rows = types.read_csv(LEGS, 5000)
        return self.store.create("Ragnar legs", columns, rows)

    def test_verified_stays_the_answer_while_a_draft_waits(self):
        table_id, v1 = self.legs()
        table, version = self.store.version(table_id, "verified")
        self.assertIsNone(version)  # nothing checked yet
        self.store.verify(table_id, v1)
        _, latest = self.store.version(table_id, "latest")
        change = {"update": [{"row_id": "r11", "values": {"time": "46:00"}}]}
        v2 = self.store.apply_change(table_id, latest["id"], change, "chatgpt", "Hunt's corrected time")
        _, verified = self.store.version(table_id, "verified")
        self.assertEqual(verified["id"], v1)
        self.assertEqual(next(r for r in verified["rows"] if r["row_id"] == "r11")["time"], 2775)  # 46:15
        self.store.verify(table_id, v2)
        _, verified = self.store.version(table_id, "verified")
        self.assertEqual(verified["id"], v2)
        statuses = {v["number"]: v["status"] for v in self.store.table(table_id)["versions"]}
        self.assertEqual(statuses, {1: "superseded", 2: "verified"})

    def test_changes_from_an_old_version_are_refused(self):
        table_id, v1 = self.legs()
        self.store.apply_change(table_id, v1, {"remove": ["r24"]}, "claude", "test")
        with self.assertRaises(types.Invalid) as caught:
            self.store.apply_change(table_id, v1, {"remove": ["r23"]}, "claude", "test")
        self.assertIn("has changed since", str(caught.exception))

    def test_row_ids_survive_and_new_rows_get_new_ones(self):
        table_id, v1 = self.legs()
        v2 = self.store.apply_change(table_id, v1, {"remove": ["r24"], "add": [
            {"leg": 25, "loop": "Green", "runner": "Eli", "distance_mi": 4.1, "time": "31:00", "pace": "7:34",
             "day_night": "Day"}]}, "claude", "test")
        _, version = self.store.version(table_id, v2)
        ids = [r["row_id"] for r in version["rows"]]
        self.assertEqual(ids[0], "r1")
        self.assertNotIn("r24", ids)
        self.assertEqual(ids[-1], "r25")
        v3 = self.store.apply_change(table_id, v2, {"remove": ["r25"]}, "claude", "test")
        v4 = self.store.apply_change(table_id, v3, {"add": [{"leg": 26}]}, "claude", "test")
        _, version = self.store.version(table_id, v4)
        self.assertEqual(version["rows"][-1]["row_id"], "r26")  # never reuses an id

    def test_a_bad_change_saves_nothing(self):
        table_id, v1 = self.legs()
        with self.assertRaises(types.Invalid) as caught:
            self.store.apply_change(table_id, v1, {"update": [{"row_id": "r1", "values": {"time": "fast"}},
                                                              {"row_id": "r99", "values": {"time": "1:00"}}],
                                                   "add": [{"leg": "x"}]}, "chatgpt", "test")
        for bit in ("update 1 (r1), time", "update 2: no row 'r99'", "add row 1, leg"):
            self.assertIn(bit, str(caught.exception))
        self.assertEqual(len(self.store.table(table_id)["versions"]), 1)

    def test_proposals_wait_and_go_stale(self):
        table_id, v1 = self.legs()
        first = self.store.propose("change", {"remove": ["r1"]}, {}, "chatgpt", "one", table_id, v1)
        second = self.store.propose("change", {"remove": ["r2"]}, {}, "chatgpt", "two", table_id, v1)
        self.assertEqual(len(self.store.proposals()), 2)
        self.store.approve(first)
        with self.assertRaises(types.Invalid):
            self.store.approve(second)  # the table moved on
        self.assertEqual(self.store.proposal(second)["status"], "stale")
        self.assertEqual(self.store.proposal(first)["status"], "approved")

    def test_other_people_cant_see_my_tables(self):
        table_id, _ = self.legs()
        other = set_current_user_id(2)
        try:
            self.assertEqual(self.store.tables(), [])
            with self.assertRaises(types.Invalid):
                self.store.table(table_id)
        finally:
            reset_current_user_id(other)

    def test_dieter_does_the_ragnar_arithmetic(self):
        table_id, v1 = self.legs()
        _, version = self.store.version(table_id, v1)
        overall = summarize(version["columns"], version["rows"])["overall"]
        self.assertEqual(overall["time"]["total"], "17:57:06")
        self.assertEqual(overall["distance_mi"]["total"], 120.4)
        by_loop = summarize(version["columns"], version["rows"], "loop", ["time"])
        green = next(g for g in by_loop["groups"] if g["value"] == "Green")
        self.assertEqual(green["time"]["min"], {"value": "30:35", "row_id": "r1"})  # Sterling's leg 1, not Eli's
        by_runner = summarize(version["columns"], version["rows"], "runner", ["distance_mi", "time"])
        joe = next(g for g in by_runner["groups"] if g["value"] == "Joe")
        self.assertEqual((joe["rows"], joe["distance_mi"]["total"], joe["time"]["total"]), (4, 21.05, "3:10:50"))


class LogbookFlowTests(unittest.TestCase):
    """The pages and the connector, through the real app."""

    @classmethod
    def setUpClass(cls):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        cls.api, cls.db = api, api.db
        cls.user_id = cls.db.create_user(f"logbook-{uuid.uuid4().hex[:8]}@example.com", "Logbook Keeper", "x",
                                         "member")
        conn = cls.db.connect()
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (cls.user_id,))
        conn.commit()
        cls.token = oauth.OAuth(cls.db, lambda uid: dict(cls.db.get_user_by_id(uid) or {}) or None) \
            .issue("chatgpt-test", cls.user_id, "dieter")["access_token"]

    def setUp(self):
        self.client = TestClient(self.api.app)
        session = secrets.token_urlsafe(16)
        self.db.create_session(self.user_id, self.api.hash_session_token(session),
                               (datetime.utcnow() + timedelta(days=1)).isoformat())
        self.client.cookies.set(self.api.SESSION_COOKIE_NAME, session)
        self.mcp = TestClient(self.api.app)  # only the token: what ChatGPT has

    def tool(self, tool_name, **arguments):
        import json
        answer = self.mcp.post("/mcp", headers={"Authorization": f"Bearer {self.token}"},
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": tool_name, "arguments": arguments}}).json()["result"]
        text = answer["content"][0]["text"]
        return {"error": text} if answer["isError"] else json.loads(text)

    def upload(self, name, text):
        page = self.client.post("/logbook/upload", data={"name": name},
                                files={"file": ("legs.csv", text.encode(), "text/csv")})
        self.assertEqual(page.status_code, 200, page.text[:500])
        self.assertIn("Check the columns", page.text)
        form = {"name": name, "csv_text": text}
        columns, _ = types.read_csv(text, 5000)
        for i, c in enumerate(columns):
            form[f"type_{i}"], form[f"label_{i}"] = c["type"], c["label"]
        saved = self.client.post("/logbook/create", data=form, follow_redirects=False)
        self.assertEqual(saved.status_code, 303)
        return saved.headers["location"].split("?")[0].rsplit("/", 1)[-1]

    def test_ragnar_end_to_end(self):
        name = f"Ragnar legs {uuid.uuid4().hex[:6]}"
        table_id = self.upload(name, LEGS)
        page = self.client.get(f"/logbook/{table_id}")
        self.assertIn("Sterling", page.text)
        self.assertIn("mark verified", page.text)

        found = self.tool("logbook_find", query=name.lower())["tables"]
        self.assertEqual([t["table_id"] for t in found], [table_id])
        self.assertIsNone(found[0]["verified_version"])
        unverified = self.tool("logbook_read_rows", table_id=table_id)
        self.assertFalse(unverified["verified"])
        self.assertNotIn("rows", unverified)
        self.assertFalse(self.tool("logbook_summarize", table_id=table_id)["verified"])

        latest = self.tool("logbook_read_rows", table_id=table_id, version="latest", limit=10)
        self.assertEqual((latest["total_rows"], latest["next_offset"], latest["rows"][0]["time"]), (24, 10, "30:35"))
        verify = self.client.post(f"/logbook/{table_id}/verify", data={"version_id": latest["version_id"]},
                                  follow_redirects=False)
        self.assertEqual(verify.status_code, 303)

        rows = self.tool("logbook_read_rows", table_id=table_id)
        self.assertTrue(rows["verified"])
        self.assertIn("v1, verified", rows["label"])
        fastest = self.tool("logbook_summarize", table_id=table_id, group_by="loop", columns=["time"])
        green = next(g for g in fastest["groups"] if g["value"] == "Green")
        self.assertEqual(green["time"]["min"]["row_id"], "r1")

        # ChatGPT proposes a fix; nothing changes until it's approved on the page.
        proposed = self.tool("logbook_propose_change", table_id=table_id, base_version_id=latest["version_id"],
                             update=[{"row_id": "r4", "values": {"time": "32:30"}}], assistant="chatgpt",
                             reason="Eli's watch said 32:30")
        self.assertEqual(proposed["status"], "pending")
        self.assertEqual(self.tool("logbook_read_rows", table_id=table_id)["rows"][3]["time"], "32:35")
        self.assertIn("Waiting for your OK", self.client.get("/").text)
        review = self.client.get(f"/logbook/proposals/{proposed['proposal_id']}")
        self.assertIn("Eli&#39;s watch said 32:30", review.text)
        self.client.post(f"/logbook/proposals/{proposed['proposal_id']}/approve")
        self.assertEqual(self.tool("logbook_read_rows", table_id=table_id)["rows"][3]["time"], "32:35")  # verified v1
        self.assertEqual(self.tool("logbook_read_rows", table_id=table_id, version="latest")["rows"][3]["time"],
                         "32:30")

        stale = self.tool("logbook_propose_change", table_id=table_id, base_version_id=latest["version_id"],
                          remove=["r1"], assistant="chatgpt", reason="test")
        self.assertIn("has changed since", stale["error"])
        bad = self.tool("logbook_propose_change", table_id=table_id,
                        base_version_id=self.tool("logbook_get", table_id=table_id)["latest_version"]["version_id"],
                        add=[{"leg": "twenty-five", "time": "soon"}], assistant="chatgpt", reason="test")
        self.assertIn("Nothing was saved", bad["error"])
        self.assertIn("add row 1, leg", bad["error"])

    def test_assistants_can_propose_a_table_but_not_approve_or_verify(self):
        names = {t["name"] for t in self.mcp.post("/mcp", headers={"Authorization": f"Bearer {self.token}"},
                                                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                 .json()["result"]["tools"]}
        self.assertIn("logbook_read_rows", names)
        self.assertFalse([n for n in names if n.startswith("logbook_") and ("approve" in n or "verify" in n)])

        name = f"Ragnar loops {uuid.uuid4().hex[:6]}"
        columns, rows = types.read_csv(LOOPS, 100)
        made = self.tool("logbook_propose_create", name=name, columns=columns, rows=rows, assistant="claude",
                         reason="From the results image")
        self.assertEqual(made["status"], "pending")
        self.assertEqual(self.tool("logbook_find", query=name.lower())["tables"], [])
        self.assertIn("reason is required", self.tool("logbook_propose_create", name="x", columns=columns, rows=[],
                                                      assistant="claude")["error"])
        approved = self.client.post(f"/logbook/proposals/{made['proposal_id']}/approve", follow_redirects=False)
        self.assertEqual(approved.status_code, 303)
        self.assertEqual(len(self.tool("logbook_find", query=name.lower())["tables"]), 1)

    def test_a_bad_csv_says_why(self):
        page = self.client.post("/logbook/upload", data={"name": "x"},
                                files={"file": ("x.csv", b"leg,leg\n1,2\n", "text/csv")}, follow_redirects=True)
        self.assertIn("same name", page.text)


if __name__ == "__main__":
    unittest.main()
