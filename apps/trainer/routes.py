"""Trainer's new pages (docs/trainer_plan.md): This week, the Sunday check-in, his usual week, a session, and the
calendar feed his phone subscribes to.

The older Trainer pages (history, Strava imports, workouts, coaching, settings) are still in api.py with the old
look; they move here when they're rebuilt.
"""

import csv
import io
import json
import os
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from core import ui
from database import get_current_user_id, reset_current_user_id, set_current_user_id

from . import plan, strava
from .manifest import APP

router = APIRouter()
_state = {"db": None}
_templates = ui.templates(Path(__file__).parent / "templates")
BASE = "/apps/trainer"
REPORT_URL = BASE + "/feedback"
TABS = (
    ("This week", BASE),
    ("Sunday check-in", BASE + "/checkin"),
    ("Usual week", BASE + "/usual"),
    ("Log", BASE + "/log"),
    ("History", BASE + "/history"),
    ("Strava", BASE + "/imports"),
)


def configure(db):
    _state["db"] = db


def store():
    return plan.PlanStore(_state["db"])


def sync():
    return strava.Sync(_state["db"], os.getenv("STRAVA_CLIENT_ID", ""), os.getenv("STRAVA_CLIENT_SECRET", ""))


def catch_up(limit=5):
    """Handle Strava events still waiting (if the background step after the webhook was cut short). Quiet on errors."""
    try:
        s = sync()
        if s.status()["pending"]:
            s.process(limit)
    except Exception as exc:
        print(f"Warning: Strava catch-up failed: {exc!r}")


def render(name, **context):
    context.setdefault("app", APP)
    context.setdefault("tabs", TABS)
    context.setdefault("report_url", REPORT_URL)
    context.setdefault("kinds", plan.KINDS)
    context.setdefault("statuses", plan.STATUSES)
    return HTMLResponse(_templates.get_template(name).render(**context))


def back(url, fallback=BASE):
    return RedirectResponse(url if url and url.startswith("/") and not url.startswith("//") else fallback,
                            status_code=303)


def _start(week):
    try:
        return plan.week_start(week) if week else plan.week_start(date.today())
    except ValueError:
        return plan.week_start(date.today())


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except plan.Invalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# ---------------------------------------------------------------- This week


@router.get(BASE)
def this_week(request: Request, week: str = ""):
    catch_up()
    start = _start(week)
    view = store().week_view(start)
    return render("trainer_week.html", title="This week", view=view, current=BASE,
                  page_url=f"{BASE}?week={view['start']}", this_monday=plan.week_start(date.today()).isoformat())


@router.post(BASE + "/week/fill")
def fill_week(week: str = Form(...), replace: str = Form("")):
    _guard(store().fill_week, _start(week), replace=bool(replace))
    return back(f"{BASE}?week={_start(week).isoformat()}")


@router.post(BASE + "/check")
def check(key: str = Form(...), done: str = Form(""), week: str = Form("")):
    """Check rows on This week: "s-<id>" a session done, "x-<id>" its strides done."""
    kind, _, raw = key.partition("-")
    if kind not in ("s", "x") or not raw.isdigit():
        raise HTTPException(status_code=400, detail="Unknown item.")
    if kind == "s":
        _guard(store().update_session, int(raw), status="done" if done else "planned")
    else:
        _guard(store().update_session, int(raw), strides_done=1 if done else 0)
    return back(f"{BASE}?week={week}" if week else BASE)


@router.post(BASE + "/habits")
def habits(day: str = Form(...), water: str = Form(""), fueled: str = Form(""), week: str = Form("")):
    if water in ("+1", "-1"):
        _guard(store().set_habit, day, add_water=int(water))
    if fueled in ("yes", "no"):
        _guard(store().set_habit, day, fueled=fueled == "yes")
    return back(f"{BASE}?week={week}#day-{day}" if week else BASE)


@router.post(BASE + "/sessions/add")
def add_session(day: str = Form(...), kind: str = Form(...), title: str = Form(""), miles: str = Form(""),
                slot: str = Form(""), details: str = Form(""), strides: str = Form("0"), week: str = Form("")):
    _guard(store().add_session, day, kind, title, miles or None, slot, details, strides or 0)
    return back(f"{BASE}?week={week or plan.week_start(day).isoformat()}")


@router.get(BASE + "/sessions/{session_id}")
def session_page(request: Request, session_id: int):
    session = _guard(store().session, session_id)
    return render("trainer_session.html", title=session["title"] or session["kind_label"], session=session,
                  current=BASE, page_url=f"{BASE}/sessions/{session_id}",
                  week=plan.week_start(session["day"]).isoformat())


