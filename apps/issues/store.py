"""Storage for Dieter Studio: problem reports and change requests.

Reports live in app_feedback_reports (database.Database.add_app_feedback_report writes them; other apps
post there too). This store reads and updates them for the Studio pages.

The old Codex pipeline kept its attempts in app_feedback_codex_runs. That table is now a read-only archive:
archive_codex_runs() copies each report's runs onto the report (codex_history_json) once, so the history
shows on the report page without any Codex code.
"""

import json

STATUSES = ("open", "in_progress", "ready_for_review", "done")
STATUS_LABELS = {
    "open": "New",
    "triaged": "New",
    "sent": "With Claude",  # sent as a GitHub issue, before Studio stopped doing that
    "in_progress": "In progress",
    "ready_for_review": "Ready to check",
    "done": "Done",
}
ACTIVE = ("open", "triaged", "sent", "in_progress", "ready_for_review")


def _loads(text, default):
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


class StudioStore:
    def __init__(self, db):
        self.db = db

    def _conn(self):
        return self.db.connect()

    def _commit(self):
        self.db._commit()

    # ------------------------------------------------------------ reading

    def reports(self, which="active"):
        sql = "SELECT * FROM app_feedback_reports"
        params = ()
        if which == "active":
            sql += f" WHERE COALESCE(status, 'open') IN ({','.join('?' * len(ACTIVE))})"
            params = ACTIVE
        elif which == "done":
            sql += " WHERE status = 'done'"
        sql += " ORDER BY created_at DESC, id DESC LIMIT 200"
        return [self._shape(row) for row in self._conn().execute(sql, params).fetchall()]

    def report(self, report_id):
        row = self._conn().execute("SELECT * FROM app_feedback_reports WHERE id = ?", (report_id,)).fetchone()
        return self._shape(row) if row else None

    def counts(self):
        rows = self._conn().execute("SELECT COALESCE(status, 'open') AS status, count(*) AS n FROM app_feedback_reports GROUP BY 1")
        counts = {"active": 0, "done": 0}
        for row in rows:
            counts["done" if row["status"] == "done" else "active"] += row["n"]
        return counts

    def _shape(self, row):
        report = dict(row)
        status = report.get("status") or "open"
        report["status"] = status
        report["status_label"] = STATUS_LABELS.get(status, status.replace("_", " ").title())
        report["history"] = _loads(report.get("audit_action_history_json"), [])
        report["codex_history"] = _loads(report.get("codex_history_json"), [])
        report["old_plan"] = report.get("audit_plan") or ""
        report["created"] = str(report.get("created_at") or "")[:10]
        return report

    # ------------------------------------------------------------ changes

    def set_status(self, report_id, status, note=""):
        if status not in STATUSES:
            raise ValueError(status)
        self._conn().execute("UPDATE app_feedback_reports SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                             (status, report_id))
        self._commit()
        self.db.append_app_feedback_report_action(report_id, "status", note or f"Marked {STATUS_LABELS[status]}.")

    def delete(self, report_id):
        self._conn().execute("DELETE FROM app_feedback_reports WHERE id = ?", (report_id,))
        self._commit()

    # ------------------------------------------------------------ Codex archive

    def archive_codex_runs(self):
        """Copy each report's old Codex runs onto the report, once. Returns how many reports were filled in."""
        conn = self._conn()
        has_runs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_feedback_codex_runs'").fetchone()
        if not has_runs:
            return 0
        cols = {r[1] for r in conn.execute("PRAGMA table_info(app_feedback_codex_runs)")}
        want = [c for c in ("id", "status", "plan", "result_note", "worker_name", "created_at", "started_at", "finished_at")
                if c in cols]
        pending = conn.execute(
            """SELECT DISTINCT r.report_id FROM app_feedback_codex_runs r
               JOIN app_feedback_reports f ON f.id = r.report_id
               WHERE COALESCE(f.codex_history_json, '') = ''""").fetchall()
        for (report_id,) in pending:
            runs = conn.execute(f"SELECT {','.join(want)} FROM app_feedback_codex_runs WHERE report_id = ? ORDER BY id",
                                (report_id,)).fetchall()
            conn.execute("UPDATE app_feedback_reports SET codex_history_json = ? WHERE id = ?",
                         (json.dumps([dict(r) for r in runs], ensure_ascii=False), report_id))
        if pending:
            self._commit()
        return len(pending)
