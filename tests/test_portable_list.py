"""Portable shopping items (core/portable_list.py) and Kitchen's side of them (apps/kitchen/shopping/portable.py)."""

import tempfile
import unittest
from pathlib import Path

from core import portable_list
from database import Database, reset_current_user_id, set_current_user_id
from apps.kitchen import shopping_list
from apps.kitchen.shopping import portable
from apps.kitchen.shopping.store import ShoppingStore

SOURCE = {"app": "thanksgiving", "label": "Thanksgiving"}
MILK = {"key": "item-milk", "text": "Whole milk", "amount": "1 quart (4 cups)", "aisle": "Dairy & eggs",
        "for": ["Mac and cheese"]}
PLATES = {"key": "extra-3", "text": "Paper plates"}


class FormatTests(unittest.TestCase):
    def test_items_are_cleaned_and_need_a_key_and_text(self):
        items = portable_list.normalize_items([MILK, {"key": "", "text": "x"}, {"key": "k", "text": "  "},
                                               dict(MILK, text="Again"), "nonsense", PLATES])
        self.assertEqual([i["key"] for i in items], ["item-milk", "extra-3"])  # one per key, the first wins
        self.assertEqual(items[1], {"key": "extra-3", "text": "Paper plates", "amount": "", "aisle": "", "for": []})

    def test_source_is_a_short_id(self):
        self.assertEqual(portable_list.normalize_source({"app": "Party Planner!", "label": ""}),
                         {"app": "partyplanner", "label": "Partyplanner"})


class KitchenSideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "t.db"))
        self.db.init()
        self.user = self.db.create_user("cook@example.com", "Cook", "x", "member")
        self.token = set_current_user_id(self.user)
        self.store = ShoppingStore(self.db)

    def tearDown(self):
        reset_current_user_id(self.token)
        if self.db.conn is not None:
            self.db.conn.close()
        self.tmp.cleanup()

    def made(self):
        return portable.create_list(self.db, "Next trip", [], [MILK, PLATES], SOURCE)

    def test_linked_items_join_recipe_lines_and_keep_their_own_keys(self):
        made = self.made()
        links = self.store.get_links(made["id"])
        sections = shopping_list.build_shopping_list([("Pancakes", "2 cups whole milk")], [], links)
        items = {i["key"]: i for s in sections for i in s["items"]}
        self.assertEqual(set(items), {"item-milk", "thanksgiving-extra-3"})  # not "extra-3": that's Kitchen's own
        self.assertEqual(items["item-milk"]["amount"], "2 cups + 1 quart (4 cups)")
        self.assertEqual(items["item-milk"]["dishes"], ["Pancakes", "Mac and cheese (Thanksgiving)"])
        self.assertTrue(items["thanksgiving-extra-3"]["linked_only"])

    def test_follow_check_carry_on_and_take_back(self):
        made = self.made()
        state = portable.linked_state(self.db, made["id"], "thanksgiving")
        self.assertEqual(set(state["items"]), {"item-milk", "extra-3"})
        self.assertTrue(portable.check(self.db, made["id"], "thanksgiving", "item-milk", True))
        self.assertIn("item-milk", self.store.get_checked(made["id"]))
        later = self.store.create_list("Later", [])
        self.store.move_link(made["id"], "thanksgiving", "extra-3", later)
        plates = portable.linked_state(self.db, made["id"], "thanksgiving")["items"]["extra-3"]
        self.assertEqual((plates["list_id"], plates["checked"]), (later, False))  # followed to the newer list
        portable.check(self.db, made["id"], "thanksgiving", "extra-3", True)
        self.assertIn("thanksgiving-extra-3", self.store.get_checked(later))
        self.assertTrue(portable.remove(self.db, made["id"], "thanksgiving", "extra-3"))
        self.assertNotIn("extra-3", portable.linked_state(self.db, made["id"], "thanksgiving")["items"])
        self.store.delete_list(made["id"])
        self.assertEqual(portable.linked_state(self.db, made["id"], "thanksgiving"), {"found": False, "items": {}})

    def test_someone_elses_list_is_not_found(self):
        made = self.made()
        set_current_user_id(self.db.create_user("other@example.com", "Other", "x", "member"))
        self.assertEqual(portable.linked_state(self.db, made["id"], "thanksgiving"), {"found": False, "items": {}})
        self.assertFalse(portable.check(self.db, made["id"], "thanksgiving", "item-milk", True))

    def test_items_need_a_source(self):
        with self.assertRaises(ValueError):
            portable.create_list(self.db, "Next trip", [], [MILK], {})


if __name__ == "__main__":
    unittest.main()
