import tempfile
import unittest
import warnings
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from apps.kitchen.thanksgiving import planner, router as tg_router
from apps.kitchen.thanksgiving.presets import OUR_MENU, preset_dish
from database import Database, reset_current_user_id, set_current_user_id

DINNER = "2026-11-26T16:00"


def menu_dishes():
    dishes = []
    for i, key in enumerate(OUR_MENU, start=1):
        dish = preset_dish(key)
        dish["id"] = i
        dishes.append(dish)
    return dishes


def find(tasks, dish_name_part, step_id):
    return next(t for t in tasks if dish_name_part in t["dish_name"] and t["step_id"] == step_id)


class AnchorTests(unittest.TestCase):
    def setUp(self):
        self.dinner = planner.parse_dt(DINNER)

    def test_resolve(self):
        self.assertEqual(planner.resolve_anchor("T-240", self.dinner), datetime(2026, 11, 26, 12, 0))
        self.assertEqual(planner.resolve_anchor("T+15", self.dinner), datetime(2026, 11, 26, 16, 15))
        self.assertEqual(planner.resolve_anchor("D-2 18:00", self.dinner), datetime(2026, 11, 24, 18, 0))

    def test_dinner_day_steps_are_relative_to_dinner(self):
        self.assertEqual(planner.anchor_for(0, "12:00", self.dinner), "T-240")
        self.assertEqual(planner.anchor_for(0, "16:30", self.dinner), "T+30")
        self.assertEqual(planner.anchor_for(1, "10:00", self.dinner), "D-1 10:00")


class MenuTimelineTests(unittest.TestCase):
    def setUp(self):
        self.tasks = planner.build_timeline({"dinner_at": DINNER}, menu_dishes())

    def test_turkey_goes_in_at_noon_after_race(self):
        roast = find(self.tasks, "turkey", "roast")
        self.assertEqual(roast["start"], datetime(2026, 11, 26, 12, 0))
        self.assertEqual(roast["end"], datetime(2026, 11, 26, 15, 15))
        self.assertEqual(find(self.tasks, "turkey", "carve")["start"], datetime(2026, 11, 26, 15, 45))

    def test_13lb_turkey_thaws_from_saturday(self):
        self.assertEqual(find(self.tasks, "turkey", "thaw")["start"], datetime(2026, 11, 21, 8, 0))

    def test_sourdough_and_pie_schedule(self):
        self.assertEqual(find(self.tasks, "Sourdough", "feed1")["start"], datetime(2026, 11, 23, 21, 0))
        self.assertEqual(find(self.tasks, "Sourdough", "mix")["start"], datetime(2026, 11, 24, 18, 0))
        self.assertEqual(find(self.tasks, "Sourdough", "bake1")["start"], datetime(2026, 11, 25, 10, 0))
        bread_done = find(self.tasks, "Sourdough", "bake2")["end"]
        self.assertGreaterEqual(find(self.tasks, "pie", "blind1")["start"], bread_done)

    def test_our_menu_has_no_oven_clashes(self):
        self.assertEqual(planner.find_conflicts(self.tasks, ovens=1), [])

    def test_moving_dinner_moves_only_dinner_day_steps(self):
        later = planner.build_timeline({"dinner_at": "2026-11-26T17:00"}, menu_dishes())
        self.assertEqual(find(later, "turkey", "roast")["start"], datetime(2026, 11, 26, 13, 0))
        self.assertEqual(find(later, "Sourdough", "mix")["start"], datetime(2026, 11, 24, 18, 0))


