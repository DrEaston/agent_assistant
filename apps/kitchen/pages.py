"""Kitchen's new-style pages: This week (the meal plan), Recipes (the library) and a recipe.

api.py still owns recipe and meal-plan data, so it loads them and calls these render functions. The older pages
(import, manage, build a meal, recipe details) stay in templates/recipe_*.html until they move too.
"""

import re
from pathlib import Path

from fastapi.responses import HTMLResponse
from markupsafe import Markup, escape

from apps.kitchen.manifest import APP as KITCHEN_APP
from core import ui

_templates = ui.templates(Path(__file__).parent / "templates")
REPORT_URL = "/apps/recipes/grocery-lists/feedback"

TABS = (
    ("This week", "/apps/recipes"),
    ("Recipes", "/apps/recipes/library"),
    ("Shopping", "/apps/recipes/grocery-lists"),
    ("Add", "/apps/recipes/import"),
)

# Filter chips on the recipe library: (label, words that count). A chip shows when 2+ recipes match it.
CHIPS = (
    ("Chicken", ("chicken",)),
    ("Pork", ("pork",)),
    ("Beef", ("beef", "steak", "burger")),
    ("Turkey", ("turkey",)),
    ("Sausage", ("sausage",)),
    ("Fish", ("salmon", "fish", "shrimp", "tilapia", "cod")),
    ("Soups", ("soup", "stew", "chili")),
    ("Pasta", ("pasta", "spaghetti", "penne", "rigatoni", "linguine", "gnocchi", "noodle")),
    ("Bowls", ("bowl",)),
    ("Baking", ("sourdough", "bread", "roll", "cookie", "cake", "pie", "muffin", "biscuit")),
    ("Veggie", ("veggie", "vegetarian", "bean", "chickpea", "lentil", "tofu")),
)


def render(name, **context):
    context.setdefault("app", KITCHEN_APP)
    context.setdefault("report_url", REPORT_URL)
    context.setdefault("tabs", TABS)
    return HTMLResponse(_templates.get_template(name).render(**context))


def chips_for(meals):
    chips = []
    for label, words in CHIPS:
        count = sum(1 for m in meals if any(w in m["search"] for w in words))
        if count >= 2:
            chips.append({"label": label, "words": " ".join(words)})
    return chips


def library_rows(meals):
    """Recipes for lists: id, title, url, and lowercase text to search and filter on."""
    rows = []
    for meal in meals:
        title = meal.get("display_title") or meal.get("title") or "Recipe"
        rows.append({
            "id": meal["id"], "title": title, "url": f"/apps/recipes/meals/{meal['id']}",
            "search": (title + " " + (meal.get("display_ingredients_text") or "")).lower(),
            "is_favorite": bool(meal.get("is_favorite")),
        })
    return rows


# ---------------------------------------------------------------- one recipe

TWO_FOUR = re.compile(r"^(?P<name>.*?)\s*\((?P<two>[^|()]+)\|(?P<four>[^|()]+)\)\s*$")
STEP_NUMBER = re.compile(r"^\s*(?:step\s*)?\d+\s*[.):-]\s*", re.I)
TIME = re.compile(r"\b(\d+(?:\s*(?:-|–|to)\s*\d+)?\s*(?:min(?:ute)?s?|hours?|hrs?|sec(?:ond)?s?))\b", re.I)


def ingredient_rows(lines):
    """Ingredient lines, with 2- and 4-serving amounts split out where the recipe has them ("Rice (3/4 cup | 1 1/2 cups)").

    Returns (rows, pantry, has_servings). Lines with no amount at all in a two-amount recipe are pantry staples.
    """
    rows, pantry = [], []
    cleaned = [re.sub(r"^[-*•]\s*", "", line.strip()) for line in lines if line.strip()]
    has_servings = any(TWO_FOUR.match(line) for line in cleaned)
    for line in cleaned:
        found = TWO_FOUR.match(line)
        if found:
            rows.append({"name": found["name"].strip(), "two": found["two"].strip(), "four": found["four"].strip()})
        elif has_servings and not re.search(r"\d", line):
            pantry.append(line)
        else:
            rows.append({"name": line, "two": "", "four": ""})
    return rows, pantry, has_servings


def step_html(text):
    """One step, escaped, with times in bold so they stand out while cooking."""
    return Markup(TIME.sub(lambda m: f"<strong>{m.group(1)}</strong>", str(escape(text))))


def steps(instructions_text):
    out = []
    for line in (instructions_text or "").splitlines():
        line = STEP_NUMBER.sub("", line.strip())
        if line:
            out.append({"html": step_html(line), "text": line})
    return out


def recipe_page(*, meal, sections, plan_item=None, cooked=False, is_guest=False):
    """sections: [(heading or None, [ingredient lines])]."""
    groups, pantry, has_servings = [], [], False
    for heading, lines in sections:
        rows, staples, servings = ingredient_rows(lines)
        has_servings = has_servings or servings
        pantry += staples
        if rows:
            groups.append({"heading": heading, "rows": rows})
    step_list = steps(meal.get("display_instructions_text"))
    return render(
        "kitchen_recipe.html",
        meal=meal,
        title=meal.get("display_title") or meal.get("title") or "Recipe",
        groups=groups,
        pantry=pantry,
        has_servings=has_servings,
        steps=step_list,
        plan_item=plan_item,
        cooked=cooked,
        is_guest=is_guest,
        page_url=f"/apps/recipes/meals/{meal['id']}",
        current="/apps/recipes/library",
    )


def week_page(*, planned, shopping, recent, thanksgiving=None, is_guest=False, added=False):
    planned_ids = {item.get("source_id") for item in planned if item.get("source_id")}
    return render("kitchen_week.html", planned=planned, shopping=shopping, recent=recent, planned_ids=planned_ids,
                  thanksgiving=thanksgiving, is_guest=is_guest, added=added,
                  page_url="/apps/recipes", current="/apps/recipes")


def library_page(*, meals, planned_ids=(), is_guest=False):
    rows = library_rows(meals)
    return render("kitchen_recipes.html", meals=rows, chips=chips_for(rows), planned_ids=set(planned_ids),
                  is_guest=is_guest, page_url="/apps/recipes/library", current="/apps/recipes/library")
