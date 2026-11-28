# Capstone: Sprint / Intensity Load Score

Dieter project 11. Reviewed with `docs/review_process.md`. This file is the brief (steps 1 and 2): Curtis signs
it off before data work starts. Numbers marked *(proposed)* are his to confirm or change.

## Brief

- **Question**: Does Strava's Relative Effort underrate short, intense sessions (strides, hill sprints, short
  reps) compared with how hard they are on the legs, and can a simple score from the activity streams do better?
- **Claim (if it holds)**: A rule-based Sprint / Intensity Load Score, built from pace, grade and HR streams,
  tracks next-day leg fatigue after short intense sessions better than Relative Effort or an HR-only load score.
- **Deliverable**: the score inside Dieter Trainer (on the run page and in the weekly load), plus a short
  write-up: method, results, charts, limitations.
- **Out of scope**: replacing TrainingPeaks or Relative Effort, whole-training-load modelling, injury
  prediction, cycling power (no bike data yet), any machine learning on Strava data (see Data rules).

## Data rules (Strava terms)

Data comes from Strava for now. The write-up stays within Strava's API Agreement:

- **No model training on Strava data.** The score is hand-built rules, not a trained model. (The November 2024
  agreement banned using Strava data for AI/ML training. The current version, effective June 1, 2026, should be
  checked for that clause again before publishing. Either way, we don't train on it.) Machine learning for the
  residency uses other data (calcium imaging, COROS exports).
- **Strava data is only shown to Curtis inside Dieter.** Per-run charts, maps and streams stay in Dieter.
- **The public write-up publishes method, code and summary results**: counts, correlations, effect sizes, and
  charts of those summaries. No raw streams, routes, maps or per-activity tables. Example workouts are described
  in words or drawn from a COROS/watch export of the same session, not from Strava.
- **It's a complement, not a copy.** The score measures what Relative Effort leaves out. It doesn't recreate
  Strava features.
- Before anything is published, the agreement is re-read and the write-up checked against it (review step 4).

## Pre-registration

**Data**

- Runs from Strava (pace, HR, elevation/grade, cadence, laps), kept in Dieter's training log. Relative Effort is
  the `suffer_score` field already stored.
- **Session labels** *(retrospective)*: Curtis labels 30-50 past runs: easy, long, tempo, threshold, strides,
  hill sprints, short reps, track reps, race. Labels come from his memory and the workout title or laps, and are
  given before he sees any Dieter score.
- **Next-day legs** *(prospective)*: after each run, Dieter asks "how are your legs today?" (0-10) the next
  morning, like the shoes prompt. Target: 40 rated days *(proposed)*, about 6-8 weeks. Old runs can't be rated
  reliably from memory, so the fatigue result rests on these new ratings.

**Split**

- Detection rules are designed on the first half of the labelled runs (by date), then frozen.
- They are scored once on the second half. Next-day ratings are only analysed after the score is frozen.

**Hypotheses and measures**

| | Hypothesis | Measure | Counts against it |
| --- | --- | --- | --- |
| H1 | The rules find the hard efforts (strides, reps, sprints) | Per-effort recall and precision against the laps and labels on held-out runs | Recall or precision below 0.85 *(proposed)* |
| H2 | Relative Effort underrates short intense sessions | Relative Effort per minute: short intense sessions vs easy runs of similar length | Short intense sessions already score clearly higher |
| H3 | The new score tracks next-day legs better | Rank correlation with next-day legs: new score vs Relative Effort vs HR-only load (TRIMP) vs duration | New score no better than the best baseline |

The baselines include Dieter's own HR load score and plain duration, not just Strava's number, so the comparison
isn't set up to win.

**Acceptance checks** *(proposed)*

1. H1 holds on held-out runs (recall and precision ≥ 0.85).
2. H3: the new score's correlation with next-day legs beats the best baseline, with the interval reported. If it
   doesn't, the write-up says so and narrows the claim.
3. Every number and chart in the write-up is rebuilt by one command from the stored data.
4. The write-up passes the Strava data rules above (reviewer checks).
5. The score shows on the run page and in weekly load, and the 18x400 track session (Nov 28, 2026) is flagged as
   high leg load.

## Score v1

From each run's streams, after smoothing GPS pace and adjusting for grade:

- **Hard efforts**: stretches above a personal threshold (pace or grade-adjusted pace above ~threshold pace,
  with a fast rise), 6 s to 3 min long. Strides, reps and sprints all fall out of this.
- **Outputs**: number of hard efforts, total time hard, peak effort length, time above mile pace, and a
  **leg load** number. Leg load weights each effort by how far above threshold it is, so ten 20-second strides
  count for more than their few minutes suggest.
- **HR lag**: short efforts don't raise HR much, which is why HR-based scores miss them. Effort detection uses
  pace, not HR.
- **Next-day warning** when leg load is high, compared with his own recent sessions.

Weights are set on the design half only, then frozen.

## Steps

1. Curtis signs off this brief (acceptance checks and targets).
2. Add the next-day legs prompt, so ratings start building now.
3. Curtis labels past runs (a quick labelling page in Trainer).
4. Build detection and the score on the design half, then freeze.
5. Score the held-out half and, once there are enough ratings, run the H2/H3 analysis.
6. Write up: method, results, limitations, data note.
7. Independent review, fixes, Curtis's sign-off (review process steps 4-6).

## Review log

| Date | Step | Who | Outcome |
| --- | --- | --- | --- |
| 2026-11-28 | Brief drafted | Claude | Waiting for Curtis's sign-off |
