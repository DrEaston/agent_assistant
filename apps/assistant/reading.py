"""Reading: books you want, and a bookstore checklist by author.

Two kinds of state, kept apart on purpose:
- each book persists: title, author, ★ high priority, status (want -> found -> owned -> reading -> read);
- a bookstore visit is temporary: the author shelves you've already checked on this trip. Checking a shelf doesn't
  mean you found anything; "Done" clears the checks for the next trip.

Shelves are by author, so books group by author, sorted by last name ("VanderMeer, Jeff" under V).
"""

import re

from database import get_current_user_id

STATUSES = ("want", "found", "owned", "reading", "read")
STATUS_LABELS = {"want": "Looking for it", "found": "Found", "owned": "Own it", "reading": "Reading", "read": "Read"}


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
        allowed = {k: v for k, v in fields.items() if k in ("title", "author", "high_priority", "status", "notes") and v is not None}
        if "status" in allowed and allowed["status"] not in STATUSES:
            raise ValueError(f"Status is one of: {', '.join(STATUSES)}.")
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
