"""Kitchen's side of portable shopping items (core/portable_list.py): another app puts items on a Kitchen list,
then follows them (were they bought?), checks them off, or takes them back.

Used by the /internal/kitchen/lists endpoints (an app running as its own service) and directly by an app
running inside the main app. Everything runs as the current user: a list that isn't theirs is "not found".
"""

from core import portable_list

from .store import ShoppingStore

BASE = "/apps/recipes/grocery-lists"
MAX_HOPS = 10  # a carried-on item can be carried on again; stop following after this many lists


def list_url(list_id):
    return f"{BASE}/{int(list_id)}"


def create_list(db, title, lines=(), items=(), source=None):
    """A new Kitchen list of typed-in lines and/or portable items. Returns {"id", "url", "count"}."""
    shopping = ShoppingStore(db)
    title = " ".join(str(title or "").split())[:120] or "Shopping list"
    items = portable_list.normalize_items(items)
    source = portable_list.normalize_source(source)
    if items and not source["app"]:
        raise ValueError("Portable items need a source app")
    list_id = shopping.create_list(title, [])
    for line in lines or ():
        if str(line).strip():
            shopping.add_extra(list_id, str(line)[:200])
    for item in items:
        shopping.add_link(list_id, source, item)
    return {"id": list_id, "url": list_url(list_id), "count": len(items) + sum(1 for l in lines or () if str(l).strip())}


def _follow(shopping, list_id, app, key):
    """Where an item from `app` is now, starting at list_id: (list, link) or (None, None) if it's on no list."""
    for _ in range(MAX_HOPS):
        grocery_list = shopping.get_list(list_id)
        link = shopping.get_link(list_id, app, key) if grocery_list else None
        if not link:
            return None, None
        if not link.get("moved_to"):
            return grocery_list, link
        list_id = link["moved_to"]
    return None, None


def linked_state(db, list_id, app):
    """{"found": the list exists for this user, "items": {source key: {"checked", "list_id", "url", "done"}}}.
    Items carried on to a newer list are reported where they are now. Keys missing from "items" are no longer on
    any list (the list was deleted, or the item taken off)."""
    shopping = ShoppingStore(db)
    if not shopping.get_list(int(list_id)):
        return {"found": False, "items": {}}
    app = portable_list.normalize_source({"app": app})["app"]
    items, checks = {}, {}
    for link in shopping.get_links(int(list_id), include_moved=True):
        if link["source_app"] != app:
            continue
        where, current = _follow(shopping, int(list_id), app, link["source_key"])
        if not where:
            continue
        if where["id"] not in checks:
            checks[where["id"]] = shopping.get_checked(where["id"])
        items[link["source_key"]] = {"checked": current["item_key"] in checks[where["id"]], "list_id": where["id"],
                                     "url": list_url(where["id"]), "done": where.get("status") == "done"}
    return {"found": True, "items": items}


def check(db, list_id, app, key, done):
    """Check (or uncheck) a linked item wherever it is now. False if it's on no list."""
    shopping = ShoppingStore(db)
    app = portable_list.normalize_source({"app": app})["app"]
    where, link = _follow(shopping, int(list_id), app, str(key))
    if not where:
        return False
    shopping.set_checked(where["id"], link["item_key"], bool(done))
    return True


def remove(db, list_id, app, key):
    """The item went back to the app it came from: take it off whichever list it's on now."""
    shopping = ShoppingStore(db)
    app = portable_list.normalize_source({"app": app})["app"]
    where, _ = _follow(shopping, int(list_id), app, str(key))
    if not where:
        return False
    return shopping.remove_link(where["id"], app, str(key))
