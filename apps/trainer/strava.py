"""Strava, live: new activities arrive by themselves, and Dieter keeps its own training log (docs/trainer_plan.md).

How it works:
- Strava's webhook: once a push subscription exists (one per Strava app, made with "Turn on live sync"), Strava
  POSTs a small event to /strava/webhook whenever an activity is created, changed or deleted. Strava wants an answer
  within two seconds, so the event is only written down (tr_strava_events) and handled after the reply, and again
  whenever This week or Today opens, so an event is never lost if the background step is cut short.
- Handling an event: find the Dieter user by Strava athlete id, refresh their token, fetch the activity, and upsert
  it into trainer_imported_workouts. Every activity type is kept (runs, rides, weights, Pilates/yoga, walks, swims):
  Dieter's log is his record, not only what Trainer charts. A deleted Strava activity stays in Dieter, marked.
- Whole history: "Import my whole history" walks his Strava history 200 activities per request (summaries only, so a
  few requests cover years) and stores them; details (laps, splits) are filled in a few at a time later, inside
  Strava's rate limits (100 requests / 15 min, 1,000 / day).
- Manual entries (source "dieter"): sessions done without a watch, e.g. a Pilates class.

Settings: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET (already used for connecting). The webhook's verify token is made
and stored here; nothing new to configure.
"""

import json
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from urllib.parse import urlencode

from database import reset_current_user_id, set_current_user_id

API = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"
DETAILS_PER_RUN = 20  # activity details fetched per pass, to stay well inside Strava's 15-minute limit

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS tr_strava_sync (key TEXT PRIMARY KEY, value TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS tr_strava_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT DEFAULT CURRENT_TIMESTAMP, object_type TEXT,
        object_id TEXT, aspect_type TEXT, owner_id TEXT, updates_json TEXT DEFAULT '{}', status TEXT DEFAULT 'pending',
        tries INTEGER DEFAULT 0, error TEXT DEFAULT '', done_at TEXT DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS tr_strava_events_status ON tr_strava_events (status, id)",
)
WORKOUT_COLUMNS = (("started_at_local", "TEXT DEFAULT ''"), ("strava_deleted_at", "TEXT DEFAULT ''"),
                   ("needs_detail", "INTEGER DEFAULT 0"), ("notes", "TEXT DEFAULT ''"))


class StravaError(Exception):
    pass


# ---------------------------------------------------------------- turning a Strava activity into a log row


def classify_strava_workout(activity_type, title=""):
    """Map a Strava activity into a Trainer bucket."""
    kind = (activity_type or "").strip().lower()
    text = f"{activity_type or ''} {title or ''}".lower()
    if kind in {"run", "trailrun", "virtualrun"}:
        if re.search(r"\b(threshold|tempo|mile|1k|cruise)\b", text):
            return "run_threshold"
        if re.search(r"\b(400|interval|speed|rep|track|fartlek)\b", text):
            return "run_speed"
        return "run"
    if kind in {"ride", "virtualride", "ebikeride"}:
        return "bike_tempo" if re.search(r"\b(tempo|threshold|interval)\b", text) else "bike"
    if kind in {"weighttraining", "workout", "crossfit", "highintensityintervaltraining"}:
        return "strength_glutes" if re.search(r"\b(glute|clam|rdl|deadlift|band|bridge|hip)\b", text) else "strength"
    if kind in {"pilates", "yoga"}:
        return kind
    return kind or "workout"


def strava_first_present(activity, detail, key):
    """Prefer detail values, but keep summary values when detail omits them."""
    if detail and detail.get(key) is not None:
        return detail.get(key)
    if activity:
        return activity.get(key)
    return None


