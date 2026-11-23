"""Thanksgiving pages Dieter can open (core/connector/pages.py)."""

from core.connector.pages import Page

PAGES = [
    Page("thanksgiving.now", "Thanksgiving: now", "What's happening now and next, today's checklist.", "/thanksgiving"),
    Page("thanksgiving.timeline", "Timeline",
         "Every step by day. Give day to jump to that day, or dish_id and step_id to jump to one step.",
         "/thanksgiving/timeline", {"day": "date", "dish_id": "integer", "step_id": "slug"},
         ("task-{dish_id}-{step_id}", "day-{day}")),
    Page("thanksgiving.menu", "Menu", "The menu card and every dish.", "/thanksgiving/menu"),
    Page("thanksgiving.dish", "Dish", "One dish: its recipe, batch size, steps and notes.",
         "/thanksgiving/menu/{dish_id}", {"dish_id": "integer"}),
    Page("thanksgiving.recipes", "Recipes", "All the recipes on one page; give dish_id to jump to one.",
         "/thanksgiving/recipes", {"dish_id": "integer"}, ("recipe-{dish_id}",)),
    Page("thanksgiving.shopping", "Thanksgiving shopping list", "The Thanksgiving shopping list.",
         "/thanksgiving/shopping"),
    Page("thanksgiving.finish_trip", "Finish trip", "Sort out what's left after a shopping trip.",
         "/thanksgiving/shopping/finish"),
    Page("thanksgiving.cook", "Cook mode", "Full-screen cook mode for the kitchen.", "/thanksgiving/cook"),
    Page("thanksgiving.settings", "Thanksgiving settings", "Dinner time, cooks and ovens.", "/thanksgiving/settings"),
]
