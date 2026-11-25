# Trainer, reimagined: a coach that plans the week and makes sure it happens

Today's Trainer is a log: Strava imports, mileage charts, shoes, coach/athlete grants, workouts a coach assigns.
It lives in the old shared app (`api.py` routes from `/apps/trainer`, `trainer_*` tables in `database.py`,
`apps/trainer/templates/trainer.html`) and tells Curtis what he already did.

The new Trainer looks forward. It plans his running week, makes sure strides and strength work actually happen,
puts each session on his schedule and reminds him, then checks the week against what Strava recorded. Coaching
roles stay; they stop being the centre of the app.

Intake: an existing app (Trainer) rebuilt in its own folder, new-style pages. The sprint/intensity-load capstone
(project 11) stays a separate study that reads Trainer's data; Trainer doesn't grow into it.

## His week right now (Nov 25, 2026; all of it subject to change, so everything below is editable)

- About **40 miles a week**.
- **Hard workout Tuesday afternoon.**
- **1–2 weight workouts** and **1–2 Pilates sessions** a week.
- **Hydration and fueling** tracked as daily habits, not afterthoughts.
- A **Sunday night check-in**: look back at the week, set next week's goals and sessions.
- It has to be **flexible**: the plan bends to his week; moving or swapping a day is one tap, and the totals
  (miles, strength, Pilates, strides) rebalance around it.

## What he gets

**This week**, the home page:

- The week as seven days: each day's session (easy 6, threshold 4×1600, long run, rest), with strides and
  strength as their own small checkable items, not buried in notes.
- What actually happened, from Strava, next to what was planned: done, short, swapped, missed.
- One line of guidance: "Threshold went well at 5:57 average; next one aims for 5:55." "Strides missed twice this
  week; add them to tomorrow's easy run."

**Plan**: a training block toward a goal.

- The goal is a race or a target ("5K under 16:00", "threshold toward 5:40/mi"), with his current marker and a date.
- A block is weeks of a repeating pattern (e.g. easy days, one threshold day, one long run, strides twice, strength
  twice, a rest day), with mileage that builds and a lighter week every few. He sets the pattern; Dieter fills in
  the dates and adjusts. No hidden algorithm: every change shows why.
- Built-in session types: easy, long, threshold/tempo, intervals, race, rest, strides (on the end of an easy run),
  strength (weights), Pilates, mobility. Sessions carry targets (distance or time, pace or effort, reps × distance, rest).

**Sunday night: set the week**

- A short guided check-in on Sunday evening (a reminder through the calendar feed, later a phone notification):
  last week's miles vs. ~40, the Tuesday workout, weights and Pilates done, how fueling and hydration went.
- Then next week: confirm or change the miles target, place the Tuesday hard session, pick the weights and
  Pilates days, and set one or two goals for the week ("strides twice", "fuel before Tuesday's workout").
  Dieter drafts it from his usual pattern; he adjusts and confirms. Claude or ChatGPT can run the same check-in.

**Hydration and fueling**

- Daily, lightweight: water (glasses or bottles, one tap each) and fueling around key sessions (before Tuesday's
  workout, during long runs, after). Shown on Today and in the Sunday review. Targets are his own numbers; Dieter
  doesn't prescribe nutrition.

**Strides and strength, so they stick**

- Strides: "6 × 20 s, relaxed fast, full recovery" attached to easy runs, checked off in one tap. Dieter counts them
  per week, because Strava doesn't show them well (his own complaint).
- Strength: short routines (15–25 min: e.g. hips/glutes, calves/feet, core), with exercises, sets and reps, and a
  simple player (exercise, reps, done, next). His existing PT exercises (glute medius etc.) seed the library.
- Both count toward the week, and a missed one is carried forward once, not piled up.

**On his schedule and in front of him**

- Each session gets a time slot. The plan is published as a **calendar feed** (an `.ics` link he subscribes to once
  in Google or Apple Calendar), so sessions show up with the calendar's own alerts on his phone and watch. No
  calendar permissions to grant, no separate app. Moving a session in Dieter moves it in the calendar.
