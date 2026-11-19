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
            {"id": 2, "name": "B", "ingredients_text": "1 Onion\n2 eggs"},
        ]
        sections = planner.build_grocery_list(dishes, [{"id": 7, "text": "paper towels"}])
        items = {i["text"]: i for s in sections for i in s["items"]}
        self.assertEqual(items["1 onion"]["dishes"], ["A", "B"])
        self.assertNotIn("turkey pan drippings", items)
        self.assertNotIn("90 g active starter", items)
        self.assertIn("paper towels", items)

    def test_instruction_timers(self):
        lines = planner.instruction_lines("Bake 25 to 30 minutes.\nRest 1 hour\n\nStir.")
        self.assertEqual([l["timers"] for l in lines], [[30], [60], []])


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

    def test_setup_page_then_plan_page(self):
        r = self.client.get("/thanksgiving")
        self.assertIn("Create the plan", r.text)
        self.assertIn("Curtis, Parrisa", r.text)
        plan = self.setup_plan()
        self.assertEqual(len(tg_router.store().get_dishes(plan["id"])), len(OUR_MENU))
        r = self.client.get("/thanksgiving")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Roast the turkey", r.text)
        self.assertIn("Shopping list", r.text)
        self.assertIn("Yukon Gold", r.text)

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
