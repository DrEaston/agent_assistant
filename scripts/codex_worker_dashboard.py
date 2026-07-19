"""Local browser dashboard for the Dieter Codex feedback worker.

This binds to 127.0.0.1 only. It starts/stops the local worker process and
shows whether the worker is listening, idle, running Codex, or finished.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.pipeline import normalize_project_id, project_label_for_id


DEFAULT_BASE_URL = "https://dieter-406739570356.us-central1.run.app"


def worker_defaults_for_project(project: str) -> dict[str, str]:
    safe_project = normalize_project_id(project)
    worker_slug = safe_project.replace("_", "-")
    return {
        "project": safe_project,
        "project_label": project_label_for_id(safe_project),
        "worker": f"{socket.gethostname()}-{worker_slug}-codex",
        "status_file": f"tmp/codex_worker_status_{safe_project}.json",
    }


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dieter Codex Worker</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #132018;
      --muted: #5b6b61;
      --line: #d7e2dc;
      --panel: #f5fbf7;
      --green: #14532d;
      --green-2: #22c55e;
      --running-bg: #f1f5f9;
      --running-text: #475569;
      --orange: #f97316;
      --cyan: #0891b2;
      --red: #b91c1c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #eef7f1;
      color: var(--ink);
    }
    main {
      width: min(920px, calc(100vw - 32px));
      margin: 32px auto;
      display: grid;
      gap: 18px;
    }
    header {
      background: var(--green);
      color: #fff;
      border-radius: 8px;
      padding: 20px;
    }
    h1, h2, p { margin: 0; }
    header p { margin-top: 6px; color: #d9fbe6; }
    section {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      display: grid;
      gap: 14px;
    }
    .status-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 16px;
      align-items: center;
    }
    .status-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .status-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      display: grid;
      gap: 8px;
      background: #fbfefc;
    }
    .status-card h2 {
      font-size: 1rem;
    }
    .badge {
      display: inline-flex;
      width: fit-content;
      border-radius: 999px;
      padding: 6px 12px;
      font-weight: 800;
      background: #e2e8f0;
      color: #334155;
      text-transform: capitalize;
    }
    .badge.listening, .badge.idle, .badge.running-listener, .badge.running { background: var(--running-bg); color: var(--running-text); }
    .badge.deploying { background: #ffedd5; color: #9a3412; }
    .badge.pipeline-idle { background: #f1f5f9; color: #475569; }
    .badge.done { background: #dbeafe; color: #1e40af; }
    .badge.failed, .badge.error { background: #fee2e2; color: #991b1b; }
    .badge.stopped { background: #f1f5f9; color: #475569; }
    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    button, a.button {
      border: 0;
      border-radius: 8px;
      padding: 10px 14px;
      font-weight: 800;
      cursor: pointer;
      background: var(--green);
      color: #fff;
      text-decoration: none;
      font: inherit;
    }
    button.secondary { background: #e8f5ed; color: var(--green); border: 1px solid #b7d8c4; }
    button.danger { background: #fee2e2; color: var(--red); border: 1px solid #fecaca; }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    dl {
      display: grid;
      grid-template-columns: max-content minmax(0, 1fr);
      gap: 8px 14px;
      margin: 0;
    }
    dt { color: var(--muted); font-weight: 700; }
    dd { margin: 0; overflow-wrap: anywhere; }
    pre {
      margin: 0;
      white-space: pre-wrap;
      overflow: auto;
      max-height: 280px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
    }
    .log-box {
      display: grid;
      gap: 8px;
      max-height: 340px;
      overflow: auto;
      background: #07130c;
      color: #e7f8ec;
      border-radius: 8px;
      padding: 12px;
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      font-size: 0.9rem;
      line-height: 1.45;
    }
    .log-entry {
      display: grid;
      grid-template-columns: 10rem minmax(0, 1fr);
      gap: 10px;
      border-bottom: 1px solid rgba(255,255,255,0.08);
      padding-bottom: 6px;
    }
    .log-entry:last-child { border-bottom: 0; padding-bottom: 0; }
    .log-time { color: #9ee7b5; }
    .log-message { overflow-wrap: anywhere; }
    .log-entry.error .log-message { color: #fecaca; }
    .run-list {
      display: grid;
      gap: 10px;
    }
    .run-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      display: grid;
      gap: 6px;
    }
    .run-item strong { overflow-wrap: anywhere; }
    .run-meta {
      color: var(--muted);
      font-size: 0.92rem;
    }
    .run-note {
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .quiet { color: var(--muted); }
    @media (max-width: 640px) {
      .status-row { grid-template-columns: 1fr; }
      .status-grid { grid-template-columns: 1fr; }
      dl { grid-template-columns: 1fr; }
      .log-entry { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <h1>Dieter Codex Worker</h1>
    <p>Local listener for approved Issues plans. Keep this page open when you want the workstation to pick up Codex work.</p>
  </header>
  <section>
    <div class="status-grid">
      <div class="status-card">
        <div class="status-row">
          <div>
            <h2>Listener</h2>
            <p id="listener-message" class="quiet">Loading...</p>
          </div>
          <span id="listener-state" class="badge">loading</span>
        </div>
      </div>
      <div class="status-card">
        <div class="status-row">
          <div>
            <h2>Codex Pipeline</h2>
            <p id="pipeline-message" class="quiet">Loading...</p>
          </div>
          <span id="pipeline-state" class="badge">loading</span>
        </div>
      </div>
    </div>
    <div class="controls">
      <button id="start">Start Listening</button>
      <button id="run-once" class="secondary">Run Once</button>
      <button id="stop" class="danger">Stop</button>
      <a class="button secondary" href="/api/status">Raw Status</a>
    </div>
  </section>
  <section>
    <h2>Details</h2>
    <dl>
      <dt>Process</dt><dd id="process">unknown</dd>
      <dt>Lane</dt><dd id="project">unknown</dd>
      <dt>Worker</dt><dd id="worker">unknown</dd>
      <dt>Issue</dt><dd id="issue">none</dd>
      <dt>Attempt</dt><dd id="attempt">none</dd>
      <dt>Started</dt><dd id="started">unknown</dd>
      <dt>Updated</dt><dd id="updated">unknown</dd>
      <dt>Repo</dt><dd id="repo">unknown</dd>
      <dt>Repo Safety</dt><dd id="repo-safety">unknown</dd>
    </dl>
  </section>
  <section id="dirty-section" hidden>
    <h2>Using Current Local Files</h2>
    <p class="quiet">This repo has uncommitted changes. The worker will use these files on disk as its starting point.</p>
    <pre id="dirty-summary"></pre>
  </section>
  <section>
    <h2>Current Activity Log</h2>
    <div id="events" class="log-box">No activity yet.</div>
  </section>
  <section>
    <h2>Recently Completed Runs</h2>
    <div id="recent-runs" class="run-list"><p class="quiet">No completed runs yet.</p></div>
  </section>
  <section>
    <h2>Last Result</h2>
    <pre id="note">No result yet.</pre>
  </section>
</main>
<script>
async function post(path) {
  const response = await fetch(path, {method: "POST"});
  if (!response.ok) throw new Error(await response.text());
  await refresh();
}
function setText(id, value) {
  document.getElementById(id).textContent = value || "";
}
function renderEvents(events) {
  const box = document.getElementById("events");
  box.replaceChildren();
  if (!events || !events.length) {
    box.textContent = "No activity yet.";
    return;
  }
  for (const event of events.slice().reverse()) {
    const row = document.createElement("div");
    row.className = "log-entry " + (event.level || "info");
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = event.time || "";
    const message = document.createElement("span");
    message.className = "log-message";
    const extras = [];
    if (event.revision) extras.push("revision " + event.revision);
    message.textContent = event.message + (extras.length ? " (" + extras.join(", ") + ")" : "");
    row.append(time, message);
    box.append(row);
  }
}
function renderRecentRuns(runs) {
  const box = document.getElementById("recent-runs");
  box.replaceChildren();
  if (!runs || !runs.length) {
    const empty = document.createElement("p");
    empty.className = "quiet";
    empty.textContent = "No completed runs yet.";
    box.append(empty);
    return;
  }
  for (const run of runs.slice().reverse()) {
    const item = document.createElement("div");
    item.className = "run-item";
    const title = document.createElement("strong");
    title.textContent = "#" + (run.report_id || "?") + " " + (run.title || "Untitled issue");
    const meta = document.createElement("div");
    meta.className = "run-meta";
    meta.textContent = [
      run.status || "unknown",
      run.area || "",
      run.revision ? "revision " + run.revision : "",
      run.time || ""
    ].filter(Boolean).join(" - ");
    const note = document.createElement("div");
    note.className = "run-note";
    note.textContent = run.note || "";
    item.append(title, meta, note);
    box.append(item);
  }
}
async function refresh() {
  const response = await fetch("/api/status", {cache: "no-store"});
  const data = await response.json();
  const status = data.status || {};
  const state = status.state || (data.running ? "listening" : "stopped");
  const listenerState = data.running ? "running-listener" : "stopped";
  const listenerBadge = document.getElementById("listener-state");
  listenerBadge.textContent = data.running ? "running" : "stopped";
  listenerBadge.className = "badge " + listenerState;
  setText("listener-message", data.running ? "Listening process is alive." : "Listening process is stopped.");
  const activePipelineStates = ["running", "deploying", "error"];
  const pipelineState = activePipelineStates.includes(state) ? state : "pipeline-idle";
  const pipelineBadge = document.getElementById("pipeline-state");
  pipelineBadge.textContent = activePipelineStates.includes(state) ? state.replaceAll("_", " ") : "idle";
  pipelineBadge.className = "badge " + pipelineState;
  const pipelineMessage = activePipelineStates.includes(state)
    ? (status.message || "Codex pipeline is active.")
    : "No Codex job is currently running.";
  setText("pipeline-message", pipelineMessage);
  setText("process", data.running ? "running (pid " + data.pid + ")" : "stopped");
  setText("project", status.project_label || data.project_label || status.project || data.project || "unknown");
  setText("worker", status.worker || data.worker || "unknown");
  setText("issue", status.report_id ? "#" + status.report_id + " " + (status.title || "") : "none");
  setText("attempt", status.attempt ? String(status.attempt) : "none");
  setText("started", status.started_at || "unknown");
  setText("updated", status.updated_at || "unknown");
  setText("repo", status.repo || data.repo || "unknown");
  setText("repo-safety", data.dirty ? "current local changes will be included" : "clean: matches committed files");
  setText("note", status.result_note || "No result yet.");
  const dirtySection = document.getElementById("dirty-section");
  dirtySection.hidden = !data.dirty;
  setText("dirty-summary", data.dirty_summary || "");
  renderEvents(status.events || []);
  renderRecentRuns(status.recent_runs || []);
  document.getElementById("start").disabled = data.running;
  document.getElementById("run-once").disabled = data.running;
  document.getElementById("stop").disabled = !data.running;
}
document.getElementById("start").addEventListener("click", () => post("/api/start").catch(alert));
document.getElementById("run-once").addEventListener("click", () => post("/api/run-once").catch(alert));
document.getElementById("stop").addEventListener("click", () => post("/api/stop").catch(alert));
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


class WorkerDashboard:
    def __init__(self, repo: Path, base_url: str, token: str, codex: str, worker: str, project: str, interval: int, status_file: Path):
        self.repo = repo
        self.base_url = base_url
        self.token = token
        self.codex = codex
        self.worker = worker
        self.project = normalize_project_id(project)
        self.project_label = project_label_for_id(self.project)
        self.interval = interval
        self.status_file = status_file
        self.process: subprocess.Popen | None = None
        self._recent_runs_cache: dict = {"time": 0, "runs": []}

    def dirty_worktree_summary(self) -> str:
        """Return git status output when local edits are present."""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(self.repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout or "").strip()
        if result.returncode != 0:
            return f"Could not check git status:\n{output}"
        return output

    def remote_recent_runs(self) -> list[dict]:
        """Fetch recent worker runs from the deployed Dieter app."""
        import time

        now = time.time()
        if now - float(self._recent_runs_cache.get("time") or 0) < 10:
            return list(self._recent_runs_cache.get("runs") or [])
        if not self.token:
            return []
        query = urllib.parse.urlencode({"token": self.token, "limit": 12, "project": self.project})
        url = f"{self.base_url.rstrip('/')}/api/app-feedback/codex-runs/recent?{query}"
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            runs = payload.get("runs") if isinstance(payload, dict) else []
            if not isinstance(runs, list):
                runs = []
            self._recent_runs_cache = {"time": now, "runs": runs}
            return runs
        except Exception:
            return list(self._recent_runs_cache.get("runs") or [])

    def status(self) -> dict:
        status = {}
        if self.status_file.exists():
            try:
                status = json.loads(self.status_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                status = {"state": "error", "message": "Status file could not be parsed."}
        remote_runs = self.remote_recent_runs()
        if remote_runs:
            status["recent_runs"] = remote_runs
        running = self.process is not None and self.process.poll() is None
        dirty_summary = self.dirty_worktree_summary()
        return {
            "running": running,
            "pid": self.process.pid if running and self.process else None,
            "repo": str(self.repo),
            "worker": self.worker,
            "project": self.project,
            "project_label": self.project_label,
            "dirty": bool(dirty_summary),
            "dirty_summary": dirty_summary,
            "status": status,
        }

    def start(self, watch: bool = True) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        command = [
            sys.executable,
            str(self.repo / "scripts" / "run_codex_feedback_worker.py"),
            "--base-url",
            self.base_url,
            "--token",
            self.token,
            "--repo",
            str(self.repo),
            "--codex",
            self.codex,
            "--worker",
            self.worker,
            "--project",
            self.project,
            "--status-file",
            str(self.status_file),
        ]
        if watch:
            command.extend(["--watch", "--interval", str(self.interval)])
        env = os.environ.copy()
        env["CODEX_WORKER_TOKEN"] = self.token
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            command,
            cwd=str(self.repo),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()


def make_handler(dashboard: WorkerDashboard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A003
            return

        def send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/":
                body = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/status":
                self.send_json(dashboard.status())
                return
            self.send_json({"error": "not found"}, status=404)

        def do_POST(self):  # noqa: N802
            if self.path == "/api/start":
                dashboard.start(watch=True)
                self.send_json({"status": "started"})
                return
            if self.path == "/api/run-once":
                dashboard.start(watch=False)
                self.send_json({"status": "started"})
                return
            if self.path == "/api/stop":
                dashboard.stop()
                self.send_json({"status": "stopping"})
                return
            self.send_json({"error": "not found"}, status=404)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Open a local Dieter Codex worker dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--base-url", default=os.getenv("DIETER_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token", default=os.getenv("CODEX_WORKER_TOKEN") or os.getenv("DIETER_REGISTRATION_CODE") or "")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--codex", default=os.getenv("CODEX_BIN") or "codex")
    parser.add_argument("--project", default=os.getenv("CODEX_WORKER_PROJECT") or "dieter")
    parser.add_argument("--worker", default=None)
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--status-file", default=os.getenv("CODEX_WORKER_STATUS_FILE") or None)
    args = parser.parse_args()
    args.project = normalize_project_id(args.project)
    defaults = worker_defaults_for_project(args.project)
    if not args.worker:
        args.worker = defaults["worker"]
    if not args.status_file:
        args.status_file = defaults["status_file"]

    if not args.token:
        print("Set CODEX_WORKER_TOKEN before launching the dashboard.", file=sys.stderr)
        return 2

    repo = Path(args.repo).resolve()
    status_file = Path(args.status_file)
    if not status_file.is_absolute():
        status_file = repo / status_file
    dashboard = WorkerDashboard(repo, args.base_url, args.token, args.codex, args.worker, args.project, args.interval, status_file)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(dashboard))
    print(f"Dieter Codex Worker dashboard ({project_label_for_id(args.project)}): http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        dashboard.stop()
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
