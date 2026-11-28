# Demo mode, reimagined: "Meet Dieter", a public showcase

Today: a guest account ("Open Demo as Guest" on the sign-in page, a demo landing page, a Guest Mode banner,
`scripts/seed_demo_data.py`), threaded through about a dozen files. A guest signs in to a read-only copy of
household data. Curtis wants it gone for now.

## Step 1: remove guest mode (after Thanksgiving) - done Nov 28, 2026

- Remove the guest sign-in button, the demo landing page, the Guest Mode banner and the `guest` role checks; the
  sign-in page offers only real sign-in.
- Keep public pages that are public on purpose (the Thanksgiving menu and its dish pages, published clips later).
- Existing guest user rows stay in the database (additive rule) but can no longer sign in.
- One pull request, after Thanksgiving, because the Thanksgiving menu page reads the guest flag.

## Step 2 (later): "Meet Dieter", if he wants a showcase

Useful for the OpenAI Residency portfolio and for showing friends, without opening his own data:

- A public page at `/meet` with Dieter the mascot (animated, once the Animation app exists) giving a short tour:
  Kitchen, the Thanksgiving planner, Trainer, the Logbook, the ChatGPT + Claude connector.
- Each stop is a **live sandbox** with made-up data (a fictional household), reset every night, so visitors can
  click, check things off and ask Dieter questions safely.
- A short "how it's built" section: one connector shared by Claude and ChatGPT, propose-then-approve, the
  research questions from the Residency project.
- Separate from his account entirely: no guest sign-in, no shared tables.

Not scheduled; build it when the portfolio needs it.
