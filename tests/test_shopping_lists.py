import json
import tempfile
import threading
import unittest
import warnings
from pathlib import Path

from fastapi import FastAPI

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from apps.kitchen import shopping_list
from apps.kitchen.shopping import router as shopping
from cloud_persistence import CloudStoragePersistence
from database import Database, reset_current_user_id, set_current_user_id

MEALS = {
    1: {"id": 1, "title": "Mac and cheese", "text": "1 lb pasta\n5 tbsp butter\n4 cups whole milk", "url": "/m/1",
        "status": "pending"},
    2: {"id": 2, "title": "Pumpkin pie", "text": "1 stick butter\n2 large eggs\n1/2 tsp salt", "url": "",
        "status": "pending"},
    3: {"id": 3, "title": "Tacos", "text": "1 lb ground beef\n1 onion", "url": "", "status": "pending"},
}


class ShoppingListBuilderTests(unittest.TestCase):
    def test_totals_aisles_and_parts(self):
        sections = shopping_list.build_shopping_list([("A", "4 tbsp butter\n1 onion"), ("B", "1 stick butter")])
        items = {i["text"]: (s["name"], i) for s in sections for i in s["items"]}
        aisle, butter = items["Butter"]
        self.assertEqual(aisle, "Dairy & eggs")
        self.assertEqual(butter["amount"], "1 1/2 sticks")
        self.assertEqual([p["amount"] for p in butter["parts"]], ["4 tbsp", "1 stick"])

    def test_aisles(self):
        cases = {
            "1 tsp black pepper": "Check the pantry",
            "1 red bell pepper": "Produce",
            "8 oz egg noodles": "Pantry & baking",
            "2 cups chicken broth": "Pantry & baking",
            "1 can cream of mushroom soup": "Pantry & baking",
            "1 bag herb-seasoned stuffing cubes": "Bakery & bread",
            "1 bag frozen peas": "Frozen",
            "2 tbsp soy sauce": "Sauces & condiments",
            "1 cup honeycrisp apples": "Produce",
            "1 tsp cinnamon": "Check the pantry",
            "1 lb ground beef": "Meat & seafood",
            "2 tbsp peanut butter": "Pantry & baking",
        }
        for line, aisle in cases.items():
            sections = shopping_list.build_shopping_list([("X", line)])
            self.assertEqual(sections[0]["name"], aisle, line)

    def test_names_use_recipe_words_when_they_agree(self):
        sections = shopping_list.build_shopping_list([("A", "4 lb Yukon Gold potatoes"), ("B", "15 oz can pumpkin puree")])
        names = {i["text"] for s in sections for i in s["items"]}
        self.assertEqual(names, {"Yukon Gold potatoes", "Pumpkin puree"})
        sections = shopping_list.build_shopping_list([("A", "1 cup chicken broth"), ("B", "1 cup beef broth")])
        self.assertEqual(sections[0]["items"][0]["text"], "Broth")

    def test_item_key_is_stable(self):
        self.assertEqual(shopping_list.item_key("Stick Butter"), "item-butter")
        self.assertEqual(shopping_list.item_key("2 large onions"), "item-onion")


class ShoppingRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "t.db"))
        self.db.init()
        self.user_id = self.db.create_user("cook@example.com", "Curtis Cook", "x", "member")
        self.other_id = self.db.create_user("other@example.com", "Other", "x", "member")
        self.as_user = self.user_id
        self.pending = [1, 2, 3]
        shopping.configure(
            self.db,
            meal_sources=lambda ids: [MEALS[i] for i in ids if i in MEALS],
            pending_meals=lambda: [{"id": i, "title": MEALS[i]["title"]} for i in self.pending],
            component_sources=lambda ids: [(MEALS[i]["title"], MEALS[i]["text"]) for i in ids if i in MEALS],
        )
        app = FastAPI()

        @app.middleware("http")
        async def as_user(request, call_next):
            request.state.current_user = {"name": "Curtis Cook", "email": "cook@example.com"}
            token = set_current_user_id(self.as_user)
            try:
                return await call_next(request)
            finally:
                reset_current_user_id(token)

        app.include_router(shopping.router)
        self.client = TestClient(app)

    def tearDown(self):
        if self.db.conn is not None:
            self.db.conn.close()
        self.tmp.cleanup()

    def make_list(self, meal_ids=(1, 2)):
        data = {"picked": "1", "title": "Groceries", "meal_id": [str(i) for i in meal_ids]}
        r = self.client.post("/apps/recipes/grocery-lists/create", data=data, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        return int(r.headers["location"].rsplit("/", 1)[1])

    def test_new_page_lists_meals_checked(self):
        page = self.client.get("/apps/recipes/grocery-lists/new").text
        self.assertEqual(page.count('name="meal_id"'), 3)
        self.assertIn("checked", page)

    def test_list_shows_totals_and_only_picked_meals(self):
        list_id = self.make_list((1, 2))
        page = self.client.get(f"/apps/recipes/grocery-lists/{list_id}").text
        self.assertIn("Butter", page)
        self.assertIn("Mac and cheese 5 tbsp · Pumpkin pie 1 stick", page)
        self.assertNotIn("ground beef", page.lower())
        self.assertIn("Tacos", page)  # offered under "add a meal"

    def test_old_button_takes_every_planned_meal(self):
        r = self.client.post("/apps/recipes/grocery-lists/create", follow_redirects=False)
        list_id = int(r.headers["location"].rsplit("/", 1)[1])
        self.assertEqual(shopping.store().get_list(list_id)["meal_ids"], [1, 2, 3])

    def test_check_off_json_and_state(self):
        list_id = self.make_list()
        r = self.client.post(f"/apps/recipes/grocery-lists/{list_id}/check", json={"key": "item-butter", "done": True})
        self.assertEqual(r.json(), {"ok": True})
        state = self.client.get(f"/apps/recipes/grocery-lists/{list_id}/state").json()
        self.assertEqual(state["checked"], ["item-butter"])
        total_before = state["left"]
        self.client.post(f"/apps/recipes/grocery-lists/{list_id}/check", json={"key": "item-butter", "done": False})
        self.assertEqual(self.client.get(f"/apps/recipes/grocery-lists/{list_id}/state").json()["left"], total_before + 1)

    def test_check_off_without_javascript(self):
        list_id = self.make_list()
        r = self.client.post(f"/apps/recipes/grocery-lists/{list_id}/check", data={"key": "item-eggs", "done": "1"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("item-eggs", shopping.store().get_checked(list_id))

    def test_pantry_staples_do_not_count_toward_left(self):
        list_id = self.make_list((2,))
        state = self.client.get(f"/apps/recipes/grocery-lists/{list_id}/state").json()
        self.assertEqual(state["left"], 2)  # butter and eggs; salt is under "Check the pantry"

    def test_extras_and_signature(self):
        list_id = self.make_list()
        before = self.client.get(f"/apps/recipes/grocery-lists/{list_id}/state").json()["signature"]
        self.client.post(f"/apps/recipes/grocery-lists/{list_id}/extras/add", data={"text": "paper towels\nfoil"})
        after = self.client.get(f"/apps/recipes/grocery-lists/{list_id}/state").json()["signature"]
        self.assertNotEqual(before, after)
        extras = shopping.store().get_extras(list_id)
        self.assertEqual([e["text"] for e in extras], ["paper towels", "foil"])
        self.client.post(f"/apps/recipes/grocery-lists/{list_id}/extras/{extras[0]['id']}/delete")
        self.assertEqual([e["text"] for e in shopping.store().get_extras(list_id)], ["foil"])

    def test_list_follows_recipe_edits(self):
        list_id = self.make_list((1,))
        MEALS[1]["text"] += "\n12 oz sharp cheddar cheese"
        try:
            self.assertIn("Sharp cheddar cheese", self.client.get(f"/apps/recipes/grocery-lists/{list_id}").text)
        finally:
            MEALS[1]["text"] = MEALS[1]["text"].replace("\n12 oz sharp cheddar cheese", "")

    def test_add_and_remove_meals(self):
        list_id = self.make_list((1,))
        self.client.post(f"/apps/recipes/grocery-lists/{list_id}/meals/add", data={"pick": "meal:3"})
        self.assertEqual(shopping.store().get_list(list_id)["meal_ids"], [1, 3])
        self.client.post(f"/apps/recipes/grocery-lists/{list_id}/meals/1/remove")
        self.assertEqual(shopping.store().get_list(list_id)["meal_ids"], [3])

    def test_done_reopen_rename_delete(self):
        list_id = self.make_list()
        self.client.post(f"/apps/recipes/grocery-lists/{list_id}/done")
        self.assertEqual(shopping.store().get_list(list_id)["status"], "done")
        self.assertIn("Done", self.client.get("/apps/recipes/grocery-lists").text)
        self.client.post(f"/apps/recipes/grocery-lists/{list_id}/reopen")
        self.assertEqual(shopping.store().get_list(list_id)["status"], "active")
        self.client.post(f"/apps/recipes/grocery-lists/{list_id}/rename", data={"title": "Sunday shop"})
        self.assertEqual(shopping.store().get_list(list_id)["title"], "Sunday shop")
        self.client.post(f"/apps/recipes/grocery-lists/{list_id}/delete")
        self.assertIsNone(shopping.store().get_list(list_id))

    def test_other_users_cannot_see_or_delete(self):
        list_id = self.make_list()
        self.as_user = self.other_id
        self.assertEqual(self.client.get(f"/apps/recipes/grocery-lists/{list_id}").status_code, 404)
        self.assertEqual(self.client.post(f"/apps/recipes/grocery-lists/{list_id}/delete").status_code, 404)
        self.as_user = self.user_id
        self.assertIsNotNone(shopping.store().get_list(list_id))

    def test_old_snapshot_list_converts_once(self):
        items = [
            {"name": "Stick Butter", "quantities": ["1 stick"], "status": "gotten"},
            {"name": "Paper Towels", "quantities": [], "status": "gotten", "source_kind": "manual"},
            {"name": "Saffron", "quantities": ["1 pinch"], "status": "need"},  # recipe since deleted
        ]
        set_current_user_id(self.user_id)
        conn = self.db.connect()
        shopping.store()._conn()
        cur = conn.execute(
            "INSERT INTO recipe_grocery_lists (title, meal_plan_item_ids_json, items_json, user_id) VALUES (?, ?, ?, ?)",
            ("Old list", json.dumps([2]), json.dumps(items), self.user_id))
        conn.commit()
        list_id = cur.lastrowid
        page = self.client.get(f"/apps/recipes/grocery-lists/{list_id}").text
        self.assertIn("Paper Towels", page)
        self.assertIn("1 pinch Saffron", page)
        checked = shopping.store().get_checked(list_id)
        self.assertIn("item-butter", checked)
        self.assertEqual(len(checked), 2)  # butter + paper towels
        self.client.get(f"/apps/recipes/grocery-lists/{list_id}")
        self.assertEqual(len(shopping.store().get_extras(list_id)), 2)  # not converted twice

    def test_preview_matches_list_totals(self):
        data = self.client.post("/apps/recipes/grocery-lists/preview", json={"component_ids": [1, 2]}).json()
        butter = next(i for s in data["sections"] for i in s["items"] if i["text"] == "Butter")
        self.assertEqual(butter["amount"], "1 1/2 sticks")  # 5 tbsp + 8 tbsp, rounded to a half stick

    def test_feedback(self):
        r = self.client.post("/apps/recipes/grocery-lists/feedback",
                             data={"text": "Butter shows twice", "page": "/apps/recipes/grocery-lists/1"},
                             follow_redirects=False)
        self.assertEqual(r.headers["location"], "/apps/recipes/grocery-lists/1?reported=1")

    def thanksgiving_dishes(self):
        from apps.kitchen.thanksgiving import router as tg_router
        from apps.kitchen.thanksgiving.presets import preset_dish
        tg_router.configure(self.db)
        token = set_current_user_id(self.user_id)
        try:
            plan_id = tg_router.store().create_plan("2026-11-26T16:00", "Curtis, Parrisa")
            mac = tg_router.store().add_dish(plan_id, preset_dish("mac"))
            pie = tg_router.store().add_dish(plan_id, preset_dish("pie"))
        finally:
            reset_current_user_id(token)
        return mac, pie

    def test_tonights_list_from_thanksgiving_dishes(self):
        mac, pie = self.thanksgiving_dishes()
        page = self.client.get("/apps/recipes/grocery-lists/new").text
        self.assertIn("From your Thanksgiving plan", page)
        self.assertIn(f'value="{pie}:Crust"', page)
        r = self.client.post("/apps/recipes/grocery-lists/create", follow_redirects=False, data={
            "picked": "1", "title": "Tonight", "dish": [f"{mac}:", f"{pie}:Crust"]})
        list_id = int(r.headers["location"].rsplit("/", 1)[1])
        page = self.client.get(f"/apps/recipes/grocery-lists/{list_id}").text
        self.assertIn("Pumpkin pie (crust)", page)
        self.assertIn("Little Smokies", page)
        self.assertNotIn("Pumpkin puree", page)  # filling is not on tonight's list
        self.assertNotIn("Evaporated milk", page)
        # Mac and cheese starts as written (full batch): 7 tbsp butter + 1 stick for the crust.
        self.assertIn("Mac and cheese 7 tbsp · Pumpkin pie (crust) 1 stick", page)
        self.assertIn("2 sticks", page)  # 15 tbsp, rounded to a half stick

    def test_whole_dish_wins_over_its_part_and_dishes_can_be_added_later(self):
        mac, pie = self.thanksgiving_dishes()
        r = self.client.post("/apps/recipes/grocery-lists/create", follow_redirects=False, data={
            "picked": "1", "dish": [f"{pie}:", f"{pie}:Crust", "999:"]})  # 999 is not your dish
        list_id = int(r.headers["location"].rsplit("/", 1)[1])
        self.assertEqual(shopping.store().get_list(list_id)["dish_parts"], [{"dish_id": pie, "part": ""}])
        self.client.post(f"/apps/recipes/grocery-lists/{list_id}/meals/add", data={"pick": f"dish:{mac}:"})
        self.assertEqual(len(shopping.store().get_list(list_id)["dish_parts"]), 2)
        self.client.post(f"/apps/recipes/grocery-lists/{list_id}/dishes/remove", data={"key": f"{pie}:"})
        self.assertEqual(shopping.store().get_list(list_id)["dish_parts"], [{"dish_id": mac, "part": ""}])


class BatchedBackupTests(unittest.TestCase):
    def test_first_change_uploads_now_and_a_burst_becomes_one_more(self):
        persistence = CloudStoragePersistence("bucket", "x.db", ".")
        uploads = []
        trailing = threading.Event()

        def fake_backup():
            uploads.append(1)
            if len(uploads) == 2:
                trailing.set()
            return True

        persistence.backup_database = fake_backup
        persistence.schedule_backup(delay=0.05)
        self.assertEqual(len(uploads), 1)  # saved before the request returns
        for _ in range(10):
            persistence.schedule_backup(delay=0.05)
        self.assertTrue(trailing.wait(2))
        self.assertEqual(len(uploads), 2)

    def test_next_request_runs_a_backup_the_paused_timer_missed(self):
        persistence = CloudStoragePersistence("bucket", "x.db", ".")
        uploads = []
        persistence.backup_database = lambda: uploads.append(1)
        persistence.schedule_backup(delay=60)  # uploads now
        persistence.schedule_backup(delay=60)  # trailing change: timer won't fire for a minute
        self.assertFalse(persistence.run_if_due(delay=60))  # not due yet
        self.assertTrue(persistence.run_if_due(delay=0))  # e.g. the CPU was paused past the deadline
        self.assertEqual(len(uploads), 2)
        self.assertFalse(persistence.run_if_due(delay=0))  # nothing left pending
        self.assertIsNone(persistence._timer)

    def test_save_pending_uploads_a_burst_before_the_request_ends(self):
        persistence = CloudStoragePersistence("bucket", "x.db", ".")
        uploads = []
        persistence.backup_database = lambda: uploads.append(1)
        self.assertFalse(persistence.save_pending())  # nothing changed
        persistence.schedule_backup(delay=60)  # first change: uploads now
        persistence.schedule_backup(delay=60)  # the rest of the burst waits...
        persistence.schedule_backup(delay=60)
        self.assertTrue(persistence.save_pending())  # ...until the request finishes, then uploads once
        self.assertEqual(uploads, [1, 1])
        self.assertIsNone(persistence._timer)

        def broken():
            raise RuntimeError("storage down")
        persistence.backup_database = broken
        persistence.schedule_backup(delay=60)
        persistence.schedule_backup(delay=60)
        persistence.save_pending()  # a storage error is logged, never raised into the request
        self.assertEqual(uploads, [1, 1])

    def test_flush_runs_a_pending_backup_now(self):
        persistence = CloudStoragePersistence("bucket", "x.db", ".")
        uploads = []
        persistence.backup_database = lambda: uploads.append(1)
        persistence.schedule_backup(delay=60)
        persistence.schedule_backup(delay=60)
        self.assertTrue(persistence.flush())
        self.assertEqual(uploads, [1, 1])
        self.assertFalse(persistence.flush())

    def test_disabled_without_bucket(self):
        persistence = CloudStoragePersistence("", "x.db", ".")
        persistence.schedule_backup()
        self.assertIsNone(persistence._timer)


if __name__ == "__main__":
    unittest.main()
