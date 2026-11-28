"""Dieter's chat on the main site (core/connector/chat.py) and Kitchen recipe changes through it
(apps/kitchen/recipe_changes.py): propose, review card, save, history, restore."""

import os
import secrets
import tempfile
import unittest
import uuid
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from fastapi.testclient import TestClient

from core.connector import chat
from core.connector.pages import Page
from core.connector.tools import Tool, ToolError, ToolSet
from database import Database, set_current_user_id


def script(*turns):
    """A fake Claude: each turn is a list of (tool name, input) calls, or a string reply."""
    turns = list(turns)
    seen = []

    def model_call(system, messages, tools, effort):
        seen.append({"system": system, "messages": list(messages), "tools": [t["name"] for t in tools]})
        turn = turns.pop(0)
        if isinstance(turn, str):
            return NS(stop_reason="end_turn", content=[NS(type="text", text=turn)])
        return NS(stop_reason="tool_use", content=[NS(type="tool_use", id=f"t{i}", name=name, input=args)
                                                   for i, (name, args) in enumerate(turn)])
    model_call.seen = seen
    return model_call


PAGES = [Page("demo.home", "Demo", "Home.", "/demo"),
         Page("demo.thing", "Thing", "One thing.", "/demo/things/{thing_id}", {"thing_id": "integer"})]


def demo_setup(saved):
    def propose(args):
        return {"id": "p1", "kind": "demo", "status": "pending", "title": "Change thing", "diffs": [],
                "save_url": "/demo/p1/save", "open_url": "/demo/things/3"}
    toolset = ToolSet("demo", [Tool("demo_propose", "Propose", "Propose.", propose),
                               Tool("demo_read", "Read", "Read.", lambda a: {"ok": True}, read_only=True)])
    return chat.ChatSetup(toolsets=[toolset], page_lists=[PAGES], notes=["Demo notes."],
                          savers={"demo": lambda pid: saved.append(pid) or {"title": "Change thing"}},
                          pending=lambda: [{"id": "p1", "kind": "demo", "title": "Change thing"}])


class ChatEngineTests(unittest.TestCase):
    def test_pages_know_where_you_are(self):
        self.assertEqual(PAGES[1].match("/demo/things/12"), {"thing_id": "12"})
        self.assertIsNone(PAGES[1].match("/demo/things"))
        self.assertEqual(PAGES[0].match("/demo/"), {})
        setup = demo_setup([])
        self.assertIn("demo.thing (Thing: thing_id=12)", setup.where("/demo/things/12?x=1#a", "T"))
        self.assertIn("/elsewhere", setup.where("/elsewhere", "T"))

    def test_open_a_page_and_propose_a_change(self):
        claude = script([("dieter_open_page", {"page": "demo.thing", "params": {"thing_id": 3}})],
                        [("demo_propose", {})], "Here it is, with a suggested change.")
        out = chat.answer(demo_setup([]), "pull up thing 3 and fix it", page_url="/demo", model_call=claude)
        self.assertEqual(out["reply"], "Here it is, with a suggested change.")
        self.assertEqual([a["type"] for a in out["do"]], ["navigate", "proposal"])
        self.assertEqual(out["do"][0]["url"], "/demo/things/3")
        first = claude.seen[0]
        self.assertIn("Demo notes.", first["system"][0]["text"])
        self.assertIn("demo.home (Demo), titled ''", first["messages"][-1]["content"])
        self.assertIn("Waiting for their OK: Change thing (proposal_id p1)", first["messages"][-1]["content"])
        self.assertIn("save_change", first["tools"])

    def test_a_proposal_opens_its_page_unless_you_are_on_it(self):
        out = chat.answer(demo_setup([]), "fix it", page_url="/demo", model_call=script([("demo_propose", {})], "ok"))
        self.assertEqual([a["type"] for a in out["do"]], ["proposal", "navigate"])
        out = chat.answer(demo_setup([]), "fix it", page_url="/demo/things/3",
                          model_call=script([("demo_propose", {})], "ok"))
        self.assertEqual([a["type"] for a in out["do"]], ["proposal"])

    def test_saving_needs_plain_words(self):
        saved = []
        claude = script([("save_change", {"proposal_id": "p1"})], "Tap Save or say save it.")
        chat.answer(demo_setup(saved), "yes", model_call=claude)
        self.assertEqual(saved, [])
        self.assertIn("Not saved", str(claude.seen[1]["messages"][-1]["content"]))
        out = chat.answer(demo_setup(saved), "yes, save it", model_call=script(
            [("save_change", {"proposal_id": "p1"})], "Saved."))
        self.assertEqual(saved, ["p1"])
        self.assertEqual(out["do"], [{"type": "decided", "id": "p1", "status": "saved"}, {"type": "reload"}])

    def test_what_it_cant_do_is_logged(self):
        logged = []
        setup = demo_setup([])
        setup.log_for_later = lambda summary, said, page: logged.append((summary, said, page))
        claude = script([("log_for_later", {"summary": "Wants a meal plan emailed"})], "Logged it for later.")
        out = chat.answer(setup, "email me my meal plan", page_url="/demo", model_call=claude)
        self.assertEqual(logged, [("Wants a meal plan emailed", "email me my meal plan", "/demo")])
        self.assertIn("log_for_later", claude.seen[0]["tools"])
        self.assertIn("Things you can't do", claude.seen[0]["system"][0]["text"])
        self.assertEqual(out["reply"], "Logged it for later.")
        plain = script("Hi.")
        chat.answer(demo_setup([]), "hi", model_call=plain)
        self.assertNotIn("log_for_later", plain.seen[0]["tools"])  # only where an app handles it

    def test_tools_can_be_held_back(self):
        setup = demo_setup([])
        setup.offer = lambda schema: schema["annotations"]["readOnlyHint"]
        claude = script([("demo_propose", {})], "Can't.")
        out = chat.answer(setup, "fix it", model_call=claude)
        self.assertNotIn("demo_propose", claude.seen[0]["tools"])
        self.assertIn("demo_read", claude.seen[0]["tools"])
        self.assertEqual(out["do"], [])
        self.assertIn("no tool called demo_propose", str(claude.seen[1]["messages"][-1]["content"]))

    def test_history_starts_with_the_person_and_has_no_dangling_message(self):
        claude = script("Hi.")
        chat.answer(demo_setup([]), "hello", history=[{"role": "assistant", "content": "earlier"},
                                                      {"role": "user", "content": "a"},
                                                      {"role": "assistant", "content": "b"},
                                                      {"role": "user", "content": "failed one"}], model_call=claude)
        roles = [m["role"] for m in claude.seen[0]["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user"])


