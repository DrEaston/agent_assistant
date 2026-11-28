"""Studio tools for Claude and ChatGPT (core/connector): read the problem reports Curtis jots down with "Report a
problem" inside Dieter, mark them done once they're fixed, and pass on a build request he asks an assistant to send.
Admins only, like the Studio pages."""

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
    status = args.get("status") or "ready_for_review"
    if status not in STATUSES:
        raise ToolError(f"status is one of: {', '.join(STATUSES)}.")
    routes.store().set_status(report["id"], status, (args.get("note") or "").strip()[:500])
    return _brief(routes.store().report(report["id"]))


ASSISTANTS = {"chatgpt": "ChatGPT", "claude": "Claude"}


def _text(args, name, limit, required=True):
    value = str(args.get(name) or "").strip()[:limit]
    if required and not value:
        raise ToolError(f"{name} is required.")
    return value


def request_change(args):
    """An assistant passes on something Curtis wants built: a Studio report Claude Code picks up like his own."""
    _admin()
    who = ASSISTANTS.get(str(args.get("assistant") or "").strip().lower())
    if not who:
        raise ToolError("assistant should be chatgpt or claude (who is sending this).")
    title = " ".join(_text(args, "title", 120).split())
    parts = [f"Sent by {who} for Curtis.", "", "## What he wants", _text(args, "what", 6000), "",
             "## How he'll know it works", _text(args, "done_when", 2000)]
    details = _text(args, "details", 20000, required=False)
    if details:
        parts += ["", "## Details (data, examples, constraints)", details]
    area = " ".join(_text(args, "area", 60, required=False).split()) or "Not sure"
    db = routes._state["db"]
    report_id = db.add_app_feedback_report(title=f"[{who}] {title}", area=area, page_url="",
                                           page_title=f"Request from {who}", reporter_name=f"{who} (for Curtis)",
                                           reporter_email="", raw_feedback="\n".join(parts))
    return {"report_id": report_id, "status": "open", "url": site_url(f"{routes.BASE}/{report_id}"),
            "message": "Sent to Dieter Studio. Claude Code builds from Studio: it works out where the change goes, "
                       "asks Curtis if anything is unclear, and opens a pull request he merges. studio_get_report "
                       "shows its progress."}


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
             "Once the fix is live, mark the report ready_for_review with a short note: what changed and how to check "
             "it. Curtis confirms it works on the report page (it waits on his Today page until then). Use done only "
             "when he has said it works; in_progress while working on it; open to put it back.", set_status,
             dict(REPORT_ID, status={"type": "string", "enum": list(STATUSES)}, note={"type": "string"}),
             ("report_id",)),
        Tool("studio_request_change", "Ask Claude Code to build something",
             "Only when Curtis asks you to send a build request or change to Dieter. It goes to Dieter Studio, where "
             "Claude Code (which changes Dieter's code) picks it up, works out where it belongs, asks him if anything "
             "is unclear, and opens a pull request he merges. One request per thing. Describe the outcome he wants "
             "and how he'll check it, in plain words; don't write code. Put any data, examples, tool or table shapes "
             "you need in details.",
             request_change,
             {"title": {"type": "string", "description": "Short, e.g. \"Store ChatGPT images with their source\"."},
              "what": {"type": "string", "description": "What he wants and why, as he'd say it."},
              "done_when": {"type": "string", "description": "How he'll check it works."},
              "details": {"type": "string", "description": "Optional: data, examples, inputs/outputs you need."},
              "area": {"type": "string", "description": "Optional: Kitchen, Assistant, Trainer, Music, Logbook, "
                                                        "Animation, Connector, or Not sure."},
              "assistant": {"type": "string", "enum": list(ASSISTANTS), "description": "Who is sending: you."}},
             ("title", "what", "done_when", "assistant")),
    ])
