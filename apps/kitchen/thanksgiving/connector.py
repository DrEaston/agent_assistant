"""Thanksgiving tools for Claude (core/connector): the plan, dishes, shopping list and plan changes.

Plan changes use the same checks as Tell Dieter (voice.check), so Claude can't do anything the dish editor can't,
and the in-app Undo button undoes Claude's last change too.
"""

from fastapi import HTTPException

from core.connector.tools import STRINGS, Tool, ToolError, ToolSet, site_url, text_list

from . import recipe_changes, voice


def _r():
    from . import router  # imported late: the router imports this module to serve the tools

    return router


def _plan():
    try:
        plan = _r().require_plan()
    except HTTPException:
        raise ToolError("There's no Thanksgiving plan yet. Open askdieter.ai/thanksgiving once to set it up.") from None
    return plan, _r().plan_context(plan)


def _when(moment):
    return f"{moment:%a %b} {moment.day}, {moment:%I:%M %p}".replace(" 0", " ")


# ---------------------------------------------------------------- reading

def get_plan(args):
    plan, ctx = _plan()
    cooks = ", ".join(ctx["cooks"]) or "not set"
    return (f"Thanksgiving plan ({site_url('/thanksgiving')}). Cooks: {cooks}. Ovens: {plan['ovens']}.\n"
            "Dish and step ids below are what thanksgiving_change_plan and thanksgiving_get_dish take.\n\n"
            + voice.plan_summary(plan, ctx["dishes"], ctx["tasks"], ctx["cooks"], ctx["now"]))


def get_dish(args):
    _, ctx = _plan()
    wanted = str(args.get("dish") or "").strip().lower()
    if not wanted:
        raise ToolError("Which dish? Give its id or name.")
    dish = next((d for d in ctx["dishes"] if str(d["id"]) == wanted), None) or next(
        (d for d in ctx["dishes"] if wanted in d["name"].lower()), None)
    if not dish:
        raise ToolError(f"No dish matches {wanted!r}. Dishes: {', '.join(d['name'] for d in ctx['dishes'])}.")
    size = next((o["label"] for o in dish.get("size_options") or [] if float(o["value"]) == float(dish.get("size_value") or 1)), "")
    steps = [{"step_id": t["step_id"], "title": t["title"], "when": _when(t["start"]), "minutes": t["minutes"],
              "kind": t["kind"], "oven_temp": t.get("temp") or None, "done": t["done"],
              "covers": t.get("how_ref") or "", "note": t.get("detail") or ""}  # covers: method lines, e.g. "Crust 6-8"
             for t in sorted(ctx["tasks"], key=lambda t: t["start"]) if t["dish_id"] == dish["id"]]
    return {"id": dish["id"], "name": dish["name"], "cook": dish.get("cook") or "", "batch_size": size,
            "notes": dish.get("notes") or "", "ingredients": dish.get("ingredients_text") or "",
            "instructions": dish.get("instructions_text") or "", "steps": steps,
            # What a recipe change edits (the batch size scales these into the two above):
            "ingredients_as_written": dish.get("base_ingredients_text", dish.get("ingredients_text")) or "",
            "instructions_as_written": dish.get("base_instructions_text", dish.get("instructions_text")) or "",
            "url": site_url(f"/thanksgiving/menu/{dish['id']}")}


STATES = {"next": "on a next-trip list", "skip": "don't need"}


