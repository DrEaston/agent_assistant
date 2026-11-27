"""Trainer's plan: the week ahead (docs/trainer_plan.md).

What's stored (additive tables, created on first use):
- tr_settings: his usual week (miles target, hard-workout day and time, weights and Pilates per week, strides per
  week) and the secret for his calendar feed.
- tr_weeks: one row per week (Monday date): miles target, goals for the week, the Sunday review.
- tr_sessions: one row per planned session: day, time, kind, miles, details, strides, status.
- tr_habits: one row per day: glasses of water, fueled for the key session, a note.

Strava runs come from the older trainer_imported_workouts table (filled by the Strava import), read-only here.
Everything is per signed-in user and everything is editable: the draft week is a starting point, not a rule.
"""

import json
import secrets
from datetime import date, datetime, time, timedelta

from database import get_current_user_id
from . import clock

KINDS = {
    "easy": "Easy run",
    "workout": "Hard workout",
    "long": "Long run",
    "race": "Race",
    "weights": "Weights",
    "pilates": "Pilates",
    "mobility": "Mobility",
    "rest": "Rest",
}
RUN_KINDS = ("easy", "workout", "long", "race")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = {d: n for d, n in zip(DAYS, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))}
STATUSES = ("planned", "done", "skipped")

DEFAULTS = {
    "miles": 40.0,           # target miles per week
    "workout_day": "tue",    # hard workout
    "workout_time": "16:00",
    "long_day": "sat",
    "rest_day": "",          # none by default: easy days fill the week
    "weights": 2,            # sessions per week (1-2)
    "pilates": 2,            # sessions per week (1-2)
    "strides": 2,            # easy runs with strides per week
    "strides_text": "6 × 20 s relaxed fast, full recovery",
    "water_goal": 8,         # glasses a day
    "run_time": "06:30",     # default calendar slot for runs
    "gym_time": "17:30",     # default slot for weights and Pilates
    # Recent race or time-trial times ("m:ss"): his real maximal efforts, for critical speed (speed.py).
    "race_mile": "", "race_3k": "", "race_5k": "", "race_10k": "",
}
RACES = {"race_mile": 1609, "race_3k": 3000, "race_5k": 5000, "race_10k": 10000}


def race_seconds(text):
    """"5:18" / "17:39" / "1:02:30" -> seconds, or None."""
    parts = str(text or "").strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if not 2 <= len(nums) <= 3 or any(n < 0 for n in nums):
        return None
    seconds = sum(n * 60 ** i for i, n in enumerate(reversed(nums)))
    return seconds if 60 <= seconds <= 4 * 3600 else None


def race_bests(settings):
    """{metres (str): seconds} from his entered race times."""
    return {str(d): race_seconds(settings.get(key)) for key, d in RACES.items() if race_seconds(settings.get(key))}

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS tr_settings (
        user_id INTEGER PRIMARY KEY, settings_json TEXT DEFAULT '{}', calendar_token TEXT DEFAULT '',
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS tr_weeks (
        user_id INTEGER NOT NULL, week_start TEXT NOT NULL, miles_target REAL, goals_json TEXT DEFAULT '[]',
        review TEXT DEFAULT '', checked_in_at TEXT DEFAULT '', PRIMARY KEY (user_id, week_start))""",
    """CREATE TABLE IF NOT EXISTS tr_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, day TEXT NOT NULL, slot TEXT DEFAULT '',
        kind TEXT NOT NULL, title TEXT DEFAULT '', miles REAL, details TEXT DEFAULT '', strides INTEGER DEFAULT 0,
        strides_done INTEGER DEFAULT 0, status TEXT DEFAULT 'planned', note TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE INDEX IF NOT EXISTS tr_sessions_day ON tr_sessions (user_id, day)",
    """CREATE TABLE IF NOT EXISTS tr_habits (
        user_id INTEGER NOT NULL, day TEXT NOT NULL, water INTEGER DEFAULT 0, fueled INTEGER DEFAULT 0,
        note TEXT DEFAULT '', PRIMARY KEY (user_id, day))""",
)

METERS_PER_MILE = 1609.344


class Invalid(ValueError):
    pass


def week_start(day=None):
    """The Monday of the week `day` (a date or ISO string) falls in."""
    if isinstance(day, str):
        day = date.fromisoformat(day[:10])
    day = day or clock.today()
    return day - timedelta(days=day.weekday())


