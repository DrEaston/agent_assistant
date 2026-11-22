"""Parse recipe ingredient lines and add up amounts for a shopping list.

    parse_line("1 1/2 sticks unsalted butter") -> {"qty": 1.5, "unit": "stick", "item": "butter", ...}
    combine([...parsed...]) -> "6 1/2 sticks (about 1 3/4 lb)"

Pure functions, no web or database imports. Lines that can't be parsed still come
through (with qty None) so nothing silently disappears from a list.
"""

import math
import re
from fractions import Fraction

# ---------------------------------------------------------------- units

UNIT_ALIASES = {
    "tsp": ("tsp", "tsps", "teaspoon", "teaspoons"),
    "tbsp": ("tbsp", "tbsps", "tablespoon", "tablespoons", "tbs", "tbl"),
    "cup": ("cup", "cups", "c"),
    "pint": ("pint", "pints", "pt"),
    "quart": ("quart", "quarts", "qt"),
    "g": ("g", "gram", "grams"),
    "kg": ("kg", "kilogram", "kilograms"),
    "oz": ("oz", "ounce", "ounces"),
    "lb": ("lb", "lbs", "pound", "pounds"),
    "stick": ("stick", "sticks"),
    "clove": ("clove", "cloves"),
    "rib": ("rib", "ribs", "stalk", "stalks"),
    "head": ("head", "heads"),
    "bunch": ("bunch", "bunches"),
    "package": ("package", "packages", "pkg", "packet", "packets"),
    "bag": ("bag", "bags"),
    "can": ("can", "cans"),
    "box": ("box", "boxes"),
    "jar": ("jar", "jars"),
    "slice": ("slice", "slices"),
    "dozen": ("dozen",),
}
UNIT_WORDS = {alias: unit for unit, aliases in UNIT_ALIASES.items() for alias in aliases}

VOLUME_TSP = {"tsp": 1, "tbsp": 3, "cup": 48, "pint": 96, "quart": 192}
WEIGHT_G = {"g": 1, "kg": 1000, "oz": 28.35, "lb": 453.6}
TBSP_PER_STICK = 8
GRAMS_PER_STICK = 113

SIZE_WORDS = {"small", "medium", "large", "extra-large", "jumbo"}

# Canonical items, most specific first. (key, display name, words that identify it)
ITEMS = (
    ("pumpkin-spice", "Pumpkin pie spice", ("pumpkin pie spice",)),
    ("pumpkin", "Pumpkin purée", ("pumpkin",)),
    ("evaporated-milk", "Evaporated milk", ("evaporated milk",)),
    ("whipped-cream", "Whipped cream", ("whipped cream", "whipping cream to serve")),
    ("cream-cheese", "Cream cheese", ("cream cheese",)),
    ("cream", "Cream", ("half-and-half", "heavy cream", "whipping cream")),
    ("milk", "Milk", ("whole milk", "milk")),
    ("cheddar", "Cheddar", ("cheddar",)),
    ("gruyere", "Gruyère", ("gruyère", "gruyere")),
    ("parmesan", "Parmesan", ("parmesan",)),
    ("butter", "Butter", ("butter",)),
    ("egg-noodles", "Egg noodles", ("egg noodle",)),
    ("eggs", "Eggs", ("egg",)),
    ("rice-flour", "Rice flour", ("rice flour",)),
    ("flour", "Flour", ("flour",)),
    ("brown-sugar", "Brown sugar", ("brown sugar",)),
    ("powdered-sugar", "Powdered sugar", ("powdered sugar", "confectioners", "icing sugar")),
    ("sugar", "Sugar", ("sugar",)),
    ("garlic-powder", "Garlic powder", ("garlic powder",)),
    ("garlic", "Garlic", ("garlic",)),
    ("onion-powder", "Onion powder", ("onion powder",)),
    ("fried-onions", "Crispy fried onions", ("fried onion",)),
    ("onion", "Onions", ("onion",)),
    ("celery", "Celery", ("celery",)),
    ("carrot", "Carrots", ("carrot",)),
    ("lemon", "Lemons", ("lemon",)),
    ("cranberries", "Fresh cranberries", ("cranberries",)),
    ("orange-juice", "Orange juice", ("orange juice",)),
    ("orange", "Oranges", ("orange",)),
    ("sweet-potato", "Sweet potatoes", ("sweet potato",)),
    ("russet-potato", "Russet potatoes", ("russet",)),
    ("yukon-potato", "Yukon Gold potatoes", ("yukon",)),
    ("potato", "Potatoes", ("potato",)),
    ("broth", "Broth", ("broth", "stock")),
    ("stuffing", "Stuffing cubes", ("stuffing cubes", "stuffing mix")),
    ("smokies", "Little Smokies", ("smokies",)),
    ("turkey", "Turkey", ("turkey",)),
    ("pasta", "Pasta", ("macaroni", "cavatappi", "pasta", "noodles")),
    ("panko", "Panko", ("panko", "breadcrumbs", "bread crumbs")),
    ("dry-mustard", "Dry mustard", ("dry mustard", "mustard powder")),
    ("paprika", "Paprika", ("paprika",)),
    ("hot-sauce", "Hot sauce", ("hot sauce",)),
    ("salt", "Salt", ("salt",)),
    ("pepper", "Black pepper", ("black pepper", "white pepper", "white or black pepper", "ground pepper",
                                "peppercorn")),
    ("oil", "Oil", ("oil",)),
    ("herbs", "Fresh thyme, rosemary, and sage", ("thyme", "rosemary", "sage")),
)

