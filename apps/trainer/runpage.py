"""One run, in detail (/apps/trainer/run/<strava id>): what was fast in it, a route map and a speed chart drawn from
its kept streams (speed.py), the speed work it did, and its best steady efforts. Drawings are plain SVG made here;
no map service is called (the route is drawn on its own, without a street map underneath)."""

import json
import math
import statistics
from datetime import date, timedelta

from . import speed

W, H = 600, 170          # speed chart
MAP_W = 600              # route map width (height follows the route's shape)


def _workout(db, user_id, external_id):
    row = db.connect().execute(
        "SELECT * FROM trainer_imported_workouts WHERE user_id = ? AND source = 'strava' AND external_id = ?",
        (user_id, str(external_id))).fetchone()
    return dict(row) if row else None


def speed_chart(series, efforts, cs):
    """{points, marks, cs_y} for a W x H chart of speed over time (one point per pixel column, averaged)."""
    pairs = [(t, v) for t, v in zip(series.get("t") or [], series.get("v") or []) if v is not None]
    if len(pairs) < 2:
        return None
    end = pairs[-1][0] or 1
    top = min(max(v for _, v in pairs), 8.0) * 1.05 or 1
    columns = {}
    for t, v in pairs:
        columns.setdefault(min(W - 1, int(t / end * W)), []).append(min(v, top))
    y = lambda v: round(H - v / top * (H - 6), 1)
    points = " ".join(f"{x},{y(sum(vs) / len(vs))}" for x, vs in sorted(columns.items()))
    marks = [{"x": round(e["at"] / end * W, 1), "w": max(1.5, round(e["seconds"] / end * W, 1))} for e in efforts]
    return {"points": points, "marks": marks, "cs_y": y(cs) if cs and cs < top else None, "w": W, "h": H,
            "minutes": round(end / 60)}


def _projection(series):
    """(w, h, {route point index: (x, y)}) for the route, MAP_W wide, or None without a route."""
    pts = [(i, p) for i, p in enumerate(series.get("ll") or []) if p]
    if len(pts) < 2:
        return None
    lat0 = statistics.fmean(p[0] for _, p in pts)
    k = math.cos(math.radians(lat0))
    xy = [(i, p[1] * k, -p[0]) for i, p in pts]
    min_x, max_x = min(x for _, x, _ in xy), max(x for _, x, _ in xy)
    min_y, max_y = min(y for _, _, y in xy), max(y for _, _, y in xy)
    span = max(max_x - min_x, max_y - min_y) or 1e-9
    scale = (MAP_W - 20) / span
    h = round((max_y - min_y) * scale + 20)
    w = round((max_x - min_x) * scale + 20)
    return w, h, {i: (round((x - min_x) * scale + 10, 1), round((y - min_y) * scale + 10, 1)) for i, x, y in xy}


HEAT_STEPS = 9          # colours on the speed heatmap: cyan (easy) .. magenta (threshold pace) .. yellow (top speed)


def heat_step(v, easy, cs, top):
    """0..HEAT_STEPS-1 for a speed: easy pace or slower is 0, threshold pace the middle, top speed the last."""
    last = HEAT_STEPS - 1
    if not v or not easy or not top or top <= easy:
        return 0
    mid = cs if cs and easy < cs < top else (easy + top) / 2
    if v <= mid:
        f = max(0.0, (v - easy) / (mid - easy)) / 2
    else:
        f = 0.5 + min(1.0, (v - mid) / (top - mid)) / 2
    return round(f * last)


HEAT_TOP = 1.35         # yellow from about mile pace (1.35 x threshold speed): his all-out sprints would squash
                        # reps and races into one colour


def heat_top(cs, vmax):
    """The heatmap's fast end: about mile pace, or his top speed if that's lower (or there's no threshold pace)."""
    if cs and vmax:
        return min(vmax, HEAT_TOP * cs)
    return vmax or (HEAT_TOP * cs if cs else None)


