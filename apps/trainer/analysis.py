"""Trainer's Overview numbers (docs/trainer_analysis_plan.md, first pass), worked out from his training log.

Everything here is an estimate from Strava's numbers and is labelled that way on the page:
- weekly run miles against his target;
- load per session: minutes weighted by heart rate (a TRIMP-style score: harder minutes count more), or by
  minutes and kind of session when there's no heart rate;
- fitness (42-day average load), fatigue (7-day average load), form (fitness minus fatigue), as in the
  Banister / "performance management" model;
- aerobic efficiency: easy-run speed per heartbeat (metres per minute ÷ average HR), week by week;
- top speed: the fastest moment Strava saw on each week's runs (GPS max speed: noisy, so the best of several weeks
  matters more than one number);
- speed (speed.py): critical speed and D', speed work (D' used), top-speed running and stride speed, week by week,
  from each run's second-by-second data.
"""

import math
from datetime import date, timedelta

from . import speed

METERS_PER_MILE = 1609.344
FITNESS_DAYS, FATIGUE_DAYS = 42, 7
NO_HR_WEIGHT = {"run": 1.0, "run_threshold": 1.6, "run_speed": 1.6, "bike": 0.7, "bike_tempo": 1.0,
                "strength": 0.6, "strength_glutes": 0.6, "pilates": 0.4, "yoga": 0.3}


def _rows(db, user_id, since):
    return [dict(r) for r in db.connect().execute(
        """SELECT started_at, workout_category, distance_meters, moving_time_seconds, average_heartrate,
                  max_heartrate, max_speed_mps, average_speed_mps
           FROM trainer_imported_workouts
           WHERE user_id = ? AND started_at >= ? AND COALESCE(strava_deleted_at, '') = ''
           ORDER BY started_at""", (user_id, since.isoformat())).fetchall()]


def _max_hr(rows):
    """His working max heart rate: the 98th percentile of recorded maxima (one spike doesn't set it)."""
    peaks = sorted(r["max_heartrate"] for r in rows if r.get("max_heartrate"))
    if len(peaks) < 5:
        return 190.0
    return float(peaks[min(len(peaks) - 1, int(len(peaks) * 0.98))])


def load(row, max_hr):
    """One session's load. With HR: minutes × intensity × e^(1.92 × intensity), intensity = avg HR / max HR (a
    simplified TRIMP that doesn't need resting HR). Without HR: minutes × a weight for the kind of session."""
    minutes = (row.get("moving_time_seconds") or 0) / 60
    hr = row.get("average_heartrate")
    if hr and max_hr:
        x = max(0.0, min(1.0, hr / max_hr))
        return minutes * x * 0.64 * math.exp(1.92 * x)
    return minutes * NO_HR_WEIGHT.get(row.get("workout_category") or "", 0.5) * 0.5


def _week(day):
    return day - timedelta(days=day.weekday())