# Items bought by the package no matter how a recipe measures them.
BUY_AS = {"herbs": "1 small pack each", "salt": "", "pepper": ""}

# Units that can follow the item name ("4 celery ribs").
TRAILING_UNITS = {"rib", "clove", "head", "bunch", "slice"}

# Counted things that are named by size, not unit ("2 large onions").
COUNT_NAMES = {"onion": ("onion", "onions"), "lemon": ("lemon", "lemons"), "orange": ("orange", "oranges"), "carrot": ("carrot", "carrots"),
               "eggs": ("egg", "eggs"), "turkey": ("turkey", "turkeys")}

# Things most kitchens have; listed under "Check the pantry" instead of "buy".
STAPLES = {"salt", "pepper", "sugar", "flour", "oil", "brown-sugar", "garlic-powder", "onion-powder",
           "paprika", "dry-mustard", "hot-sauce", "pumpkin-spice", "rice-flour"}

# Grams per cup, so mixed cup and gram amounts can be added.
GRAMS_PER_CUP = {"flour": 125, "rice-flour": 160}

_UNICODE_FRACTIONS = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅛": "1/8"}
_NUMBER = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)"
_AMOUNT = re.compile(rf"^(?P<a>{_NUMBER})(?:\s*(?:to|-|–)\s*(?P<b>{_NUMBER}))?\s*")


def _number(text):
    text = text.strip()
    if " " in text:
        whole, frac = text.split(None, 1)
        return float(int(whole) + Fraction(frac))
    return float(Fraction(text))


def _clean(line):
    text = line.strip(" -•*\t")
    for char, value in _UNICODE_FRACTIONS.items():
        text = re.sub(rf"(\d){char}", rf"\1 {value}", text).replace(char, value)
    return text


def identify(text):
    """Return (key, display name) for an ingredient description, or a key made from the words."""
    lowered = text.lower()
    for key, name, words in ITEMS:
        if any(re.search(rf"\b{re.escape(word)}", lowered) for word in words):
            return key, name
    words = re.sub(r"[^a-z0-9 ]+", " ", lowered).split()
    key = " ".join(words) or lowered
    return key, text[:1].upper() + text[1:]


