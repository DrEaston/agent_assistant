"""Pure scheduling logic for the Thanksgiving planner.

Every dish is a list of steps. A step's anchor is either:
  "T-195"      195 minutes before dinner (T+10 = 10 minutes after)
  "D-1 10:00"  10:00 on the day before dinner day
No database or web imports, so this is easy to test. Datetimes are naive app-local.
"""

import re
import uuid
from datetime import datetime, timedelta

TEMP_TOLERANCE_F = 25           # dishes within 25°F can share an oven
DISHES_PER_OVEN = 3
STEP_KINDS = ("hands", "oven", "stove", "passive", "check", "shop")
KIND_LABELS = {"hands": "Hands-on", "oven": "Oven", "stove": "Stovetop", "passive": "Wait",
               "check": "Check", "shop": "Shopping"}

# Unstuffed turkey at 325°F, minutes (middle of the USDA ranges; check the temperature early).
ROAST_TABLE = ((12, 180), (14, 195), (18, 240), (20, 255), (24, 285))

_T_ANCHOR = re.compile(r"^T([+-])(\d+)$")
_D_ANCHOR = re.compile(r"^D-(\d+) (\d{1,2}):(\d{2})$")


def parse_dt(value):
    return datetime.fromisoformat(str(value)[:16])


def new_step_id():
    return "s" + uuid.uuid4().hex[:8]


# ---------------------------------------------------------------- anchors

def resolve_anchor(anchor, dinner):
    """Turn an anchor string into a datetime. Unknown anchors fall back to dinner time."""
    anchor = (anchor or "").strip()
    match = _T_ANCHOR.match(anchor)
    if match:
        minutes = int(match.group(2))
        return dinner + timedelta(minutes=minutes if match.group(1) == "+" else -minutes)
    match = _D_ANCHOR.match(anchor)
    if match:
        day = dinner.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=int(match.group(1)))
        return day.replace(hour=min(int(match.group(2)), 23), minute=min(int(match.group(3)), 59))
    return dinner


