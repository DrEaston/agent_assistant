# Trainer for friends, and coach mode

Curtis wants to open Dieter Trainer to a few friends, and later let a coach use his data. This plan sets the
guardrails first, so sharing never breaks the terms of the services the runs come from. Not legal advice: before
inviting anyone beyond a handful of friends, have Strava's (and each watch maker's) current terms read properly.

Intake: part of Trainer (`apps/trainer/`). The older coach/athlete code in `api.py` and `database.py`
(`trainer_coach_grants`, `/apps/trainer/coach`, `/apps/trainer/coaching`) is the starting point; move it into
`apps/trainer/` as it's touched.

## The rules (why)

Strava's API Agreement (Nov 2024, tightened June 2026):

- A user's Strava data may be shown **only to that user** on a third-party app (no public feeds, leaderboards or
  heatmaps of other people's Strava runs).
- Strava API data may **not** be used in AI models "or other similar applications".
- Apps should complement Strava's look and feel, not copy it.
- New Standard-tier apps need a Strava subscription; data can't be relayed through third-party intermediaries.

Garmin's personal API key is for one person's own data, not an app serving others. COROS's self-service OAuth
and runs uploaded as .FIT files have no Strava strings attached.

## Guardrails

1. **Where a run came from is always known.** Every session in the log keeps its `source` (`strava`, `dieter`;
   later `fit_upload`, `coros`, `garmin_ciq`). Derived numbers (speed points, threshold pace, heat map) inherit the
   sources they were computed from.
2. **No AI on Strava runs, except the owner's own account.** Trainer's connector tools (used by Claude, ChatGPT
   and Dieter's chat) answer only for Dieter's owner (an `admin` account). Everyone else gets a plain refusal
   explaining why. *Built first: `apps/trainer/connector.py` `ai_allowed()`.* Later, when other sources exist, the
   tools open up for friends but only over non-Strava runs.
3. **Formulas stay on.** Threshold pace, speed points, the safe limit, near-sprint metres, the heat map and the
   draft week are plain calculations, not AI. Friends get all of these on their own pages.
4. **Each person sees only their own runs.** Pages and queries are per signed-in user. Anything shared between
   people (a friends leaderboard, a coach's view) uses only non-Strava runs, unless Strava confirms in writing
   that the use is allowed.
5. **Household stays private.** Friends get a Trainer-only account: no Kitchen, Assistant, Logbook, Media or
   Studio, and no Today page with Curtis's household items.
6. **Privacy and costs.** A short note on what's stored and a "delete my data" button; injury notes count as
   health data. A monthly cap on AI use per account once AI is on for friends.

## Steps

1. **Guardrail 2 (done in this change):** Trainer's AI tools owner-only.
2. **Friend accounts:** invite codes (sign-up already takes a registration code and can hold accounts pending),
   Trainer-only access, a Trainer-first home, the privacy note and data deletion.
3. **.FIT upload and account-export import:** works for every watch, loads history, source `fit_upload`. Runs
   through the same speed and heat map formulas as Strava runs.
4. **COROS:** OAuth connection per person (source `coros`); workouts and strides pushed to the watch later.
5. **AI for friends, non-Strava only:** connector tools filter to non-Strava sources for non-owners; a per-account
   AI cap.
6. **Garmin Connect IQ app** (source `garmin_ciq`), if several friends wear Garmins.

## Coach mode

A coach is a Dieter account the athlete shares Trainer with (existing `trainer_coach_grants`: view, or view and
assign sessions; the athlete can revoke any time).

- **What the coach sees:** the athlete's week, sessions, the formulas (threshold pace, speed points against the
  safe limit), injury and body-check history if the athlete ticks "share injuries".
- **Strava runs:** showing an athlete's Strava data to their coach is "showing it to another person". Until Strava
  confirms coaching is allowed, the coach view uses only non-Strava runs (upload, COROS, Connect IQ) and otherwise
  shows what Dieter itself holds (the plan, check-ins, notes). Ask Strava's developer team before switching it on.
- **AI for the coach:** a coach's Claude or ChatGPT can read the athlete's non-Strava runs and plan only if the
  athlete allows it on the share; never Strava runs.
- **Coach actions:** assign or move sessions on the athlete's plan (shows as "from your coach"), leave notes on a
  run. Changes the athlete hasn't seen are marked.
- **Audit:** the athlete sees who looked at or changed what.

## Checking it works

- A non-owner account asking Claude or Dieter's chat about Trainer gets the refusal, not numbers
  (`tests/test_trainer_strava.py`).
- A friend account sees only its own runs and none of Curtis's household apps.
- A coach without a grant gets a 403; after revoking, access ends at once.
- Shared views never contain a run whose source is `strava`.
