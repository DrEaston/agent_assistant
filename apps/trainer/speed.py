"""Speed work, from Strava's second-by-second data on each run (docs/trainer_analysis_plan.md, Speed).

Strava's summary only has a run's average and its single fastest moment, so strides and short reps were invisible,
and its heart-rate-based effort score barely sees a 28-second 200 (heart rate hasn't caught up before it's over).
Each run's streams (time, speed, distance: one extra Strava request per run) are read once and kept as a compact
summary, not the streams:

- fast stretches: bursts well above the run's usual speed, 6 s to 8 min, each with its distance, time and best
  5 seconds, named by length: a stride (under 150 m), a standard rep (200, 300, 400, 600, 800, 1K, 1200, mile) or "rep";
- the run's best efforts at 400 m to 5K (fastest stretch of each distance anywhere in the run);
- its best 5 seconds (his top speed);
- a speed histogram: seconds spent in each 0.1 m/s band above 3 m/s. Scores that depend on his current critical speed
  are worked out from this, so when critical speed changes nothing has to be read from Strava again.

The measures (all estimates, labelled so on the page):

- critical speed (CS) and D' from the critical speed model (Monod & Scherrer; Jones & Vanhatalo): the linear
  distance-time fit d = CS x t + D' over his best efforts lasting 2-20 minutes in the 26 weeks up to that week.
  CS is the fastest pace he could hold for a long time; D' is the metres he can run "above" it before he's spent;
- speed work: D' used, sum of (speed - CS) x seconds over everything faster than CS, in metres and in "tanks"
  (multiples of his D'). The plain two-parameter model: it's least certain for near-sprint efforts, and the published
  corrections disagree, so none is applied;
- top-speed running: metres run at 90% or more of his top speed (best 5 s in the same 26 weeks): the leg-stress side
  (weekly high-speed distance is linked to hamstring injuries in team-sport research);
- stride speed: the middle of a week's strides' best-5-second speeds: the same relaxed-fast effort each time.

Planned strides that day are ticked when strides are found.
"""

import json
import statistics
from datetime import date, timedelta