def strava_activity_import_payload(activity, detail=None):
    """Build the persisted metric payload for a Strava activity."""
    detail = detail or {}
    merged = {**(activity or {}), **detail}
    activity_type = merged.get("sport_type") or merged.get("type") or ""
    title = merged.get("name") or activity_type
    gear = merged.get("gear") if isinstance(merged.get("gear"), dict) else {}
    return {
        "external_id": str(merged.get("id")),
        "activity_type": activity_type,
        "workout_category": classify_strava_workout(activity_type, title),
        "title": title,
        "started_at": (merged.get("start_date_local") or merged.get("start_date") or "")[:10],
        "distance_meters": strava_first_present(activity, detail, "distance"),
        "moving_time_seconds": strava_first_present(activity, detail, "moving_time"),
        "elapsed_time_seconds": strava_first_present(activity, detail, "elapsed_time"),
        "elevation_gain_meters": strava_first_present(activity, detail, "total_elevation_gain"),
        "average_speed_mps": strava_first_present(activity, detail, "average_speed"),
        "max_speed_mps": strava_first_present(activity, detail, "max_speed"),
        "average_heartrate": strava_first_present(activity, detail, "average_heartrate"),
        "max_heartrate": strava_first_present(activity, detail, "max_heartrate"),
        "average_cadence": strava_first_present(activity, detail, "average_cadence"),
        "average_watts": strava_first_present(activity, detail, "average_watts"),
        "kilojoules": strava_first_present(activity, detail, "kilojoules"),
        "suffer_score": strava_first_present(activity, detail, "suffer_score"),
        "perceived_exertion": strava_first_present(activity, detail, "perceived_exertion"),
        "gear_id": strava_first_present(activity, detail, "gear_id") or gear.get("id") or "",
        "gear_name": gear.get("name") or "",
        "start_latlng": strava_first_present(activity, detail, "start_latlng") or [],
        "end_latlng": strava_first_present(activity, detail, "end_latlng") or [],
        "splits_metric": detail.get("splits_metric") or [],
        "laps": detail.get("laps") or [],
        "raw": {"summary": activity or {}, "detail": detail or {}},
    }


# ---------------------------------------------------------------- talking to Strava


