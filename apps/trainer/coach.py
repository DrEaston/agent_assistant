"""Coach mode (docs/trainer_sharing_plan.md, "Coach mode").

An athlete invites a coach with a link. The coach prescribes workouts in plain terms ("6 x 1 mile at threshold,
90 s jog; 8 x 200 m sprint"). Dieter turns each set into the athlete's own target times from the athlete's speed
model (threshold pace = critical speed; race paces from critical speed and D'; sprint = near-sprint speed) - plain
formulas, no AI.

The coach never sees the athlete's runs, paces or target times: only what the coach wrote, whether the athlete took
it, and the note the athlete chose to write. Nothing here reads Strava data for a coach. Target times are worked
out only when the athlete is the one signed in (`targets()` is called from the athlete's pages only).

Tables (additive, created on first use):
- tr_coach_links: athlete, coach (empty until the invite is accepted), invite token, status invited/active/ended,
  and full_view_until: a temporary exception (see FULL_VIEW_DAYS).
- tr_prescriptions: one prescribed workout: athlete, coach, day, title, sets (JSON), coach's notes, status
  sent/accepted/declined/done, the plan session it became, the athlete's note back.

The coach page at /coach (temporary, for showing coach mode): one shared passcode opens it, with no email or account.
Signing in makes (or reuses) one shared coach account, links it to Dieter's owner as their coach, and gives it full
view, so the coach can open the owner's Trainer read-only and see the paces Dieter picked. The account only gets the
coach pages (api.py keeps role "coach" there). Only a hash of the passcode is in the code
(DIETER_COACH_PASSCODE_HASH overrides it; an empty value turns /coach off).
"""

import hashlib
import hmac
import json
import os
import secrets
from datetime import date, timedelta

from database import get_current_user_id