VERSION = 2                         # bump when the summary changes: older rows are read again
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS tr_run_speed (
        user_id INTEGER NOT NULL, external_id TEXT NOT NULL, day TEXT NOT NULL, version INTEGER DEFAULT 0,
        strides INTEGER DEFAULT 0, efforts_json TEXT DEFAULT '[]', stride_mps REAL, usual_mps REAL, best5_mps REAL,
        hist_json TEXT DEFAULT '{}', bests_json TEXT DEFAULT '{}', checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, external_id))""",
)
MIN_SECONDS, MAX_SECONDS = 6, 480   # a stride is ~15-30 s; reps up to 8 minutes (a mile or two kilometres)
MERGE_GAP = 3                       # a moment below the line inside one burst (GPS wobble) doesn't split it
WINDOW = 5                          # top speed = best 5 seconds
STRIDE_METRES = 150
STANDARD = (200, 300, 400, 600, 800, 1000, 1200, 1609)
NAMES = {1000: "1K", 1609: "mile"}
BEST_DISTANCES = (400, 800, 1000, 1609, 3000, 5000)
HIST_FROM = 3.0                     # m/s: the histogram starts here (slower running never counts as speed work)
FIT_WEEKS = 26                      # critical speed and top speed come from this many weeks of best efforts
# The line not to cross without building up (acute:chronic workload ratio, Gabbett 2016): this week against the
# average of the 4 weeks before. Over 1.5x is where injury risk rose in that research (it's debated: a guide).
CHRONIC_WEEKS, RATIO = 4, 1.5
FLOOR_TANKS = 2                     # speed work line never below 2 x D' (a set of strides after a quiet month is fine)
FLOOR_NEAR_TOP = 500                # top-speed running line never below 500 m
LOOKBACK_WEEKS = 52                 # older runs are read back this far
METERS_PER_MILE = 1609.344


COLUMNS = (("version", "INTEGER DEFAULT 0"), ("efforts_json", "TEXT DEFAULT '[]'"), ("best5_mps", "REAL"),
           ("hist_json", "TEXT DEFAULT '{}'"), ("bests_json", "TEXT DEFAULT '{}'"))


def ensure(db):
    conn = db.connect()
    for statement in SCHEMA:
        conn.execute(statement)
    have = {r[1] for r in conn.execute("PRAGMA table_info(tr_run_speed)").fetchall()}
    for name, kind in COLUMNS:  # additive: a table made by an earlier version gets the new columns
        if name not in have:
            conn.execute(f"ALTER TABLE tr_run_speed ADD COLUMN {name} {kind}")
    conn.commit()
    return conn


def pace(mps):
    """m/s -> "m:ss" per mile."""
    if not mps:
        return ""
    seconds = round(METERS_PER_MILE / mps)
    return f"{seconds // 60}:{seconds % 60:02d}"


def clock(seconds):
    seconds = round(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds >= 60 else f"{seconds} s"


# ---------------------------------------------------------------- one run


def _samples(times, speeds, dists=None):
    out = []
    for i, (t, v) in enumerate(zip(times or [], speeds or [])):
        if t is None or v is None:
            continue
        d = dists[i] if dists and i < len(dists) and dists[i] is not None else None
        out.append((float(t), float(v), None if d is None else float(d)))
    return out


def _best_window(points):
    best = 0.0
    for i, (start, _) in enumerate(points):
        inside = [v for t, v in points[i:] if t < start + WINDOW]
        if len(inside) >= 3:
            best = max(best, sum(inside) / len(inside))
    return best


def label(metres):
    if metres < STRIDE_METRES:
        return "stride"
    near = min(STANDARD, key=lambda d: abs(d - metres))
    return NAMES.get(near, str(near)) if abs(near - metres) <= near * 0.12 else "rep"


def find_efforts(times, speeds, dists=None):
    """([{at, seconds, metres, avg_mps, top_mps, kind}], usual m/s) for each fast stretch in a run.

    Usual speed is the median while moving; a stretch is time above max(1.4 x usual, usual + 1 m/s) lasting
    MIN_SECONDS to MAX_SECONDS. Distance comes from the distance stream when there is one."""
    samples = _samples(times, speeds, dists)
    moving = [v for _, v, _ in samples if v > 1.0]
    if len(moving) < 60:
        return [], None
    usual = statistics.median(moving)
    line = max(usual * 1.4, usual + 1.0)
    bursts, current = [], None
    for t, v, d in samples:
        if v >= line:
            if current and t - current["end"] <= MERGE_GAP:
                current["end"], current["last_d"] = t, d
                current["points"].append((t, v))
            else:
                current = {"start": t, "end": t, "first_d": d, "last_d": d, "points": [(t, v)]}
                bursts.append(current)
    found = []
    for b in bursts:
        seconds = b["end"] - b["start"] + 1
        if not (MIN_SECONDS <= seconds <= MAX_SECONDS) or len(b["points"]) < 4:
            continue
        if b["first_d"] is not None and b["last_d"] is not None and b["last_d"] > b["first_d"]:
            avg = sum(v for _, v in b["points"]) / len(b["points"])
            metres = b["last_d"] - b["first_d"] + avg  # the first second's distance too
        else:
            metres = sum(v for _, v in b["points"]) * seconds / len(b["points"])
        top = _best_window(b["points"]) or max(v for _, v in b["points"])
        found.append({"at": int(b["start"]), "seconds": int(seconds), "metres": round(metres),
                      "avg_mps": round(metres / seconds, 2), "top_mps": round(top, 2), "kind": label(metres)})
    return found, round(usual, 2)


def best_efforts(times, dists):
    """{metres: seconds} for the fastest stretch of each BEST_DISTANCES distance anywhere in the run."""
    points = [(float(t), float(d)) for t, d in zip(times or [], dists or []) if t is not None and d is not None]
    out = {}
    if len(points) < 2:
        return out
    for target in BEST_DISTANCES:
        if points[-1][1] - points[0][1] < target:
            break
        best, j = None, 0
        for i in range(len(points)):
            while j < len(points) and points[j][1] - points[i][1] < target:
                j += 1
            if j == len(points):
                break
            # time to cover exactly `target` from point i, interpolating inside the last step
            (t0, d0), (t1, d1) = points[j - 1], points[j]
            need = points[i][1] + target
            at = t1 if d1 == d0 else t0 + (t1 - t0) * (need - d0) / (d1 - d0)
            took = at - points[i][0]
            if best is None or took < best:
                best = took
        if best:
            out[str(target)] = round(best, 1)
    return out


def histogram(times, speeds):
    """{band: seconds}: time in each 0.1 m/s band from HIST_FROM up (band = speed x 10, rounded down). A gap in the
    recording (paused watch) counts at most 2 seconds."""
    samples = _samples(times, speeds)
    hist = {}
    for (t, v, _), (t_next, _, _) in zip(samples, samples[1:]):
        if v < HIST_FROM:
            continue
        band = str(int(v * 10))
        hist[band] = hist.get(band, 0) + min(t_next - t, 2.0)
    return {k: round(s, 1) for k, s in hist.items()}


def top_speed(times, speeds):
    """Best 5 seconds of the whole run (m/s)."""
    samples = _samples(times, speeds)
    best = _best_window([(t, v) for t, v, _ in samples])
    return round(best, 2) if best else None


def stride_speed(efforts):
    """The run's stride speed: the middle of its strides' top speeds (m/s), or None without strides."""
    tops = [e["top_mps"] for e in efforts if e["kind"] == "stride"]
    return round(statistics.median(tops), 2) if tops else None


def summary(efforts):
    """Short words for a run's fast stretches: "5 strides (4:58/mi)", "8 × 400 (avg 76 s)"."""
    parts, groups = [], {}
    for e in efforts:
        groups.setdefault(e["kind"], []).append(e)
    for kind, items in groups.items():
        if kind == "stride":
            parts.append(f"{len(items)} stride{'s' if len(items) != 1 else ''} "
                         f"({pace(statistics.median(e['top_mps'] for e in items))}/mi)")
        elif kind == "rep":
            avg_pace = pace(sum(e["metres"] for e in items) / sum(e["seconds"] for e in items))
            parts.append(f"{len(items)} fast rep{'s' if len(items) != 1 else ''} ({avg_pace}/mi)")
        else:
            avg = sum(e["seconds"] for e in items) / len(items)
            parts.append(f"{len(items)} × {kind} (avg {clock(avg)})")
    return ", ".join(parts)


