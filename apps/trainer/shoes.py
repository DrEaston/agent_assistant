"""Shoes and the after-run check-in.

After a new run arrives (from Strava or entered in Dieter), Trainer asks once: which shoes, how it felt (1-10) and
an optional note. One tap on a shoe saves it; the shoe he wore last is first. Shoe mileage is the shoe's starting
miles plus every run logged on it, with a nudge from REPLACE_MILES.

Uses the existing tables (database.py): trainer_shoes, trainer_workout_shoes (one row per run per shoe) and
trainer_run_reflections (how it felt; the chat also saves these). New, additive: tr_run_checkin_skips, runs he
chose not to answer.
"""

from datetime import date, timedelta

from database import get_current_user_id

from . import clock, speed, strava

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS tr_run_checkin_skips (
        user_id INTEGER NOT NULL, workout_id INTEGER NOT NULL, skipped_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, workout_id))""",
)
RUN_TYPES = ("run", "trailrun", "virtualrun")
ASK_DAYS = 10            # runs from the last 10 days get the check-in; older ones are left alone
REPLACE_MILES = 400      # "time for a new pair?" from here
FEELS = {1: "very easy", 3: "easy", 5: "steady", 7: "hard", 9: "very hard", 10: "all out"}


class Invalid(ValueError):
    pass


def _miles(metres):
    return round((metres or 0) / speed.METERS_PER_MILE, 1)


class ShoeStore:
    def __init__(self, db):
        self.db, self._ready = db, False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            for statement in SCHEMA:
                conn.execute(statement)
            have = {r[1] for r in conn.execute("PRAGMA table_info(trainer_imported_workouts)").fetchall()}
            for column, kind in strava.WORKOUT_COLUMNS if have else ():
                if column not in have:   # Strava's own columns (e.g. strava_deleted_at), as strava.Sync adds them
                    conn.execute(f"ALTER TABLE trainer_imported_workouts ADD COLUMN {column} {kind}")
            conn.commit()
            self._ready = True
        return conn

    def _user(self):
        user = get_current_user_id()
        if not user:
            raise Invalid("Sign in first.")
        return user

    def _commit(self):
        self.db._commit()

    # -------------------------------------------------------- shoes

    def shoes(self, include_retired=True):
        rows = self._conn().execute(
            """SELECT s.*, COALESCE(s.initial_miles, 0) + COALESCE(SUM(u.distance_meters), 0) / ? AS miles,
                      COUNT(u.id) AS runs, MAX(w.started_at) AS last_used
               FROM trainer_shoes s
               LEFT JOIN trainer_workout_shoes u ON u.shoe_id = s.id
               LEFT JOIN trainer_imported_workouts w ON w.id = u.imported_workout_id
               WHERE s.user_id = ? GROUP BY s.id
               ORDER BY s.retired, last_used IS NULL, last_used DESC, s.name""",
            (speed.METERS_PER_MILE, self._user())).fetchall()
        out = []
        for r in rows:
            if r["retired"] and not include_retired:
                continue
            out.append({"id": r["id"], "name": r["name"], "brand": r["brand"], "model": r["model"],
                        "miles": round(r["miles"] or 0, 1), "runs": r["runs"], "retired": bool(r["retired"]),
                        "last_used": (r["last_used"] or "")[:10], "start_miles": r["initial_miles"] or 0,
                        "replace": (r["miles"] or 0) >= REPLACE_MILES and not r["retired"]})
        return out

    def add(self, name, brand="", model="", start_miles=0):
        name = " ".join(str(name or "").split())[:60]
        if not name:
            raise Invalid("Give the pair a name, like Pegasus 41 (blue).")
        try:
            start = float(start_miles or 0)
        except ValueError:
            raise Invalid("Starting miles is a number.") from None
        if not 0 <= start <= 2000:
            raise Invalid("Starting miles is between 0 and 2000.")
        conn = self._conn()
        if conn.execute("SELECT 1 FROM trainer_shoes WHERE user_id = ? AND name = ?", (self._user(), name)).fetchone():
            raise Invalid("You already have a pair with that name.")
        cur = conn.execute("INSERT INTO trainer_shoes (user_id, name, brand, model, initial_miles) VALUES (?, ?, ?, ?, ?)",
                           (self._user(), name, " ".join(str(brand or "").split())[:60],
                            " ".join(str(model or "").split())[:60], start))
        self._commit()
        return cur.lastrowid

    def retire(self, shoe_id, retired=True):
        cur = self._conn().execute("UPDATE trainer_shoes SET retired = ?, updated_at = CURRENT_TIMESTAMP "
                                   "WHERE id = ? AND user_id = ?", (1 if retired else 0, shoe_id, self._user()))
        self._commit()
        if not cur.rowcount:
            raise Invalid("That pair isn't yours.")

    # -------------------------------------------------------- the after-run check-in

    def _workout(self, workout_id):
        row = self._conn().execute("SELECT * FROM trainer_imported_workouts WHERE id = ? AND user_id = ?",
                                   (workout_id, self._user())).fetchone()
        if not row:
            raise Invalid("That run isn't in your log.")
        return row

    def workout_id(self, external_id):
        row = self._conn().execute("SELECT id FROM trainer_imported_workouts WHERE source IN ('strava', 'coros') AND external_id = ? "
                                   "AND user_id = ?", (str(external_id), self._user())).fetchone()
        return row["id"] if row else None

    def waiting(self, limit=3, today=None):
        """Recent runs not checked in yet (no shoes, no feel or note saved, not skipped), newest first."""
        since = ((today or clock.today()) - timedelta(days=ASK_DAYS)).isoformat()
        rows = self._conn().execute(
            f"""SELECT w.* FROM trainer_imported_workouts w
                WHERE w.user_id = ? AND lower(w.activity_type) IN ({",".join("?" * len(RUN_TYPES))})
                  AND substr(w.started_at, 1, 10) >= ?
                  AND COALESCE(w.strava_deleted_at, '') = ''
                  AND NOT EXISTS (SELECT 1 FROM trainer_workout_shoes u WHERE u.imported_workout_id = w.id)
                  AND NOT EXISTS (SELECT 1 FROM trainer_run_reflections f WHERE f.imported_workout_id = w.id
                                  AND f.user_id = w.user_id)
                  AND NOT EXISTS (SELECT 1 FROM tr_run_checkin_skips k WHERE k.workout_id = w.id AND k.user_id = w.user_id)
                ORDER BY w.started_at DESC LIMIT ?""",
            (self._user(), *RUN_TYPES, since, limit)).fetchall()
        return [self._run(r) for r in rows]

    def _run(self, w):
        day = (w["started_at"] or "")[:10]
        try:
            label = date.fromisoformat(day).strftime("%a %b %-d")
        except ValueError:
            label = day
        return {"workout_id": w["id"], "external_id": w["external_id"] if w["source"] == "strava" else "",
                "title": w["title"] or "Run", "day": day, "day_label": label, "miles": _miles(w["distance_meters"])}

    def count_waiting(self):
        return len(self.waiting(limit=20))

    def save(self, workout_id, shoe_id=None, feel=None, note=""):
        """Save the check-in: the pair (the whole run on it), how it felt and a note. Saving again replaces it."""
        w = self._workout(workout_id)
        conn = self._conn()
        if shoe_id:
            if not conn.execute("SELECT 1 FROM trainer_shoes WHERE id = ? AND user_id = ?",
                                (shoe_id, self._user())).fetchone():
                raise Invalid("That pair isn't yours.")
            conn.execute("DELETE FROM trainer_workout_shoes WHERE imported_workout_id = ? AND user_id = ?",
                         (workout_id, self._user()))
            conn.execute("INSERT INTO trainer_workout_shoes (imported_workout_id, user_id, shoe_id, distance_meters) "
                         "VALUES (?, ?, ?, ?)", (workout_id, self._user(), shoe_id, w["distance_meters"]))
        note = " ".join(str(note or "").split())[:500]
        rpe = None
        if feel not in (None, ""):
            try:
                rpe = int(feel)
            except ValueError:
                raise Invalid("How it felt is 1 to 10.") from None
            if not 1 <= rpe <= 10:
                raise Invalid("How it felt is 1 to 10.")
        if rpe is not None or note:
            old = conn.execute("SELECT id FROM trainer_run_reflections WHERE imported_workout_id = ? AND user_id = ? "
                               "ORDER BY id DESC LIMIT 1", (workout_id, self._user())).fetchone()
            if old:
                conn.execute("UPDATE trainer_run_reflections SET rpe = COALESCE(?, rpe), notes = CASE WHEN ? != '' "
                             "THEN ? ELSE notes END, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                             (rpe, note, note, old["id"]))
            else:
                conn.execute("INSERT INTO trainer_run_reflections (imported_workout_id, user_id, rpe, notes) "
                             "VALUES (?, ?, ?, ?)", (workout_id, self._user(), rpe, note))
        conn.execute("DELETE FROM tr_run_checkin_skips WHERE workout_id = ? AND user_id = ?", (workout_id, self._user()))
        self._commit()

    def skip(self, workout_id):
        self._workout(workout_id)
        self._conn().execute("INSERT OR IGNORE INTO tr_run_checkin_skips (user_id, workout_id) VALUES (?, ?)",
                             (self._user(), workout_id))
        self._commit()

    def for_run(self, workout_id):
        """{shoe, feel, feel_label, note} saved for a run (empty values when nothing is)."""
        conn = self._conn()
        shoe = conn.execute("SELECT s.id, s.name FROM trainer_workout_shoes u JOIN trainer_shoes s ON s.id = u.shoe_id "
                            "WHERE u.imported_workout_id = ? AND u.user_id = ? ORDER BY u.id DESC LIMIT 1",
                            (workout_id, self._user())).fetchone()
        ref = conn.execute("SELECT rpe, notes FROM trainer_run_reflections WHERE imported_workout_id = ? AND user_id = ? "
                           "ORDER BY id DESC LIMIT 1", (workout_id, self._user())).fetchone()
        rpe = ref["rpe"] if ref else None
        label = ""
        if rpe:
            label = FEELS.get(rpe) or FEELS[max(k for k in FEELS if k <= rpe)]
        return {"shoe_id": shoe["id"] if shoe else None, "shoe": shoe["name"] if shoe else "", "feel": rpe,
                "feel_label": label, "note": (ref["notes"] if ref else "") or ""}