def http_json(url, method="GET", data=None, access_token="", opener=None):
    body, headers = None, {"Accept": "application/json"}
    if data is not None:
        body = urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with (opener or urllib.request.urlopen)(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise StravaError(f"Strava said {exc.code}: {detail[:300]}") from None
    except urllib.error.URLError as exc:
        raise StravaError(f"Couldn't reach Strava: {exc.reason}") from None


class Sync:
    """Live sync and the whole-history import. `call(url, method, data, token)` talks to Strava (swapped in tests)."""

    def __init__(self, db, client_id="", client_secret="", call=None):
        self.db, self.client_id, self.client_secret = db, client_id, client_secret
        self.call = call or (lambda url, method="GET", data=None, token="": http_json(url, method, data, token))
        self._ready = False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            for statement in SCHEMA:
                conn.execute(statement)
            have = {r[1] for r in conn.execute("PRAGMA table_info(trainer_imported_workouts)").fetchall()}
            if have:
                for column, kind in WORKOUT_COLUMNS:
                    if column not in have:
                        conn.execute(f"ALTER TABLE trainer_imported_workouts ADD COLUMN {column} {kind}")
            conn.commit()
            self._ready = True
        return conn

    # ------------------------------------------------------------ settings

    def get(self, key, default=""):
        row = self._conn().execute("SELECT value FROM tr_strava_sync WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def put(self, key, value):
        self._conn().execute("INSERT INTO tr_strava_sync (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                             "value = excluded.value", (key, str(value)))
        self.db._commit()

    def verify_token(self):
        token = self.get("verify_token")
        if not token:
            token = secrets.token_urlsafe(24)
            self.put("verify_token", token)
        return token

    def status(self):
        conn = self._conn()
        pending = conn.execute("SELECT COUNT(*) FROM tr_strava_events WHERE status = 'pending'").fetchone()[0]
        last = conn.execute("SELECT received_at FROM tr_strava_events ORDER BY id DESC LIMIT 1").fetchone()
        return {"live": bool(self.get("subscription_id")), "subscription_id": self.get("subscription_id"),
                "pending": pending, "last_event": last["received_at"] if last else "",
                "history_done": self.get("history_done") == "1", "history_count": int(self.get("history_count", "0") or 0)}

    # ------------------------------------------------------------ the push subscription

    def subscribe(self, callback_url):
        """Ask Strava to push events to callback_url (Strava checks it right away with a GET)."""
        if not (self.client_id and self.client_secret):
            raise StravaError("Strava isn't set up (STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET).")
        existing = self.call(f"{API}/push_subscriptions?" + urlencode(
            {"client_id": self.client_id, "client_secret": self.client_secret}))
        for sub in existing if isinstance(existing, list) else []:
            if sub.get("callback_url") == callback_url:
                self.put("subscription_id", sub.get("id"))
                return sub.get("id")
            # Strava allows one subscription per app: replace one pointing somewhere else
            self.call(f"{API}/push_subscriptions/{sub.get('id')}?" + urlencode(
                {"client_id": self.client_id, "client_secret": self.client_secret}), method="DELETE")
        made = self.call(f"{API}/push_subscriptions", method="POST", data={
            "client_id": self.client_id, "client_secret": self.client_secret, "callback_url": callback_url,
            "verify_token": self.verify_token()})
        if not made.get("id"):
            raise StravaError("Strava didn't make the subscription.")
        self.put("subscription_id", made["id"])
        return made["id"]

    def challenge(self, mode, token, challenge):
        """Strava's check of the callback URL. Returns the echo, or None if it isn't ours."""
        if mode == "subscribe" and token and secrets.compare_digest(token, self.verify_token()):
            return {"hub.challenge": challenge}
        return None

    # ------------------------------------------------------------ events

    def receive(self, event):
        """Write an event down. False if it isn't for our subscription (ignored)."""
        sub = self.get("subscription_id")
        if not isinstance(event, dict) or (sub and str(event.get("subscription_id")) != str(sub)):
            return False
        self._conn().execute(
            "INSERT INTO tr_strava_events (object_type, object_id, aspect_type, owner_id, updates_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(event.get("object_type") or ""), str(event.get("object_id") or ""),
             str(event.get("aspect_type") or ""), str(event.get("owner_id") or ""),
             json.dumps(event.get("updates") or {})[:2000]))
        self.db._commit()
        return True

    def _user_for_athlete(self, athlete_id):
        row = self._conn().execute("SELECT user_id FROM trainer_profiles WHERE strava_athlete_id = ? "
                                   "AND strava_refresh_token != ''", (str(athlete_id),)).fetchone()
        return row["user_id"] if row else None

    def _token(self, user_id):
        profile = self.db.get_trainer_profile(user_id)
        profile = dict(profile) if profile else {}
        if not profile.get("strava_refresh_token"):
            raise StravaError("Strava isn't connected for this person.")
        if profile.get("strava_access_token") and int(profile.get("strava_token_expires_at") or 0) > time.time() + 60:
            return profile["strava_access_token"]
        fresh = self.call(TOKEN_URL, method="POST", data={
            "client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "refresh_token",
            "refresh_token": profile["strava_refresh_token"]})
        reset = set_current_user_id(user_id)
        try:
            self.db.update_trainer_strava_tokens(
                athlete_id=profile.get("strava_athlete_id"), access_token=fresh.get("access_token", ""),
                refresh_token=fresh.get("refresh_token", profile["strava_refresh_token"]),
                expires_at=fresh.get("expires_at", 0), scope=profile.get("strava_scope", ""))
        finally:
            reset_current_user_id(reset)
        return fresh.get("access_token", "")

    def _store(self, user_id, activity, detail=None, needs_detail=False):
        payload = strava_activity_import_payload(activity, detail)
        self.db.add_trainer_imported_workout(**payload, user_id=user_id)
        merged = {**(activity or {}), **(detail or {})}
        self._conn().execute(
            "UPDATE trainer_imported_workouts SET started_at_local = ?, needs_detail = ?, strava_deleted_at = '' "
            "WHERE source = 'strava' AND external_id = ? AND user_id = ?",
            (str(merged.get("start_date_local") or merged.get("start_date") or "")[:19], 1 if needs_detail else 0,
             payload["external_id"], user_id))
        self.db._commit()

    def handle(self, event):
        """One event: fetch and store the activity, or mark it deleted."""
        if event["object_type"] != "activity":
            return "ignored"  # athlete events (e.g. he revoked access) need nothing here
        user = self._user_for_athlete(event["owner_id"])
        if not user:
            return "unknown athlete"
        if event["aspect_type"] == "delete":
            self._conn().execute(
                "UPDATE trainer_imported_workouts SET strava_deleted_at = ? WHERE source = 'strava' AND external_id = ? "
                "AND user_id = ?", (datetime.utcnow().isoformat(timespec="seconds"), event["object_id"], user))
            self.db._commit()
            return "marked deleted"
        detail = self.call(f"{API}/activities/{event['object_id']}?include_all_efforts=false", token=self._token(user))
        self._store(user, detail, detail)
        return "stored"

    def process(self, limit=10):
        """Handle waiting events, oldest first. Returns how many were handled."""
        rows = self._conn().execute("SELECT * FROM tr_strava_events WHERE status = 'pending' ORDER BY id LIMIT ?",
                                    (limit,)).fetchall()
        handled = 0
        for row in rows:
            event = dict(row)
            try:
                result = self.handle(event)
                self._conn().execute("UPDATE tr_strava_events SET status = 'done', error = ?, done_at = "
                                     "CURRENT_TIMESTAMP, tries = tries + 1 WHERE id = ?", (result, event["id"]))
                handled += 1
            except Exception as exc:  # keep it for another try; give up after 5
                status = "failed" if event["tries"] + 1 >= 5 else "pending"
                self._conn().execute("UPDATE tr_strava_events SET status = ?, error = ?, tries = tries + 1 "
                                     "WHERE id = ?", (status, str(exc)[:300], event["id"]))
                if isinstance(exc, StravaError) and "429" in str(exc):
                    self.db._commit()
                    break  # rate limited: stop for now
            self.db._commit()
        return handled

    # ------------------------------------------------------------ whole history

    def import_history(self, user_id, pages=5, per_page=200):
        """Walk his history from oldest to newest, `pages` pages a call (summaries; details later). Resumable."""
        token = self._token(user_id)
        after = int(self.get(f"history_after_{user_id}", "0") or 0)
        stored = 0
        for _ in range(pages):
            batch = self.call(f"{API}/athlete/activities?" + urlencode({"after": after, "per_page": per_page,
                                                                       "page": 1}), token=token)
            if not isinstance(batch, list) or not batch:
                self.put("history_done", "1")
                break
            batch.sort(key=lambda a: a.get("start_date") or "")
            for activity in batch:
                known = self._conn().execute(
                    "SELECT 1 FROM trainer_imported_workouts WHERE source = 'strava' AND external_id = ? AND user_id = ?",
                    (str(activity.get("id")), user_id)).fetchone()
                if known:
                    continue  # already in the log (maybe with full detail): don't replace it with a summary
                self._store(user_id, activity, None, needs_detail=True)
                stored += 1
            newest = batch[-1].get("start_date") or ""
            try:
                after = int(datetime.fromisoformat(newest.replace("Z", "+00:00")).timestamp())
            except ValueError:
                break
            self.put(f"history_after_{user_id}", after)
            if len(batch) < per_page:
                self.put("history_done", "1")
                break
        self.put("history_count", int(self.get("history_count", "0") or 0) + stored)
        return stored

    def fill_details(self, user_id, limit=DETAILS_PER_RUN):
        """Fetch laps and splits for a few history activities that only have a summary."""
        rows = self._conn().execute(
            "SELECT external_id FROM trainer_imported_workouts WHERE user_id = ? AND source = 'strava' AND "
            "needs_detail = 1 AND strava_deleted_at = '' ORDER BY started_at DESC LIMIT ?", (user_id, limit)).fetchall()
        if not rows:
            return 0
        token = self._token(user_id)
        done = 0
        for row in rows:
            try:
                detail = self.call(f"{API}/activities/{row['external_id']}", token=token)
            except StravaError as exc:
                if "429" in str(exc):
                    break
                continue
            self._store(user_id, detail, detail)
            done += 1
        return done


# ---------------------------------------------------------------- his training log (Dieter's own record)

METERS_PER_MILE = 1609.344


def log(db, user_id, kind="", year=None, include_deleted=False, limit=500):
    """His activities newest first, from Strava and entered in Dieter."""
    Sync(db)._conn()  # makes sure the log's extra columns exist
    sql = ("SELECT * FROM trainer_imported_workouts WHERE user_id = ?" +
           ("" if include_deleted else " AND COALESCE(strava_deleted_at, '') = ''"))
    params = [user_id]
    if kind:
        sql += " AND workout_category LIKE ?"
        params.append(f"{kind}%")
    if year:
        sql += " AND substr(started_at, 1, 4) = ?"
        params.append(str(year))
    sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    rows = db.connect().execute(sql, params).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        out.append({"id": r["id"], "source": r.get("source") or "strava", "date": r["started_at"],
                    "time": (r.get("started_at_local") or "")[11:16], "type": r.get("activity_type") or "",
                    "category": r.get("workout_category") or "", "title": r.get("title") or "",
                    "miles": round((r.get("distance_meters") or 0) / METERS_PER_MILE, 2),
                    "minutes": round((r.get("moving_time_seconds") or 0) / 60, 1),
                    "avg_hr": r.get("average_heartrate"), "elevation_m": r.get("elevation_gain_meters"),
                    "notes": r.get("notes") or "", "deleted_on_strava": bool(r.get("strava_deleted_at")),
                    "has_detail": not r.get("needs_detail")})
    return out


def _bucket(category):
    if category.startswith("run"):
        return "run"
    if category.startswith("strength"):
        return "strength"
    if category in ("pilates", "yoga"):
        return "pilates"
    if category.startswith("bike"):
        return "bike"
    return "other"


def summary(db, user_id, weeks=12, today=None):
    """Totals instead of a list: the last `weeks` weeks and the last 12 months, by kind of session."""
    from datetime import timedelta

    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    first = monday - timedelta(weeks=weeks - 1)
    year_ago = date(today.year - 1, today.month, 1)
    entries = log(db, user_id, limit=100000)

    def empty():
        return {"run_miles": 0.0, "runs": 0, "strength": 0, "pilates": 0, "bike_miles": 0.0, "other": 0, "minutes": 0.0}

    by_week = {(first + timedelta(weeks=n)).isoformat(): empty() for n in range(weeks)}
    by_month = {}
    for e in entries:
        try:
            day = date.fromisoformat(e["date"][:10])
        except ValueError:
            continue
        bucket = _bucket(e["category"])
        targets = []
        week = (day - timedelta(days=day.weekday())).isoformat()
        if week in by_week:
            targets.append(by_week[week])
        if day >= year_ago:
            targets.append(by_month.setdefault(day.strftime("%Y-%m"), empty()))
        for t in targets:
            t["minutes"] += e["minutes"]
            if bucket == "run":
                t["runs"] += 1
                t["run_miles"] += e["miles"]
            elif bucket == "bike":
                t["bike_miles"] += e["miles"]
            elif bucket in ("strength", "pilates"):
                t[bucket] += 1
            else:
                t["other"] += 1
    rows = lambda d: [dict(v, key=k, run_miles=round(v["run_miles"], 1), bike_miles=round(v["bike_miles"], 1),
                           hours=round(v["minutes"] / 60, 1)) for k, v in sorted(d.items(), reverse=True)]
    return {"weeks": rows(by_week), "months": rows(by_month), "total": len(entries), "recent": entries[:5]}


def add_manual(db, user_id, day, activity_type, title="", minutes=None, miles=None, notes=""):
    """An activity done without Strava (e.g. a Pilates class); kept in the same log, source 'dieter'."""
    try:
        day = date.fromisoformat(str(day)[:10]).isoformat()
    except ValueError:
        raise ValueError("day is a date like 2026-12-01.") from None
    activity_type = " ".join(str(activity_type or "Workout").split())[:40]
    Sync(db)._conn()  # makes sure the log's extra columns exist
    external = f"dieter-{secrets.token_hex(6)}"
    db.add_trainer_imported_workout(
        external_id=external, activity_type=activity_type,
        workout_category=classify_strava_workout(activity_type.replace(" ", ""), title), title=title or activity_type,
        started_at=day, distance_meters=(float(miles) * METERS_PER_MILE) if miles else None,
        moving_time_seconds=int(float(minutes) * 60) if minutes else None, raw={"entered_in": "dieter"},
        user_id=user_id)
    conn = db.connect()
    conn.execute("UPDATE trainer_imported_workouts SET source = 'dieter', notes = ?, started_at_local = ? "
                 "WHERE external_id = ? AND user_id = ?", (str(notes or "")[:500], day, external, user_id))
    db._commit()
    return external


CSV_FIELDS = ("date", "time", "source", "type", "category", "title", "miles", "minutes", "avg_hr", "elevation_m",
              "notes", "deleted_on_strava")
