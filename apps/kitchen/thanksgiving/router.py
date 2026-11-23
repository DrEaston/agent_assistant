"""Routes for the Thanksgiving planner (/thanksgiving) and cook mode (/thanksgiving/cook).

Mounted from api.py with configure(db) + app.include_router(router). Renders its own
templates (autoescaped) and does not use templates/base.html.
"""

import contextvars
import json
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from jinja2 import Environment, FileSystemLoader, select_autoescape

from core import internal, ui
from core.connector import agent
from database import get_current_user_id

from . import brain, handsfree, menu, planner, sizes, voice
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
_templates.globals["asset_url"] = ui.asset_url  # shared scripts in core/ui/static (served by the main app)
_state = {"db": None, "store": None}


def configure(db, voice_model=None, fast_model=None, speaker=None, platform=None, claude_call=None):
    _state["db"] = db
    # The rest of Dieter (recipe library, problem reports): read directly when running inside the main app,
    # or over HTTP when Thanksgiving runs as its own service (see service.py).
    _state["platform"] = platform or LocalPlatform(db)
    _state["store"] = ThanksgivingStore(db)
    _state["voice_model"] = voice_model  # deep model; None = OpenAI (voice.default_model); tests pass a fake
    _state["fast_model"] = fast_model  # quick first pass; None = OpenAI (voice.fast_default_model)
    _state["speaker"] = speaker  # text -> mp3 chunks; None = OpenAI (voice.speech_chunks); tests pass a fake
    _state["claude_call"] = claude_call  # Claude brain's model call; None = the Anthropic API; tests pass a fake
    _state["deep_jobs"] = {}  # ticket -> deep model call in progress for an escalated request
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


def back(page="/thanksgiving", anchor="", request=None):
    """Redirect after a form post. With a request, go back to the Thanksgiving page the form was on
    (the timeline, a dish page…), falling back to `page`."""
    if request is not None:
        referer = urlsplit(request.headers.get("referer") or "")
        if referer.path.startswith("/thanksgiving") and not referer.path.startswith(("/thanksgiving/api",
                                                                                    "/thanksgiving/cook")):
            page = referer.path
    return RedirectResponse(url=page + (f"#{anchor}" if anchor else ""), status_code=303)


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
        "grocery_aside": grocery_aside(s, plan["id"]),
        "grocery_links": {},  # filled in by follow_linked() on the shopping pages
        "days_until": (dinner.date() - now.date()).days,
    }


# Items set aside at the end of a shopping trip: moved to a Kitchen list for the next trip, or not needed.
ASIDE_KINDS = {"next": "grocery-next", "skip": "grocery-skip"}


def grocery_aside(s, plan_id):
    """{item key: "next" | "skip"} for shopping items that left the list without being bought."""
    aside = {}
    for name, kind in ASIDE_KINDS.items():
        aside.update(dict.fromkeys(s.get_checked(plan_id, kind), name))
    return aside


# Items moved to a Kitchen list go as portable items (core/portable_list.py), so this list can still follow them:
# bought on that list shows here as in the cart; that list deleted puts them back here.
LIST_SOURCE = {"app": "thanksgiving", "label": "Thanksgiving"}


def kitchen_list_url(list_id):
    return f"/apps/recipes/grocery-lists/{int(list_id)}"


def local_new_list(db, title, lines=(), items=(), source=None):
    """Make a Kitchen shopping list for the current user. Returns {"id", "url"}."""
    from apps.kitchen.shopping import portable

    made = portable.create_list(db, title, lines, items, source)
    return {"id": made["id"], "url": made["url"]}


def portable_items(items):
    """Thanksgiving shopping items in the portable format."""
    return [{"key": i["key"], "text": i["text"], "amount": i.get("amount") or "", "aisle": i.get("section") or "",
             "for": i.get("dishes") or []} for i in items]


def follow_linked(plan, context):
    """What happened to items moved to a Kitchen list: bought there -> in the cart here; that list deleted or the
    item taken off it -> back on this list. Returns updated grocery_checked, grocery_aside and grocery_links
    ({key: {"url", "checked"}}). If Kitchen can't be reached, items stay as they were."""
    s = store()
    links = s.get_list_links(plan["id"])
    checked, aside, linked = set(context["grocery_checked"]), dict(context["grocery_aside"]), {}
    by_list = {}
    for key, list_id in links.items():
        by_list.setdefault(list_id, []).append(key)
    for list_id, keys in by_list.items():
        try:
            state = _state["platform"].linked_state(list_id) or {}
        except Exception as exc:
            print(f"Warning: couldn't read Kitchen list {list_id}: {exc}")
            linked.update({key: {"url": kitchen_list_url(list_id), "checked": False} for key in keys})
            continue
        items = state.get("items") or {}
        for key in keys:
            info = items.get(key)
            if not info:  # that list is gone, or the item was taken off it
                s.delete_list_link(plan["id"], key)
                s.set_checked(plan["id"], ASIDE_KINDS["next"], key, False)
                if aside.get(key) == "next":
                    aside.pop(key)
                continue
            linked[key] = {"url": info.get("url") or kitchen_list_url(info.get("list_id") or list_id),
                           "checked": bool(info.get("checked"))}
            if info.get("checked"):  # bought on the next trip
                aside.pop(key, None)
                checked.add(key)
    return {"grocery_checked": checked, "grocery_aside": aside, "grocery_links": linked}