def next_week_start(today=None):
    """The week a Sunday check-in plans: next Monday (on any day, the coming Monday)."""
    today = today or clock.today()
    return week_start(today) + timedelta(days=7)


def _day_of(start, code):
    return start + timedelta(days=DAYS.index(code))


def _num(value, default=None, low=None, high=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def _clock(value, default=""):
    text = str(value or "").strip()
    try:
        return time.fromisoformat(text).strftime("%H:%M") if text else default
    except ValueError:
        return default


def draft_week(settings, start):
    """A starting week from his usual pattern: [{day, slot, kind, title, miles, details, strides}].

    Runs: the hard workout on its day (about 8 mi with warm-up and cool-down), a long run of about a quarter of the
    week, easy runs on the other days sharing what's left (rounded to half miles), strides on the first easy runs.
    Weights and Pilates go on easy days, spread out, never on the hard-workout day.
    """
    s = dict(DEFAULTS, **(settings or {}))
    miles = float(s["miles"])
    workout_day, long_day = s["workout_day"], s["long_day"]
    rest = s.get("rest_day") or ""
    easy_days = [d for d in DAYS if d not in (workout_day, long_day, rest)]
    workout_miles = min(8.0, round(miles * 0.2 * 2) / 2)
    long_miles = round(miles * 0.25 * 2) / 2
    easy_total = max(0.0, miles - workout_miles - long_miles)
    each = round((easy_total / len(easy_days)) * 2) / 2 if easy_days else 0
    out = []
    for code in DAYS:
        day = _day_of(start, code).isoformat()
        if code == workout_day:
            out.append({"day": day, "slot": s["workout_time"], "kind": "workout", "title": "Hard workout",
                        "miles": workout_miles, "details": "Warm up, main set, cool down.", "strides": 0})
        elif code == long_day:
            out.append({"day": day, "slot": s["run_time"], "kind": "long", "title": "Long run", "miles": long_miles,
                        "details": "Easy effort. Fuel and drink on the way if it's over 75 minutes.", "strides": 0})
        elif code == rest:
            out.append({"day": day, "slot": "", "kind": "rest", "title": "Rest", "miles": 0, "details": "",
                        "strides": 0})
        else:
            out.append({"day": day, "slot": s["run_time"], "kind": "easy", "title": "Easy run", "miles": each,
                        "details": "", "strides": 0})
    # Strides on the first easy runs, not the day before the hard workout.
    before_workout = DAYS[(DAYS.index(workout_day) - 1) % 7]
    stride_days = [d for d in easy_days if d != before_workout][: int(s["strides"])]
    for session in out:
        code = DAYS[date.fromisoformat(session["day"]).weekday()]
        if session["kind"] == "easy" and code in stride_days:
            session["strides"] = 6
            session["details"] = s["strides_text"]
    # Weights and Pilates on easy days, spread through the week, not on the hard-workout day or the long-run day.
    gym_days = [d for d in easy_days if d != before_workout] or easy_days
    picks = gym_days[::2] + gym_days[1::2]
    for kind, count, label in (("weights", int(s["weights"]), "Weights"), ("pilates", int(s["pilates"]), "Pilates")):
        for code in picks[:count]:
            out.append({"day": _day_of(start, code).isoformat(), "slot": s["gym_time"], "kind": kind, "title": label,
                        "miles": None, "details": "", "strides": 0})
        picks = picks[count:] + picks[:count]
    return sorted(out, key=lambda x: (x["day"], x["slot"] or "99", x["kind"] not in RUN_KINDS))


class PlanStore:
    def __init__(self, db):
        self.db, self._ready = db, False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            for statement in SCHEMA:
                conn.execute(statement)
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

    # ------------------------------------------------------------ settings

    def settings(self):
        row = self._conn().execute("SELECT settings_json FROM tr_settings WHERE user_id = ?", (self._user(),)).fetchone()
        try:
            saved = json.loads(row["settings_json"]) if row else {}
        except (TypeError, json.JSONDecodeError):
            saved = {}
        return dict(DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS})

    def save_settings(self, **changes):
        current = self.settings()
        for key, value in changes.items():
            if key not in DEFAULTS or value is None:
                continue
            if key == "miles":
                current[key] = _num(value, current[key], 0, 200)
            elif key in ("weights", "pilates", "strides", "water_goal"):
                current[key] = int(_num(value, current[key], 0, 20))
            elif key in ("workout_day", "long_day"):
                if value not in DAYS:
                    raise Invalid(f"{key.replace('_', ' ')} is one of {', '.join(DAYS)}.")
                current[key] = value
            elif key == "rest_day":
                if value not in DAYS + ("",):
                    raise Invalid("rest day is a day (mon...sun) or empty for none.")
                current[key] = value
            elif key.endswith("_time"):
                current[key] = _clock(value, current[key])
            elif key in RACES:
                text = str(value).strip()
                if text and race_seconds(text) is None:
                    raise Invalid(f"{key.replace('race_', '').upper()} time is minutes:seconds, like 17:39.")
                current[key] = text
            else:
                current[key] = str(value)[:120]
        if current["workout_day"] == current["long_day"]:
            raise Invalid("The hard workout and the long run need different days.")
        self._conn().execute(
            """INSERT INTO tr_settings (user_id, settings_json) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET settings_json = excluded.settings_json,
               updated_at = CURRENT_TIMESTAMP""", (self._user(), json.dumps(current)))
        self._commit()
        return current

    # ------------------------------------------------------------ calendar feed

    def calendar_token(self, renew=False):
        """The secret in his calendar link; renew=True makes a new one (the old link stops working)."""
        user = self._user()
        row = self._conn().execute("SELECT calendar_token FROM tr_settings WHERE user_id = ?", (user,)).fetchone()
        token = row["calendar_token"] if row else ""
        if token and not renew:
            return token
        token = secrets.token_urlsafe(24)
        self._conn().execute(
            """INSERT INTO tr_settings (user_id, calendar_token) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET calendar_token = excluded.calendar_token""", (user, token))
        self._commit()
        return token

    def user_for_token(self, token):
        if not token or len(token) < 20:
            return None
        row = self._conn().execute("SELECT user_id FROM tr_settings WHERE calendar_token = ?", (token,)).fetchone()
        return row["user_id"] if row else None

    # ------------------------------------------------------------ weeks

    def week(self, start):
        start = week_start(start).isoformat()
        row = self._conn().execute("SELECT * FROM tr_weeks WHERE user_id = ? AND week_start = ?",
                                   (self._user(), start)).fetchone()
        if not row:
            return {"week_start": start, "miles_target": self.settings()["miles"], "goals": [], "review": "",
                    "checked_in_at": "", "saved": False}
        data = dict(row)
        try:
            data["goals"] = [str(g) for g in json.loads(data.pop("goals_json") or "[]")]
        except (TypeError, json.JSONDecodeError):
            data["goals"] = []
        data["saved"] = True
        return data

    def save_week(self, start, miles_target=None, goals=None, review=None, checked_in=False):
        current = self.week(start)
        miles = _num(miles_target, current["miles_target"], 0, 200) if miles_target not in (None, "") else current["miles_target"]
        goal_list = current["goals"] if goals is None else [" ".join(str(g).split())[:160] for g in goals if str(g).strip()][:6]
        text = current["review"] if review is None else str(review).strip()[:2000]
        self._conn().execute(
            """INSERT INTO tr_weeks (user_id, week_start, miles_target, goals_json, review, checked_in_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, week_start) DO UPDATE SET miles_target = excluded.miles_target,
               goals_json = excluded.goals_json, review = excluded.review,
               checked_in_at = CASE WHEN excluded.checked_in_at != '' THEN excluded.checked_in_at
                                    ELSE tr_weeks.checked_in_at END""",
            (self._user(), current["week_start"], miles, json.dumps(goal_list), text,
             datetime.now().isoformat(timespec="minutes") if checked_in else ""))
        self._commit()
        return self.week(start)

    # ------------------------------------------------------------ sessions

    def sessions(self, start, days=7):
        first = week_start(start) if days == 7 else (date.fromisoformat(start) if isinstance(start, str) else start)
        last = first + timedelta(days=days - 1)
        rows = self._conn().execute(
            "SELECT * FROM tr_sessions WHERE user_id = ? AND day BETWEEN ? AND ? ORDER BY day, slot = '', slot, id",
            (self._user(), first.isoformat(), last.isoformat())).fetchall()
        return [self._view(dict(r)) for r in rows]

    def sessions_between(self, first, last):
        rows = self._conn().execute(
            "SELECT * FROM tr_sessions WHERE user_id = ? AND day BETWEEN ? AND ? ORDER BY day, slot, id",
            (self._user(), first.isoformat(), last.isoformat())).fetchall()
        return [self._view(dict(r)) for r in rows]

    @staticmethod
    def _view(row):
        row["kind_label"] = KINDS.get(row["kind"], row["kind"])
        row["is_run"] = row["kind"] in RUN_KINDS
        row["done"] = row["status"] == "done"
        return row

    def session(self, session_id):
        row = self._conn().execute("SELECT * FROM tr_sessions WHERE id = ? AND user_id = ?",
                                   (int(session_id), self._user())).fetchone()
        if not row:
            raise Invalid(f"There's no session {session_id}.")
        return self._view(dict(row))

    def add_session(self, day, kind, title="", miles=None, slot="", details="", strides=0):
        if kind not in KINDS:
            raise Invalid(f"kind is one of: {', '.join(KINDS)}.")
        try:
            day = date.fromisoformat(str(day)[:10]).isoformat()
        except ValueError:
            raise Invalid("day is a date like 2026-12-01.") from None
        cur = self._conn().execute(
            """INSERT INTO tr_sessions (user_id, day, slot, kind, title, miles, details, strides)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (self._user(), day, _clock(slot), kind, " ".join(str(title or KINDS[kind]).split())[:120],
             _num(miles, None, 0, 100) if kind in RUN_KINDS else None, str(details or "").strip()[:500],
             int(_num(strides, 0, 0, 30))))
        self._commit()
        return self.session(cur.lastrowid)

    def update_session(self, session_id, **changes):
        session = self.session(session_id)
        fields = {}
        for key, value in changes.items():
            if value is None:
                continue
            if key == "day":
                try:
                    fields["day"] = date.fromisoformat(str(value)[:10]).isoformat()
                except ValueError:
                    raise Invalid("day is a date like 2026-12-01.") from None
            elif key == "slot":
                fields["slot"] = _clock(value)
            elif key == "kind":
                if value not in KINDS:
                    raise Invalid(f"kind is one of: {', '.join(KINDS)}.")
                fields["kind"] = value
            elif key == "miles":
                fields["miles"] = _num(value, None, 0, 100)
            elif key == "status":
                if value not in STATUSES:
                    raise Invalid(f"status is one of: {', '.join(STATUSES)}.")
                fields["status"] = value
            elif key in ("strides", "strides_done"):
                fields[key] = int(_num(value, 0, 0, 30))
            elif key in ("title", "details", "note"):
                fields[key] = " ".join(str(value).split())[:500] if key == "title" else str(value).strip()[:500]
        if not fields:
            return session
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._conn().execute(f"UPDATE tr_sessions SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                             list(fields.values()) + [session["id"], self._user()])
        self._commit()
        return self.session(session["id"])

    def delete_session(self, session_id):
        session = self.session(session_id)
        self._conn().execute("DELETE FROM tr_sessions WHERE id = ? AND user_id = ?", (session["id"], self._user()))
        self._commit()

    def fill_week(self, start, replace=False):
        """Put the draft week in (only into an empty week unless replace=True). Returns the week's sessions."""
        start = week_start(start)
        existing = self.sessions(start)
        if existing and not replace:
            return existing
        if replace:
            self._conn().execute("DELETE FROM tr_sessions WHERE user_id = ? AND day BETWEEN ? AND ? AND status = 'planned'",
                                 (self._user(), start.isoformat(), (start + timedelta(days=6)).isoformat()))
            self._commit()
        settings = self.settings()
        target = self.week(start)["miles_target"] or settings["miles"]
        for s in draft_week(dict(settings, miles=target), start):
            self.add_session(s["day"], s["kind"], s["title"], s["miles"], s["slot"], s["details"], s["strides"])
        return self.sessions(start)

    # ------------------------------------------------------------ habits

    def habits(self, first, last):
        rows = self._conn().execute("SELECT * FROM tr_habits WHERE user_id = ? AND day BETWEEN ? AND ?",
                                    (self._user(), first.isoformat(), last.isoformat())).fetchall()
        return {r["day"]: dict(r) for r in rows}

    def set_habit(self, day, water=None, fueled=None, note=None, add_water=0):
        try:
            day = date.fromisoformat(str(day)[:10]).isoformat()
        except ValueError:
            raise Invalid("day is a date like 2026-12-01.") from None
        current = self.habits(date.fromisoformat(day), date.fromisoformat(day)).get(day) or {"water": 0, "fueled": 0, "note": ""}
        new_water = int(_num(water, current["water"], 0, 40)) if water is not None else current["water"]
        new_water = max(0, min(40, new_water + int(add_water or 0)))
        new_fueled = (1 if fueled else 0) if fueled is not None else current["fueled"]
        new_note = current["note"] if note is None else str(note).strip()[:300]
        self._conn().execute(
            """INSERT INTO tr_habits (user_id, day, water, fueled, note) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, day) DO UPDATE SET water = excluded.water, fueled = excluded.fueled,
               note = excluded.note""", (self._user(), day, new_water, new_fueled, new_note))
        self._commit()
        return {"day": day, "water": new_water, "fueled": bool(new_fueled), "note": new_note}

    # ------------------------------------------------------------ Strava (read-only)

    def strava(self, first, last):
        """Imported activities in [first, last]: {day: [{title, kind, miles, minutes, strides, stride_pace}]}."""
        from . import speed

        try:
            speed.ensure(self.db)
            rows = self._conn().execute(
                """SELECT w.title, w.activity_type, w.started_at, w.distance_meters, w.moving_time_seconds,
                          w.external_id, w.source, s.strides, s.efforts_json
                   FROM trainer_imported_workouts w
                   LEFT JOIN tr_run_speed s ON s.user_id = w.user_id AND s.external_id = w.external_id
                   WHERE w.user_id = ? AND substr(w.started_at, 1, 10) BETWEEN ? AND ?
                   ORDER BY w.started_at""", (self._user(), first.isoformat(), last.isoformat())).fetchall()
        except Exception:  # the Strava table only exists once the old Trainer has run on this database
            return {}
        out = {}
        for r in rows:
            kind = (r["activity_type"] or "").lower()
            out.setdefault(str(r["started_at"])[:10], []).append({
                "title": r["title"] or r["activity_type"] or "Activity", "kind": kind,
                "is_run": "run" in kind,
                "miles": round((r["distance_meters"] or 0) / METERS_PER_MILE, 1),
                "minutes": round((r["moving_time_seconds"] or 0) / 60),
                "strides": r["strides"] or 0,
                "url": (f"/apps/trainer/run/{r['external_id']}" if r["source"] == "strava" and "run" in kind
                        and str(r["external_id"]).isdigit() else ""),
                "speed": speed.summary(json.loads(r["efforts_json"])) if r["efforts_json"] else ""})
        return out

    def today(self, day=None):
        """For the Today page: today's sessions as short lines, and what's left to do."""
        day = day or clock.today()
        sessions = [s for s in self.sessions(day, days=1) if s["kind"] != "rest"]
        lines = []
        for s in sessions:
            text = s["title"] or s["kind_label"]
            if s["miles"]:
                text += f" · {s['miles']:g} mi"
            if s["strides"]:
                text += " + strides"
            if s["slot"]:
                text += f" · {datetime.combine(day, time.fromisoformat(s['slot'])).strftime('%-I:%M %p').lower()}"
            lines.append({"text": text, "done": s["done"] and (not s["strides"] or bool(s["strides_done"]))})
        return {"lines": lines, "left": sum(1 for line in lines if not line["done"])}

    # ------------------------------------------------------------ the week, put together

    def week_view(self, start, today=None):
        today = today or clock.today()
        start = week_start(start)
        end = start + timedelta(days=6)
        sessions = self.sessions(start)
        strava = self.strava(start, end)
        habits = self.habits(start, end)
        settings = self.settings()
        info = self.week(start)
        days = []
        for offset in range(7):
            day = start + timedelta(days=offset)
            iso = day.isoformat()
            days.append({"date": iso, "name": DAY_NAMES[DAYS[offset]], "short": day.strftime("%a"),
                         "label": f"{day.strftime('%a')}, {day.strftime('%b')} {day.day}",
                         "is_today": day == today, "is_past": day < today,
                         "sessions": [s for s in sessions if s["day"] == iso],
                         "strava": strava.get(iso, []), "habits": habits.get(iso) or {"water": 0, "fueled": 0, "note": ""}})
        run_miles = sum(a["miles"] for d in days for a in d["strava"] if a["is_run"])
        planned_miles = sum(s["miles"] or 0 for s in sessions if s["is_run"])
        count = lambda kind: (sum(1 for s in sessions if s["kind"] == kind and s["done"]),
                              sum(1 for s in sessions if s["kind"] == kind))
        strides_done = sum(1 for s in sessions if s["strides"] and s["strides_done"])
        strides_planned = sum(1 for s in sessions if s["strides"])
        totals = {
            "miles_target": info["miles_target"] or settings["miles"], "miles_planned": round(planned_miles, 1),
            "miles_run": round(run_miles, 1),
            "weights": count("weights"), "pilates": count("pilates"),
            "strides": (strides_done, strides_planned),
            "workout": count("workout"),
            "water_days": sum(1 for d in days if d["habits"]["water"] >= settings["water_goal"]),
        }
        return {"start": start.isoformat(), "end": end.isoformat(),
                "label": f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}",
                "days": days, "week": info, "totals": totals, "settings": settings, "empty": not sessions,
                "prev": (start - timedelta(days=7)).isoformat(), "next": (start + timedelta(days=7)).isoformat()}


