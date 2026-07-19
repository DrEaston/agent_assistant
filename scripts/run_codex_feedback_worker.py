"""Run queued Dieter feedback issues through local Codex.

This script is intended to run on a trusted workstation with the repo,
Codex CLI, git, and deployment credentials configured.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.pipeline import classify_changed_paths, format_pipeline_prompt, normalize_project_id, pipeline_for_area, project_label_for_id


DEFAULT_BASE_URL = "https://dieter-406739570356.us-central1.run.app"
DEFAULT_PROJECT_ID = "recipes-442702"


def worker_defaults_for_project(project: str) -> dict[str, str]:
    """Return lane-specific default worker identity and status path."""
    safe_project = normalize_project_id(project)
    worker_slug = safe_project.replace("_", "-")
    return {
        "project": safe_project,
        "project_label": project_label_for_id(safe_project),
        "worker": f"{socket.gethostname()}-{worker_slug}-codex",
        "status_file": f"tmp/codex_worker_status_{safe_project}.json",
    }


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def write_status(status_file: Path, **updates) -> None:
    status_file.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    if status_file.exists():
        try:
            current = json.loads(status_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current = {}
    for key, value in updates.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    current["updated_at"] = utc_now()
    status_file.write_text(json.dumps(current, indent=2), encoding="utf-8")


def append_status_list(status_file: Path, key: str, item: dict, limit: int = 40) -> None:
    """Append one item to a list in the worker status file."""
    status_file.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    if status_file.exists():
        try:
            current = json.loads(status_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current = {}
    items = current.get(key) if isinstance(current.get(key), list) else []
    items.append(item)
    current[key] = items[-limit:]
    current["updated_at"] = utc_now()
    status_file.write_text(json.dumps(current, indent=2), encoding="utf-8")


def log_event(status_file: Path, message: str, level: str = "info", **details) -> None:
    """Write a timestamped event for the local dashboard scrollback."""
    append_status_list(
        status_file,
        "events",
        {
            "time": utc_now(),
            "level": level,
            "message": message,
            **{key: value for key, value in details.items() if value not in (None, "")},
        },
        limit=80,
    )


def remember_run(status_file: Path, run: dict, status: str, note: str = "", revision: str = "") -> None:
    """Keep a compact local history of recently completed worker runs."""
    append_status_list(
        status_file,
        "recent_runs",
        {
            "time": utc_now(),
            "run_id": run.get("id"),
            "report_id": run.get("report_id"),
            "title": run.get("title", ""),
            "area": run.get("area", ""),
            "status": status,
            "revision": revision,
            "note": (note or "")[:600],
        },
        limit=12,
    )


def run_process_with_heartbeats(
    command: list[str],
    repo: Path,
    status_file: Path,
    heartbeat_message: str,
    input_text: str = "",
    heartbeat_seconds: int = 30,
) -> tuple[int, str]:
    """Run a subprocess while keeping the local status page visibly alive."""
    process = subprocess.Popen(
        command,
        cwd=str(repo),
        stdin=subprocess.PIPE if input_text else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sent_input = False
    while True:
        try:
            if input_text and not sent_input:
                output, _ = process.communicate(input=input_text, timeout=heartbeat_seconds)
                sent_input = True
            else:
                output, _ = process.communicate(timeout=heartbeat_seconds)
            return process.returncode or 0, (output or "").strip()
        except subprocess.TimeoutExpired:
            sent_input = True
            log_event(status_file, heartbeat_message)
            write_status(status_file, message=heartbeat_message)

def dirty_worktree_summary(repo: Path) -> str:
    """Return git status output when local edits are present."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        return f"Could not check git status before running Codex:\n{output}"
    return output