def anchor_for(day_offset, clock, dinner):
    """Build an anchor from a form's day offset (days before dinner day) and 'HH:MM'.

    Dinner-day steps are stored relative to dinner so they move when dinner moves.
    Earlier days keep their clock time.
    """
    try:
        hour, minute = (int(part) for part in str(clock).split(":")[:2])
    except (TypeError, ValueError):
        hour, minute = 12, 0
    day_offset = max(int(day_offset or 0), 0)
    if day_offset == 0:
        when = dinner.replace(hour=hour, minute=minute)
        delta = int((dinner - when).total_seconds() // 60)
        return f"T-{delta}" if delta >= 0 else f"T+{-delta}"
    return f"D-{day_offset} {hour:02d}:{minute:02d}"


def day_offset_of(when, dinner):
    return max((dinner.date() - when.date()).days, 0)


# ---------------------------------------------------------------- step generators

def turkey_roast_minutes(weight_lb):
    weight = float(weight_lb or 0)
    if weight <= 0:
        return 195
    for max_weight, minutes in ROAST_TABLE:
        if weight <= max_weight:
            return minutes
    return 285 + int((weight - 24) / 2 + 0.999) * 15


def turkey_thaw_days(weight_lb):
    """Fridge thawing: about 1 day per 4 lb."""
    weight = float(weight_lb or 0)
    return max(1, int(weight / 4 + 0.999)) if weight > 0 else 0


def _step(step_id, anchor, title, minutes=5, kind="hands", temp=0, detail=""):
    return {"id": step_id, "anchor": anchor, "title": title, "minutes": int(minutes), "kind": kind,
            "temp": int(temp or 0), "detail": detail, "cook": ""}


def turkey_steps(weight_lb, frozen=True, rest_min=45):
    """Steps for a roast turkey on a bed of vegetables, timed back from dinner."""
    roast = turkey_roast_minutes(weight_lb)
    out_at = rest_min                  # minutes before dinner the turkey comes out
    roast_start = out_at + roast
    steps = []
    if frozen and weight_lb:
        days = turkey_thaw_days(weight_lb)
        # Thawed by 8 AM the day before dinner.
        steps.append(_step("thaw", f"D-{days + 1} 08:00", "Move the turkey to the fridge to thaw",
                           days * 24 * 60, "passive",
                           detail=f"About {days} days for {float(weight_lb):g} lb. In its wrapper, on a rimmed tray, "
                                  "bottom shelf."))
    steps += [
        _step("veg", "D-1 15:00", "Chop the turkey vegetables", 15,
              detail="Onions in wedges, carrots and celery in 2-inch pieces. Refrigerate."),
        _step("out", f"T-{roast_start + 45}", "Turkey out of the fridge: neck and giblets out, pat dry", 15),
        _step("preheat", f"T-{roast_start + 30}", "Preheat the oven to 325°F", 30, "oven", 325),
        _step("butter", f"T-{roast_start + 15}", "Herb butter on; turkey onto the vegetables with the broth", 15),
        _step("roast", f"T-{roast_start}", "Roast the turkey", roast, "oven", 325,
              detail=f"About {roast // 60}h {roast % 60:02d}m. Add 1/2 cup broth if the pan gets dry."),
        _step("tent", f"T-{out_at + 75}", "Tent the breast with foil if golden", 5, "check"),
        _step("temp", f"T-{out_at + 45}", "First temperature check", 5, "check",
              detail="165°F in the thickest part of the breast and thigh. Not there? Check every 15 min."),
        _step("rest", f"T-{out_at}", "Turkey out: tent with foil and rest", out_at, "passive",
              detail="Oven is free now. Strain the drippings for gravy."),
        _step("carve", "T-15", "Carve the turkey", 15),
    ]
    return steps


def generic_steps(prep_min=15, cook_min=30, method="oven", temp=375, done_before_min=0, day_offset=0,
                  start_clock="13:00"):
    """Prep + cook steps for a simple dish, finished at dinner (or cooked on an earlier day)."""
    prep_min, cook_min = int(prep_min or 0), int(cook_min or 0)
    steps = []
    if day_offset:
        hour, minute = (int(x) for x in start_clock.split(":"))
        prep_start = hour * 60 + minute
        if prep_min:
            steps.append(_step("prep", f"D-{day_offset} {prep_start // 60:02d}:{prep_start % 60:02d}", "Prep", prep_min))
        cook_start = prep_start + prep_min
        if cook_min and method != "none":
            steps.append(_step("cook", f"D-{day_offset} {cook_start // 60:02d}:{cook_start % 60:02d}",
                               "Bake" if method == "oven" else "Cook", cook_min, method, temp if method == "oven" else 0))
        return steps
    cook_start = done_before_min + (cook_min if method != "none" else 0)
    if prep_min:
        steps.append(_step("prep", f"T-{cook_start + prep_min}", "Prep", prep_min))
    if cook_min and method != "none":
        steps.append(_step("cook", f"T-{cook_start}", "Bake" if method == "oven" else "Cook", cook_min, method,
                           temp if method == "oven" else 0))
    return steps


def steps_from_recipe_details(details):
    """Steps for a dish created from an imported recipe's extracted details."""
    details = details or {}
    temp = details.get("oven_temp_f") or 0
    cook = details.get("cook_minutes") or 0
    prep = details.get("prep_minutes") or (15 if cook else 30)
    method = "oven" if temp else ("stove" if cook else "none")
    return generic_steps(prep, cook, method, temp)


# ---------------------------------------------------------------- timeline

def build_timeline(plan, dishes, cooks=()):
    """Every step of every dish as a task with real times, sorted by start."""
    dinner = parse_dt(plan["dinner_at"])
    tasks = []
    for dish in dishes:
        for step in dish.get("steps") or []:
            start = resolve_anchor(step.get("anchor"), dinner)
            minutes = max(int(step.get("minutes") or 0), 0)
            tasks.append({
                "key": f"{dish['id']}:{step['id']}",
                "step_id": step["id"],
                "title": step.get("title") or "Step",
                "detail": step.get("detail") or "",
                "kind": step.get("kind") if step.get("kind") in STEP_KINDS else "hands",
                "temp": int(step.get("temp") or 0),
                "minutes": minutes,
                "start": start,
                "end": start + timedelta(minutes=minutes),
                "dish_id": dish["id"],
                "dish_name": dish["name"],
                "cook": (step.get("cook") or dish.get("cook") or "").strip(),
                "anchor": step.get("anchor"),
                "section": step.get("section") or "",
            })
    tasks.sort(key=lambda t: (t["start"], t["end"], t["dish_name"], t["title"]))
    return tasks


def _temp_clusters(temps):
    clusters = []
    for temp in sorted(temps):
        if clusters and temp - clusters[-1][0] <= TEMP_TOLERANCE_F:
            clusters[-1].append(temp)
        else:
            clusters.append([temp])
    return clusters


def _overlap_windows(tasks):
    """Yield (start, active tasks) for each interval where 2+ tasks overlap."""
    bounds = sorted({t["start"] for t in tasks} | {t["end"] for t in tasks})
    for start, end in zip(bounds, bounds[1:]):
        active = [t for t in tasks if t["start"] < end and t["end"] > start]
        if len(active) >= 2:
            yield start, active


def find_conflicts(tasks, ovens=1):
    """Plain-language warnings: oven temperature clashes and one cook double-booked."""
    ovens = max(int(ovens or 1), 1)
    warnings = []
    seen = set()

    oven_tasks = [t for t in tasks if t["kind"] == "oven" and t["minutes"] > 0]
    for start, active in _overlap_windows(oven_tasks):
        ids = ("oven",) + tuple(sorted(t["key"] for t in active))
        if ids in seen:
            continue
        clusters = _temp_clusters([t["temp"] for t in active])
        names = ", ".join(f"{t['dish_name']}: {t['title'].lower()} ({t['temp']}°F)" for t in active)
        if len(clusters) > ovens:
            seen.add(ids)
            warnings.append({"when": start, "text": f"Oven needed at different temperatures: {names}."})
        elif len(active) > ovens * DISHES_PER_OVEN:
            seen.add(ids)
            warnings.append({"when": start, "text": f"{len(active)} things in the oven at once: {names}."})

    hands = [t for t in tasks if t["kind"] == "hands" and t["minutes"] > 0 and t["cook"]]
    for cook in sorted({t["cook"] for t in hands}):
        mine = [t for t in hands if t["cook"] == cook]
        for start, active in _overlap_windows(mine):
            ids = (cook,) + tuple(sorted(t["key"] for t in active))
            if ids in seen:
                continue
            seen.add(ids)
            names = " and ".join(f"{t['dish_name']}: {t['title'].lower()}" for t in active)
            warnings.append({"when": start, "text": f"{cook} has two hands-on jobs at once: {names}."})

    warnings.sort(key=lambda w: w["when"])
    return warnings


def status_now(tasks, done_keys, now):
    """Unfinished tasks split into overdue / happening now / next up."""
    overdue, current, upcoming = [], [], []
    for task in tasks:
        if task["key"] in done_keys:
            continue
        if task["start"] > now:
            upcoming.append(task)
        elif task["end"] > now:
            current.append(task)
        elif task["kind"] != "passive":
            overdue.append(task)
    return {"overdue": overdue, "current": current, "next": upcoming[:4]}


def group_by_day(tasks, dinner):
    groups = []
    for task in tasks:
        date = task["start"].date()
        if not groups or groups[-1]["date"] != date:
            offset = (dinner.date() - date).days
            if offset == 0:
                label = "Thanksgiving"
            elif offset == 1:
                label = f"{task['start'].strftime('%A')} (day before)"
            else:
                label = task["start"].strftime("%A")
            groups.append({"date": date, "label": label, "subtitle": short_date(task["start"]), "tasks": []})
        groups[-1]["tasks"].append(task)
    return groups


def oven_chart(tasks, day):
    """Bars for one day's oven and stovetop use, as percentages of the busy window."""
    day_tasks = [t for t in tasks if t["kind"] in ("oven", "stove") and t["start"].date() == day.date()]
    if not day_tasks:
        return None
    start = min(t["start"] for t in day_tasks).replace(minute=0)
    end = max(t["end"] for t in day_tasks)
    if end.minute:
        end = end.replace(minute=0) + timedelta(hours=1)
    span = (end - start).total_seconds() or 1

    def bar(task):
        return {**task,
                "left": round((task["start"] - start).total_seconds() / span * 100, 2),
                "width": round(max((task["end"] - task["start"]).total_seconds() / span * 100, 1.5), 2)}

    ticks = []
    tick = start
    while tick <= end:
        ticks.append({"label": format_clock(tick, short=True), "left": round((tick - start).total_seconds() / span * 100, 2)})
        tick += timedelta(hours=1)
    return {
        "oven": _stack([bar(t) for t in day_tasks if t["kind"] == "oven"]),
        "stove": _stack([bar(t) for t in day_tasks if t["kind"] == "stove"]),
        "ticks": ticks,
    }


def _stack(bars):
    rows = []
    for bar in sorted(bars, key=lambda b: b["start"]):
        for row in rows:
            if row[-1]["end"] <= bar["start"]:
                row.append(bar)
                break
        else:
            rows.append([bar])
    return rows


# ---------------------------------------------------------------- formatting

def format_clock(value, short=False):
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    if short and value.minute == 0:
        return f"{hour} {suffix}"
    return f"{hour}:{value.minute:02d} {suffix}"


def short_date(value):
    return f"{value.strftime('%a')}, {value.strftime('%b')} {value.day}"


def format_duration(minutes):
    minutes = int(minutes)
    if minutes >= 48 * 60:
        return f"{round(minutes / 1440)} days"
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    return f"{hours}h" if hours else f"{mins} min"


# ---------------------------------------------------------------- shopping list

GROCERY_SECTIONS = (
    ("Produce", ("potato", "onion", "celery", "lemon", "orange", "apple", "cranberr", "green bean", "sprout",
                 "lettuce", "cucumber", "tomato", "thyme", "rosemary", "sage", "parsley", "garlic", "carrot",
                 "herb", "mushroom", "shallot")),
    ("Meat", ("turkey", "sausage", "bacon", "chicken")),
    ("Dairy & eggs", ("butter", "milk", "cream", "egg", "cheese", "half-and-half", "whipped")),
    ("Bakery & bread", ("bread", "roll", "crust", "loaf", "stuffing cubes", "pie shell")),
    ("Pantry & baking", ("flour", "sugar", "salt", "pepper", "spice", "cinnamon", "vanilla", "stock", "broth",
                         "oil", "vinegar", "soy", "puree", "starter", "vinaigrette", "fried onion")),
)


def grocery_section(line):
    text = line.lower()
    if "drippings" in text or "starter" in text:
        return None  # made, not bought
    if "smokies" in text:
        return "Meat"
    if any(word in text for word in ("fried onion", "powder", "panko", "macaroni", "pasta", "mustard",
                                     "paprika", "hot sauce")) or " can " in f" {text} ":
        return "Pantry & baking"
    if "stuffing cubes" in text or "pie shell" in text:
        return "Bakery & bread"
    for name, words in GROCERY_SECTIONS:
        if any(word in text for word in words):
            return name
    return "Other"


def normalize_key(line):
    return re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()


def build_grocery_list(dishes, extra_items=()):
    merged = {}
    for dish in dishes:
        for raw in (dish.get("ingredients_text") or "").splitlines():
            line = raw.strip(" -•*\t")
            key = normalize_key(line)
            if not key or is_heading(line):
                continue
            section = grocery_section(line)
            if section is None:
                continue
            item = merged.setdefault(key, {"key": key, "text": line, "dishes": [], "extra_id": None,
                                           "section": section})
            if dish["name"] not in item["dishes"]:
                item["dishes"].append(dish["name"])
    for extra in extra_items:
        key = f"extra-{extra['id']}"
        merged[key] = {"key": key, "text": extra["text"], "dishes": [], "extra_id": extra["id"],
                       "section": grocery_section(extra["text"]) or "Other"}
    order = [name for name, _ in GROCERY_SECTIONS] + ["Other"]
    sections = []
    for name in order:
        items = sorted((i for i in merged.values() if i["section"] == name), key=lambda i: i["text"].lower())
        if items:
            sections.append({"name": name, "items": items})
    return sections


# ---------------------------------------------------------------- recipe text helpers

def is_heading(line):
    """A recipe section heading is a short line ending in ':' (e.g. 'Crust:')."""
    line = (line or "").strip()
    return line.endswith(":") and len(line) <= 40


def recipe_sections(text, with_timers=False):
    """Split recipe text into [{heading, lines}] using 'Heading:' lines. Text before any heading has heading ''."""
    sections = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if is_heading(line):
            sections.append({"heading": line[:-1].strip(), "lines": []})
            continue
        if not sections:
            sections.append({"heading": "", "lines": []})
        sections[-1]["lines"].append(instruction_lines(line)[0] if with_timers else line)
    return [s for s in sections if s["lines"]]


def sections_for_step(sections, section_name):
    """The sections a step needs: those whose heading starts with the step's section name. Empty = all."""
    name = (section_name or "").strip().lower()
    if not name:
        return []
    return [s for s in sections if s["heading"].lower().startswith(name)]


_DURATION = re.compile(r"(\d+)(?:\s*(?:to|-|–)\s*(\d+))?\s*(minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE)


def instruction_lines(text):
    """Split instructions into lines, each with any durations found (for one-tap timers)."""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        timers = []
        for match in _DURATION.finditer(line):
            value = int(match.group(2) or match.group(1))
            minutes = value * 60 if match.group(3).lower().startswith("h") else value
            if 0 < minutes <= 12 * 60 and minutes not in timers:
                timers.append(minutes)
        lines.append({"text": line, "timers": timers})
    return lines
