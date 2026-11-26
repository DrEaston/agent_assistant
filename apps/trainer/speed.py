"""Speed work, from Strava's second-by-second data on each run (docs/trainer_analysis_plan.md, Speed).

Strava's summary only has a run's average and its single fastest moment, so strides and short reps were invisible,
and its heart-rate-based effort score barely sees a 28-second 200 (heart rate hasn't caught up before it's over).
Each run's streams (time, speed, distance, heart rate, route: one extra Strava request per run) are read once and
kept compactly (zlib + base64 in tr_run_speed.series), so everything below can be worked out again without asking
Strava. From them:

- fast stretches: running well above his easy pace (max(1.35 x easy, easy + 1 m/s), easy being the middle of the
  slower half of his runs), 6 s to 40 min, each with its distance, time and best 5 seconds, named by length: a stride
  (under 150 m), a standard rep (200 ... mile), 3K / 5K / 10K, a "tempo" (8 min or more) or "rep". Measuring against
  his easy pace, not the run's own average, is what finds races and threshold reps (a race's average is race pace);
- the run's best efforts at 400 m to 5K: the fastest steady stretch of each distance (no stop or jog inside it: a
  "best mile" made of 400s and their recoveries isn't a mile effort and would drag critical speed down);
- its best 5 seconds (his top speed);
- a speed histogram: seconds and metres in each 0.05 m/s band above 3 m/s (the metres keep each band's exact speed). Scores that depend on his current critical speed
  are worked out from this, so when critical speed changes nothing has to be read from Strava again.

The measures (all estimates, labelled so on the page):

- critical speed (CS) and D' from the critical speed model (Monod & Scherrer; Jones & Vanhatalo), worked out by
  itself from his runs in the 26 weeks up to that week. Only hard efforts count (at least 1.45 x his usual easy
  speed): the fastest stretch of an easy run is that run's best, not an all-out effort. His best long effort (10-40 min, e.g. a 5K race; a run within
  1% of a standard distance counts as it, since GPS often measures a 5K as 4,997 m) and, when he has one that's hard
  enough, a short one (2-7 min). Two efforts: CS and D' from both. Only the long one (the usual case: training rarely
  has a 3-minute all-out effort): D' is taken as a typical 150 m and CS follows from the long effort. Race times
  entered on Usual week are used too, as an override.
  CS is the fastest pace he could hold for a long time; D' is the metres he can run "above" it before he's spent;
- speed work: D' used, sum of (speed - CS) x seconds over everything faster than CS, in metres and in "tanks"
  (multiples of his D'). The plain two-parameter model: it's least certain for near-sprint efforts, and the published
  corrections disagree, so none is applied;
- top-speed running: metres run at 90% or more of his top speed (best 5 s in the same 26 weeks): the leg-stress side
  (weekly high-speed distance is linked to hamstring injuries in team-sport research);
- stride speed: the middle of a week's strides' best-5-second speeds: the same relaxed-fast effort each time.

Planned strides that day are ticked when strides are found.
"""

import base64
import json
import statistics
import zlib
from datetime import date, timedelta