def set_grocery_checked(plan_id, key, done):
    """Check an item off here, and on the Kitchen list too if it was moved there."""
    store().set_checked(plan_id, "grocery", key, bool(done))
    list_id = store().get_list_links(plan_id).get(key)
    if list_id:
        try:
            _state["platform"].check_linked(list_id, key, bool(done))
        except Exception as exc:
            print(f"Warning: couldn't update Kitchen list {list_id}: {exc}")


def put_back(plan_id, key):
    """Back onto this list: an item set aside at the end of a trip (and off the Kitchen list it went to)."""
    list_id = store().get_list_links(plan_id).get(key)
    if list_id:
        try:
            _state["platform"].remove_linked(list_id, key)
        except Exception as exc:
            print(f"Warning: couldn't take {key} off Kitchen list {list_id}: {exc}")
        store().delete_list_link(plan_id, key)
    for kind in ASIDE_KINDS.values():
        store().set_checked(plan_id, kind, key, False)


class LocalPlatform:
    """Inside the main app: read the shared database directly."""

    def __init__(self, db):
        self.db = db

    def new_list(self, title, lines=(), items=(), source=None):
        return local_new_list(self.db, title, lines, items, source)

    def linked_state(self, list_id):
        from apps.kitchen.shopping import portable
        return portable.linked_state(self.db, list_id, LIST_SOURCE["app"])

    def check_linked(self, list_id, key, done):
        from apps.kitchen.shopping import portable
        return portable.check(self.db, list_id, LIST_SOURCE["app"], key, done)

    def remove_linked(self, list_id, key):
        from apps.kitchen.shopping import portable
        return portable.remove(self.db, list_id, LIST_SOURCE["app"], key)

    def kitchen_tools(self):
        from apps.kitchen import connector as kitchen_connector

        return kitchen_connector.tools()

    def library(self):
        return local_library(self.db)

    def library_recipe(self, meal_id):
        return local_library_recipe(self.db, meal_id)

    def report(self, fields):
        self.db.add_app_feedback_report(**fields)


class NoKitchenLists(LocalPlatform):
    """Thanksgiving as its own service without PLATFORM_URL: there's no Kitchen to put lists in. Making one here
    would save it in Thanksgiving's own database, where Kitchen never sees it, so say so instead."""

    def _no_kitchen(self, *args, **kwargs):
        raise RuntimeError("Kitchen lists aren't reachable: PLATFORM_URL isn't set for the Thanksgiving service")

    new_list = linked_state = check_linked = remove_linked = _no_kitchen


class RemotePlatform:
    """Thanksgiving as its own service: ask the main app, as the signed-in user."""

    def __init__(self, base_url):
        self.base_url = base_url

    def library(self):
        return internal.call(self.base_url, "GET", "/internal/kitchen/library", internal.current_user.get())

    def library_recipe(self, meal_id):
        return internal.call(self.base_url, "GET", f"/internal/kitchen/library/{int(meal_id)}",
                             internal.current_user.get())

    def report(self, fields):
        internal.call(self.base_url, "POST", "/internal/studio/reports", internal.current_user.get(), fields)

    def new_list(self, title, lines=(), items=(), source=None):
        return internal.call(self.base_url, "POST", "/internal/kitchen/lists", internal.current_user.get(),
                             {"title": title, "lines": list(lines), "items": list(items), "source": source})

    def linked_state(self, list_id):
        return internal.call(self.base_url, "GET",
                             f"/internal/kitchen/lists/{int(list_id)}/linked?app={LIST_SOURCE['app']}",
                             internal.current_user.get())

    def check_linked(self, list_id, key, done):
        return internal.call(self.base_url, "POST", f"/internal/kitchen/lists/{int(list_id)}/linked/check",
                             internal.current_user.get(), {"app": LIST_SOURCE["app"], "key": key, "done": done})

    def remove_linked(self, list_id, key):
        return internal.call(self.base_url, "POST", f"/internal/kitchen/lists/{int(list_id)}/linked/remove",
                             internal.current_user.get(), {"app": LIST_SOURCE["app"], "key": key})

    def kitchen_tools(self):
        from core.connector.tools import RemoteToolSet

        if getattr(self, "_kitchen", None) is None:  # one per service, so its tool list is remembered
            self._kitchen = RemoteToolSet("kitchen", self.base_url, "/internal/connector/kitchen/tools")
        return self._kitchen


