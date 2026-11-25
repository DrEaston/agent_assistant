"""The Assistant's pages, in the shared design: To-dos (home), Projects, a project, a project action, Reading.

api.py still owns the project and to-do data; it loads them and calls these render functions. The form posts it
already has (/scheduler/*, /projects/*) do the changes.
"""

from datetime import date
from pathlib import Path

from fastapi.responses import HTMLResponse

from core import ui

from .manifest import APP
from .reading import SHELF, STATUS_LABELS, STATUSES

_templates = ui.templates(Path(__file__).parent / "templates")
REPORT_URL = "/apps/assistant/feedback"

TABS = (
    ("To-dos", "/apps/assistant"),
    ("Projects", "/apps/assistant/projects"),
    ("Reading", "/apps/assistant/reading"),
)


def render(name, **context):
    context.setdefault("app", APP)
    context.setdefault("tabs", TABS)
    context.setdefault("report_url", REPORT_URL)
    return HTMLResponse(_templates.get_template(name).render(**context))


def todo_view(item, today=None):
    """A to-do for the list: id, title, notes, when ("Overdue · Nov 20", "Today", "Fri, Nov 27"), tone."""
    today = today or date.today()
    when, tone = "", None
    raw = (item.get("scheduled_for") or "")[:10]
    try:
        day = date.fromisoformat(raw) if raw else None
    except ValueError:
        day = None
    if day:
        label = f"{day.strftime('%a')}, {day.strftime('%b')} {day.day}"
        if day < today:
            when, tone = f"Overdue · {label}", "warn"
        elif day == today:
            when, tone = "Today", "info"
        else:
            when = label
    return {"id": item["id"], "title": item.get("title") or "To-do", "notes": item.get("notes") or "",
            "when": when, "tone": tone, "date": raw, "project": item.get("project_name") or "",
            "done": item.get("status") == "done"}


def home_page(*, todos, done, projects, reading, added=False):
    return render("assistant_home.html", title="To-dos", todos=todos, done=done, projects=projects, reading=reading,
                  added=added, current="/apps/assistant", page_url="/apps/assistant", today=date.today().isoformat())


def projects_page(*, projects):
    return render("assistant_projects.html", title="Projects", projects=projects, current="/apps/assistant/projects",
                  page_url="/apps/assistant/projects")


def project_page(*, project, actions, goals, blockers, notes, todos):
    return render("assistant_project.html", title=project["name"], project=project, actions=actions, goals=goals,
                  blockers=blockers, notes=notes, todos=todos, current="/apps/assistant/projects",
                  page_url=f"/projects/{project['id']}")


def action_page(*, project, action, steps):
    return render("assistant_action.html", title=action.get("action") or "Action", project=project, action=action,
                  steps=steps, current="/apps/assistant/projects",
                  page_url=f"/projects/{project['id']}/actions/{action['id']}")


def reading_page(*, books, show, suggestions=()):
    groups = {}
    shown = [b for b in books if (b["status"] == show if show else b["status"] in SHELF)]
    for book in shown:
        groups.setdefault(book["author_sort"] or "~", {"author": book["author"] or "Unknown author", "books": []})["books"].append(book)
    counts = {s: sum(1 for b in books if b["status"] == s) for s in STATUSES}
    return render("assistant_reading.html", title="Reading", groups=[groups[k] for k in sorted(groups)],
                  counts=counts, statuses=STATUSES, labels=STATUS_LABELS, show=show,
                  total=sum(1 for b in books if b["status"] in SHELF), suggestions=list(suggestions),
                  reading_now=[b for b in books if b["status"] == "reading"],
                  current="/apps/assistant/reading", page_url="/apps/assistant/reading")


def bookstore_page(*, checklist):
    return render("assistant_bookstore.html", title="At the bookstore", checklist=checklist,
                  checked=sum(1 for g in checklist if g["checked"]), current="/apps/assistant/reading",
                  page_url="/apps/assistant/reading/bookstore")