def get_shopping_list(args):
    plan, ctx = _plan()
    ctx.update(_r().follow_linked(plan, ctx))  # items moved to a Kitchen list: bought there, or back here
    aisles = []
    for section in ctx["groceries"]:
        items = []
        for item in section["items"]:
            state = STATES.get(ctx["grocery_aside"].get(item["key"])) or (
                "in the cart" if item["key"] in ctx["grocery_checked"] else "to buy")
            row = {"key": item["key"], "text": item["text"], "amount": item.get("amount") or "",
                   "for": item.get("dishes") or [], "state": state, "typed_in": bool(item.get("extra_id"))}
            if item["key"] in ctx["grocery_links"]:
                row["kitchen_list_url"] = site_url(ctx["grocery_links"][item["key"]]["url"])
            items.append(row)
        aisles.append({"aisle": section["name"], "items": items})
    left = sum(1 for a in aisles for i in a["items"] if i["state"] == "to buy")
    return {"url": site_url("/thanksgiving/shopping"), "to_buy": left, "aisles": aisles}


# ---------------------------------------------------------------- shopping changes

def _keys(args, ctx, name="keys"):
    keys = text_list(args.get(name), name)
    known = {item["key"] for section in ctx["groceries"] for item in section["items"]}
    missing = [k for k in keys if k not in known]
    if missing:
        raise ToolError(f"Not on the Thanksgiving list: {', '.join(missing)}. Use thanksgiving_get_shopping_list "
                        "for the keys.")
    return keys


def add_items(args):
    plan, _ = _plan()
    for line in text_list(args.get("items"), "items"):
        _r().store().add_extra_grocery(plan["id"], line)
    return get_shopping_list({})


def check_items(args):
    plan, ctx = _plan()
    checked = args.get("checked", True) is not False
    for key in _keys(args, ctx):
        _r().set_grocery_checked(plan["id"], key, checked)  # and on the Kitchen list, if it moved there
    return get_shopping_list({})


def finish_trip(args):
    plan, ctx = _plan()
    picks = {}
    for name, pick in (("got", "got"), ("dont_need", "skip"), ("next_trip", "next")):
        if args.get(name):
            picks.update(dict.fromkeys(_keys(args, ctx, name), pick))
    if not picks:
        raise ToolError("Say which keys go in got, dont_need or next_trip.")
    try:
        made = _r().finish_trip_picks(plan, picks)
    except ToolError:
        raise
    except Exception as exc:
        print(f"Warning: couldn't make the next-trip list: {exc}")
        raise ToolError("Couldn't make the next-trip list just now, so nothing was set aside. Try again.") from None
    answer = get_shopping_list({})
    if made:
        answer["next_trip_list"] = {"id": made["id"], "items": made["count"], "url": site_url(made["url"])}
    return answer


def _start(plan, dish_id, label, minutes):
    import time as _time

    minutes = max(1, min(int(minutes), 24 * 60))
    _r().store().add_timer(plan["id"], dish_id, str(label)[:120], int(_time.time() * 1000) + minutes * 60_000)
    return {"label": label, "minutes": minutes}


def start_timer(args):
    plan, _ = _plan()
    try:
        minutes = int(float(args.get("minutes")))
    except (TypeError, ValueError):
        raise ToolError("How many minutes?") from None
    label = " ".join(str(args.get("label") or "Timer").split())[:80] or "Timer"
    return {"started": [_start(plan, None, label, minutes)], "note": "It shows and chimes in cook mode on every screen."}


def series_pattern(series):
    """A run of timers in words, from its minutes: "15, then every 15 min (x3), then every 30 min (x2)"."""
    minutes = [int(m) for m, _ in series]
    gaps = [b - a for a, b in zip(minutes, minutes[1:])]
    parts, i = [f"first at {minutes[0]} min"] if minutes else [], 0
    while i < len(gaps):
        j = i
        while j < len(gaps) and gaps[j] == gaps[i]:
            j += 1
        parts.append(f"then every {gaps[i]} min (x{j - i})")
        i = j
    return ", ".join(parts)


def _ms_now():
    import time as _time
    return int(_time.time() * 1000)


def _timer_view(t, dishes_by_id=None):
    left = max(0, round((t["ends_at_ms"] - _ms_now()) / 60000))
    return {"id": t["id"], "label": t["label"], "minutes_left": left,
            "dish": (dishes_by_id or {}).get(t.get("dish_id"), "")}


