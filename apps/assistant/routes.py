"""The Assistant's pages and forms: to-dos at a glance, projects (actions, steps, goals, blockers, notes), Reading.

Moved out of api.py. The older Codex project pieces (reviews, research results, plans) and the to-do (scheduler)
forms are still in api.py; move them here when you touch them.
"""

import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import pages, reading

router = APIRouter()
_state = {"db": None}


def configure(db):
    _state["db"] = db


def _db():
    return _state["db"]


def dict_from_row(row):
    return dict(row) if row is not None else None


def dicts_from_rows(rows):
    return [dict(r) for r in rows]


def safe_redirect_path(path, fallback="/"):
    """Keep form redirects inside this app."""
    return path if path and path.startswith("/") and not path.startswith("//") else fallback


@router.get("/apps/assistant")
def assistant_app(request: Request):
    """The Assistant: to-dos (with dates), projects and reading at a glance (apps/assistant/pages.py)."""
    items = [pages.todo_view(dict(r)) for r in _db().get_scheduler_items(status="open", limit=100)]
    done = [pages.todo_view(dict(r)) for r in _db().get_recent_completed_scheduler_items(limit=8)]
    books = reading.ReadingStore(db).books()
    reading = {s: sum(1 for b in books if b["status"] == s) for s in reading.STATUSES}
    return pages.home_page(todos=items, done=done, projects=assistant_project_rows(), reading=reading)


def assistant_project_rows():
    rows = []
    for project in dicts_from_rows(_db().get_all_projects()):
        open_actions = dicts_from_rows(_db().get_open_recommended_actions(project["id"]))
        rows.append({"id": project["id"], "name": project["name"], "open": len(open_actions),
                     "next": open_actions[0]["action"] if open_actions else "",
                     "about": (project.get("description") or "").strip().splitlines()[0][:140]
                     if (project.get("description") or "").strip() else ""})
    return rows


@router.get("/apps/assistant/projects")
def assistant_projects_page(request: Request):
    return pages.projects_page(projects=assistant_project_rows())


@router.get("/apps/assistant/reading")
def assistant_reading_page(request: Request, show: str = ""):
    store = reading.ReadingStore(db)
    return pages.reading_page(books=store.books(), show=show if show in reading.STATUSES else "")


@router.get("/apps/assistant/reading/bookstore")
def assistant_bookstore_page(request: Request):
    return pages.bookstore_page(checklist=reading.ReadingStore(db).checklist())


@router.post("/apps/assistant/reading/add")
def assistant_reading_add(title: str = Form(...), author: str = Form(""), high_priority: str = Form("")):
    try:
        reading.ReadingStore(db).add(title, author, bool(high_priority))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/apps/assistant/reading", status_code=303)


@router.post("/apps/assistant/reading/{book_id}/status")
def assistant_reading_status(book_id: int, status: str = Form(...), next: str = Form("/apps/assistant/reading")):
    try:
        reading.ReadingStore(db).update(book_id, status=status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=safe_redirect_path(next, "/apps/assistant/reading"), status_code=303)


@router.post("/apps/assistant/reading/{book_id}/delete")
def assistant_reading_delete(book_id: int):
    reading.ReadingStore(db).delete(book_id)
    return RedirectResponse(url="/apps/assistant/reading", status_code=303)


@router.post("/apps/assistant/reading/bookstore/shelf")
def assistant_bookstore_shelf(key: str = Form(...), checked: str = Form("")):
    reading.ReadingStore(db).check_shelf(key, bool(checked))
    return RedirectResponse(url="/apps/assistant/reading/bookstore", status_code=303)


@router.post("/apps/assistant/reading/bookstore/done")
def assistant_bookstore_done():
    reading.ReadingStore(db).finish_visit()
    return RedirectResponse(url="/apps/assistant/reading", status_code=303)


@router.post("/apps/assistant/feedback")
def assistant_feedback(request: Request, text: str = Form(...), page: str = Form("/apps/assistant")):
    """The Assistant pages' "Report a problem" box: a Studio report."""
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Describe the problem.")
    user = getattr(request.state, "current_user", None) or {}
    _db().add_app_feedback_report(title=f"[Assistant] {text.splitlines()[0][:80]}", area="Assistant", page_url=page[:300],
                               page_title="Assistant", reporter_name=user.get("name", "") or "",
                               reporter_email=user.get("email", "") or "", raw_feedback=text[:4000])
    return RedirectResponse(url=safe_redirect_path(page, "/apps/assistant") , status_code=303)


@router.get("/apps/assistant/planner")
def assistant_planner_app(request: Request):
    """The old planner page: the Assistant's home now."""
    return RedirectResponse(url="/apps/assistant", status_code=303)


@router.get("/apps/assistant/scheduler")
def assistant_scheduler_app(request: Request):
    """The old scheduler page: to-dos are the Assistant's home now."""
    return RedirectResponse(url="/apps/assistant", status_code=303)


