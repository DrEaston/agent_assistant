"""Starter dishes for the Thanksgiving planner.

Every dish is a list of timed steps. Step anchors:
  "T-195"     -> 195 minutes before dinner (moves when dinner time changes)
  "D-1 10:00" -> 10:00 AM one day before dinner day (fixed clock time)
Step kinds: hands (active work), oven, stove, passive (wait/hold/cool), check, shop.
"""

from .planner import generic_steps, turkey_steps

CATEGORIES = (
    ("turkey", "Turkey"),
    ("side", "Side"),
    ("bread", "Bread"),
    ("dessert", "Dessert"),
    ("prep", "House prep"),
    ("other", "Other"),
)


def _s(step_id, anchor, title, minutes=5, kind="hands", temp=0, detail="", section=""):
    """A step. `section` names the recipe section (heading) this step needs, e.g. "Crust"."""
    return {"id": step_id, "anchor": anchor, "title": title, "minutes": minutes, "kind": kind,
            "temp": temp, "detail": detail, "cook": "", "section": section}


TURKEY_INGREDIENTS = """1 pre-brined turkey (13 lb), thawed
2 large onions
4 carrots
4 celery ribs
1 head garlic
fresh thyme and rosemary
1 tbsp oil
1 1/2 cups low-sodium chicken or turkey broth
4 tbsp unsalted butter
1 tbsp chopped fresh sage, thyme, and rosemary
1/2 onion and 1/2 lemon for the cavity
black pepper"""

TURKEY_INSTRUCTIONS = """Pre-brined: do not add salt. Use unsalted butter and low-sodium broth.
Remove the neck (body cavity) and giblet bag (neck end). Pat very dry. Leave an oven-safe leg clamp on.
Cut onions in wedges, carrots and celery in 2-inch pieces, halve the garlic head. Toss with oil and pepper in the roasting pan with the herb sprigs. Pour in the broth.
Mix the butter with chopped herbs and pepper. Rub all over the turkey and under the breast skin. Put the onion, lemon, and herb sprigs in the cavity. Tuck the wing tips.
Set the turkey on the vegetables, breast up. Roast at 325°F on a lower rack, about 3 to 3 1/4 hours for 13 lb.
Add 1/2 cup broth if the pan gets dry. Tent the breast with foil once it is golden.
Start checking at 2 1/2 hours: 165°F in the thickest part of the breast and thigh, not touching bone.
Rest, loosely tented, 30 to 40 minutes. The vegetables are soft and rich: use them for gravy or serve them."""

GRAVY_INGREDIENTS = """turkey pan drippings
4 tbsp butter (or skimmed turkey fat)
1/4 cup flour
low-sodium broth to make 3 to 4 cups liquid with the drippings"""

GRAVY_INSTRUCTIONS = """Strain the pan juices and skim the fat.
Melt 4 tbsp butter or fat, whisk in 1/4 cup flour, and cook 1 minute.
Whisk in 3 to 4 cups drippings topped up with broth. Simmer 5 to 10 minutes.
Taste before adding any salt (the turkey is pre-brined)."""

POTATO_INGREDIENTS = """4 lb Yukon Gold potatoes
1 1/2 sticks butter
1 to 1 1/4 cups half-and-half or heavy cream
2 oz cream cheese (optional)
kosher salt
white or black pepper"""

POTATO_INSTRUCTIONS = """Peel and cut potatoes into 2-inch chunks. Cover with cold, well-salted water.
Bring to a boil and cook 15 to 20 minutes, until a fork slides in easily.
Drain, then return to the hot pot 1 to 2 minutes to steam dry.
Melt the butter into the warm cream.
Mash or rice. Add the butter-cream and cream cheese. Beat with a hand mixer on low-medium 30 to 60 seconds only.
Season with salt and pepper.
Hold in a buttered slow cooker on Warm (up to 4 hours). Stir now and then; add a splash of cream and a pat of butter before serving."""

STUFFING_INGREDIENTS = """1 bag (12 to 14 oz) herb-seasoned stuffing cubes
1 stick butter
1 onion
3 celery ribs
2 to 2 1/2 cups low-sodium broth
1 egg (optional)"""

STUFFING_INSTRUCTIONS = """Chop the onion and celery. Cook in the butter until soft, about 8 minutes. Add the broth and warm through.
Toss with the cubes (and egg) and spread in a buttered 9x13 dish. Cover and refrigerate overnight.
Bake covered at 325°F on the second rack for the turkey's last 45 minutes.
When the turkey comes out, uncover, raise the oven to 400°F, and bake 10 to 15 minutes until the top is crisp."""

