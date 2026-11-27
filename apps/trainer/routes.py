"""Trainer's new pages (docs/trainer_plan.md): This week, the Sunday check-in, his usual week, a session, and the
calendar feed his phone subscribes to.

The older Trainer pages (history, Strava imports, workout library, upcoming/past, settings, the old coach mode)
were removed in Nov 2026; Strava's connect/callback/disconnect endpoints are still in api.py.
"""

import csv
import io
import json
import os
from datetime import date, timedelta
from pathlib import Path

import contextvars

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from core import ui
from database import get_current_user_id, reset_current_user_id, set_current_user_id

from . import analysis, clock, coach, coach_reader, plan, runpage, shoes, speed, strava
from .manifest import APP

_state = {"db": None}
VIEW_COOKIE = "trainer_view_as"
_viewing = contextvars.ContextVar("trainer_viewing", default=None)
# Pages that always belong to the person signed in, even while a coach is viewing an athlete's Trainer.
OWN_PAGES = ("/apps/trainer/athletes", "/apps/trainer/my-coach", "/apps/trainer/coach-invite",
             "/apps/trainer/coaching-links", "/apps/trainer/view-as", "/strava/", "/trainer-calendar/",
             "/coach")   # the coach page and its forms: sending a workout works while viewing an athlete


async def view_as(request: Request):
    """Temporary full view (coach.FULL_VIEW_DAYS): a coach the owner allowed sees the owner's Trainer pages as the
    owner, read-only. Nothing else changes: any other page or form stays the coach's own, and forms are refused."""
    raw = request.cookies.get(VIEW_COOKIE, "")
    path = request.url.path
    if not raw.isdigit() or path.startswith(OWN_PAGES) or not get_current_user_id():
        return
    athlete_id = int(raw)
    if not coach.CoachStore(_state["db"]).full_view(athlete_id):
        return
    if request.method not in ("GET", "HEAD"):
        raise HTTPException(status_code=403, detail="You're viewing someone else's Trainer: it's read-only.")
    name = coach.CoachStore(_state["db"])._name(athlete_id)
    _viewing.set({"athlete_id": athlete_id, "name": name})
    set_current_user_id(athlete_id)   # this request only (the sign-in middleware resets it afterwards)


router = APIRouter(dependencies=[Depends(view_as)])
_templates = ui.templates(Path(__file__).parent / "templates")
BASE = "/apps/trainer"
REPORT_URL = BASE + "/feedback"
TABS = (
    ("Overview", BASE),
    ("This week", BASE + "/week"),
    ("Sunday check-in", BASE + "/checkin"),
    ("Usual week", BASE + "/usual"),
    ("Log", BASE + "/log"),
    ("Shoes", BASE + "/shoes"),
)


def configure(db):
    _state["db"] = db


def store():
    return plan.PlanStore(_state["db"])


def shoe_store():
    return shoes.ShoeStore(_state["db"])


def today():
    """Trainer's Today card: today's sessions, and runs waiting for the after-run check-in."""
    card = store().today()
    try:
        card["after_run"] = shoe_store().count_waiting()
    except Exception as exc:   # the card must never break Today
        print(f"Warning: after-run count failed: {exc!r}")
        card["after_run"] = 0
    try:
        new = coaching().new_for_me()
        card["coach_new"] = {"count": len(new), "first": new[0]} if new else None
    except Exception as exc:
        print(f"Warning: coach workouts for Today failed: {exc!r}")
        card["coach_new"] = None
    return card


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
    context.setdefault("trainer_viewing", _viewing.get())
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
        return plan.week_start(week) if week else plan.week_start(clock.today())
    except ValueError:
        return plan.week_start(clock.today())


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except plan.Invalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# ---------------------------------------------------------------- This week


