"""Kitchen tools for Claude (core/connector): shopping lists and the recipe library, as the signed-in user."""

from datetime import datetime

from core.connector.tools import STRINGS, Tool, ToolError, ToolSet, site_url, text_list, whole_number

from .shopping import router as shopping


def _list(list_id):
    grocery_list = shopping.store().get_list(whole_number(list_id, "list_id"))
    if not grocery_list:
        raise ToolError(f"There's no shopping list {list_id}. Use kitchen_list_shopping_lists to find one.")
    return grocery_list


def _brief(grocery_list, items):
    return {"id": grocery_list["id"], "title": grocery_list.get("title") or "Shopping list",
            "status": grocery_list.get("status") or "active", "created": shopping.friendly_date(grocery_list.get("created_at")),
            "items_left": items["left"], "items_total": items["total"],
            "url": site_url(f"{shopping.BASE}/{grocery_list['id']}")}


def _view(grocery_list):
    items = shopping.list_items(grocery_list)
    view = _brief(grocery_list, items)
    view["for_meals"] = [m["title"] for m in items["meals"] + items["dishes"]]
    view["aisles"] = [{"aisle": section["name"], "items": [
        {"key": item["key"], "text": item["text"], "amount": item.get("amount") or "",
         "for": item.get("dishes") or [], "checked": item["key"] in items["checked"],
         "typed_in": bool(item.get("extra_id"))} for item in section["items"]]} for section in items["sections"]]
    return view


def _items_by_key(grocery_list):
    items = shopping.list_items(grocery_list)
    return {item["key"]: item for section in items["sections"] for item in section["items"]}


def _known_keys(grocery_list, keys):
    by_key = _items_by_key(grocery_list)
    missing = [k for k in keys if k not in by_key]
    if missing:
        raise ToolError(f"These keys aren't on list {grocery_list['id']}: {', '.join(missing)}. "
                        "Read the list with kitchen_get_shopping_list for the right keys.")
    return by_key


# ---------------------------------------------------------------- handlers

def list_lists(args):
    status = "done" if args.get("finished") else "active"
    lists = shopping.store().get_lists(status, 20)
    return {"lists": [_brief(g, shopping.list_items(g)) for g in lists]}


def get_list(args):
    return _view(_list(args.get("list_id")))


def add_items(args):
    grocery_list = _list(args.get("list_id"))
    for line in text_list(args.get("items"), "items"):
        shopping.store().add_extra(grocery_list["id"], line)
    return _view(grocery_list)


def check_items(args):
    grocery_list = _list(args.get("list_id"))
    keys = text_list(args.get("keys"), "keys")
    _known_keys(grocery_list, keys)
    checked = args.get("checked", True) is not False
    for key in keys:
        shopping.store().set_checked(grocery_list["id"], key, checked)
    return _view(grocery_list)


def remove_items(args):
    grocery_list = _list(args.get("list_id"))
    keys = text_list(args.get("keys"), "keys")
    by_key = _known_keys(grocery_list, keys)
    fixed = [by_key[k]["text"] for k in keys if not by_key[k].get("extra_id")]
    if fixed:
        raise ToolError(f"{', '.join(fixed)} come from the list's recipes, so they can't be removed one by one. "
                        "Check them off instead, or ask the person to take the meal off the list.")
    for key in keys:
        shopping.store().delete_extra(grocery_list["id"], by_key[key]["extra_id"])
    return _view(grocery_list)


def create_list(args):
    title = " ".join(str(args.get("title") or "").split())[:120] or shopping.default_title()
    list_id = shopping.store().create_list(title, [])
    if args.get("items"):
        for line in text_list(args.get("items"), "items"):
            shopping.store().add_extra(list_id, line)
    return _view(shopping.store().get_list(list_id))


