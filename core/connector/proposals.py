"""Proposals: changes Dieter suggests that wait for the person's OK (recipe edits first; any app can use them).

A tool that wants to change something important doesn't change it. It creates a proposal: a card the person reviews
(what changes, as old and new lines, and what else it affects) plus the data needed to apply it. The card is shown by
the shared script core/ui/static/dieter-actions.js, whose Save and Discard buttons call the app's own endpoints.

Card fields: id, kind, title, reason, diffs [{label, lines: [{op: "same"|"removed"|"added"|"gap", text}]}],
effects [str], note, save_url, discard_url, open_url, status ("pending" | "saved" | "discarded" | "expired").

Confirming must be deliberate: a tap on Save, or saying so plainly (said_save). A vague "yeah" or "ok" never counts.
"""

import json
import re
import secrets
import time
from difflib import SequenceMatcher

EXPIRES_SECONDS = 30 * 60
SAVE_WORDS = re.compile(r"\b(save|apply|keep|make)\b[^.?!]{0,40}?\b(recipe|change|changes|edit|edits|it|that|this)\b", re.I)
NOT_WORDS = re.compile(r"\b(don'?t|do not|not|no|cancel|discard|never|undo|wait|hold)\b", re.I)


def said_save(text):
    """True when the person plainly asked to save the change ("save the recipe change", "yes, save it")."""
    text = str(text or "")
    return bool(SAVE_WORDS.search(text)) and not NOT_WORDS.search(text)


def line_diff(before, after, context=1):
    """Old and new text as lines: removed, added, `context` unchanged lines around each change, and "gap" markers
    standing for the unchanged lines in between."""
    old, new = (before or "").splitlines(), (after or "").splitlines()
    ops = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            ops += [{"op": "same", "text": line} for line in old[i1:i2]]
        else:
            ops += [{"op": "removed", "text": line} for line in old[i1:i2]]
            ops += [{"op": "added", "text": line} for line in new[j1:j2]]
    keep = set()
    for i, op in enumerate(ops):
        if op["op"] != "same":
            keep.update(range(i - context, i + context + 1))
    lines, skipped = [], 0
    for i, op in enumerate(ops):
        if i not in keep:
            skipped += 1
            continue
        if skipped:
            lines.append({"op": "gap", "text": f"{skipped} unchanged line{'s' if skipped > 1 else ''}"})
            skipped = 0
        lines.append(op)
    return lines


class ProposalStore:
    """Proposals in one database table (each app's own database). owner: whose they are, e.g. "plan:3"."""

    def __init__(self, db, table):
        if not re.fullmatch(r"[a-z_]+", table):
            raise ValueError("table name")
        self.db, self.table, self._ready = db, table, False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {self.table} (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, card_json TEXT NOT NULL,
                payload_json TEXT NOT NULL, status TEXT DEFAULT 'pending', created_at REAL, decided_at REAL)""")
            conn.commit()
            self._ready = True
        return conn

    def create(self, owner, kind, card, payload):
        proposal_id = secrets.token_hex(6)
        card = dict(card, id=proposal_id, kind=kind, status="pending")
        card = {k: (v.format(id=proposal_id) if isinstance(v, str) and k.endswith("_url") else v) for k, v in card.items()}
        conn = self._conn()
        conn.execute(f"DELETE FROM {self.table} WHERE created_at < ?", (time.time() - 7 * 24 * 3600,))
        conn.execute(f"INSERT INTO {self.table} (id, owner, kind, card_json, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (proposal_id, owner, kind, json.dumps(card), json.dumps(payload), time.time()))
        self.db._commit()
        return card

    def get(self, owner, proposal_id):
        """(card, payload) with the status brought up to date, or (None, None)."""
        row = self._conn().execute(f"SELECT * FROM {self.table} WHERE id = ? AND owner = ?",
                                   (str(proposal_id or ""), owner)).fetchone()
        if not row:
            return None, None
        status = row["status"]
        if status == "pending" and time.time() - row["created_at"] > EXPIRES_SECONDS:
            status = "expired"
        return dict(json.loads(row["card_json"]), status=status), json.loads(row["payload_json"])

    def decide(self, owner, proposal_id, status):
        self._conn().execute(f"UPDATE {self.table} SET status = ?, decided_at = ? WHERE id = ? AND owner = ?",
                             (status, time.time(), proposal_id, owner))
        self.db._commit()

    def pending(self, owner, kind=None):
        rows = self._conn().execute(
            f"SELECT id FROM {self.table} WHERE owner = ? AND status = 'pending' AND created_at > ? "
            + ("AND kind = ? " if kind else "") + "ORDER BY created_at",
            (owner, time.time() - EXPIRES_SECONDS) + ((kind,) if kind else ())).fetchall()
        return [self.get(owner, row["id"]) for row in rows]
