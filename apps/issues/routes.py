"""Dieter Studio: the inbox for problem reports and change requests.

Studio collects reports (this form, "Report a problem" on any page, other services via
/internal/studio/reports) and keeps each one's history, including archived Codex runs. Claude reads them and marks
them done through the connector (apps/issues/connector.py): Curtis asks in a Claude chat, from any device.
(Sending a report to Claude Code as a GitHub issue was retired; old reports keep their issue link.)

The main app calls configure() at startup and includes `router`.
"""

import re
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core import ui

from .manifest import APP
from .store import StudioStore

router = APIRouter()
BASE = "/apps/issues"

_templates = ui.templates(Path(__file__).parent / "templates")
_state = {"store": None, "db": None, "require_admin": None, "summarize": None, "areas": ()}


def configure(db, require_admin, summarize=None, areas=()):
    """require_admin(request) raises unless the user is an admin.
    summarize(text, page_url, page_title) -> {"title", "area"} drafts a title (falls back to the first line)."""
    store = StudioStore(db)
    _state.update(store=store, db=db, require_admin=require_admin, summarize=summarize, areas=tuple(areas))


def startup():
    """Tidy-ups that write to the database, run once it's loaded (api.py prepare_database)."""
    try:
        filled = store().archive_codex_runs()
        if filled:
            print(f"Studio: archived old Codex runs onto {filled} reports")
    except Exception as exc:
        print(f"Warning: Studio could not archive Codex runs: {exc}")


def store():
    if _state["store"] is None:
        raise RuntimeError("Studio is not configured")
    return _state["store"]


def render(name, request, **context):
    context.setdefault("app", APP)
    context.setdefault("page_url", str(request.url.path))
    return HTMLResponse(_templates.get_template(name).render(**context))


def admin(request):
    _state["require_admin"](request)


def require_report(report_id):
    report = store().report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


def _user_fields(request):
    user = getattr(request.state, "current_user", None) or {}
    get = user.get if hasattr(user, "get") else (lambda k, d="": getattr(user, k, d))
    return {"reporter_name": get("display_name", "") or get("name", "") or get("email", "") or "",
            "reporter_email": get("email", "") or ""}


def _draft_title(text, page_url, page_title, area):
    if _state["summarize"]:
        try:
            draft = _state["summarize"](text, page_url=page_url, page_title=page_title) or {}
            return draft.get("title") or "", area or draft.get("area") or ""
        except Exception as exc:
            print(f"Warning: Studio title draft failed: {exc}")
    first = re.sub(r"\s+", " ", text.strip().splitlines()[0])[:90]
    return first, area


# ---------------------------------------------------------------- pages

@router.get(BASE)
def studio_home(request: Request, show: str = "active"):
    admin(request)
    which = show if show in ("active", "done", "all") else "active"
    return render("studio_index.html", request, reports=store().reports(which), show=which, counts=store().counts())


@router.get(BASE + "/new")
def studio_new(request: Request, area: str = "", page: str = ""):
    admin(request)
    return render("studio_new.html", request, area=area, page=page, areas=_state["areas"])


@router.post(BASE + "/create")
def studio_create(request: Request, text: str = Form(...), area: str = Form(""), page: str = Form(""),
                  page_title: str = Form("")):
    admin(request)
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Describe the problem or change.")
    title, area = _draft_title(text, page, page_title, area)
    db = _state["db"]
    duplicate = db.find_recent_duplicate_app_feedback_report(text, area or "Dieter")
    if duplicate:
        return RedirectResponse(url=f"{BASE}/{duplicate['id']}", status_code=303)
    report_id = db.add_app_feedback_report(title=title or "Change request", area=area or "Dieter", page_url=page[:300],
                                           page_title=page_title[:200] or "Studio", raw_feedback=text[:4000],
                                           **_user_fields(request))
    db.append_app_feedback_report_action(report_id, "created", "Report created in Studio.")
    return RedirectResponse(url=f"{BASE}/{report_id}", status_code=303)


# Old Studio addresses (under Assistant) and the old worker page keep working.
@router.get("/apps/assistant/feedback")
@router.get("/apps/assistant/feedback/{rest:path}")
@router.get(BASE + "/workers")
def studio_old_links(rest: str = ""):
    target = f"{BASE}/{rest.split('/')[0]}" if rest and rest.split("/")[0].isdigit() else BASE
    return RedirectResponse(url=target, status_code=303)


@router.get(BASE + "/{report_id}")
def studio_report(request: Request, report_id: int):
    admin(request)
    report = require_report(report_id)
    return render("studio_report.html", request, report=report)


@router.post(BASE + "/{report_id}/status")
def studio_status(request: Request, report_id: int, status: str = Form(...)):
    admin(request)
    require_report(report_id)
    try:
        store().set_status(report_id, status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown status")
    return RedirectResponse(url=f"{BASE}/{report_id}", status_code=303)


@router.post(BASE + "/{report_id}/delete")
def studio_delete(request: Request, report_id: int):
    admin(request)
    require_report(report_id)
    store().delete(report_id)
    return RedirectResponse(url=BASE, status_code=303)

