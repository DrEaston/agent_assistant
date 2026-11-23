"""Changing a Thanksgiving recipe through Dieter: propose, review, save, with history.

1. propose() checks the change and makes a proposal (core/connector/proposals.py). Nothing is saved: the cook sees a
   review card with the old and new lines and what it does to the shopping list.
2. save() applies it only if the recipe hasn't changed since, and records a version (what was asked, before, after).
3. restore() puts back an earlier version, recorded as a version itself so it can be undone too.

Edits are to the recipe as written (full batch); the dish's batch size scales it afterwards.
"""

from core.connector.proposals import ProposalStore, line_diff
from core.connector.tools import ToolError

from . import planner, sizes

FIELDS = (("ingredients_text", "Ingredients"), ("instructions_text", "Instructions"), ("notes", "Notes"))
KIND = "recipe"


def proposals(db):
    return ProposalStore(db, "tg_proposals")


def owner(plan):
    return f"plan:{plan['id']}"


def _shopping(dishes):
    return {item["key"]: item for section in planner.build_grocery_list([sizes.with_size(d) for d in dishes])
            for item in section["items"]}


def shopping_effects(dishes, dish_id, changed):
    """How the shopping list changes, e.g. ["Whole milk: 6 cups → 8 cups", "New: rosemary"]."""
    before = _shopping(dishes)
    after = _shopping([dict(d, **changed) if d["id"] == dish_id else d for d in dishes])
    effects = []
    for key, item in after.items():
        old = before.get(key)
        if not old:
            effects.append(f"New on the list: {item['text']}" + (f" ({item['amount']})" if item.get("amount") else ""))
        elif (old.get("amount") or "") != (item.get("amount") or ""):
            effects.append(f"{item['text']}: {old.get('amount') or '—'} → {item.get('amount') or '—'}")
    for key, item in before.items():
        if key not in after:
            effects.append(f"Off the list: {item['text']}")
    return effects[:12]


def propose(store, db, plan, dish, changes, reason="", request=""):
    """changes: new full text for any of ingredients_text / instructions_text / notes (as written). -> the card."""
    current = store.get_dish(plan["id"], dish["id"])
    if not current:
        raise ToolError("That dish isn't in the plan.")
    new = {}
    for field, _ in FIELDS:
        if changes.get(field) is None:
            continue
        text = "\n".join(line.rstrip() for line in str(changes[field]).strip().splitlines())
        if len(text) > 8000:
            raise ToolError(f"The new {field.split('_')[0]} is too long.")
        if text != (current.get(field) or "").strip():
            new[field] = text
    if not new:
        raise ToolError("That's the recipe as it is now; nothing would change.")
    if "ingredients_text" in new and not new["ingredients_text"].strip() and current.get("ingredients_text"):
        raise ToolError("That would delete every ingredient. Dieter doesn't remove whole recipes.")
    diffs = [{"label": label, "lines": line_diff(current.get(field) or "", new[field])} for field, label in FIELDS
             if field in new]
    effects = shopping_effects(store.get_dishes(plan["id"]), dish["id"], new) if "ingredients_text" in new else []
    card = {"title": f"Change the recipe: {current['name']}", "reason": str(reason or "").strip()[:300],
            "diffs": diffs, "effects": effects,
            "note": "Edits the recipe as written (full batch); the batch size scales it." if current.get("scale", 1) != 1
            else "Edits the recipe as written.",
            "save_url": "/thanksgiving/api/proposals/{id}/save", "discard_url": "/thanksgiving/api/proposals/{id}/discard",
            "open_url": f"/thanksgiving/menu/{current['id']}", "dish_id": current["id"]}
    payload = {"dish_id": current["id"], "request": str(request or "")[:500],
               "before": {field: current.get(field) or "" for field in new}, "after": new}
    return proposals(db).create(owner(plan), KIND, card, payload)


def save(store, db, plan, proposal_id):
    """Apply a pending recipe proposal. Returns the card (status "saved")."""
    book = proposals(db)
    card, payload = book.get(owner(plan), proposal_id)
    if not card or card["kind"] != KIND:
        raise ToolError("That change isn't waiting any more.")
    if card["status"] != "pending":
        raise ToolError(f"That change was already {card['status']}." if card["status"] != "expired"
                        else "That change expired. Ask for it again.")
    dish = store.get_dish(plan["id"], payload["dish_id"])
    if not dish:
        raise ToolError("That dish isn't in the plan any more.")
    if any((dish.get(field) or "") != text for field, text in payload["before"].items()):
        book.decide(owner(plan), proposal_id, "expired")
        raise ToolError("The recipe was changed since this was suggested. Ask again for a fresh suggestion.")
    store.update_dish(plan["id"], dish["id"], dict(dish, **payload["after"]))
    store.add_recipe_version(plan["id"], dish["id"], payload["request"], card.get("reason") or card["title"],
                             payload["before"], payload["after"])
    book.decide(owner(plan), proposal_id, "saved")
    return dict(card, status="saved")


def discard(db, plan, proposal_id):
    book = proposals(db)
    card, _ = book.get(owner(plan), proposal_id)
    if not card:
        raise ToolError("That change isn't waiting any more.")
    if card["status"] == "pending":
        book.decide(owner(plan), proposal_id, "discarded")
    return dict(card, status="discarded" if card["status"] == "pending" else card["status"])


def restore(store, plan, dish_id, version_id):
    """Put the recipe back to how it was before that version's change (recorded as a version too)."""
    version = store.get_recipe_version(plan["id"], version_id)
    dish = store.get_dish(plan["id"], dish_id)
    if not version or not dish or version["dish_id"] != dish_id:
        raise ToolError("That version isn't there.")
    back = {field: text for field, text in version["before"].items()}
    now = {field: dish.get(field) or "" for field in back}
    store.update_dish(plan["id"], dish_id, dict(dish, **back))
    store.add_recipe_version(plan["id"], dish_id, "", f"Restored the recipe from before: {version['summary']}", now, back)
    return back


def pending_cards(db, plan, dish_id=None):
    cards = [card for card, _ in proposals(db).pending(owner(plan), KIND) if card]
    return [c for c in cards if dish_id is None or c.get("dish_id") == dish_id]
