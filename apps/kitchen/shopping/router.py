"""Kitchen shopping lists: pick meals, get one totaled list sorted by aisle, check things off.

api.py owns recipes and the meal plan, so it hands this module three functions via configure():
  meal_sources(ids)        -> [{"id", "title", "text", "url", "status"}] for meal-plan items
  pending_meals()          -> [{"id", "title"}] meals waiting on the meal plan
  component_sources(ids)   -> [(title, ingredient text)] for the meal builder's preview
"""

import hashlib
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from apps.kitchen import shopping_list
from apps.kitchen.manifest import APP as KITCHEN_APP
from core import ui

from .store import ShoppingStore

router = APIRouter()
BASE = "/apps/recipes/grocery-lists"
FEEDBACK_AREA = "Kitchen / Recipes"

_templates = ui.templates(Path(__file__).parent / "templates")
_state = {"db": None, "store": None, "meal_sources": None, "pending_meals": None, "component_sources": None}


def configure(db, meal_sources, pending_meals, component_sources, thanksgiving=None):
    # thanksgiving: () -> (plan, sized dishes) when Thanksgiving runs as its own service; None = read it here.
    _state.update(db=db, store=ShoppingStore(db), meal_sources=meal_sources, pending_meals=pending_meals,
                  component_sources=component_sources, thanksgiving=thanksgiving)


def store():
    if _state["store"] is None:
        raise RuntimeError("Shopping lists are not configured")
    return _state["store"]


def render(name, **context):
    context.setdefault("app", KITCHEN_APP)
    context.setdefault("report_url", BASE + "/feedback")
    return HTMLResponse(_templates.get_template(name).render(**context))


def to_list(list_id, anchor=""):
    return RedirectResponse(url=f"{BASE}/{list_id}" + (f"#{anchor}" if anchor else ""), status_code=303)


def require_list(list_id):
    grocery_list = store().get_list(list_id)
    if not grocery_list:
        raise HTTPException(status_code=404, detail="Shopping list not found")
    return grocery_list


def default_title(now=None):
    now = now or datetime.now()
    return f"Shopping · {now.strftime('%a %b')} {now.day}"


def friendly_date(value):
    try:
        when = datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return str(value or "")
    return f"{when.strftime('%a %b')} {when.day}"


# ---------------------------------------------------------------- Thanksgiving dishes

def _thanksgiving():
    """The user's Thanksgiving plan and dishes, or (None, []) when there is no plan."""
    if _state.get("thanksgiving"):
        try:
            return _state["thanksgiving"]()
        except Exception as exc:
            print(f"Warning: Thanksgiving dishes unavailable: {exc}")
            return None, []
    from apps.kitchen.thanksgiving import router as tg_router, sizes
    try:
        plan = tg_router.store().get_plan()
    except RuntimeError:
        return None, []
    if not plan:
        return None, []
    return plan, [sizes.with_size(d) for d in tg_router.store().get_dishes(plan["id"])]  # chosen batch size


def _dish_parts(dish):
    """Ingredient section headings, when a dish has separate parts (pie: Crust, Filling, To serve)."""
    from apps.kitchen.thanksgiving import planner
    headings = [s["heading"] for s in planner.recipe_sections(dish.get("ingredients_text") or "")
                if s["heading"] and s["heading"].lower() != (dish.get("name") or "").lower()]
    return headings if len(headings) > 1 else []


def thanksgiving_choices():
    """Dishes (and dish parts) you can shop for: [{"value": "7:", "label": "Pumpkin pie", "parts": [...]}]."""
    _, dishes = _thanksgiving()
    choices = []
    for dish in dishes:
        if not (dish.get("ingredients_text") or "").strip():
            continue
        parts = _dish_parts(dish)
        choices.append({"value": f"{dish['id']}:", "label": dish["name"], "parts": [
            {"value": f"{dish['id']}:{part}", "label": f"{part} only"} for part in parts]})
    return choices


def _valid_dish_values():
    return {c["value"] for choice in thanksgiving_choices() for c in [choice] + choice["parts"]}


def parse_dish_pick(value):
    dish_id, _, part = str(value or "").partition(":")
    return {"dish_id": int(dish_id), "part": part.strip()[:60]} if dish_id.isdigit() else None


def dish_sources(dish_parts):
    """[{"key", "title", "text", "url"}] read live from the Thanksgiving plan."""
    from apps.kitchen.thanksgiving import planner
    if not dish_parts:
        return []
    _, dishes = _thanksgiving()
    by_id = {d["id"]: d for d in dishes}
    entries = []
    for pick in dish_parts:
        dish = by_id.get(pick["dish_id"])
        if not dish:
            continue
        text = dish.get("ingredients_text") or ""
        title = dish["name"]
        if pick["part"]:
            sections = [s for s in planner.recipe_sections(text) if s["heading"] == pick["part"]]
            text = "\n".join(line for s in sections for line in s["lines"])
            title = f"{dish['name']} ({pick['part'].lower()})"
        entries.append({"key": f"{dish['id']}:{pick['part']}", "title": title, "text": text,
                        "url": "/thanksgiving/menu"})
    return entries


