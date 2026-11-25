# The Logbook: checked tables that ChatGPT and Claude look facts up in

Goal: Curtis keeps named tables in Dieter's **Logbook** (Ragnar results first; the next one may have completely
different columns), loads them from a CSV, and has ChatGPT or Claude read them and suggest changes through the
connector. The Logbook is the source of fact for assistants with fuzzy memory: when a question might be answered
there, they look it up instead of remembering or working it out, and what they say or draw comes from data he has
checked.

Why it matters: the Ragnar results image ChatGPT made had a correct legs table but a wrong summary box. It said
the fastest Green Loop was "Eli, Leg 4, 30:35"; Leg 4 was Eli in 32:35, and the 30:35 was Sterling's Leg 1.

## Intake: where it goes

Run through the intake step in `CLAUDE.md`. The Logbook is **shared storage**: Ragnar is a Trainer thing, but the
next table could be for Kitchen or Music, and an app folder can't import another app's folder. So:

- The store lives in `core/logbook/` (like `core/portable_list.py`), so any app can read it.
- One small **Logbook** page for the person: his tables, a table view, CSV upload, proposals to approve, and
  "mark verified". It can grow into a full app later without changing how anything is stored.
- Apps show Logbook tables their own way later (e.g. a Trainer "Ragnar results" view built from the verified
  version).

## What goes in the Logbook (and what doesn't)

1. **Facts and records, not things an app manages.** Race results, personal bests, wine ratings, a team roster:
   Logbook. If an app needs to do something with the data (check it off, schedule it, cook from it), it belongs to
   that app with a proper shape. Recipes stay in Kitchen, not in a Logbook table.
2. **Tables an app relies on keep their columns.** Once an app shows a Logbook table, a proposal that renames or
   removes a column it uses says which page it would break, and needs his approval like any other change.
3. **Small tables.** The whole database is downloaded every time the site starts, so each table is capped (a few
   thousand rows to start). Something much bigger, like years of Strava data, gets its own home when it comes up.

## What a Logbook table is

- **Table**: stable id, owner, name, description, created/updated.
- **Columns**: name, type, order. Types: `text`, `integer`, `number`, `date`, `duration` (`30:35`, `1:02:45`;
  stored as seconds, shown as written), `boolean`.
- **Version**: stable id, number, status `draft` | `verified` | `superseded`, the rows, who made it (Curtis, the
  site, ChatGPT, Claude), the reason, created time, verified time.
- Every change makes a new version. Marking one verified keeps it available; later changes make a draft on top
  of it, and the verified one stays the answer to "latest verified" until he verifies the draft.
- Private to the owner: every query is scoped to the signed-in user, the same as the rest of Dieter.
- Additive tables only (`logbook_tables`, `logbook_versions`, `logbook_proposals`, in `core/logbook/store.py`); no
  change to existing tables. Each version keeps its columns and rows whole (tables are small). Rows carry a stable
  `row_id` (`r1`, `r2`, ...) that later versions keep and never reuse. Same SQLite file, restored and backed up the
  same way.
- Previews are read-only copies: a table loaded in a preview is gone when the preview restarts. The real Ragnar
  tables are loaded on the live site after merge.

## The contract with ChatGPT (enforced by Dieter, not promised by ChatGPT)

1. **Assistants propose; Curtis approves.** Creating a table or changing rows makes a proposal. He sees it on his
   Today page and the Logbook page as rows added, changed and removed, and approves or discards it. Only he can
   approve and only he can mark a version verified; there is no tool for either.
   Logbook proposals have their own review page (`/logbook/proposals/<id>`), are saved in the database and don't
   expire (he may get to them hours later). Approving makes a draft; he still marks it verified.
2. **No editing from stale data.** A proposal names the version it was based on. If the table has moved on, it's
   refused and the assistant is told to read it again.
3. **All or nothing, checked against the columns.** If any value doesn't fit its column type, the whole proposal is
   refused with a row-by-row list of problems. Nothing half-applied is ever saved.
4. **Every proposal says why** (a one-line reason) and records which assistant made it.
5. **Facts come from verified data only.** Reads for answers, summaries and images return the latest verified
   version with a label ("Ragnar legs, v2, verified Nov 25") that the assistant is told to quote. With no verified
   version, the tool says so and returns no rows.
6. **Dieter does the arithmetic.** A summary tool computes totals, averages, fastest and slowest, grouped by a
   column, from the verified rows. The assistant repeats those numbers instead of working them out.
7. **Look it up first.** The connector's instructions tell assistants: when a question might be answered by the
   Logbook (a race, a result, a list he keeps), search it before answering from memory, and say when it isn't
   there rather than guessing.

## Connector tools (structured JSON; ids are stable)

| Tool | What it does |
| --- | --- |
| `logbook_find` | Tables by name or words: id, name, columns with types, latest version, latest verified version. |
| `logbook_get` | One table: columns, types, and its versions (id, number, status, reason, who, when). |
| `logbook_read_rows` | Rows of a version (`version_id`, or `"verified"` for latest verified), `offset`/`limit` paging with `total` and `next_offset`, and the version label. |
| `logbook_summarize` | Sums, averages, counts, min/max (with the row they came from) over the verified version, optionally grouped by a column. |
| `logbook_propose_create` | Name, columns with types, rows, reason. Validated; becomes a proposal. |
| `logbook_propose_change` | Table id, `base_version_id`, rows to add / change (by row id) / remove, new columns, reason. Existing columns can't be renamed or removed by an assistant. Validated; becomes a proposal. |
| `logbook_list_proposals` | His pending proposals and what happened to earlier ones, so the assistant can follow up. |

All are listed by `dieter_find_tools`, so ChatGPT finds them even with an older tool list. The connector's
instructions gain a short "Logbook" paragraph restating the contract.

## Order of work

1. **This plan + the intake step** (docs only).
2. **Store and CSV import**: `core/logbook/` with types, versions, validation, size cap, tests. Upload a CSV on the
   Logbook page (column types guessed, he can change them before saving), table view, mark verified.
3. **Connector tools and proposals**: the tools above, proposals on Today and the Logbook page, tests through
   `/mcp`.
4. **Ragnar end to end**: try it in the preview with `docs/examples/ragnar_trail_arizona_2026_legs.csv` and
   `..._loops.csv`; after merge, load and verify them on the live site and ask ChatGPT for a summary, checking its
   numbers against the table.

Steps 2 and 3 may land as one pull request if they're small enough to check together.

## Example data

`docs/examples/` has the two raw tables from the Ragnar Trail Arizona results (Nov 6-7, 2026). Only the tables
are kept, not the image's summaries or awards. Checked: every pace equals time / distance, and the 24 legs add up
to 120.40 mi and 17:57:06, matching the header.