def plan_series(series, first_in=None, already=None, skip=(), done_through=None):
    """Which timers to start, and in how many minutes: [(minutes, label)].
    first_in: the first timer goes off in this many minutes and the rest keep their gaps after it.
    already: this many minutes have passed since the step started (timers already past are left out).
    skip: timer numbers (1 = first) to leave out; the others keep their usual times.
    done_through: timer N was just done (e.g. fold 3 just now): the ones after it keep their usual gaps from now."""
    timers = [(int(m), str(label)) for m, label in series]
    if done_through:
        n = max(1, min(int(done_through), len(timers)))
        base = timers[n - 1][0]
        return [(m - base, label) for m, label in timers[n:]]
    if first_in is not None and timers:
        shift = int(first_in) - timers[0][0]
        timers = [(m + shift, label) for m, label in timers]
    if already:
        timers = [(m - int(already), label) for m, label in timers]
    skip = {int(n) for n in skip or ()}
    return [(m, label) for n, (m, label) in enumerate(timers, 1) if n not in skip and m > 0]


def start_step_timers(args):
    plan, ctx = _plan()
    dish = get_dish({"dish": args.get("dish")})
    task = next((t for t in ctx["tasks"] if t["dish_id"] == dish["id"] and t.get("series")
                 and (not args.get("step_id") or t["step_id"] == args.get("step_id"))), None)
    if not task:
        raise ToolError(f"{dish['name']} has no step with timers to start. Use thanksgiving_start_timer instead.")
    try:
        first_in = None if args.get("first_in_minutes") in (None, "") else float(args["first_in_minutes"])
        already = float(args.get("already_minutes") or 0)
        skip = [int(n) for n in (args.get("skip") or [])]
        done_through = int(args["done_through"]) if args.get("done_through") not in (None, "") else None
    except (TypeError, ValueError):
        raise ToolError("first_in_minutes and already_minutes are numbers; skip is a list of timer numbers.") from None
    timers = plan_series(task["series"], first_in, already, skip, done_through)
    if not timers:
        raise ToolError("That leaves no timers to start." + (" That was the last one." if done_through else ""))
    # Starting a step's timers again replaces the set that's running (never two sets at once).
    labels = {label for _, label in task["series"]}
    replaced = 0
    if args.get("replace", True) is not False:
        for t in _r().store().get_timers(plan["id"]):
            if t.get("dish_id") == dish["id"] and t["label"] in labels:
                _r().store().dismiss_timer(plan["id"], t["id"])
                replaced += 1
    started = [_start(plan, dish["id"], label, minutes) for minutes, label in timers]
    return {"step": task["title"], "usual_pattern": series_pattern(task["series"]), "started": started,
            "replaced_running": replaced, "note": "They show and chime in cook mode on every screen."}


def list_timers(args):
    plan, ctx = _plan()
    names = {d["id"]: d["name"] for d in ctx["dishes"]}
    return {"running": [_timer_view(t, names) for t in _r().store().get_timers(plan["id"])]}


def cancel_timers(args):
    """Cancel running timers by id, or every running timer whose label has these words (e.g. "fold")."""
    plan, _ = _plan()
    running = _r().store().get_timers(plan["id"])
    ids = {int(i) for i in (args.get("timer_ids") or []) if str(i).strip().isdigit()}
    words = str(args.get("label_words") or "").lower().split()
    if not ids and not words and not args.get("all"):
        raise ToolError("Which timers? Give timer_ids (from thanksgiving_list_timers), label_words, or all.")
    gone = [t for t in running if args.get("all") or t["id"] in ids
            or (words and all(w in t["label"].lower() for w in words))]
    for t in gone:
        _r().store().dismiss_timer(plan["id"], t["id"])
    return {"cancelled": [t["label"] for t in gone], "still_running": len(running) - len(gone)}


