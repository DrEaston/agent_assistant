"""The Logbook page (/logbook): Curtis's tables, CSV upload, a table and its versions, and proposals to approve.

Only these pages approve proposals and mark versions verified (they need his signed-in session; the connector
can't). Writes are plain form posts that redirect back.
"""

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from core import ui

from . import connector, report
from .store import MAX_ROWS, who
from .types import TYPE_HELP, TYPES, Invalid, read_csv, show

APP = {"id": "logbook", "title": "Logbook", "nav_label": "Logbook", "accent": "today"}
MAX_UPLOAD = 2 * 1024 * 1024
STATUS_TONE = {"verified": "ok", "draft": "info", "superseded": None, "pending": "info", "approved": "ok",
               "discarded": None, "stale": "warn"}

router = APIRouter()
_templates = ui.templates(Path(__file__).parent / "templates")
_templates.globals.update(who=who, tone=STATUS_TONE.get, summary_line=report.summary_line, short_day=report.short_day)


def store():
    return connector.store()


def render(template, request, status_code=200, **context):
    context.setdefault("app", APP)
    context.setdefault("page_url", str(request.url.path))
    return HTMLResponse(_templates.get_template(template).render(**context), status_code=status_code)


def back(url, message=None, error=None):
    sep = "&" if "?" in url else "?"
    if message:
        url += f"{sep}done={quote(message)}"
    elif error:
        url += f"{sep}error={quote(error)}"
    return RedirectResponse(url, status_code=303)


@router.get("/logbook")
def logbook_home(request: Request):
    s = store()
    return render("logbook_home.html", request, tables=s.tables(), proposals=s.proposals("pending"),
                  done=request.query_params.get("done"), error=request.query_params.get("error"), max_rows=MAX_ROWS)


@router.post("/logbook/upload")
async def logbook_upload(request: Request, file: UploadFile = File(...), name: str = Form("")):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return back("/logbook", error="That file is over 2 MB. A Logbook table should be small.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return back("/logbook", error="That file isn't plain text. Save it as CSV (UTF-8) and try again.")
    try:
        columns, rows = read_csv(text, MAX_ROWS)
    except Invalid as exc:
        return back("/logbook", error=str(exc))
    default_name = Path(file.filename or "").stem.replace("_", " ").strip()
    return render("logbook_upload.html", request, columns=columns, rows=rows[:8], total=len(rows), csv_text=text,
                  name=" ".join(name.split()) or default_name.capitalize(), types=TYPES, type_help=TYPE_HELP)


@router.post("/logbook/create")
async def logbook_create(request: Request):
    form = await request.form()
    try:
        columns, rows = read_csv(str(form.get("csv_text") or ""), MAX_ROWS)
        for i, col in enumerate(columns):
            col["type"] = str(form.get(f"type_{i}") or col["type"])
            col["label"] = " ".join(str(form.get(f"label_{i}") or col["label"]).split())
        table_id, _ = store().create(form.get("name"), columns, rows, form.get("description") or "",
                                     made_by="curtis", reason="Uploaded from a CSV")
    except Invalid as exc:
        return back("/logbook", error=str(exc))
    return back(f"/logbook/{table_id}", "Saved as a draft. Check it, then mark it verified.")


@router.get("/logbook/proposals/{proposal_id}")
def logbook_proposal(request: Request, proposal_id: str):
    s = store()
    try:
        prop = s.proposal(proposal_id)
        table = s.table(prop["table_id"]) if prop["table_id"] else None
    except Invalid as exc:
        return back("/logbook", error=str(exc))
    return render("logbook_proposal.html", request, prop=prop, table=table, error=request.query_params.get("error"))


@router.post("/logbook/proposals/{proposal_id}/approve")
def logbook_approve(proposal_id: str):
    try:
        table_id, _ = store().approve(proposal_id)
    except Invalid as exc:
        return back(f"/logbook/proposals/{proposal_id}", error=str(exc))
    return back(f"/logbook/{table_id}", "Approved. It's a new draft: check it, then mark it verified.")


@router.post("/logbook/proposals/{proposal_id}/discard")
def logbook_discard(proposal_id: str):
    try:
        store().discard(proposal_id)
    except Invalid as exc:
        return back(f"/logbook/proposals/{proposal_id}", error=str(exc))
    return back("/logbook", "Discarded. Nothing changed.")


@router.get("/logbook/{table_id}")
def logbook_table(request: Request, table_id: str):
    s = store()
    try:
        table, version = s.version(table_id, request.query_params.get("version") or "latest")
    except Invalid as exc:
        return back("/logbook", error=str(exc))
    rows = []
    if version:
        rows = [[show(c["type"], r.get(c["name"])) for c in version["columns"]] for r in version["rows"]]
    return render("logbook_table.html", request, table=table, version=version, rows=rows,
                  label=report.label(table, version), proposals=s.proposals("pending", table["id"]),
                  done=request.query_params.get("done"), error=request.query_params.get("error"))


@router.post("/logbook/{table_id}/verify")
def logbook_verify(table_id: str, version_id: str = Form(...)):
    try:
        store().verify(table_id, version_id)
    except Invalid as exc:
        return back(f"/logbook/{table_id}", error=str(exc))
    return back(f"/logbook/{table_id}", "Verified. ChatGPT and Claude will use this version for answers.")