- **Today** (the launcher) shows today's session first, with the strides/strength checkboxes.
- The **kitchen screen** shows Today when it's idle (the helper already keeps that screen alive), so the plan is
  always in view at home. See "Always on" below.
- Morning nudge and evening check-in: later, as phone notifications (below).

**After the run**

- Strava sync happens on its own (Strava's webhook, or a check every few hours), not by pressing a button.
- A finished run is matched to the planned session; he only confirms or says "that was the threshold session."
- He can add a line: how it felt (1–5), anything sore. Two "sore" days in a row on the same spot → Dieter suggests
  an easy day and says why. It never prescribes treatment.

**Ask about it** (Claude, ChatGPT, Ask Dieter, through connector tools)

- "What's my week?", "Move Thursday's threshold to Friday", "I missed strides yesterday", "Plan a 10-week block to
  a 5K in March", "How did my threshold sessions trend?" Assistants propose plan changes; he approves them, the
  same rule as the Logbook. Reading data is free; changing the plan is a proposal.

## Always on, and alerts

In order of effort:

1. **Calendar feed** (above): reminders through the calendar he already uses. Smallest build, works everywhere.
2. **Kitchen screen**: Today as the idle screen on the kitchen PC, with today's run and the next calendar item;
   Dieter (the mascot) asleep until someone walks by. Uses the existing helper and sleep/wake.
3. **Install Dieter on the phone** (it's a web app: "Add to Home Screen") and **web push notifications**: a morning
   "today: easy 6 + strides", an evening "did you do strength?". Needs a service worker, push keys in Secret
   Manager and a small scheduler (Cloud Scheduler hitting an endpoint). Works on Android; on iPhone only after
   adding to the home screen.
4. **Through Claude**: in the Claude phone app, Claude can already put events and alarms on his phone directly;
   handy for one-offs, not a replacement for the feed.

## Data (additive, `apps/trainer/store.py`)

| Table | Holds |
| --- | --- |
| `tr_goals` | target (race/pace), date, current marker, status |
| `tr_blocks` | goal, start, weeks, weekly pattern (JSON), deload rule |
| `tr_sessions` | date, slot time, type, targets (JSON), notes, status planned/done/skipped/moved, matched activity id |
| `tr_extras` | strides/strength items on a session: kind, routine id, prescription, done |
| `tr_routines` / `tr_exercises` | strength routines and their exercises (sets, reps, cues) |
| `tr_checkins` | per day: feel 1–5, soreness spots, water, fueling notes, note |
| `tr_weeks` | the Sunday check-in: miles target, goals for the week, review of the last week |

Existing `trainer_imported_workouts`, shoes and coach grants stay where they are and are read through a small
adapter, then move into the app's store when touched (CLAUDE.md: move code out of `api.py` as you go).

## Order of work

1. **Plan doc** (this) and his OK on the shape.
2. **New-style shell** for Trainer (`core/shell`, design-system macros), routes moved out of `api.py`, existing
   Strava/shoes/coach features carried over unchanged. Tests.
3. **Week + sessions**: the This-week page, sessions with targets, strides/strength/Pilates as items, hydration and
   fueling taps, Today card, and the Sunday check-in.
4. **Calendar feed** (`/trainer/calendar/<secret>.ics`, revocable link).
5. **Plans and blocks**: goal, pattern, generating weeks, moving/skipping with carry-forward.
6. **Strength routines** and the exercise player; seed his PT exercises.
7. **Auto Strava sync and matching**; check-ins.
8. **Connector tools** (read + propose), Ask Dieter on Trainer pages.
9. **Phone notifications** (web push) and the kitchen idle screen.

Steps 2–4 make it useful on their own. The capstone study (project 11) can start reading sessions after step 7.

## Open questions

- Known so far: ~40 mi/week, hard workout Tuesday afternoon, 1–2 weights, 1–2 Pilates, Sunday night check-in.
- Still to ask: usual times for easy runs, weights and Pilates (calendar slots); long-run day; race goal and date.
- Keep coach/athlete features prominent, or tuck them into Settings for now?