def shift_timers(args):
    """Push running timers later (or earlier, with negative minutes): every one whose label has these words, or all."""
    plan, _ = _plan()
    try:
        minutes = float(args.get("minutes"))
    except (TypeError, ValueError):
        raise ToolError("How many minutes later (negative for earlier)?") from None
    words = str(args.get("label_words") or "").lower().split()
    moved = []
    for t in _r().store().get_timers(plan["id"]):
        if words and not all(w in t["label"].lower() for w in words):
            continue
        new_end = max(_ms_now() + 30_000, t["ends_at_ms"] + int(minutes * 60_000))
        _r().store().move_timer(plan["id"], t["id"], new_end)
        moved.append({"label": t["label"], "minutes_left": max(0, round((new_end - _ms_now()) / 60000))})
    if not moved:
        raise ToolError("No running timers match that.")
    return {"moved": moved}


def change_timer(args):
    plan, _ = _plan()
    try:
        timer_id = int(args.get("timer_id"))
        minutes = float(args.get("minutes_from_now"))
    except (TypeError, ValueError):
        raise ToolError("Give timer_id (from thanksgiving_list_timers) and minutes_from_now.") from None
    timer = next((t for t in _r().store().get_timers(plan["id"]) if t["id"] == timer_id), None)
    if not timer:
        raise ToolError("That timer isn't running. Use thanksgiving_list_timers.")
    _r().store().move_timer(plan["id"], timer_id, _ms_now() + int(max(0.5, min(minutes, 24 * 60)) * 60_000))
    return {"label": timer["label"], "goes_off_in_minutes": minutes}


def propose_recipe_change(args, request=""):
    plan, _ = _plan()
    dish = get_dish({"dish": args.get("dish")})
    changes = {"ingredients_text": args.get("ingredients"), "instructions_text": args.get("instructions"),
               "notes": args.get("notes")}
    card = recipe_changes.propose(_r().store(), _r()._state["db"], plan, dish, changes, args.get("reason") or "",
                                  request)
    return dict(card, review_link=site_url(card["open_url"]),
                next="Nothing is saved yet. The cook reviews the card and taps Save (or plainly says to save it).")


def recipe_history(args):
    plan, _ = _plan()
    dish = get_dish({"dish": args.get("dish")})
    return {"dish": dish["name"], "versions": [
        {"id": v["id"], "when": v["created_at"], "summary": v["summary"], "you_asked": v["request"],
         "before": v["before"], "after": v["after"]} for v in _r().store().get_recipe_versions(plan["id"], dish["id"])]}


def put_back(args):
    plan, ctx = _plan()
    for key in _keys(args, ctx):
        _r().put_back(plan["id"], key)  # also takes it off the Kitchen list it went to
    return get_shopping_list({})


# ---------------------------------------------------------------- plan changes

def change_plan(args):
    plan, ctx = _plan()
    raw = args.get("actions")
    if not isinstance(raw, list) or not raw:
        raise ToolError("actions should be a list of changes (see this tool's description).")
    actions, changes, problems = voice.check([a for a in raw if isinstance(a, dict)], plan, ctx["dishes"],
                                             ctx["tasks"], ctx["cooks"])
    warnings = voice.knock_on(plan, ctx["dishes"], actions, plan["ovens"])
    applied = bool(actions) and not args.get("preview")
    if applied:
        state = _r()._state
        state["voice_pending"].pop(plan["id"], None)
        state["voice_undo"][plan["id"]] = voice.apply(_r().store(), plan, actions)
        print("Claude: applied " + ", ".join(a["type"] for a in actions))
    return {"applied": applied, "changes": changes, "problems": problems, "oven_warnings": warnings,
            "note": "" if applied or not actions else "Preview only. Call again without preview to save it."}


def undo_change(args):
    plan, _ = _plan()
    record = _r()._state["voice_undo"].pop(plan["id"], None)
    if not record:
        raise ToolError("There's no recent change to undo.")
    voice.undo(_r().store(), plan, record)
    return {"undone": True}