# ---------------------------------------------------------------- the model


def fit_critical_speed(bests):
    """(CS m/s, D' m) from {metres: seconds} best efforts lasting 2-20 minutes (least squares on d = CS·t + D'),
    or (None, None) when there aren't two usable efforts or the fit isn't believable."""
    points = [(t, float(d)) for d, t in bests.items() if t and 120 <= t <= 1200]
    if len({d for _, d in points}) < 2:
        return None, None
    n = len(points)
    mt, md = sum(t for t, _ in points) / n, sum(d for _, d in points) / n
    var = sum((t - mt) ** 2 for t, _ in points)
    if not var:
        return None, None
    cs = sum((t - mt) * (d - md) for t, d in points) / var
    d_prime = md - cs * mt
    if not (2.5 <= cs <= 7.5 and 20 <= d_prime <= 800):
        return None, None
    return round(cs, 3), round(d_prime)


def _speed(band):
    return int(band) / 10 + 0.05  # the middle of the band


def d_prime_used(hist, cs):
    """Metres 'above' critical speed: sum of (speed - CS) x seconds over everything faster than CS."""
    if not cs:
        return 0.0
    return sum((_speed(b) - cs) * s for b, s in hist.items() if _speed(b) > cs)


def near_top_metres(hist, vmax):
    """Metres run at 90% of top speed or faster."""
    if not vmax:
        return 0.0
    return sum(_speed(b) * s for b, s in hist.items() if _speed(b) >= 0.9 * vmax)


# ---------------------------------------------------------------- reading runs from Strava


def analyze(sync, user_id, external_id, day):
    """Fetch one run's streams, summarise them, keep the result, tick planned strides. Returns the row."""
    from .strava import API

    data = sync.call(f"{API}/activities/{external_id}/streams?keys=time,velocity_smooth,distance&key_by_type=true",
                     token=sync._token(user_id))
    streams = data if isinstance(data, dict) else {}
    times = (streams.get("time") or {}).get("data") or []
    speeds = (streams.get("velocity_smooth") or {}).get("data") or []
    dists = (streams.get("distance") or {}).get("data") or []
    efforts, usual = find_efforts(times, speeds, dists)
    strides = [e for e in efforts if e["kind"] == "stride"]
    row = {"user_id": user_id, "external_id": str(external_id), "day": str(day)[:10], "version": VERSION,
           "strides": len(strides), "efforts_json": json.dumps(efforts), "stride_mps": stride_speed(efforts),
           "usual_mps": usual, "best5_mps": top_speed(times, speeds), "hist_json": json.dumps(histogram(times, speeds)),
           "bests_json": json.dumps(best_efforts(times, dists))}
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
    return dict(row, efforts=efforts)


def _waiting(db, user_id, limit, today=None):
    since = ((today or date.today()) - timedelta(weeks=LOOKBACK_WEEKS)).isoformat()
    ensure(db)
    return db.connect().execute(
        """SELECT w.external_id, substr(COALESCE(NULLIF(w.started_at_local, ''), w.started_at), 1, 10) AS day
           FROM trainer_imported_workouts w
           LEFT JOIN tr_run_speed s ON s.user_id = w.user_id AND s.external_id = w.external_id
           WHERE w.user_id = ? AND w.source = 'strava' AND w.workout_category LIKE 'run%'
             AND COALESCE(w.strava_deleted_at, '') = '' AND w.started_at >= ?
             AND (s.external_id IS NULL OR s.version < ?)
           ORDER BY w.started_at DESC LIMIT ?""", (user_id, since, VERSION, limit)).fetchall()


