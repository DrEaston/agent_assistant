import json
import re
import tempfile
import time
import unittest
import warnings
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from apps.kitchen.thanksgiving import planner, presets, router as tg_router, voice
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
        # Right after the olive loaf comes out at 8:40: a short reheat, then the plain loaf at 9.
        self.assertEqual(find(self.tasks, "Sourdough", "bake1")["start"], datetime(2026, 11, 25, 9, 0))
        self.assertEqual(find(self.tasks, "Sourdough", "preheat")["start"],
                         find(self.tasks, "Olive and rosemary sourdough", "bake2")["end"])
        bread_done = find(self.tasks, "Sourdough", "bake2")["end"]
        self.assertGreaterEqual(find(self.tasks, "pie", "blind1")["start"], bread_done)

    def test_two_loaves_one_fold_step_with_timers(self):
        plain = [t for t in self.tasks if t["dish_name"] == "Sourdough"]
        self.assertFalse([t for t in plain if t["step_id"].startswith("fold") and t["step_id"] != "folds"])
        folds = find(self.tasks, "Sourdough", "folds")
        self.assertEqual([m for m, _ in folds["series"]], [15, 30, 45, 60, 90, 120])
        self.assertEqual(folds["series_button"], "Start fold timers")
        olive = preset_dish("olive_sourdough")
        self.assertEqual(olive["category"], "bread")
        self.assertIn("365 g water", olive["ingredients_text"])
        self.assertIn("rosemary", olive["ingredients_text"])
        self.assertIn("olives", olive["ingredients_text"])
        # The olive loaf is out of the oven before the plain loaf's preheat
        self.assertLessEqual(find(self.tasks, "Olive", "bake2")["end"], find(self.tasks, "Sourdough", "preheat")["start"])
        self.assertEqual(find(self.tasks, "Olive", "folds")["series"], folds["series"])

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

    def test_no_warnings_name_cooks(self):
        # Cooks aren't shown any more (each device is one cook), so overlapping hands-on work isn't flagged.
        dishes = [
            self.dish(1, [{"id": "a", "anchor": "T-60", "title": "Peel", "minutes": 20, "kind": "hands"}], "Parrisa"),
            self.dish(2, [{"id": "b", "anchor": "T-50", "title": "Chop", "minutes": 20, "kind": "hands"}], "Parrisa"),
        ]
        self.assertEqual(planner.find_conflicts(planner.build_timeline({"dinner_at": DINNER}, dishes)), [])