def finish_list(args):
    grocery_list = _list(args.get("list_id"))
    carry = text_list(args.get("carry_over"), "carry_over") if args.get("carry_over") else []
    by_key = _known_keys(grocery_list, carry)
    answer = {"finished": _brief(grocery_list, shopping.list_items(grocery_list))}
    if carry:
        title = (" ".join(str(args.get("new_title") or "").split())[:120]
                 or "Next trip · " + shopping.friendly_date(datetime.now().isoformat()))
        new_id = shopping.store().create_list(title, [])
        for key in carry:
            item = by_key[key]
            shopping.store().add_extra(new_id, f"{item['text']} ({item['amount']})" if item.get("amount") else item["text"])
        answer["next_trip"] = _view(shopping.store().get_list(new_id))
    shopping.store().set_status(grocery_list["id"], "done")
    answer["finished"]["status"] = "done"
    return answer


def search_recipes(args):
    from .thanksgiving.router import local_library

    words = str(args.get("query") or "").lower().split()
    recipes = [r for r in local_library(shopping._state["db"]) if all(w in r["title"].lower() for w in words)]
    return {"recipes": [{"id": r["id"], "title": r["title"]} for r in recipes[:50]], "matches": len(recipes)}


def get_recipe(args):
    from .thanksgiving.router import local_library_recipe

    recipe = local_library_recipe(shopping._state["db"], whole_number(args.get("recipe_id"), "recipe_id"))
    if not recipe:
        raise ToolError(f"There's no recipe {args.get('recipe_id')} in the library.")
    return {"id": recipe["source_meal_id"], "title": recipe["name"], "ingredients": recipe["ingredients_text"],
            "instructions": recipe["instructions_text"], "make_ahead": recipe.get("notes") or ""}


LIST_ID = {"list_id": {"type": "integer", "description": "From kitchen_list_shopping_lists."}}


def tools():
    return ToolSet("kitchen", [
        Tool("kitchen_list_shopping_lists", "Shopping lists",
             "The person's Kitchen shopping lists (not the Thanksgiving list; see thanksgiving_get_shopping_list), "
             "newest first, with how many items are left.", list_lists,
             {"finished": {"type": "boolean", "description": "true for finished lists instead of active ones."}},
             read_only=True),
        Tool("kitchen_get_shopping_list", "Read a shopping list",
             "Every item on a list by aisle: key (use it to check off or remove), name, amount to buy, which meals "
             "it's for, whether it's checked, and whether it was typed in.", get_list, LIST_ID, ("list_id",),
             read_only=True),
        Tool("kitchen_add_items", "Add to a shopping list",
             "Add typed-in items to a list, e.g. [\"2 lemons\", \"foil\"].", add_items,
             dict(LIST_ID, items=STRINGS), ("list_id", "items")),
        Tool("kitchen_check_items", "Check off items",
             "Mark items bought (checked true, the default) or not bought (false).", check_items,
             dict(LIST_ID, keys=STRINGS, checked={"type": "boolean"}), ("list_id", "keys")),
        Tool("kitchen_remove_items", "Remove typed-in items",
             "Delete typed-in items from a list. Items that come from a meal's recipe can't be removed, only checked.",
             remove_items, dict(LIST_ID, keys=STRINGS), ("list_id", "keys"), destructive=True),
        Tool("kitchen_create_shopping_list", "New shopping list",
             "Start a new list, optionally with typed-in items.", create_list,
             {"title": {"type": "string"}, "items": STRINGS}),
        Tool("kitchen_finish_shopping_list", "Finish a shopping trip",
             "Mark a list done. Items in carry_over (keys of things they couldn't get) go onto a new list for the "
             "next trip, which is returned.", finish_list,
             dict(LIST_ID, carry_over=STRINGS, new_title={"type": "string"}), ("list_id",)),
        Tool("kitchen_search_recipes", "Find recipes",
             "Search the recipe library by words in the title (empty query lists them all).", search_recipes,
             {"query": {"type": "string"}}, read_only=True),
        Tool("kitchen_get_recipe", "Read a recipe",
             "A library recipe's ingredients, instructions and make-ahead notes.", get_recipe,
             {"recipe_id": {"type": "integer"}}, ("recipe_id",), read_only=True),
    ])