SOURDOUGH_INGREDIENTS = """440 g flour
90 g active starter
350 g water
9 g salt
rice flour for dusting"""

SOURDOUGH_INSTRUCTIONS = """Mix flour, starter, water, and salt in the mixer.
Stretch and fold every 15 minutes for 1 hour (4 sets).
Two more stretch and folds over the next hour.
Cold shape: turn onto the counter, fold into a round with the bench scraper, drag toward you to tighten the surface. Place seam-side up in a towel-lined bowl dusted with rice flour. Cover and refrigerate overnight.
Next morning: preheat the Dutch oven at 500°F for 45 to 60 minutes.
Poke test: a floured fingertip dent should spring back slowly. If it springs back right away, give it 30 to 45 minutes on the counter.
Flip straight from the fridge onto parchment, dust, score, and lower in with the parchment sling.
Bake 25 minutes at 500°F with the lid on.
Lid off, 450°F, 15 minutes. Go 3 to 5 minutes longer for a darker crust (205 to 210°F inside).
Cool at least 1 hour before slicing."""

# Lines ending in ":" are section headings (shown as headings, skipped on the shopping list).
PIE_INGREDIENTS = """Crust:
1 1/4 cups (160 g) all-purpose flour
1 tsp sugar
1/2 tsp salt
1 stick (113 g) very cold unsalted butter
4 to 6 tbsp ice water
Filling:
15 oz can pumpkin puree (not pie filling)
3/4 cup sugar
2 tsp pumpkin pie spice
1/2 tsp salt
2 large eggs
12 oz can evaporated milk
To serve:
whipped cream"""

PIE_INSTRUCTIONS = """Crust:
Whisk the flour, sugar, and salt in a bowl. Cut the cold butter into 1/2-inch cubes.
Toss the butter in the flour, then press each cube flat between your fingertips. Stop when you have pea-size bits and some flat, coin-size pieces. Those flat pieces are what make it flaky.
Drizzle in 4 tbsp ice water and toss with a fork. Add more, 1 tbsp at a time, just until a handful holds together when squeezed. It should look shaggy, not smooth.
For extra layers: press the dough into a rough rectangle, fold it in thirds like a letter, then pat it into a disk.
Wrap and refrigerate at least 1 hour (or 30 minutes in the freezer).
Roll on a floured counter into a 12-inch circle, turning it often so it doesn't stick. If it gets soft or sticky, chill it 10 minutes.
Ease it into a 9-inch pie plate without stretching. Trim to a 1/2-inch overhang, fold the edge under, and crimp.
Wrap the shell tightly in plastic, then foil, and freeze it in the plate. Bake it from frozen.
Blind bake:
Line the frozen shell with parchment and fill it with pie weights or dried beans. Bake at 400°F for 20 minutes.
Lift out the weights and bake 5 to 10 minutes more, until the bottom looks dry.
Filling and bake:
Whisk the pumpkin, sugar, spice, salt, eggs, and evaporated milk until smooth. Pour into the warm shell.
Bake at 425°F for 15 minutes.
Lower to 350°F and bake 40 to 50 minutes, until the edges are set and the center barely jiggles. Cover the crust edge with foil if it browns too fast.
Cool on a rack about 2 hours, then refrigerate loosely covered. Serve with whipped cream."""

OLD_MAC_INGREDIENTS = """Mac and cheese:
1 lb elbow macaroni or cavatappi
1 package (12 to 14 oz) Little Smokies
Sauce:
5 tbsp unsalted butter
1/3 cup all-purpose flour
4 cups whole milk
1 tsp dry mustard
1/2 tsp smoked paprika
1/2 tsp garlic powder
a few dashes hot sauce
kosher salt and black pepper
12 oz sharp cheddar cheese (block, not pre-shredded)
4 oz Gruyère cheese (or Monterey Jack)
Topping:
1 cup panko breadcrumbs
2 tbsp unsalted butter, melted"""

