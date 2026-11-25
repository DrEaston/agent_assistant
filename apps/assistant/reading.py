"""Reading: his shelf, suggestions he keeps or skips, and a bookstore checklist by author (docs/reading_plan.md).

Three kinds of state, kept apart on purpose:
- each book persists: title, author, ★ high priority, status (want -> found -> owned -> reading -> read, then
  maybe passed on; "not for me" for suggestions he turned down, so they're never suggested again), and once read a
  rating (1-5) and a line of thoughts;
- suggestions from Claude, ChatGPT or Ask Dieter wait for his OK: keep (onto the shelf as want) or skip (not for
  me). Assistants never put books straight on his shelf from a suggestion;
- a bookstore visit is temporary: the author shelves you've already checked on this trip. Checking a shelf doesn't
  mean you found anything; "Done" clears the checks for the next trip.

Shelves are by author, so books group by author, sorted by last name ("VanderMeer, Jeff" under V).
"""

import re
from datetime import date

from database import get_current_user_id


def _today():
    return date.today().isoformat()

STATUSES = ("want", "found", "owned", "reading", "read", "passed_on", "not_for_me")
STATUS_LABELS = {"want": "Looking for it", "found": "Found", "owned": "Own it", "reading": "Reading", "read": "Read",
                 "passed_on": "Passed on", "not_for_me": "Not for me"}
SHELF = STATUSES[:6]  # what the Reading page shows by default (not "not for me")
ASSISTANTS = ("claude", "chatgpt", "dieter")


def author_sort(author):
    """"Jeff VanderMeer" or "VanderMeer, Jeff" -> "vandermeer, jeff" (shelf order)."""
    name = " ".join(str(author or "").split())
    if "," in name:
        return name.lower()
    parts = name.split(" ")
    return (f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) > 1 else name).lower()


def display_author(author):
    """As written in the list: "Last, First" stays; "First Last" stays."""
    return " ".join(str(author or "").split())


