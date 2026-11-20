"""Spoken or typed commands for the Thanksgiving planner ("move the pie crust to Saturday at 7").

Flow: understand() asks the model to turn the request into actions from a fixed list, then
check() validates them against the plan and describes each change in plain words. Nothing is
saved until the user taps Apply, which calls apply(); apply() returns an undo record.

The model never writes to the database. Everything it proposes goes through check(), which uses
the same anchor rules as the dish editor, so voice can't do anything the editor can't.
"""

import json
import os
from datetime import datetime, timedelta

from . import planner, sizes

ACTION_TYPES = ("move", "shift", "assign", "size", "done")

SYSTEM_PROMPT = """You turn a cook's spoken or typed request into changes to their Thanksgiving plan.

Reply with a JSON object only:
{"reply": "<one short sentence saying what you WILL change, e.g. 'I'll move the pie crust to Saturday at 7 PM.' Nothing is saved until the cook taps Apply, so never say it's done>",
 "question": "<a clarifying question, or empty>",
 "actions": [ ... ]}

Allowed actions (use dish ids and step ids exactly as listed in the plan):
- {"type": "move", "dish_id": 7, "step_ids": ["dough", "shell"], "date": "YYYY-MM-DD", "time": "HH:MM"}
  Moves the earliest of those steps to that date and time; the others keep their spacing after it.
- {"type": "shift", "dish_id": 7, "step_ids": ["bake"], "days": 1, "minutes": 0}
  Moves steps later (positive) or earlier (negative).
- {"type": "assign", "dish_id": 7, "step_ids": [], "cook": "Parrisa"}
  Assigns steps to a cook. Empty step_ids means the whole dish. Use "" to unassign.
- {"type": "size", "dish_id": 7, "scale": 0.5}   Only a scale listed for that dish.
- {"type": "done", "dish_id": 7, "step_ids": ["dough"], "done": true}

Rules:
- "The crust" or "the dough" means the steps whose section is Crust (or whose title is about the crust/dough).
- Resolve days like "Saturday" or "tomorrow" from today's date. Steps must stay on or before dinner day.
- If the request is ambiguous (which day, what time, which dish), ask ONE short question and return no actions.
  Example: "the weekend" with no day -> ask "Saturday or Sunday, and what time?"
- If the request is not about the plan, reply briefly and return no actions.
- Never invent dishes, steps, cooks or sizes that aren't in the plan."""


def plan_summary(plan, dishes, tasks, cooks, now):
    """The plan as the model sees it: dates, dishes, sizes, and every step with its id and time."""
    dinner = planner.parse_dt(plan["dinner_at"])
    by_dish = {}
    for task in tasks:
        by_dish.setdefault(task["dish_id"], []).append(task)
    lines = [
        f"Now: {now:%A %Y-%m-%d %H:%M}",
        f"Dinner: {dinner:%A %Y-%m-%d %H:%M}",
        "Next days: " + ", ".join(f"{(now + timedelta(days=i)):%a %m-%d}" for i in range(0, 9)),
        f"Cooks: {', '.join(cooks) or 'none'}",
        "",
    ]
    for dish in dishes:
        size = ""
        if dish.get("size_options"):
            options = ", ".join(f"{o['value']} = {o['label']}" for o in dish["size_options"])
            size = f" | size {dish.get('size_value')} (options: {options})"
        lines.append(f"Dish {dish['id']}: {dish['name']} | cook: {dish.get('cook') or 'unassigned'}{size}")
        for task in sorted(by_dish.get(dish["id"], []), key=lambda t: t["start"]):
            section = f" [section {task['section']}]" if task.get("section") else ""
            done = " (done)" if task.get("done") else ""
            cook = f" cook {task['cook']}" if task.get("cook") else ""
            lines.append(f"  step {task['step_id']}: {task['title']}{section} | "
                         f"{task['start']:%a %Y-%m-%d %H:%M}, {task['minutes']} min{cook}{done}")
    return "\n".join(lines)


def default_model(messages):
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
    response = client.chat.completions.create(model=model, messages=messages,
                                              response_format={"type": "json_object"})
    return response.choices[0].message.content


def understand(text, summary, history=(), model=None):
    """Ask the model for actions. history: earlier [{"role", "content"}] turns of this conversation."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "system", "content": "The plan:\n" + summary}]
    messages.extend({"role": h["role"], "content": str(h["content"])[:2000]} for h in list(history)[-6:]
                    if h.get("role") in ("user", "assistant"))
    messages.append({"role": "user", "content": text[:1000]})
    raw = (model or default_model)(messages)
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"reply": "Sorry, I didn't catch that. Try saying it another way.", "question": "", "actions": []}
    actions = data.get("actions") if isinstance(data.get("actions"), list) else []
    return {"reply": str(data.get("reply") or "")[:300], "question": str(data.get("question") or "")[:300],
            "actions": [a for a in actions if isinstance(a, dict)][:12]}


# ---------------------------------------------------------------- checking and applying

class Rejected(ValueError):
    """An action that doesn't fit the plan; the message is shown to the cook."""


def _fmt(when):
    return f"{planner.short_date(when)[:3]} {planner.format_clock(when)}"


def _anchor(when, dinner):
    offset = (dinner.date() - when.date()).days
    if offset < 0:
        raise Rejected(f"{_fmt(when)} is after dinner.")
    return planner.anchor_for(offset, f"{when:%H:%M}", dinner)


