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
"""

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