class KitchenRecipeChangeTests(unittest.TestCase):
    def setUp(self):
        from apps.kitchen import recipe_changes
        self.rc = recipe_changes
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "t.db"))
        self.db.init()
        self.token = set_current_user_id(1)
        conn = self.db.connect()
        cur = conn.execute("""INSERT INTO recipe_complete_meals (source_group_id, source_kind, title, ingredients_text,
                              instructions_text, status, user_id) VALUES (-1, 'saved', 'Chili', ?, ?, 'ready', 1)""",
                           ("1 lb beef\n1 onion\n2 tbsp chili powder", "Brown the beef.\nSimmer 30 min."))
        conn.commit()
        self.meal_id = cur.lastrowid

    def tearDown(self):
        from database import reset_current_user_id
        reset_current_user_id(self.token)
        self.db.conn.close()
        self.tmp.cleanup()

    def test_propose_save_history_restore(self):
        card = self.rc.propose(self.db, self.meal_id, {"ingredients_text": "1 lb beef\n1 onion\n3 tbsp chili powder"},
                               "More chili powder", request="make it spicier")
        self.assertEqual(card["status"], "pending")
        self.assertEqual(card["save_url"], f"/apps/recipes/api/proposals/{card['id']}/save")
        self.assertIn({"op": "added", "text": "3 tbsp chili powder"}, card["diffs"][0]["lines"])
        self.assertEqual(self.rc.current(self.db, self.meal_id)["ingredients_text"].count("2 tbsp"), 1)  # not yet
        self.assertEqual([c["id"] for c in self.rc.pending_cards(self.db, self.meal_id)], [card["id"]])

        self.assertEqual(self.rc.save(self.db, card["id"])["status"], "saved")
        self.assertIn("3 tbsp", self.rc.current(self.db, self.meal_id)["ingredients_text"])
        self.assertEqual(dict(self.db.get_recipe_complete_meal(self.meal_id))["ingredients_text"].count("2 tbsp"), 1)
        with self.assertRaises(ToolError):
            self.rc.save(self.db, card["id"])  # once
        history = self.rc.history(self.db, self.meal_id)
        self.assertEqual(history[0]["summary"], "More chili powder")
        self.assertEqual(history[0]["request"], "make it spicier")

        self.rc.restore(self.db, self.meal_id, history[0]["id"])
        self.assertIn("2 tbsp", self.rc.current(self.db, self.meal_id)["ingredients_text"])
        self.assertTrue(self.rc.history(self.db, self.meal_id)[0]["summary"].startswith("Restored"))

    def test_stale_or_empty_changes_are_refused(self):
        with self.assertRaises(ToolError):
            self.rc.propose(self.db, self.meal_id, {"instructions_text": "Brown the beef.\nSimmer 30 min."})
        with self.assertRaises(ToolError):
            self.rc.propose(self.db, self.meal_id, {"ingredients_text": "  "})
        card = self.rc.propose(self.db, self.meal_id, {"title": "Weeknight chili"})
        self.db.update_recipe_complete_meal_edits(self.meal_id, title="Someone else's title")
        with self.assertRaises(ToolError):
            self.rc.save(self.db, card["id"])
        self.assertEqual(self.rc.current(self.db, self.meal_id)["title"], "Someone else's title")
        other = self.rc.propose(self.db, self.meal_id, {"title": "Chili again"})
        self.assertEqual(self.rc.discard(self.db, other["id"])["status"], "discarded")
        self.assertEqual(self.rc.pending_cards(self.db), [])


class MainSiteChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        cls.api, cls.db = api, api.db
        cls.user_id = cls.db.create_user(f"chat-{uuid.uuid4().hex[:8]}@example.com", "Chat Cook", "x", "member")
        conn = cls.db.connect()
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (cls.user_id,))
        cur = conn.execute("""INSERT INTO recipe_complete_meals (source_group_id, source_kind, title, ingredients_text,
                              instructions_text, status, user_id) VALUES (?, 'saved', 'Test tacos', ?, ?, 'ready', ?)""",
                           (-secrets.randbelow(10 ** 9) - 1000, "1 lb chicken\n8 tortillas",
                            "Cook the chicken.\nWarm the tortillas.", cls.user_id))
        conn.commit()
        cls.meal_id = cur.lastrowid

    def setUp(self):
        self.client = TestClient(self.api.app)
        token = secrets.token_urlsafe(16)
        self.db.create_session(self.user_id, self.api.hash_session_token(token),
                               (datetime.utcnow() + timedelta(days=1)).isoformat())
        self.client.cookies.set(self.api.SESSION_COOKIE_NAME, token)

    def ask(self, text, page_url, claude):
        with mock.patch.object(self.api.claude_agent, "available", return_value=True), \
                mock.patch.object(self.api.claude_agent, "create", claude):
            return self.client.post("/api/dieter/action", json={"content": text, "page_url": page_url,
                                                                "page_title": "Kitchen"}).json()

    def test_kitchen_recipe_change_through_the_chat(self):
        page = f"/apps/recipes/meals/{self.meal_id}"
        claude = script([("kitchen_get_recipe", {"recipe_id": self.meal_id})],
                        [("kitchen_propose_recipe_change", {"recipe_id": self.meal_id, "reason": "Corn tortillas",
                                                            "ingredients": "1 lb chicken\n8 corn tortillas"})],
                        "I suggested corn tortillas. Look it over and tap Save.")
        data = self.ask("use corn tortillas", page, claude)
        self.assertEqual(data["model"], "claude")
        self.assertEqual([a["type"] for a in data["do"]], ["proposal"])  # already on the recipe's page
        self.assertIn(f"kitchen.recipe (Recipe: recipe_id={self.meal_id})", claude.seen[0]["messages"][-1]["content"])
        self.assertNotIn("thanksgiving_propose_recipe_change", claude.seen[0]["tools"])
        card = data["do"][0]["card"]

        html = self.client.get(page).text
        self.assertIn("data-proposal-card=", html)
        self.assertIn("dieter-actions.js", html)
        saved = self.client.post(card["save_url"])
        self.assertEqual(saved.json()["card"]["status"], "saved")
        self.assertEqual(self.client.post(card["save_url"]).status_code, 409)
        html = self.client.get(page).text
        self.assertIn("8 corn tortillas", html)
        self.assertIn("Recipe history", html)
        change_id = self.api.kitchen_recipe_changes.history(self.db, self.meal_id)[0]["id"]
        back = self.client.post(f"{page}/history/{change_id}/restore", follow_redirects=False)
        self.assertEqual(back.status_code, 303)
        self.assertIn('<span class="name">8 tortillas</span>', self.client.get(page).text)

    def test_other_pages_keep_the_older_handlers(self):
        claude = script("unused")
        with mock.patch.object(self.api, "message_requests_planner_action", return_value=True):
            msg = self.api.DieterActionMessage(content="remind me to call mom", page_url="/")
            self.assertFalse(self.api.dieter_chat_handles(msg))
        with mock.patch.object(self.api.claude_agent, "available", return_value=True):
            self.assertFalse(self.api.dieter_chat_handles(self.api.DieterActionMessage(content="hi",
                                                                                       page_url="/apps/music")))
            self.assertTrue(self.api.dieter_chat_handles(self.api.DieterActionMessage(
                content="add milk to my list", page_url="/apps/recipes/grocery-lists/3")))
        self.assertEqual(claude.seen, [])

    def test_trainer_questions_go_to_the_chat_and_notes_to_the_reflection(self):
        claude = script([("trainer_run", {"date": "2026-11-26"})], "17:10 for 3 miles: your fastest pace this fall.")
        data = self.ask("how did my race go today? legs felt good", "/apps/trainer/run/21133954975", claude)
        self.assertEqual(data["model"], "claude")
        self.assertIn("trainer_speed", claude.seen[0]["tools"])
        self.assertIn("trainer.run (Run: run_id=21133954975)", claude.seen[0]["messages"][-1]["content"])
        self.assertIn("speed points", claude.seen[0]["system"][0]["text"] if isinstance(claude.seen[0]["system"], list)
                      else claude.seen[0]["system"])
        self.assertTrue(self.api.is_trainer_question("What's my threshold pace"))
        self.assertFalse(self.api.is_trainer_question("legs felt heavy today, rpe 7"))
        notes = script("unused")
        with mock.patch.object(self.api, "handle_trainer_reflection_request",
                               return_value={"assistant_message": "Saved.", "changed_fields": []}) as reflect:
            self.ask("legs felt heavy today, rpe 7", "/apps/trainer", notes)
        reflect.assert_called_once()
        self.assertEqual(notes.seen, [])

    def test_analyze_my_run_on_a_run_page_goes_to_the_chat(self):
        # Nov 28, 2026: "analyze my run" on a run's page got the shoe prompt instead of an analysis.
        claude = script([("trainer_run", {"run_id": "21156284815"})], "Five 230s at 4:25 with full rest: good work.")
        data = self.ask("Analyze my run", "/apps/trainer/run/21156284815", claude)
        self.assertEqual(data["model"], "claude")
        self.assertTrue(self.api.is_trainer_question("break down today's reps"))
        # After a run is posted Dieter asks for shoes (?shoe_workout_id=): an answer about shoes is still a shoe log.
        self.assertFalse(self.api.message_requests_trainer_shoe_log("Analyze my run", "/apps/trainer?shoe_workout_id=1"))
        self.assertTrue(self.api.message_requests_trainer_shoe_log("trainers 7 miles", "/apps/trainer?shoe_workout_id=1"))
        # Every exchange is saved (core/conversations.py), with which part of Dieter answered it.
        from core import conversations
        saved = conversations.recent(self.api.db, contains="Analyze my run", limit=1)[0]
        self.assertEqual((saved["answered_by"], saved["page"]), ("claude", "/apps/trainer/run/21156284815"))
        self.assertIn("good work", saved["reply"])

    def test_thanksgiving_chat_does_not_get_the_review_card_tools(self):
        # Signed calls need the services' shared key; CI has none set, so the test brings its own.
        with mock.patch.dict(os.environ, {"DIETER_INTERNAL_KEY": "test-internal-key"}):
            names = [t["name"] for t in self.client.get("/internal/connector/kitchen/tools",
                                                        headers=self.internal_headers()).json()["tools"]]
        self.assertIn("kitchen_get_recipe", names)
        self.assertNotIn("kitchen_propose_recipe_change", names)

    def internal_headers(self):
        from core import internal
        return {internal.HEADER: internal.sign({"id": self.user_id})}


