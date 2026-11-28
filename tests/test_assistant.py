"""The Assistant: to-dos, projects and reading, through Dieter's tools and the new pages."""

import secrets
import unittest
import uuid
import warnings
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from apps.assistant import connector, reading
from core.connector.tools import ToolError
from database import reset_current_user_id, set_current_user_id

CHECKLIST = """Example output:

- [ ] Choo, Yangsze

  - ★ The Fox Wife
  - The Ghost Bride

- [ ] VanderMeer, Jeff

  - ★ Borne
  - Annihilation
"""


class ReadingParseTests(unittest.TestCase):
    def test_reads_a_written_bookstore_checklist(self):
        books = reading.parse_checklist(CHECKLIST)
        self.assertEqual([(b["author"], b["title"], b["high_priority"]) for b in books],
                         [("Choo, Yangsze", "The Fox Wife", True), ("Choo, Yangsze", "The Ghost Bride", False),
                          ("VanderMeer, Jeff", "Borne", True), ("VanderMeer, Jeff", "Annihilation", False)])
        self.assertEqual(reading.author_sort("Jeff VanderMeer"), "vandermeer, jeff")
        self.assertEqual(reading.author_sort("VanderMeer, Jeff"), "vandermeer, jeff")


class AssistantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        cls.api, cls.db = api, api.db
        cls.user_id = cls.db.create_user(f"assistant-{uuid.uuid4().hex[:8]}@example.com", "Assistant User", "x", "member")
        conn = cls.db.connect()
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (cls.user_id,))
        conn.commit()

    def setUp(self):
        self.token = set_current_user_id(self.user_id)
        self.client = TestClient(self.api.app)
        session = secrets.token_urlsafe(16)
        self.db.create_session(self.user_id, self.api.hash_session_token(session),
                               (datetime.utcnow() + timedelta(days=1)).isoformat())
        self.client.cookies.set(self.api.SESSION_COOKIE_NAME, session)

    def tearDown(self):
        reset_current_user_id(self.token)

    def test_todos_with_dates(self):
        friday = (date.today() + timedelta(days=3)).isoformat()
        todo = connector.add_todo({"title": "Call the mechanic", "date": friday})
        self.assertEqual(todo["date"], friday)
        self.assertIn(todo["id"], [t["id"] for t in connector.list_todos({})["open"]])
        with self.assertRaises(ToolError):
            connector.add_todo({"title": "x", "date": "Friday"})
        moved = connector.update_todo({"todo_id": todo["id"], "date": ""})
        self.assertEqual(moved["date"], "")
        self.assertTrue(connector.complete_todo({"todo_id": todo["id"]})["done"])
        self.assertFalse(connector.complete_todo({"todo_id": todo["id"], "done": False})["done"])
        page = self.client.get("/apps/assistant").text
        self.assertIn("Call the mechanic", page)
        self.assertIn('action="/scheduler/create"', page)

    def test_projects_through_tools_and_pages(self):
        self.name = f"Garden shed {uuid.uuid4().hex[:6]}"
        project_id = self.db.add_project(self.name, "Build a small shed.")
        self.assertIn(self.name, [p["name"] for p in connector.list_projects({})["projects"]])
        action = connector.add_action({"project_id": project_id, "action": "Buy lumber", "priority": "high"})
        connector.add_note({"project_id": project_id, "note": "8x6 footprint"})
        seen = connector.get_project({"project_id": project_id})
        self.assertEqual([a["action"] for a in seen["actions"]], ["Buy lumber"])
        self.assertEqual(seen["recent_notes"][0]["note"], "8x6 footprint")
        page = self.client.get(f"/projects/{project_id}").text
        self.assertIn("Garden shed", page)
        self.assertIn("8x6 footprint", page)
        self.assertIn(f'href="/projects/{project_id}/actions/{action["id"]}"', page)
        self.assertTrue(connector.complete_action({"action_id": action["id"]})["done"])
        self.assertTrue(connector.get_project({"project_id": project_id})["actions"][0]["done"])
        self.assertIn("Done (1)", self.client.get(f"/projects/{project_id}").text)
        self.assertEqual(self.client.get(f"/projects/{project_id}/actions/{action['id']}").status_code, 200)
        self.assertIn("Garden shed", self.client.get("/apps/assistant/projects").text)

    def test_projects_can_be_started_and_planned_through_tools(self):
        name = f"Residency prep {uuid.uuid4().hex[:6]}"
        made = connector.create_project({"name": name, "description": "Roadmap v1", "kind": "research"})
        with self.assertRaises(ToolError):
            connector.create_project({"name": name.upper()})  # one project per name
        connector.update_project({"project_id": made["id"], "description": "Roadmap v2"})
        action = connector.add_action({"project_id": made["id"], "action": "Let ChatGPT connect"})
        connector.add_action_step({"action_id": action["id"], "step": "Allow ChatGPT's sign-in address"})
        seen = connector.get_project({"project_id": made["id"]})
        self.assertEqual(seen["description"], "Roadmap v2")
        self.assertEqual(seen["actions"][0]["steps"][0]["step"], "Allow ChatGPT's sign-in address")
        with self.assertRaises(ToolError):
            connector.update_project({"project_id": made["id"]})
        with self.assertRaises(ToolError):
            connector.add_action_step({"action_id": 99999999, "step": "x"})

    def test_suggestions_wait_for_his_ok_and_skips_are_remembered(self):
        tag = uuid.uuid4().hex[:6]
        connector.add_book({"title": f"Shelf Book {tag}", "author": "Writer, Some"})
        made = connector.suggest_books({"asked_for": "something strange", "assistant": "claude", "books": [
            {"title": f"Keep Me {tag}", "author": "Ann Author", "reason": "Dreamlike, like the ones he rated highly."},
            {"title": f"Skip Me {tag}", "author": "Bo Writer", "reason": "Short."},
            {"title": f"Shelf Book {tag}", "author": "Writer, Some", "reason": "Already there."}]})
        self.assertEqual([b["title"] for b in made["added"]], [f"Keep Me {tag}", f"Skip Me {tag}"])
        self.assertIn("already on his shelf", made["skipped"][0]["why"])
        page = self.client.get("/apps/assistant/reading").text
        self.assertIn("Suggestions waiting for your OK", page)
        self.assertIn("Dreamlike, like the ones he rated highly.", page)
        ids = {s["title"]: s["id"] for s in reading.ReadingStore(self.db).suggestions()}
        self.client.post(f"/apps/assistant/reading/suggestions/{ids[f'Keep Me {tag}']}", data={"decision": "keep_star"})
        self.client.post(f"/apps/assistant/reading/suggestions/{ids[f'Skip Me {tag}']}", data={"decision": "skip"})
        books = {b["title"]: b for b in reading.ReadingStore(self.db).books()}
        self.assertEqual((books[f"Keep Me {tag}"]["status"], books[f"Keep Me {tag}"]["high_priority"]), ("want", 1))
        self.assertEqual(books[f"Skip Me {tag}"]["status"], "not_for_me")
        self.assertNotIn(f"Skip Me {tag}", self.client.get("/apps/assistant/reading").text)  # hidden from the shelf
        again = connector.suggest_books({"asked_for": "x", "assistant": "chatgpt",
                                         "books": [{"title": f"Skip Me {tag}", "author": "Bo Writer", "reason": "r"}]})
        self.assertEqual(again["added"], [])  # never suggested again
        self.assertIn(f"Skip Me {tag}", [b["title"] for b in connector.reading_taste({})["not_for_me"]])

    def test_ratings_and_thoughts_once_read(self):
        book = connector.add_book({"title": f"Rated {uuid.uuid4().hex[:6]}", "author": "Choo, Yangsze"})
        connector.update_book({"book_id": book["id"], "status": "read", "rating": 5, "thoughts": "Loved the foxes."})
        taste = connector.reading_taste({})
        self.assertIn({"title": book["title"], "author": "Choo, Yangsze", "rating": 5, "thoughts": "Loved the foxes."},
                      taste["rated"])
        self.assertIn("Choo, Yangsze", taste["loved_authors"])
        self.client.post(f"/apps/assistant/reading/{book['id']}/rate", data={"rating": "4", "thoughts": "Still good."})
        self.assertEqual(reading.ReadingStore(self.db).book(book["id"])["rating"], 4)
        with self.assertRaises(ToolError):
            connector.update_book({"book_id": book["id"], "rating": 9})

    def test_reading_and_the_bookstore(self):
        book = connector.add_book({"title": "Piranesi", "author": "Susanna Clarke", "high_priority": True})
        connector.add_book({"title": "Jonathan Strange", "author": "Clarke, Susanna"})
        listing = connector.reading_list({"bookstore": True})
        clarke = next(g for g in listing["bookstore_checklist"] if "Clarke" in g["author"])
        self.assertEqual(clarke["books"][0], "★ Piranesi")  # one shelf for both spellings of her name
        connector.check_shelf({"author": "clarke"})
        self.assertTrue(next(g for g in reading.ReadingStore(self.db).checklist() if g["key"] == clarke["shelf_key"])["checked"])
        found = connector.update_book({"title": "piranesi", "status": "found"})
        self.assertEqual(found["status"], "found")
        self.assertEqual(found["id"], book["id"])
        connector.check_shelf({"finish_visit": True})
        self.assertFalse(any(g["checked"] for g in reading.ReadingStore(self.db).checklist()))
        page = self.client.get("/apps/assistant/reading").text
        self.assertIn("★ </span>Piranesi", page)
        self.assertIn("Jonathan Strange", self.client.get("/apps/assistant/reading/bookstore").text)
        r = self.client.post(f"/apps/assistant/reading/{book['id']}/status", data={"status": "read"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(reading.ReadingStore(self.db).book(book["id"])["status"], "read")

    def test_old_addresses_go_to_the_new_pages(self):
        for old, new in (("/apps/assistant/planner", "/apps/assistant"), ("/apps/assistant/scheduler", "/apps/assistant"),
                         ("/projects", "/apps/assistant/projects")):
            r = self.client.get(old, follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (303, new))

    def test_dieter_chat_answers_on_assistant_pages_and_today(self):
        from unittest import mock
        with mock.patch.object(self.api.claude_agent, "available", return_value=True):
            for path, text in (("/apps/assistant", "add milk"), ("/projects/3", "note this"), ("/", "remind me to call mom")):
                self.assertTrue(self.api.dieter_chat_handles(self.api.DieterActionMessage(content=text, page_url=path)), path)
        names = {s["name"] for s in self.api.ASSISTANT_TOOLS.list()}
        self.assertTrue({"assistant_add_todo", "assistant_reading_list", "assistant_check_shelf"} <= names)


if __name__ == "__main__":
    unittest.main()
