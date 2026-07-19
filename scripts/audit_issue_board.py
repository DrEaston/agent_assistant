"""Summarize and triage the Dieter Studio issue board.

This is intentionally read-only. Point it at a copied SQLite database, not the
live Cloud Run database file, while the Codex worker is running.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


ACTIVE_STATUSES = {"open", "in_progress", "ready_for_review"}
FAILED_CODEX_MARKERS = ("[WinError 2]", "cannot find the file specified", "could not reach queue")


@dataclass(frozen=True)
class Issue:
    id: int
    title: str
    area: str
    status: str
    raw_feedback: str
    audit_plan: str
    audit_plan_approved_at: str
    implementation_note: str
    run_id: int | None
    run_status: str
    run_note: str
    run_count: int

    @property
    def has_plan(self) -> bool:
        return bool(self.audit_plan.strip())

    @property
    def approved(self) -> bool:
        return bool(self.audit_plan_approved_at.strip())

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


def compact_text(value: str, limit: int = 150) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def normalize_terms(value: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (value or "").lower())
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "be",
        "for",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
    return {word for word in words if len(word) > 2 and word not in stopwords}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def load_issues(db_path: Path) -> list[Issue]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT r.id,
                   r.title,
                   r.area,
                   r.status,
                   r.raw_feedback,
                   r.audit_plan,
                   r.audit_plan_approved_at,
                   r.implementation_note,
                   (
                       SELECT cr.id
                       FROM app_feedback_codex_runs cr
                       WHERE cr.report_id = r.id
                         AND COALESCE(cr.hidden_at, '') = ''
                       ORDER BY cr.id DESC
                       LIMIT 1
                   ) AS run_id,
                   (
                       SELECT cr.status
                       FROM app_feedback_codex_runs cr
                       WHERE cr.report_id = r.id
                         AND COALESCE(cr.hidden_at, '') = ''
                       ORDER BY cr.id DESC
                       LIMIT 1
                   ) AS run_status,
                   (
                       SELECT cr.result_note
                       FROM app_feedback_codex_runs cr
                       WHERE cr.report_id = r.id
                         AND COALESCE(cr.hidden_at, '') = ''
                       ORDER BY cr.id DESC
                       LIMIT 1
                   ) AS run_note,
                   (
                       SELECT COUNT(*)
                       FROM app_feedback_codex_runs cr
                       WHERE cr.report_id = r.id
                         AND COALESCE(cr.hidden_at, '') = ''
                   ) AS run_count
            FROM app_feedback_reports r
            ORDER BY r.created_at DESC, r.id DESC
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        Issue(
            id=int(row["id"]),
            title=row["title"] or "",
            area=row["area"] or "Dieter",
            status=row["status"] or "open",
            raw_feedback=row["raw_feedback"] or "",
            audit_plan=row["audit_plan"] or "",
            audit_plan_approved_at=row["audit_plan_approved_at"] or "",
            implementation_note=row["implementation_note"] or "",
            run_id=row["run_id"],
            run_status=row["run_status"] or "",
            run_note=row["run_note"] or "",
            run_count=int(row["run_count"] or 0),
        )
        for row in rows
    ]


def issue_recommendation(issue: Issue) -> tuple[str, str]:
    title_text = f"{issue.title} {issue.raw_feedback}".lower()
    run_note = issue.run_note.lower()
    if issue.run_status == "running":
        return "P0", "Leave running; review after the worker finishes."
    if issue.status == "ready_for_review":
        return "P1", "Manual smoke test, then close if the behavior is correct."
    if "stalled pipeline" in title_text or "pipeline" in title_text and "audit" in title_text:
        return "P1", "Use as the canonical issue-board audit/process improvement."
    if "eeg" in title_text or "calcium" in title_text:
        return "P2", "Keep, but condense into one cross-repo onboarding/category cleanup issue."
    if "demo mode" in title_text or "demo" in title_text:
        return "P2", "Fold into one demo-mode taxonomy/safety issue unless it is already fixed."
    if issue.run_status == "failed" and any(marker.lower() in run_note for marker in FAILED_CODEX_MARKERS):
        return "P2", "Retry later if still relevant; the failure was worker infrastructure, not implementation."
    if issue.status == "done":
        return "Closed", "No action."
    if not issue.has_plan:
        return "P3", "Audit before implementation."
    return "P3", "Keep queued behind current portfolio/demo polish."


def likely_duplicate_groups(issues: list[Issue]) -> list[list[Issue]]:
    active = [issue for issue in issues if issue.active]
    parent: dict[int, int] = {issue.id: issue.id for issue in active}

    def find(issue_id: int) -> int:
        while parent[issue_id] != issue_id:
            parent[issue_id] = parent[parent[issue_id]]
            issue_id = parent[issue_id]
        return issue_id

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    terms = {issue.id: normalize_terms(f"{issue.title} {issue.raw_feedback}") for issue in active}
    for index, left in enumerate(active):
        for right in active[index + 1 :]:
            left_text = f"{left.title} {left.raw_feedback}".lower()
            right_text = f"{right.title} {right.raw_feedback}".lower()
            left_has_demo = "demo" in left_text
            right_has_demo = "demo" in right_text
            left_has_imaging = "eeg" in left_text or "calcium" in left_text
            right_has_imaging = "eeg" in right_text or "calcium" in right_text
            same_theme = (
                (left_has_demo and right_has_demo and left.area == right.area)
                or (left_has_imaging and right_has_imaging and left.area == right.area)
                or jaccard(terms[left.id], terms[right.id]) >= 0.32
            )
            if same_theme:
                union(left.id, right.id)

    groups: dict[int, list[Issue]] = defaultdict(list)
    for issue in active:
        groups[find(issue.id)].append(issue)
    return [group for group in groups.values() if len(group) > 1]


def render_markdown(issues: list[Issue]) -> str:
    active = [issue for issue in issues if issue.active]
    by_status: dict[str, int] = defaultdict(int)
    for issue in issues:
        by_status[issue.status] += 1

    lines = [
        "# Studio Issue Board Audit",
        "",
        "Read-only triage generated from a copied Dieter SQLite database.",
        "",
        "## Snapshot",
        "",
        f"- Total issues: {len(issues)}",
        f"- Active issues: {len(active)}",
        f"- Closed issues: {by_status.get('done', 0)}",
        f"- Ready for testing: {by_status.get('ready_for_review', 0)}",
        "",
        "## Recommended Queue",
        "",
    ]
    ranked = sorted(
        active,
        key=lambda issue: (
            {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "Closed": 4}.get(issue_recommendation(issue)[0], 5),
            issue.id,
        ),
    )
    for issue in ranked:
        priority, recommendation = issue_recommendation(issue)
        run = issue.run_status or "no run"
        lines.extend(
            [
                f"### {priority} - #{issue.id} {issue.title}",
                "",
                f"- Area: {issue.area}",
                f"- Status: {issue.status}; latest run: {run}",
                f"- Recommendation: {recommendation}",
                f"- Feedback: {compact_text(issue.raw_feedback)}",
                "",
            ]
        )

    groups = likely_duplicate_groups(issues)
    lines.extend(["## Condense Candidates", ""])
    if not groups:
        lines.extend(["No obvious active duplicate groups detected.", ""])
    for group in groups:
        label = ", ".join(f"#{issue.id}" for issue in sorted(group, key=lambda item: item.id))
        lines.append(f"- {label}: " + "; ".join(issue.title for issue in sorted(group, key=lambda item: item.id)))
    lines.append("")

    lines.extend(
        [
            "## Standard Review Procedure",
            "",
            "1. Pull a read-only DB snapshot while no schema migration is running.",
            "2. Separate `running` issues from triage; do not edit them until the worker finishes.",
            "3. Close or test `ready_for_review` issues before approving new work.",
            "4. Retry failed runs only after classifying the failure as infrastructure, plan, test, or implementation.",
            "5. Merge same-theme issues into the clearest parent issue and close/delete accidental duplicates.",
            "6. Keep at most one P0/P1 Codex run active; leave speculative project/category cleanup as P2/P3.",
            "7. Save the audit note, then queue the next issue from the top of the recommended queue.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a Dieter Studio issue-board SQLite snapshot.")
    parser.add_argument("--db-path", default="projects.db")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    issues = load_issues(db_path)
    markdown = render_markdown(issues)
    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