@router.get(BASE)
def overview(request: Request, week: str = ""):
    """The landing page: this week at a glance, weekly mileage, fitness and speed. (?week=... is This week.)"""
    if week:
        return this_week(request, week)
    catch_up()
    s = store()
    view = s.week_view(clock.today())
    numbers = analysis.overview(_state["db"], get_current_user_id(), miles_target=view["totals"]["miles_target"],
                                races=plan.race_bests(s.settings()))
    user = get_current_user_id()
    profile = _state["db"].get_trainer_profile(user)
    connected = bool(profile and dict(profile).get("strava_refresh_token"))
    history_done = sync().history_state(user)[0] if connected else True
    getting = {"on": connected and (not history_done or numbers["speed"]["waiting"] > 0),
               "history_done": history_done, "left": numbers["speed"]["waiting"]}
    sh = shoe_store()
    # Coach workouts with the paces Dieter picked: new ones for the athlete; for a coach in full view (temporary),
    # the ones coming up, so they can see the paces.
    model = _my_model()
    c = coaching()
    coach_new = (c.new_for_me() if not _viewing.get()
                 else [w for w in c.for_me() if w["day"] >= clock.today().isoformat()][:5])
    for w in coach_new:
        w["sets"] = coach.targets(w["sets"], model)
    return render("trainer_overview.html", title="Trainer", current=BASE, page_url=BASE, view=view, coach_new=coach_new,
                  guidance=analysis.week_guidance(view), o=numbers, getting=getting, after_run=sh.waiting(),
                  shoe_list=sh.shoes(include_retired=False), feels=shoes.FEELS,
                  today=[s for d in view["days"] if d["is_today"] for s in d["sessions"]])


@router.get(BASE + "/run/{external_id}")
def run_page(external_id: str):
    """One run in detail: what was fast, the route map and speed graph together (slide along the graph to read any
    moment, zoom in), speed work, best steady efforts."""
    if not external_id.isdigit():
        raise HTTPException(status_code=404, detail="No such run.")
    s = store()
    r = runpage.explore(_state["db"], get_current_user_id(), external_id, races=plan.race_bests(s.settings()))
    if not r:
        raise HTTPException(status_code=404, detail="That run isn't in your log.")
    sh = shoe_store()
    workout_id = sh.workout_id(external_id)
    checkin_run = sh._run(_state["db"].connect().execute("SELECT * FROM trainer_imported_workouts WHERE id = ?",
                                                         (workout_id,)).fetchone()) if workout_id else None
    return render("trainer_run.html", title=r["title"], r=r, current=BASE, page_url=f"{BASE}/run/{external_id}",
                  checkin_run=checkin_run, saved=sh.for_run(workout_id) if workout_id else None,
                  shoe_list=sh.shoes(include_retired=False), feels=shoes.FEELS)


@router.get(BASE + "/run/{external_id}/explore")
def run_explore(external_id: str):
    """The old Explore page: its map and graph are on the run page now, so old links land there."""
    if not external_id.isdigit():
        raise HTTPException(status_code=404, detail="No such run.")
    return RedirectResponse(f"{BASE}/run/{external_id}#explore", status_code=301)


@router.get(BASE + "/speed/how")
def speed_how():
    """How these numbers work: threshold pace, speed points, the safe limit, near-sprint running, in plain words."""
    e = speed.explain(_state["db"], get_current_user_id(), races=plan.race_bests(store().settings()))
    return render("trainer_speed_how.html", title="How these numbers work", e=e, current=BASE,
                  page_url=f"{BASE}/speed/how")


@router.get(BASE + "/speed/how/math")
def speed_math():
    """The math behind the Speed section: formulas, his own values, and the sources."""
    e = speed.explain(_state["db"], get_current_user_id(), races=plan.race_bests(store().settings()))
    return render("trainer_speed_math.html", title="The math", e=e, current=BASE, page_url=f"{BASE}/speed/how/math")


@router.get(BASE + "/speed/{monday}")
def speed_week(monday: str):
    """One week's runs with their speed points, the week's total and its safe limit."""
    try:
        start = plan.week_start(date.fromisoformat(monday))
    except ValueError:
        raise HTTPException(status_code=404, detail="No such week.") from None
    week = speed.week_runs(_state["db"], get_current_user_id(), start, races=plan.race_bests(store().settings()))
    label = f"Week of {start:%b} {start.day}"
    return render("trainer_speed_week.html", title=label, label=label, w=week, start=start,
                  prev=(start - timedelta(weeks=1)).isoformat(), next=(start + timedelta(weeks=1)).isoformat(),
                  current=BASE, page_url=f"{BASE}/speed/{start.isoformat()}")


@router.get(BASE + "/week")
def this_week(request: Request, week: str = ""):
    catch_up()
    start = _start(week)
    view = store().week_view(start)
    return render("trainer_week.html", title="This week", view=view, current=BASE + "/week",
                  page_url=f"{BASE}?week={view['start']}", this_monday=plan.week_start(clock.today()).isoformat())


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
def habits(day: str = Form(...), fueled: str = Form(""), week: str = Form("")):
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


# ---------------------------------------------------------------- Shoes and the after-run check-in (shoes.py)