@router.post(BASE + "/sessions/{session_id}")
def save_session(session_id: int, day: str = Form(...), kind: str = Form(...), title: str = Form(""),
                 miles: str = Form(""), slot: str = Form(""), details: str = Form(""), strides: str = Form("0"),
                 status: str = Form("planned"), note: str = Form("")):
    session = _guard(store().update_session, session_id, day=day, kind=kind, title=title, miles=miles or None,
                     slot=slot, details=details, strides=strides or 0, status=status, note=note)
    return back(f"{BASE}?week={plan.week_start(session['day']).isoformat()}")


@router.post(BASE + "/sessions/{session_id}/delete")
def delete_session(session_id: int):
    session = _guard(store().session, session_id)
    store().delete_session(session_id)
    return back(f"{BASE}?week={plan.week_start(session['day']).isoformat()}")


# ---------------------------------------------------------------- Sunday check-in


@router.get(BASE + "/checkin")
def checkin_page(request: Request, saved: int = 0):
    today = date.today()
    coming = plan.next_week_start(today) if today.weekday() >= 4 else plan.week_start(today)  # Fri-Sun: plan next week
    last = coming - timedelta(days=7)
    return render("trainer_checkin.html", title="Sunday check-in", current=BASE + "/checkin",
                  page_url=BASE + "/checkin", last=store().week_view(last), coming=store().week_view(coming),
                  saved=saved)


@router.post(BASE + "/checkin")
def save_checkin(last_week: str = Form(...), coming_week: str = Form(...), review: str = Form(""),
                 miles_target: str = Form(""), goal1: str = Form(""), goal2: str = Form(""), goal3: str = Form(""),
                 draft: str = Form("")):
    s = store()
    _guard(s.save_week, _start(last_week), review=review)
    _guard(s.save_week, _start(coming_week), miles_target=miles_target, goals=[goal1, goal2, goal3], checked_in=True)
    if draft:
        _guard(s.fill_week, _start(coming_week), replace=draft == "replace")
    return back(f"{BASE}/checkin?saved=1")


# ---------------------------------------------------------------- his usual week, and the calendar link


@router.get(BASE + "/usual")
def usual_page(request: Request, renewed: int = 0):
    token = store().calendar_token()
    host = request.headers.get("host") or "askdieter.ai"
    return render("trainer_usual.html", title="Usual week", current=BASE + "/usual", page_url=BASE + "/usual",
                  settings=store().settings(), days=plan.DAYS, day_names=plan.DAY_NAMES,
                  feed_url=f"https://{host}/trainer-calendar/{token}.ics",
                  webcal_url=f"webcal://{host}/trainer-calendar/{token}.ics", renewed=renewed)


@router.post(BASE + "/usual")
def save_usual(miles: str = Form(...), workout_day: str = Form(...), workout_time: str = Form(""),
               long_day: str = Form(...), rest_day: str = Form(""), weights: str = Form("2"),
               pilates: str = Form("2"), strides: str = Form("2"), strides_text: str = Form(""),
               water_goal: str = Form("8"), run_time: str = Form(""), gym_time: str = Form("")):
    _guard(store().save_settings, miles=miles, workout_day=workout_day, workout_time=workout_time, long_day=long_day,
           rest_day=rest_day, weights=weights, pilates=pilates, strides=strides, strides_text=strides_text or None,
           water_goal=water_goal, run_time=run_time, gym_time=gym_time)
    return back(BASE + "/usual")


@router.post(BASE + "/calendar/renew")
def renew_calendar():
    store().calendar_token(renew=True)
    return back(BASE + "/usual?renewed=1")


@router.get("/trainer-calendar/{token}.ics")
def calendar_feed(request: Request, token: str):
    """Public on purpose (calendar apps can't sign in): the long random token is the key. He can renew it."""
    user = store().user_for_token(token)
    if not user:
        raise HTTPException(status_code=404, detail="Not found")
    reset = set_current_user_id(user)
    try:
        s = store()
        today = date.today()
        first, last = plan.week_start(today) - timedelta(days=14), plan.week_start(today) + timedelta(days=27)
        sessions = s.sessions_between(first, last)
        weeks = [(plan.week_start(today) + timedelta(days=7 * n)).isoformat() for n in range(0, 3)]
        body = plan.calendar(sessions, weeks=weeks, host=request.headers.get("host") or "askdieter.ai")
    finally:
        reset_current_user_id(reset)
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "no-store", "Content-Disposition": 'inline; filename="dieter-trainer.ics"'})


# ---------------------------------------------------------------- Strava, live (apps/trainer/strava.py)


