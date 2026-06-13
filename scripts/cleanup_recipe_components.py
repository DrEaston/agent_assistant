"""Clean saved recipe component metadata."""

from __future__ import annotations

import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).resolve().parents[1] / "projects.db"

SOUP_TITLE_TERMS = (
    "soup",
    "stew",
    "chowder",
    "bisque",
    "chili",
    "gumbo",
)


def infer_component_type(title: str, current_type: str) -> str:
    normalized_title = title.lower()
    if any(term in normalized_title for term in SOUP_TITLE_TERMS):
        return "soup"
    return current_type


def main() -> None:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, title, component_type
        FROM recipe_components
        ORDER BY id
        """
    ).fetchall()

    changed = 0
    for row in rows:
        inferred_type = infer_component_type(row["title"] or "", row["component_type"] or "other")
        if inferred_type != row["component_type"]:
            con.execute(
                """
                UPDATE recipe_components
                SET component_type = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (inferred_type, row["id"]),
            )
            changed += 1

    con.commit()
    con.close()
    print(f"Updated {changed} recipe component types.")


if __name__ == "__main__":
    main()