def check(actions, plan, dishes, tasks, cooks):
    """Validate actions and describe them. Returns (normalized actions, change lines, problems)."""
    dinner = planner.parse_dt(plan["dinner_at"])
    dishes_by_id = {d["id"]: d for d in dishes}
    tasks_by_key = {(t["dish_id"], t["step_id"]): t for t in tasks}
    normalized, lines, problems = [], [], []
    for action in actions:
        try:
            kind = action.get("type")
            if kind not in ACTION_TYPES:
                raise Rejected(f"I can't do '{kind}' yet.")
            dish = dishes_by_id.get(_int(action.get("dish_id")))
            if not dish:
                raise Rejected("That dish isn't in the plan.")
            step_ids = [str(s) for s in action.get("step_ids") or []]
            steps = [tasks_by_key.get((dish["id"], s)) for s in step_ids]
            if any(s is None for s in steps):
                raise Rejected(f"Some of those {dish['name']} steps aren't in the plan.")
            if kind in ("move", "shift"):
                if not steps:
                    raise Rejected(f"Which {dish['name']} steps?")
                if kind == "move":
                    try:
                        target = datetime.strptime(f"{action.get('date')} {action.get('time')}", "%Y-%m-%d %H:%M")
                    except (TypeError, ValueError):
                        raise Rejected("I need a day and a time for that move.") from None
                    delta = target - min(s["start"] for s in steps)
                else:
                    delta = timedelta(days=_int(action.get("days")), minutes=_int(action.get("minutes")))
                moves = []
                for step in sorted(steps, key=lambda s: s["start"]):
                    new_start = step["start"] + delta
                    moves.append({"step_id": step["step_id"], "anchor": _anchor(new_start, dinner)})
                    lines.append(f"{dish['name']}: {step['title']}: {_fmt(step['start'])} → {_fmt(new_start)}")
                normalized.append({"type": "move", "dish_id": dish["id"], "moves": moves})
            elif kind == "assign":
                cook = str(action.get("cook") or "").strip()
                if cook and cook not in cooks:
                    raise Rejected(f"{cook} isn't one of the cooks ({', '.join(cooks)}).")
                ids = step_ids or [s["id"] for s in dish["steps"]]
                normalized.append({"type": "assign", "dish_id": dish["id"], "step_ids": ids, "cook": cook})
                target = "all steps" if not step_ids else ", ".join(s["title"] for s in steps)
                lines.append(f"{dish['name']} ({target}): {'assign to ' + cook if cook else 'unassign'}")
            elif kind == "size":
                scale = sizes.parse_scale(dish, action.get("scale"))
                if scale is None:
                    raise Rejected(f"{dish['name']} doesn't have that size.")
                label = next((o["label"] for o in dish.get("size_options") or [] if float(o["value"]) == scale), "")
                normalized.append({"type": "size", "dish_id": dish["id"], "scale": scale})
                lines.append(f"{dish['name']}: batch size → {label}")
            elif kind == "done":
                if not steps:
                    raise Rejected(f"Which {dish['name']} steps?")
                done = bool(action.get("done", True))
                normalized.append({"type": "done", "dish_id": dish["id"], "keys": [s["key"] for s in steps],
                                   "done": done})
                for step in steps:
                    lines.append(f"{dish['name']}: {step['title']}: mark {'done' if done else 'not done'}")
        except Rejected as exc:
            problems.append(str(exc))
    return normalized, lines, problems


def apply(store, plan, normalized):
    """Save checked actions. Returns an undo record (the dishes' previous steps, sizes and checkmarks)."""
    undo = {"dishes": {}, "checks": {}}
    for action in normalized:
        dish = store.get_dish(plan["id"], action["dish_id"])
        if not dish:
            continue
        key = str(dish["id"])
        undo["dishes"].setdefault(key, {"steps": dish["steps"], "scale": dish.get("scale") or 1})
        if action["type"] == "move":
            anchors = {m["step_id"]: m["anchor"] for m in action["moves"]}
            dish["steps"] = [dict(s, anchor=anchors.get(s["id"], s["anchor"])) for s in dish["steps"]]
            store.update_dish(plan["id"], dish["id"], dish)
        elif action["type"] == "assign":
            ids = set(action["step_ids"])
            dish["steps"] = [dict(s, cook=action["cook"]) if s["id"] in ids else s for s in dish["steps"]]
            store.update_dish(plan["id"], dish["id"], dish)
        elif action["type"] == "size":
            store.set_scale(plan["id"], dish["id"], action["scale"])
        elif action["type"] == "done":
            checked = store.get_checked(plan["id"], "step")
            for step_key in action["keys"]:
                undo["checks"].setdefault(step_key, step_key in checked)
                store.set_checked(plan["id"], "step", step_key, action["done"])
    return undo


def undo(store, plan, record):
    """Put back what apply() changed."""
    for dish_id, before in (record.get("dishes") or {}).items():
        dish = store.get_dish(plan["id"], _int(dish_id))
        if not dish:
            continue
        dish["steps"] = before.get("steps") or dish["steps"]
        store.update_dish(plan["id"], dish["id"], dish)
        store.set_scale(plan["id"], dish["id"], float(before.get("scale") or 1))
    for step_key, was_done in (record.get("checks") or {}).items():
        store.set_checked(plan["id"], "step", str(step_key), bool(was_done))


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
