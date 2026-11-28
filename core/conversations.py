"""Ask Dieter conversations: what Curtis asked, what Dieter answered, on which page, and which part of Dieter
answered (Claude's chat, or one of the older handlers in api.py such as the shoe log). Kept so Claude can see why
Dieter got something wrong (Studio's studio_list_conversations tool), and as data for later research on Dieter's
memory and agents.

Shared storage (any app's chat can log here); additive table, nothing is ever rewritten.
"""

import json
from datetime import datetime

SCHEMA = """
    CREATE TABLE IF NOT EXISTS dieter_conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        at TEXT NOT NULL,
        page_url TEXT DEFAULT '',
        page_title TEXT DEFAULT '',
        said TEXT NOT NULL,
        reply TEXT DEFAULT '',
        answered_by TEXT DEFAULT '',
        actions_json TEXT DEFAULT '[]',
        error TEXT DEFAULT ''
    )
"""
MAX_TEXT = 4000  # a long reply is cut here; enough to see what went wrong


def _conn(db):
    conn = db.connect()
    conn.execute(SCHEMA)
    return conn


def add(db, *, user_id, said, reply="", answered_by="", page_url="", page_title="", actions=(), error=""):
    """Save one exchange. Never raises: logging must not break Ask Dieter."""
    try:
        conn = _conn(db)
        conn.execute(
            "INSERT INTO dieter_conversations (user_id, at, page_url, page_title, said, reply, answered_by, "
            "actions_json, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, datetime.utcnow().isoformat(timespec="seconds"), (page_url or "")[:300],
             (page_title or "")[:200], (said or "")[:MAX_TEXT], (reply or "")[:MAX_TEXT], (answered_by or "")[:60],
             json.dumps(list(actions or []))[:MAX_TEXT], (error or "")[:500]))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: couldn't save an Ask Dieter conversation: {exc!r}")


def recent(db, *, user_id=None, limit=20, contains="", page=""):
    """Newest first. contains / page filter on what he said or the reply, and on the page address."""
    sql, args = "SELECT * FROM dieter_conversations WHERE 1 = 1", []
    if user_id is not None:
        sql += " AND user_id = ?"
        args.append(user_id)
    if contains:
        sql += " AND (said LIKE ? OR reply LIKE ?)"
        args += [f"%{contains}%", f"%{contains}%"]
    if page:
        sql += " AND page_url LIKE ?"
        args.append(f"%{page}%")
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 20), 100)))
    rows = _conn(db).execute(sql, args).fetchall()
    out = []
    for row in rows:
        try:
            actions = json.loads(row["actions_json"] or "[]")
        except ValueError:
            actions = []
        out.append({"id": row["id"], "at": row["at"], "page": row["page_url"], "page_title": row["page_title"],
                    "said": row["said"], "reply": row["reply"], "answered_by": row["answered_by"],
                    "actions": actions, "error": row["error"]})
    return out
