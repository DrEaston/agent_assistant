"""Assistant tools for Claude (core/connector) and Dieter's chat: to-dos, projects, and reading.

api.py calls configure() with the main database (it still owns the project and to-do tables).
"""

from datetime import date, datetime

from core.connector.tools import Tool, ToolError, ToolSet, site_url, whole_number

from .reading import STATUS_LABELS, STATUSES, ReadingStore

_state = {"db": None}


def configure(db):
    _state["db"] = db


def db():
    if _state["db"] is None:
        raise ToolError("The Assistant isn't set up.")
    return _state["db"]


def reading():
    return ReadingStore(db())


def _row(r):
    return dict(r) if r else None


def _date(value):
    """"2026-11-27" (or empty for no date). Claude works dates out from 'Friday'; the server checks them."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        raise ToolError("Dates are YYYY-MM-DD, like 2026-11-27.") from None


# ---------------------------------------------------------------- to-dos

def _todo(item):
    return {"id": item["id"], "title": item.get("title") or "", "date": (item.get("scheduled_for") or "")[:10],
            "notes": item.get("notes") or "", "done": item.get("status") == "done",
            "project": item.get("project_name") or ""}


def list_todos(args):
    open_items = [_todo(dict(r)) for r in db().get_scheduler_items(status="open", limit=100)]
    answer = {"today": date.today().isoformat(), "open": open_items}
    if args.get("include_done"):
        answer["recently_done"] = [_todo(dict(r)) for r in db().get_recent_completed_scheduler_items(limit=10)]
    return answer


def _owned_todo(todo_id):
    item = _row(db().get_scheduler_item(whole_number(todo_id, "todo_id")))
    if not item:
        raise ToolError(f"There's no to-do {todo_id}. Use assistant_list_todos.")
    return item


def add_todo(args):
    title = " ".join(str(args.get("title") or "").split())[:200]
    if not title:
        raise ToolError("What's the to-do?")
    todo_id = db().add_scheduler_item(title, scheduled_for=_date(args.get("date")), notes=str(args.get("notes") or "")[:2000],
                                      source="dieter")
    return _todo(dict(db().get_scheduler_item(todo_id)))


def update_todo(args):
    item = _owned_todo(args.get("todo_id"))
    db().update_scheduler_item(item["id"], title=(str(args["title"])[:200] if args.get("title") else None),
                               scheduled_for=(_date(args["date"]) if "date" in args else None),
                               notes=(str(args["notes"])[:2000] if "notes" in args else None))
    return _todo(dict(db().get_scheduler_item(item["id"])))


def complete_todo(args):
    item = _owned_todo(args.get("todo_id"))
    if args.get("done", True) is False:
        db().reopen_scheduler_item(item["id"])
    else:
        db().mark_scheduler_item_complete(item["id"])
    return _todo(dict(db().get_scheduler_item(item["id"])))


# ---------------------------------------------------------------- projects

def _project(project_id):
    project = _row(db().get_project_by_id(whole_number(project_id, "project_id")))
    if not project:
        raise ToolError(f"There's no project {project_id}. Use assistant_list_projects.")
    return project


def list_projects(args):
    return {"projects": [{"id": p["id"], "name": p["name"], "about": (p.get("description") or "")[:160],
                          "open_actions": len(db().get_open_recommended_actions(p["id"])),
                          "url": site_url(f"/projects/{p['id']}")}
                         for p in (dict(r) for r in db().get_all_projects())]}


def get_project(args):
    project = _project(args.get("project_id"))
    actions = []
    for a in (dict(r) for r in db().get_recommended_actions(project["id"])):
        steps = [dict(s) for s in db().get_task_steps(a["id"])]
        actions.append({"id": a["id"], "action": a.get("action") or "", "priority": a.get("priority") or "",
                        "done": a.get("status") == "done",
                        "steps": [{"id": s["id"], "step": s.get("step") or "", "done": s.get("status") == "done"} for s in steps]})
    return {"id": project["id"], "name": project["name"], "description": project.get("description") or "",
            "actions": actions,
            "goals": [{"id": g["id"], "goal": g.get("goal") or "", "done": bool(g.get("completed"))}
                      for g in (dict(r) for r in db().get_weekly_goals(project["id"]))],
            "blockers": [{"id": b["id"], "blocker": b.get("description") or ""}
                         for b in (dict(r) for r in db().get_blockers(project["id"]))],
            "recent_notes": [{"when": str(n.get("created_at") or "")[:10], "note": n.get("content") or ""}
                             for n in (dict(r) for r in db().get_notes(project["id"]))][:15],
            "url": site_url(f"/projects/{project['id']}")}


def add_note(args):
    project = _project(args.get("project_id"))
    text = str(args.get("note") or "").strip()[:4000]
    if not text:
        raise ToolError("The note is empty.")
    db().add_note(project["id"], text)
    return {"added": True, "project": project["name"]}


def add_action(args):
    project = _project(args.get("project_id"))
    text = " ".join(str(args.get("action") or "").split())[:300]
    if not text:
        raise ToolError("What's the action?")
    priority = args.get("priority") if args.get("priority") in ("high", "medium", "low") else "medium"
    action_id = db().add_recommended_action(project["id"], text, priority)
    return {"id": action_id, "action": text, "project": project["name"]}


def create_project(args):
    name = " ".join(str(args.get("name") or "").split())[:120]
    if not name:
        raise ToolError("What's the project called?")
    existing = [dict(p) for p in db().get_all_projects() if dict(p)["name"].strip().lower() == name.lower()]
    if existing:
        raise ToolError(f"There's already a project called {name} (id {existing[0]['id']}).")
    kind = args.get("kind") if args.get("kind") in ("general", "research", "technical") else "general"
    project_id = db().add_project(name, str(args.get("description") or "").strip()[:20000], project_type=kind)
    return {"id": project_id, "name": name, "url": site_url(f"/projects/{project_id}")}


def update_project(args):
    """Rename a project or replace its description (the project's standing context, e.g. a roadmap)."""
    project = _project(args.get("project_id"))
    name = " ".join(str(args.get("name") or "").split())[:120] or None
    description = args.get("description")
    if name is None and description is None:
        raise ToolError("Give a new name or description.")
    db().update_project_details(project["id"], name=name,
                                description=None if description is None else str(description).strip()[:20000])
    return {"id": project["id"], "name": name or project["name"], "url": site_url(f"/projects/{project['id']}")}


def _action(action_id):
    action = _row(db().get_recommended_action(whole_number(action_id, "action_id")))
    if not action:
        raise ToolError(f"There's no action {action_id}. Use assistant_get_project.")
    _project(action["project_id"])  # someone else's project isn't found
    return action


def add_action_step(args):
    action = _action(args.get("action_id"))
    text = " ".join(str(args.get("step") or "").split())[:300]
    if not text:
        raise ToolError("What's the step?")
    step_id = db().add_task_step(action["id"], text)
    return {"id": step_id, "step": text, "action": action.get("action") or ""}


def complete_action(args):
    action = _row(db().get_recommended_action(whole_number(args.get("action_id"), "action_id")))
    if not action:
        raise ToolError(f"There's no action {args.get('action_id')}. Read the project for its actions.")
    _project(action["project_id"])  # yours (or shared with you)
    if args.get("done", True) is False:
        db().reopen_recommended_action(action["id"])
    else:
        db().mark_recommended_action_complete(action["id"])
    return {"id": action["id"], "action": action.get("action") or "", "done": args.get("done", True) is not False}


# ---------------------------------------------------------------- reading

def _book_view(b):
    return {"id": b["id"], "title": b["title"], "author": b["author"], "high_priority": bool(b["high_priority"]),
            "status": b["status"]}


def reading_list(args):
    status = args.get("status") or None
    if status and status not in STATUSES:
        raise ToolError(f"status is one of: {', '.join(STATUSES)}.")
    store = reading()
    answer = {"books": [_book_view(b) for b in store.books(status)]}
    if args.get("bookstore"):
        answer["bookstore_checklist"] = [{"author": g["author"], "shelf_key": g["key"], "shelf_checked": g["checked"],
                                          "books": [("★ " if b["high_priority"] else "") + b["title"] for b in g["books"]]}
                                         for g in store.checklist()]
    return answer


def add_book(args):
    try:
        book_id = reading().add(args.get("title"), args.get("author") or "", bool(args.get("high_priority")),
                                args.get("status") or "want", args.get("notes") or "")
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return _book_view(reading().book(book_id))


def _book(args):
    store = reading()
    if args.get("book_id"):
        book = store.book(whole_number(args["book_id"], "book_id"))
        if book:
            return book
    matches = store.find(args.get("title") or "")
    if not matches:
        raise ToolError("No book matches that. Use assistant_reading_list to see them.")
    if len(matches) > 1 and not args.get("book_id"):
        raise ToolError("More than one book matches: " + "; ".join(f"{b['title']} by {b['author']} (id {b['id']})"
                                                                     for b in matches[:5]) + ". Give book_id.")
    return matches[0]


def update_book(args):
    book = _book(args)
    try:
        reading().update(book["id"], status=args.get("status"), high_priority=args.get("high_priority"),
                         notes=args.get("notes"), title=args.get("new_title"), author=args.get("author"))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return _book_view(reading().book(book["id"]))


def check_shelf(args):
    store = reading()
    if args.get("finish_visit"):
        store.finish_visit()
        return {"bookstore": "Checklist cleared for the next trip."}
    wanted = str(args.get("author") or "").lower().strip()
    groups = [g for g in store.checklist() if wanted and (wanted in g["author"].lower() or wanted in g["key"])]
    if not groups:
        raise ToolError("No author on the bookstore checklist matches that.")
    for g in groups:
        store.check_shelf(g["key"], args.get("checked", True) is not False)
    return {"checked": [g["author"] for g in groups], "checked_on": args.get("checked", True) is not False}


CHAT_NOTES = """Assistant: Curtis's to-dos (with optional dates; due ones show on his Today page), his projects (description,
next actions with steps, goals, blockers, notes) and his reading list (assistant_ tools). "Remind me to X on Friday" is a
to-do with that date: work the date out from today (in assistant_list_todos) and say the day back. Reading: books he
wants, grouped by author, with ★ high priority and a status (want, found, owned, reading, read). At a bookstore he
checks author shelves off (assistant_check_shelf); that only means he looked, not that he found a book. "I found
Borne" sets that book's status to found. Give the bookstore checklist compactly: one line per author, ★ titles first."""

TODO_ID = {"todo_id": {"type": "integer", "description": "From assistant_list_todos."}}
PROJECT_ID = {"project_id": {"type": "integer", "description": "From assistant_list_projects."}}
DATE = {"type": "string", "description": "YYYY-MM-DD, or empty for no date."}


def tools():
    return ToolSet("assistant", [
        Tool("assistant_list_todos", "To-dos", "Open to-dos (date, notes), and today's date. include_done adds recently "
             "finished ones.", list_todos, {"include_done": {"type": "boolean"}}, read_only=True),
        Tool("assistant_add_todo", "Add a to-do", "A to-do or reminder, optionally on a date.", add_todo,
             {"title": {"type": "string"}, "date": DATE, "notes": {"type": "string"}}, ("title",)),
        Tool("assistant_update_todo", "Change a to-do", "New title, date (empty clears it) or notes.", update_todo,
             dict(TODO_ID, title={"type": "string"}, date=DATE, notes={"type": "string"}), ("todo_id",)),
        Tool("assistant_complete_todo", "Finish a to-do", "Mark a to-do done (done false reopens it).", complete_todo,
             dict(TODO_ID, done={"type": "boolean"}), ("todo_id",)),
        Tool("assistant_list_projects", "Projects", "Active projects with how many actions are open.", list_projects,
             read_only=True),
        Tool("assistant_get_project", "Read a project", "A project's description, actions (with steps), goals, "
             "blockers and recent notes.", get_project, PROJECT_ID, ("project_id",), read_only=True),
        Tool("assistant_add_project_note", "Add a project note", "Add a note to a project.", add_note,
             dict(PROJECT_ID, note={"type": "string"}), ("project_id", "note")),
        Tool("assistant_add_project_action", "Add a next action", "Add a next action to a project.", add_action,
             dict(PROJECT_ID, action={"type": "string"}, priority={"type": "string", "enum": ["high", "medium", "low"]}),
             ("project_id", "action")),
        Tool("assistant_create_project", "Start a project",
             "A new project with a description (its standing context: goals, roadmap, decisions). kind: general, "
             "research or technical.", create_project,
             {"name": {"type": "string"}, "description": {"type": "string"},
              "kind": {"type": "string", "enum": ["general", "research", "technical"]}}, ("name",)),
        Tool("assistant_update_project", "Change a project",
             "Rename a project or replace its description. Read it first (assistant_get_project): the description is "
             "replaced whole.", update_project,
             dict(PROJECT_ID, name={"type": "string"}, description={"type": "string"}), ("project_id",)),
        Tool("assistant_add_action_step", "Add a step to an action",
             "Break a project's next action into checklist steps.", add_action_step,
             {"action_id": {"type": "integer", "description": "From assistant_get_project."},
              "step": {"type": "string"}}, ("action_id", "step")),
        Tool("assistant_complete_project_action", "Finish a next action", "Mark a project action done (done false "
             "reopens it).", complete_action, {"action_id": {"type": "integer"}, "done": {"type": "boolean"}},
             ("action_id",)),
        Tool("assistant_reading_list", "Reading list", "Books by author with ★ priority and status. bookstore true adds "
             "the bookstore checklist (authors A-Z with the books he's looking for, and which shelves he's checked).",
             reading_list, {"status": {"type": "string", "enum": list(STATUSES)}, "bookstore": {"type": "boolean"}},
             read_only=True),
        Tool("assistant_add_book", "Add a book", "Add a book he wants (or already has, with status).", add_book,
             {"title": {"type": "string"}, "author": {"type": "string"}, "high_priority": {"type": "boolean"},
              "status": {"type": "string", "enum": list(STATUSES)}, "notes": {"type": "string"}}, ("title",)),
        Tool("assistant_update_book", "Update a book", "Change a book's status (found, owned, reading, read), ★ "
             "priority or notes. Find it by title words or book_id.", update_book,
             {"title": {"type": "string", "description": "Words from the title to find it."},
              "book_id": {"type": "integer"}, "status": {"type": "string", "enum": list(STATUSES)},
              "high_priority": {"type": "boolean"}, "notes": {"type": "string"}, "new_title": {"type": "string"},
              "author": {"type": "string"}}),
        Tool("assistant_check_shelf", "Bookstore shelf", "At a bookstore: mark an author's shelf checked (checked false "
             "unchecks), or finish_visit true to clear the checklist for next time.", check_shelf,
             {"author": {"type": "string"}, "checked": {"type": "boolean"}, "finish_visit": {"type": "boolean"}}),
    ])
