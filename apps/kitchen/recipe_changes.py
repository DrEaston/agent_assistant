"""Changing a Kitchen library recipe through Dieter: propose, review, save, with history.

The same steps as Thanksgiving's (apps/kitchen/thanksgiving/recipe_changes.py), on the main app's recipes:
1. propose() makes a proposal (core/connector/proposals.py): a review card with the old and new lines. Nothing saved.
2. save() applies it only if the recipe hasn't changed since, and records it in recipe_change_log (the history the
   recipe page and its details page show).
3. restore() puts back the recipe from before a logged change, logged as a change itself so it can be undone too.

Edits go to the recipe's edited_* text; the imported original is never overwritten.
"""

import json

from core.connector.proposals import ProposalStore, line_diff
from core.connector.tools import ToolError, whole_number
from database import get_current_user_id

FIELDS = (("title", "Title"), ("ingredients_text", "Ingredients"), ("instructions_text", "Instructions"))
KIND = "kitchen_recipe"
MODEL = "dieter-claude"


def proposals(db):
    return ProposalStore(db, "kitchen_proposals")


def owner():
    return f"user:{get_current_user_id() or 0}"


def current(db, recipe_id):
    """The recipe as shown (Dieter's edits over the imported text), or None."""
    row = db.get_recipe_complete_meal(recipe_id)
    if not row:
        return None
    meal = dict(row)
    return {"id": meal["id"],
            "title": meal.get("edited_title") or meal.get("title") or "",
            "ingredients_text": meal.get("edited_ingredients_text") or meal.get("ingredients_text") or "",
            "instructions_text": meal.get("edited_instructions_text") or meal.get("instructions_text") or ""}


def _clean(text):
    return "\n".join(line.rstrip() for line in str(text).strip().splitlines())


def propose(db, recipe_id, changes, reason="", request=""):
    """changes: the new full text of any of title / ingredients_text / instructions_text. -> the card."""
    recipe = current(db, whole_number(recipe_id, "recipe_id"))
    if not recipe:
        raise ToolError(f"There's no recipe {recipe_id} in the library.")
    new = {}
    for field, _ in FIELDS:
        if changes.get(field) is None:
            continue
        text = _clean(changes[field])
        if len(text) > 8000:
            raise ToolError(f"The new {field.split('_')[0]} is too long.")
        if text != recipe[field].strip():
            new[field] = text
    if not new:
        raise ToolError("That's the recipe as it is now; nothing would change.")
    for field in new:
        if not new[field] and recipe[field].strip():
            raise ToolError(f"That would empty the {field.split('_')[0]}. Dieter doesn't delete whole parts of a recipe.")
    card = {"title": f"Change the recipe: {recipe['title']}", "reason": str(reason or "").strip()[:300],
            "diffs": [{"label": label, "lines": line_diff(recipe[field], new[field])} for field, label in FIELDS
                      if field in new],
            "effects": [], "note": "Edits Dieter's copy of the recipe; the imported original is kept.",
            "save_url": "/apps/recipes/api/proposals/{id}/save", "discard_url": "/apps/recipes/api/proposals/{id}/discard",
            "open_url": f"/apps/recipes/meals/{recipe['id']}", "recipe_id": recipe["id"]}
    payload = {"recipe_id": recipe["id"], "request": str(request or "")[:500],
               "before": {field: recipe[field] for field in new}, "after": new}
    return proposals(db).create(owner(), KIND, card, payload)


def _apply(db, recipe_id, values):
    db.update_recipe_complete_meal_edits(recipe_id, **{field: values.get(field) for field, _ in FIELDS})


def save(db, proposal_id):
    """Apply a pending proposal. Returns the card (status "saved")."""
    book = proposals(db)
    card, payload = book.get(owner(), proposal_id)
    if not card or card["kind"] != KIND:
        raise ToolError("That change isn't waiting any more.")
    if card["status"] != "pending":
        raise ToolError(f"That change was already {card['status']}." if card["status"] != "expired"
                        else "That change expired. Ask for it again.")
    recipe = current(db, payload["recipe_id"])
    if not recipe:
        raise ToolError("That recipe isn't in the library any more.")
    if any(recipe[field] != text for field, text in payload["before"].items()):
        book.decide(owner(), proposal_id, "expired")
        raise ToolError("The recipe was changed since this was suggested. Ask again for a fresh suggestion.")
    _apply(db, recipe["id"], payload["after"])
    db.add_recipe_change_log("meal", recipe["id"], payload["request"], card.get("reason") or card["title"],
                             list(payload["after"]), payload["before"], payload["after"], MODEL)
    book.decide(owner(), proposal_id, "saved")
    return dict(card, status="saved")


def discard(db, proposal_id):
    book = proposals(db)
    card, _ = book.get(owner(), proposal_id)
    if not card or card["kind"] != KIND:
        raise ToolError("That change isn't waiting any more.")
    if card["status"] == "pending":
        book.decide(owner(), proposal_id, "discarded")
    return dict(card, status="discarded" if card["status"] == "pending" else card["status"])


def history(db, recipe_id):
    """Logged changes, newest first: id, when, summary, what was asked, fields changed, before and after."""
    out = []
    for row in db.get_recipe_change_log("meal", recipe_id):
        row = dict(row)
        try:
            before, after = json.loads(row.get("before_json") or "{}"), json.loads(row.get("after_json") or "{}")
            fields = json.loads(row.get("changed_fields_json") or "[]")
        except json.JSONDecodeError:
            continue
        if not fields:
            continue  # feedback that changed nothing
        out.append({"id": row["id"], "when": str(row.get("created_at") or "")[:16], "summary": row.get("summary") or "",
                    "request": row.get("user_message") or "", "fields": fields,
                    "before": {f: before.get(f, "") for f in fields}, "after": {f: after.get(f, "") for f in fields},
                    "diffs": [{"label": label, "lines": line_diff(before.get(f, ""), after.get(f, ""))}
                              for f, label in FIELDS if f in fields]})
    return out


def restore(db, recipe_id, change_id):
    """Put the recipe back to how it was before that change (logged as a change too)."""
    change = next((c for c in history(db, recipe_id) if c["id"] == change_id), None)
    recipe = current(db, recipe_id)
    if not change or not recipe:
        raise ToolError("That change isn't in this recipe's history.")
    back = {field: text for field, text in change["before"].items() if field in dict(FIELDS)}
    if not any(back.values()):
        raise ToolError("There's nothing to restore from that change.")
    now = {field: recipe[field] for field in back}
    _apply(db, recipe_id, back)
    db.add_recipe_change_log("meal", recipe_id, "", f"Restored the recipe from before: {change['summary']}",
                             list(back), now, back, MODEL)
    return back


def pending_cards(db, recipe_id=None):
    cards = [card for card, _ in proposals(db).pending(owner(), KIND) if card]
    return [c for c in cards if recipe_id is None or c.get("recipe_id") == recipe_id]
