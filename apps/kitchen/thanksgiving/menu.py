"""The shareable Thanksgiving menu: dishes grouped into courses, each with a photo and a one-line description.

Photos, descriptions and credits live in static/photos/menu.json (content, not code): a dish uses the first
entry whose "match" words appear in its name. House prep isn't food, so it never appears on the menu.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

from apps.kitchen.ingredients import is_not_bought, parse_line

PHOTO_DIR = Path(__file__).parent / "static" / "photos"
COURSES = (("turkey", "The main event"), ("side", "On the side"), ("bread", "From the oven"),
           ("dessert", "Dessert"), ("other", "And also"))
# Things every recipe has; they don't make a dish sound like anything.
STAPLES = {"salt", "kosher salt", "pepper", "black pepper", "water", "oil", "olive oil", "vegetable oil", "flour",
           "all-purpose flour", "sugar", "butter", "unsalted butter", "egg", "eggs", "ice water", "cooking spray"}


@lru_cache(maxsize=1)
def photos():
    try:
        return json.loads((PHOTO_DIR / "menu.json").read_text(encoding="utf-8"))["dishes"]
    except (OSError, ValueError, KeyError):
        return []


def photo_for(dish):
    name = (dish.get("name") or "").lower()
    return next((p for p in photos() if any(word in name for word in p.get("match", []))), None)


def photo_file(name):
    """The photo file for /thanksgiving/photos/<name>, only if it's one listed in menu.json."""
    if name in {p["file"] for p in photos()}:
        path = PHOTO_DIR / name
        return path if path.is_file() else None
    return None


def blurb(dish, limit=4):
    """'sharp cheddar, Gruyère, goat cheese and Little Smokies' from the ingredient list."""
    seen, parts = set(), []
    for line in (dish.get("ingredients_text") or "").splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if not line or line.endswith(":") or is_not_bought(line):
            continue
        name = parse_line(line)["rest"].split(",")[0]
        name = re.sub(r"\b(or|for|to|plus)\b.*$", "", name).strip(" .;")
        name = re.sub(r"^(large|small|medium|fresh|whole|dry|dried|ground|chopped|minced|shredded|grated)\s+", "", name, flags=re.I)
        key = name.lower()
        if len(name) < 3 or key in STAPLES or key in seen or len(name) > 40:
            continue
        seen.add(key)
        parts.append(name)
        if len(parts) == limit:
            break
    if not parts:
        return dish.get("notes") or ""
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return text[0].upper() + text[1:]


def courses(dishes):
    """[{"key", "label", "dishes": [{"dish", "photo", "blurb"}]}] in serving order, without house prep."""
    out = []
    food = [d for d in dishes if d.get("category") != "prep"]
    for key, label in COURSES:
        items = []
        for d in food:
            if (d.get("category") if d.get("category") in dict(COURSES) else "other") != key:
                continue
            photo = photo_for(d)
            items.append({"dish": d, "photo": photo,
                          "blurb": (photo or {}).get("description") or d.get("notes") or blurb(d)})
        if items:
            out.append({"key": key, "label": label, "dishes": items})
    return out


def credits(course_list):
    used = {}
    for course in course_list:
        for item in course["dishes"]:
            if item["photo"]:
                used[item["photo"]["file"]] = item["photo"]
    return list(used.values())
