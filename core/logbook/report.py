"""What the Logbook hands out: rows as Curtis would write them, version labels, sums Dieter works out, and what a
proposal would change."""

from datetime import datetime

from .types import Invalid, format_duration, show

NUMERIC = ("integer", "number", "duration")


def out_value(kind, value):
    """A stored value for JSON: durations as written ("30:35"), everything else as stored."""
    if value is None:
        return None
    if kind == "duration":
        return format_duration(value)
    return value


def rows_out(columns, rows):
    return [dict({"row_id": r["row_id"]}, **{c["name"]: out_value(c["type"], r.get(c["name"])) for c in columns})
            for r in rows]


def short_day(ts):
    if not ts:
        return ""
    when = datetime.fromtimestamp(ts)
    return f"{when.strftime('%b')} {when.day}, {when.year}"


def label(table, version):
    """'Ragnar legs, v2, verified Nov 25, 2026' -- what an assistant quotes as its source."""
    if not version:
        return f"{table['name']}, no version yet"
    if version["status"] == "verified":
        return f"{table['name']}, v{version['number']}, verified {short_day(version['verified_at'])}"
    return f"{table['name']}, v{version['number']}, {version['status']} (not verified)"


def version_brief(table, version):
    if not version:
        return None
    return {"version_id": version["id"], "number": version["number"], "status": version["status"],
            "rows": version.get("row_count", len(version.get("rows") or [])), "label": label(table, version),
            "made_by": version.get("made_by"), "reason": version.get("reason") or "",
            "created": short_day(version.get("created_at")), "verified": short_day(version.get("verified_at"))}


def columns_out(columns):
    return [{"name": c["name"], "type": c["type"], "label": c["label"]} for c in columns]


def _fmt(kind, value):
    if value is None:
        return None
    if kind == "duration":
        return format_duration(round(value))
    if kind == "integer" and float(value).is_integer():
        return int(value)
    return round(value, 4)


def summarize(columns, rows, group_by=None, measure=None):
    """Count, total, average, min and max (with the row they came from) of each numeric column, overall or per value
    of group_by. measure limits which columns are summed. Durations come back written like 1:02:45."""
    kinds = {c["name"]: c["type"] for c in columns}
    if group_by and group_by not in kinds:
        raise Invalid(f"There's no column {group_by!r} to group by. Columns: {', '.join(kinds)}.")
    names = [c["name"] for c in columns if c["type"] in NUMERIC and c["name"] != group_by]
    if measure:
        wrong = [m for m in measure if m not in kinds]
        if wrong:
            raise Invalid(f"There's no column {wrong[0]!r}. Columns: {', '.join(kinds)}.")
        not_numeric = [m for m in measure if kinds[m] not in NUMERIC]
        if not_numeric:
            raise Invalid(f"{not_numeric[0]!r} is {kinds[not_numeric[0]]}; only number, integer and duration "
                          "columns can be added up.")
        names = list(measure)
    groups = {}
    for r in rows:
        key = r.get(group_by) if group_by else None
        groups.setdefault(key, []).append(r)

    def stats(members):
        out = {"rows": len(members)}
        for name in names:
            kind = kinds[name]
            have = [(r[name], r["row_id"]) for r in members if r.get(name) is not None]
            if not have:
                out[name] = {"count": 0}
                continue
            values = [v for v, _ in have]
            low, high = min(have, key=lambda p: p[0]), max(have, key=lambda p: p[0])
            out[name] = {"count": len(values), "total": _fmt(kind, sum(values)),
                         "average": _fmt(kind, sum(values) / len(values)),
                         "min": {"value": _fmt(kind, low[0]), "row_id": low[1]},
                         "max": {"value": _fmt(kind, high[0]), "row_id": high[1]}}
        return out

    if not group_by:
        return {"overall": stats(rows)}
    kind = kinds[group_by]
    return {"group_by": group_by,
            "groups": [dict({"value": out_value(kind, key)}, **stats(members)) for key, members in groups.items()],
            "overall": stats(rows)}


def describe_row(columns, row):
    return " | ".join(show(c["type"], row.get(c["name"])) or "-" for c in columns)


def change_summary(base_columns, base_rows, columns, rows):
    """What changes between two versions: {added_columns, added, changed: [{row_id, before, after}], removed},
    each row written out as text for the review card."""
    before = {r["row_id"]: r for r in base_rows}
    after = {r["row_id"]: r for r in rows}
    old_names = {c["name"] for c in base_columns}
    changed = []
    for row_id, row in after.items():
        if row_id in before and any(before[row_id].get(c["name"]) != row.get(c["name"]) for c in columns):
            changed.append({"row_id": row_id, "before": describe_row(columns, before[row_id]),
                            "after": describe_row(columns, row)})
    return {"added_columns": [f"{c['label']} ({c['type']})" for c in columns if c["name"] not in old_names],
            "added": [{"row_id": r["row_id"], "row": describe_row(columns, r)} for r in rows
                      if r["row_id"] not in before],
            "changed": changed,
            "removed": [{"row_id": r["row_id"], "row": describe_row(base_columns, r)} for r in base_rows
                        if r["row_id"] not in after]}


def create_summary(columns, rows):
    return {"added_columns": [f"{c['label']} ({c['type']})" for c in columns],
            "added": [{"row_id": f"new {i}", "row": describe_row(columns, r)} for i, r in enumerate(rows, 1)],
            "changed": [], "removed": []}


def summary_line(summary):
    bits = []
    for key, word in (("added", "added"), ("changed", "changed"), ("removed", "removed")):
        n = len(summary.get(key) or [])
        if n:
            bits.append(f"{n} row{'s' if n != 1 else ''} {word}")
    n = len(summary.get("added_columns") or [])
    if n:
        bits.append(f"{n} new column{'s' if n != 1 else ''}")
    return ", ".join(bits) or "no changes"
