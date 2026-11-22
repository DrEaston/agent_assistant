"""Send Studio reports to GitHub, where Claude Code picks them up.

Needs DIETER_GITHUB_TOKEN: a fine-grained token for the Dieter repo with Issues read/write.
Without it, Studio still works as an inbox; the "Send to Claude" button just isn't shown.

An issue opened with the `claude` label starts the Claude Code workflow (.github/workflows/claude.yml),
which plans the change, opens a pull request, and gets a preview link from the preview workflow.
"""

import os

import httpx

API = "https://api.github.com"
LABEL = "claude"


def repo():
    return os.getenv("DIETER_GITHUB_REPO", "DrEaston/agent_assistant")


def token():
    return os.getenv("DIETER_GITHUB_TOKEN", "")


def enabled():
    return bool(token())


def _headers():
    return {"Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dieter-studio"}


def issue_body(report, base_url="https://askdieter.ai"):
    page = report.get("page_url") or ""
    lines = [
        "@claude please work on this request from Dieter Studio.",
        "",
        "## What I said",
        "",
        (report.get("raw_feedback") or "").strip(),
        "",
        "## Where",
        "",
        f"- App area: {report.get('area') or 'Dieter'}",
        f"- Page: {base_url + page if page.startswith('/') else page or 'not recorded'}",
        f"- Studio report: {base_url}/apps/issues/{report['id']}",
        "",
        "## How to work",
        "",
        "- Read CLAUDE.md first. Keep the change inside the app's own folder where you can.",
        "- If anything about what I want is unclear, ask here before writing code.",
        "- Open a pull request against `main` with a short plain-English summary of what changes for me.",
        "- The checks and preview link come from the pull request workflows; fix any failing check.",
    ]
    if report.get("codex_history"):
        lines += ["", "## Earlier attempts (old Codex pipeline)", ""]
        for run in report["codex_history"][-3:]:
            note = (run.get("result_note") or "").strip().replace("\n", " ")
            lines.append(f"- Run {run.get('id')}: {run.get('status')}. {note[:300]}")
    return "\n".join(lines)


def create_issue(report, client=None):
    """Open a GitHub issue for the report. Returns the issue's web URL."""
    payload = {"title": report.get("title") or "Change request from Dieter Studio",
               "body": issue_body(report), "labels": [LABEL, "from-studio"]}
    http = client or httpx.Client(timeout=20)
    try:
        response = http.post(f"{API}/repos/{repo()}/issues", json=payload, headers=_headers())
    finally:
        if client is None:
            http.close()
    response.raise_for_status()
    return response.json()["html_url"]


def issue_state(url, client=None):
    """{'state', 'comments', 'pull_request'} for an issue URL, or None when GitHub can't be reached."""
    if not (enabled() and url and "/issues/" in url):
        return None
    number = url.rstrip("/").rsplit("/", 1)[-1]
    http = client or httpx.Client(timeout=10)
    try:
        response = http.get(f"{API}/repos/{repo()}/issues/{number}", headers=_headers())
        if response.status_code != 200:
            return None
        data = response.json()
        return {"state": data.get("state"), "comments": data.get("comments", 0)}
    except httpx.HTTPError:
        return None
    finally:
        if client is None:
            http.close()