def library_recipes():
    """Ready recipes from the Kitchen library the user can pull into the plan ([] if it can't be reached)."""
    try:
        return _state["platform"].library() or []
    except Exception as exc:
        print(f"Warning: Kitchen library unavailable: {exc}")
        return []


def library_recipe(meal_id):
    try:
        return _state["platform"].library_recipe(meal_id)
    except Exception as exc:
        print(f"Warning: Kitchen recipe {meal_id} unavailable: {exc}")
        return None


def local_library(db):
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


def local_library_recipe(db, meal_id):
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


def render_page(request, template, tab, extend=None, **extra):
    """One tab of the planner (they share tg_page.html: header, tabs, Tell Dieter)."""
    plan = store().get_plan() or create_default_plan(request)
    context = plan_context(plan)
    day_choices = []
    for offset in range(7, -1, -1):
        day = context["dinner"] - planner.timedelta(days=offset)
        day_choices.append({"offset": offset, "label": "Thanksgiving" if offset == 0 else planner.short_date(day)})
    context.update(extra)
    if extend:
        context.update(extend(context))  # page-specific values worked out from the plan
    return render(template, **context, planner=planner, categories=CATEGORIES, presets=PRESET_BUTTONS,
                  step_kinds=planner.KIND_LABELS, day_choices=day_choices, tab=tab,
                  page_url=str(request.url.path))


@router.get("/thanksgiving", response_class=HTMLResponse)
def plan_page(request: Request):
    return render_page(request, "tg_now.html", "now")


@router.get("/thanksgiving/timeline", response_class=HTMLResponse)
def timeline_page(request: Request):
    return render_page(request, "tg_timeline.html", "timeline")


PUBLIC_PATHS = {"/thanksgiving/menu"}  # open without signing in (the main app and service.py both allow it)
PUBLIC_RECIPE = re.compile(r"/thanksgiving/menu/(\d+)")  # a dish's recipe, linked from the shared menu card


def is_public_path(path):
    return path in PUBLIC_PATHS or bool(PUBLIC_RECIPE.fullmatch(path))


def has_recipe(dish):
    return bool((dish.get("ingredients_text") or "").strip() or (dish.get("instructions_text") or "").strip())


def public_recipe(request, dish_id):
    """Signed out: one dish's recipe as written, and who's bringing it. Only dishes on the shared menu; no notes or timeline."""
    plan = store().household_plan()
    dish = next((d for d in store().get_dishes(plan["id"]) if d["id"] == dish_id), None) if plan else None
    if not dish or dish.get("category") == "prep" or not (has_recipe(dish) or (dish.get("cook") or "").strip()):
        raise HTTPException(status_code=404, detail="That recipe isn't on the menu.")
    return render("tg_recipe_public.html", dish=dish, blurb=menu.entry_for(dish).get("description") or menu.blurb(dish),
                  ingredient_sections=planner.recipe_sections(dish["ingredients_text"] or ""),
                  instruction_sections=planner.recipe_sections(dish["instructions_text"] or ""),
                  page_url=str(request.url.path))


def public_menu(request):
    """Signed out: just the menu card, for sharing the plain link. No tabs, no editing, no names."""
    plan = store().household_plan()
    if not plan:
        return render("tg_menu_public.html", menu_courses=[], dinner=planner.parse_dt(DEFAULT_DINNER),
                      menu_time="", page_url=str(request.url.path))
    dinner = planner.parse_dt(plan["dinner_at"])
    courses = menu.courses(store().get_dishes(plan["id"]))
    return render("tg_menu_public.html", menu_courses=courses, dinner=dinner, menu_time=menu.short_time(dinner),
                  menu_arrival=menu.arrival(), menu_address=menu.address(),
                  page_url=str(request.url.path))


@router.get("/thanksgiving/menu", response_class=HTMLResponse)
def menu_page(request: Request):
    if not get_current_user_id():
        return public_menu(request)
    def courses(context):
        found = menu.courses(context["dishes"])
        return {"menu_courses": found, "menu_time": menu.short_time(context["dinner"]), "menu_arrival": menu.arrival(), "menu_address": menu.address(),
                "dishes": [d for d in context["dishes"] if d.get("category") != "prep"],
                "prep_dishes": [d for d in context["dishes"] if d.get("category") == "prep"]}
    return render_page(request, "tg_menu.html", "menu", extend=courses, library=library_recipes())




@router.get("/thanksgiving/menu/{dish_id}", response_class=HTMLResponse)
def dish_page(request: Request, dish_id: int):
    if not get_current_user_id():
        return public_recipe(request, dish_id)
    def pick(context):
        dish = context["dishes_by_id"].get(dish_id)
        if not dish:
            raise HTTPException(status_code=404, detail="That dish isn't on the menu.")
        return {"dish": dish, "dish_tasks": [t for t in context["tasks"] if t["dish_id"] == dish_id]}
    return render_page(request, "tg_dish.html", "menu", extend=pick)