def waiting_count(db, user_id):
    return len(_waiting(db, user_id, 10000))


def fill(sync, user_id, limit=5):
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


# ---------------------------------------------------------------- week by week


def _rows(db, user_id, since):
    ensure(db)
    rows = db.connect().execute("SELECT * FROM tr_run_speed WHERE user_id = ? AND day >= ? ORDER BY day",
                                (user_id, since.isoformat())).fetchall()
    out = []
    for r in rows:
        out.append({"day": date.fromisoformat(r["day"]), "efforts": json.loads(r["efforts_json"] or "[]"),
                    "hist": json.loads(r["hist_json"] or "{}"), "bests": json.loads(r["bests_json"] or "{}"),
                    "best5": r["best5_mps"]})
    return out


def model_at(runs, end):
    """CS, D' and top speed from the FIT_WEEKS up to `end`."""
    start = end - timedelta(weeks=FIT_WEEKS)
    window = [r for r in runs if start < r["day"] <= end]
    bests = {}
    for r in window:
        for d, t in r["bests"].items():
            if t and (d not in bests or t < bests[d]):
                bests[d] = t
    cs, d_prime = fit_critical_speed(bests)
    vmax = max((r["best5"] or 0 for r in window), default=0) or None
    return {"cs": cs, "d_prime": d_prime, "vmax": vmax, "bests": bests}


def weekly(db, user_id, week_starts):
    """Per week (Monday -> numbers): critical speed and D' as of that week, speed work (D' used, tanks), top-speed
    metres, strides and stride speed, and the fast stretches by kind."""
    if not week_starts:
        return {}
    runs = _rows(db, user_id, week_starts[0] - timedelta(weeks=FIT_WEEKS + CHRONIC_WEEKS))
    lead = [week_starts[0] - timedelta(weeks=n) for n in range(CHRONIC_WEEKS, 0, -1)]
    out = {}
    for w in lead + list(week_starts):
        end = w + timedelta(days=6)
        model = model_at(runs, end)
        week_runs = [r for r in runs if w <= r["day"] <= end]
        used = sum(d_prime_used(r["hist"], model["cs"]) for r in week_runs)
        near = sum(near_top_metres(r["hist"], model["vmax"]) for r in week_runs)
        efforts = [e for r in week_runs for e in r["efforts"]]
        strides = [e["top_mps"] for e in efforts if e["kind"] == "stride"]
        kinds = {}
        for e in efforts:
            k = kinds.setdefault(e["kind"], {"count": 0, "metres": 0})
            k["count"] += 1
            k["metres"] += e["metres"]
        stride_mps = round(statistics.median(strides), 2) if strides else None
        out[w] = {"cs": model["cs"], "cs_pace": pace(model["cs"]), "d_prime": model["d_prime"],
                  "vmax": model["vmax"], "speed_work": round(used) if model["cs"] else None,
                  "tanks": round(used / model["d_prime"], 1) if model["cs"] and model["d_prime"] else None,
                  "near_top": round(near), "strides": len(strides), "stride_mps": stride_mps,
                  "stride_pace": pace(stride_mps), "kinds": kinds, "read": len(week_runs)}
    for w in week_starts:
        before = [out[w - timedelta(weeks=n)] for n in range(1, CHRONIC_WEEKS + 1)]
        week = out[w]
        work_usual = sum(b["speed_work"] or 0 for b in before) / CHRONIC_WEEKS
        top_usual = sum(b["near_top"] for b in before) / CHRONIC_WEEKS
        week["work_line"] = (round(max(RATIO * work_usual, FLOOR_TANKS * week["d_prime"])) if week["d_prime"] else None)
        week["top_line"] = round(max(RATIO * top_usual, FLOOR_NEAR_TOP))
        week["work_over"] = bool(week["work_line"] and (week["speed_work"] or 0) > week["work_line"])
        week["top_over"] = week["near_top"] > week["top_line"]
    return {w: out[w] for w in week_starts}


def latest(db, user_id):
    """The most recent run with fast stretches: {day, summary}, or None."""
    ensure(db)
    row = db.connect().execute("SELECT day, efforts_json FROM tr_run_speed WHERE user_id = ? AND efforts_json != '[]' "
                               "ORDER BY day DESC LIMIT 1", (user_id,)).fetchone()
    if not row:
        return None
    return {"day": row["day"], "summary": summary(json.loads(row["efforts_json"]))}