class ConflictTests(unittest.TestCase):
    def dish(self, dish_id, steps, cook=""):
        return {"id": dish_id, "name": f"Dish {dish_id}", "cook": cook, "steps": steps}

    def test_oven_temperature_clash(self):
        dishes = [
            self.dish(1, [{"id": "a", "anchor": "T-60", "title": "Bake", "minutes": 60, "kind": "oven", "temp": 325}]),
            self.dish(2, [{"id": "b", "anchor": "T-30", "title": "Roast", "minutes": 30, "kind": "oven", "temp": 425}]),
        ]
        warnings_ = planner.find_conflicts(planner.build_timeline({"dinner_at": DINNER}, dishes))
        self.assertEqual(len(warnings_), 1)
        self.assertIn("different temperatures", warnings_[0]["text"])
        self.assertEqual(planner.find_conflicts(planner.build_timeline({"dinner_at": DINNER}, dishes), ovens=2), [])

    def test_same_cook_double_booked(self):
        dishes = [
            self.dish(1, [{"id": "a", "anchor": "T-60", "title": "Peel", "minutes": 20, "kind": "hands"}], "Parrisa"),
            self.dish(2, [{"id": "b", "anchor": "T-50", "title": "Chop", "minutes": 20, "kind": "hands"}], "Parrisa"),
            self.dish(3, [{"id": "c", "anchor": "T-50", "title": "Stir", "minutes": 20, "kind": "hands"}], "Sam"),
        ]
        warnings_ = planner.find_conflicts(planner.build_timeline({"dinner_at": DINNER}, dishes))
        self.assertEqual(len(warnings_), 1)
        self.assertIn("Parrisa", warnings_[0]["text"])


class HelperTests(unittest.TestCase):
    def test_status_now(self):
        tasks = planner.build_timeline({"dinner_at": DINNER}, menu_dishes())
        status = planner.status_now(tasks, set(), datetime(2026, 11, 26, 12, 10))
        self.assertIn("roast", [t["step_id"] for t in status["current"]])
        self.assertTrue(status["overdue"])  # nothing was checked off
        self.assertEqual(status["next"][0]["start"], datetime(2026, 11, 26, 13, 30))

    def test_grocery_list_merges_and_skips_homemade(self):
        dishes = [
            {"id": 1, "name": "A", "ingredients_text": "1 onion\nturkey pan drippings\n90 g active starter"},
            {"id": 2, "name": "B", "ingredients_text": "2 large onions\n2 eggs\n350 g water"},
        ]
        sections = planner.build_grocery_list(dishes, [{"id": 7, "text": "paper towels"}])
        items = {i["text"]: i for s in sections for i in s["items"]}
        self.assertEqual(items["Onions"]["dishes"], ["A", "B"])
        self.assertEqual(items["Onions"]["amount"], "3")
        self.assertEqual([p["amount"] for p in items["Onions"]["parts"]], ["1", "2"])
        self.assertEqual(set(items), {"Onions", "Eggs", "paper towels"})

    def test_instruction_timers(self):
        lines = planner.instruction_lines("Bake 25 to 30 minutes.\nRest 1 hour\n\nStir.")
        self.assertEqual([l["timers"] for l in lines], [[30], [60], []])


