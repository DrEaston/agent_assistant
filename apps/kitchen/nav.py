"""Kitchen pages Dieter can open (core/connector/pages.py)."""

from core.connector.pages import Page

PAGES = [
    Page("kitchen.home", "Kitchen", "Kitchen home: this week's meals and the current shopping list.", "/apps/recipes"),
    Page("kitchen.library", "Recipe library", "Every saved recipe.", "/apps/recipes/library"),
    Page("kitchen.recipe", "Recipe", "One recipe from the library (ids from kitchen_search_recipes).",
         "/apps/recipes/meals/{recipe_id}", {"recipe_id": "integer"}),
    Page("kitchen.shopping_lists", "Shopping lists", "All Kitchen shopping lists.", "/apps/recipes/grocery-lists"),
    Page("kitchen.shopping_list", "Shopping list", "One Kitchen shopping list (ids from kitchen_list_shopping_lists).",
         "/apps/recipes/grocery-lists/{list_id}", {"list_id": "integer"}),
]
