"""What each timed step tells the cook: the recipe lines it covers ("how") and a short note ("detail").

A recipe is split into steps at different times, and in cook mode a step pops up on its own. So each step names the
recipe lines it covers, by how they start (planner.step_how finds the full line in the recipe as it is now), and a
note for anything the recipe lines don't say: a double batch, turning the oven up for the next step.

GUIDE[dish name][step id] = {"how": [line starts], "detail": note, "old_detail": the note it replaces}.
apply() fills these into a dish's steps: "how" only where a step has none, "detail" only where the step still has
the old note (or none), so anything edited by hand stays. New preset dishes get it (presets.py), and saved plans get
it once per GUIDE_VERSION (store.py).
"""

GUIDE_VERSION = "step-how-1"

GUIDE = {
    "Pumpkin pie": {
        "dough": {"how": ["Whisk the flour", "Toss the butter", "Drizzle in", "For extra layers", "Wrap and refrigerate"],
                  "detail": "Double batch: make two disks, one per pie.",
                  "old_detail": "Keep everything cold. Chill at least 1 hour."},
        "shell": {"how": ["Roll on a floured counter", "Ease it into", "Wrap the shell"],
                  "detail": "Chilled more than an hour? Let it sit 5 to 10 minutes first. Double batch: roll each disk "
                            "into its own shell.",
                  "old_detail": "Freeze it in the plate, wrapped in plastic and foil."},
        "blind1": {"how": ["Line the frozen shell"], "detail": "Double batch: both shells go in together."},
        "filling": {"how": ["Whisk the pumpkin"], "detail": "Set it aside until the shells are out."},
        "blind2": {"how": ["Lift out the weights"], "detail": "Then turn the oven up to 425°F for the filling."},
        "bake1": {"how": ["Bake at 425"], "detail": "Pour the filling into the warm shells first."},
        "bake2": {"how": ["Lower to 350"]},
        "cool": {"how": ["Cool on a rack"]},
        "chill": {"how": ["Cool on a rack"]},
        "cream": {"how": ["Chill the bowl", "Cover and refrigerate"],
                  "old_detail": "Soft peaks. Whisk a few strokes by hand before serving.", "detail": ""},
    },
}


def apply(dish_name, steps):
    """Fill in GUIDE for this dish's steps (in place). Returns True if anything changed."""
    guide = GUIDE.get(dish_name) or {}
    changed = False
    for step in steps or []:
        entry = guide.get(step.get("id"))
        if not entry:
            continue
        if entry.get("how") and not step.get("how"):
            step["how"] = list(entry["how"])
            changed = True
        if "detail" in entry:
            current = (step.get("detail") or "").strip()
            if current != entry["detail"] and current in ("", entry.get("old_detail", "")):
                step["detail"] = entry["detail"]
                changed = True
    return changed
