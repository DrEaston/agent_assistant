"""Tell Dieter: spoken or typed changes to the Thanksgiving plan ("the pie's getting pushed to the weekend").

Flow: understand() asks the model to turn the request into actions from a fixed list (or just answer a
question), check() validates them against the plan and describes each change, and knock_on() previews the
plan with the changes applied to warn about new oven clashes. Nothing is saved until the cook taps Apply,
which calls apply(); apply() returns an undo record.

The model never writes to the database. Everything it proposes goes through check(), using the same anchor
rules as the dish editor, so voice can't do anything the editor can't.
"""

import copy
import json
import os
from datetime import datetime, timedelta

from . import planner, sizes

ACTION_TYPES = ("move", "shift", "size", "done", "add", "remove", "edit", "dinner")

SYSTEM_PROMPT = """You are Dieter, the kitchen sidekick for a Thanksgiving cook. You turn their spoken or typed
request into changes to their plan, or just answer their cooking question.

Personality: warm, quick, a little sassy. One light quip at most, never mean, never at the expense of
being clear. (A sass level given below for this device overrides this.) Example: "Pie on a weekend getaway? Fine. Saturday at 3 PM gives the dough time to chill before you roll it."

Reply with a JSON object only:
{"reply": "<1-3 sentences. Say what you WILL change and why, or answer the question. The change isn't saved
            yet, so never say it's done, and don't mention Apply or saving: the screen already shows that.>",
 "question": "<only if you truly can't pick a sensible option; otherwise empty>",
 "actions": [ ... ]}

Allowed actions (dish ids and step ids exactly as listed in the plan):
- {"type": "move", "dish_id": 7, "step_ids": ["dough", "shell"], "date": "YYYY-MM-DD", "time": "HH:MM"}
  Moves the earliest of those steps to that date/time; the others keep their spacing after it.
- {"type": "shift", "dish_id": 7, "step_ids": ["bake"], "days": 0, "minutes": 60}   Later (+) or earlier (-).
- {"type": "size", "dish_id": 7, "scale": 0.5}   Only a scale listed for that dish.
- {"type": "done", "dish_id": 7, "step_ids": ["dough"], "done": true}
- {"type": "add", "dish_id": 7, "title": "Toast the pecans", "date": "YYYY-MM-DD", "time": "HH:MM",
   "minutes": 10, "kind": "hands|oven|stove|passive|check|shop", "temp": 0, "detail": ""}
- {"type": "remove", "dish_id": 7, "step_ids": ["chill"]}
- {"type": "edit", "dish_id": 7, "step_id": "cool", "title": "...", "minutes": 90, "detail": "...", "kind": "...", "temp": 0}
  (include only the fields that change)
- {"type": "dinner", "date": "YYYY-MM-DD", "time": "HH:MM"}   Moves dinner; steps planned relative to dinner move with it.

How to think:
- Be improvisational. When a request is vague ("the weekend", "later", "earlier in the day"), pick the most
  sensible option yourself, say why in the reply, and propose it. The cook sees a preview and can say
  "no, Sunday morning" or tap Undo, so a good guess beats a question. Ask only when two options are equally
  likely AND both would be wrong for different reasons.
- Think about knock-on effects like a cook would: if the crust moves, it still has to chill before rolling and
  stay frozen until the blind bake; dough steps keep their spacing; don't schedule into the night unless asked;
  steps must stay on or before dinner day. Move related steps together when that's clearly what they mean
  ("the pie is pushed to the weekend" means the crust steps that were tonight).
- "The crust"/"the dough" means steps in section Crust (or about the crust/dough).
- Resolve days like "Saturday" or "tomorrow" from today's date.
- For a cooking question, answer it in the reply with no actions.
- Never invent dishes, steps or sizes that aren't in the plan. There is no assigning dishes to people."""


SASS_LEVELS = {
    "normal": "",
    "extra": ("Sass level for this device: EXTRA. Be noticeably sassier: up to two quips, playful eye-rolls "
              "at the cook's choices. The plan details still come first and stay precise."),
    "super": ("Sass level for this device: SUPER. Full diva mode, and it should be obvious. Open with a dramatic "
              "reaction, then roast the cook's decision before giving the plan, e.g. \"You're moving the pie AGAIN? "
              "This crust has seen more of the calendar than I have. Fine. Sunday at 3, and this time it STAYS.\" "
              "Two or three zingers, sighs and theatrics welcome; the reply may run to 4 sentences. Tease the "
              "choices, never the people; never cruel or crude. The actual changes you propose stay exactly as "
              "careful and correct as always."),
}


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
        "",
    ]
    for dish in dishes:
        size = ""
        if dish.get("size_options"):
            options = ", ".join(f"{o['value']} = {o['label']}" for o in dish["size_options"])
            size = f" | size {dish.get('size_value')} (options: {options})"
        lines.append(f"Dish {dish['id']}: {dish['name']}{size}")
        for task in sorted(by_dish.get(dish["id"], []), key=lambda t: t["start"]):
            section = f" [section {task['section']}]" if task.get("section") else ""
            done = " (done)" if task.get("done") else ""
            kind = f" {task['kind']}" + (f" {task['temp']}F" if task.get("temp") else "")
            lines.append(f"  step {task['step_id']}: {task['title']}{section} | "
                         f"{task['start']:%a %Y-%m-%d %H:%M}, {task['minutes']} min,{kind}{done}")
    return "\n".join(lines)


