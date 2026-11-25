"""Logbook tools for ChatGPT and Claude: find and read tables, have Dieter add things up, and propose changes.

Assistants can only propose. Approving a proposal and marking a version verified happen on the Logbook page, by the
owner; there is deliberately no tool for either.
"""

from core.connector.pages import Page
from core.connector.tools import Tool, ToolError, ToolSet, site_url, whole_number

from . import report
from .store import MAX_ROWS, LogbookStore
from .types import TYPES, Invalid, check_columns, check_rows

ASSISTANTS = ("chatgpt", "claude")
_store = None


def configure(db):
    global _store
    _store = LogbookStore(db)
    return _store


def store():
    if _store is None:
        raise ToolError("The Logbook isn't set up on this server.")
    return _store


def _run(fn):
    try:
        return fn()
    except Invalid as exc:
        raise ToolError(str(exc)) from None


def _table_brief(table):
    pending = len(store().proposals("pending", table["id"]))
    return {"table_id": table["id"], "name": table["name"], "description": table["description"] or "",
            "columns": report.columns_out(table["columns"]),
            "latest_version": report.version_brief(table, table["latest"]),
            "verified_version": report.version_brief(table, table["verified"]),
            "pending_proposals": pending,
            "url": site_url(f"/logbook/{table['id']}")}


def find(args):
    words = str(args.get("query") or "").lower().split()

    def go():
        found = [t for t in store().tables()
                 if all(w in f"{t['name']} {t['description']} {' '.join(c['name'] for c in t['columns'])}".lower()
                        for w in words)]
        return {"tables": [_table_brief(t) for t in found],
                "note": "" if found else "Nothing in the Logbook matches. Say so rather than answering from memory."}
    return _run(go)


def get(args):
    def go():
        table = store().table(args.get("table_id"))
        out = _table_brief(table)
        out["versions"] = [report.version_brief(table, v) for v in table["versions"]]
        return out
    return _run(go)


def read_rows(args):
    which = str(args.get("version") or "verified")
    offset = max(0, whole_number(args.get("offset") or 0, "offset"))
    limit = min(500, max(1, whole_number(args.get("limit") or 200, "limit")))

    def go():
        table, version = store().version(args.get("table_id"), which)
        if not version:
            if which == "verified":
                return {"table_id": table["id"], "name": table["name"], "verified": False,
                        "message": f"{table['name']!r} has no verified version yet, so there are no checked facts "
                                   "to use. Tell Curtis; he can verify it on the Logbook page.",
                        "latest_version": report.version_brief(table, table["latest"]),
                        "url": site_url(f"/logbook/{table['id']}")}
            raise Invalid(f"{table['name']!r} has no versions yet.")
        rows = version["rows"]
        page = rows[offset:offset + limit]
        nxt = offset + limit if offset + limit < len(rows) else None
        return {"table_id": table["id"], "name": table["name"], "version_id": version["id"],
                "version": version["number"], "status": version["status"], "verified": version["status"] == "verified",
                "label": report.label(table, version),
                "columns": report.columns_out(version["columns"]),
                "total_rows": len(rows), "offset": offset, "next_offset": nxt,
                "rows": report.rows_out(version["columns"], page)}
    return _run(go)


def summarize(args):
    measure = args.get("columns")
    if measure is not None and not isinstance(measure, list):
        raise ToolError("columns should be a list of column names.")

    def go():
        table, version = store().version(args.get("table_id"), "verified")
        if not version:
            return {"table_id": table["id"], "name": table["name"], "verified": False,
                    "message": f"{table['name']!r} has no verified version yet, so Dieter won't add it up. Tell "
                               "Curtis; he can verify it on the Logbook page."}
        group_by = str(args.get("group_by") or "") or None
        out = report.summarize(version["columns"], version["rows"], group_by, measure)
        return dict({"table_id": table["id"], "name": table["name"], "version_id": version["id"],
                     "label": report.label(table, version), "verified": True}, **out)
    return _run(go)