def is_not_bought(line):
    """Water, pan drippings, sourdough starter, and recipe-card notes are not things to buy."""
    text = line.lower().strip()
    if "starter" in text or ("drippings" in text and "broth" not in text and "stock" not in text):
        return True
    # A sentence like "Chicken breasts can be substituted for pork chops." is a note, not an item.
    if text.endswith(".") and len(text.split()) > 4 and not _AMOUNT.match(text):
        return True
    return re.search(r"\bwater\b", text) is not None and "chestnut" not in text


# Recipe cards often put the amount last, per serving size: "Potatoes (12 oz | 24 oz)".
# The first amount is the smaller (2-serving) one.
_TRAILING_AMOUNT = re.compile(r"^(?P<name>.*?\S)\s*\(\s*(?P<amount>[^()|]*\d[^()|]*?)\s*(?:\|[^()]*)?\)\s*$")


def _amount_first(text):
    """'Garlic (2 cloves | 4 cloves)' -> '2 cloves Garlic'. Lines that already start with an amount are left alone."""
    if _AMOUNT.match(text):
        return text
    match = _TRAILING_AMOUNT.match(text)
    if match and _AMOUNT.match(match.group("amount")):
        return f"{match.group('amount')} {match.group('name')}"
    return text


def split_line(line):
    """'1/2 onion and 1/2 lemon for the cavity' -> two lines. Other 'and's stay together."""
    parts = re.split(rf"\s+and\s+(?={_NUMBER}\s)", _clean(line))
    return [p for p in parts if p.strip()]


def parse_line(line):
    """Parse one ingredient line. qty is None when there's no amount."""
    text = _amount_first(_clean(line))
    qty = None
    unit = ""
    rest = text
    match = _AMOUNT.match(text)
    if match:
        # For a range ("4 to 6 tbsp") shop for the top of the range.
        qty = _number(match.group("b") or match.group("a"))
        rest = text[match.end():]
        # "1 package (12 to 14 oz) Little Smokies": the parenthetical is a note, not the amount.
        words = rest.split(None, 1)
        first = words[0].lower().rstrip(".,") if words else ""
        if first in UNIT_WORDS:
            unit = UNIT_WORDS[first]
            rest = words[1] if len(words) > 1 else ""
        elif first in SIZE_WORDS:
            rest = words[1] if len(words) > 1 else ""
        if not unit:
            trailing = next((UNIT_WORDS[w] for w in re.findall(r"[a-z]+", rest.lower())
                             if UNIT_WORDS.get(w) in TRAILING_UNITS), "")
            unit = trailing
    rest = re.sub(r"\([^)]*\)", " ", rest)
    rest = re.sub(r"^\s*(?:of\s+)?", "", rest)
    rest = re.sub(r"\s+", " ", rest).strip(" ,")
    key, name = identify(rest or text)
    if qty is not None and not unit and key in COUNT_NAMES:
        unit = "each"
    if qty is not None and not unit and key == "butter":
        unit = "stick"
    return {"text": text, "qty": qty, "unit": unit or ("each" if qty is not None else ""), "item": key,
            "name": name, "rest": rest}


# ---------------------------------------------------------------- adding up

def _frac(value, denominators=(4, 3)):
    """1.5 -> '1 1/2', 0.333 -> '1/3' (nearest quarter or third by default)."""
    best = min((Fraction(round(value * d), d) for d in denominators), key=lambda f: abs(float(f) - value))
    whole, part = divmod(best, 1)
    if part == 0:
        return str(int(whole))
    frac = f"{part.numerator}/{part.denominator}"
    return f"{int(whole)} {frac}" if whole else frac


def _plural(word, qty):
    if word in ("each", "", "oz", "g", "lb", "tsp", "tbsp", "kg") or qty <= 1:
        return word
    if word in ("bunch", "box"):
        return word + "es"
    return word + "s"