CHANGE_HELP = """Change the Thanksgiving plan, checked the same way as the in-app editor. Get ids from
thanksgiving_get_plan first. Each action is one of:
- {"type": "move", "dish_id": 7, "step_ids": ["dough", "shell"], "date": "YYYY-MM-DD", "time": "HH:MM"}  earliest
  step goes there; the others keep their spacing
- {"type": "shift", "dish_id": 7, "step_ids": ["bake"], "days": 0, "minutes": 60}  later (+) or earlier (-)
- {"type": "size", "dish_id": 7, "scale": 0.5}  only a scale listed for that dish
- {"type": "done", "dish_id": 7, "step_ids": ["dough"], "done": true}
- {"type": "add", "dish_id": 7, "title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "minutes": 10,
   "kind": "hands|oven|stove|passive|check|shop", "temp": 0, "detail": ""}
- {"type": "remove", "dish_id": 7, "step_ids": ["chill"]}
- {"type": "edit", "dish_id": 7, "step_id": "cool", "title": "...", "minutes": 90, "detail": "...", "kind": "...", "temp": 0}
- {"type": "dinner", "date": "YYYY-MM-DD", "time": "HH:MM"}  steps planned around dinner move with it
- {"type": "grocery", "text": "heavy whipping cream", "amount": "1 pint"}
Steps must stay on or before dinner. preview: true shows the changes and new oven clashes without saving.
Saved changes can be undone with thanksgiving_undo_last_change (or Undo in the app)."""


