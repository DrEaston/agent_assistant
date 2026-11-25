# Datasets: shared tables that ChatGPT and Claude can read and propose changes to

Goal: Curtis can keep named tables in Dieter (Ragnar results first; the next one may have completely different
columns), load them from a CSV, and have ChatGPT or Claude read them and suggest changes through the connector.
What an assistant says or draws from a dataset comes from data he has checked, not from its own arithmetic.

Why it matters: the Ragnar results image ChatGPT made had a correct legs table but a wrong summary box. It said
the fastest Green Loop was "Eli, Leg 4, 30:35"; Leg 4 was Eli in 32:35, and the 30:35 was Sterling's Leg 1.

## Intake: where it goes

Run through the intake step in `CLAUDE.md`. Datasets are **shared storage**: Ragnar is a Trainer thing, but the
next dataset could be for Kitchen or Music, and an app folder can't import another app's folder. So:

- The store lives in `core/datasets/` (like `core/portable_list.py`), so any app can read it.
- One small **Data** page for the person: his datasets, a table view, CSV upload, proposals to approve, and
  "mark verified". It can grow into a full app later without changing how anything is stored.
- Apps show datasets their own way later (e.g. a Trainer "Ragnar results" view built from the verified version).

## What a dataset is

- **Dataset**: stable id, owner, name, description, created/updated.
- **Columns**: name, type, order. Types: `text`, `integer`, `number`, `date`, `duration` (`30:35`, `1:02:45`;
  stored as seconds, shown as written), `boolean`.
- **Version**: stable id, number, status `draft` | `verified` | `superseded`, the rows, who made it (Curtis, the
  site, ChatGPT, Claude), the reason, created time, verified time.
- Every change makes a new version. Marking one verified keeps it available; later changes make a draft on top
  of it, and the verified one stays the answer to "latest verified" until he verifies the draft.
- Private to the owner: every query is scoped to the signed-in user, the same as the rest of Dieter.
- Additive tables only (`datasets`, `dataset_versions`, `dataset_rows`, `dataset_proposals`) in the store's own
  module; no change to existing tables.

## The contract with ChatGPT (enforced by Dieter, not promised by ChatGPT)

1. **Assistants propose; Curtis approves.** Creating a dataset or changing rows makes a proposal. He sees it on
   his Today page and the Data page as rows added, changed and removed, and approves or discards it. Only he can
   approve and only he can mark a version verified; there is no tool for either.
   Reuses `core/connector/proposals.py` cards, but dataset proposals are saved in the database and don't expire
   after 30 minutes (he may get to them hours later).
2. **No editing from stale data.** A proposal names the version it was based on. If the dataset has moved on, it's
   refused and the assistant is told to read it again.
3. **All or nothing, checked against the columns.** If any value doesn't fit its column type, the whole proposal is
   refused with a row-by-row list of problems. Nothing half-applied is ever saved.
4. **Every proposal says why** (a one-line reason) and records which assistant made it.
5. **Summaries and images use verified data only.** Reads for a summary return the latest verified version with a
   label ("Ragnar legs, v2, verified Nov 25") that the assistant is told to quote. With no verified version, the
   tool says so and returns no rows.
6. **Dieter does the arithmetic.** A summary tool computes totals, averages, fastest and slowest, grouped by a
   column, from the verified rows. The assistant repeats those numbers instead of working them out.

## Connector tools (structured JSON; ids are stable)

| Tool | What it does |
| --- | --- |
| `datasets_find` | Datasets by name or words: id, name, columns with types, latest version, latest verified version. |
| `datasets_get` | One dataset: columns, types, and its versions (id, number, status, reason, who, when). |
| `datasets_read_rows` | Rows of a version (`version_id`, or `"verified"` for latest verified), `offset`/`limit` paging with `total` and `next_offset`, and the version label. |
| `datasets_summarize` | Sums, averages, counts, min/max (with the row they came from) over the verified version, optionally grouped by a column. |
| `datasets_propose_create` | Name, columns with types, rows, reason. Validated; becomes a proposal. |
| `datasets_propose_change` | Dataset id, `base_version_id`, rows to add / change (by row id) / remove, reason. Validated; becomes a proposal. |
| `datasets_list_proposals` | His pending proposals and what happened to earlier ones, so the assistant can follow up. |

All are listed by `dieter_find_tools`, so ChatGPT finds them even with an older tool list. The connector's
instructions gain a short "Datasets" paragraph restating the contract.

## Order of work

1. **This plan + the intake step** (docs only).
2. **Store and CSV import**: `core/datasets/` with types, versions, validation, tests. Upload a CSV on the Data page
   (column types guessed, he can change them before saving), table view, mark verified.
3. **Connector tools and proposals**: the tools above, proposals on Today and the Data page, tests through `/mcp`.
4. **Ragnar end to end** in the preview: load `docs/examples/ragnar_trail_arizona_2026_legs.csv` and
   `..._loops.csv`, verify them, then ask ChatGPT for a summary and check its numbers against the table.

Steps 2 and 3 may land as one pull request if they're small enough to check together.

## Example data

`docs/examples/` has the two raw tables from the Ragnar Trail Arizona results (Nov 6-7, 2026). Only the tables
are kept, not the image's summaries or awards. Checked: every pace equals time / distance, and the 24 legs add up
to 120.40 mi and 17:57:06, matching the header.