OLD_MAC_INSTRUCTIONS = """Smokies:
Cut the smokies into coins. Brown them in a dry skillet over medium-high, 4 to 5 minutes. Set aside.
Pasta:
Boil the pasta in well-salted water 2 minutes less than the package says. Drain; don't rinse.
Sauce:
Shred the cheese yourself (bagged cheese has starch that makes the sauce grainy). Save 1/2 cup cheddar for the top.
Warm the milk in the microwave, 2 to 3 minutes. Warm milk is what keeps the sauce from going lumpy.
Roux: melt 5 tbsp butter in a big pot over medium. Whisk in the flour and cook 2 minutes, whisking, until it smells toasty but is still pale.
Pour in the warm milk a splash at a time, whisking the whole time. Simmer 5 to 8 minutes, whisking often, until it coats a spoon.
Take the pot off the heat. Whisk in the mustard, paprika, garlic powder, hot sauce, 1 tsp salt, and pepper.
Stir in the cheese a handful at a time until smooth. Off the heat keeps it silky.
Assemble:
Fold in the pasta and smokies. Taste for salt. The sauce should look too loose: the pasta soaks it up.
Spread in a buttered 9x13 dish.
To make ahead: cool, cover, and refrigerate up to 2 days. Keep the topping separate.
Bake:
Mix the panko with the melted butter and the saved cheddar. Scatter over the top.
Made fresh: bake at 400°F for 20 to 25 minutes, until bubbling at the edges and golden on top.
From the fridge: sit out 45 minutes, bake covered at 400°F for 20 minutes, then uncovered 15 minutes.
Rest 10 minutes before serving."""

# Current version: 2 oz each of the cheddar and Gruyère swapped for goat cheese (Nov 19 practice run).
MAC_INGREDIENTS = OLD_MAC_INGREDIENTS.replace(
    "12 oz sharp cheddar cheese (block, not pre-shredded)\n4 oz Gruyère cheese (or Monterey Jack)",
    "10 oz sharp cheddar cheese (block, not pre-shredded)\n2 oz Gruyère cheese (or Monterey Jack)\n"
    "4 oz goat cheese, crumbled")
MAC_INSTRUCTIONS = OLD_MAC_INSTRUCTIONS.replace(
    "Stir in the cheese a handful at a time until smooth. Off the heat keeps it silky.",
    "Stir in the cheddar and Gruyère a handful at a time until smooth. Off the heat keeps it silky.\n"
    "Crumble in the goat cheese last and stir just until it disappears. It makes the sauce extra creamy "
    "(save cheddar, not goat cheese, for the top: goat cheese doesn't brown).")

# Text of earlier versions, so saved dishes that were never edited can be upgraded in place.
OLD_PIE_INSTRUCTIONS = """Shell: roll, fit, and crimp. Wrap tightly and freeze in the plate. Bake from frozen.
Blind bake: line the frozen shell with parchment and pie weights. Bake at 400°F for 20 minutes.
Remove the weights and bake 5 to 10 minutes more, until the bottom looks dry.
Whisk the pumpkin, sugar, spice, salt, eggs, and evaporated milk until smooth. Pour into the warm shell.
Bake at 425°F for 15 minutes.
Reduce to 350°F and bake 40 to 50 minutes, until the edges are set and the center barely jiggles. Shield the crust edge with foil if it browns fast.
Cool on a rack about 2 hours, then refrigerate loosely covered. Serve with whipped cream."""


def _sourdough_steps():
    steps = [
        _s("feed1", "D-3 21:00", "Starter out of the fridge: discard and feed", 5,
           detail="Leave it on the counter overnight."),
        _s("feed2", "D-2 09:00", "Feed the starter again", 5, detail="It should peak around 6 PM."),
        _s("mix", "D-2 18:00", "Mix the sourdough in the mixer", 15),
    ]
    for i, time in enumerate(("18:15", "18:30", "18:45", "19:00"), start=1):
        steps.append(_s(f"fold{i}", f"D-2 {time}", f"Stretch and fold ({i} of 6)", 3))
    steps += [
        _s("fold5", "D-2 19:30", "Stretch and fold (5 of 6)", 3),
        _s("fold6", "D-2 20:00", "Stretch and fold (6 of 6)", 3),
        _s("shape", "D-2 20:05", "Cold shape and refrigerate overnight", 10,
           detail="Seam-side up in a rice-floured, towel-lined bowl. Cover."),
        _s("preheat", "D-1 09:00", "Dutch oven in, preheat to 500°F", 55, "oven", 500),
        _s("poke", "D-1 09:55", "Poke test the dough", 5, "check",
           detail="Slow spring-back = ready. Fast spring-back = 30 to 45 min on the counter."),
        _s("bake1", "D-1 10:00", "Score and bake, lid ON", 25, "oven", 500),
        _s("bake2", "D-1 10:25", "Lid OFF, oven to 450°F", 15, "oven", 450),
        _s("cool", "D-1 10:40", "Cool the loaf", 60, "passive", detail="At least 1 hour before slicing."),
        _s("recrisp", "T-25", "Re-crisp the loaf", 10, "oven", 400),
    ]
    return steps


