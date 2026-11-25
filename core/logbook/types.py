"""Logbook column types: checking a value, storing it, showing it, and guessing types from a CSV.

Stored values are plain JSON: text -> str, integer -> int, number -> float, date -> "YYYY-MM-DD",
duration -> whole seconds (int), boolean -> bool. An empty cell is None in any column.
"""

import csv
import io
import re
from datetime import date

TYPES = ("text", "integer", "number", "date", "duration", "boolean")
TYPE_HELP = {
    "text": "any text",
    "integer": "a whole number, like 24",
    "number": "a number, like 4.95",
    "date": "a date, like 2026-11-06",
    "duration": "a time like 30:35 or 1:02:45",
    "boolean": "yes or no",
}
MAX_TEXT = 2000
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
TRUE_WORDS, FALSE_WORDS = {"yes", "y", "true", "t", "1"}, {"no", "n", "false", "f", "0"}


class Invalid(ValueError):
    """A value or a table doesn't fit; the message says why in plain words."""


def column_name(raw):
    """A column name as stored: lower case, words joined by underscores ("Distance (mi)" -> "distance_mi")."""
    name = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")
    if name and name[0].isdigit():
        name = "c_" + name
    return name[:40]


def check_columns(columns):
    """[{name, type, label?}] -> cleaned list. Raises Invalid for a bad or repeated name or an unknown type."""
    if not isinstance(columns, list) or not columns:
        raise Invalid("A table needs at least one column.")
    if len(columns) > 50:
        raise Invalid("A table can have at most 50 columns.")
    out, seen = [], set()
    for i, col in enumerate(columns, 1):
        if not isinstance(col, dict):
            raise Invalid(f"Column {i} should be {{name, type}}.")
        name = str(col.get("name") or "").strip()
        kind = str(col.get("type") or "").strip().lower()
        if not NAME_RE.match(name):
            raise Invalid(f"Column {i} name {name!r} should be lower case letters, digits and _ (like distance_mi).")
        if name in seen:
            raise Invalid(f"Column {name!r} appears twice.")
        if kind not in TYPES:
            raise Invalid(f"Column {name!r} has type {kind!r}; use one of {', '.join(TYPES)}.")
        seen.add(name)
        label = " ".join(str(col.get("label") or "").split())[:60] or name.replace("_", " ").capitalize()
        out.append({"name": name, "type": kind, "label": label})
    return out


def parse_duration(text):
    """'30:35' -> 1835, '1:02:45' -> 3765. Raises Invalid."""
    parts = text.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts) or any(len(p) != 2 for p in parts[1:]):
        raise Invalid("not a time like 30:35 or 1:02:45")
    numbers = [int(p) for p in parts]
    hours, minutes, seconds = numbers if len(numbers) == 3 else [0] + numbers
    if seconds > 59 or (len(numbers) == 3 and minutes > 59):
        raise Invalid("not a time like 30:35 or 1:02:45")
    return hours * 3600 + minutes * 60 + seconds


