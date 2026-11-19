"""SQLite storage for the Thanksgiving planner.

Uses the app's shared Database connection and its _commit(), so the Cloud Storage
backup hook still runs. Plans are scoped to the current user; everything else is
scoped to a plan the caller already loaded for that user.
"""

import json

from database import get_current_user_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    title TEXT DEFAULT 'Thanksgiving',
    dinner_at TEXT NOT NULL,
    cooks TEXT DEFAULT '',
    ovens INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tg_dishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES tg_plans(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    category TEXT DEFAULT 'side',
    cook TEXT DEFAULT '',
    ingredients_text TEXT DEFAULT '',
    instructions_text TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    steps_json TEXT DEFAULT '[]',
    source_meal_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tg_extra_groceries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES tg_plans(id) ON DELETE CASCADE,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_checks (
    plan_id INTEGER NOT NULL REFERENCES tg_plans(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    item_key TEXT NOT NULL,
    checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plan_id, kind, item_key)
);
CREATE TABLE IF NOT EXISTS tg_timers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES tg_plans(id) ON DELETE CASCADE,
    dish_id INTEGER,
    label TEXT NOT NULL,
    ends_at_ms INTEGER NOT NULL,
    state TEXT DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS tg_alerts (
    plan_id INTEGER NOT NULL REFERENCES tg_plans(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    state TEXT NOT NULL,
    until_ms INTEGER DEFAULT 0,
    PRIMARY KEY (plan_id, item_key)
);
"""

CATEGORY_ORDER = ("prep", "turkey", "side", "bread", "dessert", "other")


class ThanksgivingStore:
    def __init__(self, db):
        self.db = db
        self._ready = False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            conn.executescript(SCHEMA)
            conn.commit()
            self._ready = True
            self._upgrade_saved_dishes(conn)
        return conn

    def _upgrade_saved_dishes(self, conn):
        """Bring never-edited preset dishes up to the current recipe text (e.g. the pie crust)."""
        from .presets import upgrade_saved_dish

        changed = False
        for row in conn.execute("SELECT * FROM tg_dishes").fetchall():
            dish = dict(row)
            try:
                dish["steps"] = json.loads(dish.get("steps_json") or "[]")
            except json.JSONDecodeError:
                continue
            update = upgrade_saved_dish(dish)
            if update:
                conn.execute(
                    "UPDATE tg_dishes SET ingredients_text = ?, instructions_text = ?, steps_json = ? WHERE id = ?",
                    (update["ingredients_text"], update["instructions_text"], json.dumps(update["steps"]), dish["id"]),
                )
                changed = True
        if changed:
            self._commit()

    def _commit(self):
        self.db._commit()

    # ---------------------------------------------------------------- plans

    def get_plan(self):
        row = self._conn().execute(
            "SELECT * FROM tg_plans WHERE user_id IS ? ORDER BY id DESC LIMIT 1", (get_current_user_id(),)
        ).fetchone()
        return dict(row) if row else None

    def create_plan(self, dinner_at, cooks, ovens=1):
        cur = self._conn().execute(
            "INSERT INTO tg_plans (user_id, dinner_at, cooks, ovens) VALUES (?, ?, ?, ?)",
            (get_current_user_id(), dinner_at, cooks, ovens),
        )
        self._commit()
        return cur.lastrowid

    def update_plan(self, plan_id, dinner_at, cooks, ovens):
        self._conn().execute(
            "UPDATE tg_plans SET dinner_at = ?, cooks = ?, ovens = ? WHERE id = ? AND user_id IS ?",
            (dinner_at, cooks, ovens, plan_id, get_current_user_id()),
        )
        self._commit()

    # ---------------------------------------------------------------- dishes

    def get_dishes(self, plan_id):
        rows = self._conn().execute("SELECT * FROM tg_dishes WHERE plan_id = ? ORDER BY id", (plan_id,)).fetchall()
        dishes = []
        for row in rows:
            dish = dict(row)
            try:
                dish["steps"] = json.loads(dish.get("steps_json") or "[]")
            except json.JSONDecodeError:
                dish["steps"] = []
            dishes.append(dish)
        order = {name: i for i, name in enumerate(CATEGORY_ORDER)}
        dishes.sort(key=lambda d: (order.get(d["category"], 99), d["id"]))
        return dishes

    def get_dish(self, plan_id, dish_id):
        return next((d for d in self.get_dishes(plan_id) if d["id"] == dish_id), None)

    def add_dish(self, plan_id, dish):
        cur = self._conn().execute(
            """INSERT INTO tg_dishes (plan_id, name, category, cook, ingredients_text, instructions_text, notes,
                                      steps_json, source_meal_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (plan_id, dish["name"][:120] or "Untitled dish", dish.get("category") or "side", dish.get("cook") or "",
             dish.get("ingredients_text") or "", dish.get("instructions_text") or "", dish.get("notes") or "",
             json.dumps(dish.get("steps") or []), dish.get("source_meal_id")),
        )
        self._commit()
        return cur.lastrowid

    def update_dish(self, plan_id, dish_id, dish):
        self._conn().execute(
            """UPDATE tg_dishes SET name = ?, category = ?, cook = ?, ingredients_text = ?, instructions_text = ?,
                                    notes = ?, steps_json = ?
               WHERE id = ? AND plan_id = ?""",
            (dish["name"][:120] or "Untitled dish", dish.get("category") or "side", dish.get("cook") or "",
             dish.get("ingredients_text") or "", dish.get("instructions_text") or "", dish.get("notes") or "",
             json.dumps(dish.get("steps") or []), dish_id, plan_id),
        )
        self._commit()

    def delete_dish(self, plan_id, dish_id):
        conn = self._conn()
        prefix = f"{int(dish_id)}:%"
        conn.execute("DELETE FROM tg_dishes WHERE id = ? AND plan_id = ?", (dish_id, plan_id))
        conn.execute("DELETE FROM tg_checks WHERE plan_id = ? AND kind = 'step' AND item_key LIKE ?", (plan_id, prefix))
        conn.execute("DELETE FROM tg_alerts WHERE plan_id = ? AND item_key LIKE ?", (plan_id, prefix))
        conn.execute("DELETE FROM tg_timers WHERE plan_id = ? AND dish_id = ?", (plan_id, dish_id))
        self._commit()

    # ---------------------------------------------------------------- groceries

    def get_extra_groceries(self, plan_id):
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM tg_extra_groceries WHERE plan_id = ? ORDER BY id", (plan_id,)).fetchall()]

    def add_extra_grocery(self, plan_id, text):
        self._conn().execute("INSERT INTO tg_extra_groceries (plan_id, text) VALUES (?, ?)", (plan_id, text[:200]))
        self._commit()

    def delete_extra_grocery(self, plan_id, item_id):
        conn = self._conn()
        conn.execute("DELETE FROM tg_extra_groceries WHERE id = ? AND plan_id = ?", (item_id, plan_id))
        conn.execute("DELETE FROM tg_checks WHERE plan_id = ? AND kind = 'grocery' AND item_key = ?",
                     (plan_id, f"extra-{int(item_id)}"))
        self._commit()

    # ---------------------------------------------------------------- checkmarks

    def get_checked(self, plan_id, kind):
        rows = self._conn().execute(
            "SELECT item_key FROM tg_checks WHERE plan_id = ? AND kind = ?", (plan_id, kind)).fetchall()
        return {row["item_key"] for row in rows}

    def set_checked(self, plan_id, kind, key, checked):
        conn = self._conn()
        if checked:
            conn.execute("INSERT OR IGNORE INTO tg_checks (plan_id, kind, item_key) VALUES (?, ?, ?)",
                         (plan_id, kind, key))
        else:
            conn.execute("DELETE FROM tg_checks WHERE plan_id = ? AND kind = ? AND item_key = ?", (plan_id, kind, key))
        self._commit()

    # ---------------------------------------------------------------- timers and alerts (shared across devices)

    def get_timers(self, plan_id):
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM tg_timers WHERE plan_id = ? AND state = 'running' ORDER BY ends_at_ms", (plan_id,)).fetchall()]

    def add_timer(self, plan_id, dish_id, label, ends_at_ms):
        cur = self._conn().execute(
            "INSERT INTO tg_timers (plan_id, dish_id, label, ends_at_ms) VALUES (?, ?, ?, ?)",
            (plan_id, dish_id, label[:120], int(ends_at_ms)))
        self._commit()
        return cur.lastrowid

    def dismiss_timer(self, plan_id, timer_id):
        self._conn().execute("UPDATE tg_timers SET state = 'dismissed' WHERE id = ? AND plan_id = ?",
                             (timer_id, plan_id))
        self._commit()

    def get_alerts(self, plan_id):
        rows = self._conn().execute(
            "SELECT item_key, state, until_ms FROM tg_alerts WHERE plan_id = ?", (plan_id,)).fetchall()
        return {row["item_key"]: {"state": row["state"], "until_ms": row["until_ms"]} for row in rows}

    def set_alert(self, plan_id, key, state, until_ms=0):
        self._conn().execute(
            """INSERT INTO tg_alerts (plan_id, item_key, state, until_ms) VALUES (?, ?, ?, ?)
               ON CONFLICT(plan_id, item_key) DO UPDATE SET state = excluded.state, until_ms = excluded.until_ms""",
            (plan_id, key, state, int(until_ms or 0)))
        self._commit()