def _volume_text(tsp):
    if tsp >= 192 and tsp % 48 == 0:
        quarts = tsp / 192
        return f"{_frac(quarts)} {_plural('quart', quarts)} ({_frac(tsp / 48)} cups)"
    if tsp >= 12:
        cups = tsp / 48
        return f"{_frac(cups)} {_plural('cup', cups)}"
    if tsp >= 3:
        return f"{_frac(tsp / 3)} tbsp"
    return f"{_frac(tsp)} tsp"


def _weight_text(grams, metric=False):
    if metric:
        text = f"{round(grams / 5) * 5} g"
        return text + (f" (about {_frac(grams / WEIGHT_G['lb'])} lb)" if grams >= 400 else "")
    ounces = grams / WEIGHT_G["oz"]
    if ounces >= 16:
        pounds = grams / WEIGHT_G["lb"]
        return f"{_frac(pounds)} lb"
    return f"{_frac(ounces, (2,)) if ounces < 4 else round(ounces)} oz"


def _butter_text(tbsp):
    sticks = tbsp / TBSP_PER_STICK
    if sticks < 1:
        return f"{_frac(tbsp)} tbsp"
    text = f"{_frac(sticks, (2,))} {_plural('stick', sticks)}"
    if sticks >= 4:
        text += f" (about {_frac(sticks / 4)} lb)"
    return text


def _dimension(item, unit):
    if item == "butter" and (unit in VOLUME_TSP or unit in WEIGHT_G or unit == "stick"):
        return "butter"
    if item in GRAMS_PER_CUP and (unit in VOLUME_TSP or unit in WEIGHT_G):
        return "weight"
    if unit in VOLUME_TSP:
        return "volume"
    if unit in WEIGHT_G:
        return "weight"
    return unit or "none"


def _to_base(item, unit, qty):
    if item == "butter":
        if unit == "stick":
            return qty * TBSP_PER_STICK
        if unit in VOLUME_TSP:
            return qty * VOLUME_TSP[unit] / 3
        return qty * WEIGHT_G[unit] / GRAMS_PER_STICK * TBSP_PER_STICK
    if item in GRAMS_PER_CUP and unit in VOLUME_TSP:
        return qty * VOLUME_TSP[unit] / 48 * GRAMS_PER_CUP[item]
    if unit in VOLUME_TSP:
        return qty * VOLUME_TSP[unit]
    if unit in WEIGHT_G:
        return qty * WEIGHT_G[unit]
    if unit == "dozen":
        return qty * 12
    return qty


def combine(parsed, buy=False):
    """A total like '6 1/2 sticks (about 1 3/4 lb)' for parsed lines of the same item.

    Amounts that can't be converted are listed side by side ('2 cups + 1 can');
    lines without an amount add '+ some' (or nothing, if no line had an amount).
    buy=True rounds whole things up (3 1/2 onions -> 4) and uses BUY_AS overrides.
    """
    if buy and parsed and parsed[0]["item"] in BUY_AS:
        return BUY_AS[parsed[0]["item"]]
    totals = {}
    unmeasured = False
    for p in parsed:
        if p["qty"] is None:
            unmeasured = True
            continue
        dim = _dimension(p["item"], "each" if p["unit"] == "dozen" else p["unit"])
        totals[dim] = totals.get(dim, 0) + _to_base(p["item"], p["unit"], p["qty"])
    item = parsed[0]["item"] if parsed else ""
    pieces = []
    for dim, value in totals.items():
        if dim == "butter":
            pieces.append(_butter_text(value))
        elif dim == "volume":
            pieces.append(_volume_text(value))
        elif dim == "weight":
            pieces.append(_weight_text(value, metric=item in GRAMS_PER_CUP))
        elif dim in ("each", "none"):
            pieces.append(str(math.ceil(value - 1e-9)) if buy else _frac(value))
        else:
            pieces.append(f"{_frac(value)} {_plural(dim, value)}")
    if unmeasured and pieces:
        pieces.append("some")
    return " + ".join(pieces)

