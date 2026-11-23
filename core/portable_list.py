"""Portable shopping items: the one format every Dieter app uses to put items on someone else's list.

A planner (Thanksgiving today; a meal planner, a party planner later) owns its own list of things to buy. When
some of those move to a Kitchen shopping list (e.g. "Next trip" at the end of a shopping trip), they go as
portable items, so both apps keep seeing, and can check off, the same item:

    item = {
        "key":    "item-milk",          # the sending app's stable id for the item (its checkmark key)
        "text":   "Whole milk",         # what to buy
        "amount": "1 quart (4 cups)",   # how much, already added up ("" when it doesn't apply)
        "aisle":  "Dairy & eggs",       # store section ("" = let the list decide)
        "for":    ["Mac and cheese"],   # what it's for, shown under the item
    }
    source = {"app": "thanksgiving", "label": "Thanksgiving"}   # who sent it

The Kitchen side of the contract (signed calls from another service, or direct calls inside the main app; see
apps/kitchen/shopping/portable.py):

    POST /internal/kitchen/lists                {"title", "items": [item...], "source": source}  -> {"id", "url"}
    GET  /internal/kitchen/lists/{id}/linked?app=thanksgiving
         -> {"found": bool, "items": {key: {"checked": bool, "list_id": int, "url": str}}}
            (follows an item that was carried on to a newer list; a key that's missing is no longer on any list)
    POST /internal/kitchen/lists/{id}/linked/check   {"app", "key", "done"}
    POST /internal/kitchen/lists/{id}/linked/remove  {"app", "key"}   (the item went back to its own app)

Keys are the sender's own. Ingredient keys ("item-...") are shared by every Dieter list, so the same ingredient
from a recipe and from a planner is one line; any other key is kept apart per app.
"""

LIMITS = {"key": 200, "text": 200, "amount": 120, "aisle": 60, "for": 80, "app": 40, "label": 60}
MAX_ITEMS = 200
MAX_FOR = 12


def _text(value, limit):
    return " ".join(str(value or "").split())[:limit]


def normalize_source(source):
    """{"app", "label"} with safe values. app is a short lowercase id."""
    source = source if isinstance(source, dict) else {}
    app = "".join(ch for ch in _text(source.get("app"), LIMITS["app"]).lower() if ch.isalnum() or ch in "-_")
    label = _text(source.get("label"), LIMITS["label"]) or app.title()
    return {"app": app, "label": label}


def normalize_item(item):
    """One portable item, cleaned; None when it has no key or no text."""
    if not isinstance(item, dict):
        return None
    key, text = _text(item.get("key"), LIMITS["key"]), _text(item.get("text"), LIMITS["text"])
    if not key or not text:
        return None
    raw_for = item.get("for") or []
    if isinstance(raw_for, str):
        raw_for = [raw_for]
    for_ = [t for t in (_text(f, LIMITS["for"]) for f in list(raw_for)[:MAX_FOR]) if t]
    return {"key": key, "text": text, "amount": _text(item.get("amount"), LIMITS["amount"]),
            "aisle": _text(item.get("aisle"), LIMITS["aisle"]), "for": for_}


def normalize_items(items):
    """A list of portable items, cleaned, at most MAX_ITEMS, one per key (the first wins)."""
    cleaned, seen = [], set()
    for raw in list(items or [])[:MAX_ITEMS]:
        item = normalize_item(raw)
        if item and item["key"] not in seen:
            seen.add(item["key"])
            cleaned.append(item)
    return cleaned