def _assistant(args):
    who = str(args.get("assistant") or "").strip().lower()
    if who not in ASSISTANTS:
        raise ToolError("assistant should be chatgpt or claude (who is proposing this).")
    reason = " ".join(str(args.get("reason") or "").split())
    if not reason:
        raise ToolError("reason is required: one line on why (e.g. \"Added Hunt's leg 11 time from the results "
                        "email\").")
    return who, reason


def _proposed(proposal_id, summary, table_name):
    return {"proposal_id": proposal_id, "status": "pending",
            "message": f"Proposed for {table_name!r}: {report.summary_line(summary)}. Nothing has changed yet. "
                       "Curtis approves or discards it on the Logbook page (it also shows on his Today page).",
            "summary": summary, "review_url": site_url(f"/logbook/proposals/{proposal_id}")}


def propose_create(args):
    who, reason = _assistant(args)

    def go():
        s = store()
        name = " ".join(str(args.get("name") or "").split())
        if not name:
            raise Invalid("name is required.")
        if s.find_by_name(name):
            raise Invalid(f"There's already a Logbook table called {name!r}. Propose a change to it instead.")
        columns = check_columns(args.get("columns"))
        rows = args.get("rows") or []
        if len(rows) > MAX_ROWS:
            raise Invalid(f"A Logbook table holds at most {MAX_ROWS} rows.")
        clean = check_rows(columns, rows)
        summary = report.create_summary(columns, clean)
        payload = {"name": name, "description": str(args.get("description") or ""), "columns": columns,
                   "rows": rows}
        return _proposed(s.propose("create", payload, summary, who, reason), summary, name)
    return _run(go)


def propose_change(args):
    who, reason = _assistant(args)

    def go():
        s = store()
        table, base = s.version(args.get("table_id"), "latest")
        base_id = str(args.get("base_version_id") or "")
        if not base or base["id"] != base_id:
            raise Invalid(f"{table['name']!r} has changed since that version (newest is "
                          f"{base['id'] if base else 'none'}). Read it again with logbook_read_rows "
                          "version=\"latest\" and base the change on that.")
        change = {k: args.get(k) for k in ("add", "update", "remove", "add_columns") if args.get(k)}
        columns, rows, _ = s.changed(base, change)
        summary = report.change_summary(base["columns"], base["rows"], columns, rows)
        return _proposed(s.propose("change", change, summary, who, reason, table["id"], base_id), summary,
                         table["name"])
    return _run(go)


def list_proposals(args):
    status = str(args.get("status") or "pending")
    if status not in ("pending", "all"):
        raise ToolError("status is pending or all.")

    def go():
        out = []
        for p in store().proposals(status, args.get("table_id") or None):
            out.append({"proposal_id": p["id"], "kind": p["kind"], "table_id": p["table_id"],
                        "status": p["status"], "made_by": p["made_by"], "reason": p["reason"],
                        "summary": report.summary_line(p["summary"]), "note": p["note"],
                        "result_version_id": p["result_version_id"]})
        return {"proposals": out}
    return _run(go)


PAGES = [
    Page("logbook.home", "Logbook", "His checked tables, proposals waiting for his OK, and CSV upload.", "/logbook"),
    Page("logbook.table", "Logbook table", "One table with its versions (table_id from logbook_find).",
         "/logbook/{table_id}", {"table_id": "slug"}),
]

TABLE_ID = {"table_id": {"type": "string", "description": "From logbook_find."}}
WHO = {"assistant": {"type": "string", "enum": list(ASSISTANTS), "description": "Who is proposing: you."},
       "reason": {"type": "string", "description": "One line on why, e.g. \"Added Hunt's leg 11 time from the "
                                                   "results email\"."}}
ROWS = {"type": "array", "items": {"type": "object"},
        "description": "Rows as objects keyed by column name. Durations like \"30:35\" or \"1:02:45\", dates like "
                       "\"2026-11-06\", booleans true/false."}
COLUMNS = {"type": "array", "items": {"type": "object", "properties": {
    "name": {"type": "string", "description": "lower_case_with_underscores"},
    "type": {"type": "string", "enum": list(TYPES)},
    "label": {"type": "string", "description": "How it's shown, e.g. \"Distance (mi)\"."}},
    "required": ["name", "type"]}}

