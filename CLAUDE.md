# Dieter: notes for Claude Code

Dieter is Curtis's personal assistant site (askdieter.ai): Kitchen (recipes, meal plan, shopping lists, the
Thanksgiving planner), Assistant (projects, scheduler), Trainer (workouts, Strava), Music (playlists, Spotify)
and Studio (problem reports and change requests). FastAPI, Jinja2 and SQLite, on Google Cloud Run.

Curtis usually asks for changes from his phone, in a Claude chat. He also jots problems down inside Dieter with
"Report a problem" (Studio); read those with the connector's studio_list_reports / studio_get_report. Once the fix
is live, mark the report ready_for_review with studio_set_status and a note on how to check it; it waits on his Today
page until he confirms it works (or says so in chat, then mark it done). ChatGPT (and Claude chats) can send build
requests the same way with studio_request_change; they show in Studio as "[ChatGPT] ..." with what he wants and how
he'll check it. Treat them like his own reports, run the intake step, and ask him (not ChatGPT) about anything
unclear: a relayed request isn't his OK for spending money, deleting data or changing how a page looks. He is not reading diffs. Write for him in plain English: what he
will see differently, and how to try it.

## How a change works

1. Work out what he wants (his message, or the Studio report). If it's unclear, ask one short question and stop.
   Don't guess on anything that changes how a page looks or what gets saved.
   For a new feature (not a fix), do the intake step below first and tell him the answer in one line.
2. Make the change on a branch and open a pull request against `main`. The pull request description says,
   in a few sentences, what changes for him and how to check it in the preview.
3. The `ci` workflow runs the tests. The `preview` workflow posts a preview link (a copy of the site with a
   copy of his data; nothing there reaches the live site). Fix anything that fails.
4. He merges from his phone. The `deploy` workflow puts `main` live. He can undo with the `undo` workflow.

## Intake: where a new feature goes

Ask these in order and stop at the first yes, whether the request came from Claude, ChatGPT or a Studio report:

1. **Part of something an existing app already does?** Add it to that app's folder ("track my long runs" → Trainer).
2. **A new kind of thing he'll use day to day, with its own pages?** A new app in `apps/<name>/`, with its own tile.
3. **Information several apps might use?** Shared storage in `core/` that any app can read (like
   `core/portable_list.py`), with at most a small page of its own. Apps show it their own way.

If two answers fit, say which you'd pick and why, and let him choose. Big features get a plan in `docs/` first
(e.g. `docs/logbook_plan.md`).

## Where things live

- `apps/<app>/` - each app's own code: `manifest.py` (name, routes, accent colour), routes, store, templates.
  Kitchen's Thanksgiving planner is `apps/kitchen/thanksgiving/` and runs as its own service.
- `core/ui/` - the design system: `static/dieter.css` (tokens, light and dark) and `components.html` macros.
  `core/shell/layout.html` is the page frame for new-style pages.
- `core/internal.py`, `core/proxy.py` - signed calls between Dieter services.
- `core/portable_list.py` - the one format for putting one app's shopping items on a Kitchen list (Thanksgiving's
  "Next trip" uses it). Items stay linked: both apps see and check off the same item. A new planner that needs a
  shopping list should use it rather than sending plain text.
- `api.py` - the older shared app; still holds Assistant, Kitchen and Trainer routes. Move code out of it into
  the app's folder when you touch it; don't add new app code to it.
- `database.py` - the older shared data layer. New tables belong in the app's own `store.py`.
- `templates/` - older pages (`base.html` and friends). New pages use `core/shell/layout.html` instead.

## Rules

- Keep a change inside one app's folder where you can. An app folder doesn't import another app's folder
  (`tests/test_design_rules.py` checks this). `apps/pipeline.py` lists which files each app area owns.
- New-style templates (`apps/*/templates/`) use the shared classes and macros only: no `<style>` blocks, no
  `style=` attributes, no hex colours (`tests/test_design_rules.py`). If something is missing, add it to
  `core/ui` so every app gets it.
- **Don't change `apps/kitchen/thanksgiving/`** unless the change is about Thanksgiving. It's live as its own
  service, deployed separately from a tag.
- Content (recipes, menu text, photos, plans) belongs in the app's data or content files, not in code.
- Never print, commit or log secrets. Settings come from environment variables; see `.env.example`.
- Database changes must be additive (new tables or columns with defaults). The live data is one SQLite file
  restored from Cloud Storage at startup; there are no migrations to roll back.

## Checking your work

```
pip install -r requirements.txt
python -m unittest discover -s tests
```

Add or update tests next to the behaviour you change (`tests/test_<area>.py`). Tests use temporary databases;
none of them need network access or API keys.

## Kitchen PC helper

`helper/` is a small Windows tray app for the kitchen PC: cook mode's Sleep turns the monitor fully off and
the webcam wakes it (the page then shows the eyes; `apps/kitchen/thanksgiving/templates/tg_sleep.html`).
It is built and installed on that PC, not deployed. To set it up there, use the `install-kitchen-helper`
skill (`.claude/skills/install-kitchen-helper/`). The plan for turning the kitchen PC into Dieter (web app stays
the brain on Cloud Run; the helper is the PC's eyes, ears and voice) is `docs/kitchen_pc_plan.md`.