@router.get("/thanksgiving/recipes", response_class=HTMLResponse)
def recipes_page(request: Request):
    return render_page(request, "tg_recipes.html", "recipes")


@router.get("/thanksgiving/shopping", response_class=HTMLResponse)
def shopping_page(request: Request, trip: str = "", n: int = 0):
    notice = None
    if trip.isdigit():
        notice = {"ok": True, "url": f"/apps/recipes/grocery-lists/{int(trip)}", "count": max(n, 0)}
    elif trip == "done":
        notice = {"ok": True, "url": "", "count": 0}
    elif trip == "failed":
        notice = {"ok": False}
    return render_page(request, "tg_shopping.html", "shopping", extend=lambda ctx: follow_linked(ctx["plan"], ctx),
                       trip_notice=notice)


@router.get("/thanksgiving/shopping/finish", response_class=HTMLResponse)
def finish_trip_page(request: Request):
    return render_page(request, "tg_shopping_finish.html", "shopping",
                       extend=lambda ctx: follow_linked(ctx["plan"], ctx))


@router.post("/thanksgiving/shopping/finish")
async def finish_trip(request: Request):
    """End a shopping trip. Each item still on the list is "got" (checked), "skip" (don't need it) or "next"
    (moved to a new Kitchen list for the next trip). Items with no choice stay on the list."""
    plan = require_plan()
    form = await request.form()
    picks = {key[5:]: str(value) for key, value in form.items() if key.startswith("pick-")}
    try:
        made = finish_trip_picks(plan, picks)
    except Exception as exc:
        print(f"Warning: couldn't make the next-trip list: {exc}")
        return RedirectResponse(url="/thanksgiving/shopping?trip=failed", status_code=303)
    if not made:
        return RedirectResponse(url="/thanksgiving/shopping?trip=done", status_code=303)
    return RedirectResponse(url=f"/thanksgiving/shopping?trip={int(made['id'])}&n={made['count']}", status_code=303)


def finish_trip_picks(plan, picks):
    """picks: {item key: "got" | "skip" | "next"}. "next" items go to a new Kitchen list (made first, so nothing is
    set aside if that fails; the error is raised). Returns {"id", "url", "count"} for that list, or None."""
    context = plan_context(plan)
    items = {item["key"]: item for section in context["groceries"] for item in section["items"]}
    later = [items[key] for key, pick in picks.items() if pick == "next" and key in items]
    made = None
    if later:
        made = dict(_state["platform"].new_list(f"Next trip · {planner.short_date(context['now'])}", [],
                                                items=portable_items(later), source=LIST_SOURCE),
                    count=len(later))
    for key, pick in picks.items():
        if key not in items:
            continue
        if pick == "got":
            store().set_checked(plan["id"], "grocery", key, True)
        elif pick in ASIDE_KINDS:
            store().set_checked(plan["id"], ASIDE_KINDS[pick], key, True)
            if pick == "next" and made:
                store().set_list_link(plan["id"], key, made["id"])
    return made


@router.get("/thanksgiving/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return render_page(request, "tg_settings.html", "settings")


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
def save_settings(dinner_date: str = Form(...), dinner_time: str = Form(...), cooks: str = Form(None),
                  ovens: int = Form(1)):
    plan = require_plan()
    dinner_at = f"{dinner_date}T{dinner_time}"
    planner.parse_dt(dinner_at)
    # Cook names are no longer shown or edited (you use one device per cook instead); keep what's saved.
    cooks_text = ", ".join(parse_cooks(cooks)) if cooks is not None else plan["cooks"]
    store().update_plan(plan["id"], dinner_at, cooks_text, max(1, min(ovens, 2)))
    return back("/thanksgiving/settings")


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
    return back(f"/thanksgiving/menu/{dish_id}")


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
            # No cook field on the form any more: keep whatever was saved.
            "cook": (step_cooks[i] if i < len(step_cooks) else existing_steps.get(step_id, {}).get("cook", "")).strip(),
            "detail": (details[i] if i < len(details) else "").strip()[:300],
            "section": existing_steps.get(step_id, {}).get("section", ""),
            # Timers the step can start (presets.with_timers); not on the form, so keep what was saved.
            "series": existing_steps.get(step_id, {}).get("series") or [],
            "series_button": existing_steps.get(step_id, {}).get("series_button") or "",
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
        "cook": (form.get("cook", existing.get("cook") or "") or "").strip(),
        "ingredients_text": form.get("ingredients_text", existing["ingredients_text"]),
        "instructions_text": form.get("instructions_text", existing["instructions_text"]),
        "notes": form.get("notes", existing["notes"]),
        "steps": steps,
    })
    return back(f"/thanksgiving/menu/{dish_id}")


