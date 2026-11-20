"""Routes for the Thanksgiving planner (/thanksgiving) and cook mode (/thanksgiving/cook).

Mounted from api.py with configure(db) + app.include_router(router). Renders its own
templates (autoescaped) and does not use templates/base.html.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import planner, sizes, voice
from .presets import CATEGORIES, OUR_MENU, PRESET_BUTTONS, preset_dish
from .store import ThanksgivingStore

router = APIRouter()
FEEDBACK_AREA = "Kitchen / Recipes"
DEFAULT_DINNER = "2026-11-26T16:00"
COOK_COLORS = ("#1d4ed8", "#be185d", "#047857", "#b45309")

_templates = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
)
_state = {"db": None, "store": None}


def configure(db, voice_model=None):
    _state["db"] = db
    _state["store"] = ThanksgivingStore(db)
    _state["voice_model"] = voice_model  # None = OpenAI (voice.default_model); tests pass a fake
    _state["voice_pending"] = {}  # plan id -> checked actions waiting for Apply
    _state["voice_undo"] = {}  # plan id -> undo record for the last applied command


def store():
    if _state["store"] is None:
        raise RuntimeError("Thanksgiving planner is not configured")
    return _state["store"]


def app_tz():
    try:
        return ZoneInfo(os.getenv("APP_TIMEZONE", "America/Phoenix"))
    except Exception:
        # No tz database (e.g. Windows without tzdata): use the machine's local zone.
        return datetime.now().astimezone().tzinfo


def local_now():
    return datetime.now(app_tz()).replace(tzinfo=None, second=0, microsecond=0)


def to_ms(value):
    return int(value.replace(tzinfo=app_tz()).timestamp() * 1000)


def current_user_name(request):
    user = getattr(request.state, "current_user", None) or {}
    name = user.get("name") if hasattr(user, "get") else getattr(user, "name", "")
    return (name or "").split(" ")[0] or "Me"


def parse_cooks(text):
    return [c.strip() for c in (text or "").split(",") if c.strip()][:4]


def cook_colors(cooks):
    return {cook: COOK_COLORS[i % len(COOK_COLORS)] for i, cook in enumerate(cooks)}


def require_plan():
    plan = store().get_plan()
    if not plan:
        raise HTTPException(status_code=404, detail="Set up the Thanksgiving plan first.")
    return plan


def back(anchor=""):
    return RedirectResponse(url="/thanksgiving" + (f"#{anchor}" if anchor else ""), status_code=303)


def render(name, **context):
    return HTMLResponse(_templates.get_template(name).render(**context))


# ---------------------------------------------------------------- context

def plan_context(plan, now=None):
    now = now or local_now()
    s = store()
    dinner = planner.parse_dt(plan["dinner_at"])
    cooks = parse_cooks(plan["cooks"])
    dishes = [sizes.with_size(d) for d in s.get_dishes(plan["id"])]  # chosen batch size applied
    tasks = planner.build_timeline(plan, dishes)
    done = s.get_checked(plan["id"], "step")
    for task in tasks:
        task["done"] = task["key"] in done
    for dish in dishes:
        dish["ingredient_sections"] = planner.recipe_sections(dish["ingredients_text"])
        dish["instruction_sections"] = planner.recipe_sections(dish["instructions_text"])
    return {
        "dishes_by_id": {d["id"]: d for d in dishes},
        "plan": plan,
        "dinner": dinner,
        "now": now,
        "cooks": cooks,
        "colors": cook_colors(cooks),
        "dishes": dishes,
        "tasks": tasks,
        "done": done,
        "status": planner.status_now(tasks, done, now),
        "conflicts": planner.find_conflicts(tasks, plan["ovens"]),
        "days": planner.group_by_day(tasks, dinner),
        "dinner_day_chart": planner.oven_chart(tasks, dinner),
        "groceries": planner.build_grocery_list(dishes, s.get_extra_groceries(plan["id"])),
        "grocery_checked": s.get_checked(plan["id"], "grocery"),
        "days_until": (dinner.date() - now.date()).days,
    }


def library_recipes():
    """Ready recipes from the Kitchen library the user can pull into the plan."""
    db = _state["db"]
    try:
        meals = [dict(row) for row in db.get_recipe_complete_meals()]
    except Exception:
        return []
    recipes = []
    for meal in meals:
        if meal.get("status") != "ready":
            continue
        recipes.append({"id": meal["id"], "title": meal.get("edited_title") or meal.get("title") or "Recipe"})
    recipes.sort(key=lambda r: r["title"].lower())
    return recipes


def library_recipe(meal_id):
    db = _state["db"]
    meal = next((dict(m) for m in db.get_recipe_complete_meals() if m["id"] == meal_id), None)
    if not meal:
        return None
    details = {}
    row = db.connect().execute(
        "SELECT details_json FROM recipe_extractions WHERE group_id = ?", (meal.get("source_group_id"),)
    ).fetchone()
    if row and row["details_json"]:
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError:
            details = {}
    return {
        "name": meal.get("edited_title") or meal.get("title") or "Recipe",
        "category": "side",
        "ingredients_text": meal.get("edited_ingredients_text") or meal.get("ingredients_text") or "",
        "instructions_text": meal.get("edited_instructions_text") or meal.get("instructions_text") or "",
        "notes": details.get("make_ahead") or "",
        "steps": planner.steps_from_recipe_details(details),
        "source_meal_id": meal["id"],
    }


# ---------------------------------------------------------------- plan page

def create_default_plan(request):
    """First visit: create the plan with our menu so there is nothing to set up."""
    me = current_user_name(request)
    plan_id = store().create_plan(DEFAULT_DINNER, f"{me}, Parrisa", 1)
    for key in OUR_MENU:
        dish = preset_dish(key)
        if key == "turkey":
            # Curtis is racing Thursday morning; someone at home pulls the turkey out at 11:15.
            for step in dish["steps"]:
                if step["id"] == "out":
                    step["cook"] = "Parrisa"
        store().add_dish(plan_id, dish)
    return store().get_plan()


@router.get("/thanksgiving", response_class=HTMLResponse)
def plan_page(request: Request):
    plan = store().get_plan() or create_default_plan(request)
    context = plan_context(plan)
    day_choices = []
    for offset in range(7, -1, -1):
        day = context["dinner"] - planner.timedelta(days=offset)
        day_choices.append({"offset": offset, "label": "Thanksgiving" if offset == 0 else planner.short_date(day)})
    return render(
        "tg_plan.html", **context, planner=planner, categories=CATEGORIES, presets=PRESET_BUTTONS,
        step_kinds=planner.KIND_LABELS, day_choices=day_choices, library=library_recipes(),
        page_url=str(request.url.path),
    )


@router.post("/thanksgiving/setup")
def setup_plan(dinner_date: str = Form(...), dinner_time: str = Form("16:00"), cooks: str = Form(""),
               ovens: int = Form(1), starter_menu: str = Form("")):
    if store().get_plan():
        return back()
    dinner_at = f"{dinner_date}T{dinner_time or '16:00'}"
    planner.parse_dt(dinner_at)  # validate
    plan_id = store().create_plan(dinner_at, ", ".join(parse_cooks(cooks)), max(1, min(ovens, 2)))
    if starter_menu:
        for key in OUR_MENU:
            store().add_dish(plan_id, preset_dish(key))
    return back()


@router.post("/thanksgiving/settings")
def save_settings(dinner_date: str = Form(...), dinner_time: str = Form(...), cooks: str = Form(""),
                  ovens: int = Form(1)):
    plan = require_plan()
    dinner_at = f"{dinner_date}T{dinner_time}"
    planner.parse_dt(dinner_at)
    store().update_plan(plan["id"], dinner_at, ", ".join(parse_cooks(cooks)), max(1, min(ovens, 2)))
    return back("settings")


@router.post("/thanksgiving/dishes/add")
def add_dish(preset: str = Form(""), meal_id: int = Form(0), name: str = Form("")):
    plan = require_plan()
    dish = None
    if preset:
        dish = preset_dish(preset)
    elif meal_id:
        dish = library_recipe(meal_id)
    elif name.strip():
        dish = {"name": name.strip(), "category": "side", "steps": planner.generic_steps()}
    if not dish:
        raise HTTPException(status_code=400, detail="Pick a dish, a recipe, or type a name.")
    dish_id = store().add_dish(plan["id"], dish)
    return back(f"dish-{dish_id}")


@router.post("/thanksgiving/dishes/{dish_id}/save")
async def save_dish(dish_id: int, request: Request):
    plan = require_plan()
    existing = store().get_dish(plan["id"], dish_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Dish not found")
    form = await request.form()
    dinner = planner.parse_dt(plan["dinner_at"])
    ids, titles = form.getlist("step_id"), form.getlist("step_title")
    days, clocks = form.getlist("step_day"), form.getlist("step_time")
    minutes, kinds = form.getlist("step_minutes"), form.getlist("step_kind")
    temps, step_cooks = form.getlist("step_temp"), form.getlist("step_cook")
    details = form.getlist("step_detail")
    removed = set(form.getlist("step_remove"))
    existing_steps = {s.get("id"): s for s in existing["steps"]}
    steps = []
    for i, step_id in enumerate(ids):
        if step_id in removed or not (titles[i] if i < len(titles) else "").strip():
            continue
        steps.append({
            "id": step_id or planner.new_step_id(),
            "anchor": planner.anchor_for(_int(days, i), clocks[i] if i < len(clocks) else "12:00", dinner),
            "title": titles[i].strip()[:120],
            "minutes": max(_int(minutes, i), 0),
            "kind": kinds[i] if i < len(kinds) and kinds[i] in planner.STEP_KINDS else "hands",
            "temp": max(_int(temps, i), 0),
            "cook": (step_cooks[i] if i < len(step_cooks) else "").strip(),
            "detail": (details[i] if i < len(details) else "").strip()[:300],
            "section": existing_steps.get(step_id, {}).get("section", ""),
        })
    if (form.get("new_step_title") or "").strip():
        steps.append({
            "id": planner.new_step_id(),
            "anchor": planner.anchor_for(form.get("new_step_day") or 0, form.get("new_step_time") or "12:00", dinner),
            "title": form["new_step_title"].strip()[:120],
            "minutes": max(_int([form.get("new_step_minutes")], 0), 0) or 10,
            "kind": form.get("new_step_kind") if form.get("new_step_kind") in planner.STEP_KINDS else "hands",
            "temp": max(_int([form.get("new_step_temp")], 0), 0),
            "cook": "", "detail": "",
        })
    store().update_dish(plan["id"], dish_id, {
        "name": (form.get("name") or existing["name"]).strip(),
        "category": form.get("category") or existing["category"],
        "cook": (form.get("cook") or "").strip(),
        "ingredients_text": form.get("ingredients_text", existing["ingredients_text"]),
        "instructions_text": form.get("instructions_text", existing["instructions_text"]),
        "notes": form.get("notes", existing["notes"]),
        "steps": steps,
    })
    return back(f"dish-{dish_id}")


@router.post("/thanksgiving/dishes/{dish_id}/size")
def set_dish_size(dish_id: int, scale: str = Form(...)):
    plan = require_plan()
    dish = store().get_dish(plan["id"], dish_id)
    if not dish:
        raise HTTPException(status_code=404, detail="Dish not found")
    value = sizes.parse_scale(dish, scale)
    if value is None:
        raise HTTPException(status_code=400, detail="Pick one of the listed sizes.")
    store().set_scale(plan["id"], dish_id, value)
    return back(f"size-{dish_id}")  # lands on the dropdown without opening the editor


@router.post("/thanksgiving/api/command")
async def api_command(request: Request):
    """Turn a spoken or typed request into a preview of changes. Nothing is saved until Apply."""
    data = await _json(request)
    text = str(data.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Say or type what to change.")
    plan = require_plan()
    context = plan_context(plan)
    summary = voice.plan_summary(plan, context["dishes"], context["tasks"], context["cooks"], context["now"])
    history = data.get("history") if isinstance(data.get("history"), list) else []
    try:
        result = voice.understand(text, summary, [h for h in history if isinstance(h, dict)], _state["voice_model"])
    except Exception as exc:
        print(f"Warning: voice command failed: {exc}")
        raise HTTPException(status_code=503, detail="Dieter couldn't think that through right now. Try again.") from exc
    actions, changes, problems = voice.check(result["actions"], plan, context["dishes"], context["tasks"],
                                             context["cooks"])
    _state["voice_pending"][plan["id"]] = actions
    return {"reply": result["reply"], "question": result["question"], "changes": changes, "problems": problems,
            "can_apply": bool(actions)}


@router.post("/thanksgiving/api/command/apply")
def api_command_apply():
    plan = require_plan()
    actions = _state["voice_pending"].pop(plan["id"], None)
    if not actions:
        raise HTTPException(status_code=409, detail="Nothing to apply. Say the change again.")
    _state["voice_undo"][plan["id"]] = voice.apply(store(), plan, actions)
    return {"ok": True}


@router.post("/thanksgiving/api/command/cancel")
def api_command_cancel():
    plan = require_plan()
    _state["voice_pending"].pop(plan["id"], None)
    return {"ok": True}


@router.post("/thanksgiving/api/command/undo")
def api_command_undo():
    plan = require_plan()
    record = _state["voice_undo"].pop(plan["id"], None)
    if not record:
        raise HTTPException(status_code=409, detail="Nothing to undo.")
    voice.undo(store(), plan, record)
    return {"ok": True}


@router.post("/thanksgiving/api/dishes/{dish_id}/size")
async def api_set_dish_size(dish_id: int, request: Request):
    """Cook mode's batch-size dropdown."""
    data = await _json(request)
    plan = require_plan()
    dish = store().get_dish(plan["id"], dish_id)
    if not dish:
        raise HTTPException(status_code=404, detail="Dish not found")
    value = sizes.parse_scale(dish, data.get("scale"))
    if value is None:
        raise HTTPException(status_code=400, detail="Pick one of the listed sizes.")
    store().set_scale(plan["id"], dish_id, value)
    return {"ok": True}