from . import clock, plan, speed

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS tr_coach_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT, athlete_id INTEGER NOT NULL, coach_id INTEGER,
        token TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'invited',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, accepted_at TEXT DEFAULT '', ended_at TEXT DEFAULT '',
        full_view_until TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS tr_prescriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, athlete_id INTEGER NOT NULL, coach_id INTEGER NOT NULL,
        day TEXT NOT NULL, title TEXT DEFAULT '', sets_json TEXT DEFAULT '[]', notes TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'sent', session_id INTEGER, athlete_note TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, answered_at TEXT DEFAULT '')""",
)
ADDED_COLUMNS = (("rounds", "INTEGER DEFAULT 1"), ("round_rest", "TEXT DEFAULT ''"))
MAX_ROUNDS = 20
PASSCODE_ROLE = "coach"          # the shared passcode account: api.py lets it use the coach pages only
PASSCODE_EMAIL = "coach-passcode@askdieter.local"
PASSCODE_HASH = os.getenv("DIETER_COACH_PASSCODE_HASH",
                          "pbkdf2_sha256$210000$7e3f1a9c5b2d8e4f6a1c3b5d7e9f0a2c$"
                          "7a30bc772cb73e197e21ddb3c9a55d88a5ad5ce69f488077bd791eb9659f134e")
PBKDF2_ITERATIONS = 210_000      # same format as api.py's hash_password


def _hash(passcode):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _matches(passcode, stored):
    try:
        algorithm, iterations, salt_hex, digest_hex = (stored or "").split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
    except Exception:
        return False

INTENSITIES = {
    "easy": "easy",
    "threshold": "threshold",
    "10k": "10K pace",
    "5k": "5K pace",
    "3k": "3K pace",
    "mile": "mile pace",
    "sprint": "sprint",
    "feel": "by feel",
}
RACE_METRES = {"10k": 10000, "5k": 5000, "3k": 3000, "mile": 1609}
DISTANCES = (100, 150, 200, 300, 400, 600, 800, 1000, 1200, 1609, 2000, 3218)
DISTANCE_NAMES = {1609: "1 mile", 3218: "2 miles", 1000: "1 km", 2000: "2 km"}
SPRINT_SHARE = 0.9      # "sprint" = Dieter's near-sprint line: 90% of his top speed (best 5 seconds)
SPREAD = 0.015          # target range: +/- 1.5% of the target speed
MAX_SETS = 6
STATUSES = ("sent", "accepted", "declined", "done")
FULL_VIEW_DAYS = 30     # Temporary, for building coach mode with a real coach: Dieter's owner (admin) can let one
                        # coach see all of their Trainer pages read-only. It switches itself off after this many days.
                        # This shows the owner's Strava-sourced runs to another person, which Strava's API terms
                        # restrict; it is the owner's own call about their own data, and it is never offered to
                        # other accounts.


class Invalid(ValueError):
    pass


def distance_name(metres):
    return DISTANCE_NAMES.get(metres) or f"{metres} m"


def describe_set(s):
    """'3 × 1200 m at threshold, 60 s jog between, then 2 min' - what the coach wrote, with no times.
    rest: between the reps of this set; rest_after: after the set, before the next one."""
    text = f"{s['reps']} × {distance_name(s['metres'])}"
    if s["intensity"] in ("sprint", "easy", "feel"):
        text += f" {INTENSITIES[s['intensity']]}"
    else:
        text += f" at {INTENSITIES[s['intensity']]}"
    if s.get("rest") and s["reps"] > 1:
        text += f", {s['rest']} between"
    if s.get("rest_after"):
        text += f", then {s['rest_after']}"
    return text


def describe_rounds(rounds, round_rest=""):
    """'3 rounds, 4 min between rounds', or '' for a single round."""
    if not rounds or rounds <= 1:
        return ""
    return f"{rounds} rounds" + (f", {round_rest} between rounds" if round_rest else "")


def describe_workout(sets, rounds=1, round_rest=""):
    """The whole workout in one line: '3 × (3 × 1200 m at threshold, ...; 4 × 200 m sprint), 4 min between rounds'."""
    body = "; ".join(describe_set(s) for s in sets)
    if rounds and rounds > 1:
        return f"{rounds} × ({body})" + (f", {round_rest} between rounds" if round_rest else "")
    return body


def clean_rounds(rounds, round_rest=""):
    try:
        rounds = int(str(rounds or 1).strip() or 1)
    except ValueError:
        raise Invalid("Rounds is a number, like 3.") from None
    if not 1 <= rounds <= MAX_ROUNDS:
        raise Invalid(f"Rounds are between 1 and {MAX_ROUNDS}.")
    return rounds, (" ".join(str(round_rest or "").split())[:40] if rounds > 1 else "")


def clean_sets(raw):
    """[{reps, metres, intensity, rest}] from form rows; empty rows are skipped."""
    sets = []
    for row in raw or []:
        reps, metres = str(row.get("reps") or "").strip(), str(row.get("metres") or "").strip()
        if not reps and not metres:
            continue
        try:
            reps, metres = int(reps), int(float(metres))
        except ValueError:
            raise Invalid("Each set needs a number of reps and a distance.") from None
        if not 1 <= reps <= 50:
            raise Invalid("Reps are between 1 and 50.")
        if not 50 <= metres <= 20000:
            raise Invalid("A rep is between 50 m and 20 km.")
        intensity = row.get("intensity") or "feel"
        if intensity not in INTENSITIES:
            raise Invalid(f"Effort is one of: {', '.join(INTENSITIES.values())}.")
        sets.append({"reps": reps, "metres": metres, "intensity": intensity,
                     "rest": " ".join(str(row.get("rest") or "").split())[:40],
                     "rest_after": " ".join(str(row.get("rest_after") or "").split())[:40]})
    if not sets:
        raise Invalid("Add at least one set, like 6 × 1 mile at threshold.")
    return sets[:MAX_SETS]


# ------------------------------------------------------------ the athlete's times (athlete only)


def athlete_model(db, user_id, today=None, races=None):
    """{cs, d_prime, vmax, easy} for the athlete from their own runs (and race times they entered)."""
    today = today or clock.today()
    runs = speed._rows(db, user_id, today - timedelta(weeks=speed.FIT_WEEKS))
    model = speed.model_at(runs, today, races)
    model["easy"] = speed.easy_speed(db, user_id, today)
    return model


def target_speed(intensity, model):
    """m/s for an effort from the athlete's model, or None when Dieter can't tell yet."""
    cs, d_prime = model.get("cs"), model.get("d_prime")
    if intensity == "threshold":
        return cs
    if intensity in RACE_METRES:
        metres = RACE_METRES[intensity]
        if cs and d_prime is not None and metres > d_prime:
            return metres / ((metres - d_prime) / cs)   # race time from d = CS·t + D'
        return None
    if intensity == "sprint":
        return SPRINT_SHARE * model["vmax"] if model.get("vmax") else None
    if intensity == "easy":
        return model.get("easy")
    return None


PROFILE_EFFORTS = (("easy", None), ("threshold", 1609), ("10k", 1609), ("5k", 1609), ("3k", 1609), ("mile", 400),
                   ("sprint", 200))


def pace_profile(model):
    """The paces Dieter would pick for each effort: [{effort, pace, rep}] (rep: time for a typical rep length)."""
    out = []
    for intensity, metres in PROFILE_EFFORTS:
        v = target_speed(intensity, model)
        if not v:
            continue
        out.append({"effort": INTENSITIES[intensity].capitalize(), "pace": f"{speed.pace(v)}/mi",
                    "rep": f"{distance_name(metres)} in {speed.clock(round(metres / v))}" if metres else ""})
    return out


def targets(sets, model):
    """Each set with the athlete's target: per-rep time and range, and pace per mile for longer reps."""
    out = []
    for s in sets:
        v = target_speed(s["intensity"], model)
        row = dict(s, text=describe_set(s), target="")
        if v:
            rep = s["metres"] / v
            if s["intensity"] == "easy":
                row["target"] = f"about {speed.pace(v)}/mi"
            elif s["metres"] < 400 or s["intensity"] == "sprint":
                row["target"] = f"about {speed.clock(rep)} each"
            else:
                fast, slow = s["metres"] / (v * (1 + SPREAD)), s["metres"] / (v * (1 - SPREAD))
                row["target"] = f"{speed.clock(rep)} each ({speed.clock(fast)}–{speed.clock(slow)})"
                if s["metres"] >= 800 and s["metres"] != 1609:
                    row["target"] += f", {speed.pace(v)}/mi"
        elif s["intensity"] != "feel":
            row["target"] = "Dieter needs a few more hard runs (or a race time on Usual week) to set this."
        out.append(row)
    return out


