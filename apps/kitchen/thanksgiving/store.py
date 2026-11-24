"""SQLite storage for the Thanksgiving planner.

Uses the app's shared Database connection and its _commit(), so the Cloud Storage
backup hook still runs. Plans are scoped to the current user; everything else is
scoped to a plan the caller already loaded for that user.
"""

import json
import re

from database import get_current_user_id

# "1 1/2 sticks butter" -> "1 1/2 sticks unsalted butter" (lines that already say salted or unsalted are left alone).
UNSALTED_BUTTER = re.compile(r"^(\s*\d[\d/ ]*\s*(?:tbsp|tablespoons?|sticks?|cups?)\s+)butter\b", re.IGNORECASE)

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
CREATE TABLE IF NOT EXISTS tg_list_links (
    plan_id INTEGER NOT NULL REFERENCES tg_plans(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    list_id INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (plan_id, item_key)
);
CREATE TABLE IF NOT EXISTS tg_recipe_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES tg_plans(id) ON DELETE CASCADE,
    dish_id INTEGER NOT NULL,
    request TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
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

def _minus_minutes(anchor, minutes):
    """"D-2 18:15" minus 10 -> "D-2 18:05" (same day; the fold step starts right after mixing)."""
    import re as _re
    match = _re.fullmatch(r"(D-\d+) (\d\d):(\d\d)", anchor or "")
    if not match:
        return anchor
    total = max(int(match.group(2)) * 60 + int(match.group(3)) - minutes, 0)
    return f"{match.group(1)} {total // 60:02d}:{total % 60:02d}"


CATEGORY_ORDER = ("prep", "appetizer", "turkey", "side", "bread", "dessert", "other")


class ThanksgivingStore:
    def __init__(self, db):
        self.db = db
        self._ready = False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tg_dishes)").fetchall()}
            if "scale" not in columns:
                # Batch size: 0.75 = three-quarters of the recipe as written (see sizes.py).
                conn.execute("ALTER TABLE tg_dishes ADD COLUMN scale REAL DEFAULT 1")
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
        changed = self._add_later_dishes(conn) or changed
        changed = self._update_saved_dishes(conn) or changed
        changed = self._reset_retired_sizes(conn) or changed
        if changed:
            self._commit()

    def _reset_retired_sizes(self, conn):
        """A dish saved at a size that no longer exists (e.g. the old 3/4 mac and cheese) goes back to as written."""
        from .sizes import size_options

        changed = False
        for row in conn.execute("SELECT id, name, category, ingredients_text, scale FROM tg_dishes").fetchall():
            scale = row["scale"] if row["scale"] is not None else 1
            allowed = [o[0] for o in size_options(dict(row))] or [1.0]
            if not any(abs(scale - value) < 1e-6 for value in allowed):
                conn.execute("UPDATE tg_dishes SET scale = 1 WHERE id = ?", (row["id"],))
                changed = True
        return changed

    def _add_later_dishes(self, conn):
        """Add dishes that joined the menu after a plan was made, once per plan (deleting one keeps it gone)."""
        from .presets import LATER_ADDITIONS, preset_dish

        changed = False
        for plan in conn.execute("SELECT id FROM tg_plans").fetchall():
            plan_id = plan["id"]
            for marker, key in LATER_ADDITIONS:
                done = conn.execute("SELECT 1 FROM tg_checks WHERE plan_id = ? AND kind = 'migration' AND item_key = ?",
                                    (plan_id, marker)).fetchone()
                if done:
                    continue
                dish = preset_dish(key)
                exists = conn.execute("SELECT 1 FROM tg_dishes WHERE plan_id = ? AND name = ?",
                                      (plan_id, dish["name"])).fetchone()
                if not exists:
                    conn.execute(
                        """INSERT INTO tg_dishes (plan_id, name, category, cook, ingredients_text, instructions_text,
                                                  notes, steps_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (plan_id, dish["name"], dish["category"], dish.get("cook") or "", dish["ingredients_text"],
                         dish["instructions_text"], dish["notes"], json.dumps(dish["steps"])))
                conn.execute("INSERT INTO tg_checks (plan_id, kind, item_key) VALUES (?, 'migration', ?)",
                             (plan_id, marker))
                changed = True
        return changed

    def _update_saved_dishes(self, conn):
        """One-time changes to dishes already in a plan (each runs once per plan; your later edits stay)."""
        from . import step_guide
        from .presets import WHIPPED_CREAM_INGREDIENTS, WHIPPED_CREAM_INSTRUCTIONS, fold_step, whipped_cream_step

        def once(plan_id, marker):
            done = conn.execute("SELECT 1 FROM tg_checks WHERE plan_id = ? AND kind = 'migration' AND item_key = ?",
                                (plan_id, marker)).fetchone()
            if not done:
                conn.execute("INSERT INTO tg_checks (plan_id, kind, item_key) VALUES (?, 'migration', ?)", (plan_id, marker))
            return not done

        changed = False
        for plan in conn.execute("SELECT id FROM tg_plans").fetchall():
            plan_id = plan["id"]
            if once(plan_id, "mac-by-parrisa"):
                conn.execute("UPDATE tg_dishes SET cook = 'Parrisa' WHERE plan_id = ? AND name = 'Mac and cheese' "
                             "AND COALESCE(cook, '') = ''", (plan_id,))
                changed = True
            if once(plan_id, "pie-whipped-cream"):
                changed = True
                for row in conn.execute("SELECT * FROM tg_dishes WHERE plan_id = ? AND name = 'Pumpkin pie'", (plan_id,)).fetchall():
                    ingredients, instructions = row["ingredients_text"] or "", row["instructions_text"] or ""
                    if "whipped cream:" not in ingredients.lower():
                        if "To serve:\nwhipped cream" in ingredients:
                            ingredients = ingredients.replace("To serve:\nwhipped cream", WHIPPED_CREAM_INGREDIENTS)
                        else:
                            ingredients = ingredients.rstrip() + "\n" + WHIPPED_CREAM_INGREDIENTS
                    if "whipped cream:" not in instructions.lower():
                        instructions = instructions.rstrip().replace(" Serve with whipped cream.", "") + "\n" + WHIPPED_CREAM_INSTRUCTIONS
                    try:
                        steps = json.loads(row["steps_json"] or "[]")
                    except json.JSONDecodeError:
                        steps = []
                    if not any(step.get("id") == "cream" for step in steps):
                        steps.append(whipped_cream_step())
                    conn.execute("UPDATE tg_dishes SET ingredients_text = ?, instructions_text = ?, steps_json = ? WHERE id = ?",
                                 (ingredients, instructions, json.dumps(steps), row["id"]))
            if once(plan_id, "potatoes-half-russet"):
                changed = True
                for row in conn.execute("SELECT * FROM tg_dishes WHERE plan_id = ? AND name = 'Whipped mashed potatoes'",
                                        (plan_id,)).fetchall():
                    ingredients = (row["ingredients_text"] or "").replace(
                        "4 lb Yukon Gold potatoes", "2 lb russet potatoes\n2 lb Yukon Gold potatoes", 1)
                    instructions = (row["instructions_text"] or "").replace(
                        "30 to 60 seconds only.", "30 to 60 seconds only (russets turn gluey if over-beaten).", 1)
                    conn.execute("UPDATE tg_dishes SET ingredients_text = ?, instructions_text = ? WHERE id = ?",
                                 (ingredients, instructions, row["id"]))
            if once(plan_id, "unsalted-butter"):
                # The turkey is pre-brined, so every dish uses unsalted butter.
                changed = True
                for row in conn.execute("SELECT id, ingredients_text FROM tg_dishes WHERE plan_id = ?", (plan_id,)).fetchall():
                    text = row["ingredients_text"] or ""
                    fixed = "\n".join(UNSALTED_BUTTER.sub(r"\1unsalted butter", line) for line in text.split("\n"))
                    if fixed != text:
                        conn.execute("UPDATE tg_dishes SET ingredients_text = ? WHERE id = ?", (fixed, row["id"]))
            if once(plan_id, "mac-full-batch"):
                # Dinner is 5 or 6 adults and a kid: the practice-run half batch (about 4 servings) is too small.
                changed = True
                conn.execute("UPDATE tg_dishes SET scale = 1.0 WHERE plan_id = ? AND name = 'Mac and cheese' AND scale < 1",
                             (plan_id,))
            if once(plan_id, "pie-double"):
                # Two pies, so 5 or 6 adults and a kid get seconds and leftovers.
                changed = True
                for row in conn.execute("SELECT id, ingredients_text FROM tg_dishes WHERE plan_id = ? AND name = 'Pumpkin pie'",
                                        (plan_id,)).fetchall():
                    # Counted in cans, so the list says "2 cans" rather than "2 lb".
                    text = (row["ingredients_text"] or "").replace(
                        "15 oz can pumpkin puree", "1 can (15 oz) pumpkin puree").replace(
                        "12 oz can evaporated milk", "1 can (12 oz) evaporated milk")
                    conn.execute("UPDATE tg_dishes SET ingredients_text = ? WHERE id = ?", (text, row["id"]))
                conn.execute("UPDATE tg_dishes SET scale = 2.0 WHERE plan_id = ? AND name = 'Pumpkin pie' AND scale = 1",
                             (plan_id,))
            if once(plan_id, "sourdough-fold-timers"):
                # Six "Stretch and fold (n of 6)" rows become one step whose Start fold timers sets them all.
                changed = True
                for row in conn.execute("SELECT * FROM tg_dishes WHERE plan_id = ? AND name = 'Sourdough'",
                                        (plan_id,)).fetchall():
                    try:
                        steps = json.loads(row["steps_json"] or "[]")
                    except json.JSONDecodeError:
                        continue
                    folds = [s for s in steps if str(s.get("id", "")).startswith("fold")]
                    if not folds or any(s.get("id") == "folds" for s in steps):
                        continue
                    first = steps.index(folds[0])
                    kept = [s for s in steps if s not in folds]
                    kept.insert(first, fold_step(anchor=folds[0].get("anchor") or "D-2 18:15"))
                    kept[first]["anchor"] = _minus_minutes(folds[0].get("anchor") or "D-2 18:15", 10)
                    instructions = (row["instructions_text"] or "").replace(
                        "Stretch and fold every 15 minutes for 1 hour (4 sets).\nTwo more stretch and folds over the next hour.",
                        "Stretch and folds, 6 sets over 2 hours: every 15 minutes for the first hour, then every 30 minutes. "
                        "In cook mode, Start fold timers chimes for each set.")
                    conn.execute("UPDATE tg_dishes SET steps_json = ?, instructions_text = ? WHERE id = ?",
                                 (json.dumps(kept), instructions, row["id"]))
            if once(plan_id, step_guide.GUIDE_VERSION):
                # Each step says what to do: the recipe lines it covers (step_guide.py). Checkmarks are kept: they're
                # stored by step id, which doesn't change.
                changed = True
                for row in conn.execute("SELECT id, name, steps_json FROM tg_dishes WHERE plan_id = ?", (plan_id,)).fetchall():
                    try:
                        steps = json.loads(row["steps_json"] or "[]")
                    except json.JSONDecodeError:
                        continue
                    if step_guide.apply(row["name"], steps):
                        conn.execute("UPDATE tg_dishes SET steps_json = ? WHERE id = ?", (json.dumps(steps), row["id"]))
        return changed

    def _commit(self):
        self.db._commit()

    # ---------------------------------------------------------------- plans

    def household_plan(self):
        """The plan shown at the public /thanksgiving/menu: the first one made (this site has one household)."""
        row = self._conn().execute("SELECT * FROM tg_plans ORDER BY id LIMIT 1").fetchone()
        return dict(row) if row else None

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
        from .sizes import DEFAULT_SCALES

        name = dish["name"][:120] or "Untitled dish"
        cur = self._conn().execute(
            """INSERT INTO tg_dishes (plan_id, name, category, cook, ingredients_text, instructions_text, notes,
                                      steps_json, source_meal_id, scale)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (plan_id, name, dish.get("category") or "side", dish.get("cook") or "",
             dish.get("ingredients_text") or "", dish.get("instructions_text") or "", dish.get("notes") or "",
             json.dumps(dish.get("steps") or []), dish.get("source_meal_id"),
             dish.get("scale") or DEFAULT_SCALES.get(name, 1)),
        )
        self._commit()
        return cur.lastrowid

    def set_scale(self, plan_id, dish_id, scale):
        self._conn().execute("UPDATE tg_dishes SET scale = ? WHERE id = ? AND plan_id = ?", (scale, dish_id, plan_id))
        self._commit()

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

    # ---------------------------------------------------------------- recipe history (recipe_changes.py)

    def add_recipe_version(self, plan_id, dish_id, request, summary, before, after):
        self._conn().execute(
            "INSERT INTO tg_recipe_versions (plan_id, dish_id, request, summary, before_json, after_json) VALUES (?, ?, ?, ?, ?, ?)",
            (plan_id, dish_id, str(request or "")[:500], str(summary or "")[:300], json.dumps(before), json.dumps(after)))
        self._commit()

    def _version(self, row):
        return dict(row, before=json.loads(row["before_json"]), after=json.loads(row["after_json"]))

    def get_recipe_versions(self, plan_id, dish_id):
        rows = self._conn().execute("SELECT * FROM tg_recipe_versions WHERE plan_id = ? AND dish_id = ? ORDER BY id DESC",
                                    (plan_id, dish_id)).fetchall()
        return [self._version(r) for r in rows]

    def get_recipe_version(self, plan_id, version_id):
        row = self._conn().execute("SELECT * FROM tg_recipe_versions WHERE plan_id = ? AND id = ?",
                                   (plan_id, version_id)).fetchone()
        return self._version(row) if row else None

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
        cur = self._conn().execute("INSERT INTO tg_extra_groceries (plan_id, text) VALUES (?, ?)",
                                   (plan_id, text[:200]))
        self._commit()
        return cur.lastrowid

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

    # ---------------------------------------------------------------- items moved to a Kitchen list

    def get_list_links(self, plan_id):
        """{item key: Kitchen list id} for shopping items moved to a next-trip list (core/portable_list.py)."""
        rows = self._conn().execute("SELECT item_key, list_id FROM tg_list_links WHERE plan_id = ?",
                                    (plan_id,)).fetchall()
        return {row["item_key"]: row["list_id"] for row in rows}

    def set_list_link(self, plan_id, key, list_id):
        self._conn().execute("INSERT OR REPLACE INTO tg_list_links (plan_id, item_key, list_id) VALUES (?, ?, ?)",
                             (plan_id, key, int(list_id)))
        self._commit()

    def delete_list_link(self, plan_id, key):
        self._conn().execute("DELETE FROM tg_list_links WHERE plan_id = ? AND item_key = ?", (plan_id, key))
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