# ---------------------------------------------------------------- calendar feed (iCalendar)


def _ics_text(value):
    return (str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n"))


def _fold(line):
    """iCalendar lines are at most 75 octets; longer ones continue on the next line after a space."""
    out, raw = [], line.encode("utf-8")
    while len(raw) > 75:
        cut = 75
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = b" " + raw[cut:]
    out.append(raw.decode("utf-8"))
    return "\r\n".join(out)


DURATIONS = {"easy": 50, "workout": 90, "long": 110, "race": 60, "weights": 45, "pilates": 50, "mobility": 20, "rest": 0}


def calendar(sessions, weeks=(), host="askdieter.ai", now=None):
    """The planned sessions (and Sunday check-ins) as an iCalendar feed his phone's calendar subscribes to.

    Sessions with a time get that slot and a reminder 30 minutes before; sessions without one are all-day. Times are
    "floating" (no time zone), so 4 pm stays 4 pm wherever his phone is.
    """
    stamp = (now or datetime.utcnow()).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Dieter//Trainer//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Dieter Trainer", "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
             "X-PUBLISHED-TTL:PT1H"]
    for s in sessions:
        if s["kind"] == "rest" or s["status"] == "skipped":
            continue
        day = date.fromisoformat(s["day"])
        title = s["title"] or KINDS.get(s["kind"], s["kind"])
        if s.get("miles"):
            title += f" · {s['miles']:g} mi"
        if s.get("strides"):
            title += f" + {s['strides']} strides"
        if s["status"] == "done":
            title = "✓ " + title
        lines += ["BEGIN:VEVENT", f"UID:tr-session-{s['id']}@{host}", f"DTSTAMP:{stamp}",
                  _fold(f"SUMMARY:{_ics_text(title)}")]
        if s.get("slot"):
            start = datetime.combine(day, time.fromisoformat(s["slot"]))
            end = start + timedelta(minutes=DURATIONS.get(s["kind"], 45) or 30)
            lines += [f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}", f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}"]
            alarm = True
        else:
            lines += [f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
                      f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}"]
            alarm = False
        if s.get("details"):
            lines.append(_fold(f"DESCRIPTION:{_ics_text(s['details'])}"))
        lines.append(_fold(f"URL:https://{host}/apps/trainer?week={week_start(day).isoformat()}"))
        if alarm and s["status"] == "planned":
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", _fold(f"DESCRIPTION:{_ics_text(title)}"),
                      "TRIGGER:-PT30M", "END:VALARM"]
        lines.append("END:VEVENT")
    for start in weeks:  # Sunday evening: set the coming week
        sunday = date.fromisoformat(start) - timedelta(days=1)
        begin = datetime.combine(sunday, time(19, 0))
        lines += ["BEGIN:VEVENT", f"UID:tr-checkin-{start}@{host}", f"DTSTAMP:{stamp}",
                  "SUMMARY:Set the week (Dieter Trainer)",
                  f"DTSTART:{begin.strftime('%Y%m%dT%H%M%S')}",
                  f"DTEND:{(begin + timedelta(minutes=15)).strftime('%Y%m%dT%H%M%S')}",
                  _fold(f"URL:https://{host}/apps/trainer/checkin"),
                  "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Set the week in Dieter Trainer", "TRIGGER:PT0M",
                  "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