@router.post("/thanksgiving/dishes/{dish_id}/size")
def set_dish_size(request: Request, dish_id: int, scale: str = Form(...)):
    plan = require_plan()
    dish = store().get_dish(plan["id"], dish_id)
    if not dish:
        raise HTTPException(status_code=404, detail="Dish not found")
    value = sizes.parse_scale(dish, scale)
    if value is None:
        raise HTTPException(status_code=400, detail="Pick one of the listed sizes.")
    store().set_scale(plan["id"], dish_id, value)
    return back(f"/thanksgiving/menu/{dish_id}", f"size-{dish_id}", request)  # back to the dropdown


# ---------------------------------------------------------------- Tell Dieter: fast first pass, deep when needed
#
# Every request first goes to a fast model (voice.triage, ~1 s). It answers questions and small talk itself and
# can ignore chatter on an open mic. Anything that changes the plan, "what if" timing, or anything it isn't sure
# of is handed to the deep model (voice.understand), which proposes checked changes. Some requests skip the fast
# pass: a change already waiting for yes/no, or pushback ("are you sure?"). Requests that obviously ask for a
# change start the deep model at the same time as the fast one, to save a second.
# Clients that send two_step get {"escalate": true, "say_now", "ticket"} right away and fetch the deep answer
# from /thanksgiving/api/think; older clients just wait for the final answer.

CHANGE_WORDS = re.compile(r"\b(move|moving|push|pushed|pull|shift|change|reschedule|instead|earlier|later|add|"
                          r"remove|delete|drop|skip|double|halve|half batch|resize|mark|dinner at|what if|"
                          r"did|finished|check(ed)? (it |them |that |those )?off|tick)\b")
PUSHBACK = re.compile(r"\b(are you sure|think (about it|harder|again)|double ?check|that's wrong|that is wrong|"
                      r"doesn't work|won't work|that's not right)\b")
_thinkers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dieter-deep")
JOB_MAX_AGE = 300


def _empty_answer(**extra):
    answer = {"ignored": False, "reply": "", "question": "", "changes": [], "problems": [], "warnings": [],
              "can_apply": False, "applied": False, "end": False, "do": []}  # do: actions for the chat (navigate)
    answer.update(extra)
    return answer


def _start_deep(plan, text, history, summary, sass, prompt, handoff, device, spoken, which="classic", context=None):
    now = time.time()
    for old, job in list(_state["deep_jobs"].items()):
        if now - job["started"] > JOB_MAX_AGE:
            _state["deep_jobs"].pop(old, None)
    ticket = secrets.token_urlsafe(9)
    run = contextvars.copy_context().run  # the worker thread acts as the same signed-in user
    if which == "claude":
        try:
            kitchen = _state["platform"].kitchen_tools()
        except Exception as exc:
            print(f"Warning: Kitchen tools unavailable: {exc}")
            kitchen = None
        future = _thinkers.submit(run, brain.understand, text, summary, history, sass, prompt, handoff, plan,
                                  context or plan_context(plan), kitchen, _state.get("claude_call"))
    else:
        future = _thinkers.submit(run, voice.understand, text, summary, history, _state["voice_model"], sass, prompt,
                                  handoff)
    _state["deep_jobs"][ticket] = {"future": future, "plan_id": plan["id"], "text": text, "sass": sass,
                                   "device": device, "spoken": spoken, "started": now, "brain": which}
    return ticket


def _log_tier(device, text, route, note, ms):
    print("Tell Dieter quick: " + json.dumps({"device": device or "unnamed", "said": text[:200], "route": route,
                                              "note": str(note or "")[:200], "ms": ms}))