def _live(until, today=None):
    """The full-view end date if it hasn't passed, else ""."""
    return until if until and until >= (today or clock.today()).isoformat() else ""


# ------------------------------------------------------------ store


class CoachStore:
    def __init__(self, db):
        self.db, self._ready = db, False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            for statement in SCHEMA:
                conn.execute(statement)
            have = {r[1] for r in conn.execute("PRAGMA table_info(tr_prescriptions)").fetchall()}
            for column, kind in ADDED_COLUMNS:          # additive: older workouts are 1 round
                if column not in have:
                    conn.execute(f"ALTER TABLE tr_prescriptions ADD COLUMN {column} {kind}")
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

    def _name(self, user_id):
        row = self.db.get_user_by_id(user_id) if user_id else None
        row = dict(row) if row else {}
        return row.get("display_name") or (row.get("email") or "").split("@")[0] or "Someone"

    # -------------------------------------------------------- links

    def invite(self):
        """The athlete's open invite link token (one at a time; made on first ask)."""
        me = self._user()
        row = self._conn().execute("SELECT token FROM tr_coach_links WHERE athlete_id = ? AND status = 'invited'",
                                   (me,)).fetchone()
        if row:
            return row["token"]
        token = secrets.token_urlsafe(18)
        self._conn().execute("INSERT INTO tr_coach_links (athlete_id, token) VALUES (?, ?)", (me, token))
        self._commit()
        return token

    def open_invite(self):
        """{link_id, token} of the athlete's unused invite, or None (doesn't make one)."""
        row = self._conn().execute("SELECT id, token FROM tr_coach_links WHERE athlete_id = ? AND status = 'invited'",
                                   (self._user(),)).fetchone()
        return {"link_id": row["id"], "token": row["token"]} if row else None

    def invite_info(self, token):
        row = self._conn().execute("SELECT * FROM tr_coach_links WHERE token = ?", (token,)).fetchone()
        if not row or row["status"] != "invited":
            raise Invalid("This invite link has been used or cancelled. Ask for a new one.")
        return {"athlete": self._name(row["athlete_id"]), "athlete_id": row["athlete_id"]}

    def accept(self, token):
        """The signed-in person becomes the coach on this invite."""
        me = self._user()
        info = self.invite_info(token)
        if info["athlete_id"] == me:
            raise Invalid("That's your own invite link: send it to your coach.")
        conn = self._conn()
        if conn.execute("SELECT 1 FROM tr_coach_links WHERE athlete_id = ? AND coach_id = ? AND status = 'active'",
                        (info["athlete_id"], me)).fetchone():
            conn.execute("UPDATE tr_coach_links SET status = 'ended', ended_at = CURRENT_TIMESTAMP WHERE token = ?",
                         (token,))
        else:
            conn.execute("UPDATE tr_coach_links SET coach_id = ?, status = 'active', accepted_at = CURRENT_TIMESTAMP "
                         "WHERE token = ?", (me, token))
        self._commit()
        return info

    def end(self, link_id):
        """Either side can stop coaching; it ends at once. An open invite can be cancelled the same way."""
        me = self._user()
        cur = self._conn().execute(
            "UPDATE tr_coach_links SET status = 'ended', ended_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status IN ('invited', 'active') AND (athlete_id = ? OR coach_id = ?)",
            (link_id, me, me))
        self._commit()
        if not cur.rowcount:
            raise Invalid("Nothing to stop.")

    def my_coaches(self):
        rows = self._conn().execute("SELECT * FROM tr_coach_links WHERE athlete_id = ? AND status = 'active' "
                                    "ORDER BY accepted_at", (self._user(),)).fetchall()
        return [{"link_id": r["id"], "coach_id": r["coach_id"], "name": self._name(r["coach_id"]),
                 "since": (r["accepted_at"] or "")[:10], "full_view_until": _live(r["full_view_until"])}
                for r in rows]

    def is_owner(self, user_id=None):
        row = self.db.get_user_by_id(user_id or self._user())
        row = dict(row) if row else {}
        return row.get("role") == "admin" and row.get("status", "active") == "active"

    def set_full_view(self, link_id, on, today=None):
        """The owner lets one of their coaches see all their Trainer pages, read-only, for FULL_VIEW_DAYS (or stops it)."""
        me = self._user()
        if not self.is_owner(me):
            raise PermissionError("Full view is only for Dieter's owner, for now.")
        until = ((today or clock.today()) + timedelta(days=FULL_VIEW_DAYS)).isoformat() if on else ""
        cur = self._conn().execute("UPDATE tr_coach_links SET full_view_until = ? WHERE id = ? AND athlete_id = ? "
                                   "AND status = 'active'", (until, link_id, me))
        self._commit()
        if not cur.rowcount:
            raise Invalid("That coach isn't linked to you.")
        return until

    def full_view(self, athlete_id, today=None):
        """Whether the signed-in coach may see this athlete's whole Trainer right now (read-only)."""
        me = get_current_user_id()
        if not me or me == athlete_id:
            return False
        row = self._conn().execute("SELECT full_view_until FROM tr_coach_links WHERE athlete_id = ? AND coach_id = ? "
                                   "AND status = 'active'", (athlete_id, me)).fetchone()
        return bool(row and _live(row["full_view_until"], today) and self.is_owner(athlete_id))

    def my_athletes(self):
        rows = self._conn().execute("SELECT * FROM tr_coach_links WHERE coach_id = ? AND status = 'active' "
                                    "ORDER BY accepted_at", (self._user(),)).fetchall()
        out = []
        for r in rows:
            counts = {s: 0 for s in STATUSES}
            for p in self._conn().execute("SELECT status FROM tr_prescriptions WHERE athlete_id = ? AND coach_id = ?",
                                          (r["athlete_id"], r["coach_id"])).fetchall():
                counts[p["status"]] = counts.get(p["status"], 0) + 1
            out.append({"link_id": r["id"], "athlete_id": r["athlete_id"], "name": self._name(r["athlete_id"]),
                        "since": (r["accepted_at"] or "")[:10], "counts": counts,
                        "full_view_until": _live(r["full_view_until"]) if self.is_owner(r["athlete_id"]) else ""})
        return out

    def coaches_athlete(self, athlete_id):
        """The signed-in coach's active link to this athlete (raises if there isn't one)."""
        me = self._user()
        row = self._conn().execute("SELECT * FROM tr_coach_links WHERE athlete_id = ? AND coach_id = ? "
                                   "AND status = 'active'", (athlete_id, me)).fetchone()
        if not row:
            raise PermissionError("This athlete hasn't shared Trainer with you (or stopped sharing).")
        return {"link_id": row["id"], "athlete_id": athlete_id, "name": self._name(athlete_id),
                "full_view_until": _live(row["full_view_until"]) if self.is_owner(athlete_id) else ""}

    # -------------------------------------------------------- prescriptions

    def _view(self, row, for_athlete):
        sets = json.loads(row["sets_json"] or "[]")
        keys = row.keys()
        rounds = (row["rounds"] if "rounds" in keys else 1) or 1
        round_rest = (row["round_rest"] if "round_rest" in keys else "") or ""
        p = {"id": row["id"], "day": row["day"], "title": row["title"], "notes": row["notes"],
             "status": row["status"], "athlete_note": row["athlete_note"], "session_id": row["session_id"],
             "sets": [dict(s, text=describe_set(s)) for s in sets],
             "rounds": rounds, "round_rest": round_rest, "rounds_text": describe_rounds(rounds, round_rest),
             "coach": self._name(row["coach_id"]) if for_athlete else "",
             "summary": describe_workout(sets, rounds, round_rest)}
        return p

    def prescribe(self, athlete_id, day, title, sets, notes="", rounds=1, round_rest=""):
        """A coach sends a workout. It waits for the athlete to take it; nothing changes on their plan until then."""
        link = self.coaches_athlete(athlete_id)
        try:
            day = date.fromisoformat(str(day)[:10]).isoformat()
        except ValueError:
            raise Invalid("Pick a day for the workout.") from None
        sets = clean_sets(sets)
        rounds, round_rest = clean_rounds(rounds, round_rest)
        title = " ".join(str(title or "").split())[:120] or "Workout from your coach"
        cur = self._conn().execute(
            "INSERT INTO tr_prescriptions (athlete_id, coach_id, day, title, sets_json, notes, rounds, round_rest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (link["athlete_id"], self._user(), day, title, json.dumps(sets), " ".join(str(notes or "").split())[:500],
             rounds, round_rest))
        self._commit()
        return cur.lastrowid

    def withdraw(self, prescription_id):
        """The coach takes back a workout the athlete hasn't answered yet."""
        cur = self._conn().execute("DELETE FROM tr_prescriptions WHERE id = ? AND coach_id = ? AND status = 'sent'",
                                   (prescription_id, self._user()))
        self._commit()
        if not cur.rowcount:
            raise Invalid("Only a workout your athlete hasn't answered yet can be taken back.")

    def sent_to(self, athlete_id):
        """What the coach sees for one athlete: their own prescriptions, status and the athlete's notes. No times."""
        link = self.coaches_athlete(athlete_id)
        rows = self._conn().execute("SELECT * FROM tr_prescriptions WHERE athlete_id = ? AND coach_id = ? "
                                    "ORDER BY day DESC, id DESC LIMIT 60", (athlete_id, self._user())).fetchall()
        return link, [self._view(r, for_athlete=False) for r in rows]

    def for_me(self):
        """The athlete's prescriptions from every coach, newest day first."""
        rows = self._conn().execute("SELECT * FROM tr_prescriptions WHERE athlete_id = ? ORDER BY "
                                    "CASE status WHEN 'sent' THEN 0 ELSE 1 END, day DESC, id DESC LIMIT 60",
                                    (self._user(),)).fetchall()
        return [self._view(r, for_athlete=True) for r in rows]

    def new_for_me(self):
        """Workouts coaches sent the signed-in athlete that they haven't answered yet, soonest day first."""
        rows = self._conn().execute("SELECT * FROM tr_prescriptions WHERE athlete_id = ? AND status = 'sent' "
                                    "ORDER BY day, id", (self._user(),)).fetchall()
        return [self._view(r, for_athlete=True) for r in rows]

    def _mine(self, prescription_id):
        row = self._conn().execute("SELECT * FROM tr_prescriptions WHERE id = ? AND athlete_id = ?",
                                   (prescription_id, self._user())).fetchone()
        if not row:
            raise Invalid("That workout isn't yours.")
        return row

    def take(self, prescription_id, model):
        """The athlete takes the workout: it goes on their plan with their own target times in the details."""
        row = self._mine(prescription_id)
        if row["status"] != "sent":
            raise Invalid("You've already answered this one.")
        sets = targets(json.loads(row["sets_json"] or "[]"), model)
        details = "; ".join(s["text"] + (f" ({s['target']})" if s["target"] and not s["target"].startswith("Dieter")
                                          else "") for s in sets)
        rounds = (row["rounds"] if "rounds" in row.keys() else 1) or 1
        if rounds > 1:
            details = f"{rounds} rounds of: {details}" + (f". {row['round_rest']} between rounds"
                                                         if row["round_rest"] else "")
        if row["notes"]:
            details += f". Coach: {row['notes']}"
        session = plan.PlanStore(self.db).add_session(row["day"], "workout", row["title"], details=details)
        self._conn().execute("UPDATE tr_prescriptions SET status = 'accepted', session_id = ?, "
                             "answered_at = CURRENT_TIMESTAMP WHERE id = ?", (session["id"], prescription_id))
        self._commit()
        return session

    def decline(self, prescription_id, note=""):
        row = self._mine(prescription_id)
        if row["status"] != "sent":
            raise Invalid("You've already answered this one.")
        self._answer(prescription_id, "declined", note)

    def done(self, prescription_id, note=""):
        """The athlete says it's done, with an optional note for the coach (all the coach hears about how it went)."""
        row = self._mine(prescription_id)
        if row["status"] not in ("sent", "accepted", "done"):
            raise Invalid("This one was declined.")
        self._answer(prescription_id, "done", note)

    def _answer(self, prescription_id, status, note):
        self._conn().execute("UPDATE tr_prescriptions SET status = ?, athlete_note = ?, answered_at = CURRENT_TIMESTAMP "
                             "WHERE id = ?", (status, " ".join(str(note or "").split())[:500], prescription_id))
        self._commit()

    # -------------------------------------------------------- the /coach passcode (no email)

    def _owner(self):
        rows = self.db.get_users_by_role("admin")
        return rows[0]["id"] if rows else None

    def passcode_sign_in(self, passcode):
        """The shared coach account, ready to use, if the passcode is right; else None. On each sign-in the account
        is linked to Dieter's owner as their coach, with full view for FULL_VIEW_DAYS from now."""
        passcode = str(passcode or "").strip()
        owner = self._owner()
        if not passcode or not PASSCODE_HASH or not owner or not _matches(passcode, PASSCODE_HASH):
            return None
        user = self.db.get_user_by_email(PASSCODE_EMAIL)
        if user:
            coach_id = user["id"]
        else:
            coach_id = self.db.create_user(PASSCODE_EMAIL, "Coach", _hash(secrets.token_urlsafe(32)), PASSCODE_ROLE)
        conn = self._conn()
        conn.execute("UPDATE users SET status = 'active', role = ? WHERE id = ?", (PASSCODE_ROLE, coach_id))
        until = (clock.today() + timedelta(days=FULL_VIEW_DAYS)).isoformat()
        if conn.execute("SELECT 1 FROM tr_coach_links WHERE athlete_id = ? AND coach_id = ? AND status = 'active'",
                        (owner, coach_id)).fetchone():
            conn.execute("UPDATE tr_coach_links SET full_view_until = ? WHERE athlete_id = ? AND coach_id = ? "
                         "AND status = 'active'", (until, owner, coach_id))
        else:
            conn.execute("INSERT INTO tr_coach_links (athlete_id, coach_id, token, status, accepted_at, full_view_until) "
                         "VALUES (?, ?, ?, 'active', CURRENT_TIMESTAMP, ?)",
                         (owner, coach_id, secrets.token_urlsafe(18), until))
        self._commit()
        return coach_id

    def send_to_all(self, day, title, sets, notes="", rounds=1, round_rest=""):
        """The coach sends one workout to every athlete they coach. Returns how many it went to."""
        athletes = self.my_athletes()
        if not athletes:
            raise Invalid("No athletes to send to yet.")
        for a in athletes:
            self.prescribe(a["athlete_id"], day, title, sets, notes, rounds, round_rest)
        return len(athletes)

    def sent_by_me(self, limit=20):
        """What the signed-in coach sent lately, to everyone: day, title, sets, who, status, their note."""
        rows = self._conn().execute("SELECT * FROM tr_prescriptions WHERE coach_id = ? ORDER BY day DESC, id DESC "
                                    "LIMIT ?", (self._user(), limit)).fetchall()
        return [dict(self._view(r, for_athlete=False), athlete=self._name(r["athlete_id"]),
                     athlete_id=r["athlete_id"]) for r in rows]