def model_name():
    return os.getenv("OPENAI_PLANNER_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"


def default_model(messages):
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(model=model_name(), messages=messages,
                                              response_format={"type": "json_object"})
    return response.choices[0].message.content


def understand(text, summary, history=(), model=None, sass="normal", spoken_prompt=""):
    """Ask the model for actions. history: earlier [{"role", "content"}] turns. sass: this device's level.
    spoken_prompt: extra instructions when the phrase came from the hands-free mic (it may answer {"ignore": true})."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "system", "content": "The plan:\n" + summary}]
    if SASS_LEVELS.get(sass):
        messages.append({"role": "system", "content": SASS_LEVELS[sass]})
    if spoken_prompt:
        messages.append({"role": "system", "content": spoken_prompt})
    messages.extend({"role": h["role"], "content": str(h["content"])[:2000]} for h in list(history)[-8:]
                    if h.get("role") in ("user", "assistant"))
    messages.append({"role": "user", "content": text[:1000]})
    raw = (model or default_model)(messages)
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"reply": "That one went over my head. Try saying it another way?", "question": "", "actions": []}
    if spoken_prompt and isinstance(data, dict) and data.get("ignore") is True:
        return {"reply": "", "question": "", "actions": [], "ignore": True}
    actions = data.get("actions") if isinstance(data.get("actions"), list) else []
    return {"reply": str(data.get("reply") or "")[:800], "question": str(data.get("question") or "")[:300],
            "actions": [a for a in actions if isinstance(a, dict)][:15], "ignore": False}


# ---------------------------------------------------------------- checking

class Rejected(ValueError):
    """An action that doesn't fit the plan; the message is shown to the cook."""


def _fmt(when):
    return f"{planner.short_date(when)[:3]} {planner.format_clock(when)}"


def _anchor(when, dinner):
    offset = (dinner.date() - when.date()).days
    if offset < 0:
        raise Rejected(f"{_fmt(when)} is after dinner.")
    return planner.anchor_for(offset, f"{when:%H:%M}", dinner)


def _when(action):
    try:
        return datetime.strptime(f"{action.get('date')} {action.get('time')}", "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        raise Rejected("I need a day and a time for that.") from None


def _int(value, low=None, high=None):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = 0
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def check(actions, plan, dishes, tasks, cooks=()):
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
            if kind == "dinner":
                new_dinner = _when(action)
                normalized.append({"type": "dinner", "dinner_at": f"{new_dinner:%Y-%m-%dT%H:%M}"})
                lines.append(f"Dinner: {_fmt(dinner)} → {_fmt(new_dinner)} (steps planned around dinner move with it)")
                continue
            dish = dishes_by_id.get(_int(action.get("dish_id")))
            if not dish:
                raise Rejected("That dish isn't in the plan.")
            step_ids = [str(s) for s in action.get("step_ids") or []]
            if kind == "edit" and action.get("step_id"):
                step_ids = [str(action["step_id"])]
            steps = [tasks_by_key.get((dish["id"], s)) for s in step_ids]
            if any(s is None for s in steps):
                raise Rejected(f"Some of those {dish['name']} steps aren't in the plan.")
            if kind in ("move", "shift"):
                if not steps:
                    raise Rejected(f"Which {dish['name']} steps?")
                if kind == "move":
                    delta = _when(action) - min(s["start"] for s in steps)
                else:
                    delta = timedelta(days=_int(action.get("days")), minutes=_int(action.get("minutes")))
                moves = []
                for step in sorted(steps, key=lambda s: s["start"]):
                    new_start = step["start"] + delta
                    moves.append({"step_id": step["step_id"], "anchor": _anchor(new_start, dinner)})
                    lines.append(f"{dish['name']}: {step['title']}: {_fmt(step['start'])} → {_fmt(new_start)}")
                normalized.append({"type": "move", "dish_id": dish["id"], "moves": moves})
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
            elif kind == "add":
                title = str(action.get("title") or "").strip()[:120]
                if not title:
                    raise Rejected("A new step needs a name.")
                when = _when(action)
                step_kind = action.get("kind") if action.get("kind") in planner.STEP_KINDS else "hands"
                step = {"id": planner.new_step_id(), "anchor": _anchor(when, dinner), "title": title,
                        "minutes": _int(action.get("minutes"), 0, 24 * 60) or 10, "kind": step_kind,
                        "temp": _int(action.get("temp"), 0, 550) if step_kind == "oven" else 0,
                        "detail": str(action.get("detail") or "").strip()[:300], "cook": "", "section": ""}
                normalized.append({"type": "add", "dish_id": dish["id"], "step": step})
                lines.append(f"{dish['name']}: add “{title}” at {_fmt(when)} ({planner.format_duration(step['minutes'])})")
            elif kind == "remove":
                if not steps:
                    raise Rejected(f"Which {dish['name']} steps?")
                normalized.append({"type": "remove", "dish_id": dish["id"], "step_ids": [s["step_id"] for s in steps]})
                for step in steps:
                    lines.append(f"{dish['name']}: remove “{step['title']}”")
            elif kind == "edit":
                if len(steps) != 1:
                    raise Rejected(f"Which {dish['name']} step should change?")
                step = steps[0]
                fields, described = {}, []
                if str(action.get("title") or "").strip():
                    fields["title"] = str(action["title"]).strip()[:120]
                    described.append(f"rename to “{fields['title']}”")
                if action.get("minutes") is not None:
                    fields["minutes"] = _int(action.get("minutes"), 0, 24 * 60)
                    described.append(f"{planner.format_duration(step['minutes'])} → {planner.format_duration(fields['minutes'])}")
                if action.get("detail") is not None:
                    fields["detail"] = str(action.get("detail") or "").strip()[:300]
                    described.append(f"note: “{fields['detail']}”" if fields["detail"] else "clear the note")
                if action.get("kind") in planner.STEP_KINDS:
                    fields["kind"] = action["kind"]
                    described.append(f"type → {planner.KIND_LABELS.get(action['kind'], action['kind'])}")
                if action.get("temp") is not None:
                    fields["temp"] = _int(action.get("temp"), 0, 550)
                    described.append(f"oven → {fields['temp']}°F")
                if not fields:
                    raise Rejected(f"What should change about “{step['title']}”?")
                normalized.append({"type": "edit", "dish_id": dish["id"], "step_id": step["step_id"], "fields": fields})
                lines.append(f"{dish['name']}: {step['title']}: {', '.join(described)}")
        except Rejected as exc:
            problems.append(str(exc))
    return normalized, lines, problems


# ---------------------------------------------------------------- applying (to the store, or to a preview copy)

def _change_steps(steps, action):
    """The dish's steps after one action (move/add/remove/edit). Returns a new list."""
    if action["type"] == "move":
        anchors = {m["step_id"]: m["anchor"] for m in action["moves"]}
        return [dict(s, anchor=anchors.get(s["id"], s["anchor"])) for s in steps]
    if action["type"] == "add":
        return list(steps) + [dict(action["step"])]
    if action["type"] == "remove":
        gone = set(action["step_ids"])
        return [s for s in steps if s["id"] not in gone]
    if action["type"] == "edit":
        return [dict(s, **action["fields"]) if s["id"] == action["step_id"] else s for s in steps]
    return steps


def knock_on(plan, dishes, normalized, ovens=1):
    """Oven clashes the change would create (warnings that aren't in the plan today)."""
    if not normalized:
        return []
    before = {w["text"] for w in planner.find_conflicts(planner.build_timeline(plan, dishes), ovens)}
    trial_plan = dict(plan)
    trial = {d["id"]: copy.deepcopy(d) for d in dishes}
    for action in normalized:
        if action["type"] == "dinner":
            trial_plan["dinner_at"] = action["dinner_at"]
        elif action["dish_id"] in trial:
            trial[action["dish_id"]]["steps"] = _change_steps(trial[action["dish_id"]]["steps"], action)
    after = planner.find_conflicts(planner.build_timeline(trial_plan, list(trial.values())), ovens)
    return [f"{_fmt(w['when'])}: {w['text']}" for w in after if w["text"] not in before][:3]


def apply(store, plan, normalized):
    """Save checked actions. Returns an undo record (previous steps, sizes, checkmarks and dinner time)."""
    undo = {"dishes": {}, "checks": {}, "plan": None}
    for action in normalized:
        if action["type"] == "dinner":
            if undo["plan"] is None:
                undo["plan"] = {"dinner_at": plan["dinner_at"]}
            store.update_plan(plan["id"], action["dinner_at"], plan["cooks"], plan["ovens"])
            continue
        dish = store.get_dish(plan["id"], action["dish_id"])
        if not dish:
            continue
        undo["dishes"].setdefault(str(dish["id"]), {"steps": dish["steps"], "scale": dish.get("scale") or 1})
        if action["type"] in ("move", "add", "remove", "edit"):
            dish["steps"] = _change_steps(dish["steps"], action)
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
    if record.get("plan"):
        store.update_plan(plan["id"], record["plan"]["dinner_at"], plan["cooks"], plan["ovens"])
    for dish_id, before in (record.get("dishes") or {}).items():
        dish = store.get_dish(plan["id"], _int(dish_id))
        if not dish:
            continue
        dish["steps"] = before.get("steps") or dish["steps"]
        store.update_dish(plan["id"], dish["id"], dish)
        store.set_scale(plan["id"], dish["id"], float(before.get("scale") or 1))
    for step_key, was_done in (record.get("checks") or {}).items():
        store.set_checked(plan["id"], "step", str(step_key), bool(was_done))