@router.post("/thanksgiving/dishes/{dish_id}/turkey")
def retime_turkey(dish_id: int, weight_lb: float = Form(...), frozen: str = Form("")):
    plan = require_plan()
    dish = store().get_dish(plan["id"], dish_id)
    if not dish:
        raise HTTPException(status_code=404, detail="Dish not found")
    old_cooks = {s["id"]: s.get("cook", "") for s in dish["steps"]}
    steps = planner.turkey_steps(max(0.0, min(weight_lb, 40.0)), frozen=bool(frozen))
    for step in steps:
        step["cook"] = old_cooks.get(step["id"], "")
    dish["steps"] = steps
    dish["notes"] = f"{weight_lb:g} lb turkey." + ("" if frozen else " Fresh / already thawed.")
    store().update_dish(plan["id"], dish_id, dish)
    return back(f"dish-{dish_id}")


@router.post("/thanksgiving/dishes/{dish_id}/delete")
def delete_dish(dish_id: int):
    plan = require_plan()
    store().delete_dish(plan["id"], dish_id)
    return back("menu")


@router.post("/thanksgiving/steps/toggle")
def toggle_step(key: str = Form(...), done: str = Form(""), anchor: str = Form("timeline")):
    plan = require_plan()
    store().set_checked(plan["id"], "step", key, bool(done))
    return back(anchor)