def _answer(text, history, device, sass, spoken="", which="classic"):
    """First answer to one request: the fast model's reply, an ignore, or an escalation ticket for the deep model.
    spoken: "" (typed), or how an open mic heard it ("name" / "follow-up" / "conversation")."""
    plan = require_plan()
    context = plan_context(plan)
    summary = voice.plan_summary(plan, context["dishes"], context["tasks"], context["cooks"], context["now"])
    history = [h for h in history if isinstance(h, dict)]
    prompt = handsfree.spoken_prompt(spoken) if spoken else ""
    lowered = text.lower()
    if _state["voice_pending"].get(plan["id"]):
        forced = "a change is waiting for the cook's yes or no"
    elif PUSHBACK.search(lowered):
        forced = "the cook pushed back on the last answer"
    else:
        forced = ""
    if which == "claude":
        # One model does it all: say a quick line while Claude works, then show its answer.
        ticket = _start_deep(plan, text, history, summary, sass, prompt, forced, device, spoken, "claude", context)
        _log_tier(device, text, "claude", forced, 0)
        # Hands-free speaks a quick line while Claude works; on screen "Thinking…" is enough.
        return _empty_answer(escalate=True, ticket=ticket, say_now="One sec." if spoken in ("name", "follow-up") else "")
    started = time.perf_counter()
    ticket = None
    if forced or CHANGE_WORDS.search(lowered):
        ticket = _start_deep(plan, text, history, summary, sass, prompt, forced, device, spoken)
    if forced:
        _log_tier(device, text, "deep", forced, 0)
        return _empty_answer(escalate=True, ticket=ticket, say_now="Let me think that through.")
    try:
        quick = voice.triage(text, summary, history, _state["fast_model"], sass=sass, spoken_prompt=prompt)
    except Exception as exc:
        print(f"Warning: quick pass failed: {exc}")
        quick = {"route": "deep", "reply": "", "say_now": "", "why": "the quick pass failed", "end": False}
    fast_ms = round((time.perf_counter() - started) * 1000)
    _log_tier(device, text, quick["route"], quick.get("why") or quick.get("reply"), fast_ms)
    if quick["route"] in ("answer", "ignore"):
        if ticket:
            job = _state["deep_jobs"].pop(ticket, None)
            if job:
                job["future"].cancel()  # an unneeded head start; its answer is dropped if it already ran
        if quick["route"] == "ignore":
            return _empty_answer(ignored=True, tier="fast")
        return _empty_answer(reply=quick["reply"], end=quick["end"], tier="fast")
    if ticket is None:
        ticket = _start_deep(plan, text, history, summary, sass, prompt, quick["why"], device, spoken)
    return _empty_answer(escalate=True, ticket=ticket, say_now=quick["say_now"] or "Let me work that out.")


def _finish(ticket):
    """Wait for the deep model's answer to an escalated request, check its changes, and keep them pending."""
    plan = require_plan()
    job = _state["deep_jobs"].pop(str(ticket or ""), None)
    if not job or job["plan_id"] != plan["id"]:
        raise HTTPException(status_code=404, detail="That answer expired. Say it again?")
    try:
        result = job["future"].result(timeout=90)
    except Exception as exc:
        print(f"Warning: voice command failed: {exc}")
        raise HTTPException(status_code=503, detail="Dieter couldn't think that through right now. Try again.") from exc
    deep_ms = round((time.time() - job["started"]) * 1000)
    if result.get("ignore"):
        print("Hands-free: " + json.dumps({"device": job["device"] or "unnamed", "said": job["text"][:200],
                                           "ignored": "by AI"}))
        return _empty_answer(ignored=True, tier="deep")
    context = plan_context(plan)
    actions, changes, problems = voice.check(result["actions"], plan, context["dishes"], context["tasks"],
                                             context["cooks"])
    warnings = voice.knock_on(plan, context["dishes"], actions, plan["ovens"])
    applied = bool(actions) and all(a["type"] in ("done", "grocery") for a in actions)
    if applied:
        # Checking off boxes, or adding to the shopping list, is saved right away; Undo is shown instead of Apply.
        _state["voice_pending"].pop(plan["id"], None)
        _state["voice_undo"][plan["id"]] = voice.apply(store(), plan, actions)
    else:
        _state["voice_pending"][plan["id"]] = actions
    # Logged so "it did something dumb" can be looked up later.
    print("Tell Dieter: " + json.dumps({"device": job["device"] or "unnamed", "sass": job["sass"],
                                        "hands_free": job["spoken"] or "", "said": job["text"][:500],
                                        "reply": result["reply"], "question": result["question"],
                                        "changes": changes, "problems": problems, "warnings": warnings,
                                        "applied": applied, "ms": deep_ms, "model": _model_label(job),
                                        "opened": [a.get("url") for a in result.get("do") or []]}))
    return _empty_answer(reply=result["reply"], question=result["question"], changes=changes, problems=problems,
                         warnings=warnings, can_apply=bool(actions) and not applied, applied=applied,
                         end=bool(result.get("end")), tier="deep", do=result.get("do") or [])


def _model_label(job):
    if job.get("brain") == "claude":
        return agent.model_name() if _state.get("claude_call") is None else "test-claude"
    return voice.model_name() if _state["voice_model"] is None else "test"


def warm_up():
    """Open the AI connections before the cook's first phrase (a cold one adds a few seconds to that phrase)."""
    if _state.get("fast_model") is not None or _state.get("warmed", 0) > time.time() - 240:
        return
    _state["warmed"] = time.time()
    if agent.available() and _state.get("claude_call") is None:
        _thinkers.submit(contextvars.copy_context().run, _warm_claude)
    for name, timeout in ((voice.fast_model_name(), 6), (voice.model_name(), 60)):
        _thinkers.submit(_ping, name, timeout)


