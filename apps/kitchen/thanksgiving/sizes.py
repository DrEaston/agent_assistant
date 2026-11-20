"""Batch sizes for Thanksgiving dishes.

A dish stores its recipe as written plus a scale (0.75 = three-quarters of the recipe). with_size()
returns a copy with ingredient and instruction amounts scaled, the pan swapped, and a note about
bake times, so every view (plan, cook mode, shopping lists) shows the same batch.
"""

import re
from fractions import Fraction

from apps.kitchen import ingredients

# Dish name -> sizes. A size: (scale, label, pan replacing "9x13" in the recipe, note).
DISH_SIZES = {
    "Mac and cheese": (
        (0.5, "Half: serves 3", "8x8", "Bake fresh 15 to 20 minutes. From the fridge: covered 15 minutes, then uncovered about 10."),
        (0.75, "Thanksgiving: 3 + leftovers", "11x7 or 9x9",
         "Bake fresh about 20 minutes. From the fridge: covered 20 minutes, then uncovered 10 to 15."),
        (1.0, "Full: serves 8 to 10", "9x13", ""),
    ),
}
GENERIC_SIZES = (
    (0.5, "Half batch", "", ""),
    (1.0, "As written", "", ""),
    (1.5, "1 1/2 batches", "", ""),
    (2.0, "Double", "", ""),
)
NO_SIZES = {"turkey", "prep"}  # one turkey is one turkey; house prep has no recipe
DEFAULT_SCALES = {"Mac and cheese": 0.75}

_UNITS = r"(?:tbsp|tsp|tablespoons?|teaspoons?|cups?|oz|ounces?|lb|lbs|pounds?|g|grams?|sticks?|cloves?|quarts?|pints?)"
_NUM = r"\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?"
_INSTRUCTION_AMOUNT = re.compile(rf"(?<![\d/x.])({_NUM})(\s*(?:to|-|–)\s*({_NUM}))?(\s+{_UNITS}\b)", re.IGNORECASE)
_LEADING = re.compile(rf"^({_NUM})(\s*(?:to|-|–)\s*({_NUM}))?(?=\s|$)")
_METRIC_NOTE = re.compile(rf"\(\s*({_NUM})\s*(g|grams?|oz|ml)\s*\)", re.IGNORECASE)
_PACKAGE_UNITS = {"package", "bag", "can", "box", "jar", "each"}


def size_options(dish):
    if dish.get("category") in NO_SIZES or not (dish.get("ingredients_text") or "").strip():
        return ()
    return DISH_SIZES.get(dish.get("name"), GENERIC_SIZES)


def _number(text):
    text = text.strip()
    if " " in text:
        whole, frac = text.split(None, 1)
        return int(whole) + Fraction(frac)
    return Fraction(text)


_SPOONS = {"tsp", "tbsp"}
_WHOLE_PACKS = {"package", "bag", "box", "jar"}
_PLURAL_WORDS = {"stick", "cup", "package", "bag", "box", "jar", "can", "clove", "head", "rib", "quart", "pint", "pound"}


