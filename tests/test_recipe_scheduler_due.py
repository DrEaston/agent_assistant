import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import api


def fake_request(path="/apps/recipes", role="member"):
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        query_params={},
        state=SimpleNamespace(current_user=SimpleNamespace(name="Curtis", role=role)),
    )


def fake_recipe_app():
    project = {"id": 1, "name": "Kitchen"}
    return {
        "project": project,
        "import_action": None,
        "import_url": "",
        "groups": [],
        "complete_meals": [],
        "components": [],
        "component_sections": [],
        "meal_plan_items": [],
        "grocery_lists": [],
        "done_grocery_lists": [],
        "available_grocery_options": [],
        "stats": {},
    }


class RecipeSchedulerDueTests(unittest.TestCase):
    def test_recipe_home_renders_coming_up_scheduler_items(self):
        scheduler_due = {
            "due_items": [],
            "upcoming_items": [
                {
                    "id": 42,
                    "title": "to bother parrisa",
                    "context_label": "General",
                    "scheduled_for": "2026-06-17",
                    "scheduler_visual_priority": "week",
                    "is_today": False,
                    "notes": "",
                }
            ],
            "upcoming_priority": "week",
            "today": "2026-06-16",
        }

        with (
            patch.object(api, "get_recipe_app_context", return_value=fake_recipe_app()),
            patch.object(api, "scheduler_due_context", return_value=scheduler_due),
        ):
            response = api.recipe_home_page(fake_request())

        html = response.body.decode("utf-8")
        self.assertIn("Coming Up", html)
        self.assertIn("to bother parrisa", html)
        self.assertIn("scheduler-upcoming-strip", html)
        self.assertIn('action="/scheduler/42/notes/add"', html)
        self.assertIn('placeholder="Add item"', html)

    def test_guest_coming_up_reminder_is_local_only_and_dismissible(self):
        scheduler_due = {
            "due_items": [],
            "upcoming_items": [
                {
                    "id": 77,
                    "title": "Grocery run for cinnamon rolls",
                    "context_label": "Kitchen",
                    "scheduled_for": "2026-07-13",
                    "scheduler_visual_priority": "week",
                    "is_today": False,
                    "notes": "- [ ] Flour\n- [ ] Brown sugar\n- [ ] Cinnamon\n- [ ] Cream cheese",
                }
            ],
            "upcoming_priority": "week",
            "today": "2026-07-11",
        }

        with (
            patch.object(api, "get_recipe_app_context", return_value=fake_recipe_app()),
            patch.object(api, "scheduler_due_context", return_value=scheduler_due),
        ):
            response = api.recipe_home_page(fake_request(role="guest"))

        html = response.body.decode("utf-8")
        self.assertIn("Grocery run for cinnamon rolls", html)
        self.assertIn("scheduler-upcoming-priority-week", html)
        self.assertIn('data-scheduler-reminder-id="77"', html)
        self.assertIn("schedulerDismissed:", html)
        self.assertNotIn('<aside class="scheduler-due-overlay"', html)
        self.assertNotIn('action="/scheduler/77/notes/add"', html)
        self.assertNotIn('action="/scheduler/77/complete"', html)
        self.assertNotIn('action="/scheduler/77/delete"', html)

    def test_almost_due_scheduler_item_is_week_priority_not_due(self):
        item = api.enrich_scheduler_item_priority(
            {"title": "Grocery run for cinnamon rolls", "scheduled_for": "2026-07-13"},
            today=date(2026, 7, 11),
        )

        self.assertFalse(item["is_due"])
        self.assertFalse(item["is_today"])
        self.assertTrue(item["is_this_week"])
        self.assertEqual(item["scheduler_visual_priority"], "week")


if __name__ == "__main__":
    unittest.main()
