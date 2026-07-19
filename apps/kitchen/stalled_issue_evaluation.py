"""Stalled Studio issue evaluation helpers for Kitchen / Recipes."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import re
from urllib.parse import urlparse


KITCHEN_AREA = "Kitchen / Recipes"
ELIGIBLE_STATUSES = {"open", "triaged", "in_progress", "ready_for_review"}
TERMINAL_STATUSES = {"done"}
DEFAULT_INACTIVITY_DAYS = 3
DEFAULT_RECENT_ISSUE_DAYS = 1


def parse_timestamp(value):
    """Parse the app's SQLite timestamp strings into naive UTC-ish datetimes."""
    if not value:
        return None
    text = str(value).strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def utc_now():
    return datetime.utcnow().replace(microsecond=0)


def action_history(report):
    try:
        parsed = json.loads((report or {}).get("audit_action_history_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        parsed = []
    return parsed if isinstance(parsed, list) else []


def history_fingerprint(report):
    """Return a compact fingerprint for duplicate-plan prevention."""
    parts = [
        str((report or {}).get("id") or ""),
        str((report or {}).get("status") or ""),
        str((report or {}).get("title") or ""),
        str((report or {}).get("raw_feedback") or ""),
        str((report or {}).get("audit_plan_updated_at") or ""),
        str((report or {}).get("implementation_note_updated_at") or ""),
    ]
    for entry in action_history(report)[-12:]:
        if isinstance(entry, dict):
            if str(entry.get("action") or "").startswith("auto_evaluation_"):
                continue
            parts.append("|".join([
                str(entry.get("action") or ""),
                str(entry.get("summary") or ""),
                str(entry.get("created_at") or ""),
            ]))
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def compact_text(text, limit=220):
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "..."


def infer_objective(report):
    """Infer the intended issue outcome from title, feedback, plan, and history."""
    title = compact_text((report or {}).get("title") or "Untitled issue", 90)
    raw = compact_text((report or {}).get("raw_feedback") or "", 220)
    plan = compact_text((report or {}).get("audit_plan") or "", 220)
    history = action_history(report)
    attempted = [
        compact_text(entry.get("summary") or entry.get("action") or "", 120)
        for entry in history
        if isinstance(entry, dict) and (entry.get("summary") or entry.get("action"))
    ][-4:]
    source = " ".join([title, raw, plan]).lower()
    if any(token in source for token in ["button", "page", "screen", "visible", "render", "homepage", "menu", "click", "flow"]):
        validation_kind = "UI behavior"
    elif any(token in source for token in ["api", "endpoint", "worker", "queue", "codex", "status", "transition"]):
        validation_kind = "workflow behavior"
    else:
        validation_kind = "app behavior"
    objective = f"Verify {title} is adequately resolved in Kitchen / Recipes."
    return {
        "objective": objective,
        "problem": raw or title,
        "expected_outcome": plan or raw or title,
        "attempted_fixes": attempted,
        "validation_kind": validation_kind,
    }


def issue_activity_at(report):
    dates = [
        parse_timestamp((report or {}).get("updated_at")),
        parse_timestamp((report or {}).get("audit_plan_updated_at")),
        parse_timestamp((report or {}).get("implementation_note_updated_at")),
    ]
    for entry in action_history(report):
        if isinstance(entry, dict):
            dates.append(parse_timestamp(entry.get("created_at")))
    return max([value for value in dates if value], default=None)


def user_activity_at(report):
    return (
        parse_timestamp((report or {}).get("user_last_active_at"))
        or parse_timestamp((report or {}).get("user_updated_at"))
        or issue_activity_at(report)
    )


def stalled_eligibility(report, now=None, inactivity_days=DEFAULT_INACTIVITY_DAYS, recent_issue_days=DEFAULT_RECENT_ISSUE_DAYS):
    """Explain whether a Studio issue is eligible for stalled evaluation."""
    now = now or utc_now()
    status = ((report or {}).get("status") or "open").strip()
    area = ((report or {}).get("area") or "").strip()
    if area != KITCHEN_AREA:
        return {"eligible": False, "reason": "Issue is outside Kitchen / Recipes."}
    if status in TERMINAL_STATUSES or status not in ELIGIBLE_STATUSES:
        return {"eligible": False, "reason": f"Issue status {status or 'unknown'} is not eligible."}
    if (report or {}).get("codex_run_status") in {"queued", "running"}:
        return {"eligible": False, "reason": "A Codex worker run is already queued or running."}

    user_at = user_activity_at(report)
    issue_at = issue_activity_at(report)
    inactivity_cutoff = now - timedelta(days=max(0, int(inactivity_days)))
    recent_issue_cutoff = now - timedelta(days=max(0, int(recent_issue_days)))
    if user_at and user_at > inactivity_cutoff:
        return {"eligible": False, "reason": f"Owning user was active at {user_at.strftime('%Y-%m-%d %H:%M:%S')}."}
    if issue_at and issue_at > recent_issue_cutoff:
        return {"eligible": False, "reason": f"Issue changed recently at {issue_at.strftime('%Y-%m-%d %H:%M:%S')}."}

    pieces = [
        f"Owning user has been inactive since {user_at.strftime('%Y-%m-%d %H:%M:%S') if user_at else 'an unknown time'}",
        f"issue status is {status}",
    ]
    if issue_at:
        pieces.append(f"last issue activity was {issue_at.strftime('%Y-%m-%d %H:%M:%S')}")
    return {"eligible": True, "reason": "; ".join(pieces) + "."}


def route_for_issue(report):
    page_url = ((report or {}).get("page_url") or "").strip()
    parsed_path = urlparse(page_url).path if page_url else ""
    if parsed_path.startswith("/apps/recipes"):
        return parsed_path
    return "/apps/recipes"


def build_evaluation_plan(report, eligibility=None, now=None):
    now = now or utc_now()
    eligibility = eligibility or stalled_eligibility(report, now=now)
    inferred = infer_objective(report)
    route = route_for_issue(report)
    checks = [
        f"Load {route} as a Kitchen page and confirm it responds without server errors.",
        "Inspect visible Kitchen content for the reported behavior or the nearest relevant flow.",
        "Confirm available controls/state match the inferred expected outcome without changing issue state.",
    ]
    if (report or {}).get("status") == "ready_for_review":
        checks.append("Treat the issue as implemented and evaluate whether it appears ready for Curtis to test.")
    assumptions = [
        "Issue history is the source of intent; no separate pipeline runtime exists.",
        "The first automated pass is observation-only and does not close or mutate the issue.",
        "User inactivity is measured from the best available user/issue timestamps.",
    ]
    fingerprint = history_fingerprint(report)
    lines = [
        "# Automated Stalled-Issue Evaluation Plan",
        "",
        f"- Issue: #{(report or {}).get('id')} {(report or {}).get('title') or 'Untitled issue'}",
        f"- Area: {(report or {}).get('area') or KITCHEN_AREA}",
        f"- Current status: {(report or {}).get('status') or 'open'}",
        f"- Selected as stalled because: {eligibility.get('reason') or 'No reason recorded.'}",
        f"- Inferred objective: {inferred['objective']}",
        f"- Validation type: {inferred['validation_kind']}",
        f"- History fingerprint: {fingerprint}",
        "",
        "## Intended Outcome",
        "",
        inferred["expected_outcome"],
        "",
        "## Proposed Non-Destructive Checks",
        "",
    ]
    lines.extend(f"- {check}" for check in checks)
    lines.extend(["", "## Assumptions", ""])
    lines.extend(f"- {assumption}" for assumption in assumptions)
    if inferred["attempted_fixes"]:
        lines.extend(["", "## Recent History Signals", ""])
        lines.extend(f"- {item}" for item in inferred["attempted_fixes"])
    return {
        "plan": "\n".join(lines),
        "fingerprint": fingerprint,
        "inferred_objective": inferred["objective"],
        "checks": checks,
        "assumptions": assumptions,
        "route": route,
    }


def existing_plan_matches(report):
    fingerprint = history_fingerprint(report)
    plan = (report or {}).get("auto_evaluation_plan") or ""
    return bool(plan.strip() and f"History fingerprint: {fingerprint}" in plan)


def evaluate_observed_behavior(report, app_check):
    """Build a persisted observation-only evaluation result."""
    plan = build_evaluation_plan(report)
    route = plan["route"]
    observation = app_check(route)
    status_code = int(observation.get("status_code") or 0)
    body = observation.get("body") or ""
    expected_terms = [
        term.lower()
        for term in re.findall(r"[A-Za-z][A-Za-z0-9']{3,}", " ".join([
            str((report or {}).get("title") or ""),
            str((report or {}).get("raw_feedback") or ""),
        ]))
    ][:10]
    matched_terms = [term for term in expected_terms if term in body.lower()]
    passed = 200 <= status_code < 400 and ("Kitchen" in body or "recipe" in body.lower())
    confidence = "medium" if passed and matched_terms else ("low" if passed else "low")
    recommendation = (
        "Request manual verification from Curtis before changing issue state."
        if passed
        else "Keep the issue in progress; the safe page check did not confirm the Kitchen page is healthy."
    )
    lines = [
        "# Automated Evaluation Outcome",
        "",
        f"- Issue: #{(report or {}).get('id')} {(report or {}).get('title') or 'Untitled issue'}",
        f"- Inferred objective: {plan['inferred_objective']}",
        f"- Route checked: {route}",
        f"- HTTP status observed: {status_code or 'unknown'}",
        f"- Appears adequately resolved: {'yes, with manual-test caveats' if passed else 'no'}",
        f"- Confidence: {confidence}",
        f"- Recommended next step: {recommendation}",
        "",
        "## Tests Performed",
        "",
    ]
    lines.extend(f"- {check}" for check in plan["checks"])
    lines.extend([
        "",
        "## Observed Behavior",
        "",
        compact_text(observation.get("summary") or body, 800) or "No response body was available.",
    ])
    if matched_terms:
        lines.extend(["", "## Matched Intent Terms", "", ", ".join(sorted(set(matched_terms)))])
    return {
        "status": "completed",
        "summary": "\n".join(lines),
        "appears_resolved": passed,
        "confidence": confidence,
        "route": route,
    }