@router.get("/projects/{project_id}")
def project_detail(request: Request, project_id: int):
    """A project in the Assistant: next actions (with steps), goals, blockers, notes (apps/assistant/pages.py)."""
    project = dict_from_row(_db().get_project_by_id(project_id))
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    actions = []
    for action in dicts_from_rows(_db().get_recommended_actions(project_id)):
        steps = dicts_from_rows(_db().get_task_steps(action["id"]))
        actions.append(dict(action, done=action.get("status") == "done", steps_total=len(steps),
                            steps_done=sum(1 for s in steps if s.get("status") == "done")))
    notes = [dict(n, when=str(n.get("created_at") or "")[:10]) for n in dicts_from_rows(_db().get_notes(project_id))]
    todos = [pages.todo_view(dict(r)) for r in _db().get_scheduler_items(status="open", limit=100)
             if dict(r).get("project_id") == project_id]
    return pages.project_page(project=project, actions=actions, goals=dicts_from_rows(_db().get_weekly_goals(project_id)),
                                        blockers=dicts_from_rows(_db().get_blockers(project_id)), notes=notes, todos=todos)


@router.post("/projects/{project_id}/update")
def update_project_form(
    project_id: int,
    name: str = Form(...),
    description: str = Form(""),
    priority_score: int = Form(3),
):
    """Update the project identity and brief from its detail page."""
    project = dict_from_row(_db().get_project_by_id(project_id))
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    clean_name = re.sub(r"\s+", " ", (name or "").strip())
    if not clean_name:
        raise HTTPException(status_code=400, detail="Project name is required")
    _db().update_project_details(
        project_id,
        name=clean_name[:80],
        description=(description or "").strip(),
        priority_score=max(1, min(5, priority_score)),
    )
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}/actions/{action_id}")
def action_detail(request: Request, project_id: int, action_id: int):
    """A project action and its steps (apps/assistant/pages.py)."""
    project = dict_from_row(_db().get_project_by_id(project_id))
    action = dict_from_row(_db().get_recommended_action(action_id))
    if not project or not action or action["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Task not found")
    steps = [dict(s, done=s.get("status") == "done") for s in dicts_from_rows(_db().get_task_steps(action_id))]
    return pages.action_page(project=project, action=dict(action, done=action.get("status") == "done"), steps=steps)


@router.post("/projects/{project_id}/notes/add")
@router.post("/projects/{project_id}/notes/create")
def add_note_form(project_id: int, content: str = Form(...)):
    """Add note via form."""
    _db().add_note(project_id, content)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/actions/add")
@router.post("/projects/{project_id}/actions/create")
def add_action_form(project_id: int, title: str = Form(None), action: str = Form(None), priority: str = Form(...)):
    """Add action via form."""
    _db().add_recommended_action(project_id, title or action, priority)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/actions/{action_id}/complete")
def complete_action_form(project_id: int, action_id: int):
    """Mark action complete via form."""
    _db().mark_recommended_action_complete(action_id)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/actions/{action_id}/steps/create")
def add_task_step_form(project_id: int, action_id: int, step: str = Form(...)):
    """Add a checklist step to a task."""
    _db().add_task_step(action_id, step)
    return RedirectResponse(url=f"/projects/{project_id}/actions/{action_id}", status_code=303)


@router.post("/projects/{project_id}/actions/{action_id}/steps/{step_id}/complete")
def complete_task_step_form(project_id: int, action_id: int, step_id: int):
    """Mark a task checklist step complete."""
    _db().mark_task_step_complete(step_id)
    return RedirectResponse(url=f"/projects/{project_id}/actions/{action_id}", status_code=303)


@router.post("/projects/{project_id}/actions/{action_id}/steps/{step_id}/reopen")
def reopen_task_step_form(project_id: int, action_id: int, step_id: int):
    """Reopen a completed task checklist step."""
    _db().reopen_task_step(step_id)
    return RedirectResponse(url=f"/projects/{project_id}/actions/{action_id}", status_code=303)


@router.post("/projects/{project_id}/blockers/add")
@router.post("/projects/{project_id}/blockers/create")
def add_blocker_form(project_id: int, description: str = Form(...), severity: str = Form(...)):
    """Add blocker via form."""
    _db().add_blocker(project_id, description, severity)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/goals/add")
@router.post("/projects/{project_id}/goals/create")
def add_goal_form(project_id: int, title: str = Form(None), goal: str = Form(None), target_completion: str = Form("")):
    """Add goal via form."""
    _db().add_weekly_goal(project_id, title or goal)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/goals/{goal_id}/complete")
def complete_goal_form(project_id: int, goal_id: int):
    """Mark goal as complete via form."""
    _db().mark_goal_complete(goal_id)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/blockers/{blocker_id}/delete")
def delete_blocker_form(project_id: int, blocker_id: int):
    """Delete blocker via form."""
    _db().delete_blocker(blocker_id)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)