@router.get("/strava/webhook")
def strava_webhook_check(request: Request):
    """Strava checks the callback when the subscription is made: echo hub.challenge if the token is ours."""
    q = request.query_params
    answer = sync().challenge(q.get("hub.mode"), q.get("hub.verify_token"), q.get("hub.challenge"))
    if not answer:
        raise HTTPException(status_code=403, detail="Not ours.")
    return JSONResponse(answer)


@router.post("/strava/webhook")
async def strava_webhook(request: Request):
    """A new, changed or deleted Strava activity. Written down now (Strava wants an answer in 2 s), handled after."""
    try:
        event = await request.json()
    except ValueError:
        event = None
    s = sync()
    if s.receive(event):
        # Usually handled right here (one Strava call); anything left waits for the next page view (catch_up).
        try:
            await run_in_threadpool(s.process, 1)
        except Exception as exc:
            print(f"Warning: Strava event not handled yet: {exc!r}")
    return {"ok": True}


def _is_admin(request):
    user = getattr(request.state, "current_user", None) or {}
    return (user.get("role") if isinstance(user, dict) else None) == "admin"


@router.post(BASE + "/sync/live")
def turn_on_live_sync(request: Request):
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Only Dieter's admin can turn on live sync.")
    host = request.headers.get("host") or "askdieter.ai"
    try:
        sync().subscribe(f"https://{host}/strava/webhook")
    except strava.StravaError as exc:
        return back(BASE + "/log?problem=" + str(exc)[:200].replace("&", " "))
    return back(BASE + "/log?live=1")


@router.post(BASE + "/sync/history")
def import_history():
    """One chunk of his Strava history per tap (a few hundred activities), done while he waits.

    Cloud Run only gives a request CPU until it has answered, so work left for after the answer can stall; each tap
    does a bounded chunk and the page says how far it got.
    """
    user = get_current_user_id()
    s = sync()
    try:
        stored = s.import_history(user, pages=3)
        s.fill_details(user, limit=5)
    except strava.StravaError as exc:
        return back(BASE + "/log?problem=" + str(exc)[:200].replace("&", " "))
    return back(BASE + f"/log?history={stored}")


@router.post(BASE + "/sync/now")
def sync_now():
    user = get_current_user_id()
    s = sync()
    try:
        s.process(10)
        s.fill_details(user, limit=10)
    except strava.StravaError as exc:
        return back(BASE + "/log?problem=" + str(exc)[:200].replace("&", " "))
    return back(BASE + "/log")


# ---------------------------------------------------------------- his training log


@router.get(BASE + "/log")
def log_page(request: Request, kind: str = "", year: str = "", live: int = 0, history: int = -1, problem: str = ""):
    catch_up()
    user = get_current_user_id()
    totals = strava.summary(_state["db"], user)
    profile = _state["db"].get_trainer_profile(user)
    connected = bool(profile and dict(profile).get("strava_refresh_token"))
    return render("trainer_log.html", title="Training log", current=BASE + "/log", page_url=BASE + "/log",
                  totals=totals, status=sync().status(), connected=connected,
                  is_admin=_is_admin(request), live=live, history=history, problem=problem,
                  today=date.today().isoformat())


@router.post(BASE + "/log/add")
def log_add(day: str = Form(...), activity_type: str = Form(...), title: str = Form(""), minutes: str = Form(""),
            miles: str = Form(""), notes: str = Form("")):
    try:
        strava.add_manual(_state["db"], get_current_user_id(), day, activity_type, title, minutes or None,
                          miles or None, notes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return back(BASE + "/log")


@router.get(BASE + "/log.csv")
def log_csv(kind: str = "", year: str = ""):
    rows = strava.log(_state["db"], get_current_user_id(), kind=kind, year=year or None, include_deleted=True,
                      limit=100000)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=strava.CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="dieter-training-log.csv"'})


@router.get(BASE + "/log.json")
def log_json(kind: str = "", year: str = ""):
    rows = strava.log(_state["db"], get_current_user_id(), kind=kind, year=year or None, include_deleted=True,
                      limit=100000)
    return Response(json.dumps(rows, indent=1), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="dieter-training-log.json"'})


# ---------------------------------------------------------------- Report a problem


@router.post(REPORT_URL)
def feedback(request: Request, text: str = Form(...), page: str = Form(BASE)):
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Describe the problem.")
    user = getattr(request.state, "current_user", None) or {}
    _state["db"].add_app_feedback_report(title=f"[Trainer] {text.splitlines()[0][:80]}", area="Trainer",
                                         page_url=page[:300], page_title="Trainer",
                                         reporter_name=user.get("name", "") or "",
                                         reporter_email=user.get("email", "") or "", raw_feedback=text[:4000])
    return back(page)