def tools():
    keys = {"keys": STRINGS}
    return ToolSet("thanksgiving", [
        Tool("thanksgiving_get_plan", "Thanksgiving plan",
             "Dinner time, every dish with its batch size, every step with its id, time and whether it's done, "
             "what's overdue and next, and the shopping list. Read this before changing anything.", get_plan,
             read_only=True),
        Tool("thanksgiving_get_dish", "Thanksgiving dish",
             "One dish's recipe (ingredients and instructions at its chosen size), notes, who's making it and its "
             "steps.", get_dish, {"dish": {"type": "string", "description": "Dish id or part of its name."}},
             ("dish",), read_only=True),
        Tool("thanksgiving_get_shopping_list", "Thanksgiving shopping list",
             "The Thanksgiving list by aisle, added up across dishes. Each item has a key and a state: to buy, in "
             "the cart, on a next-trip list, or don't need. Next-trip items stay linked: kitchen_list_url says "
             "where, and checking one off there or here does both.", get_shopping_list, read_only=True),
        Tool("thanksgiving_add_to_shopping_list", "Add to the Thanksgiving list",
             "Add typed-in items, e.g. [\"1 pint heavy whipping cream\"].", add_items, {"items": STRINGS},
             ("items",)),
        Tool("thanksgiving_check_items", "Check off Thanksgiving items",
             "Put items in the cart (checked true, the default) or take them out (false).", check_items,
             dict(keys, checked={"type": "boolean"}), ("keys",)),
        Tool("thanksgiving_finish_trip", "Finish the Thanksgiving shopping trip",
             "Sort out what's left after a trip: got (into the cart), dont_need (set aside), next_trip (moved to a "
             "new Kitchen shopping list, whose link is returned; they stay linked to this list).", finish_trip,
             {"got": STRINGS, "dont_need": STRINGS, "next_trip": STRINGS}),
        Tool("thanksgiving_put_back", "Put items back on the Thanksgiving list",
             "Undo next-trip or don't-need for these items (next-trip items come off the Kitchen list).", put_back, keys, ("keys",)),
        Tool("thanksgiving_start_timer", "Start a timer",
             "Start one kitchen timer, e.g. {\"label\": \"potatoes\", \"minutes\": 20}. It shows and chimes in cook "
             "mode on every screen.", start_timer,
             {"label": {"type": "string"}, "minutes": {"type": "number"}}, ("minutes",)),
        Tool("thanksgiving_start_step_timers", "Start a step's timers",
             "Start the run of timers a step comes with (e.g. the sourdough's six stretch and folds: 15, then every "
             "15 min x3, then every 30 min x2). Give the dish, and step_id if the dish has more than one such step. "
             "Starting them again REPLACES the running set (never two sets). Adjust without changing the pattern: "
             "first_in_minutes = the first one goes off in that many minutes and the rest keep their gaps after it "
             "(\"first fold in 10 minutes\"); already_minutes = that many minutes have passed since the step started "
             "(\"I'm 5 minutes in\"); done_through = N when the cook just did timer N, early or late (\"I just did fold "
             "3\" = 3): the rest keep their usual gaps from now; skip = timer numbers to leave out, the rest keep "
             "their usual times (\"leave out the first timer\" = [1]). The answer lists each timer's minutes: say "
             "them back.", start_step_timers,
             {"dish": {"type": "string"}, "step_id": {"type": "string"},
              "first_in_minutes": {"type": "number"}, "already_minutes": {"type": "number"},
              "skip": {"type": "array", "items": {"type": "integer"}}, "done_through": {"type": "integer"},
              "replace": {"type": "boolean", "description": "false only to run a second set alongside (rare)."}},
             ("dish",)),
        Tool("thanksgiving_list_timers", "Running timers",
             "Every running kitchen timer: id, label, minutes left, dish.", list_timers, read_only=True),
        Tool("thanksgiving_cancel_timers", "Cancel timers",
             "Stop running timers: timer_ids (from thanksgiving_list_timers), or every one whose label has "
             "label_words (e.g. \"fold\"), or all.", cancel_timers,
             {"timer_ids": {"type": "array", "items": {"type": "integer"}}, "label_words": {"type": "string"},
              "all": {"type": "boolean"}}, destructive=True),
        Tool("thanksgiving_shift_timers", "Push timers later",
             "Move running timers later by minutes (negative = earlier): those whose label has label_words (e.g. "
             "\"fold\"), or all of them. \"Push the folds back 5 minutes\".", shift_timers,
             {"minutes": {"type": "number"}, "label_words": {"type": "string"}}, ("minutes",)),
        Tool("thanksgiving_change_timer", "Change a timer",
             "Make a running timer go off in minutes_from_now instead.", change_timer,
             {"timer_id": {"type": "integer"}, "minutes_from_now": {"type": "number"}}, ("timer_id", "minutes_from_now")),
        Tool("thanksgiving_propose_recipe_change", "Propose a recipe change",
             "Suggest a change to one dish's recipe. Nothing is saved: the cook reviews a card (old and new lines, "
             "and what it does to the shopping list) and taps Save. Read the dish first and send the FULL new text of "
             "each part you change, edited from ingredients_as_written / instructions_as_written (the recipe as "
             "written, before batch scaling). Leave parts you don't change out. Give one short reason.",
             propose_recipe_change,
             {"dish": {"type": "string"}, "ingredients": {"type": "string"}, "instructions": {"type": "string"},
              "notes": {"type": "string"}, "reason": {"type": "string"}}, ("dish", "reason")),
        Tool("thanksgiving_recipe_history", "Recipe history",
             "Saved changes to a dish's recipe, newest first: what was asked, and the text before and after.",
             recipe_history, {"dish": {"type": "string"}}, ("dish",), read_only=True),
        Tool("thanksgiving_change_plan", "Change the Thanksgiving plan", CHANGE_HELP, change_plan,
             {"actions": {"type": "array", "items": {"type": "object"}}, "preview": {"type": "boolean"}},
             ("actions",)),
        Tool("thanksgiving_undo_last_change", "Undo the last plan change",
             "Put back what the last saved thanksgiving_change_plan (or Tell Dieter) changed.", undo_change),
    ])

