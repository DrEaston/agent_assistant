"""What each timed step tells the cook: the recipe lines it covers ("how") and a short note ("detail").

A recipe is split into steps at different times, and in cook mode a step pops up on its own. So each step names the
recipe lines it covers, by how they start (planner.step_how finds the full line in the recipe as it is now), and a
note for anything the recipe lines don't say: a double batch, turning the oven up for the next step.

GUIDE[dish name][step id] = {"how": [line starts], "detail": note, "old_detail": the note it replaces,
                              "anchor": (old, new), "minutes": (old, new)}.
apply() fills these into a dish's steps: "how" only where a step has none, "detail" only where the step still has
the old note (or none), a new time or length only where the step still has the old one. So anything edited by hand
stays. New preset dishes get it (presets.py), and saved plans get
it once per GUIDE_VERSION (store.py).
"""

GUIDE_VERSION = "step-how-4"  # bump when GUIDE changes, so saved plans get it

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

    "Roast turkey on vegetables": {
        "veg": {"old_detail": "Onions in wedges, carrots and celery in 2-inch pieces. Refrigerate.",
                "detail": "Onions in wedges, carrots and celery in 2-inch pieces. Refrigerate. Save the onion ends and "
                          "celery leaves in a bag for the gravy stock."},
        "out": {"how": ["Remove the neck"], "detail": "Save the neck, heart and gizzard for the gravy stock (not the liver)."},
        "butter": {"how": ["Pre-brined", "Cut onions in wedges", "Mix the butter"]},
        "roast": {"how": ["Set the turkey on the vegetables", "Add 1/2 cup broth"]},
        "tent": {"how": ["Add 1/2 cup broth"]},
        "temp": {"how": ["Start checking at"]},
        "rest": {"how": ["Rest, loosely tented"]},
        "carve": {"detail": "Legs and thighs off first, then slice the breast across the grain."},
    },
    "Whipped mashed potatoes": {
        "peel": {"how": ["Peel and cut"]},
        # A big pot takes about 15 minutes to boil, then 15 to 20 to cook: 20 minutes drained them too early.
        "boil": {"how": ["Bring to a boil"], "minutes": (20, 35),
                  "detail": "About 15 minutes to come to a boil, then 15 to 20 to cook."},
        "whip": {"how": ["Drain, then return", "Melt the butter", "Mash or rice", "Season with salt"],
                  "anchor": ("T-110", "T-95")},
        "hold": {"how": ["Hold in a buttered"], "anchor": ("T-95", "T-80"), "minutes": (95, 80)},
    },
    "Gravy": {
        "stock": {"how": ["Put the neck", "Bring to a boil", "Strain and keep warm"]},
        "make": {"how": ["Strain the pan juices", "Melt 4 tbsp", "Whisk in 3 to 4", "Taste before"]},
    },
    "Stuffing": {
        "prep": {"how": ["Chop the onion", "Toss with the cubes"],
                  "old_detail": "Onion and celery in butter, broth, toss with cubes, into a buttered 9x13.", "detail": ""},
        "out": {"detail": "It sits out about 30 minutes before baking."},
        "bake": {"how": ["Bake covered at 325"],
                  "detail": "Second rack, next to the turkey (and the pigs in a blanket for the first 20 minutes)."},
        "crisp": {"how": ["When the turkey comes out"], "detail": "The oven is still climbing from 325°F after the turkey: give it a few extra minutes."},
    },
    "Sourdough": {
        "mix": {"how": ["Mix flour, starter"]},
        "folds": {"how": ["Stretch and folds"]},
        "shape": {"how": ["Cold shape"]},
        "preheat": {"how": ["Next morning"]},
        "poke": {"how": ["Poke test"]},
        "bake1": {"how": ["Flip straight", "Bake 25 minutes"]},
        "bake2": {"how": ["Lid off"]},
        "cool": {"how": ["Cool at least"]},
        "recrisp": {"detail": "400°F, about 10 minutes, straight on the rack."},
    },
    "Olive and rosemary sourdough": {
        "mix": {"how": ["Pat the chopped olives", "Mix flour, starter"]},
        "folds": {"how": ["Stretch and folds"]},
        "shape": {"how": ["Cold shape"]},
        "preheat": {"how": ["Next morning"]},
        "poke": {"how": ["Poke test"]},
        "bake1": {"how": ["Flip straight", "Bake 25 minutes"]},
        "bake2": {"how": ["Lid off"]},
        "cool": {"how": ["Cool at least"]},
        "recrisp": {"detail": "400°F, about 10 minutes, straight on the rack."},
    },
    "Mac and cheese": {
        # The practice run makes it start to finish, baked fresh (that's the only step "Made fresh" is for).
        "practice": {"how": ["Cut the smokies", "Boil the pasta", "Shred the cheese", "Warm the milk", "Roux",
                             "Pour in the warm milk", "Take the pot off", "Stir in the cheddar", "Crumble in the goat",
                             "Fold in the pasta", "Spread in a buttered", "Mix the panko", "Rack", "Made fresh",
                             "Top not golden", "Rest 10 minutes"]},
        "make": {"how": ["Cut the smokies", "Boil the pasta", "Shred the cheese", "Warm the milk", "Roux",
                          "Pour in the warm milk", "Take the pot off", "Stir in the cheddar", "Crumble in the goat",
                          "Fold in the pasta", "Spread in a buttered", "To make ahead"],
                  "old_detail": "Smokies, pasta, cheese sauce, assemble. Keep the topping separate.", "detail": ""},
        "out": {"how": ["From the fridge"]},
        "bake": {"how": ["Mix the panko", "Rack", "From the fridge"], "detail": "The oven is still climbing from 325°F after the turkey: give it a few extra minutes."},
        "uncover": {"how": ["From the fridge", "Top not golden"]},
        "rest": {"how": ["Rest 10 minutes"]},
    },
    "Cranberry sauce": {
        # Moved off 6 PM Tuesday: that's when both sourdoughs are mixed and the folds start.
        "make": {"how": ["Rinse the cranberries", "Bring the sugar", "Add the cranberries", "Zest the orange"],
                  "anchor": ("D-2 18:00", "D-2 16:00")},
        "fridge": {"how": ["Cool, then refrigerate"], "anchor": ("D-2 19:00", "D-2 17:00")},
    },
    "Pigs in a blanket": {
        "wrap": {"how": ["Keep the pastry cold", "Pat the sausages", "Roll each sausage"]},
        "bake": {"how": ["Beat the egg", "Bake at 325"]},
        "serve": {"how": ["Cool 5 minutes"]},
    },
    "Scalloped potatoes": {
        "warm": {"old_detail": "Shares the oven with the stuffing and mac and cheese.",
                  "detail": "Shares the oven with the stuffing and mac and cheese. The oven is still climbing from 325°F after the turkey: give it a few extra minutes."},
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
        for field in ("anchor", "minutes"):
            if field in entry and str(step.get(field)) == str(entry[field][0]):
                step[field] = entry[field][1]
                changed = True
        if "detail" in entry:
            current = (step.get("detail") or "").strip()
            if current != entry["detail"] and current in ("", entry.get("old_detail", "")):
                step["detail"] = entry["detail"]
                changed = True
    return changed