def _shoes(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except shoes.Invalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get(BASE + "/shoes")
def shoes_page(request: Request):
    sh = shoe_store()
    _shoes(sh._user)
    return render("trainer_shoes.html", title="Shoes", current=BASE + "/shoes", page_url=BASE + "/shoes",
                  shoes=sh.shoes(), active=sh.shoes(include_retired=False), waiting=sh.waiting(limit=10),
                  feels=shoes.FEELS)


@router.post(BASE + "/shoes/add")
def add_shoe(name: str = Form(""), brand: str = Form(""), model: str = Form(""), start_miles: str = Form("0")):
    _shoes(shoe_store().add, name, brand, model, start_miles)
    return back(BASE + "/shoes")


@router.post(BASE + "/shoes/{shoe_id}/retire")
def retire_shoe(shoe_id: int, retired: str = Form("")):
    _shoes(shoe_store().retire, shoe_id, bool(retired))
    return back(BASE + "/shoes")


@router.post(BASE + "/after-run/{workout_id}")
def after_run(workout_id: int, shoe_id: str = Form(""), feel: str = Form(""), note: str = Form(""),
              skip: str = Form(""), next: str = Form("")):
    """The after-run check-in: a tap on a pair saves it with how it felt and the note; Skip stops asking."""
    sh = shoe_store()
    if skip:
        _shoes(sh.skip, workout_id)
    else:
        _shoes(sh.save, workout_id, int(shoe_id) if shoe_id.isdigit() else None, feel, note)
    return back(next, BASE)


# ---------------------------------------------------------------- Coach mode (apps/trainer/coach.py)
# The athlete's pages (my-coach) show their own target times; the coach's pages (athletes) show only what the coach
# wrote and what the athlete answered. Coach pages never read the athlete's runs.


def coaching():
    return coach.CoachStore(_state["db"])


def _coach(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (coach.Invalid, plan.Invalid) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


def _my_model():
    return coach.athlete_model(_state["db"], get_current_user_id(), races=plan.race_bests(store().settings()))


@router.get(BASE + "/my-coach")
def my_coach(request: Request):
    c = coaching()
    _coach(c._user)
    model = _my_model()
    workouts = c.for_me()
    for w in workouts:
        w["sets"] = coach.targets(w["sets"], model)
    invite = c.open_invite()
    invite_url = f"{str(request.base_url).rstrip('/')}{BASE}/coach-invite/{invite['token']}" if invite else ""
    return render("trainer_my_coach.html", title="My coach", current=BASE + "/my-coach", page_url=BASE + "/my-coach",
                  coaches=c.my_coaches(), workouts=workouts, invite=invite, invite_url=invite_url,
                  has_model=bool(model.get("cs")), is_owner=c.is_owner(), full_view_days=coach.FULL_VIEW_DAYS)


@router.post(BASE + "/my-coach/invite")
def my_coach_invite():
    _coach(coaching().invite)
    return back(BASE + "/my-coach#invite")


@router.post(BASE + "/coaching-links/{link_id}/end")
def end_coaching(link_id: int, next: str = Form("")):
    _coach(coaching().end, link_id)
    return back(next, BASE + "/my-coach")


@router.post(BASE + "/my-coach/workouts/{prescription_id}/take")
def take_workout(prescription_id: int):
    session = _coach(coaching().take, prescription_id, _my_model())
    return back(f"{BASE}?week={plan.week_start(session['day']).isoformat()}#day-{session['day']}")


@router.post(BASE + "/my-coach/workouts/{prescription_id}/decline")
def decline_workout(prescription_id: int, note: str = Form("")):
    _coach(coaching().decline, prescription_id, note)
    return back(BASE + "/my-coach")


@router.post(BASE + "/my-coach/workouts/{prescription_id}/done")
def workout_done(prescription_id: int, note: str = Form("")):
    _coach(coaching().done, prescription_id, note)
    return back(BASE + "/my-coach")


@router.get(BASE + "/coach-invite/{token}")
def coach_invite_page(request: Request, token: str):
    c = coaching()
    _coach(c._user)
    try:
        info, problem = c.invite_info(token), ""
    except coach.Invalid as exc:
        info, problem = None, str(exc)
    return render("trainer_coach_invite.html", title="Coach invite", current=BASE + "/athletes",
                  page_url=BASE + "/athletes", info=info, problem=problem, token=token)


@router.post(BASE + "/coach-invite/{token}")
def coach_invite_accept(token: str):
    info = _coach(coaching().accept, token)
    return back(f"{BASE}/athletes/{info['athlete_id']}")


def _coach_home():
    """Where the Coach page lives for whoever is signed in: /coach for the passcode coach account."""
    row = _state["db"].get_user_by_id(get_current_user_id())
    return "/coach" if row and row["role"] == coach.PASSCODE_ROLE else BASE + "/athletes"


def _athlete_model(athlete_id):
    """The athlete's speed model, worked out for a coach. Only called while the athlete has full view on for
    this coach (temporary; to be blocked for Strava-sourced data before Trainer is shared more widely)."""
    token = set_current_user_id(athlete_id)
    try:
        return coach.athlete_model(_state["db"], athlete_id, races=plan.race_bests(store().settings()))
    finally:
        reset_current_user_id(token)


@router.get(BASE + "/athletes")
def my_athletes(request: Request, athlete: int = 0):
    """The coach's landing page: pick an athlete to open their profile, and send a workout."""
    c = coaching()
    _coach(c._user)
    athletes = c.my_athletes()
    chosen = athlete if any(a["athlete_id"] == athlete for a in athletes) else (
        athletes[0]["athlete_id"] if athletes else 0)
    return render("trainer_athletes.html", title="Coach", current=BASE + "/athletes", page_url=_coach_home(),
                  athletes=athletes, chosen=chosen, coach_home=_coach_home(),
                  intensities=coach.INTENSITIES, distances=[(m, coach.distance_name(m)) for m in coach.DISTANCES],
                  set_rows=range(3), tomorrow=(clock.today() + timedelta(days=1)).isoformat())


def coach_page(request: Request, sent: int = 0, error: str = "", workout: str = "", day: str = ""):
    """/coach (api.py serves it there for the passcode coach): pick an athlete and open their Trainer, read-only;
    write a workout and send it to all athletes; what was sent lately."""
    c = coaching()
    return render("trainer_coach.html", title="Coach", current="/coach", page_url="/coach",
                  athletes=c.my_athletes(), recent=c.sent_by_me(), sent=sent, coach_home="/coach", report_url=None,
                  error=error, workout=workout, tomorrow=day or (clock.today() + timedelta(days=1)).isoformat())


def coach_sign_in_page(error="", signed_in_as=""):
    """The passcode box at /coach (api.py serves it to anyone not signed in as the coach)."""
    return render("trainer_coach_signin.html", title="Coach", error=error, signed_in_as=signed_in_as,
                  page_url="/coach", report_url=None)


@router.post("/coach/send")
async def coach_send(request: Request):
    """The coach's words -> AI reads them into sets -> sent to every athlete, whose pages work out the paces."""
    form = await request.form()
    text, day = str(form.get("workout") or ""), str(form.get("day") or "")
    try:
        workout = await run_in_threadpool(coach_reader.read, text)
        count = coaching().send_to_all(day, workout["title"], workout["sets"], workout["notes"])
    except (coach.Invalid, PermissionError) as exc:
        return coach_page(request, error=str(exc), workout=text, day=day)
    return back(f"/coach?sent={count}#sent")


@router.post("/coach/view")
def coach_view(request: Request, athlete_id: str = Form("")):
    """Open the chosen athlete's Trainer, read-only (their full view for this coach)."""
    if not athlete_id.isdigit() or not coaching().full_view(int(athlete_id)):
        return coach_page(request, error="That athlete's page isn't open to you right now. Sign out and in again "
                                         "with the passcode to renew it.")
    response = back(BASE)
    response.set_cookie(VIEW_COOKIE, athlete_id, max_age=12 * 3600, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
    return response


@router.get(BASE + "/athletes/open")
def open_athlete(athlete: int = 0):
    return back(f"{BASE}/athletes/{athlete}" if athlete else _coach_home())


@router.post(BASE + "/athletes/send")
async def send_workout(request: Request):
    form = await request.form()
    raw = str(form.get("athlete_id") or "")
    if not raw.isdigit():
        raise HTTPException(status_code=400, detail="Pick an athlete.")
    return await _prescribe(form, int(raw))


@router.get(BASE + "/athletes/{athlete_id}")
def athlete_page(request: Request, athlete_id: int, sent: str = ""):
    c = coaching()
    link, workouts = _coach(c.sent_to, athlete_id)
    paces = []
    if c.full_view(athlete_id):   # temporary: the athlete's own switch on My coach
        model = _athlete_model(athlete_id)
        paces = coach.pace_profile(model)
        for w in workouts:
            w["sets"] = coach.targets(w["sets"], model)
    return render("trainer_athlete.html", title=link["name"], current=BASE + "/athletes",
                  page_url=f"{BASE}/athletes/{athlete_id}", athlete=link, workouts=workouts, sent=bool(sent),
                  paces=paces, show_paces=bool(link["full_view_until"]), coach_home=_coach_home())


@router.post(BASE + "/athletes/{athlete_id}/prescribe")
async def prescribe(request: Request, athlete_id: int):
    return await _prescribe(await request.form(), athlete_id)


async def _prescribe(form, athlete_id):
    sets = [{"reps": form.get(f"reps_{i}"), "metres": form.get(f"metres_{i}"),
             "intensity": form.get(f"intensity_{i}"), "rest": form.get(f"rest_{i}")} for i in range(coach.MAX_SETS)]
    _coach(coaching().prescribe, athlete_id, form.get("day"), form.get("title"), sets, form.get("notes"))
    return back(f"{BASE}/athletes/{athlete_id}?sent=1")


@router.post(BASE + "/athletes/{athlete_id}/workouts/{prescription_id}/withdraw")
def withdraw_workout(athlete_id: int, prescription_id: int):
    _coach(coaching().coaches_athlete, athlete_id)
    _coach(coaching().withdraw, prescription_id)
    home = _coach_home()
    return back(home + "#sent" if home == "/coach" else f"{BASE}/athletes/{athlete_id}")


@router.post(BASE + "/coaching-links/{link_id}/full-view")
def full_view_switch(link_id: int, on: str = Form("")):
    """The owner turns a coach's temporary full view on (for coach.FULL_VIEW_DAYS) or off."""
    _coach(coaching().set_full_view, link_id, bool(on))
    return back(BASE + "/my-coach#coaches")


@router.post(BASE + "/athletes/{athlete_id}/view")
def start_viewing(request: Request, athlete_id: int):
    """A coach with full view opens the athlete's Trainer (read-only) until they tap Stop viewing."""
    if not coaching().full_view(athlete_id):
        raise HTTPException(status_code=403, detail="Full view isn't on for this athlete.")
    response = back(BASE)
    response.set_cookie(VIEW_COOKIE, str(athlete_id), max_age=12 * 3600, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
    return response


@router.post(BASE + "/view-as/stop")
def stop_viewing():
    response = back(_coach_home())
    response.delete_cookie(VIEW_COOKIE)
    return response


# ---------------------------------------------------------------- Sunday check-in


@router.get(BASE + "/checkin")
def checkin_page(request: Request, saved: int = 0):
    today = clock.today()
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
               water_goal: str = Form("8"), run_time: str = Form(""), gym_time: str = Form(""),
               race_mile: str = Form(""), race_3k: str = Form(""), race_5k: str = Form(""), race_10k: str = Form("")):
    _guard(store().save_settings, miles=miles, workout_day=workout_day, workout_time=workout_time, long_day=long_day,
           rest_day=rest_day, weights=weights, pilates=pilates, strides=strides, strides_text=strides_text or None,
           water_goal=water_goal, run_time=run_time, gym_time=gym_time, race_mile=race_mile, race_3k=race_3k,
           race_5k=race_5k, race_10k=race_10k)
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
        today = clock.today()
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


@router.post(BASE + "/sync/step")
def sync_step():
    """One small step of getting someone's Strava history in, called over and over by the Overview while it's open
    (so a page never waits on the whole thing): first the list of activities, then each run's details. Answers
    {stage, done, left, wait}: wait > 0 means Strava's request limit was hit; try again after that many seconds."""
    user = get_current_user_id()
    s = sync()
    try:
        if not s.history_state(user)[0]:
            stored = s.import_history(user, pages=1)
            return JSONResponse({"stage": "history", "done": stored, "left": None, "wait": 0,
                                 "imported": s.history_state(user)[1]})
        read = speed.fill(s, user, limit=10)
        return JSONResponse({"stage": "runs", "done": int(read), "left": speed.waiting_count(_state["db"], user),
                             "wait": 90 if read.limited else 0})
    except strava.StravaError as exc:
        return JSONResponse({"stage": "error", "done": 0, "left": None, "wait": 90 if "429" in str(exc) else 0,
                             "problem": str(exc)[:200]})


@router.post(BASE + "/sync/now")
def sync_now():
    user = get_current_user_id()
    s = sync()
    try:
        s.process(10)
        s.fill_details(user, limit=10)
        speed.fill(s, user, limit=40)
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
                  totals=totals, status=sync().status(get_current_user_id()), connected=connected,
                  is_admin=_is_admin(request), live=live, history=history, problem=problem,
                  today=clock.today().isoformat())


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
