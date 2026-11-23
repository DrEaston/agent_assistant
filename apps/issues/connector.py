"""Studio tools for Claude (core/connector): read the problem reports Curtis jots down with "Report a problem"
inside Dieter, and mark them done once they're fixed. Admins only, like the Studio pages."""

from core.connector.tools import Tool, ToolError, ToolSet, site_url, whole_number
from database import get_current_user_id

from . import routes
from .store import STATUSES


def _admin():
    user = routes._state["db"].get_user_by_id(get_current_user_id()) if get_current_user_id() else None
    if not user or dict(user).get("role") != "admin":
        raise ToolError("Studio reports are for Dieter's admin only.")


def _brief(report):
    return {"id": report["id"], "title": report.get("title") or "", "status": report["status_label"],
            "area": report.get("area") or "", "page": report.get("page_url") or "",
            "created": report["created"], "url": site_url(f"{routes.BASE}/{report['id']}")}


def list_reports(args):
    _admin()
    which = args.get("show") or "active"
    if which not in ("active", "done", "all"):
        raise ToolError("show is active, done or all.")
    return {"reports": [_brief(r) for r in routes.store().reports(which)]}


def _report(report_id):
    report = routes.store().report(whole_number(report_id, "report_id"))
    if not report:
        raise ToolError(f"There's no Studio report {report_id}. Use studio_list_reports to find one.")
    return report


def get_report(args):
    _admin()
    report = _report(args.get("report_id"))
    return dict(_brief(report), text=report.get("raw_feedback") or "", page_title=report.get("page_title") or "",
                reporter=report.get("reporter_name") or "",
                history=[{"what": h.get("action") or "", "note": h.get("summary") or "", "when": h.get("created_at") or ""}
                         for h in report["history"] if isinstance(h, dict)][-10:])


def set_status(args):
    _admin()
    report = _report(args.get("report_id"))
    status = args.get("status") or "done"
    if status not in STATUSES:
        raise ToolError(f"status is one of: {', '.join(STATUSES)}.")
    routes.store().set_status(report["id"], status, (args.get("note") or "").strip()[:500])
    return _brief(routes.store().report(report["id"]))


REPORT_ID = {"report_id": {"type": "integer", "description": "From studio_list_reports."}}


def tools():
    return ToolSet("studio", [
        Tool("studio_list_reports", "Studio reports",
             "Problems and change requests Curtis jotted down with \"Report a problem\" inside Dieter, newest first: "
             "title, status, app area and the page he was on.", list_reports,
             {"show": {"type": "string", "enum": ["active", "done", "all"],
                       "description": "active (default), done, or all."}}, read_only=True),
        Tool("studio_get_report", "Read a Studio report",
             "One report in full: what he wrote, the page he was on, and its history.", get_report, REPORT_ID,
             ("report_id",), read_only=True),
        Tool("studio_set_status", "Update a Studio report",
             "Mark a report done once the fix is live (or in_progress, ready_for_review, open), with a short note on "
             "what changed.", set_status,
             dict(REPORT_ID, status={"type": "string", "enum": list(STATUSES)}, note={"type": "string"}),
             ("report_id",)),
    ])