def overview(db, user_id, miles_target=40.0, weeks=16, today=None, races=None):
    today = today or date.today()
    this_monday = _week(today)
    first_week = this_monday - timedelta(weeks=weeks - 1)
    since = first_week - timedelta(days=FITNESS_DAYS * 3)  # enough lead-in for the averages to settle
    rows = _rows(db, user_id, since)
    max_hr = _max_hr(rows)

    # Daily load, then fitness / fatigue as exponentially weighted averages.
    daily = {}
    for r in rows:
        try:
            day = date.fromisoformat(str(r["started_at"])[:10])
        except ValueError:
            continue
        r["day"] = day
        daily[day] = daily.get(day, 0.0) + load(r, max_hr)
    fitness = fatigue = 0.0
    series, day = {}, since
    while day <= today:
        value = daily.get(day, 0.0)
        fitness += (value - fitness) / FITNESS_DAYS
        fatigue += (value - fatigue) / FATIGUE_DAYS
        series[day] = (fitness, fatigue)
        day += timedelta(days=1)

    week_starts = [first_week + timedelta(weeks=n) for n in range(weeks)]
    runs = [r for r in rows if "day" in r and (r["workout_category"] or "").startswith("run")]
    by_week = {w: [r for r in runs if _week(r["day"]) == w] for w in week_starts}

    miles = [{"week": w.isoformat(), "label": f"{w.strftime('%b')} {w.day}", "current": w == this_monday,
              "miles": round(sum((r["distance_meters"] or 0) for r in by_week[w]) / METERS_PER_MILE, 1),
              "runs": len(by_week[w])} for w in week_starts]

    def efficiency(week_runs):
        easy = [r for r in week_runs if r["workout_category"] == "run" and r.get("average_heartrate")
                and r.get("average_speed_mps") and (r.get("moving_time_seconds") or 0) >= 1200]
        if not easy:
            return None
        return round(sum(r["average_speed_mps"] * 60 / r["average_heartrate"] for r in easy) / len(easy), 3)

    def pace(mps):
        if not mps:
            return ""
        seconds = METERS_PER_MILE / mps
        return f"{int(seconds // 60)}:{int(round(seconds % 60)):02d}"

    strides = speed.weekly(db, user_id, week_starts, races)
    weekly = []
    for w in week_starts:
        top = max((r["max_speed_mps"] or 0 for r in by_week[w]), default=0)
        f = series.get(min(w + timedelta(days=6), today), (0.0, 0.0))
        weekly.append({"week": w.isoformat(), "label": f"{w.strftime('%b')} {w.day}",
                       "efficiency": efficiency(by_week[w]), "top_mps": top or None, "top_pace": pace(top),
                       "fitness": round(f[0], 1), **strides[w]})

    now_fit, now_fat = series.get(today, (0.0, 0.0))
    form = now_fit - now_fat
    month_ago = series.get(today - timedelta(days=28), (0.0, 0.0))[0]
    recent_top = max((w["top_mps"] or 0 for w in weekly[-4:]), default=0)
    before_top = max((w["top_mps"] or 0 for w in weekly[:-4]), default=0)
    eff_recent = [w["efficiency"] for w in weekly[-4:] if w["efficiency"]]
    eff_before = [w["efficiency"] for w in weekly[-12:-4] if w["efficiency"]]
    avg = lambda xs: sum(xs) / len(xs) if xs else None
    stride_recent = [w["stride_mps"] for w in weekly[-4:] if w["stride_mps"]]
    stride_before = [w["stride_mps"] for w in weekly[-12:-4] if w["stride_mps"]]
    s_recent, s_before = avg(stride_recent), avg(stride_before)

    def gain(now, then):
        """Seconds per mile faster (+) or slower (-)."""
        return round(METERS_PER_MILE / then - METERS_PER_MILE / now) if now and then else None

    this_week, past4 = weekly[-1], weekly[-5:-1]
    cs_then = weekly[-13]["cs"] if len(weekly) >= 13 else None  # 12 weeks before this one
    work_usual = avg([w["speed_work"] for w in past4 if w["speed_work"] is not None])
    near_usual = avg([w["near_top"] for w in past4])

    return {
        "miles": miles, "miles_target": miles_target, "chart_max": max([m["miles"] for m in miles] + [miles_target]) or 1,
        "fitness": {
            "fitness": round(now_fit, 1), "fatigue": round(now_fat, 1), "form": round(form, 1),
            "fitness_change": round(now_fit - month_ago, 1),
            "form_label": ("fresh" if form > 5 else "balanced" if form > -10 else "building (tired)" if form > -25
                           else "very fatigued"),
            "max_hr": int(max_hr),
            "series": [w["fitness"] for w in weekly],
        },
        "speed": {
            "top_pace_recent": pace(recent_top), "top_pace_before": pace(before_top),
            "faster": bool(recent_top and before_top and recent_top > before_top),
            "efficiency_recent": round(avg(eff_recent), 3) if eff_recent else None,
            "efficiency_before": round(avg(eff_before), 3) if eff_before else None,
            "efficiency_up": bool(eff_recent and eff_before and avg(eff_recent) > avg(eff_before)),
            "stride_pace_recent": pace(s_recent), "stride_pace_before": pace(s_before),
            "stride_gain": gain(s_recent, s_before),
            "cs_pace": this_week["cs_pace"], "d_prime": this_week["d_prime"], "cs_then_pace": pace(cs_then),
            "cs_gain": gain(this_week["cs"], cs_then),
            "speed_work": this_week["speed_work"], "tanks": this_week["tanks"],
            "speed_work_usual": round(work_usual) if work_usual is not None else None,
            "near_top": this_week["near_top"], "near_top_usual": round(near_usual) if near_usual is not None else None,
            "work_line": this_week["work_line"], "work_over": this_week["work_over"],
            "top_line": this_week["top_line"], "top_over": this_week["top_over"],
            "over_weeks": [{"week": w["week"], "label": w["label"], "times": w["times_line"], "how": w["how_over"],
                            "cause": w["cause"]}
                           for w in weekly if w["work_over"] or w["top_over"]],
            "recent": speed.recent(db, user_id, limit=8, races=races),
            "work_max": max([w["speed_work"] or 0 for w in weekly] + [w["work_line"] or 0 for w in weekly] + [1]),
            "top_max": max([w["near_top"] for w in weekly] + [w["top_line"] or 0 for w in weekly] + [1]),
            "latest": speed.latest(db, user_id), "waiting": speed.waiting_count(db, user_id),
            "from_races": bool(races), "line_ready": this_week["line_ready"], "cs_how": this_week["cs_how"],
            "weeks": weekly,
        },
    }


def week_guidance(view, today=None):
    """Plain-English lines about this week: what's done, what's left, and what to fit in."""
    today = today or date.today()
    t = view["totals"]
    lines = []
    target, run = t["miles_target"] or 0, t["miles_run"]
    days_left = sum(1 for d in view["days"] if d["date"] >= today.isoformat())
    if target:
        left = max(0.0, round(target - run, 1))
        if left <= 0:
            lines.append(f"Mileage done: {run} of {target:g} miles. Anything more is a bonus; keep it easy.")
        else:
            lines.append(f"{run} of {target:g} miles so far: {left:g} to go"
                         + (f" over {days_left} day{'s' if days_left != 1 else ''}." if days_left else "."))
    workouts = [(d, s) for d in view["days"] for s in d["sessions"] if s["kind"] == "workout"]
    for d, s in workouts:
        if s["done"]:
            lines.append(f"Hard workout done ({d['name']}).")
        elif d["date"] < today.isoformat():
            ran = any(a["is_run"] for a in d["strava"])
            lines.append(f"{d['name']}'s hard workout isn't checked off" +
                         (" (Strava has a run that day): tick it on This week if that was it." if ran
                          else ". Moved or skipped? Change it on This week so the plan stays honest."))
        else:
            lines.append(f"Hard workout {d['name']}" + (f" at {s['slot']}" if s["slot"] else "") +
                         ". Fuel and drink beforehand.")
    done, planned = t["strides"]
    if planned and done < planned:
        lines.append(f"Strides {done} of {planned}: add them to the end of an easy run.")
    for kind, label in (("weights", "Weights"), ("pilates", "Pilates")):
        done, planned = t[kind]
        if planned and done < planned:
            lines.append(f"{label} {done} of {planned} this week.")
    if t["water_days"] < sum(1 for d in view["days"] if d["is_past"] or d["is_today"]):
        lines.append("Water: tap +1 on This week as you go, so hydration shows in the Sunday check-in.")
    return lines
