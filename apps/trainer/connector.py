"""Trainer tools for Claude, ChatGPT and Dieter's own chat (core/connector): this week's plan, the training log's
totals, whether Strava is syncing, one run in detail, and his speed picture (threshold pace, speed points against the
safe limit, hardest efforts over the year). Read-only for now; plan changes and injury check-ins come with
docs/trainer_analysis_plan.md."""

from datetime import date, timedelta

from core.connector.tools import Tool, ToolError, ToolSet, site_url
from database import get_current_user_id

from . import clock, plan, routes, runpage, speed, strava

CHAT_NOTES = """Trainer: Curtis's running (trainer_ tools; read-only). trainer_run is one run (by run_id, or every run on a
date: a race day often has a warm-up, the race and a cool-down), trainer_speed his speed picture over the weeks. Use
the words his pages use (explain them only if he asks; the explainer page is trainer.speed_how): threshold pace = about
the fastest pace he could hold for half an hour, worked out from his hardest efforts in the last 6 months (sports
scientists call it critical speed); speed load = speed points = metres he got ahead of a pacer running at threshold
pace (faster running scores more per second; slower than threshold scores nothing); speed fitness = his average
weekly speed load over the 4 weeks before (what his legs are used to); speed advantage = the ground he can gain on
threshold pace in one all-out effort (D', in metres; sets race predictions and coach paces); safe limit = 2 x
speed fitness, never below 2 x speed advantage; "a little over" up to 1.5x the limit, "well over" past that; near-sprint running =
metres at 90% of top speed or faster. Distances are what his watch measured: trust them (a race reading 3.0 miles was
a short course). For "how did my race go" or "am I getting faster": compare it with his earlier hard efforts
(trainer_speed hard_efforts_last_year; pace for pace, and holding a pace longer is better), say where its speed points
sit against that week's safe limit, and how his threshold pace has moved. Use the tools' numbers, briefly; never
invent numbers."""


AI_OFF_FOR_SHARED = ("Trainer's AI tools are only on for Dieter's owner for now. Runs here come from Strava, and "
                     "Strava's API terms don't allow its data to be used with AI, so for shared accounts Trainer "
                     "shows its numbers on its own pages instead (docs/trainer_sharing_plan.md).")


def ai_allowed(user_id):
    """Guardrail (docs/trainer_sharing_plan.md): Strava-sourced runs only reach an AI (Claude, ChatGPT, Dieter's chat)
    for the owner's own account. Friends and coaches see Trainer's formulas on its pages, never through these tools."""
    db = routes._state.get("db")
    row = db.get_user_by_id(user_id) if db and user_id else None
    row = dict(row) if row else {}
    return row.get("role") == "admin" and row.get("status", "active") == "active"


def _user():
    user = get_current_user_id()
    if not user:
        raise ToolError("Sign in first.")
    if not ai_allowed(user):
        raise ToolError(AI_OFF_FOR_SHARED)
    return user


def sync_status(args):
    user = _user()
    status = routes.sync().status(get_current_user_id())
    profile = routes._state["db"].get_trainer_profile(user)
    profile = dict(profile) if profile else {}
    conn = routes._state["db"].connect()
    recent = conn.execute("SELECT received_at, aspect_type, status, error FROM tr_strava_events ORDER BY id DESC "
                          "LIMIT 5").fetchall()
    newest = strava.log(routes._state["db"], user, limit=1)
    return {"strava_connected": bool(profile.get("strava_refresh_token")),
            "live_sync": status["live"], "events_waiting": status["pending"],
            "recent_events": [dict(r) for r in recent],
            "history_import": {"activities_imported": status["history_count"], "finished": status["history_done"]},
            "sessions_in_log": strava.summary(routes._state["db"], user)["total"],
            "newest_session": newest[0] if newest else None,
            "url": site_url("/apps/trainer/log")}


def log_totals(args):
    totals = strava.summary(routes._state["db"], _user(), weeks=int(args.get("weeks") or 12))
    totals.pop("recent", None)
    return totals


def this_week(args):
    _user()
    view = routes.store().week_view(args.get("week") or clock.today())
    return {"week": view["label"], "totals": view["totals"], "goals": view["week"]["goals"],
            "days": [{"date": d["date"], "sessions": [{"id": s["id"], "kind": s["kind"], "title": s["title"],
                                                       "miles": s["miles"], "time": s["slot"], "status": s["status"],
                                                       "strides": s["strides"], "strides_done": bool(s["strides_done"])}
                                                      for s in d["sessions"]],
                      "strava": d["strava"], "fueled": bool(d["habits"]["fueled"])}
                     for d in view["days"]],
            "url": site_url(f"/apps/trainer?week={view['start']}")}


def _races():
    return plan.race_bests(routes.store().settings())


def _run(user, external_id, races):
    r = runpage.detail(routes._state["db"], user, external_id, races=races)
    if not r:
        return None
    return {"run_id": r["id"], "title": r["title"], "date": r["day"].isoformat(), "start_time": r["time"],
            "miles": r["miles"], "moving_time": r["moving"], "pace_per_mile": r["pace"],
            "avg_heart_rate": r["avg_hr"], "max_heart_rate": r["max_hr"],
            "what_was_fast": [g["text"] for g in r["groups"]], "speed_points": r["speed_work"],
            "threshold_pace_then": r["cs_pace"], "top_speed_pace": r["top_pace"],
            "fast_stretches": [{"what": e["kind"], "metres": e["metres"], "time": e["time"], "pace": e["pace"],
                                "rest_after": e["rest_text"]} for e in r["efforts"]][:30],
            "best_steady_efforts": r["bests"], "details_read": r["has_series"],
            "url": site_url(f"/apps/trainer/run/{r['id']}")}


