"""Run queued Dieter feedback issues through local Codex.

This script is intended to run on a trusted workstation with the repo,
Codex CLI, git, and deployment credentials configured.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_BASE_URL = "https://dieter-406739570356.us-central1.run.app"


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


def claim_next(base_url: str, token: str, worker_name: str) -> dict:
    url = f"{base_url.rstrip('/')}/api/app-feedback/codex-runs/next?token={urllib.parse.quote(token)}&worker={urllib.parse.quote(worker_name)}"
    request = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def run_codex(codex_path: str, repo: Path, run: dict) -> tuple[int, str]:
    issue_path = repo / f"codex_feedback_issue_{run['report_id']}.md"
    final_path = repo / f"codex_feedback_issue_{run['report_id']}_result.md"
    plan = run.get("plan") or ""
    prompt = "\n".join(
        [
            "You are Codex working locally in the Dieter repo.",
            "",
            "Implement this approved issue plan. Keep the issue open for user testing.",
            "When finished, leave a concise final note with:",
            "- what changed",
            "- tests/checks run",
            "- commit hash if committed",
            "- deployed revision if deployed",
            "- what Curtis should manually test",
            "",
            "Do not mark the app issue closed. The worker will move it to Ready for Testing.",
            "",
            plan,
        ]
    )
    issue_path.write_text(prompt, encoding="utf-8")
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
    completed = subprocess.run(
        command,
        input=prompt,
        text=True,
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=None,
    )
    final_note = ""
    if final_path.exists():
        final_note = final_path.read_text(encoding="utf-8").strip()
    if not final_note:
        final_note = completed.stdout[-4000:].strip()
    return completed.returncode, final_note


def main() -> int:
    parser = argparse.ArgumentParser(description="Run queued Dieter feedback issues with local Codex.")
    parser.add_argument("--base-url", default=os.getenv("DIETER_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token", default=os.getenv("CODEX_WORKER_TOKEN") or os.getenv("DIETER_REGISTRATION_CODE") or "")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--codex", default=os.getenv("CODEX_BIN") or "codex")
    parser.add_argument("--worker", default=f"{socket.gethostname()}-codex")
    args = parser.parse_args()

    if not args.token:
        print("Set CODEX_WORKER_TOKEN or DIETER_REGISTRATION_CODE before running this worker.", file=sys.stderr)
        return 2

    repo = Path(args.repo).resolve()
    claim = claim_next(args.base_url, args.token, args.worker)
    if claim.get("status") == "empty":
        print("No queued Codex issue runs.")
        return 0
    if claim.get("status") != "claimed":
        print(json.dumps(claim, indent=2))
        return 1

    run = claim["run"]
    print(f"Running Codex for issue #{run['report_id']}: {run.get('title', '')}")
    try:
        return_code, note = run_codex(args.codex, repo, run)
        status = "ready_for_testing" if return_code == 0 else "failed"
        if return_code != 0:
            note = f"Codex worker failed with exit code {return_code}.\n\n{note}"
    except Exception as exc:
        status = "failed"
        note = f"Codex worker crashed: {exc}"

    result = post_json(
        f"{args.base_url.rstrip('/')}/api/app-feedback/codex-runs/{run['id']}/finish",
        {"token": args.token, "status": status, "result_note": note},
    )
    print(json.dumps(result, indent=2))
    print(note)
    return 0 if status == "ready_for_testing" else 1


if __name__ == "__main__":
    raise SystemExit(main())