def _pie_steps():
    return [
        _s("dough", "D-7 19:00", "Make the butter crust dough and chill it", 20,
           detail="Keep everything cold. Chill at least 1 hour.", section="Crust"),
        _s("shell", "D-7 20:15", "Roll, crimp, and freeze the pie shell", 20,
           detail="Freeze it in the plate, wrapped in plastic and foil.", section="Crust"),
        _s("blind1", "D-1 11:00", "Blind bake the frozen shell with weights", 20, "oven", 400, section="Blind bake"),
        _s("filling", "D-1 11:05", "Whisk the pumpkin filling", 10, section="Filling"),
        _s("blind2", "D-1 11:20", "Weights out, bake until the bottom is dry", 8, "oven", 400, section="Blind bake"),
        _s("bake1", "D-1 11:30", "Pour filling in, bake at 425°F", 15, "oven", 425, section="Filling"),
        _s("bake2", "D-1 11:45", "Lower to 350°F and bake until barely jiggly", 45, "oven", 350, section="Filling"),
        _s("cool", "D-1 12:30", "Cool the pie on a rack", 120, "passive"),
        _s("chill", "D-1 14:30", "Refrigerate the pie", 2),
    ]


def upgrade_saved_dish(dish):
    """Return updated fields for a saved preset dish whose recipe was never edited, else None."""
    if dish.get("name") == "Pumpkin pie" and (dish.get("instructions_text") or "").strip() == OLD_PIE_INSTRUCTIONS:
        old = {s.get("id"): s for s in dish.get("steps") or []}
        steps = []
        for step in _pie_steps():
            if step["id"] in old and step["id"] not in ("dough", "shell"):
                # Keep the user's timing and assignments on steps that already existed.
                kept = dict(old[step["id"]])
                kept["section"] = step["section"]
                steps.append(kept)
            else:
                step["cook"] = old.get("shell", {}).get("cook", "")
                steps.append(step)
        return {"ingredients_text": PIE_INGREDIENTS, "instructions_text": PIE_INSTRUCTIONS, "steps": steps}
    if dish.get("name") == "Mac and cheese" and (dish.get("ingredients_text") or "").strip() == OLD_MAC_INGREDIENTS:
        # The goat cheese swap. Instructions update only if they're also untouched; steps and cooks stay.
        instructions = (dish.get("instructions_text") or "")
        if instructions.strip() == OLD_MAC_INSTRUCTIONS:
            instructions = MAC_INSTRUCTIONS
        return {"ingredients_text": MAC_INGREDIENTS, "instructions_text": instructions,
                "steps": dish.get("steps") or []}
    return None


def _potato_steps():
    return [
        _s("peel", "T-150", "Peel and cut the potatoes", 20),
        _s("boil", "T-130", "Boil the potatoes", 20, "stove"),
        _s("whip", "T-110", "Drain, dry, and whip the potatoes", 15),
        _s("hold", "T-95", "Hold potatoes warm in the slow cooker", 95, "passive",
           detail="Stir now and then. Splash of cream and a pat of butter before serving."),
    ]


def _stuffing_steps():
    return [
        _s("prep", "D-1 16:00", "Assemble the stuffing and refrigerate", 30,
           detail="Onion and celery in butter, broth, toss with cubes, into a buttered 9x13."),
        _s("out", "T-120", "Stuffing out of the fridge", 2),
        _s("bake", "T-90", "Bake stuffing covered, second rack", 45, "oven", 325),
        _s("crisp", "T-45", "Uncover stuffing, oven to 400°F to crisp", 15, "oven", 400),
    ]


def _mac_steps():
    return [
        _s("practice", "D-7 17:30", "Practice run: mac and cheese, baked fresh", 75,
           detail="Taste it and note any changes for Thursday."),
        _s("make", "D-1 17:00", "Make the mac and cheese and refrigerate", 60,
           detail="Smokies, pasta, cheese sauce, assemble. Keep the topping separate."),
        _s("out", "T-90", "Mac and cheese out of the fridge", 2),
        _s("bake", "T-45", "Top the mac and cheese, bake covered at 400°F", 20, "oven", 400, section="Bake"),
        _s("uncover", "T-25", "Uncover the mac and cheese to brown", 15, "oven", 400, section="Bake"),
        _s("rest", "T-10", "Rest the mac and cheese", 10, "passive"),
    ]


