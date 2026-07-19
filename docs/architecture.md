# Dieter Architecture Notes

Dieter is a single FastAPI service organized around app areas. The current
shape favors easy local iteration and one small Cloud Run deployment, while the
app manifest layer keeps feature ownership visible inside the monorepo.

## Runtime Shape

```text
Browser
  -> FastAPI routes in api.py
     -> Jinja2 templates for HTML pages
     -> JSON endpoints under /api/*
     -> Database helper methods in database.py
     -> Optional LLM and integration services
```

The app initializes one SQLite database on startup. Local development defaults
to `projects.db`; demo review can use `demo.db`; Cloud Run restores a snapshot
to `/tmp/projects.db`.

## App Manifests

Top-level app metadata lives in `apps/<app>/manifest.py`. Each manifest owns:

- route prefixes
- app title and navigation label
- shell classes
- app menu links
- issue area
- launcher card copy
- theme colors

`apps/registry.py` aggregates those manifests for shared navigation and shell
selection. This keeps app-specific shell changes out of `templates/base.html`.

## Main App Areas

- `apps/assistant/`: planner and scheduler shell metadata
- `apps/kitchen/`: mature recipe, meal plan, grocery list, and cooking feedback shell metadata
- `apps/trainer/`: important in-progress workout, Strava, shoe, coach, and athlete shell metadata
- `apps/music/`: prototype playlist draft and Spotify workflow shell metadata
- `apps/issues/`: Studio shell metadata for feedback capture, planning, approval, Codex worker runs, and testing loops
- `apps/launcher/`: top-level app launcher shell metadata

App-specific templates live under `apps/<app>/templates/` when practical. The
Jinja loader includes both shared templates and app-owned template folders.

## AI Workflows

The app uses LLM services only when credentials are configured. Core pages and
demo data do not require an API key.

Current AI-assisted workflows include:

- planner and scheduler edit proposals
- recipe extraction and cleanup
- prototype playlist dictation parsing
- feedback synthesis into implementation plans
- Codex work-packet generation for approved issues

## Studio Feedback To Codex Flow

Dieter Studio captures feedback, classifies it by app area, generates or stores
an implementation plan, and can queue approved work for a local Codex worker.

`apps/pipeline.py` provides lightweight ownership hints so queued work prefers
app-owned folders and calls out shared/platform edits when they are necessary.

Studio is now project-aware at the configuration layer. Dieter app areas run
against this repository. External project areas are also defined for:

- EEG Headband, routed by `EEG_REPO_PATH`
- Calcium Imaging, routed by `CALCIUM_IMAGING_REPO_PATH`

The current database still stores a single issue `area` string, so external
projects use area labels such as `EEG / Firmware` and
`Calcium Imaging / Analysis`. The local worker resolves those labels to the
matching repo environment variable before launching Codex. External project
runs do not attempt a Dieter Cloud Run deployment.

## Persistence

Local development uses SQLite directly. The Cloud Run deployment keeps the
service intentionally small:

- restore SQLite from Cloud Storage on startup
- write to `/tmp/projects.db`
- upload a consistent snapshot after commits
- sync uploaded recipe images and thumbnails through Cloud Storage
- run with max instances and concurrency set to 1 to avoid snapshot conflicts

This is a personal-use persistence strategy. A heavier multi-user deployment
should move database state to Cloud SQL, Firestore, or another managed database.

## Known Refactoring Direction

`api.py` and `database.py` are currently broad shared files. That made early
iteration fast, but app route groups should gradually move into app-owned
routers and focused data modules. The app manifest system, boundary docs, and
regression tests exist to make that extraction safer.