def run(args):
    """One run by run_id, or every run on a date, with that week's speed points and safe limit."""
    user = _user()
    db, races = routes._state["db"], _races()
    run_id, day = str(args.get("run_id") or "").strip(), str(args.get("date") or "").strip()
    if run_id:
        if not run_id.isdigit():
            raise ToolError("run_id is the number from trainer_speed or the run page's address.")
        one = _run(user, run_id, races)
        if not one:
            raise ToolError("That run isn't in his log.")
        runs = [one]
    else:
        try:
            on = date.fromisoformat(day) if day else clock.today()
        except ValueError:
            raise ToolError("date should look like 2026-11-26.") from None
        ids = [r[0] for r in db.connect().execute(
            "SELECT external_id FROM trainer_imported_workouts WHERE user_id = ? AND source = 'strava' AND "
            "workout_category LIKE 'run%' AND COALESCE(strava_deleted_at, '') = '' AND "
            "substr(COALESCE(NULLIF(started_at_local, ''), started_at), 1, 10) = ? ORDER BY started_at",
            (user, on.isoformat())).fetchall()]
        runs = [x for x in (_run(user, i, races) for i in ids) if x]
        if not runs:
            return {"date": on.isoformat(), "runs": [], "note": "No runs in his log that day."}
    monday = date.fromisoformat(runs[0]["date"])
    monday -= timedelta(days=monday.weekday())
    week = speed.weekly(db, user, [monday], races)[monday]
    return {"runs": runs, "that_week": {"monday": monday.isoformat(), "speed_points": week["speed_work"],
                                        "safe_limit": week["work_line"], "over": week["how_over"] or "not over",
                                        "times_the_limit": week["times_line"], "near_sprint_metres": week["near_top"],
                                        "near_sprint_safe_limit": week["top_line"]}}


def speed_picture(args):
    """Threshold pace now and week by week, speed points against the safe limit, recent runs, hardest efforts."""
    user = _user()
    db, races = routes._state["db"], _races()
    weeks = max(4, min(int(args.get("weeks") or 16), 52))
    monday = clock.today() - timedelta(days=clock.today().weekday())
    starts = [monday - timedelta(weeks=n) for n in range(weeks - 1, -1, -1)]
    by_week = speed.weekly(db, user, starts, races)
    now = by_week[monday]
    return {"threshold_pace_now": now["cs_pace"], "threshold_pace_from": now["cs_how"],
            "weeks": [{"monday": w.isoformat(), "speed_points": v["speed_work"], "safe_limit": v["work_line"],
                       "over": v["how_over"] or "", "times_the_limit": v["times_line"] if v["how_over"] else None,
                       "near_sprint_metres": v["near_top"], "threshold_pace": v["cs_pace"],
                       "threshold_pace_from": v["cs_how"],
                       "biggest_session": v["cause"]["summary"] if v["cause"] else None, "runs_read": v["read"]}
                      for w, v in by_week.items()],
            "recent_runs": [{"run_id": r["id"], "date": r["day"], "title": r["title"], "miles": r["miles"],
                             "what_was_fast": r["summary"], "speed_points": r["points"]}
                            for r in speed.recent(db, user, limit=10, races=races)],
            "hard_efforts_last_year": speed.hard_efforts(db, user, races=races),
            "runs_still_to_read": speed.waiting_count(db, user),
            "url": site_url("/apps/trainer")}


def tools():
    return ToolSet("trainer", [
        Tool("trainer_sync_status", "Is Strava syncing?",
             "Whether Strava is connected and live sync is on, recent Strava events and their outcome, how far the "
             "history import got, and the newest session in his log.", sync_status, {}, read_only=True),
        Tool("trainer_log_totals", "Training log totals",
             "His training log as weekly (last N weeks) and monthly totals: run miles, runs, weights, Pilates, hours.",
             log_totals, {"weeks": {"type": "integer", "minimum": 1, "maximum": 52}}, read_only=True),
        Tool("trainer_this_week", "This week's training",
             "The planned week (runs with miles, strides, weights, Pilates; status), what Strava recorded each day, "
             "fueling, totals against his miles target, and the week's goals. week: any date in the week.",
             this_week, {"week": {"type": "string", "description": "A date like 2026-11-30; default this week."}},
             read_only=True),
        Tool("trainer_run", "One run in detail",
             "A run from Strava's second-by-second data: miles, pace, heart rate, what was fast (reps, strides, a "
             "race), its speed points, best steady efforts, and that week's speed points against the safe limit. "
             "run_id (from trainer_speed or the run page), or date for every run that day (default today).",
             run, {"run_id": {"type": "string", "description": "The run's number, like 21133954975."},
                   "date": {"type": "string", "description": "A date like 2026-11-26."}}, read_only=True),
        Tool("trainer_speed", "His speed picture",
             "Threshold pace now and week by week (and which efforts it came from), weekly speed points against the "
             "safe limit, recent runs with their speed points, and his hardest efforts of 10+ minutes in the last "
             "year (for progression). weeks: how many weeks back (default 16).",
             speed_picture, {"weeks": {"type": "integer", "minimum": 4, "maximum": 52}}, read_only=True),
    ])