class ReadingStore:
    def __init__(self, db):
        self.db, self._ready = db, False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            conn.execute("""CREATE TABLE IF NOT EXISTS reading_books (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT NOT NULL, author TEXT DEFAULT '',
                author_sort TEXT DEFAULT '', high_priority INTEGER DEFAULT 0, status TEXT DEFAULT 'want',
                notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS reading_shelf_checks (
                user_id INTEGER, author_sort TEXT, checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, author_sort))""")
            have = {r[1] for r in conn.execute("PRAGMA table_info(reading_books)").fetchall()}
            for column, kind in (("rating", "INTEGER"), ("thoughts", "TEXT DEFAULT ''"), ("started_at", "TEXT DEFAULT ''"),
                                 ("finished_at", "TEXT DEFAULT ''"), ("source", "TEXT DEFAULT ''")):
                if column not in have:  # additive: new columns with defaults
                    conn.execute(f"ALTER TABLE reading_books ADD COLUMN {column} {kind}")
            conn.execute("""CREATE TABLE IF NOT EXISTS reading_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT NOT NULL, author TEXT DEFAULT '',
                reason TEXT DEFAULT '', asked_for TEXT DEFAULT '', made_by TEXT DEFAULT '',
                status TEXT DEFAULT 'waiting', book_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                decided_at TEXT DEFAULT '')""")
            conn.commit()
            self._ready = True
        return conn

    def _user(self):
        return get_current_user_id() or 0

    def books(self, status=None):
        sql, params = "SELECT * FROM reading_books WHERE user_id = ?", [self._user()]
        if status:
            sql += " AND status = ?"
            params.append(status)
        rows = [dict(r) for r in self._conn().execute(sql + " ORDER BY author_sort, title", params).fetchall()]
        for book in rows:
            book["status_label"] = STATUS_LABELS.get(book["status"], book["status"])
        return rows

    def book(self, book_id):
        row = self._conn().execute("SELECT * FROM reading_books WHERE id = ? AND user_id = ?",
                                   (int(book_id), self._user())).fetchone()
        return dict(row) if row else None

    def find(self, text):
        """Books whose title (or author) matches the words given, best first."""
        words = [w for w in re.split(r"\W+", str(text or "").lower()) if w and w not in ("the", "a", "an", "by")]
        if not words:
            return []
        scored = []
        for book in self.books():
            hay = f"{book['title']} {book['author']}".lower()
            hits = sum(1 for w in words if w in hay)
            if hits == len(words) or (hits and words[0] in book["title"].lower()):
                scored.append((hits, book))
        return [b for _, b in sorted(scored, key=lambda x: -x[0])]

    def add(self, title, author="", high_priority=False, status="want", notes=""):
        title = " ".join(str(title or "").split())[:200]
        if not title:
            raise ValueError("A book needs a title.")
        status = status if status in STATUSES else "want"
        cur = self._conn().execute(
            "INSERT INTO reading_books (user_id, title, author, author_sort, high_priority, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self._user(), title, display_author(author)[:120], author_sort(author), 1 if high_priority else 0, status,
             str(notes or "")[:500]))
        self.db._commit()
        return cur.lastrowid

    def update(self, book_id, **fields):
        allowed = {k: v for k, v in fields.items()
                   if k in ("title", "author", "high_priority", "status", "notes", "rating", "thoughts") and v is not None}
        if "status" in allowed and allowed["status"] not in STATUSES:
            raise ValueError(f"Status is one of: {', '.join(STATUSES)}.")
        if "rating" in allowed:
            try:
                rating = int(allowed["rating"])
            except (TypeError, ValueError):
                raise ValueError("Rating is 1 to 5.") from None
            if not 0 <= rating <= 5:
                raise ValueError("Rating is 1 to 5.")
            allowed["rating"] = rating or None
        if "thoughts" in allowed:
            allowed["thoughts"] = str(allowed["thoughts"]).strip()[:1000]
        status = allowed.get("status")
        if status == "reading":
            allowed["started_at"] = _today()
        elif status == "read":
            allowed["finished_at"] = _today()
        if "author" in allowed:
            allowed["author_sort"] = author_sort(allowed["author"])
        if "high_priority" in allowed:
            allowed["high_priority"] = 1 if allowed["high_priority"] else 0
        if not allowed:
            return
        sets = ", ".join(f"{k} = ?" for k in allowed)
        self._conn().execute(f"UPDATE reading_books SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                             list(allowed.values()) + [int(book_id), self._user()])
        self.db._commit()

    def delete(self, book_id):
        self._conn().execute("DELETE FROM reading_books WHERE id = ? AND user_id = ?", (int(book_id), self._user()))
        self.db._commit()

    def reading_now(self):
        return [b for b in self.books("reading")]

    def taste(self):
        """What assistants read before suggesting: rated books (best first), favourite authors, and turned down."""
        books = self.books()
        rated = sorted([b for b in books if b.get("rating")], key=lambda b: (-b["rating"], b["title"]))
        authors = {}
        for b in rated:
            authors.setdefault(b["author"], []).append(b["rating"])
        loved = sorted((a for a, r in authors.items() if a and sum(r) / len(r) >= 4), key=lambda a: a.lower())
        return {"rated": [{"title": b["title"], "author": b["author"], "rating": b["rating"],
                           "thoughts": b.get("thoughts") or ""} for b in rated],
                "loved_authors": loved,
                "not_for_me": [{"title": b["title"], "author": b["author"]} for b in books if b["status"] == "not_for_me"],
                "on_shelf": [{"title": b["title"], "author": b["author"], "status": b["status"]}
                             for b in books if b["status"] != "not_for_me"]}

    # ------------------------------------------------------------ suggestions (wait for his OK)

    def _already(self, title, author):
        """A book with this title (and author, if given) already on the shelf or turned down, or already suggested."""
        key = " ".join(str(title or "").lower().split())
        sort = author_sort(author) if author else ""
        for b in self.books():
            if b["title"].lower() == key and (not sort or not b["author_sort"] or b["author_sort"] == sort):
                return b["status"]
        row = self._conn().execute("SELECT 1 FROM reading_suggestions WHERE user_id = ? AND lower(title) = ? "
                                   "AND status = 'waiting'", (self._user(), key)).fetchone()
        return "waiting" if row else None

    def suggest(self, books, asked_for="", made_by=""):
        """Suggestions wait for his OK. Returns {"added": [...], "skipped": [{title, why}]}."""
        made_by = made_by if made_by in ASSISTANTS else "dieter"
        added, skipped = [], []
        for raw in list(books or [])[:10]:
            raw = raw if isinstance(raw, dict) else {"title": raw}
            title = " ".join(str(raw.get("title") or "").split())[:200]
            if not title:
                continue
            author = display_author(raw.get("author"))[:120]
            already = self._already(title, author)
            if already:
                skipped.append({"title": title, "why": "already suggested" if already == "waiting"
                                else f"already on his shelf ({STATUS_LABELS.get(already, already)})"})
                continue
            cur = self._conn().execute(
                "INSERT INTO reading_suggestions (user_id, title, author, reason, asked_for, made_by) VALUES (?, ?, ?, ?, ?, ?)",
                (self._user(), title, author, str(raw.get("reason") or "").strip()[:300],
                 str(asked_for or "").strip()[:300], made_by))
            added.append({"id": cur.lastrowid, "title": title, "author": author})
        self.db._commit()
        return {"added": added, "skipped": skipped}

    def suggestions(self, status="waiting"):
        rows = self._conn().execute("SELECT * FROM reading_suggestions WHERE user_id = ? AND status = ? ORDER BY id",
                                    (self._user(), status)).fetchall()
        return [dict(r) for r in rows]

    def decide(self, suggestion_id, keep, high_priority=False):
        """Keep: onto the shelf as want. Skip: on the shelf as "not for me", so it's never suggested again."""
        row = self._conn().execute("SELECT * FROM reading_suggestions WHERE id = ? AND user_id = ? AND status = 'waiting'",
                                   (int(suggestion_id), self._user())).fetchone()
        if not row:
            raise ValueError("That suggestion isn't waiting any more.")
        book_id = self.add(row["title"], row["author"], high_priority and keep, "want" if keep else "not_for_me",
                           row["reason"] if keep else "")
        self._conn().execute("UPDATE reading_books SET source = ? WHERE id = ?",
                             (f"suggested by {row['made_by']}" + (f": {row['asked_for']}" if row["asked_for"] else ""),
                              book_id))
        self._conn().execute("UPDATE reading_suggestions SET status = ?, book_id = ?, decided_at = CURRENT_TIMESTAMP "
                             "WHERE id = ?", ("kept" if keep else "skipped", book_id, row["id"]))
        self.db._commit()
        return self.book(book_id)

    # ------------------------------------------------------------ bookstore visit

    def checklist(self):
        """Authors with books you're looking for, A-Z by last name: [{author, key, checked, books}]."""
        checked = {r["author_sort"] for r in self._conn().execute(
            "SELECT author_sort FROM reading_shelf_checks WHERE user_id = ?", (self._user(),)).fetchall()}
        groups = {}
        for book in self.books("want"):
            key = book["author_sort"] or "~unknown"
            group = groups.setdefault(key, {"author": book["author"] or "Unknown author", "key": key, "books": []})
            group["books"].append(book)
        out = sorted(groups.values(), key=lambda g: g["key"])
        for group in out:
            group["checked"] = group["key"] in checked
            group["books"].sort(key=lambda b: (-b["high_priority"], b["title"].lower()))
            group["letter"] = group["key"][:1].upper()
        return out

    def check_shelf(self, key, checked=True):
        if checked:
            self._conn().execute("INSERT OR IGNORE INTO reading_shelf_checks (user_id, author_sort) VALUES (?, ?)",
                                 (self._user(), str(key)[:200]))
        else:
            self._conn().execute("DELETE FROM reading_shelf_checks WHERE user_id = ? AND author_sort = ?",
                                 (self._user(), str(key)[:200]))
        self.db._commit()

    def finish_visit(self):
        self._conn().execute("DELETE FROM reading_shelf_checks WHERE user_id = ?", (self._user(),))
        self.db._commit()


def parse_checklist(text):
    """Books from a bookstore checklist written like "- [ ] Choo, Yangsze" then "  - ★ The Fox Wife"."""
    books, author = [], None
    for raw in (text or "").splitlines():
        found = re.match(r"^\s*-\s*\[[ xX]\]\s+(.+?)\s*$", raw)
        if found:
            author = found.group(1)
            continue
        title = re.match(r"^\s+-\s+(★\s*)?(.+?)\s*$", raw)
        if author and title:
            books.append({"author": author, "title": title.group(2), "high_priority": bool(title.group(1))})
    return books