def _fmt(value, unit=""):
    value = float(value)
    if value >= 10:
        return str(round(value))
    if unit in _SPOONS:
        # Measuring spoons come in quarters; a three-way tie rounds down (3/8 tsp -> 1/4 tsp).
        quarters = int(value * 4 + 0.5 - 1e-9) or 1
        return ingredients._frac(quarters / 4)
    if unit in _WHOLE_PACKS:
        # You buy whole packages; use at least half of one.
        halves = -(-value * 2 // 1)
        return ingredients._frac(max(halves, 1) / 2)
    return ingredients._frac(value)


def _factor(factor):
    return Fraction(factor).limit_denominator(100)


def _scaled_amount(match, factor, unit):
    """'4 to 6' x 0.75 -> '3 to 4 1/2'."""
    text = _fmt(_number(match.group(1)) * _factor(factor), unit)
    if match.group(3):
        joiner = match.group(2)[: match.group(2).index(match.group(3))]
        text += joiner + _fmt(_number(match.group(3)) * _factor(factor), unit)
    return text


def _fix_plural(amount_text, rest):
    """'2 stick butter' -> '2 sticks butter'; '3/4 sticks' -> '3/4 stick'."""
    match = re.match(r"(\s+)([A-Za-z]+)", rest)
    if not match:
        return rest
    word = match.group(2)
    singular = word[:-1] if word.lower().endswith("s") and word[:-1].lower() in _PLURAL_WORDS else word
    if singular.lower() not in _PLURAL_WORDS:
        return rest
    try:
        many = float(_number(amount_text.split(" to ")[-1])) > 1
    except (ValueError, ZeroDivisionError):
        return rest
    return match.group(1) + (singular + "s" if many else singular) + rest[match.end():]


def _scale_match(match, factor, unit_group=None):
    unit = UNIT_ALIASES.get((match.group(unit_group) or "").strip().lower(), "") if unit_group else ""
    text = _scaled_amount(match, factor, unit)
    if unit_group:
        text += _fix_plural(text, match.group(unit_group))
    return text


UNIT_ALIASES = {alias: unit for alias, unit in ingredients.UNIT_WORDS.items()}


def scale_ingredient(line, factor):
    """'1 stick (113 g) butter' x 0.75 -> '3/4 stick (85 g) butter'. Headings and unmeasured lines stay."""
    if factor == 1 or not line.strip() or line.strip().endswith(":"):
        return line
    stripped = line.strip()
    match = _LEADING.match(stripped)
    if not match:
        return line
    parsed = ingredients.parse_line(stripped)
    unit = parsed["unit"]
    rest = stripped[match.end():]
    value = float(_number(match.group(3) or match.group(1)) * _factor(factor))
    if unit == "lb" and value < 1 and not match.group(3):
        # "3/4 lb pasta" reads better as "12 oz pasta".
        rest = re.sub(r"^\s+(lbs?|pounds?)\b", " oz", rest, count=1, flags=re.IGNORECASE)
        return f"{round(value * 16)}{rest}"
    if unit == "cup" and value < 0.25 and not match.group(3):
        # Small cup amounts are easier to measure with spoons: 1/6 cup -> 2 3/4 tbsp.
        rest = re.sub(r"^\s+(cups?|c)\b", " tbsp", rest, count=1, flags=re.IGNORECASE)
        return f"{_fmt(value * 16, 'tbsp')}{rest}"
    amount = _scaled_amount(match, factor, unit)
    scaled = amount + _fix_plural(amount, rest)
    if unit not in _PACKAGE_UNITS:
        # A weight conversion right after the amount scales too; a package size does not.
        scaled = _METRIC_NOTE.sub(
            lambda m: f"({_fmt(_number(m.group(1)) * _factor(factor))} {m.group(2)})", scaled, count=1)
    return scaled


def scale_instructions(text, factor, pan=""):
    out = []
    for line in (text or "").splitlines():
        if factor != 1 and not line.strip().endswith(":"):
            line = _INSTRUCTION_AMOUNT.sub(lambda m: _scale_match(m, factor, 4), line)
        if pan:
            line = line.replace("9x13", pan)
        out.append(line)
    return "\n".join(out)


def current_size(dish):
    options = size_options(dish)
    scale = float(dish.get("scale") or 1)
    return next((o for o in options if abs(o[0] - scale) < 1e-6), None)


def with_size(dish):
    """A copy of the dish with its chosen batch size applied (the stored recipe is untouched)."""
    sized = dict(dish)
    options = size_options(dish)
    size = current_size(dish)
    scale = size[0] if size else 1.0
    sized["scale"] = scale
    sized["base_ingredients_text"] = dish.get("ingredients_text") or ""
    sized["base_instructions_text"] = dish.get("instructions_text") or ""
    sized["size_options"] = [{"value": _value(o[0]), "label": o[1]} for o in options]
    sized["size_value"] = _value(scale)
    sized["size_label"] = size[1] if size else ""
    sized["size_note"] = size[3] if size else ""
    pan = size[2] if size else ""
    if scale != 1 or pan:
        sized["ingredients_text"] = "\n".join(scale_ingredient(l, scale) for l in sized["base_ingredients_text"].splitlines())
        sized["instructions_text"] = scale_instructions(sized["base_instructions_text"], scale, pan)
    if sized["size_note"]:
        sized["instructions_text"] = sized["instructions_text"].rstrip() + f"\nFor this size: {sized['size_note']}"
    return sized


def _value(scale):
    return f"{scale:g}"


def parse_scale(dish, value):
    """The scale for a submitted dropdown value, if it is one of this dish's sizes."""
    try:
        wanted = float(value)
    except (TypeError, ValueError):
        return None
    return next((o[0] for o in size_options(dish) if abs(o[0] - wanted) < 1e-6), None)