def _warm_claude():
    agent.warm()
    try:
        _state["platform"].kitchen_tools().list()  # also fetches and remembers Kitchen's tool list
    except Exception as exc:
        print(f"Warning: Kitchen tools warm-up failed: {exc}")


def _ping(name, timeout):
    try:
        voice._client(timeout).chat.completions.create(model=name, messages=[{"role": "user", "content": "hi"}],
                                                       max_completion_tokens=16)
    except Exception as exc:
        print(f"Warning: warm-up failed for {name}: {exc}")


async def _answer_request(data, spoken=""):
    text = str(data.get("text") or "").strip()[:1000]
    if not text:
        raise HTTPException(status_code=400, detail="Say or type what to change.")
    history = data.get("history") if isinstance(data.get("history"), list) else []
    device, sass = _device_and_sass(data)
    result = await run_in_threadpool(_answer, text, history, device, sass, spoken, _brain_for(data))
    if result.get("escalate") and not data.get("two_step"):
        result = await run_in_threadpool(_finish, result["ticket"])
    return result


def _brain_for(data):
    """"claude" or "classic" (OpenAI): the device's choice (This device → Brain), else DIETER_BRAIN, else claude.
    Claude needs ANTHROPIC_API_KEY; without it every device stays on classic."""
    choice = data.get("brain") if data.get("brain") in ("claude", "classic") else os.getenv("DIETER_BRAIN", "claude")
    if choice == "claude" and not (_state.get("claude_call") or agent.available()):
        return "classic"
    return choice if choice in ("claude", "classic") else "classic"


def _device_and_sass(data):
    # Each device can name itself and pick its own sass level (saved on that device).
    sass = data.get("sass") if data.get("sass") in voice.SASS_LEVELS else "normal"
    return str(data.get("device") or "").strip()[:40], sass


@router.post("/thanksgiving/api/command")
async def api_command(request: Request):
    """Turn a spoken or typed request into an answer or a preview of changes. Nothing is saved until Apply."""
    result = await _answer_request(await _json(request))
    result.pop("ignored", None)
    result.pop("end", None)
    return result


@router.post("/thanksgiving/api/think")
async def api_think(request: Request):
    """The deep model's answer for an escalated request (ticket from /api/command or /api/handsfree/ask)."""
    data = await _json(request)
    return await run_in_threadpool(_finish, data.get("ticket"))


@router.post("/thanksgiving/api/handsfree/heard")
async def api_handsfree_heard(request: Request):
    """One phrase from the hands-free mic. Returns {"addressed": false} for chatter, a quick command for the
    tablet to carry out itself, or the AI's answer (same fields as /api/command)."""
    data = await _json(request)
    text = str(data.get("text") or "").strip()[:500]
    device, sass = _device_and_sass(data)
    heard = handsfree.hear(text, follow_up=bool(data.get("follow_up")))
    if not heard["addressed"]:
        if heard["near_miss"]:
            # Only the first few words: enough to tune the wake word without logging the kitchen's conversation.
            print("Hands-free near miss: " + json.dumps({"device": device or "unnamed",
                                                         "start": " ".join(text.split()[:3])}))
        return {"addressed": False}
    quick = handsfree.command(heard["rest"])
    if quick:
        print("Hands-free: " + json.dumps({"device": device or "unnamed", "said": text[:200], "via": heard["via"],
                                           "command": quick}))
        return {"addressed": True, "via": heard["via"], "command": quick}
    # Not a quick command: the tablet plays its "heard you" tone, then asks the AI via /handsfree/ask.
    return {"addressed": True, "via": heard["via"], "ask": heard["rest"]}


@router.post("/thanksgiving/api/handsfree/ask")
async def api_handsfree_ask(request: Request):
    """The AI's answer to a hands-free phrase, kept short for speaking. It may decide the phrase wasn't for it."""
    data = await _json(request)
    text = str(data.get("text") or "").strip()[:500]
    if not text:
        raise HTTPException(status_code=400, detail="Nothing to ask.")
    via = data.get("via") if data.get("via") in ("follow-up", "conversation") else "name"
    return await _answer_request(data, spoken=via)


@router.get("/thanksgiving/api/internal/dishes")
def api_internal_dishes():
    """The plan and its dishes at their chosen batch size, for Kitchen shopping lists (which live in the main app)."""
    plan = store().get_plan()
    if not plan:
        return {"plan": None, "dishes": []}
    return {"plan": dict(plan), "dishes": [sizes.with_size(d) for d in store().get_dishes(plan["id"])]}