def _gravy_steps():
    return [_s("make", "T-35", "Make the gravy from the drippings", 15, "stove")]


def _house_steps():
    return [
        _s("fridge", "D-5 07:45", "Clear fridge space for the turkey", 15),
        _s("shop", "D-4 11:00", "Big grocery shop", 90, "shop", detail="Use the Shopping list."),
        _s("table", "D-1 19:00", "Set the table; pull out serving dishes and the slow cooker", 30),
    ]


def _dish(name, category, ingredients="", instructions="", steps=None, notes=""):
    return {"name": name, "category": category, "cook": "", "ingredients_text": ingredients,
            "instructions_text": instructions, "notes": notes, "steps": steps or []}


def preset_dish(key, dinner=None):
    """Return a new dish dict for a preset key, or None."""
    if key == "turkey":
        return _dish("Roast turkey on vegetables", "turkey", TURKEY_INGREDIENTS, TURKEY_INSTRUCTIONS,
                     turkey_steps(13, frozen=True), notes="13 lb pre-brined Butterball.")
    if key == "gravy":
        return _dish("Gravy", "side", GRAVY_INGREDIENTS, GRAVY_INSTRUCTIONS, _gravy_steps())
    if key == "potatoes":
        return _dish("Whipped mashed potatoes", "side", POTATO_INGREDIENTS, POTATO_INSTRUCTIONS, _potato_steps())
    if key == "stuffing":
        return _dish("Stuffing", "side", STUFFING_INGREDIENTS, STUFFING_INSTRUCTIONS, _stuffing_steps())
    if key == "sourdough":
        return _dish("Sourdough", "bread", SOURDOUGH_INGREDIENTS, SOURDOUGH_INSTRUCTIONS, _sourdough_steps(),
                     notes="Starter lives in the fridge, so it gets two feedings.")
    if key == "mac":
        return _dish("Mac and cheese", "side", MAC_INGREDIENTS, MAC_INSTRUCTIONS, _mac_steps(),
                     notes="With Little Smokies.")
    if key == "pie":
        return _dish("Pumpkin pie", "dessert", PIE_INGREDIENTS, PIE_INSTRUCTIONS, _pie_steps())
    if key == "house":
        return _dish("House prep", "prep", steps=_house_steps())
    simple = SIMPLE_SIDES.get(key)
    if simple:
        name, ingredients, prep, method, cook, temp = simple
        return _dish(name, "side", ingredients, "", generic_steps(prep, cook, method, temp))
    return None


# key: (name, ingredients, prep_min, method, cook_min, oven_temp)
SIMPLE_SIDES = {
    "green_beans": ("Green bean casserole", "2 lb green beans\n1 can cream of mushroom soup\n3/4 cup milk\n"
                    "1 tsp soy sauce\n1 1/3 cups crispy fried onions", 15, "oven", 30, 375),
    "cranberry": ("Cranberry sauce", "12 oz fresh cranberries\n1 cup sugar\n1 cup orange juice", 5, "stove", 15, 0),
    "rolls": ("Dinner rolls", "2 dozen dinner rolls\nbutter", 5, "oven", 15, 375),
    "brussels": ("Roasted Brussels sprouts", "2 lb Brussels sprouts\nolive oil\nkosher salt\nblack pepper",
                 15, "oven", 25, 400),
    "salad": ("Green salad", "2 heads lettuce\n1 cucumber\ncherry tomatoes\nvinaigrette", 15, "none", 0, 0),
}

OUR_MENU = ("house", "turkey", "potatoes", "gravy", "stuffing", "mac", "sourdough", "pie")

# Dishes added to the menu after plans already existed: (migration key, preset key).
LATER_ADDITIONS = (("add-mac", "mac"),)

PRESET_BUTTONS = (
    ("turkey", "Turkey"), ("potatoes", "Mashed potatoes"), ("gravy", "Gravy"), ("stuffing", "Stuffing"),
    ("mac", "Mac and cheese"),
    ("sourdough", "Sourdough"), ("pie", "Pumpkin pie"), ("green_beans", "Green beans"),
    ("cranberry", "Cranberry sauce"), ("rolls", "Rolls"), ("brussels", "Brussels sprouts"),
    ("salad", "Salad"), ("house", "House prep"),
)