# ---------------------------------------------------------------- building a list

def list_items(grocery_list):
    """A list's meals, dishes, aisle sections and checkmarks, with counts (no page extras)."""
    s = store()
    meals = _state["meal_sources"](grocery_list["meal_ids"])
    dishes = dish_sources(grocery_list.get("dish_parts") or [])
    sources = [(meal["title"], meal["text"]) for meal in meals] + [(d["title"], d["text"]) for d in dishes]
    if (grocery_list.get("format") or 1) < 2:
        live = shopping_list.build_shopping_list(sources)
        live_keys = {item["key"] for section in live for item in section["items"]}
        s.convert_legacy(grocery_list, live_keys, shopping_list.item_key)
    extras = s.get_extras(grocery_list["id"])
    sections = shopping_list.build_shopping_list(sources, extras)
    checked = s.get_checked(grocery_list["id"])
    buy_keys = [item["key"] for section in sections if section["name"] != shopping_list.PANTRY_CHECK
                for item in section["items"]]
    return {"meals": meals, "dishes": dishes, "sections": sections, "checked": checked,
            "left": sum(1 for k in buy_keys if k not in checked), "total": len(buy_keys)}


def list_context(grocery_list):
    """Everything a list page needs: meals, aisle sections, checkmarks, counts."""
    items = list_items(grocery_list)
    meals, dishes, sections, checked = items["meals"], items["dishes"], items["sections"], items["checked"]
    keys = [item["key"] for section in sections for item in section["items"]]
    on_list = set(grocery_list["meal_ids"])
    addable = [m for m in _state["pending_meals"]() if m["id"] not in on_list]
    dish_keys = {d["key"] for d in dishes}
    addable_dishes = []
    for choice in thanksgiving_choices():
        options = [choice] + [dict(p, label=f"{choice['label']}: {p['label'].lower()}") for p in choice["parts"]]
        for option in options:
            if option["value"] not in dish_keys:
                addable_dishes.append({"value": option["value"], "label": option["label"]})
    return {
        "grocery_list": grocery_list,
        "created": friendly_date(grocery_list.get("created_at")),
        "meals": meals,
        "dishes": dishes,
        "addable_meals": addable,
        "addable_dishes": addable_dishes,
        "sections": sections,
        "checked": checked,
        "left": items["left"],
        "total": items["total"],
        "signature": signature(keys),
        "pantry_name": shopping_list.PANTRY_CHECK,
    }


def signature(keys):
    """Changes when the set of items changes (another device added a meal or item)."""
    return hashlib.sha1("|".join(sorted(keys)).encode()).hexdigest()[:12]


def summary(grocery_list):
    """Counts for the index and home pages, without rendering the whole list."""
    context = list_context(grocery_list)
    return {"id": grocery_list["id"], "title": grocery_list.get("title") or "Shopping list",
            "created": context["created"], "left": context["left"], "total": context["total"],
            "meal_titles": [m["title"] for m in context["meals"] + context["dishes"]],
            "status": grocery_list.get("status"),
            "done_at": friendly_date(grocery_list.get("done_at")) if grocery_list.get("done_at") else ""}


def active_summaries(limit=8):
    return [summary(g) for g in store().get_lists("active", limit)]


# ---------------------------------------------------------------- pages

@router.get(BASE, response_class=HTMLResponse)
def lists_page(request: Request):
    active = [summary(g) for g in store().get_lists("active", 50)]
    done = [summary(g) for g in store().get_lists("done", 50)]
    return render("shop_index.html", active=active, done=done, page_url=BASE,
                  reported=request.query_params.get("reported") == "1")


@router.get(BASE + "/new", response_class=HTMLResponse)
def new_list_page():
    return render("shop_new.html", meals=_state["pending_meals"](), dishes=thanksgiving_choices(),
                  title=default_title(), page_url=BASE + "/new")


@router.post(BASE + "/create")
async def create_list(request: Request):
    form = await request.form()
    pending = {m["id"] for m in _state["pending_meals"]()}
    dish_parts = []
    if "picked" in form:
        meal_ids = [int(v) for v in form.getlist("meal_id") if str(v).isdigit() and int(v) in pending]
        valid = _valid_dish_values()
        for value in form.getlist("dish"):
            pick = parse_dish_pick(value)
            if pick and str(value) in valid and pick not in dish_parts:
                dish_parts.append(pick)
        whole = {d["dish_id"] for d in dish_parts if not d["part"]}
        dish_parts = [d for d in dish_parts if not d["part"] or d["dish_id"] not in whole]  # no double counting
    else:
        meal_ids = sorted(pending)  # the old one-tap button: everything on the meal plan
    title = str(form.get("title") or "").strip() or default_title()
    list_id = store().create_list(title, meal_ids, dish_parts)
    return to_list(list_id)


@router.get(BASE + "/{list_id}", response_class=HTMLResponse)
def list_page(request: Request, list_id: int):
    grocery_list = require_list(list_id)
    return render("shop_list.html", **list_context(grocery_list), page_url=f"{BASE}/{list_id}",
                  reported=request.query_params.get("reported") == "1")