def heat_key(easy, cs, top):
    """The key under the map: paces at the slow end, threshold pace (if known) and the fast end."""
    if not easy or not top or top <= easy:
        return None
    return {"steps": list(range(HEAT_STEPS)), "easy": speed.pace(easy), "top": speed.pace(top) + "/mi and faster",
            "threshold": speed.pace(cs) if cs and easy < cs < top else ""}


def _heat(series, proj, easy, cs, top):
    """[{step, points}]: the route in runs of one colour, each route point coloured by the speed since the last."""
    times, speeds = series.get("t") or [], series.get("v") or []
    keys = sorted(proj)
    out, run, current = [], [], None
    for a, b in zip(keys, keys[1:]):
        vs = [v for v in speeds[a * speed.LL_STEP:b * speed.LL_STEP + 1] if v is not None]
        step = heat_step(sum(vs) / len(vs) if vs else 0, easy, cs, top)
        if step != current and run:
            out.append({"step": current, "points": " ".join(f"{x},{y}" for x, y in run)})
            run = [run[-1]]
        if not run:
            run = [proj[a]]
        run.append(proj[b])
        current = step
    if len(run) > 1:
        out.append({"step": current, "points": " ".join(f"{x},{y}" for x, y in run)})
    return out


def route_map(series, efforts, easy=None, cs=None, top=None):
    """{w, h, path, fast, heat, key, start, finish} for the route: coloured by speed when his easy and top speeds
    are known (heat, key), otherwise with the fast stretches drawn over it (fast)."""
    projected = _projection(series)
    if not projected:
        return None
    w, h, proj = projected
    heat = _heat(series, proj, easy, cs, top) if heat_key(easy, cs, top) else []
    times = series.get("t") or []
    path = " ".join(f"{x},{y}" for x, y in proj.values())
    fast = []
    for e in ([] if heat else efforts):
        seg = [proj[i] for i in proj
               if i * speed.LL_STEP < len(times) and e["at"] <= times[i * speed.LL_STEP] <= e["at"] + e["seconds"]]
        if len(seg) > 1:
            fast.append(" ".join(f"{x},{y}" for x, y in seg))
    first, last = list(proj.values())[0], list(proj.values())[-1]
    return {"w": w, "h": h, "path": path, "fast": fast, "heat": heat,
            "key": heat_key(easy, cs, top) if heat else None, "start": first, "finish": last}


EXPLORE_POINTS = 2400    # samples sent to the Explore page at most (a 2-hour run: one every 3 seconds)
SMOOTH = 5               # pace on the Explore page: averaged over 5 seconds (a single GPS reading jumps around)


def explore(db, user_id, external_id, races=None):
    """The Explore page's data: the run page's numbers plus, sample by sample, time, distance, 5-second speed,
    heart rate and the spot on the map, for the page's script to draw and follow a finger along."""
    r = detail(db, user_id, external_id, races)
    if not r:
        return None
    row = db.connect().execute("SELECT series FROM tr_run_speed WHERE user_id = ? AND external_id = ?",
                               (user_id, str(external_id))).fetchone()
    series = speed.unpack(row["series"] if row else "")
    times, speeds = series.get("t") or [], series.get("v") or []
    dists, hrs = series.get("d") or [], series.get("hr") or []
    n = len(times)
    if n < 2:
        return dict(r, samples=None)
    # 5-second average speed at each sample (from the distance stream where there is one: steadier than speed)
    smooth = []
    j = 0
    for i in range(n):
        while times[i] - times[j] > SMOOTH:
            j += 1
        if i > j and dists[i] is not None and dists[j] is not None and times[i] > times[j]:
            smooth.append((dists[i] - dists[j]) / (times[i] - times[j]))
        else:
            smooth.append(speeds[i] or 0.0)
    projected = _projection(series)
    proj = projected[2] if projected else {}
    keys = sorted(proj)
    step = max(1, math.ceil(n / EXPLORE_POINTS))
    out = {"t": [], "d": [], "v": [], "hr": [], "x": [], "y": []}
    k = 0
    for i in range(0, n, step):
        out["t"].append(times[i])
        out["d"].append(round(dists[i], 1) if dists[i] is not None else None)
        out["v"].append(round(min(smooth[i], 9.0), 2))
        out["hr"].append(hrs[i] if i < len(hrs) else None)
        if keys:
            while k + 1 < len(keys) and keys[k + 1] * speed.LL_STEP <= i:
                k += 1
            x, y = proj[keys[k]]
            out["x"].append(x)
            out["y"].append(y)
    cs = None
    if r.get("cs_pace"):
        minutes, seconds = r["cs_pace"].split(":")
        cs = round(speed.METERS_PER_MILE / (int(minutes) * 60 + int(seconds)), 3)
    stretches = [{"n": e["n"], "at": e["at"], "seconds": e["seconds"], "label": f"{e['metres']:,} m in {e['time']}"}
                 for e in r["efforts"]]
    return dict(r, samples=out, cs=cs, stretches=stretches,
                map_size=[projected[0], projected[1]] if projected else None)