class RecipeSectionTests(unittest.TestCase):
    def test_pie_has_crust_section_and_step_mapping(self):
        pie = preset_dish("pie")
        ing = planner.recipe_sections(pie["ingredients_text"])
        ins = planner.recipe_sections(pie["instructions_text"])
        self.assertEqual([s["heading"] for s in ing], ["Crust", "Filling", "To serve"])
        crust_steps = planner.sections_for_step(ins, "Crust")
        self.assertEqual(len(crust_steps), 1)
        self.assertIn("coin-size", " ".join(crust_steps[0]["lines"]))
        self.assertEqual([s["heading"] for s in planner.sections_for_step(ins, "Filling")], ["Filling and bake"])
        self.assertEqual(planner.sections_for_step(ins, ""), [])

    def test_crust_is_made_tonight_in_two_steps(self):
        tasks = planner.build_timeline({"dinner_at": DINNER}, [dict(preset_dish("pie"), id=1)])
        self.assertEqual(find(tasks, "pie", "dough")["start"], datetime(2026, 11, 19, 19, 0))
        self.assertEqual(find(tasks, "pie", "shell")["start"], datetime(2026, 11, 19, 20, 15))
        self.assertEqual(find(tasks, "pie", "shell")["section"], "Crust")

    def test_headings_stay_off_the_shopping_list(self):
        sections = planner.build_grocery_list([dict(preset_dish("pie"), id=1)])
        texts = [i["text"] for s in sections for i in s["items"]]
        self.assertNotIn("Crust:", texts)
        self.assertIn("Unsalted butter", texts)

    def test_mac_and_cheese_shopping_and_oven(self):
        sections = planner.build_grocery_list([dict(preset_dish("mac"), id=1)])
        where = {i["text"]: s["name"] for s in sections for i in s["items"]}
        self.assertEqual(where["Little Smokies"], "Meat & seafood")
        self.assertEqual(where["Garlic powder"], "Check the pantry")
        self.assertEqual(where["Elbow macaroni or cavatappi"], "Pantry & baking")
        self.assertEqual(where["Sharp cheddar cheese"], "Dairy & eggs")
        self.assertEqual(where["Panko breadcrumbs"], "Pantry & baking")
        tasks = planner.build_timeline({"dinner_at": DINNER}, menu_dishes())
        bake = find(tasks, "Mac", "bake")
        self.assertEqual(bake["start"], datetime(2026, 11, 26, 15, 15))  # after the turkey comes out
        self.assertEqual(find(tasks, "Mac", "practice")["start"], datetime(2026, 11, 19, 17, 30))

    def test_whole_menu_totals(self):
        sections = planner.build_grocery_list(menu_dishes())
        items = {i["text"]: i for s in sections for i in s["items"]}
        self.assertEqual(items["Butter"]["amount"], "5 1/2 sticks (about 1 1/3 lb)")
        self.assertEqual(items["Yukon Gold potatoes"]["amount"], "4 lb")
        self.assertEqual(items["Onions"]["amount"], "4")  # 2 + 1/2 + 1, rounded up
        self.assertEqual(items["Celery"]["amount"], "7 ribs")
        self.assertEqual(items["Eggs"]["amount"], "3")
        self.assertEqual(items["Broth"]["amount"], "1 quart (4 cups) + some")
        self.assertEqual(items["Whole milk"]["amount"], "1 quart (4 cups)")
        self.assertEqual(items["Lemons"]["amount"], "1")
        self.assertNotIn("Water", items)
        self.assertNotIn("Ice water", items)
        pantry = next(s for s in sections if s["name"] == "Check the pantry")
        self.assertIn("Flour", [i["text"] for i in pantry["items"]])
        self.assertEqual(sections[-1]["name"], "Check the pantry")

    def test_unedited_old_pie_is_upgraded_keeping_assignments(self):
        from apps.kitchen.thanksgiving import presets
        old_steps = [{"id": "shell", "anchor": "D-7 20:00", "title": "Make the pie shell", "minutes": 30,
                      "kind": "hands", "cook": "Curtis"},
                     {"id": "bake1", "anchor": "D-1 12:00", "title": "Bake", "minutes": 15, "kind": "oven",
                      "temp": 425, "cook": "Parrisa"}]
        old = {"name": "Pumpkin pie", "instructions_text": presets.OLD_PIE_INSTRUCTIONS, "steps": old_steps}
        update = presets.upgrade_saved_dish(old)
        self.assertIn("Crust:", update["instructions_text"])
        steps = {s["id"]: s for s in update["steps"]}
        self.assertEqual(steps["dough"]["cook"], "Curtis")
        self.assertEqual(steps["bake1"]["anchor"], "D-1 12:00")  # user's timing kept
        self.assertEqual(steps["bake1"]["cook"], "Parrisa")
        edited = dict(old, instructions_text="My own pie notes")
        self.assertIsNone(presets.upgrade_saved_dish(edited))