VERSION = 6                         # bump when the summary changes: older rows are read again
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS tr_run_speed (
        user_id INTEGER NOT NULL, external_id TEXT NOT NULL, day TEXT NOT NULL, version INTEGER DEFAULT 0,
        strides INTEGER DEFAULT 0, efforts_json TEXT DEFAULT '[]', stride_mps REAL, usual_mps REAL, best5_mps REAL,
        hist_json TEXT DEFAULT '{}', bests_json TEXT DEFAULT '{}', checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, external_id))""",
)
MIN_SECONDS, MAX_SECONDS = 6, 2400  # a stride is ~15-30 s; up to 40 minutes (a 10K race)
MERGE_GAP = 3                       # a moment below the line inside one burst (GPS wobble) doesn't split it;
MERGE_SHARE = 0.05                  # in a long effort, a dip up to 5% of its length so far (a turn) doesn't either
FAST_RATIO, FAST_PLUS = 1.35, 1.0   # fast running: above max(1.35 x easy speed, easy + 1 m/s)
WINDOW = 5                          # top speed = best 5 seconds
STRIDE_METRES = 150
STANDARD = (200, 300, 400, 600, 800, 1000, 1200, 1609)
RACES = (3000, 5000, 10000)
NAMES = {1000: "1K", 1609: "mile", 3000: "3K", 5000: "5K", 10000: "10K"}
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
           ("hist_json", "TEXT DEFAULT '{}'"), ("bests_json", "TEXT DEFAULT '{}'"), ("series", "TEXT DEFAULT ''"))


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


def label(metres, seconds=0, run_metres=0):
    """What a fast stretch was. A race distance (3K, 5K, 10K) only when the stretch is most of the run (he records
    races as their own activity): a 5 km tempo inside a longer run is a tempo, not a 5K."""
    if metres < STRIDE_METRES:
        return "stride"
    race = min(RACES, key=lambda d: abs(d - metres))
    if abs(race - metres) <= race * 0.04 and metres >= 0.85 * run_metres:
        return NAMES[race]
    near = min(STANDARD, key=lambda d: abs(d - metres))
    if abs(near - metres) <= near * 0.12:
        return NAMES.get(near, str(near))
    return "tempo" if seconds >= 480 else "rep"


def find_efforts(times, speeds, dists=None, easy=None):
    """([{at, seconds, metres, avg_mps, top_mps, kind}], usual m/s) for each fast stretch in a run.

    Usual speed is the run's median while moving. A stretch is time above max(1.35 x easy, easy + 1 m/s), `easy`
    being his easy speed across runs (the run's own usual when it isn't known yet), lasting MIN_SECONDS to
    MAX_SECONDS. Distance comes from the distance stream when there is one."""
    samples = _samples(times, speeds, dists)
    moving = [v for _, v, _ in samples if v > 1.0]
    if len(moving) < 60:
        return [], None
    usual = statistics.median(moving)
    base = easy or usual
    line = max(base * FAST_RATIO, base + FAST_PLUS)
    known = [d for _, _, d in samples if d is not None]
    run_metres = (known[-1] - known[0]) if len(known) > 1 else 0
    bursts, current = [], None
    for t, v, d in samples:
        if v >= line:
            if current and t - current["end"] <= max(MERGE_GAP, MERGE_SHARE * (current["end"] - current["start"])):
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
                      "avg_mps": round(metres / seconds, 2), "top_mps": round(top, 2),
                      "kind": label(metres, seconds, run_metres)})
    return found, round(usual, 2)


STEADY = 0.75  # a best effort's slowest 15 seconds must be at least 75% of its average speed (no jog or stop inside;
STEADY_SECONDS = 15  # a few slow seconds at a turn or a water stop are fine)


def _rolling(times, speeds, seconds=STEADY_SECONDS):
    """Average speed over the `seconds` before each sample (None where there's no speed)."""
    out, window, total = [], [], 0.0
    for t, v in zip(times, speeds):
        if v is None:
            out.append(None)
            continue
        window.append((t, v))
        total += v
        while window and window[0][0] <= t - seconds:
            total -= window.pop(0)[1]
        out.append(total / len(window))
    return out


def best_efforts(times, dists, speeds=None):
    """{metres: seconds} for the fastest steady stretch of each BEST_DISTANCES distance in the run."""
    rows = [(float(t), float(d), None if v is None else float(v))
            for t, d, v in zip(times or [], dists or [], speeds or [None] * len(times or []))
            if t is not None and d is not None]
    points = [(t, d) for t, d, _ in rows]
    vs = _rolling([t for t, _, _ in rows], [v for _, _, v in rows])  # 15-second averages, for the steady check
    out = {}
    if len(points) < 2:
        return out
    from collections import deque
    for target in BEST_DISTANCES:
        if points[-1][1] - points[0][1] < target:
            break
        # low: indices in [settled, j] with rising 15 s speeds (a sliding minimum). `settled` is 15 s into the
        # window, so the average there doesn't reach back to before the effort (a jog, a standing start).
        best, j, low, settled = None, 0, deque(), 0
        for i in range(len(points)):
            while settled < len(points) and points[settled][0] < points[i][0] + STEADY_SECONDS:
                settled += 1
            while low and low[0] < settled:
                low.popleft()
            while j < len(points) and points[j][1] - points[i][1] < target:
                if vs[j] is not None:
                    while low and vs[low[-1]] >= vs[j]:
                        low.pop()
                    low.append(j)
                j += 1
            if j == len(points):
                break
            span = points[j][0] - points[i][0]
            if speeds and low and span and vs[low[0]] < STEADY * target / span:
                continue  # a jog or a stop inside: not one steady effort
            # time to cover exactly `target` from point i, interpolating inside the last step
            (t0, d0), (t1, d1) = points[j - 1], points[j]
            need = points[i][1] + target
            at = t1 if d1 == d0 else t0 + (t1 - t0) * (need - d0) / (d1 - d0)
            took = at - points[i][0]
            if best is None or took < best:
                best = took
        if best:
            out[str(target)] = round(best, 1)
    # A race GPS measured a touch short (5K as 4,997 m): the whole run counts as that distance, time scaled up.
    total = points[-1][1] - points[0][1]
    for target in BEST_DISTANCES:
        if str(target) not in out and 0.99 * target <= total <= 1.01 * target:
            took = (points[-1][0] - points[0][0]) * target / total
            settled = [v for (t, _), v in zip(points, vs)
                       if points[0][0] + 30 <= t <= points[-1][0] - 5 and v is not None]
            if speeds and settled and min(settled) < STEADY * target / took:
                continue
            out[str(target)] = round(took, 1)
    return out


def histogram(times, speeds):
    """{band: [seconds, metres]} in each 0.05 m/s band from HIST_FROM up (band = speed x 20, rounded down). The
    metres keep the band's exact average speed, so scores near critical speed aren't thrown off by the banding. A gap
    in the recording (paused watch) counts at most 2 seconds."""
    samples = _samples(times, speeds)
    hist = {}
    for (t, v, _), (t_next, _, _) in zip(samples, samples[1:]):
        if v < HIST_FROM:
            continue
        dt = min(t_next - t, 2.0)
        band = hist.setdefault(str(int(v * 20)), [0.0, 0.0])
        band[0] += dt
        band[1] += v * dt
    return {k: [round(s, 1), round(m, 1)] for k, (s, m) in hist.items()}


def top_speed(times, speeds):
    """Best 5 seconds of the whole run (m/s)."""
    samples = _samples(times, speeds)
    best = _best_window([(t, v) for t, v, _ in samples])
    return round(best, 2) if best else None


def stride_speed(efforts):
    """The run's stride speed: the middle of its strides' top speeds (m/s), or None without strides."""
    tops = [e["top_mps"] for e in efforts if e["kind"] == "stride"]
    return round(statistics.median(tops), 2) if tops else None


def _rest(gap):
    return f"{round(gap)} s rest" if gap < 120 else f"{clock(gap)} rest"


def groups(efforts):
    """Fast stretches in order, runs of the same kind together: [{kind, items, rest (seconds, or None)}]."""
    out = []
    for e in efforts:
        if out and out[-1]["kind"] == e["kind"]:
            out[-1]["items"].append(e)
        else:
            out.append({"kind": e["kind"], "items": [e]})
    for g in out:
        items = g["items"]
        gaps = [b["at"] - (a["at"] + a["seconds"]) for a, b in zip(items, items[1:])]
        g["rest"] = statistics.median(gaps) if gaps else None
    return out


def describe(group):
    """One group as he'd write it: "18 × 400 avg 1:22, 30 s rest", "5K in 17:52", "5 strides (4:58/mi)"."""
    kind, items, n = group["kind"], group["items"], len(group["items"])
    rest = f", {_rest(group['rest'])}" if group["rest"] is not None else ""
    if kind == "stride":
        return f"{n} stride{'s' if n != 1 else ''} ({pace(statistics.median(e['top_mps'] for e in items))}/mi)"
    if kind in ("3K", "5K", "10K"):
        target = {"3K": 3000, "5K": 5000, "10K": 10000}[kind]
        return "; ".join(f"{kind} in {clock(e['seconds'] * target / e['metres'])}" for e in items)
    if kind == "tempo":
        return "; ".join(f"tempo {e['metres'] / METERS_PER_MILE:.1f} mi at {pace(e['avg_mps'])}/mi" for e in items)
    if kind == "rep":
        metres = round(statistics.median(e["metres"] for e in items), -1)
        speed = sum(e["metres"] for e in items) / sum(e["seconds"] for e in items)
        if n == 1:
            return f"{int(metres)} m at {pace(speed)}/mi"
        return f"{n} × {int(metres)} m at {pace(speed)}/mi{rest}"
    avg = sum(e["seconds"] for e in items) / n
    distance = kind if kind in ("1K", "mile") else kind  # "400", "mile"
    if n == 1:
        return f"{distance} in {clock(avg)}"
    return f"{n} × {distance} avg {clock(avg)}{rest}"


def summary(efforts):
    """Short words for a run's fast stretches, in order: "18 × 400 avg 1:22, 30 s rest; 5 strides (4:58/mi)"."""
    return "; ".join(describe(g) for g in groups(efforts))


# ---------------------------------------------------------------- the model


def fit_critical_speed(bests):
    """(CS m/s, D' m) from {metres: seconds} best efforts lasting 2-20 minutes (least squares on d = CS·t + D'),
    or (None, None) when there aren't two usable efforts or the fit isn't believable."""
    points = [(t, float(d)) for d, t in bests.items() if t and 120 <= t <= 2400]
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


def _bands(hist):
    """(seconds, metres, average speed) per band."""
    for seconds, metres in hist.values():
        if seconds:
            yield seconds, metres, metres / seconds


def d_prime_used(hist, cs):
    """Metres 'above' critical speed: sum of (speed - CS) x seconds over everything faster than CS."""
    if not cs:
        return 0.0
    return sum(metres - cs * seconds for seconds, metres, v in _bands(hist) if v > cs)


def near_top_metres(hist, vmax):
    """Metres run at 90% of top speed or faster."""
    if not vmax:
        return 0.0
    return sum(metres for seconds, metres, v in _bands(hist) if v >= 0.9 * vmax)


# ---------------------------------------------------------------- reading runs from Strava


LL_STEP = 3   # route points kept: every 3rd sample


def pack(streams):
    """Strava streams -> compact text: times (s), speed (cm/s), distance (dm), heart rate, route every LL_STEP."""
    get = lambda k: (streams.get(k) or {}).get("data") or []
    times, speeds, dists, hr, ll = get("time"), get("velocity_smooth"), get("distance"), get("heartrate"), get("latlng")
    n = len(times)
    series = {"t": [int(t) for t in times],
              "v": [None if v is None else round(v * 100) for v in (speeds + [None] * n)[:n]],
              "d": [None if d is None else round(d * 10) for d in (dists + [None] * n)[:n]],
              "hr": [None if h is None else int(h) for h in (hr + [None] * n)[:n]] if hr else [],
              "ll": [[round(p[0], 5), round(p[1], 5)] if p else None for p in ll[::LL_STEP]] if ll else []}
    return base64.b64encode(zlib.compress(json.dumps(series, separators=(",", ":")).encode(), 9)).decode()


def unpack(text):
    """compact text -> {t, v (m/s), d (m), hr, ll} lists; {} when there's nothing."""
    if not text:
        return {}
    s = json.loads(zlib.decompress(base64.b64decode(text)))
    return {"t": s["t"], "v": [None if v is None else v / 100 for v in s["v"]],
            "d": [None if d is None else d / 10 for d in s["d"]], "hr": s.get("hr") or [], "ll": s.get("ll") or []}


def summarize(series, easy=None):
    """Everything kept about one run, from its series."""
    times, speeds, dists = series.get("t") or [], series.get("v") or [], series.get("d") or []
    efforts, usual = find_efforts(times, speeds, dists, easy)
    return {"strides": sum(1 for e in efforts if e["kind"] == "stride"), "efforts_json": json.dumps(efforts),
            "stride_mps": stride_speed(efforts), "usual_mps": usual, "best5_mps": top_speed(times, speeds),
            "hist_json": json.dumps(histogram(times, speeds)), "bests_json": json.dumps(best_efforts(times, dists, speeds))}


def easy_speed(db, user_id, today=None):
    """His easy speed: the middle of the slower half of his runs' usual speeds in the last FIT_WEEKS (None if fewer
    than 5 runs are read)."""
    ensure(db)
    since = ((today or date.today()) - timedelta(weeks=FIT_WEEKS)).isoformat()
    usual = sorted(r[0] for r in db.connect().execute(
        "SELECT usual_mps FROM tr_run_speed WHERE user_id = ? AND day >= ? AND usual_mps > 0",
        (user_id, since)).fetchall())
    return statistics.median(usual[:max(1, len(usual) // 2)]) if len(usual) >= 5 else None


def _store(db, user_id, external_id, day, packed, easy):
    row = {"user_id": user_id, "external_id": str(external_id), "day": str(day)[:10], "version": VERSION,
           "series": packed, **summarize(unpack(packed), easy)}
    conn = ensure(db)
    conn.execute(f"INSERT OR REPLACE INTO tr_run_speed ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                 list(row.values()))
    return conn, row


def analyze(sync, user_id, external_id, day):
    """Fetch one run's streams, keep them and their summary, tick planned strides. Returns the row."""
    from .strava import API

    data = sync.call(f"{API}/activities/{external_id}/streams?keys=time,velocity_smooth,distance,heartrate,latlng"
                     "&key_by_type=true", token=sync._token(user_id))
    conn, row = _store(sync.db, user_id, external_id, day, pack(data if isinstance(data, dict) else {}),
                       easy_speed(sync.db, user_id))
    efforts = json.loads(row["efforts_json"])
    strides = [e for e in efforts if e["kind"] == "stride"]
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


def refresh(db, user_id):
    """Work out again every run whose streams are kept but whose summary is older (no Strava needed)."""
    ensure(db)
    rows = db.connect().execute("SELECT external_id, day, series FROM tr_run_speed WHERE user_id = ? AND version < ? "
                                "AND series != ''", (user_id, VERSION)).fetchall()
    easy = easy_speed(db, user_id)
    for r in rows:
        _store(db, user_id, r["external_id"], r["day"], r["series"], easy)
    if rows:
        db._commit()
    return len(rows)


def fill(sync, user_id, limit=5):
    """Read a few runs not read yet, newest first. Stops quietly on Strava trouble (tries again next time)."""
    from .strava import StravaError

    sync._conn()  # the log's extra columns
    refresh(sync.db, user_id)
    done = 0
    for row in _waiting(sync.db, user_id, limit):
        try:
            analyze(sync, user_id, row["external_id"], row["day"])
        except StravaError as exc:
            if "429" in str(exc):
                break
            if "404" in str(exc):
                # No streams to read (no GPS, or gone from Strava): mark it read with nothing in it, so it isn't
                # asked for again on every visit and the weeks around it can get their line.
                _save_empty(sync.db, user_id, row["external_id"], row["day"])
                done += 1
                continue
            print(f"Warning: couldn't read speed for run {row['external_id']}: {exc}")
            continue
        done += 1
    return done


def _save_empty(db, user_id, external_id, day):
    conn = ensure(db)
    conn.execute("INSERT OR REPLACE INTO tr_run_speed (user_id, external_id, day, version) VALUES (?, ?, ?, ?)",
                 (user_id, str(external_id), str(day)[:10], VERSION))
    db._commit()


# ---------------------------------------------------------------- week by week


def _rows(db, user_id, since):
    ensure(db)
    rows = db.connect().execute("SELECT * FROM tr_run_speed WHERE user_id = ? AND day >= ? ORDER BY day",
                                (user_id, since.isoformat())).fetchall()
    out = []
    for r in rows:
        out.append({"id": r["external_id"], "day": date.fromisoformat(r["day"]),
                    "efforts": json.loads(r["efforts_json"] or "[]"),
                    "hist": json.loads(r["hist_json"] or "{}"), "bests": json.loads(r["bests_json"] or "{}"),
                    "best5": r["best5_mps"], "usual": r["usual_mps"]})
    return out


TYPICAL_D_PRIME = 150          # m: a trained distance runner's D' (used when there's no hard short effort)
LONG, SHORT = (600, 2400), (120, 420)
DISTANCE_NAMES = {"400": "400 m", "800": "800 m", "1000": "1K", "1609": "mile", "3000": "3K", "5000": "5K",
                  "10000": "10K"}


def _effort_name(e):
    metres, _, day, source = e
    what = DISTANCE_NAMES.get(str(int(metres)), f"{int(metres)} m")
    return f"your {what} race time" if source == "race" else f"your {what} on {day:%b} {day.day}"


HARD = 1.45   # an effort counts toward critical speed only at 1.45 x his usual easy speed or faster


def estimate(efforts):
    """(CS, D', how) from {metres: (seconds, day, source)}; source is "race" (entered) or "run".

    Long effort L (10-40 min): the one giving the highest CS with a typical D'. Short effort S (2-7 min): the one
    furthest above that line (the most evidence of a bigger reserve). L and S together give both CS and D' when the
    answer is believable (D' 80-400 m); otherwise CS from L with the typical D'. Without a long effort, the full fit
    over whatever there is, if believable."""
    longs = [(float(d), t, day, src) for d, (t, day, src) in efforts.items() if LONG[0] <= t <= LONG[1]]
    shorts = [(float(d), t, day, src) for d, (t, day, src) in efforts.items() if SHORT[0] <= t <= SHORT[1]]
    if longs:
        long = max(longs, key=lambda e: (e[0] - TYPICAL_D_PRIME) / e[1])
        cs = (long[0] - TYPICAL_D_PRIME) / long[1]
        if shorts:
            short = max(shorts, key=lambda e: e[0] - cs * e[1])
            cs2 = (long[0] - short[0]) / (long[1] - short[1])
            dp2 = short[0] - cs2 * short[1]
            if 80 <= dp2 <= 400 and 2.5 <= cs2 <= 7.5:
                return round(cs2, 3), round(dp2), f"from {_effort_name(long)} and {_effort_name(short)}"
        if 2.5 <= cs <= 7.5:
            return round(cs, 3), TYPICAL_D_PRIME, f"from {_effort_name(long)}, with a typical D′"
    cs, dp = fit_critical_speed({d: t for d, (t, _, _) in efforts.items()})
    return (cs, dp, "from your best efforts") if cs else (None, None, "")


def model_at(runs, end, races=None):
    """CS, D' and top speed from the FIT_WEEKS of runs up to `end`, and his race times if he entered any."""
    start = end - timedelta(weeks=FIT_WEEKS)
    window = [r for r in runs if start < r["day"] <= end]
    efforts = {d: (t, end, "race") for d, t in (races or {}).items() if t}
    usual = sorted(r["usual"] for r in window if r.get("usual"))
    # His easy speed: the middle of the slower half of his runs (a race's own "usual" is race pace).
    easy = statistics.median(usual[:max(1, len(usual) // 2)]) if usual else None
    for r in window:
        for d, t in r["bests"].items():
            if not t or (d in efforts and t >= efforts[d][0]):
                continue
            if easy and float(d) / t < HARD * easy:
                continue  # not a hard effort: an easy or steady stretch
            efforts[d] = (t, r["day"], "run")
    cs, d_prime, how = estimate(efforts)
    vmax = max((r["best5"] or 0 for r in window), default=0) or None
    return {"cs": cs, "d_prime": d_prime, "vmax": vmax, "how": how}


def weekly(db, user_id, week_starts, races=None):
    """Per week (Monday -> numbers): critical speed and D' as of that week, speed work (D' used, tanks), top-speed
    metres, strides and stride speed, and the fast stretches by kind."""
    if not week_starts:
        return {}
    runs = _rows(db, user_id, week_starts[0] - timedelta(weeks=FIT_WEEKS + CHRONIC_WEEKS))
    lead = [week_starts[0] - timedelta(weeks=n) for n in range(CHRONIC_WEEKS, 0, -1)]
    out = {}
    for w in lead + list(week_starts):
        end = w + timedelta(days=6)
        model = model_at(runs, end, races)
        week_runs = [r for r in runs if w <= r["day"] <= end]
        per_run = [(d_prime_used(r["hist"], model["cs"]), r) for r in week_runs]
        used = sum(u for u, _ in per_run)
        top_run = max(per_run, key=lambda x: x[0], default=(0, None))[1]
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
                  "cs_how": model["how"],
                  "vmax": model["vmax"], "speed_work": round(used) if model["cs"] else None,
                  "tanks": round(used / model["d_prime"], 1) if model["cs"] and model["d_prime"] else None,
                  "near_top": round(near), "strides": len(strides), "stride_mps": stride_mps,
                  "stride_pace": pace(stride_mps), "kinds": kinds, "read": len(week_runs),
                  "cause": ({"day": top_run["day"], "id": top_run["id"], "summary": summary(top_run["efforts"])}
                            if top_run and top_run["efforts"] else None)}
    unread = {date.fromisoformat(r["day"]) for r in _waiting(db, user_id, 10000)}
    for w in week_starts:
        before = [out[w - timedelta(weeks=n)] for n in range(1, CHRONIC_WEEKS + 1)]
        week = out[w]
        # The line needs the 4 weeks before (and this one) read; until then it would be wrongly low.
        week["line_ready"] = not any(w - timedelta(weeks=CHRONIC_WEEKS) <= d <= w + timedelta(days=6) for d in unread)
        work_usual = sum(b["speed_work"] or 0 for b in before) / CHRONIC_WEEKS
        top_usual = sum(b["near_top"] for b in before) / CHRONIC_WEEKS
        week["work_line"] = (round(max(RATIO * work_usual, FLOOR_TANKS * week["d_prime"])) if week["d_prime"] else None)
        week["top_line"] = round(max(RATIO * top_usual, FLOOR_NEAR_TOP))
        if not week["line_ready"]:
            week["work_line"] = week["top_line"] = None
        week["work_over"] = bool(week["work_line"] and (week["speed_work"] or 0) > week["work_line"])
        week["top_over"] = bool(week["top_line"] and week["near_top"] > week["top_line"])
        ratios = [r for r in ((week["speed_work"] or 0) / week["work_line"] if week["work_line"] else 0,
                              week["near_top"] / week["top_line"] if week["top_line"] else 0)]
        week["times_line"] = round(max(ratios), 1)
        week["how_over"] = ("well over" if week["times_line"] >= 1.3 else "a little over") \
            if (week["work_over"] or week["top_over"]) else ""
    return {w: out[w] for w in week_starts}


def latest(db, user_id):
    """The most recent run with fast stretches: {day, id, summary}, or None."""
    runs = recent(db, user_id, limit=1, fast_only=True)
    return runs[0] if runs else None


def recent(db, user_id, limit=10, fast_only=False):
    """His latest runs with what was fast in them: [{id, day, title, miles, summary}]."""
    ensure(db)
    rows = db.connect().execute(
        f"""SELECT s.external_id, s.day, s.efforts_json, w.title, w.distance_meters
            FROM tr_run_speed s LEFT JOIN trainer_imported_workouts w
              ON w.user_id = s.user_id AND w.external_id = s.external_id AND w.source = 'strava'
            WHERE s.user_id = ? {"AND s.efforts_json NOT IN ('[]', '')" if fast_only else ""}
            ORDER BY s.day DESC LIMIT ?""", (user_id, limit)).fetchall()
    return [{"id": r["external_id"], "day": r["day"], "title": r["title"] or "Run",
             "miles": round((r["distance_meters"] or 0) / METERS_PER_MILE, 1),
             "summary": summary(json.loads(r["efforts_json"] or "[]"))} for r in rows]