def changed_paths(repo: Path) -> list[str]:
    """Return changed paths from git status porcelain output."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return []
    paths = []
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            paths.append(path)
    return paths


def shared_change_summary(repo: Path, area: str) -> str:
    """Summarize platform/shared changes currently visible for an app-scoped run."""
    classified = classify_changed_paths(changed_paths(repo), area)
    shared = classified.get("shared", [])
    other = classified.get("other", [])
    if not shared and not other:
        return ""
    lines = []
    if shared:
        lines.extend(["Shared/platform files currently changed:", *[f"- {path}" for path in shared[:20]]])
    if other:
        if lines:
            lines.append("")
        lines.extend(["Files outside this app area's owned paths currently changed:", *[f"- {path}" for path in other[:20]]])
    return "\n".join(lines)


def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def claim_next(base_url: str, token: str, worker_name: str, project: str = "dieter") -> dict:
    query = urllib.parse.urlencode({"token": token, "worker": worker_name, "project": normalize_project_id(project)})
    url = f"{base_url.rstrip('/')}/api/app-feedback/codex-runs/next?{query}"
    request = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def scan_stalled_issues(base_url: str, token: str, limit: int, inactivity_days: int, recent_issue_days: int) -> dict:
    """Ask Studio to generate reviewable stalled Kitchen evaluation plans."""
    query = urllib.parse.urlencode(
        {
            "token": token,
            "limit": limit,
            "inactivity_days": inactivity_days,
            "recent_issue_days": recent_issue_days,
        }
    )
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/app-feedback/stalled/scan?{query}",
        data=b"",
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def build_codex_prompt(run: dict, previous_failure: str = "") -> str:
    plan = run.get("plan") or ""
    area_guidance = format_pipeline_prompt(run.get("area") or "")
    parts = [
        "You are Codex working locally in the target repo for this Studio project lane.",
        "",
        "Implement this approved issue plan. Keep the issue open for user testing.",
        "Run focused checks after editing.",
        "",
        "App boundary guidance:",
        area_guidance,
        "",
        "Deployment is part of this pipeline:",
        "- Make the code changes needed for the approved plan.",
        "- Run checks/tests that fit the change.",
        "- Leave the worktree in a deployable state.",
        "- The worker will run the Cloud Run deploy script after you finish.",
        "- If a previous Codex or deploy attempt failed, diagnose that failure and fix it before finishing.",
        "",
        "When finished, leave a concise final note with:",
        "- what changed",
        "- tests/checks run",
        "- commit hash if committed",
        "- deployed revision if deployed by the worker",
        "- what Curtis should manually test",
        "",
        "Do not mark the app issue closed. The worker will move it to Ready for Testing after deployment.",
    ]
    if previous_failure.strip():
        parts.extend(
            [
                "",
                "Previous failure to diagnose and fix:",
                previous_failure.strip()[-8000:],
            ]
        )
    parts.extend(["", plan])
    return "\n".join(parts)


def run_codex(codex_path: str, repo: Path, run: dict, status_file: Path, previous_failure: str = "") -> tuple[int, str]:
    issue_path = status_file.parent / f"codex_feedback_issue_{run['report_id']}.md"
    final_path = status_file.parent / f"codex_feedback_issue_{run['report_id']}_result.md"
    prompt = build_codex_prompt(run, previous_failure=previous_failure)
    issue_path.write_text(prompt, encoding="utf-8")
    log_event(status_file, f"Wrote Codex prompt to {issue_path.name}.")
    if final_path.exists():
        final_path.unlink()
    command = [
        codex_path,
        "exec",
        "--cd",
        str(repo),
        "--sandbox",
        "danger-full-access",
        "-c",
        'approval_policy="never"',
        "--output-last-message",
        str(final_path),
        "-",
    ]
    log_event(status_file, "Starting Codex exec.")
    return_code, output = run_process_with_heartbeats(
        command,
        repo,
        status_file,
        heartbeat_message=f"Codex is still running on issue #{run['report_id']}.",
        input_text=prompt,
    )
    log_event(status_file, f"Codex exec finished with exit code {return_code}.")
    final_note = ""
    if final_path.exists():
        final_note = final_path.read_text(encoding="utf-8").strip()
    if not final_note:
        final_note = output[-4000:].strip()
    return return_code, final_note


def repo_for_run(default_repo: Path, run: dict):
    """Resolve which local repository should receive a Studio run."""
    pipeline = pipeline_for_area(run.get("area") or "")
    if not pipeline or not pipeline.repo_env:
        return default_repo, "", pipeline
    configured = os.getenv(pipeline.repo_env, "").strip()
    if not configured:
        return default_repo, f"Set {pipeline.repo_env} to route {pipeline.project_label} issues to the correct repo.", pipeline
    repo = Path(configured).expanduser().resolve()
    if not repo.exists():
        return default_repo, f"{pipeline.repo_env} points to a missing path: {repo}", pipeline
    return repo, "", pipeline


def run_deploy(repo: Path, project_id: str, status_file: Path) -> tuple[int, str, str]:
    """Deploy the current worktree and return exit code, output, and revision."""
    script = repo / "scripts" / "deploy_cloud_run.ps1"
    command = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-ProjectId",
        project_id,
    ]
    log_event(status_file, "Starting Cloud Run deploy.")
    return_code, output = run_process_with_heartbeats(
        command,
        repo,
        status_file,
        heartbeat_message="Cloud Run deploy is still running.",
    )
    revision = ""
    match = re.search(r"revision \[([^\]]+)\]", output, re.IGNORECASE)
    if match:
        revision = match.group(1)
    log_event(status_file, f"Cloud Run deploy finished with exit code {return_code}.", revision=revision)
    return return_code, output, revision


def run_one(args, repo: Path, status_file: Path) -> int:
    write_status(
        status_file,
        state="listening",
        message="Checking for queued Codex issues.",
        base_url=args.base_url,
        worker=args.worker,
        project=args.project,
        project_label=project_label_for_id(args.project),
        repo=str(repo),
        run_id=None,
        report_id=None,
        title=None,
        area=None,
        result_status=None,
        result_note=None,
        finished_at=None,
    )
    try:
        if args.scan_stalled:
            try:
                scan = scan_stalled_issues(
                    args.base_url,
                    args.token,
                    args.stalled_scan_limit,
                    args.stalled_inactivity_days,
                    args.stalled_recent_issue_days,
                )
                generated = len(scan.get("generated") or [])
                skipped = len(scan.get("skipped") or [])
                if generated or skipped:
                    log_event(status_file, f"Stalled issue scan completed: {generated} generated, {skipped} skipped.")
            except Exception as exc:
                log_event(status_file, f"Stalled issue scan failed: {exc}", level="warning")
        claim = claim_next(args.base_url, args.token, args.worker, args.project)
    except Exception as exc:
        write_status(status_file, state="error", message=f"Could not reach queue: {exc}")
        log_event(status_file, f"Could not reach queue: {exc}", level="error")
        raise

    if claim.get("status") == "empty":
        print("No queued Codex issue runs.")
        write_status(
            status_file,
            state="idle",
            message="Listening. No queued Codex issue runs.",
            project=args.project,
            project_label=project_label_for_id(args.project),
            run_id=None,
            report_id=None,
            title=None,
            area=None,
            result_status=None,
            result_note=None,
            finished_at=None,
        )
        return 0
    if claim.get("status") != "claimed":
        print(json.dumps(claim, indent=2))
        write_status(status_file, state="error", message="Queue returned an unexpected response.", response=claim)
        log_event(status_file, "Queue returned an unexpected response.", level="error")
        return 1

    run = claim["run"]
    target_repo, repo_error, pipeline = repo_for_run(repo, run)
    if repo_error:
        note = repo_error
        post_json(
            f"{args.base_url.rstrip('/')}/api/app-feedback/codex-runs/{run['id']}/finish",
            {"token": args.token, "status": "failed", "result_note": note},
        )
        write_status(
            status_file,
            state="failed",
            message=repo_error,
            run_id=run.get("id"),
            report_id=run.get("report_id"),
            title=run.get("title", ""),
            area=run.get("area", ""),
            project=run.get("project") or args.project,
            project_label=run.get("project_label") or project_label_for_id(args.project),
            repo=str(target_repo),
            result_status="failed",
            result_note=note,
            finished_at=utc_now(),
        )
        return 1
    log_event(status_file, f"Claimed issue #{run['report_id']}: {run.get('title', '')}.")
    write_status(
        status_file,
        state="running",
        message=f"Codex is working on issue #{run['report_id']}: {run.get('title', '')}",
        run_id=run.get("id"),
        report_id=run.get("report_id"),
        title=run.get("title", ""),
        area=run.get("area", ""),
        project=run.get("project") or args.project,
        project_label=run.get("project_label") or project_label_for_id(args.project),
        repo=str(target_repo),
        started_at=utc_now(),
    )
    print(f"Running Codex for issue #{run['report_id']}: {run.get('title', '')}")
    result_status = "failed"
    note = ""
    failure_context = ""
    revision = ""
    dirty_summary = dirty_worktree_summary(target_repo)
    if dirty_summary:
        boundary_summary = shared_change_summary(target_repo, run.get("area") or "")
        log_event(
            status_file,
            "Running Codex with current uncommitted worktree changes included.",
            level="info",
        )
        if boundary_summary:
            log_event(
                status_file,
                "Current worktree includes shared or cross-area edits.",
                level="warning",
            )
        write_status(
            status_file,
            message=f"Codex is working on issue #{run['report_id']} using the current local worktree.",
            shared_change_summary=boundary_summary or None,
        )
    max_attempts = max(1, int(args.repair_attempts) + 1)
    for attempt in range(1, max_attempts + 1):
        try:
            write_status(
                status_file,
                state="running",
                message=f"Codex attempt {attempt}/{max_attempts} is working on issue #{run['report_id']}.",
                attempt=attempt,
            )
            return_code, note = run_codex(args.codex, target_repo, run, status_file, previous_failure=failure_context)
            if return_code != 0:
                failure_context = f"Codex attempt {attempt} failed with exit code {return_code}.\n\n{note}"
                note = failure_context
                if attempt < max_attempts:
                    write_status(
                        status_file,
                        state="running",
                        message=f"Codex attempt {attempt} failed. Starting automatic repair attempt.",
                        result_note=note[-4000:],
                    )
                    continue
                break

            boundary_summary = shared_change_summary(target_repo, run.get("area") or "")
            if boundary_summary:
                note = "\n\n".join(
                    [
                        note.strip(),
                        "App boundary note:",
                        boundary_summary,
                    ]
                ).strip()
                write_status(status_file, shared_change_summary=boundary_summary)
            if not pipeline or pipeline.deploy_after:
                write_status(
                    status_file,
                    state="deploying",
                    message=f"Codex finished issue #{run['report_id']}. Deploying to Cloud Run.",
                    result_note=note[-4000:],
                )
                deploy_code, deploy_output, revision = run_deploy(target_repo, args.project_id, status_file)
            else:
                deploy_code, deploy_output, revision = 0, "External project run completed; no Dieter Cloud Run deploy configured.", ""
            deploy_tail = deploy_output[-5000:]
            if deploy_code == 0:
                result_status = "ready_for_testing"
                deployment_note = (
                    f"Deployed revision: {revision or 'unknown'}"
                    if not pipeline or pipeline.deploy_after
                    else "External project run completed; no deployment was attempted."
                )
                note = "\n\n".join(
                    [
                        note.strip(),
                        deployment_note,
                        "Deploy output:",
                        deploy_tail,
                    ]
                ).strip()
                break

            failure_context = "\n\n".join(
                [
                    f"Cloud Run deploy attempt {attempt} failed with exit code {deploy_code}.",
                    "Deploy output:",
                    deploy_tail,
                    "Codex note from the code attempt:",
                    note,
                ]
            )
            note = failure_context
            if attempt < max_attempts:
                write_status(
                    status_file,
                    state="running",
                    message=f"Deploy failed after Codex attempt {attempt}. Starting automatic repair attempt.",
                    result_note=note[-4000:],
                )
                continue
            break
        except Exception as exc:
            failure_context = f"Codex worker crashed during attempt {attempt}: {exc}"
            note = failure_context
            log_event(status_file, failure_context, level="error")
            if attempt >= max_attempts:
                break

    result = post_json(
        f"{args.base_url.rstrip('/')}/api/app-feedback/codex-runs/{run['id']}/finish",
        {"token": args.token, "status": result_status, "result_note": note},
    )
    print(json.dumps(result, indent=2))
    print(note)
    log_event(status_file, f"Issue #{run['report_id']} finished with status {result_status.replace('_', ' ')}.")
    remember_run(status_file, run, result_status, note=note, revision=revision if result_status == "ready_for_testing" else "")
    write_status(
        status_file,
        state="done" if result_status == "ready_for_testing" else "failed",
        message=f"Issue #{run['report_id']} finished with status {result_status.replace('_', ' ')}.",
        result_status=result_status,
        result_note=note[-4000:],
        finished_at=utc_now(),
    )
    return 0 if result_status == "ready_for_testing" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run queued Dieter feedback issues with local Codex.")
    parser.add_argument("--base-url", default=os.getenv("DIETER_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token", default=os.getenv("CODEX_WORKER_TOKEN") or os.getenv("DIETER_REGISTRATION_CODE") or "")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--codex", default=os.getenv("CODEX_BIN") or "codex")
    parser.add_argument("--project", default=os.getenv("CODEX_WORKER_PROJECT") or "dieter")
    parser.add_argument("--worker", default=None)
    parser.add_argument("--project-id", default=os.getenv("DIETER_GCP_PROJECT_ID") or DEFAULT_PROJECT_ID)
    parser.add_argument("--repair-attempts", type=int, default=int(os.getenv("CODEX_WORKER_REPAIR_ATTEMPTS") or "1"))
    parser.add_argument("--scan-stalled", action=argparse.BooleanOptionalAction, default=os.getenv("CODEX_WORKER_SCAN_STALLED", "1").lower() not in {"0", "false", "no"})
    parser.add_argument("--stalled-scan-limit", type=int, default=int(os.getenv("CODEX_WORKER_STALLED_SCAN_LIMIT") or "10"))
    parser.add_argument("--stalled-inactivity-days", type=int, default=int(os.getenv("CODEX_WORKER_STALLED_INACTIVITY_DAYS") or "3"))
    parser.add_argument("--stalled-recent-issue-days", type=int, default=int(os.getenv("CODEX_WORKER_STALLED_RECENT_ISSUE_DAYS") or "1"))
    parser.add_argument("--watch", action="store_true", help="Keep listening for queued issues until stopped.")
    parser.add_argument("--interval", type=int, default=30, help="Polling interval in seconds for watch mode.")
    parser.add_argument("--status-file", default=os.getenv("CODEX_WORKER_STATUS_FILE") or None)
    args = parser.parse_args()
    args.project = normalize_project_id(args.project)
    defaults = worker_defaults_for_project(args.project)
    if not args.worker:
        args.worker = defaults["worker"]
    if not args.status_file:
        args.status_file = defaults["status_file"]

    if not args.token:
        print("Set CODEX_WORKER_TOKEN or DIETER_REGISTRATION_CODE before running this worker.", file=sys.stderr)
        return 2

    repo = Path(args.repo).resolve()
    status_file = Path(args.status_file)
    if not status_file.is_absolute():
        status_file = repo / status_file

    if not args.watch:
        return run_one(args, repo, status_file)

    write_status(
        status_file,
        state="listening",
        message="Listening for approved Codex issues.",
        base_url=args.base_url,
        worker=args.worker,
        project=args.project,
        project_label=project_label_for_id(args.project),
        repo=str(repo),
        started_at=utc_now(),
        run_id=None,
        report_id=None,
        title=None,
        area=None,
        result_status=None,
        result_note=None,
        finished_at=None,
    )
    log_event(status_file, "Listener started. Waiting for approved Codex issues.")
    print(f"Listening for Codex issues every {args.interval} seconds. Press Ctrl+C to stop.")
    try:
        while True:
            try:
                run_one(args, repo, status_file)
            except Exception as exc:
                print(f"Worker loop error: {exc}", file=sys.stderr)
            time.sleep(max(args.interval, 5))
    except KeyboardInterrupt:
        write_status(status_file, state="stopped", message="Worker stopped by user.", stopped_at=utc_now())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