@router.get("/thanksgiving/api/speak")
def api_speak(text: str = ""):
    """Hands-free's voice: OpenAI speech for `text`, streamed as mp3 so the tablet starts talking right away.
    A GET so an <audio> element can play it while it downloads. The tablet falls back to its own voice on error."""
    text = text.strip()[:voice.SPEECH_MAX_CHARS]
    if not text:
        raise HTTPException(status_code=400, detail="Nothing to say.")
    chunks = (_state.get("speaker") or voice.speech_chunks)(text)
    try:
        first = next(chunks)  # fail here (a clean error) rather than mid-stream
    except StopIteration:
        first = b""
    except Exception as error:
        print("Hands-free voice failed: " + json.dumps({"error": str(error)[:200]}))
        raise HTTPException(status_code=502, detail="Voice unavailable.")

    def body():
        yield first
        try:
            yield from chunks
        except Exception as error:
            print("Hands-free voice cut off: " + json.dumps({"error": str(error)[:200]}))

    return StreamingResponse(body(), media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/thanksgiving/api/warm")
def api_warm():
    """The voice panel calls this when it opens, so the first phrase doesn't wait on a cold connection."""
    warm_up()
    return {"ok": True}


@router.post("/thanksgiving/api/voice-log")
async def api_voice_log(request: Request):
    """The voice panel reports microphone/recognition problems here so they show up in the server log."""
    data = await _json(request)
    print("Voice problem: " + json.dumps({k: str(data.get(k) or "")[:300] for k in ("event", "detail", "agent")}))
    return {"ok": True}


@router.post("/thanksgiving/api/command/apply")
def api_command_apply():
    plan = require_plan()
    actions = _state["voice_pending"].pop(plan["id"], None)
    if not actions:
        raise HTTPException(status_code=409, detail="Nothing to apply. Say the change again.")
    _state["voice_undo"][plan["id"]] = voice.apply(store(), plan, actions)
    print("Tell Dieter: applied " + json.dumps([a["type"] for a in actions]))
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
    return back(f"/thanksgiving/menu/{dish_id}")


@router.post("/thanksgiving/dishes/{dish_id}/delete")
def delete_dish(dish_id: int):
    plan = require_plan()
    store().delete_dish(plan["id"], dish_id)
    return back("/thanksgiving/menu")


@router.post("/thanksgiving/steps/toggle")
def toggle_step(request: Request, key: str = Form(...), done: str = Form(""), anchor: str = Form("")):
    plan = require_plan()
    store().set_checked(plan["id"], "step", key, bool(done))
    return back("/thanksgiving/timeline", anchor, request)


@router.post("/thanksgiving/groceries/toggle")
def toggle_grocery(key: str = Form(...), done: str = Form(""), row: str = Form("")):
    plan = require_plan()
    set_grocery_checked(plan["id"], key, bool(done))
    # Without JavaScript the page reloads; land back on the item, not the top of the list.
    return back("/thanksgiving/shopping", row if re.fullmatch(r"[a-z0-9-]{1,80}", row) else "")


@router.post("/thanksgiving/groceries/put-back")
def put_back_grocery(key: str = Form(...)):
    """Back onto the list: an item set aside at the end of a trip."""
    plan = require_plan()
    put_back(plan["id"], key)
    return back("/thanksgiving/shopping")


@router.post("/thanksgiving/groceries/add")
def add_grocery(text: str = Form(...)):
    plan = require_plan()
    if text.strip():
        store().add_extra_grocery(plan["id"], text.strip())
    return back("/thanksgiving/shopping")


@router.post("/thanksgiving/groceries/{item_id}/delete")
def delete_grocery(item_id: int):
    plan = require_plan()
    store().delete_extra_grocery(plan["id"], item_id)
    return back("/thanksgiving/shopping")


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
    _state["platform"].report(dict(
        title=f"[Thanksgiving] {text.splitlines()[0][:80]}",
        area=FEEDBACK_AREA,
        page_url=page[:300],
        page_title="Thanksgiving planner",
        reporter_name=get("name", "") or "",
        reporter_email=get("email", "") or "",
        raw_feedback=text[:4000],
    ))


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
            "done": task["key"] in done, "section": task["section"], "series": task.get("series") or [],
            "series_button": task.get("series_button") or "",
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
    try:
        minutes = int(data.get("minutes") or 5)
    except (TypeError, ValueError):
        minutes = 5
    minutes = max(1, min(minutes, 24 * 60))  # a snooze is minutes to hours, never "forever"
    until = int(time.time() * 1000) + minutes * 60_000 if state == "snooze" else 0
    store().set_alert(plan["id"], key, state, until)
    return {"ok": True}


@router.post("/thanksgiving/api/feedback")
async def api_feedback(request: Request):
    data = await _json(request)
    save_feedback(request, str(data.get("text") or ""), str(data.get("page") or "/thanksgiving/cook"))
    return {"ok": True}


# ---------------------------------------------------------------- tools for Claude (core/connector)
# The main app's /mcp reaches these with a signed user when Thanksgiving runs as its own service.
from core.connector.tools import serve_tools  # noqa: E402

from . import connector  # noqa: E402

serve_tools(router, "/thanksgiving/api/connector/tools", connector.tools())
