"""Today (the home page) and Kitchen's new pages: recipe parsing, filters, and that the pages render."""

import unittest
from datetime import date, datetime

from apps.kitchen import pages
from apps.launcher import today


class RecipeParsingTests(unittest.TestCase):
    def test_two_and_four_serving_amounts_split_out(self):
        rows, pantry, has_servings = pages.ingredient_rows([
            "White Rice (3/4 cup | 1 1/2 cups)", "Garlic (2 cloves | 4 cloves)", "Kosher Salt", "Butter", "",
        ])
        self.assertTrue(has_servings)
        self.assertEqual(rows[0], {"name": "White Rice", "two": "3/4 cup", "four": "1 1/2 cups"})
        self.assertEqual(pantry, ["Kosher Salt", "Butter"])

    def test_plain_recipes_keep_their_lines(self):
        rows, pantry, has_servings = pages.ingredient_rows(["- 500 g bread flour", "Salt"])
        self.assertFalse(has_servings)
        self.assertEqual([r["name"] for r in rows], ["500 g bread flour", "Salt"])
        self.assertEqual(pantry, [])

    def test_steps_drop_numbers_and_bold_times(self):
        steps = pages.steps("1. Simmer until tender, 15-20 minutes.\n\n2) Rest <covered> 5 min")
        self.assertEqual(len(steps), 2)
        self.assertEqual(str(steps[0]["html"]), "Simmer until tender, <strong>15-20 minutes</strong>.")
        self.assertIn("&lt;covered&gt;", str(steps[1]["html"]))  # recipe text is escaped
        self.assertIn("<strong>5 min</strong>", str(steps[1]["html"]))

    def test_chips_only_for_common_kinds(self):
        rows = pages.library_rows([
            {"id": 1, "display_title": "Chicken bowls"}, {"id": 2, "display_title": "Chicken soup"},
            {"id": 3, "display_title": "Pork chops"},
        ])
        labels = [c["label"] for c in pages.chips_for(rows)]
        self.assertIn("Chicken", labels)
        self.assertNotIn("Pork", labels)


class PageRenderTests(unittest.TestCase):
    def test_recipe_page_has_servings_switch_and_cook_mode(self):
        meal = {"id": 7, "display_title": "Butter chicken", "display_instructions_text": "1. Cook rice, 15 min"}
        html = pages.recipe_page(meal=meal, sections=[(None, ["Rice (3/4 cup | 1 1/2 cups)"])]).body.decode()
        self.assertIn('data-four="1 1/2 cups"', html)
        self.assertIn('id="cook-start"', html)
        self.assertIn('/apps/recipes/meal-plan/meals/7/add', html)
        self.assertIn('class="dock"', html)

    def test_recipe_on_the_plan_offers_mark_cooked(self):
        meal = {"id": 7, "display_title": "Butter chicken", "display_instructions_text": ""}
        html = pages.recipe_page(meal=meal, sections=[], plan_item={"id": 3}).body.decode()
        self.assertIn("/apps/recipes/meal-plan/3/cooked", html)
        self.assertNotIn('id="cook-start"', html)  # no steps, no cook mode

    def test_week_page_lists_plan_and_shopping(self):
        html = pages.week_page(planned=[{"id": 3, "title": "Soup", "url": "/apps/recipes/meals/9", "source_id": 9}],
                               shopping={"id": 5, "left": 2, "total": 10, "created": "Thu Nov 19"},
                               recent=[{"id": 9, "title": "Soup", "url": "/apps/recipes/meals/9"}]).body.decode()
        self.assertIn("/apps/recipes/meal-plan/3/cooked", html)
        self.assertIn("2 of 10 left", html)
        self.assertIn("On the plan</span>", html)  # recipe 9 is already planned

    def test_today_page(self):
        html = today.render(user_name="Curtis Easton", now=datetime(2026, 11, 22, 15, 0),
                            thanksgiving=today.thanksgiving_card({"dinner_at": "2026-11-26T16:00:00"}, [1, 2],
                                                                 date(2026, 11, 22)),
                            todos=[{"id": 4, "title": "Hardware", "when": ""}]).body.decode()
        self.assertIn("Good afternoon, Curtis", html)
        self.assertIn("4 days to go", html)
        self.assertIn("Thursday · 4 PM", html)
        self.assertIn("/scheduler/4/complete", html)


class TodayHelperTests(unittest.TestCase):
    def test_thanksgiving_card_counts_down_then_disappears(self):
        plan = {"dinner_at": "2026-11-26T16:00:00"}
        self.assertEqual(today.thanksgiving_card(plan, [], date(2026, 11, 25))["countdown"], "Tomorrow")
        self.assertTrue(today.thanksgiving_card(plan, [], date(2026, 11, 26))["is_today"])
        self.assertIsNone(today.thanksgiving_card(plan, [], date(2026, 11, 27)))
        self.assertIsNone(today.thanksgiving_card(None, [], date(2026, 11, 22)))

    def test_greetings(self):
        self.assertEqual(today.greeting(datetime(2026, 1, 1, 8)), "Good morning")
        self.assertEqual(today.greeting(datetime(2026, 1, 1, 18)), "Good evening")


if __name__ == "__main__":
    unittest.main()