class PrimingTests(unittest.TestCase):
    """The warm-up must send exactly what a real question starts with, or Anthropic's cached copy doesn't match."""

    def test_site_chat_primes_the_same_tools_and_instructions(self):
        from core.connector import agent
        setup = demo_setup([])
        primed = []
        with mock.patch.object(agent, "create", lambda **k: primed.append(k) or NS(usage=None, stop_reason="end_turn")):
            chat.prime(setup)
        claude = script("Hi.")
        chat.answer(setup, "hello", model_call=claude)
        self.assertEqual(primed[0]["system"], claude.seen[0]["system"])
        self.assertEqual([t["name"] for t in primed[0]["tools"]], claude.seen[0]["tools"])
        self.assertEqual(primed[0]["max_tokens"], 8)
        self.assertEqual(primed[0]["system"][0]["cache_control"], {"type": "ephemeral", "ttl": "1h"})

    def test_thanksgiving_primes_both_ways_of_talking(self):
        from apps.kitchen.thanksgiving import brain
        from core.connector import agent
        primed = []
        with mock.patch.object(agent, "prime", lambda system, tools: primed.append((system, tools))):
            brain.prime(None)
        self.assertEqual(len(primed), 2)
        spoken, typed = (t for _, t in primed)
        self.assertEqual(primed[0][0], agent.cached_system(brain.PERSONA))
        self.assertEqual(spoken, brain._tools(brain.connector.tools(), None, True))
        self.assertEqual(typed, brain._tools(brain.connector.tools(), None, False))
        self.assertIn("stay_quiet", [t["name"] for t in spoken])
        self.assertNotIn("stay_quiet", [t["name"] for t in typed])


if __name__ == "__main__":
    unittest.main()
