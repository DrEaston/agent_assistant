"""Strides and stride speed, from Strava's second-by-second speed on each run (docs/trainer_analysis_plan.md, Speed).

Strava's summary only has a run's average and its single fastest moment, so strides at the end of an easy run were
invisible. Here each run's speed stream (one extra Strava request per run) is read once and boiled down to:

- strides: short fast bursts, 6-60 seconds, well above that run's usual speed (a stride, or a short rep). Each keeps
  its start, length and top speed (the best 5 seconds, so one GPS spike can't set it);
- stride speed for the run: the middle of its strides' top speeds.

Week by week, stride speed is the "how speedy am I getting" number: the same relaxed-fast effort, measured the same
way every time. A planned session with strides that day is ticked "strides done" when they're found.

Only the results are kept (tr_run_speed), not the stream. Runs are read when they arrive from Strava, and older runs
a few at a time when Trainer pages open, inside Strava's rate limits.
"""

import json
import statistics
from datetime import date, timedelta

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS tr_run_speed (
        user_id INTEGER NOT NULL, external_id TEXT NOT NULL, day TEXT NOT NULL, strides INTEGER DEFAULT 0,
        strides_json TEXT DEFAULT '[]', stride_mps REAL, usual_mps REAL, checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, external_id))""",
)
MIN_SECONDS, MAX_SECONDS = 6, 60   # a stride is ~15-30 s; up to a minute still counts as a short fast burst
MERGE_GAP = 3                      # seconds below the line inside one burst (GPS wobble) don't split it
WINDOW = 5                         # top speed = best 5 seconds
LOOKBACK_WEEKS = 16                # older runs are read back this far
METERS_PER_MILE = 1609.344


def ensure(db):
    conn = db.connect()
    for statement in SCHEMA:
        conn.execute(statement)
    conn.commit()
    return conn


def find_strides(times, speeds):
    """[{at, seconds, top_mps, peak_mps}] for each fast burst in a run, and the run's usual speed (m/s).

    times: seconds from the start; speeds: m/s (Strava's velocity_smooth). Usual speed is the median while moving;
    a burst is time spent above max(1.4 × usual, usual + 1 m/s) that lasts MIN_SECONDS to MAX_SECONDS."""
    samples = [(float(t), float(v)) for t, v in zip(times or [], speeds or []) if t is not None and v is not None]
    moving = [v for _, v in samples if v > 1.0]
    if len(moving) < 60:
        return [], None
    usual = statistics.median(moving)
    line = max(usual * 1.4, usual + 1.0)
    bursts, current = [], None
    for t, v in samples:
        if v >= line:
            if current and t - current["end"] <= MERGE_GAP:
                current["end"] = t
                current["points"].append((t, v))
            else:
                current = {"start": t, "end": t, "points": [(t, v)]}
                bursts.append(current)
    found = []
    for b in bursts:
        seconds = b["end"] - b["start"] + 1
        if not (MIN_SECONDS <= seconds <= MAX_SECONDS) or len(b["points"]) < 4:
            continue
        found.append({"at": int(b["start"]), "seconds": int(seconds), "top_mps": round(_best_window(b["points"]), 2),
                      "peak_mps": round(max(v for _, v in b["points"]), 2)})
    return found, round(usual, 2)


def _best_window(points):
    best = 0.0
    for i, (start, _) in enumerate(points):
        inside = [v for t, v in points[i:] if t < start + WINDOW]
        if len(inside) >= 3:
            best = max(best, sum(inside) / len(inside))
    return best or max(v for _, v in points)


def stride_speed(strides):
    """The run's stride speed: the middle of its strides' top speeds (m/s), or None without strides."""
    tops = [s["top_mps"] for s in strides]
    return round(statistics.median(tops), 2) if tops else None


def pace(mps):
    """m/s -> "m:ss" per mile."""
    if not mps:
        return ""
    seconds = round(METERS_PER_MILE / mps)
    return f"{seconds // 60}:{seconds % 60:02d}"


# ---------------------------------------------------------------- reading runs from Strava


def analyze(sync, user_id, external_id, day):
    """Fetch one run's speed stream, find its strides, keep the result, tick planned strides. Returns the row."""
    from .strava import API

    data = sync.call(f"{API}/activities/{external_id}/streams?keys=time,velocity_smooth&key_by_type=true",
                     token=sync._token(user_id))
    streams = data if isinstance(data, dict) else {}
    times = (streams.get("time") or {}).get("data") or []
    speeds = (streams.get("velocity_smooth") or {}).get("data") or []
    strides, usual = find_strides(times, speeds)
    row = {"user_id": user_id, "external_id": str(external_id), "day": str(day)[:10], "strides": len(strides),
           "strides_json": json.dumps(strides), "stride_mps": stride_speed(strides), "usual_mps": usual}
    conn = ensure(sync.db)
    conn.execute(f"INSERT OR REPLACE INTO tr_run_speed ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                 list(row.values()))
    if strides:
        # Planned strides that day are done (only ticks; never unticks what he checked himself).
        try:
            conn.execute("UPDATE tr_sessions SET strides_done = 1 WHERE user_id = ? AND day = ? AND strides > 0",
                         (user_id, row["day"]))
        except Exception:  # no plan yet: nothing to tick
            pass
    sync.db._commit()
    return dict(row, strides_list=strides)


def _waiting(db, user_id, limit, today=None):
    since = ((today or date.today()) - timedelta(weeks=LOOKBACK_WEEKS)).isoformat()
    ensure(db)
    return db.connect().execute(
        """SELECT w.external_id, substr(COALESCE(NULLIF(w.started_at_local, ''), w.started_at), 1, 10) AS day
           FROM trainer_imported_workouts w
           LEFT JOIN tr_run_speed s ON s.user_id = w.user_id AND s.external_id = w.external_id
           WHERE w.user_id = ? AND w.source = 'strava' AND w.workout_category LIKE 'run%'
             AND COALESCE(w.strava_deleted_at, '') = '' AND w.started_at >= ? AND s.external_id IS NULL
           ORDER BY w.started_at DESC LIMIT ?""", (user_id, since, limit)).fetchall()


def fill(sync, user_id, limit=4):
    """Read a few runs not read yet, newest first. Stops quietly on Strava trouble (tries again next time)."""
    from .strava import StravaError

    sync._conn()  # the log's extra columns
    done = 0
    for row in _waiting(sync.db, user_id, limit):
        try:
            analyze(sync, user_id, row["external_id"], row["day"])
        except StravaError as exc:
            if "429" in str(exc):
                break
            print(f"Warning: couldn't read speed for run {row['external_id']}: {exc}")
            continue
        done += 1
    return done


# ---------------------------------------------------------------- the Overview's numbers


def weekly(db, user_id, week_starts):
    """Per week (Monday ISO -> {strides, stride_mps, stride_pace}); stride_mps is the middle of that week's strides'
    top speeds."""
    if not week_starts:
        return {}
    ensure(db)
    first = week_starts[0]
    rows = db.connect().execute("SELECT day, strides_json FROM tr_run_speed WHERE user_id = ? AND day >= ?",
                                (user_id, first.isoformat())).fetchall()
    tops = {w: [] for w in week_starts}
    for r in rows:
        d = date.fromisoformat(r["day"])
        monday = d - timedelta(days=d.weekday())
        if monday in tops:
            tops[monday].extend(s["top_mps"] for s in json.loads(r["strides_json"] or "[]"))
    out = {}
    for w, values in tops.items():
        mps = round(statistics.median(values), 2) if values else None
        out[w] = {"strides": len(values), "stride_mps": mps, "stride_pace": pace(mps)}
    return out


def latest(db, user_id):
    """The most recent run with strides: {day, strides, stride_pace, best_pace}, or None."""
    ensure(db)
    row = db.connect().execute("SELECT * FROM tr_run_speed WHERE user_id = ? AND strides > 0 ORDER BY day DESC "
                               "LIMIT 1", (user_id,)).fetchone()
    if not row:
        return None
    strides = json.loads(row["strides_json"] or "[]")
    return {"day": row["day"], "strides": row["strides"], "stride_pace": pace(row["stride_mps"]),
            "best_pace": pace(max(s["top_mps"] for s in strides)) if strides else ""}


def for_day(db, user_id, day):
    """Strides found on one day's runs (for the week view and the connector)."""
    ensure(db)
    rows = db.connect().execute("SELECT strides_json FROM tr_run_speed WHERE user_id = ? AND day = ?",
                                (user_id, str(day)[:10])).fetchall()
    return [s for r in rows for s in json.loads(r["strides_json"] or "[]")]