def format_duration(seconds):
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def to_value(kind, raw):
    """A raw value (from CSV text or an assistant's JSON) -> the stored value. Raises Invalid."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if kind == "text":
        if isinstance(raw, (dict, list)):
            raise Invalid("should be text")
        text = str(raw).strip()
        if len(text) > MAX_TEXT:
            raise Invalid(f"is longer than {MAX_TEXT} characters")
        return text
    if kind == "boolean":
        if isinstance(raw, bool):
            return raw
        word = str(raw).strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise Invalid("should be yes or no")
    if isinstance(raw, bool):
        raise Invalid(f"should be {TYPE_HELP[kind]}")
    if kind == "integer":
        if isinstance(raw, float) and raw.is_integer():
            return int(raw)
        if isinstance(raw, int):
            return raw
        text = str(raw).strip().replace(",", "")
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        raise Invalid("should be a whole number")
    if kind == "number":
        if isinstance(raw, (int, float)):
            value = float(raw)
        else:
            text = str(raw).strip().replace(",", "")
            if not re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)", text):
                raise Invalid("should be a number")
            value = float(text)
        if value != value or value in (float("inf"), float("-inf")):
            raise Invalid("should be a number")
        return value
    if kind == "date":
        text = str(raw).strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                return date.fromisoformat(text).isoformat()
        except ValueError:
            pass
        raise Invalid("should be a date like 2026-11-06")
    if kind == "duration":
        if isinstance(raw, int):
            if raw < 0:
                raise Invalid("can't be negative")
            return raw
        return parse_duration(str(raw).strip())
    raise Invalid(f"unknown type {kind}")


def show(kind, value):
    """A stored value as Curtis would write it."""
    if value is None:
        return ""
    if kind == "duration":
        return format_duration(value)
    if kind == "boolean":
        return "Yes" if value else "No"
    if kind == "number":
        return "%.10g" % value
    return str(value)


NOTHING_SAVED = "Nothing was saved. Fix these and send it again: "


def fail(problems):
    """Raise Invalid listing every problem (at most 30)."""
    more = f" (and {len(problems) - 30} more)" if len(problems) > 30 else ""
    raise Invalid(NOTHING_SAVED + "; ".join(problems[:30]) + more)


def row_problems(columns, rows, label=lambda i: f"row {i}"):
    """Raw rows (dicts by column name) -> (stored rows, problems). label(i) names row i (from 1) in a problem."""
    if not isinstance(rows, list):
        return [], ["rows should be a list of objects keyed by column name"]
    names = {c["name"]: c["type"] for c in columns}
    out, problems = [], []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            problems.append(f"{label(i)}: should be an object keyed by column name")
            continue
        extra = [k for k in row if k not in names and k != "row_id"]
        if extra:
            problems.append(f"{label(i)}: no column called {', '.join(map(repr, extra))}")
        clean = {}
        for name, kind in names.items():
            try:
                clean[name] = to_value(kind, row.get(name))
            except Invalid as exc:
                problems.append(f"{label(i)}, {name} = {row.get(name)!r}: {exc}")
        out.append(clean)
    return out, problems


def check_rows(columns, rows):
    """Raw rows -> stored rows, all or nothing: raises Invalid listing every problem."""
    out, problems = row_problems(columns, rows)
    if problems:
        fail(problems)
    return out


def guess_type(values):
    """The narrowest type every non-empty value fits."""
    values = [v for v in values if str(v).strip()]
    if not values:
        return "text"
    for kind in ("integer", "number", "duration", "date", "boolean"):
        try:
            for v in values:
                to_value(kind, v)
            return kind
        except Invalid:
            continue
    return "text"


def read_csv(text, max_rows):
    """CSV text -> (columns with guessed types, raw rows keyed by column name). Raises Invalid."""
    text = text.lstrip("﻿")
    try:
        records = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise Invalid(f"That file isn't a readable CSV ({exc}).") from None
    records = [r for r in records if any(cell.strip() for cell in r)]
    if not records:
        raise Invalid("That CSV is empty.")
    header, body = records[0], records[1:]
    if len(body) > max_rows:
        raise Invalid(f"That CSV has {len(body)} rows; a Logbook table holds at most {max_rows}.")
    names = [column_name(h) for h in header]
    if any(not n for n in names):
        raise Invalid("Every column in the first row needs a name.")
    if len(set(names)) != len(names):
        raise Invalid("Two columns in the first row have the same name.")
    for i, r in enumerate(body, 2):
        if len(r) > len(names) and any(c.strip() for c in r[len(names):]):
            raise Invalid(f"Line {i} has more cells than there are columns.")
    rows = [{n: (r[j].strip() if j < len(r) else "") for j, n in enumerate(names)} for r in body]
    columns = [{"name": n, "type": guess_type([row[n] for row in rows]),
                "label": "" if h.strip() == n else h.strip()}
               for n, h in zip(names, header)]
    return check_columns(columns), rows