@router.post("/thanksgiving/groceries/toggle")
def toggle_grocery(key: str = Form(...), done: str = Form("")):
    plan = require_plan()
    store().set_checked(plan["id"], "grocery", key, bool(done))
    return back("shopping")


@router.post("/thanksgiving/groceries/add")
def add_grocery(text: str = Form(...)):
    plan = require_plan()
    if text.strip():
        store().add_extra_grocery(plan["id"], text.strip())
    return back("shopping")


@router.post("/thanksgiving/groceries/{item_id}/delete")
def delete_grocery(item_id: int):
    plan = require_plan()
    store().delete_extra_grocery(plan["id"], item_id)
    return back("shopping")


@router.post("/thanksgiving/feedback")
def feedback_form(request: Request, text: str = Form(...), page: str = Form("/thanksgiving")):
    save_feedback(request, text, page)
    return RedirectResponse(url=(page if page.startswith("/thanksgiving") else "/thanksgiving") + "?reported=1",
                            status_code=303)


def save_feedback(request, text, page):
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Describe the problem.")
    user = getattr(request.state, "current_user", None) or {}
    get = user.get if hasattr(user, "get") else (lambda k, d="": getattr(user, k, d))
    _state["db"].add_app_feedback_report(
        title=f"[Thanksgiving] {text.splitlines()[0][:80]}",
        area=FEEDBACK_AREA,
        page_url=page[:300],
        page_title="Thanksgiving planner",
        reporter_name=get("name", "") or "",
        reporter_email=get("email", "") or "",
        raw_feedback=text[:4000],
    )