CHAT_NOTES = """Logbook: Curtis's checked tables (race results, records, lists he keeps). It's the source of fact:
when a question might be answered there, use logbook_find before answering from memory, and say when it isn't there.
For answers, summaries and images use only the verified version (logbook_read_rows version="verified") and quote its
label; let logbook_summarize do totals, averages, fastest and slowest instead of working them out. To change a table,
read it with version="latest", then logbook_propose_change with that version_id; nothing changes until Curtis
approves it on the Logbook page. You can't approve or verify anything."""


def tools():
    return ToolSet("logbook", [
        Tool("logbook_find", "Find Logbook tables",
             "Curtis's Logbook: checked tables he keeps (race results, records, lists). Check here before answering "
             "from memory. Returns each table's table_id, name, columns with types, latest version and latest "
             "verified version. query: words in the name, description or column names (empty lists them all).",
             find, {"query": {"type": "string"}}, read_only=True),
        Tool("logbook_get", "Look at a Logbook table",
             "One table: columns with types and every version (version_id, number, status draft/verified/superseded, "
             "who made it and why).", get, TABLE_ID, ("table_id",), read_only=True),
        Tool("logbook_read_rows", "Read a Logbook table's rows",
             "Rows of one version, a page at a time (offset, limit up to 500; keep going while next_offset isn't "
             "null). version: \"verified\" (default; use it for answers, summaries and images, and quote the label), "
             "\"latest\" (to propose a change: use its version_id as base_version_id), or a version_id. With no "
             "verified version it says so and returns no rows. Each row has a row_id for changes.",
             read_rows, dict(TABLE_ID, version={"type": "string"}, offset={"type": "integer"},
                             limit={"type": "integer"}), ("table_id",), read_only=True),
        Tool("logbook_summarize", "Add up a Logbook table",
             "Dieter does the arithmetic on the verified version: rows, total, average, min and max (with row_id) of "
             "each number/duration column, overall and per value of group_by (e.g. group_by=\"runner\"). Use these "
             "numbers as given rather than working them out. columns: only these columns.",
             summarize, dict(TABLE_ID, group_by={"type": "string"},
                             columns={"type": "array", "items": {"type": "string"}}), ("table_id",), read_only=True),
        Tool("logbook_propose_create", "Propose a new Logbook table",
             "A new table with typed columns and rows. Every value is checked against its column type; if anything "
             "doesn't fit, nothing is saved and every problem is listed. It becomes a proposal Curtis approves on the "
             f"Logbook page. At most {MAX_ROWS} rows.",
             propose_create, dict(WHO, name={"type": "string"}, description={"type": "string"}, columns=COLUMNS,
                                  rows=ROWS), ("name", "columns", "rows", "assistant", "reason")),
        Tool("logbook_propose_change", "Propose a change to a Logbook table",
             "Change rows of a table: add (new rows), update ([{row_id, values: {column: new value}}]), remove "
             "([row_id]), add_columns ([{name, type, label}]; existing columns can't be renamed or removed). "
             "base_version_id must be the newest version (from logbook_read_rows version=\"latest\"); if the table "
             "changed since, it's refused. Checked all or nothing, then waits for Curtis to approve it.",
             propose_change, dict(TABLE_ID, **WHO, base_version_id={"type": "string"}, add=ROWS,
                                  update={"type": "array", "items": {"type": "object"}},
                                  remove={"type": "array", "items": {"type": "string"}}, add_columns=COLUMNS),
             ("table_id", "base_version_id", "assistant", "reason")),
        Tool("logbook_list_proposals", "Logbook proposals",
             "Proposals waiting for Curtis (status=\"pending\", default) or all recent ones with what happened: "
             "approved (result_version_id), discarded, or stale (the table changed first; note says why).",
             list_proposals, dict(status={"type": "string", "enum": ["pending", "all"]},
                                  table_id={"type": "string"}), read_only=True),
    ])