class SizeTests(unittest.TestCase):
    def test_scaling_lines(self):
        from apps.kitchen.thanksgiving.sizes import scale_ingredient
        self.assertEqual(scale_ingredient("1 stick (113 g) very cold unsalted butter", 0.5),
                         "1/2 stick (56 g) very cold unsalted butter")
        self.assertEqual(scale_ingredient("1 stick (113 g) butter", 2), "2 sticks (226 g) butter")
        self.assertEqual(scale_ingredient("4 to 6 tbsp ice water", 0.5), "2 to 3 tbsp ice water")
        self.assertEqual(scale_ingredient("1 lb elbow macaroni", 0.75), "12 oz elbow macaroni")
        self.assertEqual(scale_ingredient("1/3 cup flour", 0.5), "2 3/4 tbsp flour")
        self.assertEqual(scale_ingredient("1 package (12 to 14 oz) Little Smokies", 0.75),
                         "1 package (12 to 14 oz) Little Smokies")  # you buy whole packages
        self.assertEqual(scale_ingredient("1/2 tsp smoked paprika", 0.75), "1/4 tsp smoked paprika")
        self.assertEqual(scale_ingredient("kosher salt", 0.5), "kosher salt")
        self.assertEqual(scale_ingredient("Crust:", 0.5), "Crust:")

    def test_instructions_scale_amounts_not_times_or_pans(self):
        from apps.kitchen.thanksgiving.sizes import scale_instructions
        text = "Melt 5 tbsp butter. Bake at 400°F for 20 to 25 minutes in a 9x13 dish."
        self.assertEqual(scale_instructions(text, 0.5, "8x8"),
                         "Melt 2 1/2 tbsp butter. Bake at 400°F for 20 to 25 minutes in a 8x8 dish.")

    def test_turkey_and_house_prep_have_no_sizes(self):
        from apps.kitchen.thanksgiving import sizes
        self.assertEqual(sizes.with_size(preset_dish("turkey"))["size_options"], [])
        self.assertEqual(sizes.with_size(preset_dish("house"))["size_options"], [])
        self.assertEqual(len(sizes.with_size(preset_dish("pie"))["size_options"]), 4)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "t.db"))
        self.db.init()
        self.user_id = self.db.create_user("cook@example.com", "Curtis Cook", "x", "member")
        self.token = set_current_user_id(self.user_id)
        tg_router.configure(self.db)
        app = FastAPI()

        @app.middleware("http")
        async def as_user(request, call_next):
            request.state.current_user = {"name": "Curtis Cook", "email": "cook@example.com"}
            token = set_current_user_id(self.user_id)
            try:
                return await call_next(request)
            finally:
                reset_current_user_id(token)

        app.include_router(tg_router.router)
        self.client = TestClient(app)

    def tearDown(self):
        reset_current_user_id(self.token)
        if self.db.conn is not None:
            self.db.conn.close()
        self.tmp.cleanup()

    def setup_plan(self):
        r = self.client.post("/thanksgiving/setup", data={
            "dinner_date": "2026-11-26", "dinner_time": "16:00", "cooks": "Curtis, Parrisa", "ovens": "1",
            "starter_menu": "1"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        return tg_router.store().get_plan()

    def test_first_visit_creates_plan_with_menu(self):
        r = self.client.get("/thanksgiving")
        self.assertEqual(r.status_code, 200)
        plan = tg_router.store().get_plan()
        self.assertEqual(plan["cooks"], "Curtis, Parrisa")
        self.assertEqual(plan["dinner_at"], "2026-11-26T16:00")
        dishes = tg_router.store().get_dishes(plan["id"])
        self.assertEqual(len(dishes), len(OUR_MENU))
        turkey = next(d for d in dishes if d["category"] == "turkey")
        self.assertEqual(next(s for s in turkey["steps"] if s["id"] == "out")["cook"], "Parrisa")
        self.client.get("/thanksgiving")
        self.assertEqual(len(tg_router.store().get_dishes(plan["id"])), len(OUR_MENU))  # not duplicated
        self.assertIn("Roast the turkey", r.text)
        self.assertIn("Shopping list", r.text)
        self.assertIn("Yukon Gold", r.text)

    def test_steps_expand_to_their_recipe_section(self):
        self.client.get("/thanksgiving")
        page = self.client.get("/thanksgiving").text
        self.assertIn("Roll, crimp, and freeze the pie shell", page)
        self.assertIn('class="t-open"', page)
        self.assertIn("coin-size pieces", page)
        state = self.client.get("/thanksgiving/api/state").json()
        pie = next(d for d in state["dishes"] if d["name"] == "Pumpkin pie")
        self.assertEqual([s["heading"] for s in pie["instruction_sections"]][0], "Crust")
        self.assertEqual(next(s for s in pie["steps"] if s["key"].endswith(":dough"))["section"], "Crust")

    def test_saved_old_pie_upgrades_on_startup(self):
        from apps.kitchen.thanksgiving import presets
        self.client.get("/thanksgiving")
        plan = tg_router.store().get_plan()
        pie = next(d for d in tg_router.store().get_dishes(plan["id"]) if d["name"] == "Pumpkin pie")
        pie["instructions_text"] = presets.OLD_PIE_INSTRUCTIONS
        tg_router.store().update_dish(plan["id"], pie["id"], pie)
        tg_router.configure(self.db)  # simulates a server restart
        upgraded = tg_router.store().get_dish(plan["id"], pie["id"])
        self.assertIn("Crust:", upgraded["instructions_text"])

    def mac(self):
        plan = tg_router.store().get_plan()
        return plan, next(d for d in tg_router.store().get_dishes(plan["id"]) if d["name"] == "Mac and cheese")

    def test_mac_starts_on_thanksgiving_size_and_dropdown_changes_it(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        self.assertEqual(mac["scale"], 0.75)
        page = self.client.get("/thanksgiving").text
        self.assertIn("Batch size", page)
        self.assertIn('value="0.75" selected', page)
        self.assertIn("12 oz elbow macaroni", page)
        self.assertIn("11x7 or 9x9", page)
        self.assertIn("1 lb elbow macaroni", page)  # the editor keeps the recipe as written
        groceries = {i["text"]: i["amount"] for s in tg_router.plan_context(plan)["groceries"] for i in s["items"]}
        self.assertEqual(groceries["Sharp cheddar cheese"], "9 oz")
        r = self.client.post(f"/thanksgiving/dishes/{mac['id']}/size", data={"scale": "0.5"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], f"/thanksgiving#size-{mac['id']}")
        self.assertEqual(self.mac()[1]["scale"], 0.5)
        self.assertEqual(self.client.post(f"/thanksgiving/dishes/{mac['id']}/size", data={"scale": "3"}).status_code, 400)
        state = self.client.get("/thanksgiving/api/state").json()
        cook_mac = next(d for d in state["dishes"] if d["name"] == "Mac and cheese")
        self.assertEqual(cook_mac["size_label"], "Half: serves 3")
        self.assertIn("8 oz elbow macaroni or cavatappi", [l for s in cook_mac["ingredient_sections"] for l in s["lines"]])

    def test_size_picker_shows_wherever_the_recipe_opens(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        page = self.client.get("/thanksgiving").text
        # Dish card, View recipe, and each timeline step's recipe all carry the picker.
        self.assertGreaterEqual(page.count('aria-label="Batch size for Mac and cheese"'), 3)
        self.assertIn("View recipe · Thanksgiving: 3 + leftovers", page)
        state = self.client.get("/thanksgiving/api/state").json()
        cook_mac = next(d for d in state["dishes"] if d["name"] == "Mac and cheese")
        self.assertEqual(cook_mac["size_value"], "0.75")
        self.assertEqual([o["value"] for o in cook_mac["size_options"]], ["0.5", "0.75", "1"])
        r = self.client.post(f"/thanksgiving/api/dishes/{mac['id']}/size", json={"scale": "1"})
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(self.mac()[1]["scale"], 1.0)
        self.assertEqual(self.client.post(f"/thanksgiving/api/dishes/{mac['id']}/size", json={"scale": "9"}).status_code, 400)

    def test_size_migration_runs_once_and_keeps_choices(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        conn = self.db.connect()
        conn.execute("UPDATE tg_dishes SET scale = 1 WHERE id = ?", (mac["id"],))
        conn.execute("DELETE FROM tg_checks WHERE kind = 'migration' AND item_key = 'default-sizes'")
        conn.commit()
        tg_router.configure(self.db)  # restart: an existing plan gets the Thanksgiving size once
        self.assertEqual(self.mac()[1]["scale"], 0.75)
        tg_router.store().set_scale(plan["id"], mac["id"], 1.0)  # you pick Full
        tg_router.configure(self.db)
        self.assertEqual(self.mac()[1]["scale"], 1.0)

    def test_saving_an_edited_recipe_keeps_it_unscaled(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        form = {"name": mac["name"], "category": mac["category"], "cook": "",
                "ingredients_text": mac["ingredients_text"] + "\n1 cup frozen peas",
                "instructions_text": mac["instructions_text"], "notes": mac["notes"]}
        self.client.post(f"/thanksgiving/dishes/{mac['id']}/save", data=form)
        saved = self.mac()[1]
        self.assertIn("1 lb elbow macaroni", saved["ingredients_text"])
        self.assertIn("1 cup frozen peas", saved["ingredients_text"])
        self.assertIn("3/4 cup frozen peas", self.client.get("/thanksgiving").text)

    def test_mac_and_cheese_joins_existing_plan_once(self):
        self.client.get("/thanksgiving")
        plan = tg_router.store().get_plan()
        mac = next(d for d in tg_router.store().get_dishes(plan["id"]) if d["name"] == "Mac and cheese")
        tg_router.store().delete_dish(plan["id"], mac["id"])  # a plan made before mac existed
        tg_router.configure(self.db)
        names = [d["name"] for d in tg_router.store().get_dishes(plan["id"])]
        self.assertEqual(names.count("Mac and cheese"), 1)
        mac = next(d for d in tg_router.store().get_dishes(plan["id"]) if d["name"] == "Mac and cheese")
        tg_router.store().delete_dish(plan["id"], mac["id"])  # user removes it: it stays gone
        tg_router.configure(self.db)
        names = [d["name"] for d in tg_router.store().get_dishes(plan["id"])]
        self.assertNotIn("Mac and cheese", names)

    def test_editing_a_dish_keeps_step_sections(self):
        self.client.get("/thanksgiving")
        plan = tg_router.store().get_plan()
        pie = next(d for d in tg_router.store().get_dishes(plan["id"]) if d["name"] == "Pumpkin pie")
        data = {"name": pie["name"], "cook": "", "category": "dessert", "ingredients_text": pie["ingredients_text"],
                "instructions_text": pie["instructions_text"], "notes": "",
                "step_id": [], "step_day": [], "step_time": [], "step_title": [], "step_minutes": [],
                "step_kind": [], "step_temp": [], "step_cook": [], "step_detail": []}
        dinner = planner.parse_dt(plan["dinner_at"])
        for s in pie["steps"]:
            when = planner.resolve_anchor(s["anchor"], dinner)
            data["step_id"].append(s["id"]); data["step_day"].append(str(planner.day_offset_of(when, dinner)))
            data["step_time"].append(when.strftime("%H:%M")); data["step_title"].append(s["title"])
            data["step_minutes"].append(str(s["minutes"])); data["step_kind"].append(s["kind"])
            data["step_temp"].append(str(s.get("temp") or "")); data["step_cook"].append("")
            data["step_detail"].append(s.get("detail", ""))
        self.client.post(f"/thanksgiving/dishes/{pie['id']}/save", data=data)
        saved = tg_router.store().get_dish(plan["id"], pie["id"])
        self.assertEqual(next(s for s in saved["steps"] if s["id"] == "dough")["section"], "Crust")
        self.assertEqual([s["anchor"] for s in saved["steps"]], [s["anchor"] for s in pie["steps"]])

    def test_text_is_escaped(self):
        self.setup_plan()
        self.client.post("/thanksgiving/dishes/add", data={"name": "<script>alert(1)</script>"})
        r = self.client.get("/thanksgiving")
        self.assertNotIn("<script>alert(1)</script>", r.text)
        self.assertIn("&lt;script&gt;", r.text)

    def test_cook_state_and_actions(self):
        plan = self.setup_plan()
        state = self.client.get("/thanksgiving/api/state").json()
        self.assertEqual(state["cooks"], ["Curtis", "Parrisa"])
        turkey = next(d for d in state["dishes"] if d["category"] == "turkey")
        key = next(s["key"] for s in turkey["steps"] if s["key"].endswith(":roast"))

        self.client.post("/thanksgiving/api/steps/done", json={"key": key, "done": True})
        self.assertIn(key, tg_router.store().get_checked(plan["id"], "step"))
        self.client.post("/thanksgiving/api/alerts", json={"key": key, "state": "snooze", "minutes": 5})
        self.assertEqual(self.client.get("/thanksgiving/api/state").json()["alerts"][key]["state"], "snooze")

        timer = self.client.post("/thanksgiving/api/timers", json={"dish_id": turkey["id"], "label": "Baste", "minutes": 10}).json()
        timers = self.client.get("/thanksgiving/api/state").json()["timers"]
        self.assertEqual([t["label"] for t in timers], ["Baste"])
        self.client.post(f"/thanksgiving/api/timers/{timer['id']}/dismiss")
        self.assertEqual(self.client.get("/thanksgiving/api/state").json()["timers"], [])

        page = self.client.get("/thanksgiving/cook")
        self.assertIn("Start cooking", page.text)

    def test_save_dish_edits_steps_and_assigns_cook(self):
        plan = self.setup_plan()
        dish = next(d for d in tg_router.store().get_dishes(plan["id"]) if d["name"] == "Gravy")
        r = self.client.post(f"/thanksgiving/dishes/{dish['id']}/save", data={
            "name": "Gravy", "cook": "Parrisa", "category": "side",
            "step_id": ["make"], "step_day": ["0"], "step_time": ["15:30"], "step_title": ["Make the gravy"],
            "step_minutes": ["20"], "step_kind": ["stove"], "step_temp": [""], "step_cook": [""], "step_detail": [""],
            "new_step_title": "Warm the gravy boat", "new_step_day": "0", "new_step_time": "15:50",
            "new_step_minutes": "5", "new_step_kind": "hands",
            "ingredients_text": dish["ingredients_text"], "instructions_text": "", "notes": "",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        saved = tg_router.store().get_dish(plan["id"], dish["id"])
        self.assertEqual(saved["cook"], "Parrisa")
        self.assertEqual([s["anchor"] for s in saved["steps"]], ["T-30", "T-10"])
        self.assertEqual(saved["steps"][0]["minutes"], 20)

    def test_turkey_retime(self):
        plan = self.setup_plan()
        turkey = next(d for d in tg_router.store().get_dishes(plan["id"]) if d["category"] == "turkey")
        self.client.post(f"/thanksgiving/dishes/{turkey['id']}/turkey", data={"weight_lb": "18"})
        steps = {s["id"]: s for s in tg_router.store().get_dish(plan["id"], turkey["id"])["steps"]}
        self.assertNotIn("thaw", steps)  # not frozen
        self.assertEqual(steps["roast"]["minutes"], 240)

    def test_feedback_goes_to_studio_queue(self):
        self.setup_plan()
        self.client.post("/thanksgiving/api/feedback", json={"text": "Timer did not chime", "page": "/thanksgiving/cook"})
        reports = [dict(r) for r in self.db.get_app_feedback_reports("open")]
        self.assertTrue(any(r["title"].startswith("[Thanksgiving] Timer did not chime") for r in reports))
        self.assertEqual(reports[0]["area"], "Kitchen / Recipes")

    def test_plans_are_per_user(self):
        self.setup_plan()
        other = self.db.create_user("other@example.com", "Other", "x", "member")
        token = set_current_user_id(other)
        try:
            self.assertIsNone(tg_router.store().get_plan())
        finally:
            reset_current_user_id(token)


if __name__ == "__main__":
    unittest.main()
