# Review process: capstone, research and consulting work

How Curtis reviews substantial work that Claude (or anyone) does for him: the Dieter capstone, research
write-ups, and consulting deliverables. Small fixes (a colour, a button) don't need it: the pull-request preview
is the review.

The idea: agree what "done" means before the work starts, make every claim checkable, have someone who didn't
do the work try to break it, then a short sign-off by Curtis. Each step leaves a written record.

## 1. Brief (before any work)

One page, signed off by Curtis before work starts. Changes later are allowed, but are written down with the date
and reason (section 6).

- **Question**: what we're trying to find out or deliver, in one sentence.
- **Claim or deliverable**: what the finished work will say or be.
- **Out of scope**: what it deliberately won't do.
- **Acceptance checks**: 3-5 checks that decide whether it's done. Each one is pass/fail and checkable by
  someone else ("the score ranks the labelled hard sessions above Strava's on at least 15 of 20"), not a
  feeling ("the score is useful").
- **Constraints**: data rules (terms of use, permissions, privacy), budget, deadline.

## 2. Pre-registration (research only)

Written into the brief before any results are looked at:

- **Hypothesis** and the **baseline** it is compared against.
- **Measures**: exactly what is computed, on which data.
- **What would count against it**: the result that would make us drop or narrow the claim.
- **Data split**: which data is used to design the method and which is held back to test it. Held-back data
  isn't looked at until the method is frozen.

If the plan changes after seeing data, that's fine, but the change is logged and the result is reported as
exploratory, not confirmatory.

## 3. Evidence pack (with the deliverable)

- Every number, table and chart in the write-up links to the code and data that make it, and one command
  re-makes them.
- A **limitations** section: what the work can't show, where it might be wrong, and what would change the answer.
- A **data note**: where the data came from and why using and showing it is allowed.

## 4. Independent review

A reviewer who did not do the work gets only the brief and the deliverable (not the working conversation) and
tries to break it:

- Re-runs the numbers from the evidence pack and checks they match.
- Checks each acceptance check against the brief, pass or fail, with the evidence.
- Looks for the usual failures: held-back data leaking into design, cherry-picked examples, a baseline set up to
  lose, conclusions bigger than the evidence, missing limitations, terms-of-use problems.
- Offers the best alternative explanation for the main result.

The reviewer is a fresh Claude agent by default. For high-stakes work, add a second reviewer from a different
model (ChatGPT) or a person. Findings come back ranked: **blocking** (must fix), **should fix**, **note**.
Every blocking finding is fixed or answered in writing before step 5.

## 5. Curtis's sign-off (about 10 minutes)

1. Does it answer the question in the brief?
2. Did every acceptance check pass? (Read the reviewer's table.)
3. Would I bet on the main claim? If not, what would I need to see?
4. What's the weakest part, and is it stated in the limitations?
5. Would someone outside the project follow it?
6. Would I put my name on it?

Outcome: **accept**, **accept with changes** (listed), or **back to work** (with the reason).

## 6. Review log

Each project keeps a short log in its folder or Dieter project notes: date, step, who, outcome, what changed.
For research, the log is also evidence of process for applications and portfolio write-ups.

## For consulting work

Same steps, lighter where it fits:

- The brief (1) is agreed with the client, and the acceptance checks are theirs.
- Pre-registration (2) only applies when the work makes an empirical claim (a forecast, a model, a comparison).
- The evidence pack (3) respects the client's data rules; what can't leave their systems is described, not copied.
- Independent review (4) and sign-off (5) always happen before anything goes to the client.
