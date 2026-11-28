# Reading, reimagined: a shelf that knows his taste and works in a bookstore

Today: a "Reading list" project (project 17) whose description is a long Codex spec, plus structured books
(Assistant tools: assistant_reading_list, assistant_add_book, assistant_update_book, assistant_check_shelf) with
title, author ("Last, First"), ★ priority and status. Studio #35 asks for a guided flow instead of documents.

Intake: part of Assistant (books already live there). Its own section and pages inside Assistant, not a new app.

## What he gets

**His shelf**

- Every book moves through: **want** → **found** (bought, not started) → **reading** → **read** (rating 1–5 and a
  line of thoughts) → optionally **passed on** (lent, gave away). Plus **not for me** for picks he turned down,
  so they're never suggested again.
- "Currently reading" shows on Today.

**Guided picks** (Claude, ChatGPT or Ask Dieter)

- "I want something like The Fox Wife", "a short weird book for a flight", "more like VanderMeer but warmer".
- The assistant asks 3–4 questions at most (mood, length, what to avoid, new author or favourite), then proposes
  five books, each with one line on why it fits *him* (drawn from what he rated highly), and checks the shelf so
  it never proposes something he owns, read or turned down.
- Proposals wait for his OK, like the Logbook and Media: keep → **want** (★ if he says so), skip → **not for me**.
  No more documents to regenerate.

**Bookstore mode** (phone, one page)

- Authors A–Z by last name, ★ titles first under each author, with jump letters for the shelves. (The spec in
  project 17, kept.)
- Tap an author: "checked that shelf this trip" (not "found"); it resets when he starts a new trip.
- "Found it" on a title moves it to **found**.
- Scan a barcode (the phone camera, in the browser) to add or mark a book he's holding.
- Works with a weak signal: the page loads the whole list once.

**His taste, over time**

- Ratings and thoughts become a small taste profile (authors, themes, length, what he dislikes) that assistants
  read before suggesting. He can see and edit it.

## Data (additive, Assistant store)

Existing book rows gain: `found_at`, `started_at`, `finished_at`, `rating`, `thoughts`, `passed_on_to`, and the
`not_for_me` status. New: `reading_trips` (started, checked authors) and `reading_proposals` (book, reason,
asked-for, status).

## Connector tools

Read: shelf, bookstore list, taste profile. Propose: picks (with reasons), status changes. He approves picks;
assistants can mark "found" or "checked VanderMeer" when he says so in the chat.

## Order of work

1. This plan, his OK.
2. Statuses, ratings and the shelf page; Today card.
3. Bookstore mode with trips and barcode add.
4. Proposals: the guided-picks tools and approve/skip on the shelf page and Today.
5. Taste profile.
6. Retire project 17's spec text (keep it as a note) and close Studio #35.
