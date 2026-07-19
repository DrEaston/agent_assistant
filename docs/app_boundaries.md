# Dieter App Boundaries

Dieter stays in one repository, but top-level apps should keep their app-owned wiring in app-owned folders.

## App-Owned Folders

Top-level app metadata lives under `apps/<app>/`:

- `apps/assistant/` - Assistant and scheduler shell metadata
- `apps/kitchen/` - Kitchen and recipe shell metadata
- `apps/trainer/` - Trainer shell metadata
- `apps/music/` - Music playlist shell metadata
- `apps/issues/` - Issues shell metadata
- `apps/launcher/` - Dieter app launcher shell metadata

Each app manifest owns its route prefixes, title, nav label, app shell links, launcher card, and theme colors. Normal app navigation or theme changes should happen in the app's manifest instead of `templates/base.html`.

App-specific Jinja templates live under `apps/<app>/templates/` when practical. The shared Jinja loader searches these folders after `templates/`, so existing `get_template("trainer.html")` style calls continue to work while the files are owned by their app folder.

## Shared Registry

`apps/registry.py` is the small shared aggregation point. It imports app manifests and exposes:

- `app_shell_for_path()` for request-path shell lookup
- `global_nav_apps()` for stable top navigation
- `launcher_cards()` for the `/apps` launcher

This file should change only when adding, removing, or reordering a top-level app. Feature work inside Trainer, Music, Kitchen, or another app should not need to edit it.

## Templates

`templates/base.html` renders a generic app shell from the active manifest. Avoid adding app-specific route branches there. If an app needs a new shell link, body class, color, launcher copy, or issue area, update its manifest.

Shared templates such as `base.html`, auth, project detail pages, and issue pages remain in `templates/`. Avoid putting new app-specific screens there.

## Route Handlers

The current FastAPI route handlers still live in `api.py`. That is an unavoidable shared touchpoint for now. When adding substantial new route groups, prefer extracting them behind an app-owned module or router instead of adding more unrelated app code to the middle of `api.py`.

## Shared Code

Use shared modules only for behavior that is genuinely reused across apps. App-specific helpers, constants, styles, and tests should stay with that app when practical. If a change requires editing both an app manifest and a shared file, keep the shared edit small and document why it is shared in the commit or issue note.

## Codex Issue Worker

The app stays in one repository and deploys through one Cloud Run service for now, so approved Codex jobs still run serially. This avoids two workers editing or deploying the same monolithic app at the same time.

`apps/pipeline.py` provides lightweight area ownership hints for the local Codex worker. When an issue is queued for Trainer, Kitchen / Recipes, Music, Studio, Assistant / Planner, Scheduler, Auth, or Dieter, the worker prompt tells Codex which app-owned folders to prefer and which files are shared/platform files.

The same file also defines external Studio project areas for EEG Headband and Calcium Imaging. Those areas are labels such as `EEG / Firmware` or `Calcium Imaging / Analysis`. The local worker can route those issues to another repo when `EEG_REPO_PATH` or `CALCIUM_IMAGING_REPO_PATH` is set.

Shared/platform files include `api.py`, `database.py`, `templates/base.html`, deployment files, `scripts/`, `docs/`, and `tests/`. Editing them is allowed when necessary, but the worker records an app-boundary note so broad changes are visible during review/testing.