@router.get(BASE + "/{list_id}/state")
def list_state(list_id: int):
    context = list_context(require_list(list_id))
    return {"checked": sorted(context["checked"]), "signature": context["signature"], "left": context["left"],
            "status": context["grocery_list"].get("status")}


# ---------------------------------------------------------------- actions

@router.post(BASE + "/{list_id}/check")
async def check_item(request: Request, list_id: int):
    require_list(list_id)
    if request.headers.get("content-type", "").startswith("application/json"):
        data = await request.json()
        key, done = str(data.get("key") or ""), bool(data.get("done"))
    else:
        form = await request.form()
        key, done = str(form.get("key") or ""), bool(form.get("done"))
    if not key:
        raise HTTPException(status_code=400, detail="Missing item")
    store().set_checked(list_id, key, done)
    if request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return to_list(list_id, "list")


@router.post(BASE + "/{list_id}/extras/add")
def add_extra(list_id: int, text: str = Form(...)):
    require_list(list_id)
    for line in text.splitlines():
        if line.strip():
            store().add_extra(list_id, line)
    return to_list(list_id, "list")


@router.post(BASE + "/{list_id}/extras/{extra_id}/delete")
def delete_extra(list_id: int, extra_id: int):
    require_list(list_id)
    store().delete_extra(list_id, extra_id)
    return to_list(list_id, "list")


@router.post(BASE + "/{list_id}/meals/add")
def add_meal(list_id: int, pick: str = Form("")):
    """pick is "meal:12" (meal plan) or "dish:7:Crust" (a Thanksgiving dish or one part of it)."""
    grocery_list = require_list(list_id)
    kind, _, value = pick.partition(":")
    if kind == "meal" and value.isdigit():
        meal_id = int(value)
        if meal_id in {m["id"] for m in _state["pending_meals"]()} and meal_id not in grocery_list["meal_ids"]:
            store().set_meals(list_id, grocery_list["meal_ids"] + [meal_id])
    elif kind == "dish":
        dish_pick = parse_dish_pick(value)
        if dish_pick and value in _valid_dish_values() and dish_pick not in grocery_list["dish_parts"]:
            store().set_dish_parts(list_id, grocery_list["dish_parts"] + [dish_pick])
    return to_list(list_id, "meals")


@router.post(BASE + "/{list_id}/dishes/remove")
def remove_dish(list_id: int, key: str = Form(...)):
    grocery_list = require_list(list_id)
    pick = parse_dish_pick(key)
    store().set_dish_parts(list_id, [d for d in grocery_list["dish_parts"] if d != pick])
    return to_list(list_id, "meals")


@router.post(BASE + "/{list_id}/meals/{meal_id}/remove")
def remove_meal(list_id: int, meal_id: int):
    grocery_list = require_list(list_id)
    store().set_meals(list_id, [i for i in grocery_list["meal_ids"] if i != meal_id])
    return to_list(list_id, "meals")


@router.post(BASE + "/{list_id}/rename")
def rename_list(list_id: int, title: str = Form(...)):
    require_list(list_id)
    if title.strip():
        store().rename(list_id, title.strip())
    return to_list(list_id)


@router.post(BASE + "/{list_id}/done")
def finish_list(list_id: int):
    require_list(list_id)
    store().set_status(list_id, "done")
    return RedirectResponse(url="/apps/recipes#meal-plan", status_code=303)


@router.post(BASE + "/{list_id}/reopen")
def reopen_list(list_id: int):
    require_list(list_id)
    store().set_status(list_id, "active")
    return to_list(list_id)


@router.post(BASE + "/{list_id}/delete")
def delete_list(list_id: int):
    require_list(list_id)  # also stops deleting someone else's list
    store().delete_list(list_id)
    return RedirectResponse(url=BASE, status_code=303)


@router.post(BASE + "/preview")
async def preview(request: Request):
    """The meal builder's "Preview shopping list": same totals as a real list, nothing saved."""
    data = await request.json()
    ids = [int(i) for i in data.get("component_ids") or [] if str(i).isdigit()]
    sections = shopping_list.build_shopping_list(_state["component_sources"](ids))
    return {"count": shopping_list.count_items(sections),
            "sections": [{"name": s["name"], "items": [{"text": i["text"], "amount": i["amount"],
                                                         "dishes": i["dishes"]} for i in s["items"]]}
                         for s in sections]}


@router.post(BASE + "/feedback")
def feedback(request: Request, text: str = Form(...), page: str = Form(BASE)):
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Describe the problem.")
    user = getattr(request.state, "current_user", None) or {}
    get = user.get if hasattr(user, "get") else (lambda k, d="": getattr(user, k, d))
    _state["db"].add_app_feedback_report(
        title=f"[Shopping] {text.splitlines()[0][:80]}",
        area=FEEDBACK_AREA,
        page_url=page[:300],
        page_title="Shopping list",
        reporter_name=get("name", "") or "",
        reporter_email=get("email", "") or "",
        raw_feedback=text[:4000],
    )
    target = page if page.startswith(BASE) else BASE
    return RedirectResponse(url=target + "?reported=1", status_code=303)
