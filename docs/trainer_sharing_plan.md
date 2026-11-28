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

## Coach mode (built: `apps/trainer/coach.py`)

Replaces the older coach code in `api.py` (`trainer_coach_grants`, `/apps/trainer/coach`, `/apps/trainer/coaching`),
which showed a coach the athlete's imported Strava runs and let any granted coach assign workouts. Those pages are off
the menu; retire the routes and table once nothing uses them.

- **Anyone can be a coach.** No athlete/coach account type: an athlete makes an invite link on **My coach**, the
  coach opens it and accepts. A coach's athletes are on **My athletes**; a coach still has their own Trainer.
- **The coach writes the workout in plain terms:** sets of reps × distance at an effort (easy, threshold, 10K, 5K,
  3K or mile pace, sprint, by feel) with rest, a day, a title and notes. "6 × 1 mile at threshold, 90 s jog;
  8 × 200 m sprint".
- **Dieter fills in the athlete's times, for the athlete only**, from the athlete's own speed model (formulas, no
  AI): threshold = critical speed; race paces from d = CS·t + D'; sprint = 90% of top speed; easy = easy pace.
  Shown as a time per rep and a range (± 1.5%).
- **Suggestions, not orders.** A workout waits on My coach until the athlete taps "Put it on my plan" (it becomes a
  workout session with their times in the details) or declines it. The coach can take back one that's unanswered.
- **What the coach sees:** what they wrote, its status (waiting, on their plan, done, declined) and the note the
  athlete chooses to write. Never the athlete's runs, paces, target times, or anything else in Dieter. The coach
  pages don't read runs at all, so Strava's "only to that user" rule holds.
- **Either side can stop** at any time; it ends at once.

- **Temporary full view (for building this with Daniel).** Only Dieter's owner can switch it on, per coach, for
  30 days (`coach.FULL_VIEW_DAYS`); it switches itself off, and stopping coaching ends it. The coach taps "View ..."
  and sees the owner's Trainer pages as the owner, read-only (forms are refused), with a banner and a Stop viewing
  button. This shows the owner's Strava runs to another person, which Strava's terms restrict: the owner's own
  call about their own data, never offered to other accounts. Remove it once the real coach view exists.

Later: the coach seeing more, but only what the athlete switches on (weekly miles they log, injury check-ins) and
never Strava-sourced numbers unless Strava confirms coaching use; notes both ways on a workout; a coach's AI reading
shared, non-Strava data if the athlete allows it.

## Checking it works

- A non-owner account asking Claude or Dieter's chat about Trainer gets the refusal, not numbers
  (`tests/test_trainer_strava.py`).
- A friend account sees only its own runs and none of Curtis's household apps.
- A coach without a link gets a 403; after either side stops, access ends at once.
- The coach's pages never contain the athlete's times or paces (`tests/test_trainer_coach.py`).
- Shared views never contain a run whose source is `strava`.