class HelperTests(unittest.TestCase):
    def test_status_now(self):
        tasks = planner.build_timeline({"dinner_at": DINNER}, menu_dishes())
        status = planner.status_now(tasks, set(), datetime(2026, 11, 26, 12, 10))
        self.assertIn("roast", [t["step_id"] for t in status["current"]])
        self.assertTrue(status["overdue"])  # nothing was checked off
        self.assertEqual(status["next"][0]["start"], datetime(2026, 11, 26, 13, 0))  # whip the cream

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
        self.assertEqual([s["heading"] for s in ing], ["Crust", "Filling", "Whipped cream"])
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
        self.assertEqual(items["Unsalted butter"]["amount"], "5 1/2 sticks (about 1 1/3 lb)")
        self.assertEqual(items["Yukon Gold potatoes"]["amount"], "2 lb")
        self.assertEqual(items["Russet potatoes"]["amount"], "2 lb")
        self.assertEqual(items["Onions"]["amount"], "4")  # 2 + 1/2 + 1, rounded up
        self.assertEqual(items["Celery"]["amount"], "7 ribs")
        self.assertEqual(items["Eggs"]["amount"], "4")  # 3 for the pie and stuffing, 1 for the pigs' egg wash
        self.assertEqual(items["Puff pastry"]["amount"], "1 sheet")
        self.assertEqual(items["Little Smokies"]["amount"], "2 packages")  # mac and cheese + pigs in a blanket
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
        pages = self.all_pages()
        self.assertIn("Roast the turkey", pages)
        self.assertIn("Shopping list", pages)
        self.assertIn("Yukon Gold", pages)

    def test_ingredient_checklist_is_shared_and_starts_over(self):
        self.client.get("/thanksgiving")
        state = self.client.get("/thanksgiving/api/state").json()
        pie = next(d for d in state["dishes"] if d["name"] == "Pumpkin pie")
        crust = pie["checklist"][0]
        self.assertEqual(crust["heading"], "Crust")
        flour = crust["lines"][0]
        self.assertIn("flour", flour["text"])
        self.assertFalse(flour["added"])
        dough = next(s for s in pie["steps"] if s["key"].endswith(":dough"))
        self.assertEqual(dough["uses"], ["Crust"])
        self.assertEqual(self.client.post("/thanksgiving/api/ingredients/added", json={"key": "nope"}).status_code, 400)
        self.client.post("/thanksgiving/api/ingredients/added", json={"key": flour["key"], "added": True})
        # Another device asks for the state: the flour is added there too.
        again = next(d for d in self.client.get("/thanksgiving/api/state").json()["dishes"] if d["id"] == pie["id"])
        self.assertTrue(again["checklist"][0]["lines"][0]["added"])
        # A batch-size change rescales the text but keeps what's been added.
        self.client.post(f"/thanksgiving/api/dishes/{pie['id']}/size", json={"scale": "1"})
        resized = next(d for d in self.client.get("/thanksgiving/api/state").json()["dishes"] if d["id"] == pie["id"])
        self.assertTrue(resized["checklist"][0]["lines"][0]["added"])
        self.client.post("/thanksgiving/api/ingredients/reset", json={"dish_id": pie["id"]})
        cleared = next(d for d in self.client.get("/thanksgiving/api/state").json()["dishes"] if d["id"] == pie["id"])
        self.assertFalse(any(l["added"] for g in cleared["checklist"] for l in g["lines"]))

    def test_steps_and_recipe_link_both_ways(self):
        self.client.get("/thanksgiving")
        state = self.client.get("/thanksgiving/api/state").json()
        pie = next(d for d in state["dishes"] if d["name"] == "Pumpkin pie")
        shell = next(s for s in pie["steps"] if s["key"].endswith(":shell"))
        self.assertEqual(shell["how_ref"], "Crust 6–8")
        self.assertEqual(shell["how"][0]["index"], 5)
        self.assertEqual(pie["method"][0]["heading"], "Crust")
        self.assertEqual(pie["method"][0]["lines"][5]["n"], 6)
        # Timeline: the step shows its own lines and where they are in the recipe.
        page = self.client.get("/thanksgiving/timeline").text
        self.assertIn("Roll, crimp, and freeze the pie shell", page)
        self.assertIn('class="t-how"', page)
        self.assertIn(f'href="/thanksgiving/menu/{pie["id"]}#line-{pie["id"]}-5">Crust 6–8 in the recipe →</a>', page)
        # Dish page: each method line names the step that covers it, linking back to that step.
        dish = self.client.get(f"/thanksgiving/menu/{pie['id']}").text
        self.assertIn(f'id="line-{pie["id"]}-5"', dish)
        self.assertIn(f'href="/thanksgiving/menu/{pie["id"]}#task-{pie["id"]}-shell"', dish)
        self.assertIn(f'id="task-{pie["id"]}-shell"', dish)
        self.assertIn(f'href="/thanksgiving/cook?dish={pie["id"]}"', dish)
        self.assertNotIn("not scheduled", dish)  # every pie line is in a step

    def test_each_tab_is_its_own_page(self):
        self.client.get("/thanksgiving")
        for url, heading in [("/thanksgiving", "Up next"), ("/thanksgiving/timeline", "<h2>Timeline</h2>"),
                             ("/thanksgiving/menu", "The Menu</h2>"), ("/thanksgiving/recipes", "<h2>Recipes</h2>"),
                             ("/thanksgiving/shopping", "Shopping list"), ("/thanksgiving/settings", "Dinner date")]:
            page = self.client.get(url).text
            self.assertIn(heading, page, url)
            self.assertIn(f'<a href="{url}" aria-current="page">', page, url)
            self.assertIn('id="voice-live"', page, url)  # Tell Dieter on every page
        timeline = self.client.get("/thanksgiving/timeline").text
        self.assertNotIn("The Menu</h2>", timeline)
        self.assertNotIn("Dinner date", timeline)

    def test_menu_card_is_a_multicourse_menu_without_names_or_house_prep(self):
        self.client.get("/thanksgiving")
        page = self.client.get("/thanksgiving/menu").text
        card = page.split('<article class="feast"')[1].split("</article>")[0]
        self.assertNotIn("<img", card)
        self.assertIn("Dinner at 4<", card)
        self.assertIn("Guests welcome at 3", card)
        courses = re.findall(r'class="course-name">([^<]+)<', card)
        self.assertEqual(courses, ["Before dinner", "The main course", "Alongside", "Bread", "To finish"])
        bread = card.split('course-name">Bread')[1].split('course-name">To finish')[0]
        self.assertIn("A naturally leavened loaf from our own starter", bread)  # the plain loaf
        self.assertIn("Olive and rosemary sourdough", bread)
        self.assertIn("Our sourdough with olives and fresh rosemary", bread)
        before = card.split('course-name">Before dinner')[1].split('course-name">The main course')[0]
        for name in ("Charcuterie board", "Fresh veggies", "Pigs in a blanket"):
            self.assertIn(name, before)
        self.assertIn("Thinly sliced potatoes baked in cream", card)  # scalloped, not the whipped potatoes' blurb
        self.assertIn("Russets and Yukon Golds", card)
        main = card.split('course-name">The main course')[1].split('course-name">Alongside')[0]
        for name in ("Roast turkey", "Gravy", "Cranberry sauce"):
            self.assertIn(name, main)
        self.assertIn("Green bean casserole", card)
        self.assertIn("Sharp cheddar, Gruyère and goat cheese", card)
        self.assertIn("softly whipped cream", card)
        self.assertNotIn("House prep", card)
        for who in ("Parrisa", "Janice"):
            self.assertNotIn(who, card)  # who makes what stays off the shareable card
        self.assertNotIn("In the kitchen", page)
        self.assertIn("House prep steps", page)
        self.assertIn('id="add-dish"', page)
        self.assertEqual(self.client.get("/thanksgiving/photos/turkey.jpg").status_code, 404)

    def test_main_app_lets_signed_out_visitors_see_only_the_menu(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        client = TestClient(api.app)
        r = client.get("/thanksgiving/menu", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("The Menu", r.text)
        self.assertEqual(client.get("/thanksgiving", follow_redirects=False).status_code, 303)
        self.assertNotEqual(client.get("/thanksgiving/menu/1", follow_redirects=False).status_code, 303)  # shared recipes
        self.assertEqual(client.post("/thanksgiving/menu/1", follow_redirects=False).status_code, 303)
        self.assertEqual(client.get("/thanksgiving/menu/1/edit", follow_redirects=False).status_code, 303)

    def test_janice_brings_green_beans_and_cranberry_is_made_tuesday(self):
        self.client.get("/thanksgiving")
        plan = tg_router.store().get_plan()
        dishes = {d["name"]: d for d in tg_router.store().get_dishes(plan["id"])}
        beans, cran = dishes["Green bean casserole"], dishes["Cranberry sauce"]
        self.assertEqual((beans["cook"], beans["ingredients_text"]), ("Janice", ""))  # nothing to buy
        warm = next(s for s in beans["steps"] if s["id"] == "warm")
        self.assertEqual((warm["anchor"], warm["temp"]), ("T-25", 400))  # same oven temperature as the mac
        self.assertEqual(next(s for s in cran["steps"] if s["id"] == "make")["anchor"], "D-2 16:00")  # before the sourdough mixing at 6
        self.assertIn("Janice's", self.dish_page(beans))
        grocery = self.client.get("/thanksgiving/shopping").text
        self.assertIn("cranberries", grocery)
        self.assertIn("powdered sugar", grocery.lower())  # the pie's whipped cream
        self.assertNotIn("crispy fried onions", grocery)

    def test_plan_made_earlier_gets_the_new_dishes_once(self):
        self.client.get("/thanksgiving")
        plan = tg_router.store().get_plan()
        conn = self.db.connect()
        # Make it look like the plan from before 11/22.
        conn.execute("DELETE FROM tg_dishes WHERE plan_id = ? AND name IN ('Green bean casserole', 'Cranberry sauce')",
                     (plan["id"],))
        conn.execute("UPDATE tg_dishes SET cook = '' WHERE plan_id = ? AND name = 'Mac and cheese'", (plan["id"],))
        pie = next(d for d in tg_router.store().get_dishes(plan["id"]) if d["name"] == "Pumpkin pie")
        old_ingredients = pie["ingredients_text"].replace(presets.WHIPPED_CREAM_INGREDIENTS, "To serve:\nwhipped cream")
        old_steps = [s for s in pie["steps"] if s["id"] != "cream"]
        conn.execute("UPDATE tg_dishes SET ingredients_text = ?, instructions_text = ?, steps_json = ? WHERE id = ?",
                     (old_ingredients, pie["instructions_text"].split("\nWhipped cream:")[0], json.dumps(old_steps), pie["id"]))
        conn.execute("DELETE FROM tg_checks WHERE plan_id = ? AND kind = 'migration' AND item_key IN "
                     "('add-green-beans-janice', 'add-cranberry', 'mac-by-parrisa', 'pie-whipped-cream')", (plan["id"],))
        conn.commit()
        for _ in range(2):
            tg_router.configure(self.db)
        dishes = tg_router.store().get_dishes(plan["id"])
        names = [d["name"] for d in dishes]
        self.assertEqual((names.count("Green bean casserole"), names.count("Cranberry sauce")), (1, 1))
        by_name = {d["name"]: d for d in dishes}
        self.assertEqual(by_name["Mac and cheese"]["cook"], "Parrisa")
        pie = by_name["Pumpkin pie"]
        self.assertIn("1 cup cold heavy cream", pie["ingredients_text"])
        self.assertNotIn("To serve:", pie["ingredients_text"])
        self.assertEqual(pie["instructions_text"].count("Whipped cream:"), 1)
        self.assertEqual([s["id"] for s in pie["steps"]].count("cream"), 1)

    def test_menu_lists_dishes_and_each_opens_its_own_page(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        menu = self.client.get("/thanksgiving/menu").text
        self.assertIn(f'href="/thanksgiving/menu/{mac["id"]}"', menu)
        self.assertNotIn('name="ingredients_text"', menu)  # the editor lives on the dish page
        page = self.dish_page(mac)
        self.assertIn("<h2>Mac and cheese</h2>", page)
        self.assertIn('name="ingredients_text"', page)
        self.assertIn("Ingredients", page)
        self.assertNotIn("Roast the turkey", page)  # only this dish's steps
        self.assertEqual(self.client.get("/thanksgiving/menu/99999").status_code, 404)
        recipes = self.client.get("/thanksgiving/recipes").text  # an index: each recipe lives on its dish page
        self.assertIn(f'id="recipe-{mac["id"]}"', recipes)
        self.assertIn(f'href="/thanksgiving/menu/{mac["id"]}"', recipes)
        self.assertNotIn("Whisk in the flour", recipes)

    def test_saves_return_to_the_page_they_came_from(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        key = f"{mac['id']}:{mac['steps'][0]['id']}"
        r = self.client.post("/thanksgiving/steps/toggle", data={"key": key, "done": "1"}, follow_redirects=False,
                             headers={"referer": f"http://testserver/thanksgiving/menu/{mac['id']}"})
        self.assertEqual(r.headers["location"], f"/thanksgiving/menu/{mac['id']}")
        r = self.client.post("/thanksgiving/steps/toggle", data={"key": key, "anchor": "task-x"}, follow_redirects=False,
                             headers={"referer": "http://evil.example/elsewhere"})
        self.assertEqual(r.headers["location"], "/thanksgiving/timeline#task-x")
        r = self.client.post("/thanksgiving/groceries/add", data={"text": "Ice"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/thanksgiving/shopping")
        r = self.client.post("/thanksgiving/dishes/add", data={"name": "Rolls"}, follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith("/thanksgiving/menu/"))

    def test_finished_days_can_fold_away(self):
        self.client.get("/thanksgiving")
        plan = tg_router.store().get_plan()
        context = tg_router.plan_context(plan)
        first_day = context["days"][0]
        for task in first_day["tasks"]:
            tg_router.store().set_checked(plan["id"], "step", task["key"], True)
        page = self.client.get("/thanksgiving/timeline").text
        self.assertEqual(page.count('class="day all-done"'), 1)
        self.assertIn(f"all {len(first_day['tasks'])} done", page)
        self.assertIn('id="hide-done" checked', page)

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

    def all_pages(self):
        """Every planner page (each tab and each dish page), joined, for checks that don't care which page."""
        plan = tg_router.store().get_plan()
        urls = ["/thanksgiving", "/thanksgiving/timeline", "/thanksgiving/menu", "/thanksgiving/recipes",
                "/thanksgiving/shopping", "/thanksgiving/settings"]
        urls += [f"/thanksgiving/menu/{d['id']}" for d in tg_router.store().get_dishes(plan["id"])]
        return "\n".join(self.client.get(u).text for u in urls)

    def dish_page(self, dish):
        return self.client.get(f"/thanksgiving/menu/{dish['id']}").text

    def mac(self):
        plan = tg_router.store().get_plan()
        return plan, next(d for d in tg_router.store().get_dishes(plan["id"]) if d["name"] == "Mac and cheese")

    def test_mac_starts_full_and_dropdown_switches_to_half(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        self.assertEqual(mac["scale"], 1.0)
        page = self.dish_page(mac)
        self.assertIn("Batch size", page)
        self.assertIn('value="1" selected>Full · 9x13 pan · about 8 servings', page)
        self.assertIn("Half · 8x8 pan · about 4 servings", page)
        self.assertNotIn("Thanksgiving: 3", page)
        r = self.client.post(f"/thanksgiving/dishes/{mac['id']}/size", data={"scale": "0.5"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], f"/thanksgiving/menu/{mac['id']}#size-{mac['id']}")
        self.assertEqual(self.mac()[1]["scale"], 0.5)
        page = self.dish_page(mac)
        self.assertIn("8 oz elbow macaroni", page)
        self.assertIn("buttered 8x8 dish", page)
        self.assertIn("1 lb elbow macaroni", page)  # the editor keeps the recipe as written
        groceries = {i["text"]: i["amount"] for s in tg_router.plan_context(plan)["groceries"] for i in s["items"]}
        self.assertEqual(groceries["Sharp cheddar cheese"], "5 oz")
        self.assertEqual(groceries["Goat cheese"], "2 oz")
        for retired in ("0.75", "3"):
            self.assertEqual(self.client.post(f"/thanksgiving/dishes/{mac['id']}/size",
                                              data={"scale": retired}).status_code, 400)
        state = self.client.get("/thanksgiving/api/state").json()
        cook_mac = next(d for d in state["dishes"] if d["name"] == "Mac and cheese")
        self.assertEqual(cook_mac["size_label"], "Half · 8x8 pan · about 4 servings")
        self.assertIn("8 oz elbow macaroni or cavatappi", [l for s in cook_mac["ingredient_sections"] for l in s["lines"]])

    def test_size_picker_shows_wherever_the_recipe_opens(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        page = self.all_pages()
        # The recipe lives on the dish page, which carries the picker (cook mode has its own).
        self.assertGreaterEqual(page.count('aria-label="Batch size for Mac and cheese"'), 1)
        self.assertIn(f'<span data-size-label-for="{mac["id"]}">· Full · 9x13 pan · about 8 servings</span>',
                      self.dish_page(mac))
        # The recipe text sits in blocks the page refreshes in place when the size changes.
        self.assertGreaterEqual(page.count(f'data-recipe-for="{mac["id"]}"'), 1)
        state = self.client.get("/thanksgiving/api/state").json()
        cook_mac = next(d for d in state["dishes"] if d["name"] == "Mac and cheese")
        self.assertEqual(cook_mac["size_value"], "1")
        self.assertEqual([o["value"] for o in cook_mac["size_options"]], ["0.5", "1"])
        r = self.client.post(f"/thanksgiving/api/dishes/{mac['id']}/size", json={"scale": "0.5"})
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(self.mac()[1]["scale"], 0.5)
        self.assertEqual(self.client.post(f"/thanksgiving/api/dishes/{mac['id']}/size", json={"scale": "9"}).status_code, 400)

    def test_checked_groceries_wait_in_the_cart(self):
        self.client.get("/thanksgiving")
        page = self.client.get("/thanksgiving/shopping").text
        self.assertNotIn('id="in-the-cart"', page)
        key = re.search(r'name="key" value="([^"]+)"', page).group(1)
        r = self.client.post("/thanksgiving/groceries/toggle", data={"key": key, "done": "1", "row": "g-produce-1"},
                             follow_redirects=False)
        self.assertEqual(r.headers["location"], "/thanksgiving/shopping#g-produce-1")
        page = self.client.get("/thanksgiving/shopping").text
        cart = page.split('id="in-the-cart"')[1]
        self.assertIn("In the cart (1)", cart)
        self.assertIn(f'value="{key}"', cart)
        self.assertEqual(page.count(f'name="key" value="{key}"'), 1)  # gone from its aisle
        self.assertIn('aria-label="Not bought', cart)  # uncheck it from the cart
        self.client.post("/thanksgiving/groceries/toggle", data={"key": key, "row": "<script>"})
        self.assertNotIn('id="in-the-cart"', self.client.get("/thanksgiving/shopping").text)

    def test_finish_trip_moves_what_we_didnt_get_to_a_new_list(self):
        from apps.kitchen.shopping.store import ShoppingStore
        self.client.get("/thanksgiving")
        page = self.client.get("/thanksgiving/shopping").text
        self.assertIn('href="/thanksgiving/shopping/finish"', page)
        keys = re.findall(r'name="key" value="([^"]+)"', page)
        got, later, skip, untouched = keys[0], keys[1], keys[2], keys[3:]
        self.client.post("/thanksgiving/groceries/toggle", data={"key": got, "done": "1"})
        finish = self.client.get("/thanksgiving/shopping/finish").text
        self.assertNotIn(f'name="pick-{got}"', finish)  # already in the cart
        self.assertIn(f'name="pick-{later}" value="next" checked', finish)  # "Next trip" is the default
        form = {f"pick-{later}": "next", f"pick-{skip}": "skip"}
        form.update({f"pick-{k}": "got" for k in untouched})
        r = self.client.post("/thanksgiving/shopping/finish", data=form, follow_redirects=False)
        list_id = int(re.search(r"trip=(\d+)&n=1", r.headers["location"]).group(1))
        made = ShoppingStore(self.db).get_list(list_id)
        self.assertTrue(made["title"].startswith("Next trip"))
        linked = ShoppingStore(self.db).get_links(list_id)  # a portable item, still tied to Thanksgiving
        self.assertEqual([(l["source_app"], l["source_key"]) for l in linked], [("thanksgiving", later)])
        self.assertEqual(ShoppingStore(self.db).get_extras(list_id), [])
        page = self.client.get(r.headers["location"]).text
        self.assertIn("1 item moved to", page)
        self.assertIn(f'href="/apps/recipes/grocery-lists/{list_id}"', page)
        aside = page.split('id="set-aside"')[1]
        self.assertIn("Not buying now (2)", aside)
        self.assertIn(f'<a href="/apps/recipes/grocery-lists/{list_id}">On your next-trip list</a>', aside)
        self.assertIn("Don't need", aside)
        self.assertNotIn('href="/thanksgiving/shopping/finish"', page)  # nothing left to decide
        self.client.post("/thanksgiving/groceries/put-back", data={"key": skip})
        page = self.client.get("/thanksgiving/shopping").text
        self.assertIn("Not buying now (1)", page)
        self.assertIn(f'name="key" value="{skip}"', page)

    def test_next_trip_items_stay_linked_to_thanksgiving(self):
        from apps.kitchen.shopping import router as shopping
        from apps.kitchen.shopping.store import ShoppingStore
        self.client.get("/thanksgiving")
        keys = re.findall(r'name="key" value="([^"]+)"', self.client.get("/thanksgiving/shopping").text)
        milk, herbs = keys[0], keys[1]
        r = self.client.post("/thanksgiving/shopping/finish", data={f"pick-{milk}": "next", f"pick-{herbs}": "next"},
                             follow_redirects=False)
        list_id = int(re.search(r"trip=(\d+)", r.headers["location"]).group(1))
        page = self.client.get("/thanksgiving/shopping").text
        self.assertIn(f'<a href="/apps/recipes/grocery-lists/{list_id}">On your next-trip list</a>', page)
        # On the Kitchen list they're ordinary items, with Thanksgiving's keys and what they're for.
        self.addCleanup(shopping._state.update, dict(shopping._state))
        shopping.configure(self.db, meal_sources=lambda ids: [], pending_meals=lambda: [],
                           component_sources=lambda ids: [])
        kitchen = shopping.list_items(ShoppingStore(self.db).get_list(list_id))
        on_list = {i["key"]: i for section in kitchen["sections"] for i in section["items"]}
        self.assertEqual(set(on_list), {milk, herbs})
        self.assertEqual(on_list[milk]["linked"], ["Thanksgiving"])
        # Bought on the Kitchen list: in the cart here too.
        ShoppingStore(self.db).set_checked(list_id, milk, True)
        page = self.client.get("/thanksgiving/shopping").text
        self.assertIn("Not buying now (1)", page)
        self.assertIn(f'value="{milk}"', page.split('id="in-the-cart"')[1].split('id="set-aside"')[0])
        # Unchecked here: unchecked on the Kitchen list too, so it's back to waiting on the next trip.
        self.client.post("/thanksgiving/groceries/toggle", data={"key": milk, "done": ""})
        self.assertNotIn(milk, ShoppingStore(self.db).get_checked(list_id))
        self.assertIn("Not buying now (2)", self.client.get("/thanksgiving/shopping").text)
        # Put back here: off the Kitchen list.
        self.client.post("/thanksgiving/groceries/put-back", data={"key": herbs})
        self.assertEqual([l["source_key"] for l in ShoppingStore(self.db).get_links(list_id)], [milk])
        # The Kitchen list deleted: the item comes back to buy here, not lost.
        ShoppingStore(self.db).delete_list(list_id)
        page = self.client.get("/thanksgiving/shopping").text
        self.assertNotIn('id="set-aside"', page)
        self.assertIn(f'name="key" value="{milk}"', page)
        self.assertEqual(tg_router.store().get_list_links(tg_router.store().get_plan()["id"]), {})

    def test_finish_trip_keeps_items_if_the_list_cant_be_made(self):
        class Down:
            def new_list(self, title, lines):
                raise RuntimeError("platform down")
        self.client.get("/thanksgiving")
        tg_router.configure(self.db, platform=Down())
        key = re.search(r'name="key" value="([^"]+)"', self.client.get("/thanksgiving/shopping").text).group(1)
        r = self.client.post("/thanksgiving/shopping/finish", data={f"pick-{key}": "next"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/thanksgiving/shopping?trip=failed")
        page = self.client.get(r.headers["location"]).text
        self.assertIn("Couldn't make the next-trip list", page)
        self.assertNotIn('id="set-aside"', page)

    def test_plan_made_before_the_appetizers_gets_them_once(self):
        self.client.get("/thanksgiving")
        plan = tg_router.store().get_plan()
        conn = self.db.connect()
        new = ("Charcuterie board", "Fresh veggies", "Pigs in a blanket", "Scalloped potatoes", "Braised onions")
        conn.execute(f"DELETE FROM tg_dishes WHERE name IN ({','.join('?' * len(new))})", new)
        conn.execute("DELETE FROM tg_checks WHERE kind = 'migration' AND item_key IN "
                     "('add-charcuterie', 'add-veggies', 'add-pigs', 'add-scalloped-janice', 'add-onions-janice')")
        conn.commit()
        tg_router.configure(self.db)  # restart
        dishes = {d["name"]: d for d in tg_router.store().get_dishes(plan["id"])}
        for name in new:
            self.assertIn(name, dishes)
        self.assertEqual(dishes["Charcuterie board"]["cook"], "Parrisa")
        self.assertEqual(dishes["Braised onions"]["cook"], "Janice")
        self.assertEqual(dishes["Pigs in a blanket"]["category"], "appetizer")
        tg_router.configure(self.db)  # a second restart adds nothing twice
        names = [d["name"] for d in tg_router.store().get_dishes(plan["id"])]
        self.assertEqual(names.count("Pigs in a blanket"), 1)

    def test_saved_sourdough_gets_one_fold_step_and_the_olive_loaf_once(self):
        from apps.kitchen.thanksgiving import presets
        self.client.get("/thanksgiving")
        plan = tg_router.store().get_plan()
        conn = self.db.connect()
        row = conn.execute("SELECT id, steps_json FROM tg_dishes WHERE name = 'Sourdough'").fetchone()
        steps = [s for s in json.loads(row["steps_json"]) if s["id"] != "folds"]
        old_folds = [presets._s(f"fold{i}", f"D-2 {t}", f"Stretch and fold ({i} of 6)", 3)
                     for i, t in enumerate(("18:15", "18:30", "18:45", "19:00", "19:30", "20:00"), start=1)]
        at = next(i for i, s in enumerate(steps) if s["id"] == "mix") + 1
        steps[at:at] = old_folds
        old_text = presets.SOURDOUGH_INSTRUCTIONS.replace(
            "Stretch and folds, 6 sets over 2 hours: every 15 minutes for the first hour, then every 30 minutes. "
            "In cook mode, Start fold timers chimes for each set.",
            "Stretch and fold every 15 minutes for 1 hour (4 sets).\nTwo more stretch and folds over the next hour.")
        conn.execute("UPDATE tg_dishes SET steps_json = ?, instructions_text = ? WHERE id = ?",
                     (json.dumps(steps), old_text, row["id"]))
        conn.execute("DELETE FROM tg_dishes WHERE name = 'Olive and rosemary sourdough'")
        conn.execute("DELETE FROM tg_checks WHERE kind = 'migration' AND item_key IN "
                     "('sourdough-fold-timers', 'add-olive-sourdough')")
        conn.commit()
        tg_router.configure(self.db)  # restart
        dishes = {d["name"]: d for d in tg_router.store().get_dishes(plan["id"])}
        sourdough = dishes["Sourdough"]
        ids = [s["id"] for s in sourdough["steps"]]
        self.assertNotIn("fold1", ids)
        self.assertEqual(ids.count("folds"), 1)
        folds = next(s for s in sourdough["steps"] if s["id"] == "folds")
        self.assertEqual((folds["anchor"], len(folds["series"])), ("D-2 18:05", 6))
        self.assertIn("Start fold timers chimes", sourdough["instructions_text"])
        self.assertIn("Olive and rosemary sourdough", dishes)
        tg_router.configure(self.db)  # a second restart changes nothing
        names = [d["name"] for d in tg_router.store().get_dishes(plan["id"])]
        self.assertEqual(names.count("Olive and rosemary sourdough"), 1)

    def test_step_timers_survive_editing_and_start_from_cook_mode_or_dieter(self):
        self.client.get("/thanksgiving")
        plan = tg_router.store().get_plan()
        sourdough = next(d for d in tg_router.store().get_dishes(plan["id"]) if d["name"] == "Sourdough")
        state = self.client.get("/thanksgiving/api/state").json()
        folds = next(s for d in state["dishes"] if d["id"] == sourdough["id"] for s in d["steps"] if s["key"].endswith(":folds"))
        self.assertEqual((len(folds["series"]), folds["series_button"]), (6, "Start fold timers"))
        cook = self.client.get("/thanksgiving/cook").text
        self.assertIn("data-series=", cook)
        # Saving the dish in the editor keeps the timers (they aren't on the form)
        page = self.client.get(f"/thanksgiving/menu/{sourdough['id']}").text
        form = {"name": "Sourdough", "category": "bread", "ingredients_text": sourdough["ingredients_text"],
                "instructions_text": sourdough["instructions_text"], "notes": sourdough["notes"]}
        fields = []
        for step in sourdough["steps"]:
            fields += [("step_id", step["id"]), ("step_title", step["title"]), ("step_day", "0"), ("step_time", "12:00"),
                       ("step_minutes", str(step["minutes"])), ("step_kind", step["kind"]), ("step_temp", str(step["temp"])),
                       ("step_detail", step["detail"])]
        from urllib.parse import urlencode
        self.client.post(f"/thanksgiving/dishes/{sourdough['id']}/save", content=urlencode(list(form.items()) + fields),
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        saved = next(s for s in tg_router.store().get_dish(plan["id"], sourdough["id"])["steps"] if s["id"] == "folds")
        self.assertEqual(len(saved["series"]), 6)
        self.assertEqual(saved["series_button"], "Start fold timers")
        # Dieter can start them too
        from apps.kitchen.thanksgiving import connector
        tools = connector.tools()
        started = tools.call("thanksgiving_start_step_timers", {"dish": "Sourdough"})
        self.assertEqual([t["label"] for t in started["started"]][:2], ["Stretch and fold 1 of 6", "Stretch and fold 2 of 6"])
        tools.call("thanksgiving_start_timer", {"label": "potatoes", "minutes": 20})
        labels = [t["label"] for t in self.client.get("/thanksgiving/api/state").json()["timers"]]
        self.assertEqual(len(labels), 7)
        self.assertIn("potatoes", labels)
        with self.assertRaisesRegex(Exception, "no step with timers"):
            tools.call("thanksgiving_start_step_timers", {"dish": "Gravy"})
        # Starting them again replaces the running set, here offset so the first fold is in 10 minutes.
        again = tools.call("thanksgiving_start_step_timers", {"dish": "Sourdough", "first_in_minutes": 10})
        self.assertEqual(again["replaced_running"], 6)
        self.assertEqual([t["minutes"] for t in again["started"]], [10, 25, 40, 55, 85, 115])
        self.assertEqual(again["usual_pattern"], "first at 15 min, then every 15 min (x3), then every 30 min (x2)")
        running = tools.call("thanksgiving_list_timers", {})["running"]
        self.assertEqual(len(running), 7)  # six folds and the potatoes
        # "Leave out the first timer": folds 2 to 6 at their usual times.
        skipped = tools.call("thanksgiving_start_step_timers", {"dish": "Sourdough", "skip": [1]})
        self.assertEqual([(t["minutes"], t["label"][-6:]) for t in skipped["started"]][:2], [(30, "2 of 6"), (45, "3 of 6")])
        potatoes = next(t for t in tools.call("thanksgiving_list_timers", {})["running"] if t["label"] == "potatoes")
        tools.call("thanksgiving_change_timer", {"timer_id": potatoes["id"], "minutes_from_now": 5})
        moved = next(t for t in tools.call("thanksgiving_list_timers", {})["running"] if t["id"] == potatoes["id"])
        self.assertIn(moved["minutes_left"], (4, 5))
        # Stray fold timers someone made by hand ("Fold 4", "Last stretch and fold") go too when folds are re-timed.
        tools.call("thanksgiving_start_timer", {"label": "Fold 4", "minutes": 12})
        tools.call("thanksgiving_start_timer", {"label": "Last stretch and fold", "minutes": 90})
        # "I just did fold 3": folds 4 to 6 at their usual gaps from now (15, then 30, then 30).
        after3 = tools.call("thanksgiving_start_step_timers", {"dish": "Sourdough", "done_through": 3})
        self.assertEqual(after3["replaced_running"], 7)  # five from skip=[1], plus the two strays
        self.assertEqual([(t["minutes"], t["label"][-6:]) for t in after3["started"]],
                         [(15, "4 of 6"), (45, "5 of 6"), (75, "6 of 6")])
        pushed = tools.call("thanksgiving_shift_timers", {"label_words": "fold", "minutes": 5})
        self.assertEqual([t["minutes_left"] for t in pushed["moved"]], [20, 50, 80])
        gone = tools.call("thanksgiving_cancel_timers", {"label_words": "fold"})
        self.assertEqual(len(gone["cancelled"]), 3)
        self.assertEqual([t["label"] for t in tools.call("thanksgiving_list_timers", {})["running"]], ["potatoes"])

    def test_saved_mac_gets_goat_cheese_swap_on_restart(self):
        from apps.kitchen.thanksgiving import presets
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        conn = self.db.connect()
        conn.execute("UPDATE tg_dishes SET ingredients_text = ?, instructions_text = ?, scale = 0.5 WHERE id = ?",
                     (presets.OLD_MAC_INGREDIENTS, presets.OLD_MAC_INSTRUCTIONS, mac["id"]))
        # The one-time switch to a full batch for Thanksgiving dinner already ran; a size chosen after it stays.
        conn.execute("INSERT INTO tg_checks (plan_id, kind, item_key) VALUES (?, 'migration', 'mac-full-batch')", (plan["id"],))
        conn.commit()
        tg_router.configure(self.db)  # restart
        upgraded = self.mac()[1]
        self.assertIn("4 oz goat cheese, crumbled", upgraded["ingredients_text"])
        self.assertIn("10 oz sharp cheddar", upgraded["ingredients_text"])
        self.assertIn("goat cheese last", upgraded["instructions_text"])
        self.assertEqual(upgraded["scale"], 0.5)
        self.assertEqual(upgraded["steps"], mac["steps"])
        page = self.dish_page(mac)
        self.assertIn("2 oz goat cheese, crumbled", page)  # half batch
        self.assertIn("5 oz sharp cheddar", page)

    def test_saved_mac_gets_covering_and_rack_instructions(self):
        from apps.kitchen.thanksgiving import presets
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        conn = self.db.connect()
        conn.execute("UPDATE tg_dishes SET instructions_text = ? WHERE id = ?", (presets.MAC_INSTRUCTIONS_V2, mac["id"]))
        conn.commit()
        tg_router.configure(self.db)  # restart
        text = self.mac()[1]["instructions_text"]
        self.assertIn("bake UNCOVERED at 400°F", text)
        self.assertIn("Cover tightly with foil and bake at 400°F for 20 minutes", text)
        self.assertIn("Rack: middle of the oven", text)
        conn.execute("UPDATE tg_dishes SET instructions_text = 'My own notes' WHERE id = ?", (mac["id"],))
        conn.commit()
        tg_router.configure(self.db)
        self.assertEqual(self.mac()[1]["instructions_text"], "My own notes")  # your edits are never overwritten

    def test_speak_streams_the_voice(self):
        said = []

        def speaker(text):
            said.append(text)
            yield b"ID3"
            yield b"more"
        tg_router.configure(self.db, speaker=speaker)
        r = self.client.get("/thanksgiving/api/speak", params={"text": "  Next, baste the turkey.  "})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "audio/mpeg")
        self.assertEqual(r.content, b"ID3more")
        self.assertEqual(said, ["Next, baste the turkey."])
        self.assertEqual(self.client.get("/thanksgiving/api/speak", params={"text": " "}).status_code, 400)

    def test_speak_fails_cleanly_so_the_tablet_uses_its_own_voice(self):
        def speaker(text):
            raise RuntimeError("no voice today")
            yield b""
        tg_router.configure(self.db, speaker=speaker)
        self.assertEqual(self.client.get("/thanksgiving/api/speak", params={"text": "Hello"}).status_code, 502)

    def voice_with(self, reply):
        """Swap in a fake model that returns `reply` (a dict) and records what it was shown."""
        seen = {}

        def fake_model(messages):
            seen["messages"] = messages
            return json.dumps(reply)

        def fast_hands_off(messages):
            seen["fast_messages"] = messages
            return json.dumps({"route": "deep", "say_now": "One sec.", "why": "test"})

        tg_router.configure(self.db, voice_model=fake_model, fast_model=fast_hands_off)
        return seen

    def fast_with(self, reply, deep=None):
        """Fake fast model returning `reply`; the deep model returns `deep` (or fails the test if called)."""
        seen = {"deep_calls": 0}

        def fast(messages):
            seen["fast_messages"] = messages
            return json.dumps(reply)

        def deep_model(messages):
            seen["deep_calls"] += 1
            seen["messages"] = messages
            return json.dumps(deep or {"reply": "deep", "question": "", "actions": []})

        tg_router.configure(self.db, voice_model=deep_model, fast_model=fast)
        return seen

    def pie(self):
        plan = tg_router.store().get_plan()
        return plan, next(d for d in tg_router.store().get_dishes(plan["id"]) if d["name"] == "Pumpkin pie")

    def test_dish_page_delay_pushes_unfinished_steps_and_undoes(self):
        self.client.get("/thanksgiving")
        plan, pie = self.pie()
        dinner = planner.parse_dt(plan["dinner_at"])
        when = lambda d: {s["id"]: planner.resolve_anchor(s["anchor"], dinner) for s in d["steps"]}
        before = when(pie)
        first = pie["steps"][0]["id"]
        self.client.post("/thanksgiving/steps/toggle", data={"key": f"{pie['id']}:{first}", "done": "1"})
        page = self.client.get(f"/thanksgiving/menu/{pie['id']}").text
        self.assertIn(f'action="/thanksgiving/dishes/{pie["id"]}/delay"', page)
        self.assertIn("data-step-toggle", page)  # ticks save in place, no reload
        r = self.client.post(f"/thanksgiving/dishes/{pie['id']}/delay", data={"minutes": "30"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith(f"/thanksgiving/menu/{pie['id']}?delayed="))
        after = when(self.pie()[1])
        self.assertEqual(after[first], before[first])  # done steps stay put
        for step_id in before:
            if step_id != first:
                self.assertEqual((after[step_id] - before[step_id]).total_seconds(), 30 * 60, step_id)
        page = self.client.get(r.headers["location"]).text
        self.assertIn("Moved the rest of Pumpkin pie 30 min later", page)
        self.client.post(f"/thanksgiving/dishes/{pie['id']}/delay/undo")
        self.assertEqual(when(self.pie()[1]), before)

    def test_dish_page_delay_wont_go_past_dinner(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        before = mac["steps"]
        r = self.client.post(f"/thanksgiving/dishes/{mac['id']}/delay", data={"minutes": "1440"}, follow_redirects=False)
        self.assertIn("delay_problem=", r.headers["location"])
        self.assertEqual(self.mac()[1]["steps"], before)

    def test_voice_moves_the_crust_with_preview_apply_and_undo(self):
        self.client.get("/thanksgiving")
        plan, pie = self.pie()
        before = {s["id"]: s["anchor"] for s in pie["steps"]}
        seen = self.voice_with({"reply": "Moving the crust to Saturday at 7 PM.", "question": "", "actions": [
            {"type": "move", "dish_id": pie["id"], "step_ids": ["dough", "shell"], "date": "2026-11-21", "time": "19:00"}]})
        r = self.client.post("/thanksgiving/api/command", json={"text": "move the pie crust to Saturday at 7 PM"}).json()
        self.assertTrue(r["can_apply"])
        self.assertEqual(len(r["changes"]), 2)
        self.assertIn("Sat 7:00 PM", r["changes"][0])
        self.assertIn("Sat 8:15 PM", r["changes"][1])  # the roll-and-freeze step keeps its spacing
        summary = seen["messages"][1]["content"]
        self.assertIn("step dough: Make the butter crust dough and chill it [section Crust]", summary)
        self.assertEqual(tg_router.store().get_dish(plan["id"], pie["id"])["steps"], pie["steps"])  # preview only
        self.assertEqual(self.client.post("/thanksgiving/api/command/apply").json(), {"ok": True})
        moved = {s["id"]: s["anchor"] for s in self.pie()[1]["steps"]}
        self.assertEqual(moved["dough"], "D-5 19:00")
        self.assertEqual(moved["shell"], "D-5 20:15")
        self.assertEqual(moved["bake1"], before["bake1"])  # the rest of the pie stays put
        self.assertEqual(self.client.post("/thanksgiving/api/command/apply").status_code, 409)
        self.assertEqual(self.client.post("/thanksgiving/api/command/undo").json(), {"ok": True})
        self.assertEqual({s["id"]: s["anchor"] for s in self.pie()[1]["steps"]}, before)
        self.assertEqual(self.client.post("/thanksgiving/api/command/undo").status_code, 409)

    def test_voice_asks_when_unclear_and_cancel_clears(self):
        self.client.get("/thanksgiving")
        self.voice_with({"reply": "", "question": "Saturday or Sunday, and what time?", "actions": []})
        r = self.client.post("/thanksgiving/api/command", json={"text": "push the crust to the weekend"}).json()
        self.assertEqual(r["question"], "Saturday or Sunday, and what time?")
        self.assertFalse(r["can_apply"])
        self.assertEqual(self.client.post("/thanksgiving/api/command/apply").status_code, 409)

    def test_voice_rejects_what_the_editor_would(self):
        self.client.get("/thanksgiving")
        plan, pie = self.pie()
        self.voice_with({"reply": "", "question": "", "actions": [
            {"type": "move", "dish_id": pie["id"], "step_ids": ["dough"], "date": "2026-11-28", "time": "10:00"},
            {"type": "assign", "dish_id": pie["id"], "step_ids": [], "cook": "Grandma"},
            {"type": "move", "dish_id": 999, "step_ids": ["dough"], "date": "2026-11-21", "time": "10:00"},
            {"type": "delete", "dish_id": pie["id"]},
            {"type": "size", "dish_id": pie["id"], "scale": 7}]})
        r = self.client.post("/thanksgiving/api/command", json={"text": "do weird things"}).json()
        self.assertFalse(r["can_apply"])
        self.assertEqual(len(r["problems"]), 5)
        self.assertIn("after dinner", r["problems"][0])

    def test_checking_off_by_voice_is_saved_right_away_and_undoable(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        self.voice_with({"reply": "Checked off the practice run.", "question": "", "actions": [
            {"type": "done", "dish_id": mac["id"], "step_ids": ["practice"], "done": True}]})
        r = self.client.post("/thanksgiving/api/command", json={"text": "I did the practice run"}).json()
        self.assertTrue(r["applied"])
        self.assertFalse(r["can_apply"])
        self.assertIn(f"{mac['id']}:practice", tg_router.store().get_checked(plan["id"], "step"))
        self.assertEqual(self.client.post("/thanksgiving/api/command/apply").status_code, 409)  # nothing pending
        self.client.post("/thanksgiving/api/command/undo")
        self.assertNotIn(f"{mac['id']}:practice", tg_router.store().get_checked(plan["id"], "step"))

    def test_the_model_is_told_which_steps_are_unchecked(self):
        self.client.get("/thanksgiving")
        seen = self.voice_with({"reply": "Here's what's left.", "question": "", "actions": []})
        self.client.post("/thanksgiving/api/command", json={"text": "what's not done"})
        plan_text = next(m["content"] for m in seen["messages"] if m["content"].startswith("The plan:"))
        self.assertIn("Status, unchecked steps from earlier", plan_text)
        self.assertIn("Status, next unchecked steps:", plan_text)
        # Every unchecked step is listed under one of the two, with its ids (which one depends on today's date).
        status = plan_text.split("Status, unchecked steps from earlier")[1].split("\nDish 1:")[0]
        self.assertIn("(step ", status)

    def test_voice_size_and_done_but_no_assigning_people(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        self.voice_with({"reply": "Got it.", "question": "", "actions": [
            {"type": "assign", "dish_id": mac["id"], "step_ids": [], "cook": "Parrisa"},
            {"type": "size", "dish_id": mac["id"], "scale": 0.5},
            {"type": "done", "dish_id": mac["id"], "step_ids": ["practice"], "done": True}]})
        r = self.client.post("/thanksgiving/api/command", json={"text": "half batch mac, practice is done"}).json()
        self.assertEqual(len(r["changes"]), 2)
        self.assertEqual(len(r["problems"]), 1)  # assigning cooks was removed
        summary_text = json.dumps(r)
        self.assertNotIn("Parrisa", summary_text)
        self.client.post("/thanksgiving/api/command/apply")
        mac = self.mac()[1]
        self.assertEqual(mac["scale"], 0.5)
        self.assertIn(f"{mac['id']}:practice", tg_router.store().get_checked(plan["id"], "step"))
        self.client.post("/thanksgiving/api/command/undo")
        mac = self.mac()[1]
        self.assertEqual(mac["scale"], 1.0)
        self.assertNotIn(f"{mac['id']}:practice", tg_router.store().get_checked(plan["id"], "step"))

    def test_voice_add_remove_edit_and_undo(self):
        self.client.get("/thanksgiving")
        plan, pie = self.pie()
        before = [dict(s) for s in pie["steps"]]
        self.voice_with({"reply": "Pecans, fancy.", "question": "", "actions": [
            {"type": "add", "dish_id": pie["id"], "title": "Toast pecans for the top", "date": "2026-11-25",
             "time": "09:00", "minutes": 12, "kind": "oven", "temp": 350},
            {"type": "remove", "dish_id": pie["id"], "step_ids": ["cream"]},
            {"type": "edit", "dish_id": pie["id"], "step_id": "cool", "minutes": 150, "detail": "Away from the dog"}]})
        r = self.client.post("/thanksgiving/api/command", json={"text": "pecans, no chilling, cool longer"}).json()
        self.assertEqual(r["problems"], [])
        self.assertIn("add “Toast pecans for the top”", r["changes"][0])
        self.assertIn("remove “Whip the cream for the pie and refrigerate”", r["changes"][1])
        self.assertIn("2h → 2h 30m", r["changes"][2])
        self.client.post("/thanksgiving/api/command/apply")
        steps = {s["id"]: s for s in self.pie()[1]["steps"]}
        self.assertNotIn("cream", steps)
        self.assertEqual(steps["cool"]["minutes"], 150)
        added = next(s for s in steps.values() if s["title"] == "Toast pecans for the top")
        self.assertEqual((added["anchor"], added["temp"], added["kind"]), ("D-1 09:00", 350, "oven"))
        self.client.post("/thanksgiving/api/command/undo")
        self.assertEqual(self.pie()[1]["steps"], before)

    def test_voice_puts_things_on_the_shopping_list_right_away(self):
        self.client.get("/thanksgiving")
        plan = tg_router.store().get_plan()
        seen = self.voice_with({"reply": "On the list.", "question": "", "actions": [
            {"type": "grocery", "text": "heavy whipping cream", "amount": "1 pint"},
            {"type": "grocery", "text": "  "}]})
        r = self.client.post("/thanksgiving/api/command", json={"text": "put whipping cream on the list"}).json()
        self.assertIn('"grocery"', seen["messages"][0]["content"])
        self.assertTrue(r["applied"])
        self.assertEqual(r["changes"], ["Shopping list: add “1 pint heavy whipping cream”"])
        self.assertEqual(len(r["problems"]), 1)
        self.assertEqual([e["text"] for e in tg_router.store().get_extra_groceries(plan["id"])],
                         ["1 pint heavy whipping cream"])
        self.assertIn("heavy whipping cream", self.client.get("/thanksgiving/shopping").text)
        self.client.post("/thanksgiving/api/command/undo")
        self.assertEqual(tg_router.store().get_extra_groceries(plan["id"]), [])

    def claude_with(self, *turns):
        """A fake Claude that plays `turns` in order. Each turn: a list of ("text", str) / ("tool", name, input)."""
        from types import SimpleNamespace as NS
        seen = {"calls": []}
        script = list(turns)

        def call(system, messages, tools, effort):
            seen["calls"].append({"system": system, "messages": [dict(m) for m in messages], "tools": tools})
            blocks = []
            for i, part in enumerate(script.pop(0) if script else [("text", "Done.")]):
                if part[0] == "text":
                    blocks.append(NS(type="text", text=part[1]))
                else:
                    blocks.append(NS(type="tool_use", id=f"t{len(seen['calls'])}-{i}", name=part[1], input=part[2]))
            stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
            return NS(stop_reason=stop, content=blocks)

        tg_router.configure(self.db, claude_call=call)
        return seen

    def test_claude_brain_reads_the_list_and_adds_to_it_with_undo(self):
        self.client.get("/thanksgiving")
        plan = tg_router.store().get_plan()
        seen = self.claude_with(
            [("tool", "thanksgiving_get_shopping_list", {})],
            [("tool", "propose_changes", {"actions": [{"type": "grocery", "text": "heavy whipping cream",
                                                         "amount": "1 pint"}]})],
            [("text", "Whipping cream's on the list. One pint covers the pie.")])
        r = self.client.post("/thanksgiving/api/command", json={"text": "put whipping cream on the list",
                                                                "brain": "claude"}).json()
        self.assertEqual(r["reply"], "Whipping cream's on the list. One pint covers the pie.")
        self.assertTrue(r["applied"])
        self.assertEqual(r["changes"], ["Shopping list: add “1 pint heavy whipping cream”"])
        first = seen["calls"][0]
        self.assertIn("You are Dieter", first["system"][0]["text"])
        self.assertIn("cache_control", first["system"][0])
        self.assertIn("The cook says: put whipping cream on the list", first["messages"][-1]["content"])
        self.assertIn("Pumpkin pie", first["messages"][-1]["content"])  # the plan comes along
        names = {t["name"] for t in first["tools"]}
        self.assertTrue({"propose_changes", "thanksgiving_get_shopping_list", "kitchen_get_shopping_list"} <= names)
        self.assertNotIn("stay_quiet", names)  # typed, so it can't ignore you
        listing = seen["calls"][1]["messages"][-1]["content"][0]
        self.assertEqual(listing["type"], "tool_result")
        self.assertIn('"to_buy"', listing["content"])
        self.assertEqual([e["text"] for e in tg_router.store().get_extra_groceries(plan["id"])],
                         ["1 pint heavy whipping cream"])
        self.client.post("/thanksgiving/api/command/undo")
        self.assertEqual(tg_router.store().get_extra_groceries(plan["id"]), [])

    def test_claude_brain_logs_what_it_cant_do(self):
        self.client.get("/thanksgiving")
        seen = self.claude_with(
            [("tool", "log_for_later", {"summary": "Wants the dog fed at 5"})],
            [("text", "I can't feed the dog yet, but I've logged it to review later.")])
        r = self.client.post("/thanksgiving/api/command", json={"text": "feed the dog at 5", "brain": "claude"}).json()
        self.assertIn("logged it", r["reply"])
        self.assertIn("log_for_later", {t["name"] for t in seen["calls"][0]["tools"]})
        self.assertIn("Things you can't do", seen["calls"][0]["system"][0]["text"])
        row = self.db.connect().execute("SELECT * FROM app_feedback_reports ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["title"], "[Dieter couldn't] Wants the dog fed at 5")
        self.assertIn("Asked Dieter: feed the dog at 5", row["raw_feedback"])

    def test_claude_brain_opens_pages_on_the_screen(self):
        self.client.get("/thanksgiving")
        plan, pie = self.pie()
        seen = self.claude_with(
            [("tool", "dieter_open_page", {"page": "thanksgiving.dish", "params": {"dish_id": "../etc"}})],
            [("tool", "dieter_open_page", {"page": "thanksgiving.dish", "params": {"dish_id": pie["id"]}})],
            [("text", "Here's the pie.")])
        r = self.client.post("/thanksgiving/api/command", json={"text": "pull up the pie recipe",
                                                                "brain": "claude"}).json()
        self.assertEqual(r["reply"], "Here's the pie.")
        self.assertEqual(r["do"], [{"type": "navigate", "url": f"/thanksgiving/menu/{pie['id']}", "title": "Dish"}])
        bad = seen["calls"][1]["messages"][-1]["content"][0]
        self.assertTrue(bad["is_error"])  # a bad id never becomes an address
        self.assertIn("dieter_open_page", {t["name"] for t in seen["calls"][0]["tools"]})
        self.assertIn("pull up", seen["calls"][0]["system"][0]["text"])
        # Classic answers never move the screen
        self.voice_with({"reply": "Sure.", "question": "", "actions": []})
        self.assertEqual(self.client.post("/thanksgiving/api/command", json={"text": "hi"}).json()["do"], [])

    def test_chat_pages_load_the_shared_actions_and_days_have_targets(self):
        self.client.get("/thanksgiving")
        page = self.client.get("/thanksgiving/timeline").text
        self.assertRegex(page, r'<script src="/static/ui/dieter-actions\.js\?v=[0-9a-f]{10}"></script>')
        self.assertIn('id="day-2026-11-26"', page)
        self.assertIn("DieterActions.resume()", page)

    def test_recipe_changes_wait_for_save_and_keep_history(self):
        self.client.get("/thanksgiving")
        plan, pie = self.pie()
        old = pie["ingredients_text"]
        new = old.replace("3/4 cup sugar", "2/3 cup sugar")
        seen = self.claude_with(
            [("tool", "thanksgiving_propose_recipe_change", {"dish": "Pumpkin pie", "ingredients": new,
                                                             "reason": "A little less sweet"})],
            [("text", "Here's a less sweet pie. Look it over and tap Save.")])
        r = self.client.post("/thanksgiving/api/command", json={"text": "make the pie less sweet", "brain": "claude"}).json()
        card = next(a["card"] for a in r["do"] if a["type"] == "proposal")
        self.assertIn({"type": "navigate", "url": f"/thanksgiving/menu/{pie['id']}", "title": "Dish", "auto": True}, r["do"])  # opened to show the card (cook mode stays put)
        self.assertEqual(card["title"], "Change the recipe: Pumpkin pie")
        self.assertIn({"op": "removed", "text": "3/4 cup sugar"}, card["diffs"][0]["lines"])
        self.assertIn({"op": "added", "text": "2/3 cup sugar"}, card["diffs"][0]["lines"])
        self.assertTrue(any("Sugar" in e or "sugar" in e for e in card["effects"]), card["effects"])
        self.assertEqual(self.pie()[1]["ingredients_text"], old)  # nothing saved yet
        page = self.client.get(f"/thanksgiving/menu/{pie['id']}").text
        self.assertIn("data-proposal-card=", page)  # the card waits on the dish page too
        saved = self.client.post(card["save_url"]).json()
        self.assertEqual(saved["card"]["status"], "saved")
        self.assertIn("2/3 cup sugar", self.pie()[1]["ingredients_text"])
        self.assertEqual(self.client.post(card["save_url"]).status_code, 409)  # only once
        page = self.client.get(f"/thanksgiving/menu/{pie['id']}").text
        self.assertIn("Recipe history", page)
        self.assertIn("A little less sweet", page)
        self.assertIn("make the pie less sweet", page)
        self.assertNotIn("data-proposal-card=", page)
        version = tg_router.store().get_recipe_versions(plan["id"], pie["id"])[0]
        self.client.post(f"/thanksgiving/dishes/{pie['id']}/versions/{version['id']}/restore")
        self.assertEqual(self.pie()[1]["ingredients_text"], old)
        self.assertEqual(len(tg_router.store().get_recipe_versions(plan["id"], pie["id"])), 2)  # the restore is history too

    def test_saying_yeah_does_not_save_a_recipe_but_save_it_does(self):
        self.client.get("/thanksgiving")
        plan, pie = self.pie()
        from apps.kitchen.thanksgiving import connector
        card = connector.propose_recipe_change({"dish": "Pumpkin pie", "notes": "Bake the day before.", "reason": "Note"})
        self.claude_with([("tool", "save_recipe_change", {"proposal_id": card["id"]})], [("text", "Say save it.")])
        r = self.client.post("/thanksgiving/api/command", json={"text": "yeah", "brain": "claude"}).json()
        self.assertNotEqual(self.pie()[1]["notes"], "Bake the day before.")
        self.claude_with([("tool", "save_recipe_change", {"proposal_id": card["id"]})], [("text", "Saved.")])
        r = self.client.post("/thanksgiving/api/command", json={"text": "yes, save it", "brain": "claude"}).json()
        self.assertEqual(self.pie()[1]["notes"], "Bake the day before.")
        self.assertIn({"type": "reload"}, r["do"])

    def test_a_card_goes_stale_if_the_recipe_was_edited_since(self):
        self.client.get("/thanksgiving")
        plan, pie = self.pie()
        from apps.kitchen.thanksgiving import connector
        card = connector.propose_recipe_change({"dish": "Pumpkin pie", "notes": "One", "reason": "Note"})
        tg_router.store().update_dish(plan["id"], pie["id"], dict(self.pie()[1], notes="Changed by hand"))
        r = self.client.post(card["save_url"])
        self.assertEqual(r.status_code, 409)
        self.assertIn("changed since", r.json()["detail"])
        self.assertEqual(self.pie()[1]["notes"], "Changed by hand")
        with self.assertRaisesRegex(Exception, "nothing would change"):
            connector.propose_recipe_change({"dish": "Pumpkin pie", "notes": "Changed by hand", "reason": "x"})
        discard = self.client.post(connector.propose_recipe_change(
            {"dish": "Pumpkin pie", "notes": "Two", "reason": "x"})["discard_url"]).json()
        self.assertEqual(discard["card"]["status"], "discarded")

    def test_claude_brain_plan_changes_wait_for_apply(self):
        self.client.get("/thanksgiving")
        self.claude_with(
            [("tool", "propose_changes", {"actions": [{"type": "dinner", "date": "2026-11-26", "time": "17:00"}]})],
            [("text", "I'd push dinner to five.")])
        r = self.client.post("/thanksgiving/api/command", json={"text": "dinner at 5", "brain": "claude"}).json()
        self.assertFalse(r["applied"])
        self.assertTrue(r["can_apply"])
        self.assertEqual(tg_router.store().get_plan()["dinner_at"], "2026-11-26T16:00")
        self.client.post("/thanksgiving/api/command/apply")
        self.assertEqual(tg_router.store().get_plan()["dinner_at"], "2026-11-26T17:00")

    def test_claude_brain_stays_quiet_when_the_mic_hears_someone_else(self):
        self.client.get("/thanksgiving")
        seen = self.claude_with([("tool", "stay_quiet", {})], [("text", "")])
        r = self.client.post("/thanksgiving/api/handsfree/ask", json={"text": "pass the salt", "via": "follow-up",
                                                                      "brain": "claude"}).json()
        self.assertTrue(r["ignored"])
        system = " ".join(block["text"] for block in seen["calls"][0]["system"])
        self.assertIn("call stay_quiet", system)
        self.assertNotIn('{"ignore": true}', system)

    def test_claude_brain_needs_a_key_or_stays_classic(self):
        self.assertEqual(tg_router._brain_for({"brain": "claude"}), "classic")  # no key in tests
        self.claude_with()
        self.assertEqual(tg_router._brain_for({"brain": "claude"}), "claude")
        self.assertEqual(tg_router._brain_for({"brain": "classic"}), "classic")
        self.assertEqual(tg_router._brain_for({}), "claude")  # Claude is the default once there is a key

    def test_voice_moves_dinner_and_undo_restores_it(self):
        self.client.get("/thanksgiving")
        self.voice_with({"reply": "Fashionably late.", "question": "", "actions": [
            {"type": "dinner", "date": "2026-11-26", "time": "17:00"}]})
        r = self.client.post("/thanksgiving/api/command", json={"text": "move dinner to 5"}).json()
        self.assertIn("Dinner: Thu 4:00 PM → Thu 5:00 PM", r["changes"][0])
        self.client.post("/thanksgiving/api/command/apply")
        self.assertEqual(tg_router.store().get_plan()["dinner_at"], "2026-11-26T17:00")
        self.client.post("/thanksgiving/api/command/undo")
        self.assertEqual(tg_router.store().get_plan()["dinner_at"], "2026-11-26T16:00")

    def test_voice_warns_about_new_oven_clashes(self):
        self.client.get("/thanksgiving")
        plan, pie = self.pie()
        # The pie's 425°F bake moved onto dinner day lands while the turkey roasts at 325°F.
        self.voice_with({"reply": "Living dangerously.", "question": "", "actions": [
            {"type": "move", "dish_id": pie["id"], "step_ids": ["bake1"], "date": "2026-11-26", "time": "13:00"}]})
        r = self.client.post("/thanksgiving/api/command", json={"text": "bake the pie thursday at 1"}).json()
        self.assertTrue(r["can_apply"])
        self.assertTrue(any("different temperatures" in w for w in r["warnings"]))

    def test_voice_sass_level_comes_from_the_device(self):
        self.client.get("/thanksgiving")
        seen = self.voice_with({"reply": "Oh, honey.", "question": "", "actions": []})
        self.client.post("/thanksgiving/api/command", json={"text": "hi", "device": "Kitchen tablet", "sass": "super"})
        system = " ".join(m["content"] for m in seen["messages"] if m["role"] == "system")
        self.assertIn("SUPER", system)
        self.client.post("/thanksgiving/api/command", json={"text": "hi"})
        system = " ".join(m["content"] for m in seen["messages"] if m["role"] == "system")
        self.assertNotIn("Sass level for this device", system)
        self.client.post("/thanksgiving/api/command", json={"text": "hi", "sass": "nuclear"})  # unknown -> normal
        system = " ".join(m["content"] for m in seen["messages"] if m["role"] == "system")
        self.assertNotIn("Sass level for this device", system)
        self.assertIn("This device:", self.client.get("/thanksgiving").text)

    def test_handsfree_ignores_chatter_and_runs_quick_commands(self):
        self.client.get("/thanksgiving")
        seen = self.voice_with({"reply": "Should not be called.", "question": "", "actions": []})
        heard = lambda text, **kw: self.client.post("/thanksgiving/api/handsfree/heard", json={"text": text, **kw}).json()
        self.assertEqual({"addressed": False}, heard("we can do the next step after dinner"))
        self.assertEqual({"addressed": False}, heard("next step"))  # no name, no follow-up window
        self.assertEqual({"type": "next"}, heard("next step", follow_up=True)["command"])
        self.assertEqual({"type": "next"}, heard("Dieter, next step")["command"])
        self.assertEqual({"type": "timer", "minutes": 12, "label": ""}, heard("Peter, start a 12 minute timer")["command"])
        r = heard("Dieter can the crust sit overnight?")
        self.assertEqual("can the crust sit overnight", r["ask"])
        self.assertNotIn("messages", seen)  # the quick path never calls the AI

    def test_handsfree_ask_can_ignore_or_propose(self):
        self.client.get("/thanksgiving")
        plan, pie = self.pie()
        seen = self.voice_with({"ignore": True})
        r = self.client.post("/thanksgiving/api/handsfree/ask", json={"text": "pass the salt", "via": "follow-up"}).json()
        self.assertTrue(r["ignored"])
        system = " ".join(m["content"] for m in seen["messages"] if m["role"] == "system")
        self.assertIn("read aloud", system)
        self.assertIn("may be a reply to you", system)
        # Typed Tell Dieter never gets the "ignore" instructions.
        self.client.post("/thanksgiving/api/command", json={"text": "pass the salt"})
        self.assertNotIn("read aloud", " ".join(m["content"] for m in seen["messages"] if m["role"] == "system"))
        self.voice_with({"reply": "Pushing the dough back half an hour.", "question": "",
                         "actions": [{"type": "shift", "dish_id": pie["id"], "step_ids": ["dough"], "days": 0,
                                      "minutes": 30}]})
        r = self.client.post("/thanksgiving/api/handsfree/ask", json={"text": "push the dough back half an hour"}).json()
        self.assertFalse(r["ignored"])
        self.assertTrue(r["can_apply"], r)
        self.assertEqual(200, self.client.post("/thanksgiving/api/command/apply").status_code)

    def test_conversation_mic_can_end_itself(self):
        self.client.get("/thanksgiving")
        seen = self.voice_with({"reply": "Happy cooking!", "question": "", "actions": [], "end": True})
        r = self.client.post("/thanksgiving/api/handsfree/ask", json={"text": "thanks that's it", "via": "conversation"}).json()
        self.assertTrue(r["end"])
        self.assertIn("tapped the mic", " ".join(m["content"] for m in seen["messages"] if m["role"] == "system"))
        # Typed Tell Dieter can't be ended (or ignored) by the model.
        r = self.client.post("/thanksgiving/api/command", json={"text": "thanks"}).json()
        self.assertNotIn("end", r)
        self.assertIn('id="voice-live"', self.client.get("/thanksgiving").text)

    def test_fast_pass_answers_simple_questions_alone(self):
        self.client.get("/thanksgiving")
        seen = self.fast_with({"route": "answer", "reply": "Noon on Thursday."})
        r = self.client.post("/thanksgiving/api/command", json={"text": "when does the turkey go in"}).json()
        self.assertEqual("Noon on Thursday.", r["reply"])
        self.assertEqual("fast", r["tier"])
        self.assertEqual(0, seen["deep_calls"])
        self.assertIn("QUICK first answer", seen["fast_messages"][0]["content"])

    def test_fast_pass_hands_changes_to_the_deep_model(self):
        self.client.get("/thanksgiving")
        plan, pie = self.pie()
        self.fast_with({"route": "deep", "say_now": "Let me work out the timing.", "why": "moves the crust"},
                       deep={"reply": "Sunday at 3.", "question": "",
                             "actions": [{"type": "shift", "dish_id": pie["id"], "step_ids": ["dough"], "days": 0,
                                          "minutes": 30}]})
        first = self.client.post("/thanksgiving/api/handsfree/ask",
                                 json={"text": "can we do the crust later", "via": "conversation", "two_step": True}).json()
        self.assertTrue(first["escalate"])
        self.assertEqual("Let me work out the timing.", first["say_now"])
        final = self.client.post("/thanksgiving/api/think", json={"ticket": first["ticket"]}).json()
        self.assertEqual("deep", final["tier"])
        self.assertTrue(final["can_apply"])
        # A change is now waiting, so the next phrase skips the fast pass entirely.
        seen = self.fast_with({"route": "answer", "reply": "should not be used"}, deep={"reply": "Okay, 4 instead.",
                                                                                       "question": "", "actions": []})
        tg_router._state["voice_pending"][plan["id"]] = [{"type": "noop"}]
        r = self.client.post("/thanksgiving/api/command", json={"text": "actually make it 4"}).json()
        self.assertEqual("Okay, 4 instead.", r["reply"])
        self.assertNotIn("fast_messages", seen)
        self.assertIn("waiting for the cook's yes or no", " ".join(m["content"] for m in seen["messages"]))
        # Without two_step the request just waits for the deep answer (older pages keep working).
        self.assertEqual(404, self.client.post("/thanksgiving/api/think", json={"ticket": "nope"}).status_code)

    def test_pushback_and_broken_fast_answers_go_deep(self):
        self.client.get("/thanksgiving")
        seen = self.fast_with({"route": "answer", "reply": "fast"})
        r = self.client.post("/thanksgiving/api/command", json={"text": "are you sure about that"}).json()
        self.assertEqual("deep", r["tier"])
        self.assertNotIn("fast_messages", seen)
        for broken in ({"route": "banana"}, {"route": "answer", "reply": ""}, {"route": "ignore"}):
            self.fast_with(broken)  # a typed request is never ignored
            r = self.client.post("/thanksgiving/api/command", json={"text": "hello there"}).json()
            self.assertEqual("deep", r["tier"], broken)

    def test_open_mic_fast_pass_can_ignore_and_end(self):
        self.client.get("/thanksgiving")
        seen = self.fast_with({"route": "ignore"})
        r = self.client.post("/thanksgiving/api/handsfree/ask", json={"text": "did you see the game", "via": "conversation"}).json()
        self.assertTrue(r["ignored"])
        self.assertEqual(0, seen["deep_calls"])
        self.fast_with({"route": "answer", "reply": "Happy cooking!", "end": True})
        r = self.client.post("/thanksgiving/api/handsfree/ask", json={"text": "thanks that's all", "via": "conversation"}).json()
        self.assertTrue(r["end"])

    def test_hands_free_is_gone_and_hey_dieter_opens_the_chat(self):
        self.client.get("/thanksgiving")
        for url in ("/thanksgiving/cook", "/thanksgiving", "/thanksgiving/timeline"):
            page = self.client.get(url).text
            self.assertNotIn('id="hf-toggle"', page)
            self.assertNotIn('id="hf-bar"', page)
            self.assertIn('id="voice-wake"', page)  # This device → Listen for "Hey Dieter"
            self.assertIn("startWake()", page)

    def test_tell_dieter_listens_first_closes_by_voice_and_flags_unsaved_changes(self):
        self.client.get("/thanksgiving")
        page = self.client.get("/thanksgiving/cook").text
        self.assertIn("mic.click()", page)  # talking is the default; typing is the backup
        self.assertIn('case "close"', page)  # "close", "bye", "thanks, that's all" close the panel
        self.assertIn("Not saved yet. Tap Apply", page)

    def test_full_screen_button_on_every_page_always_shown_in_cook_mode(self):
        self.setup_plan()
        cook = self.client.get("/thanksgiving/cook").text
        self.assertEqual(cook.count('id="fullscreen-btn"'), 1)
        self.assertIn('data-always="yes"', cook)
        for url in ("/thanksgiving", "/thanksgiving/timeline", "/thanksgiving/menu", "/thanksgiving/recipes",
                    "/thanksgiving/shopping"):
            page = self.client.get(url).text
            self.assertIn('id="fullscreen-btn"', page, url)
            self.assertIn('data-always=""', page, url)  # only on the kitchen PC (or when already full screen)

    def test_dieter_knows_recipe_parts_and_the_combined_shopping_list(self):
        # "Whipped cream isn't on the list": it's part of the pumpkin pie, and its cream is on the shopping list.
        plan = self.setup_plan()
        context = tg_router.plan_context(plan)
        summary = voice.plan_summary(plan, context["dishes"], context["tasks"], context["cooks"], context["now"])
        pie = re.split(r"Dish \d+: Pumpkin pie", summary, maxsplit=1)[1].splitlines()[1]
        self.assertIn("recipe parts", pie)
        self.assertIn("Whipped cream", pie)
        shopping = summary.split("Shopping list", 1)[1].splitlines()
        cream = [line for line in shopping if re.match(r"\s*Cream \d", line)]
        self.assertEqual(len(cream), 1, shopping)
        self.assertIn("Pumpkin pie", cream[0])
        self.assertIn("mashed potatoes", cream[0].lower())
        self.assertIn('"Add a dish"', voice.SYSTEM_PROMPT)

    def test_timers_stand_alone_with_a_label(self):
        self.client.get("/thanksgiving")
        page = self.client.get("/thanksgiving/cook").text
        self.assertIn('id="timer-text"', page)
        self.assertIn('id="sleep-btn"', page)  # kitchen screen sleep (helper/ turns the monitor off)
        self.assertIn("http://127.0.0.1:47613", page)
        self.assertNotIn("data-timer=", page)  # no timer buttons on steps or recipe lines
        self.assertTrue(self.client.post("/thanksgiving/api/timers", json={"label": "Potatoes", "minutes": 12}).json()["ok"])
        timers = self.client.get("/thanksgiving/api/state").json()["timers"]
        self.assertEqual([(t["label"], t["dish_id"]) for t in timers], [("Potatoes", None)])

    def test_voice_answers_questions_without_changes(self):
        self.client.get("/thanksgiving")
        self.voice_with({"reply": "Yes: wrapped tight, the dough keeps 3 days in the fridge or a month frozen.",
                         "question": "", "actions": []})
        r = self.client.post("/thanksgiving/api/command", json={"text": "can I make the crust 4 days ahead?"}).json()
        self.assertIn("a month frozen", r["reply"])
        self.assertFalse(r["can_apply"])

    def test_no_cook_names_on_the_pages(self):
        self.client.get("/thanksgiving")
        plan_page = self.client.get("/thanksgiving").text
        self.assertNotIn("Parrisa", plan_page)  # the default plan assigns the turkey step to Parrisa
        self.assertNotIn("Unassigned", plan_page)
        self.assertNotIn('name="cooks"', plan_page)
        self.assertNotIn('name="step_cook"', plan_page)
        # Saving a dish or the settings keeps the hidden assignments instead of wiping them.
        plan = tg_router.store().get_plan()
        turkey = next(d for d in tg_router.store().get_dishes(plan["id"]) if d["category"] == "turkey")
        form = {"name": turkey["name"], "category": turkey["category"], "step_id": [s["id"] for s in turkey["steps"]],
                "step_title": [s["title"] for s in turkey["steps"]], "step_day": ["0"] * len(turkey["steps"]),
                "step_time": ["12:00"] * len(turkey["steps"]), "step_minutes": [str(s["minutes"]) for s in turkey["steps"]],
                "step_kind": [s["kind"] for s in turkey["steps"]], "step_temp": [str(s.get("temp") or "") for s in turkey["steps"]]}
        self.client.post(f"/thanksgiving/dishes/{turkey['id']}/save", data=form)
        saved = tg_router.store().get_dish(plan["id"], turkey["id"])
        self.assertEqual(next(s for s in saved["steps"] if s["id"] == "out")["cook"], "Parrisa")
        self.client.post("/thanksgiving/settings", data={"dinner_date": "2026-11-26", "dinner_time": "16:00", "ovens": "1"})
        self.assertEqual(tg_router.store().get_plan()["cooks"], plan["cooks"])

    def test_voice_button_on_both_pages(self):
        self.client.get("/thanksgiving")
        self.assertIn("Tell Dieter", self.client.get("/thanksgiving").text)
        self.assertIn("Tell Dieter", self.client.get("/thanksgiving/cook").text)

    def test_retired_size_resets_on_restart_and_choices_stay(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        conn = self.db.connect()
        conn.execute("UPDATE tg_dishes SET scale = 0.75 WHERE id = ?", (mac["id"],))  # the removed size
        conn.commit()
        tg_router.configure(self.db)  # restart
        self.assertEqual(self.mac()[1]["scale"], 1.0)
        tg_router.store().set_scale(plan["id"], mac["id"], 0.5)  # you pick Half
        tg_router.configure(self.db)
        self.assertEqual(self.mac()[1]["scale"], 0.5)

    def test_saving_an_edited_recipe_keeps_it_unscaled(self):
        self.client.get("/thanksgiving")
        plan, mac = self.mac()
        form = {"name": mac["name"], "category": mac["category"], "cook": "",
                "ingredients_text": mac["ingredients_text"] + "\n1 cup frozen peas",
                "instructions_text": mac["instructions_text"], "notes": mac["notes"]}
        tg_router.store().set_scale(plan["id"], mac["id"], 0.5)
        self.client.post(f"/thanksgiving/dishes/{mac['id']}/save", data=form)
        saved = self.mac()[1]
        self.assertIn("1 lb elbow macaroni", saved["ingredients_text"])
        self.assertIn("1 cup frozen peas", saved["ingredients_text"])
        self.assertIn("1/2 cup frozen peas", self.dish_page(mac))

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
        pages = self.all_pages()
        self.assertNotIn("<script>alert(1)</script>", pages)
        self.assertIn("&lt;script&gt;", pages)

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
        # "Later:" 10 min / 1 h / 2 h / 4 h; anything silly is held to between a minute and a day
        for minutes, expected in ((240, 240), (10, 10), (100000, 24 * 60), ("soon", 5), (-3, 1)):
            before = int(time.time() * 1000)
            self.client.post("/thanksgiving/api/alerts", json={"key": key, "state": "snooze", "minutes": minutes})
            until = self.client.get("/thanksgiving/api/state").json()["alerts"][key]["until_ms"]
            self.assertAlmostEqual((until - before) / 60_000, expected, delta=0.1, msg=minutes)

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


class StepGuideTests(unittest.TestCase):
    """A step that pops up in cook mode says what to do: the recipe lines it covers, as the recipe says them now."""

    def test_step_how_finds_lines_by_their_start(self):
        from apps.kitchen.thanksgiving import planner
        text = "\n".join(["Crust:", "Roll on a floured counter into a 12-inch circle.", "Ease it into a 9-inch plate.",
                          "Bake at 425°F for 15 minutes."])
        found = planner.step_how(text, ["roll on a floured", "Bake at 425", "Gone line"])
        self.assertEqual([l["text"] for l in found],
                         ["Roll on a floured counter into a 12-inch circle.", "Bake at 425°F for 15 minutes."])
        self.assertEqual([l["ref"] for l in found], ["Crust 1", "Crust 3"])
        self.assertEqual(planner.ref_range(found), "Crust 1, Crust 3")
        self.assertEqual(planner.missing_how(text, ["Roll on", "Gone line"]), ["Gone line"])
        self.assertEqual(planner.step_how(text, None), [])

    def test_guide_fills_in_steps_but_keeps_hand_edits(self):
        from apps.kitchen.thanksgiving import step_guide
        steps = [{"id": "shell", "detail": "Freeze it in the plate, wrapped in plastic and foil."},
                 {"id": "dough", "detail": "My own note", "how": ["Whisk the flour"]},
                 {"id": "mine", "detail": ""}]
        self.assertTrue(step_guide.apply("Pumpkin pie", steps))
        self.assertEqual(steps[0]["how"][0], "Roll on a floured counter")
        self.assertIn("Double batch", steps[0]["detail"])
        self.assertEqual(steps[1]["detail"], "My own note")  # edited by hand: kept
        self.assertEqual(steps[1]["how"], ["Whisk the flour"])
        self.assertNotIn("how", steps[2])
        self.assertFalse(step_guide.apply("Pumpkin pie", steps))  # nothing left to do

    def test_every_menu_recipe_line_is_in_a_step(self):
        """The check that would have caught the pie: every method line of every dish on our menu is covered by a
        timed step, and every line a step names is really in its recipe."""
        from apps.kitchen.thanksgiving import planner, presets, step_guide
        plan = {"dinner_at": "2026-11-26T16:00"}
        problems = []
        for n, key in enumerate(presets.OUR_MENU):
            dish = presets.preset_dish(key)
            if not dish:
                continue
            dish = dict(dish, id=n + 1)
            tasks = planner.build_timeline(plan, [dish])
            covered = planner.coverage(dish, tasks)
            for line in planner.recipe_lines(dish["instructions_text"]):
                if line["index"] not in covered:
                    problems.append(f"{dish['name']} {line['ref']} isn't in any step: {line['text'][:60]}")
            for t in tasks:
                if t["missing_how"]:
                    problems.append(f"{dish['name']} [{t['step_id']}] names lines not in the recipe: {t['missing_how']}")
            for step_id, entry in step_guide.GUIDE.get(dish["name"], {}).items():
                if not entry.get("remove") and step_id not in {s["id"] for s in dish["steps"]}:
                    problems.append(f"{dish['name']}: the guide has no step {step_id}")
            groups = {g["heading"].lower() for g in planner.ingredient_checklist(dish)}
            for t in tasks:
                for use in t["uses"]:
                    if use != "*" and use.lower() not in groups:
                        problems.append(f"{dish['name']} [{t['step_id']}] uses ingredients {use!r}, not in the recipe")
        self.assertEqual(problems, [])

    def test_new_pie_steps_show_their_instructions(self):
        from apps.kitchen.thanksgiving import planner, presets
        pie = dict(presets.preset_dish("pie"), id=1)
        plan = {"dinner_at": "2026-11-26T16:00"}
        tasks = {t["step_id"]: t for t in planner.build_timeline(plan, [pie])}
        self.assertTrue(tasks["shell"]["how"][0]["text"].startswith("Roll on a floured counter"))
        self.assertEqual(tasks["shell"]["how_ref"], "Crust 6–8")
        self.assertEqual(tasks["dough"]["how_ref"], "Crust 1–5")
        self.assertTrue(all(tasks[k]["how"] for k in ("dough", "blind1", "filling", "blind2", "bake1", "bake2", "cream")))


if __name__ == "__main__":
    unittest.main()
