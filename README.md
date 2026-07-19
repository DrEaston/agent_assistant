# Dieter Personal Assistant

Dieter is a modular personal assistant platform built with Python, FastAPI,
Jinja2, and SQLite. It started as a project planner and grew into a small
personal operating system with app-specific workflows for planning, scheduling,
recipes, training support, and feedback-driven implementation.

The project is intentionally kept in one repository while using app manifests,
shared navigation, and ownership rules to keep feature areas understandable as
the codebase grows.

## Live Demo

The public demo is available at:

```text
https://dieter.ai
```

Use **Open Guest Demo** on the homepage to enter the read-only guest profile.
Guest mode is intended for portfolio/reviewer access: it shows sample Kitchen,
Scheduler, Trainer, and Studio workflows while disabling writes, private
integrations, and Codex execution.

## What It Demonstrates

- FastAPI web app with JSON APIs and server-rendered Jinja2 screens
- SQLite data layer shared by browser routes, API routes, and CLI-era helpers
- Modular app shells for Assistant, Kitchen, Music, Trainer, Studio, and the app launcher
- Mature Kitchen/Recipe and Scheduler workflows with shared navigation, persistence, and regression coverage
- Optional AI workflows for scheduler/planner edits, recipe cleanup, feedback synthesis, and Codex work packets
- Optional integrations and prototypes for Spotify, Strava, Google Cloud Run, Cloud Storage, and Secret Manager
- Regression tests for navigation, scheduler behavior, member approval, recipe/scheduler integration, and app-boundary rules

## App Areas

- **Assistant**: project planner, priorities, blockers, scheduler, and action steps
- **Kitchen**: the most complete app area; recipe import, meal planning, reusable recipe components, grocery lists, and cooking feedback
- **Scheduler**: mature agenda/checklist workflows embedded in Assistant and surfaced across Kitchen
- **Trainer**: important in-progress app area for workout planning, Strava imports, shoe tracking, reflections, and coach/athlete views
- **Music**: early prototype for dictated playlist drafts, editable song rows, and Spotify submission
- **Studio**: the agentic development console; captures app feedback, synthesizes plans, manages approval, queues Codex runs, and tracks testing feedback
- **Launcher**: top-level app entry point and shared navigation

Studio is also prepared for external project routing. Dieter issues run against
this repository. EEG Headband and Calcium Imaging issue areas are configured as
future external targets; set `EEG_REPO_PATH` or `CALCIUM_IMAGING_REPO_PATH` for
the local worker to route approved runs to those repos.

## Demo Mode

The public guest profile at `https://dieter.ai` does not require personal data
or external-service credentials.

For local review, demo mode creates a separate `demo.db` with invented data and
a demo user. The seeded demo highlights Kitchen/Recipes with Overnight Cinnamon
Rolls and other sample meals, plus Scheduler, Studio, and Trainer data.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\seed_demo_data.py --force
$env:DB_PATH="demo.db"
$env:DEMO_MODE="1"
.\.venv\Scripts\python.exe -m uvicorn api:app --reload
```

If you have a local Dieter database and want the demo to reuse a few lived-in
examples, copy only sanitized Scheduler, grocery list, and Trainer metrics:

```powershell
.\.venv\Scripts\python.exe scripts\seed_demo_data.py --force --source-db projects.db
```

That copy path omits raw Strava payloads, GPS traces, project/action ownership
links, and account tokens. If no source rows are available, the demo falls back
to curated examples such as the mechanic scheduler card and cinnamon-roll
grocery list.

Open:

```text
http://localhost:8000
```

In demo mode, `/` shows the public Dieter landing page. Use **Open Guest Demo**
for read-only access, or **Member Login** for a real account.

Local seeded demo login:

```text
demo@example.com
demo-password
```

AI actions, Spotify, Strava, and Cloud Run persistence are optional. Without
their credentials, the core app still loads and the integration-specific actions
remain unavailable or unconfigured.
When `DEMO_MODE=1` is set, the app labels itself as a read-only public preview
and refuses Codex worker polling even if a token is accidentally configured.

For review, Kitchen/Recipes and Scheduler are the strongest product surfaces.
Trainer shows the next important product direction. Music/Spotify is included
as a prototype integration, not as a polished showcase app.

Docker Compose is also available for local runtime state:

```powershell
docker compose up --build
```

Compose stores SQLite data under `data/` and uploads under `uploads/`; both are
ignored by Git.

## Tests

Run the regression suite with:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

The focused app-boundary checks can also be run directly:

```powershell
python -m unittest tests.test_app_pipeline tests.test_app_issue_menu
```

## Configuration

Copy `.env.example` to `.env` for local development. The only variable needed
for the basic demo is `DB_PATH` if you want to use a database other than
`projects.db`.

Common optional variables:

```text
DB_PATH=demo.db
UPLOADS_DIR=uploads
OPENAI_API_KEY=...
STRAVA_CLIENT_ID=...
STRAVA_CLIENT_SECRET=...
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
EEG_REPO_PATH=C:\path\to\eeg_repo
CALCIUM_IMAGING_REPO_PATH=C:\path\to\calcium_imaging_repo
```

Secrets and runtime data should not be committed. Local SQLite databases,
uploads, temporary files, worker status files, and `.env` are ignored.

## Architecture

```text
FastAPI app (api.py)
  HTML routes rendered with Jinja2
  JSON routes under /api/*
  app-specific route groups for Assistant, Kitchen, Music, Trainer, Studio

apps/
  manifests define app shell metadata, navigation, route prefixes, theme values
  registry resolves the active app shell and launcher cards
  pipeline defines app ownership hints for issue/Codex workflows

database.py
  SQLite schema initialization
  project, scheduler, recipe, trainer, playlist, user, and feedback operations

templates/ and apps/*/templates/
  shared templates plus app-owned screens

scripts/
  deployment, local worker, cleanup, and demo-data utilities
```

More detail:

- [App boundaries](docs/app_boundaries.md)
- [New app/page process](docs/new_app_page_process.md)
- [Cloud Run deployment](docs/cloud_run_deployment.md)
- [Architecture notes](docs/architecture.md)
- [Screenshot plan](docs/screenshot_plan.md)

## Deployment

The app can deploy to Google Cloud Run for personal use. The current production
deployment model uses:

- Cloud Run service with max instances set to 1
- optional min instances set to 1 to keep `dieter.ai` warm
- SQLite restored to `/tmp/projects.db`
- Cloud Storage snapshots after database commits
- Cloud Storage sync for uploaded recipe images and thumbnails
- Secret Manager for API keys

See [Cloud Run deployment](docs/cloud_run_deployment.md) for the full setup.

## Repository Safety

This repository should be publishable without personal data:

- Use `demo.db` for portfolio review.
- Keep real `projects.db` and uploads out of Git.
- Keep `.env` out of Git.
- Use `.env.example` for placeholder configuration only.
- Replace screenshots if they contain private tasks, recipes, workouts, or account details.

## Current Tradeoffs

Dieter is a personal project that grew quickly. Some shared files, especially
`api.py` and `database.py`, are intentionally broad right now. The app-boundary
manifest system and regression tests are the first step toward extracting
larger route groups into app-owned routers without losing the convenience of a
single deployable service.
