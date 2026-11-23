"""Storage for Kitchen shopping lists.

A list is a set of meal-plan items plus typed-in extras. The items to buy are rebuilt from
the meals' current recipes every time the list is shown, so editing a recipe updates the list.
Checkmarks are stored by item key ("item-butter"), so they survive those rebuilds.

Lists made before this design (format 1) kept a frozen items_json snapshot; convert_legacy()
turns those into live lists once, keeping their checkmarks and typed-in items.
"""

import json

from database import get_current_user_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipe_grocery_checks (
    list_id INTEGER NOT NULL REFERENCES recipe_grocery_lists(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (list_id, item_key)
);
CREATE TABLE IF NOT EXISTS recipe_grocery_extras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL REFERENCES recipe_grocery_lists(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS recipe_grocery_links (
    list_id INTEGER NOT NULL REFERENCES recipe_grocery_lists(id) ON DELETE CASCADE,
    source_app TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_label TEXT DEFAULT '',
    item_key TEXT NOT NULL,
    text TEXT NOT NULL,
    amount TEXT DEFAULT '',
    aisle TEXT DEFAULT '',
    for_json TEXT DEFAULT '[]',
    moved_to INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (list_id, source_app, source_key)
);
"""


def linked_item_key(app, source_key):
    """The checkmark key a linked item has on a Kitchen list. Ingredient keys are shared ("item-milk"), so the
    same ingredient from a recipe and from a planner is one line; anything else stays apart per app."""
    return source_key if source_key.startswith("item-") else f"{app}-{source_key}"

LIVE_FORMAT = 2


class ShoppingStore:
    def __init__(self, db):
        self.db = db
        self._ready = False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(recipe_grocery_lists)").fetchall()}
            if "format" not in columns:
                conn.execute("ALTER TABLE recipe_grocery_lists ADD COLUMN format INTEGER DEFAULT 1")
            if "dish_parts_json" not in columns:
                # Thanksgiving dishes (or one part, e.g. the pie crust): [{"dish_id": 7, "part": "Crust"}]
                conn.execute("ALTER TABLE recipe_grocery_lists ADD COLUMN dish_parts_json TEXT DEFAULT '[]'")
            if "done_at" not in columns:
                conn.execute("ALTER TABLE recipe_grocery_lists ADD COLUMN done_at TEXT DEFAULT ''")
            conn.commit()
            self._ready = True
        return conn

    def _commit(self):
        self.db._commit()

    @staticmethod
    def _user():
        return get_current_user_id()

    @staticmethod
    def _prepare(row):
        data = dict(row)
        try:
            data["meal_ids"] = [int(i) for i in json.loads(data.get("meal_plan_item_ids_json") or "[]")]
        except (json.JSONDecodeError, TypeError, ValueError):
            data["meal_ids"] = []
        try:
            data["dish_parts"] = [{"dish_id": int(d["dish_id"]), "part": str(d.get("part") or "")}
                                  for d in json.loads(data.get("dish_parts_json") or "[]")]
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            data["dish_parts"] = []
        return data

    # ---------------------------------------------------------------- lists

    def get_list(self, list_id):
        user = self._user()
        row = self._conn().execute(
            "SELECT * FROM recipe_grocery_lists WHERE id = ? AND (? IS NULL OR user_id = ?)",
            (list_id, user, user)).fetchone()
        return self._prepare(row) if row else None

    def get_lists(self, status, limit=50):
        user = self._user()
        rows = self._conn().execute(
            """SELECT * FROM recipe_grocery_lists WHERE status = ? AND (? IS NULL OR user_id = ?)
               ORDER BY created_at DESC, id DESC LIMIT ?""", (status, user, user, limit)).fetchall()
        return [self._prepare(r) for r in rows]

    def create_list(self, title, meal_ids, dish_parts=()):
        cur = self._conn().execute(
            """INSERT INTO recipe_grocery_lists (title, meal_plan_item_ids_json, items_json, user_id, format,
                                                 dish_parts_json)
               VALUES (?, ?, '[]', ?, ?, ?)""",
            (title[:120], json.dumps([int(i) for i in meal_ids]), self._user(), LIVE_FORMAT,
             json.dumps(list(dish_parts))))
        self._commit()
        return cur.lastrowid

    def set_meals(self, list_id, meal_ids):
        self._conn().execute("UPDATE recipe_grocery_lists SET meal_plan_item_ids_json = ? WHERE id = ?",
                             (json.dumps([int(i) for i in meal_ids]), list_id))
        self._commit()

    def set_dish_parts(self, list_id, dish_parts):
        self._conn().execute("UPDATE recipe_grocery_lists SET dish_parts_json = ? WHERE id = ?",
                             (json.dumps(list(dish_parts)), list_id))
        self._commit()

    def set_status(self, list_id, status):
        self._conn().execute(
            "UPDATE recipe_grocery_lists SET status = ?, done_at = CASE WHEN ? = 'done' THEN CURRENT_TIMESTAMP ELSE '' END "
            "WHERE id = ?", (status, status, list_id))
        self._commit()

    def rename(self, list_id, title):
        self._conn().execute("UPDATE recipe_grocery_lists SET title = ? WHERE id = ?", (title[:120], list_id))
        self._commit()

    def delete_list(self, list_id):
        conn = self._conn()
        conn.execute("DELETE FROM recipe_grocery_checks WHERE list_id = ?", (list_id,))
        conn.execute("DELETE FROM recipe_grocery_extras WHERE list_id = ?", (list_id,))
        conn.execute("DELETE FROM recipe_grocery_links WHERE list_id = ?", (list_id,))
        conn.execute("DELETE FROM recipe_grocery_lists WHERE id = ?", (list_id,))
        self._commit()

    # ---------------------------------------------------------------- checks and extras

    def get_checked(self, list_id):
        rows = self._conn().execute("SELECT item_key FROM recipe_grocery_checks WHERE list_id = ?",
                                    (list_id,)).fetchall()
        return {row[0] for row in rows}

    def set_checked(self, list_id, key, checked):
        conn = self._conn()
        if checked:
            conn.execute("INSERT OR IGNORE INTO recipe_grocery_checks (list_id, item_key) VALUES (?, ?)",
                         (list_id, key[:200]))
        else:
            conn.execute("DELETE FROM recipe_grocery_checks WHERE list_id = ? AND item_key = ?", (list_id, key))
        self._commit()

    def get_extras(self, list_id):
        rows = self._conn().execute("SELECT id, text FROM recipe_grocery_extras WHERE list_id = ? ORDER BY id",
                                    (list_id,)).fetchall()
        return [{"id": row[0], "text": row[1]} for row in rows]

    def add_extra(self, list_id, text):
        cur = self._conn().execute("INSERT INTO recipe_grocery_extras (list_id, text) VALUES (?, ?)",
                                   (list_id, text.strip()[:200]))
        self._commit()
        return cur.lastrowid

    def delete_extra(self, list_id, extra_id):
        conn = self._conn()
        conn.execute("DELETE FROM recipe_grocery_extras WHERE id = ? AND list_id = ?", (extra_id, list_id))
        conn.execute("DELETE FROM recipe_grocery_checks WHERE list_id = ? AND item_key = ?",
                     (list_id, f"extra-{int(extra_id)}"))
        self._commit()

    # ---------------------------------------------------------------- linked items (core/portable_list.py)

    @staticmethod
    def _link(row):
        data = dict(row)
        try:
            data["for"] = [str(f) for f in json.loads(data.get("for_json") or "[]")]
        except (json.JSONDecodeError, TypeError):
            data["for"] = []
        return data

    def add_link(self, list_id, source, item):
        """Put a portable item from another app on this list (replacing that app's earlier copy of it)."""
        self._conn().execute(
            """INSERT OR REPLACE INTO recipe_grocery_links
               (list_id, source_app, source_key, source_label, item_key, text, amount, aisle, for_json, moved_to)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (list_id, source["app"], item["key"], source["label"], linked_item_key(source["app"], item["key"]),
             item["text"], item["amount"], item["aisle"], json.dumps(item["for"])))
        self._commit()

    def get_links(self, list_id, include_moved=False):
        """Linked items on a list. With include_moved, also those carried on to a newer list (moved_to set)."""
        rows = self._conn().execute(
            "SELECT * FROM recipe_grocery_links WHERE list_id = ?" + ("" if include_moved else " AND moved_to IS NULL")
            + " ORDER BY created_at, source_key", (list_id,)).fetchall()
        return [self._link(r) for r in rows]

    def get_link(self, list_id, app, source_key):
        row = self._conn().execute(
            "SELECT * FROM recipe_grocery_links WHERE list_id = ? AND source_app = ? AND source_key = ?",
            (list_id, app, source_key)).fetchone()
        return self._link(row) if row else None

    def move_link(self, list_id, app, source_key, new_list_id):
        """Carry a linked item on to a newer list. The old row remembers where it went, so the app that sent it
        can still follow it."""
        link = self.get_link(list_id, app, source_key)
        if not link:
            return False
        self.add_link(new_list_id, {"app": app, "label": link.get("source_label") or ""},
                      {"key": source_key, "text": link["text"], "amount": link.get("amount") or "",
                       "aisle": link.get("aisle") or "", "for": link["for"]})
        self._conn().execute(
            "UPDATE recipe_grocery_links SET moved_to = ? WHERE list_id = ? AND source_app = ? AND source_key = ?",
            (new_list_id, list_id, app, source_key))
        self._commit()
        return True

    def remove_link(self, list_id, app, source_key):
        cur = self._conn().execute(
            "DELETE FROM recipe_grocery_links WHERE list_id = ? AND source_app = ? AND source_key = ?",
            (list_id, app, source_key))
        self._commit()
        return cur.rowcount > 0

    # ---------------------------------------------------------------- old snapshot lists

    def convert_legacy(self, grocery_list, live_keys, key_for):
        """Turn a format-1 snapshot list into a live list, once.

        Typed-in items become extras. Snapshot items that the live recipes no longer produce
        (e.g. a deleted recipe) also become extras, so nothing disappears. "In cart" items
        become checkmarks.
        """
        if (grocery_list.get("format") or 1) >= LIVE_FORMAT:
            return False
        try:
            items = json.loads(grocery_list.get("items_json") or "[]")
        except json.JSONDecodeError:
            items = []
        conn = self._conn()
        list_id = grocery_list["id"]
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            quantity = " + ".join(q for q in item.get("quantities") or [] if q)
            text = f"{quantity} {item['name']}".strip()
            key = key_for(item["name"])
            if item.get("source_kind") == "manual" or key not in live_keys:
                cur = conn.execute("INSERT INTO recipe_grocery_extras (list_id, text) VALUES (?, ?)",
                                   (list_id, text[:200]))
                key = f"extra-{cur.lastrowid}"
            if item.get("status") == "gotten":
                conn.execute("INSERT OR IGNORE INTO recipe_grocery_checks (list_id, item_key) VALUES (?, ?)",
                             (list_id, key))
        conn.execute("UPDATE recipe_grocery_lists SET format = ? WHERE id = ?", (LIVE_FORMAT, list_id))
        self._commit()
        return True
