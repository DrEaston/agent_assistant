"""Build a shopping list from recipes: one line per ingredient, amounts added up, sorted by aisle.

Shared by the Thanksgiving planner and the everyday Kitchen shopping lists. Pure functions.

    build_shopping_list([("Mac and cheese", "1 lb pasta\\n4 tbsp butter"), ...], extras=[{"id": 3, "text": "foil"}])
    -> [{"name": "Dairy & eggs", "items": [{"key": "item-butter", "text": "Unsalted butter", "amount": ...}]}, ...]
"""

import re

from apps.kitchen import ingredients

PANTRY_CHECK = "Check the pantry"

# Checked in this order; the first match wins. Each keyword matches at a word start.
_PANTRY_FIRST = ("tomato paste", "crushed tomato", "diced tomato", "tomato sauce", "breadcrumb", "bread crumb",
                 "panko", "fried onion", "evaporated", "condensed", "puree", "purée", "soup", "peanut butter",
                 "noodle", "pasta", "macaroni", "cavatappi", "broth", "stock", "coconut milk", "oats", "can ")
AISLES = (
    ("Frozen", ("frozen",)),
    ("Pantry & baking", _PANTRY_FIRST),
    ("Sauces & condiments", ("hot sauce", "soy sauce", "ketchup", "mayo", "mustard", "vinegar", "worcestershire",
                             "bbq", "barbecue", r"honey\b", r"jam\b", "salsa", "dressing", "vinaigrette",
                             "sriracha", "pesto", "maple syrup")),
    ("Meat & seafood", ("turkey", "sausage", "bacon", "chicken", "beef", "pork", "steak", r"ham\b", "smokies",
                        "shrimp", "salmon", "fish", r"cod\b", "tilapia", "prosciutto", "lamb", "ground ")),
    ("Dairy & eggs", ("butter", "milk", "cream", r"eggs?\b", "cheese", "cheddar", "gruy", "parmesan", "mozzarella",
                      "feta", "yogurt", "half-and-half", "whipped", "ricotta")),
    ("Bakery & bread", ("bread", r"rolls?\b", r"buns?\b", "tortilla", "loaf", "stuffing", "pie shell", "ciabatta",
                        "baguette", "pita")),
    ("Produce", ("potato", "onion", "celery", "lemon", "lime", "orange", "apple", "cranberr", "green bean", "sprout",
                 "lettuce", "cucumber", "tomato", "thyme", "rosemary", r"sage\b", "parsley", "cilantro", "basil",
                 "dill", "mint", "garlic", "carrot", "herb", "mushroom", "shallot", "scallion", "avocado",
                 "broccoli", "kale", "spinach", "zucchini", "squash", "bell pepper", "jalape", "ginger", "cabbage",
                 "berr", "banana", r"pears?\b", "grape", "arugula", "cauliflower", "asparagus", "leek", "beet")),
    ("Pantry & baking", ("flour", "sugar", "oil", r"rice\b", "beans", "chickpea", "couscous", "oats", "chocolate",
                         "cornstarch", "nuts", "pecan", "walnut", "almond", "cereal", "chips", "crackers")),
)
SECTION_ORDER = ("Produce", "Meat & seafood", "Dairy & eggs", "Bakery & bread", "Pantry & baking",
                 "Sauces & condiments", "Frozen", "Other", PANTRY_CHECK)

# Spices and baking basics most kitchens keep on hand.
_STAPLE_WORDS = ("cinnamon", "nutmeg", "cumin", "oregano", "chili powder", "cayenne", "bay lea", "baking soda",
                 "baking powder", "vanilla", "italian seasoning", "red pepper flakes", "allspice", "ground cloves",
                 "ground ginger", "dried ", "extract", "seasoning", "spice")

_COMPILED = [(name, [re.compile(r"\b" + word) for word in words]) for name, words in AISLES]
_STAPLE_RE = [re.compile(r"\b" + word) for word in _STAPLE_WORDS]


def aisle_for(text):
    lowered = f"{(text or '').lower()} "
    for name, patterns in _COMPILED:
        if any(p.search(lowered) for p in patterns):
            return name
    return "Other"


