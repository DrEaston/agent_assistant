"""The shareable Thanksgiving menu card: dishes grouped into numbered courses, each with a one-line description.

Course names, order and descriptions live in menu.json next to this file (content, not code). A dish uses the
first entry whose "match" words appear in its name. House prep isn't food, so it never appears. Who makes a
dish (the dish's `cook`) is kept on the dish pages, never on the card.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

from apps.kitchen.ingredients import is_not_bought, parse_line

CONTENT = Path(__file__).parent / "menu.json"
DEFAULT_COURSES = (("bread", "To begin"), ("turkey", "The main course"), ("side", "Alongside"),
                   ("dessert", "To finish"), ("other", "And also"))
NUMERALS = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII")
HOURS = ("twelve", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven")
# Things every recipe has; they don't make a dish sound like anything.
STAPLES = {"salt", "kosher salt", "pepper", "black pepper", "water", "oil", "olive oil", "vegetable oil", "flour",
           "all-purpose flour", "sugar", "butter", "unsalted butter", "egg", "eggs", "ice water", "cooking spray"}


@lru_cache(maxsize=1)
def content():
    try:
        return json.loads(CONTENT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def arrival():
    """The line under the dinner time on the menu card, e.g. "Guests are welcome from three o'clock"."""
    return (content().get("arrival") or "").strip()


def course_list():
    found = [(c["key"], c["name"]) for c in content().get("courses", []) if c.get("key") and c.get("name")]
    return tuple(found) or DEFAULT_COURSES


def entry_for(dish):
    name = (dish.get("name") or "").lower()
    return next((e for e in content().get("dishes", []) if any(word in name for word in e.get("match", []))), {})


def blurb(dish, limit=4):
    """'Sharp cheddar, Gruyère, goat cheese and Little Smokies' from the ingredient list."""
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
        return ""
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return text[0].upper() + text[1:]


def courses(dishes):
    """[{"key", "label", "numeral", "dishes": [{"dish", "blurb"}]}] in serving order, without house prep."""
    known = dict(course_list())
    grouped = {key: [] for key in known}
    for d in dishes:
        if d.get("category") == "prep":
            continue
        entry = entry_for(d)
        key = entry.get("course") or d.get("category")
        key = key if key in known else ("other" if "other" in known else next(iter(known)))
        grouped[key].append({"dish": d, "blurb": entry.get("description") or blurb(d)})
    out = []
    for key, label in course_list():
        if grouped.get(key):
            out.append({"key": key, "label": label, "numeral": NUMERALS[min(len(out), len(NUMERALS) - 1)],
                        "dishes": grouped[key]})
    return out


def address():
    """Where dinner is, shown on the menu card with a map link."""
    return (content().get("address") or "").strip()


def short_time(when):
    """4:00 PM -> "4"; 4:30 PM -> "4:30" (the menu card: "Dinner at 4")."""
    return str(when.hour % 12 or 12) + (f":{when.minute:02d}" if when.minute else "")


def time_in_words(when):
    """4:00 PM -> "Four o'clock"; 4:30 PM -> "Half past four"; other times stay as 4:15 PM."""
    hour = HOURS[when.hour % 12]
    if when.minute == 0:
        return f"{hour.capitalize()} o'clock"
    if when.minute == 30:
        return f"Half past {hour}"
    return when.strftime("%I:%M %p").lstrip("0")
