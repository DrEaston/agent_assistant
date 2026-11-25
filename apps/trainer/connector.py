"""Trainer tools for Claude and ChatGPT (core/connector): this week's plan, the training log's totals, and whether
Strava is syncing. Read-only for now; plan changes and injury check-ins come with docs/trainer_analysis_plan.md."""

from datetime import date

from core.connector.tools import Tool, ToolError, ToolSet, site_url
from database import get_current_user_id

from . import routes, strava


def _user():
    user = get_current_user_id()
    if not user:
        raise ToolError("Sign in first.")
    return user


def sync_status(args):
    user = _user()
    status = routes.sync().status()
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
    view = routes.store().week_view(args.get("week") or date.today())
    return {"week": view["label"], "totals": view["totals"], "goals": view["week"]["goals"],
            "days": [{"date": d["date"], "sessions": [{"id": s["id"], "kind": s["kind"], "title": s["title"],
                                                       "miles": s["miles"], "time": s["slot"], "status": s["status"],
                                                       "strides": s["strides"], "strides_done": bool(s["strides_done"])}
                                                      for s in d["sessions"]],
                      "strava": d["strava"], "water": d["habits"]["water"], "fueled": bool(d["habits"]["fueled"])}
                     for d in view["days"]],
            "url": site_url(f"/apps/trainer?week={view['start']}")}


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
             "water and fueling, totals against his miles target, and the week's goals. week: any date in the week.",
             this_week, {"week": {"type": "string", "description": "A date like 2026-11-30; default this week."}},
             read_only=True),
    ])