def is_staple(item_key, text):
    if item_key in ingredients.STAPLES:
        return True
    lowered = (text or "").lower()
    return any(p.search(lowered) for p in _STAPLE_RE)


def normalize_key(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def item_key(text):
    """The stable checkmark key for a free-text item ('2 large onions' -> 'item-onion')."""
    parts = ingredients.split_line(text)
    parsed = ingredients.parse_line(parts[0] if parts else text)
    return f"item-{normalize_key(parsed['item'])}"


def build_shopping_list(sources, extras=()):
    """sources: (source name, ingredient text) pairs. extras: [{"id", "text"}] typed-in items.

    Each item: key, text (name), amount (total to buy), parts (per-source amounts), dishes, section, extra_id.
    """
    found = {}
    for source_name, text in sources:
        for raw in (text or "").splitlines():
            line = raw.strip(" -•*\t")
            if not normalize_key(line) or ingredients_heading(line) or ingredients.is_not_bought(line):
                continue
            for part in ingredients.split_line(line):
                parsed = ingredients.parse_line(part)
                entry = found.setdefault(parsed["item"], {"name": parsed["name"], "lines": [], "by_source": {}})
                entry["lines"].append(parsed)
                entry["by_source"].setdefault(source_name, []).append(parsed)
    merged = {}
    for key, entry in found.items():
        first = entry["lines"][0]
        label = f"{entry['name']} {first['rest'] or first['text']}"
        section = PANTRY_CHECK if is_staple(key, label) else aisle_for(label)
        item = f"item-{normalize_key(key)}"
        merged[item] = {
            "key": item, "text": display_name(key, entry), "extra_id": None, "section": section,
            "amount": ingredients.combine(entry["lines"], buy=True),
            "dishes": list(entry["by_source"]),
            "parts": [{"dish": name, "amount": ingredients.combine(lines)}
                      for name, lines in entry["by_source"].items()],
        }
    for extra in extras:
        key = f"extra-{extra['id']}"
        merged[key] = {"key": key, "text": extra["text"], "dishes": [], "extra_id": extra["id"],
                       "section": aisle_for(extra["text"]), "amount": "", "parts": []}
    sections = []
    for name in SECTION_ORDER:
        items = sorted((i for i in merged.values() if i["section"] == name), key=lambda i: i["text"].lower())
        if items:
            sections.append({"name": name, "items": items})
    return sections


_LEADING_FILLER = re.compile(
    r"^(?:(?:cans?|jars?|a few dashes(?: of)?|a (?:pinch|dash|splash) of|pinch of|dash of"
    r"|(?:very\s+)?(?:cold|chilled|softened|melted|room[- ]temperature|warm|chopped|minced|diced|sliced"
    r"|shredded|grated))\s+)+",
    re.IGNORECASE)
_UNIT_TAIL = re.compile(r"\s+(?:ribs?|stalks?|cloves?|heads?|bunch(?:es)?)$")


def _describe(parsed):
    rest = parsed["rest"].split(",")[0]
    rest = re.split(r"\s+(?:for|to)\s+", rest)[0].strip()
    rest = _LEADING_FILLER.sub("", rest)
    return _UNIT_TAIL.sub("", rest)


def display_name(key, entry):
    """The recipe's own words when every recipe says the same thing ('Yukon Gold potatoes'), else 'Potatoes'."""
    if key in ingredients.BUY_AS or key in ingredients.COUNT_NAMES:
        return entry["name"]
    described = {_describe(p) for p in entry["lines"]}
    if key == "butter" and described and all("unsalted" in d.lower() for d in described):
        return "Unsalted butter"
    if len({d.lower().rstrip("s") for d in described}) == 1:
        text = sorted(described)[0]
        if 2 < len(text) <= 40:
            return text[:1].upper() + text[1:]
    return entry["name"]


def ingredients_heading(line):
    """A recipe section heading is a short line ending in ':' (e.g. 'Crust:')."""
    line = (line or "").strip()
    return line.endswith(":") and len(line) <= 40


def count_items(sections):
    return sum(len(s["items"]) for s in sections)
