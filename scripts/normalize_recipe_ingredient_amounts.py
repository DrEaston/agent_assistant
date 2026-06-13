"""Normalize saved recipe component ingredient amounts for grocery display."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).resolve().parents[1] / "projects.db"

PROVENANCE_RE = re.compile(
    r"(source:|card shows|the card|2-serving|4-serving|for 2 servings|for 4 servings|"
    r"using the .*serving amount|estimated as|based on the card)",
    re.IGNORECASE,
)

ONE_COUNT_AMOUNTS = {"", "1", "1.0", "one"}

SAUCE_TABLESPOON_INGREDIENTS = {
    "balsamic vinegar",
    "cherry jam",
    "soy sauce",
    "mustard",
    "dijon mustard",
    "honey",
    "mayonnaise",
    "sour cream",
    "cream cheese",
    "tomato paste",
}


def clean_text(value: object) -> str:
    return str(value or "").strip()


def clean_note(note: str) -> str:
    note = clean_text(note)
    if not note:
        return ""
    if PROVENANCE_RE.search(note):
        return ""
    return note


def normalize_ingredient(ingredient: dict) -> tuple[dict, bool]:
    before = dict(ingredient)
    name = clean_text(ingredient.get("name"))
    amount = clean_text(ingredient.get("amount"))
    unit = clean_text(ingredient.get("unit"))
    preparation = clean_text(ingredient.get("preparation"))
    source_text = clean_text(ingredient.get("source_text"))
    purchase_note = clean_note(clean_text(ingredient.get("purchase_note")))
    confidence = clean_text(ingredient.get("confidence")).lower() or "low"
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    lower_name = name.lower()
    lower_unit = unit.lower()
    combined = " ".join([lower_name, lower_unit, source_text.lower()])

    if "onion" in lower_name and (not unit or lower_unit in {"piece", "pieces"}):
        unit = "onion" if amount in {"", "1", "1.0"} else "onions"

    if "onion" in lower_name and not preparation and "diced" in source_text.lower():
        preparation = "diced"

    if lower_name == "lemon" and not unit:
        unit = "lemon" if amount in ONE_COUNT_AMOUNTS else "lemons"

    if lower_name == "zucchini" and not unit:
        unit = "zucchini"

    if "cannellini" in lower_name and "bean" in lower_name:
        name = "cannellini beans"
        if lower_unit in {"can", "cans"}:
            unit = "can" if amount in {"", "1", "1.0"} else "cans"

    if "stock concentrate" in combined or "stock concentrates" in combined:
        name = "vegetable stock concentrate" if "veggie" in combined or "vegetable" in combined else name
        if not unit or lower_unit in {"concentrate", "concentrates", "portion", "portions", "packet", "packets"}:
            unit = "Tbsp"
            if not amount:
                amount = "1"

    if amount in ONE_COUNT_AMOUNTS and not unit:
        if lower_name in SAUCE_TABLESPOON_INGREDIENTS or any(term in lower_name for term in ("vinegar", "jam", "sauce")):
            unit = "Tbsp"

    normalized = {
        "name": name,
        "amount": amount,
        "unit": unit,
        "preparation": preparation,
        "source_text": source_text,
        "purchase_note": purchase_note,
        "confidence": confidence,
    }
    return normalized, normalized != before


def main() -> None:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, title, structured_ingredients_json
        FROM recipe_components
        WHERE structured_ingredients_json IS NOT NULL
          AND structured_ingredients_json != '[]'
        ORDER BY id
        """
    ).fetchall()

    changed_components = 0
    changed_ingredients = 0
    for row in rows:
        try:
            ingredients = json.loads(row["structured_ingredients_json"] or "[]")
        except json.JSONDecodeError:
            continue

        normalized = []
        row_changed = False
        for ingredient in ingredients:
            if not isinstance(ingredient, dict):
                continue
            clean_ingredient, changed = normalize_ingredient(ingredient)
            normalized.append(clean_ingredient)
            row_changed = row_changed or changed
            changed_ingredients += int(changed)

        if row_changed:
            con.execute(
                """
                UPDATE recipe_components
                SET structured_ingredients_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (json.dumps(normalized), row["id"]),
            )
            changed_components += 1

    con.commit()
    con.close()
    print(f"Normalized {changed_ingredients} ingredients across {changed_components} components.")


if __name__ == "__main__":
    main()