def detail(db, user_id, external_id, races=None):
    workout = _workout(db, user_id, external_id)
    speed.ensure(db)
    row = db.connect().execute("SELECT * FROM tr_run_speed WHERE user_id = ? AND external_id = ?",
                               (user_id, str(external_id))).fetchone()
    if not workout and not row:
        return None
    workout = workout or {}
    row = dict(row) if row else {}
    series = speed.unpack(row.get("series") or "")
    efforts = json.loads(row.get("efforts_json") or "[]")
    for a, b in zip(efforts, efforts[1:]):
        a["rest"] = b["at"] - (a["at"] + a["seconds"])
    day = date.fromisoformat(row.get("day") or str(workout.get("started_at") or "")[:10] or date.today().isoformat())
    runs = speed._rows(db, user_id, day - timedelta(weeks=speed.FIT_WEEKS))
    model = speed.model_at(runs, day, races)
    hist = json.loads(row.get("hist_json") or "{}")
    used = speed.d_prime_used(hist, model["cs"]) if model["cs"] else None
    hr = [h for h in series.get("hr") or [] if h]
    distance = workout.get("distance_meters") or 0
    moving = workout.get("moving_time_seconds") or 0
    bests = json.loads(row.get("bests_json") or "{}")
    return {
        "id": str(external_id), "title": workout.get("title") or "Run", "day": day,
        "time": (workout.get("started_at_local") or "")[11:16],
        "miles": round(distance / speed.METERS_PER_MILE, 2), "moving": speed.clock(moving) if moving else "",
        "pace": speed.pace(distance / moving) if distance and moving else "",
        "avg_hr": round(workout["average_heartrate"]) if workout.get("average_heartrate") else None,
        "max_hr": max(hr) if hr else (round(workout["max_heartrate"]) if workout.get("max_heartrate") else None),
        "groups": [dict(g, text=speed.describe(g)) for g in speed.groups(efforts)],
        "efforts": [dict(e, n=i + 1, pace=speed.pace(e["avg_mps"]), top_pace=speed.pace(e["top_mps"]),
                         time=speed.clock(e["seconds"]), rest_text=speed.clock(e["rest"]) if e.get("rest") else "")
                    for i, e in enumerate(efforts)],
        "speed_work": round(used) if used is not None else None,
        "tanks": round(used / model["d_prime"], 1) if used and model["d_prime"] else None,
        "cs_pace": speed.pace(model["cs"]), "top_pace": speed.pace(row.get("best5_mps")),
        "bests": [{"name": speed.DISTANCE_NAMES.get(d, d), "time": speed.clock(t), "pace": speed.pace(float(d) / t)}
                  for d, t in sorted(bests.items(), key=lambda x: float(x[0]))],
        "chart": speed_chart(series, efforts, model["cs"]),
        "map": route_map(series, efforts, easy=speed.easy_speed(db, user_id, day), cs=model["cs"],
                         top=heat_top(model["cs"], model["vmax"] or row.get("best5_mps"))),
        "has_series": bool(series),
        "strava_url": f"https://www.strava.com/activities/{external_id}",
    }