def _int(values, index):
    try:
        return int(float(values[index]))
    except (IndexError, TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- cook mode

@router.get("/thanksgiving/cook", response_class=HTMLResponse)
def cook_page(request: Request):
    plan = store().get_plan() or create_default_plan(request)
    return render("tg_cook.html", plan=plan, cooks=parse_cooks(plan["cooks"]), page_url="/thanksgiving/cook")


@router.get("/thanksgiving/api/state")
def cook_state():
    plan = require_plan()
    s = store()
    dinner = planner.parse_dt(plan["dinner_at"])
    cooks = parse_cooks(plan["cooks"])
    dishes = [sizes.with_size(d) for d in s.get_dishes(plan["id"])]
    tasks = planner.build_timeline(plan, dishes)
    done = s.get_checked(plan["id"], "step")
    by_dish = {}
    for task in tasks:
        by_dish.setdefault(task["dish_id"], []).append({
            "key": task["key"], "title": task["title"], "detail": task["detail"], "kind": task["kind"],
            "temp": task["temp"], "minutes": task["minutes"], "cook": task["cook"],
            "start_ms": to_ms(task["start"]), "end_ms": to_ms(task["end"]),
            "clock": planner.format_clock(task["start"]), "day": planner.short_date(task["start"]),
            "done": task["key"] in done, "section": task["section"],
        })
    return JSONResponse({
        "now_ms": int(time.time() * 1000),
        "dinner_ms": to_ms(dinner),
        "dinner_label": f"{planner.short_date(dinner)} at {planner.format_clock(dinner)}",
        "cooks": cooks,
        "colors": cook_colors(cooks),
        "dishes": [{
            "id": d["id"], "name": d["name"], "cook": d["cook"], "category": d["category"],
            "notes": d["notes"], "size_label": d.get("size_label", ""),
            "size_options": d.get("size_options", []), "size_value": d.get("size_value", ""),
            "ingredient_sections": planner.recipe_sections(d["ingredients_text"]),
            "instruction_sections": planner.recipe_sections(d["instructions_text"], with_timers=True),
            "steps": by_dish.get(d["id"], []),
        } for d in dishes],
        "timers": [{"id": t["id"], "dish_id": t["dish_id"], "label": t["label"], "ends_ms": t["ends_at_ms"]}
                   for t in s.get_timers(plan["id"])],
        "alerts": s.get_alerts(plan["id"]),
        "conflicts": [w["text"] for w in planner.find_conflicts(tasks, plan["ovens"])
                      if w["when"] >= local_now() - planner.timedelta(hours=1)][:3],
    })


async def _json(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


@router.post("/thanksgiving/api/steps/done")
async def api_step_done(request: Request):
    plan = require_plan()
    data = await _json(request)
    key = str(data.get("key") or "")
    if not key:
        raise HTTPException(status_code=400, detail="Missing step")
    store().set_checked(plan["id"], "step", key, bool(data.get("done", True)))
    return {"ok": True}


@router.post("/thanksgiving/api/timers")
async def api_add_timer(request: Request):
    plan = require_plan()
    data = await _json(request)
    minutes = max(1, min(int(float(data.get("minutes") or 0) or 1), 24 * 60))
    timer_id = store().add_timer(plan["id"], data.get("dish_id"), str(data.get("label") or "Timer"),
                                 int(time.time() * 1000) + minutes * 60_000)
    return {"ok": True, "id": timer_id}


@router.post("/thanksgiving/api/timers/{timer_id}/dismiss")
def api_dismiss_timer(timer_id: int):
    plan = require_plan()
    store().dismiss_timer(plan["id"], timer_id)
    return {"ok": True}


@router.post("/thanksgiving/api/alerts")
async def api_alert(request: Request):
    plan = require_plan()
    data = await _json(request)
    key = str(data.get("key") or "")
    state = data.get("state")
    if not key or state not in ("ack", "snooze"):
        raise HTTPException(status_code=400, detail="Bad alert update")
    until = int(time.time() * 1000) + int(data.get("minutes") or 5) * 60_000 if state == "snooze" else 0
    store().set_alert(plan["id"], key, state, until)
    return {"ok": True}


@router.post("/thanksgiving/api/feedback")
async def api_feedback(request: Request):
    data = await _json(request)
    save_feedback(request, str(data.get("text") or ""), str(data.get("page") or "/thanksgiving/cook"))
    return {"ok": True}
