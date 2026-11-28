"""The Logbook store: tables, their versions, and proposals waiting for the owner's OK.

Every change makes a new version (rows and columns are stored whole on each version; tables are small). A version
is "draft", "verified" or "superseded". Marking one verified supersedes the one verified before; later changes make
drafts on top, and "latest verified" stays the checked one until the owner verifies again.

Rows carry a stable row_id ("r1", "r2", ...) that survives later versions, so a change can name the row it edits.
Everything is scoped to the signed-in user (get_current_user_id). Only owner-facing pages call approve() and
verify(); the connector has no tool for either.
"""

import json
import secrets
import time

from database import get_current_user_id

from .types import Invalid, check_columns, check_rows, fail, row_problems

MAX_ROWS = 5000
MAX_TABLES = 200
WHO = {"curtis": "you", "site": "you", "chatgpt": "ChatGPT", "claude": "Claude"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS logbook_tables (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS logbook_versions (
    id TEXT PRIMARY KEY,
    table_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    status TEXT DEFAULT 'draft',
    columns_json TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    next_row INTEGER DEFAULT 1,
    made_by TEXT DEFAULT 'site',
    reason TEXT DEFAULT '',
    created_at REAL,
    verified_at REAL
);
CREATE INDEX IF NOT EXISTS logbook_versions_table ON logbook_versions (table_id, number);
CREATE TABLE IF NOT EXISTS logbook_proposals (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    table_id TEXT DEFAULT '',
    kind TEXT NOT NULL,
    base_version_id TEXT DEFAULT '',
    payload_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    made_by TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    note TEXT DEFAULT '',
    result_version_id TEXT DEFAULT '',
    created_at REAL,
    decided_at REAL
);
"""


def _id(prefix):
    return f"{prefix}-{secrets.token_hex(6)}"


def clean_name(value, what="name", limit=80):
    text = " ".join(str(value or "").split())[:limit]
    if not text:
        raise Invalid(f"The table needs a {what}.")
    return text


def who(made_by):
    return WHO.get(made_by, made_by or "someone")


class LogbookStore:
    def __init__(self, db):
        self.db, self._ready = db, False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            conn.executescript(SCHEMA)
            conn.commit()
            self._ready = True
        return conn

    def _user(self):
        user = get_current_user_id()
        if not user:
            raise Invalid("Sign in to Dieter first.")
        return user

    # ---------- reading ----------

    def tables(self):
        rows = self._conn().execute(
            "SELECT * FROM logbook_tables WHERE user_id = ? ORDER BY updated_at DESC", [self._user()]).fetchall()
        return [self._table(dict(r)) for r in rows]

    def table(self, table_id):
        row = self._conn().execute("SELECT * FROM logbook_tables WHERE id = ? AND user_id = ?",
                                   [str(table_id or ""), self._user()]).fetchone()
        if not row:
            raise Invalid(f"There's no Logbook table {table_id!r}. logbook_find lists them.")
        return self._table(dict(row))

    def find_by_name(self, name):
        name = " ".join(str(name or "").split()).lower()
        return next((t for t in self.tables() if t["name"].lower() == name), None)

    def _table(self, row):
        versions = self.versions(row["id"], _user=row["user_id"])
        latest = versions[0] if versions else None
        verified = next((v for v in versions if v["status"] == "verified"), None)
        row["latest"], row["verified"], row["versions"] = latest, verified, versions
        row["columns"] = latest["columns"] if latest else []
        return row

    def versions(self, table_id, _user=None):
        rows = self._conn().execute(
            "SELECT id, table_id, number, status, columns_json, made_by, reason, created_at, verified_at, "
            "json_array_length(rows_json) AS row_count FROM logbook_versions WHERE table_id = ? AND user_id = ? "
            "ORDER BY number DESC", [table_id, _user or self._user()]).fetchall()
        out = []
        for r in rows:
            v = dict(r)
            v["columns"] = json.loads(v.pop("columns_json"))
            out.append(v)
        return out

    def version(self, table_id, version_id):
        """A version with its rows. version_id may be "latest" or "verified"; None when there's no such version."""
        table = self.table(table_id)
        if version_id in (None, "", "latest"):
            pick = table["latest"]
        elif version_id == "verified":
            pick = table["verified"]
        else:
            pick = next((v for v in table["versions"] if v["id"] == version_id), None)
            if not pick:
                raise Invalid(f"Table {table['name']!r} has no version {version_id!r}.")
        if not pick:
            return table, None
        row = self._conn().execute("SELECT rows_json, next_row FROM logbook_versions WHERE id = ?",
                                   [pick["id"]]).fetchone()
        version = dict(pick, rows=json.loads(row["rows_json"]), next_row=row["next_row"])
        return table, version

    # ---------- writing (the owner, or an approved proposal) ----------

    def create(self, name, columns, rows, description="", made_by="site", reason=""):
        """A new table with version 1 as a draft. rows are raw values; they're checked against the columns."""
        user = self._user()
        name = clean_name(name)
        columns = check_columns(columns)
        if len(rows) > MAX_ROWS:
            raise Invalid(f"A Logbook table holds at most {MAX_ROWS} rows; this has {len(rows)}.")
        clean = check_rows(columns, rows)
        if self.find_by_name(name):
            raise Invalid(f"There's already a Logbook table called {name!r}.")
        count = self._conn().execute("SELECT COUNT(*) FROM logbook_tables WHERE user_id = ?", [user]).fetchone()[0]
        if count >= MAX_TABLES:
            raise Invalid(f"The Logbook holds at most {MAX_TABLES} tables.")
        table_id, now = _id("tbl"), time.time()
        self._conn().execute(
            "INSERT INTO logbook_tables (id, user_id, name, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", [table_id, user, name, " ".join(str(description or "").split())[:300], now,
                                          now])
        numbered = [dict({"row_id": f"r{i}"}, **r) for i, r in enumerate(clean, 1)]
        version_id = self._add_version(table_id, user, 1, columns, numbered, len(numbered) + 1, made_by, reason)
        self.db._commit()
        return table_id, version_id

    def _add_version(self, table_id, user, number, columns, rows, next_row, made_by, reason):
        version_id, now = _id("ver"), time.time()
        conn = self._conn()
        # An older draft nobody verified is replaced by this one.
        conn.execute("UPDATE logbook_versions SET status = 'superseded' WHERE table_id = ? AND status = 'draft'",
                     [table_id])
        conn.execute(
            "INSERT INTO logbook_versions (id, table_id, user_id, number, status, columns_json, rows_json, next_row, "
            "made_by, reason, created_at) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?)",
            [version_id, table_id, user, number, json.dumps(columns), json.dumps(rows), next_row, made_by,
             " ".join(str(reason or "").split())[:300], now])
        conn.execute("UPDATE logbook_tables SET updated_at = ? WHERE id = ?", [now, table_id])
        return version_id

    def verify(self, table_id, version_id):
        """The owner checked this version: it becomes the verified one (the previous verified one is superseded)."""
        table = self.table(table_id)
        version = next((v for v in table["versions"] if v["id"] == version_id), None)
        if not version:
            raise Invalid("That version isn't in this table.")
        if version["status"] == "verified":
            return
        if table["latest"]["id"] != version_id:
            raise Invalid("Only the newest version can be marked verified.")
        conn, now = self._conn(), time.time()
        conn.execute("UPDATE logbook_versions SET status = 'superseded' WHERE table_id = ? AND status = 'verified'",
                     [table_id])
        conn.execute("UPDATE logbook_versions SET status = 'verified', verified_at = ? WHERE id = ?",
                     [now, version_id])
        conn.execute("UPDATE logbook_tables SET updated_at = ? WHERE id = ?", [now, table_id])
        self.db._commit()

    def apply_change(self, table_id, base_version_id, change, made_by, reason):
        """A new draft version from a change {add, update, remove, add_columns}. Refused if the table moved on."""
        table, base = self.version(table_id, "latest")
        if not base or base["id"] != base_version_id:
            raise Invalid(f"{table['name']!r} has changed since version {base_version_id!r} was read. Read it again "
                          "and base the change on the newest version.")
        columns, rows, next_row = self.changed(base, change)
        version_id = self._add_version(table_id, self._user(), base["number"] + 1, columns, rows, next_row,
                                       made_by, reason)
        self.db._commit()
        return version_id

    @staticmethod
    def changed(base, change):
        """(columns, rows, next_row) after a change, checked all or nothing. change: {add: [rows], update:
        [{row_id, values}], remove: [row_id], add_columns: [{name, type, label}]}."""
        columns = list(base["columns"])
        new_columns = change.get("add_columns") or []
        if new_columns:
            taken = {c["name"] for c in columns}
            clash = [c.get("name") for c in new_columns if isinstance(c, dict) and c.get("name") in taken]
            if clash:
                raise Invalid(f"Column {clash[0]!r} is already in the table.")
            columns = check_columns(columns + list(new_columns))
        by_id = {r["row_id"]: dict(r) for r in base["rows"]}
        for r in by_id.values():
            for c in columns:
                r.setdefault(c["name"], None)
        problems = []
        remove = [str(x) for x in change.get("remove") or []]
        for row_id in remove:
            if row_id not in by_id:
                problems.append(f"remove: no row {row_id!r}")
        updates = change.get("update") or []
        checked_updates = []
        for i, item in enumerate(updates, 1):
            row_id = str((item or {}).get("row_id") or "") if isinstance(item, dict) else ""
            values = item.get("values") if isinstance(item, dict) else None
            if row_id not in by_id:
                problems.append(f"update {i}: no row {row_id!r}")
                continue
            if row_id in remove:
                problems.append(f"update {i}: row {row_id!r} is also being removed")
                continue
            if not isinstance(values, dict) or not values:
                problems.append(f"update {i}: values should be an object of the columns that change")
                continue
            merged = dict(by_id[row_id], **values)
            merged.pop("row_id", None)
            checked_updates.append((row_id, merged, i))
        adds = change.get("add") or []
        for row_id, merged, i in checked_updates:
            clean, found = row_problems(columns, [merged], label=lambda _, i=i, row_id=row_id: f"update {i} ({row_id})")
            problems.extend(found)
            if clean and not found:
                by_id[row_id] = dict({"row_id": row_id}, **clean[0])
        added, found = row_problems(columns, adds, label=lambda i: f"add row {i}")
        problems.extend(found)
        if problems:
            fail(problems)
        rows = [by_id[r["row_id"]] for r in base["rows"] if r["row_id"] not in remove]
        next_row = base.get("next_row") or len(base["rows"]) + 1
        for r in added:
            rows.append(dict({"row_id": f"r{next_row}"}, **r))
            next_row += 1
        if len(rows) > MAX_ROWS:
            raise Invalid(f"A Logbook table holds at most {MAX_ROWS} rows; this change makes {len(rows)}.")
        if not (remove or checked_updates or added or new_columns):
            raise Invalid("That change doesn't change anything.")
        return columns, rows, next_row

    # ---------- proposals ----------

    def propose(self, kind, payload, summary, made_by, reason, table_id="", base_version_id=""):
        proposal_id = _id("prop")
        self._conn().execute(
            "INSERT INTO logbook_proposals (id, user_id, table_id, kind, base_version_id, payload_json, summary_json, "
            "made_by, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [proposal_id, self._user(), table_id, kind, base_version_id, json.dumps(payload), json.dumps(summary),
             made_by, " ".join(str(reason or "").split())[:300], time.time()])
        self.db._commit()
        return proposal_id

    def proposals(self, status="pending", table_id=None, limit=50):
        sql, args = "SELECT * FROM logbook_proposals WHERE user_id = ?", [self._user()]
        if status != "all":
            sql += " AND status = ?"
            args.append(status)
        if table_id:
            sql += " AND table_id = ?"
            args.append(table_id)
        rows = self._conn().execute(sql + " ORDER BY created_at DESC LIMIT ?", args + [limit]).fetchall()
        return [self._proposal(dict(r)) for r in rows]

    def proposal(self, proposal_id):
        row = self._conn().execute("SELECT * FROM logbook_proposals WHERE id = ? AND user_id = ?",
                                   [str(proposal_id or ""), self._user()]).fetchone()
        if not row:
            raise Invalid("That proposal isn't here any more.")
        return self._proposal(dict(row))

    def _proposal(self, row):
        row["payload"] = json.loads(row.pop("payload_json"))
        row["summary"] = json.loads(row.pop("summary_json"))
        name = row["payload"].get("name") if row["kind"] == "create" else None
        if not name and row["table_id"]:
            found = self._conn().execute("SELECT name FROM logbook_tables WHERE id = ?", [row["table_id"]]).fetchone()
            name = found["name"] if found else None
        row["table_name"] = name or "a table that's gone"
        return row

    def _decide(self, proposal_id, status, note="", result_version_id=""):
        self._conn().execute(
            "UPDATE logbook_proposals SET status = ?, note = ?, result_version_id = ?, decided_at = ? "
            "WHERE id = ? AND user_id = ?",
            [status, note, result_version_id, time.time(), proposal_id, self._user()])
        self.db._commit()

    def approve(self, proposal_id):
        """The owner said yes: apply it as a new draft version. A stale proposal is marked stale instead."""
        prop = self.proposal(proposal_id)
        if prop["status"] != "pending":
            raise Invalid(f"That proposal was already {prop['status']}.")
        p = prop["payload"]
        try:
            if prop["kind"] == "create":
                table_id, version_id = self.create(p["name"], p["columns"], p["rows"], p.get("description", ""),
                                                   prop["made_by"], prop["reason"])
            else:
                table_id = prop["table_id"]
                version_id = self.apply_change(table_id, prop["base_version_id"], p, prop["made_by"], prop["reason"])
        except Invalid as exc:
            self._decide(proposal_id, "stale", str(exc))
            raise
        self._decide(proposal_id, "approved", result_version_id=version_id)
        if prop["kind"] == "create":
            self._conn().execute("UPDATE logbook_proposals SET table_id = ? WHERE id = ?", [table_id, proposal_id])
            self.db._commit()
        return table_id, version_id

    def today(self):
        """For the Today page: proposals waiting and tables never verified (None when there's nothing to do)."""
        pending = self.proposals("pending", limit=20)
        unverified = [t for t in self.tables() if t["latest"] and not t["verified"]]
        if not pending and not unverified:
            return None
        return {"pending": len(pending), "first": pending[0] if pending else None, "unverified": len(unverified)}

    def discard(self, proposal_id):
        prop = self.proposal(proposal_id)
        if prop["status"] != "pending":
            raise Invalid(f"That proposal was already {prop['status']}.")
        self._decide(proposal_id, "discarded")


