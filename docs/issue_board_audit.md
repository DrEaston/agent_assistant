# Studio Issue Board Audit

Read-only triage generated from a copied Dieter SQLite database.

## Snapshot

- Total issues: 25
- Active issues: 7
- Closed issues: 18
- Ready for testing: 2

## Recommended Queue

### P0 - #32 Add home page prompts for starting AI projects

- Area: Assistant / Planner
- Status: in_progress; latest run: running
- Recommendation: Leave running; review after the worker finishes.
- Feedback: When you hit the app home page there are no action buttons. We need some ai prompts to to different tasks. Like I need to start a new project - res...

### P1 - #27 Lighten Ask Dieter button to match app headers

- Area: Assistant / Planner
- Status: ready_for_review; latest run: ready_for_testing
- Recommendation: Manual smoke test, then close if the behavior is correct.
- Feedback: Ask dieter button should be a little lighter than main banner. Like all the other apps

### P1 - #28 Remove issues banner from planner app

- Area: Assistant / Planner
- Status: ready_for_review; latest run: ready_for_testing
- Recommendation: Manual smoke test, then close if the behavior is correct.
- Feedback: There is an issue displayed at the top of the planner app. Issues should be on the issues page only

### P1 - #33 Automated evaluation for stalled pipelines

- Area: Kitchen / Recipes
- Status: in_progress; latest run: failed
- Recommendation: Use as the canonical issue-board audit/process improvement.
- Feedback: I need an automated system for evaluating stalled pipelines. So you will go read the status, extrapolate what I'm trying to do, and do some testing

### P2 - #29 Consolidate EEG and calcium imaging issue categories

- Area: Issues
- Status: in_progress; latest run: failed
- Recommendation: Keep, but condense into one cross-repo onboarding/category cleanup issue.
- Feedback: I don't need 3 separate eeg issues or calcium imaging issues in the issues app

### P2 - #30 Hide demo mode bar when not in demo mode

- Area: Dieter
- Status: in_progress; latest run: failed
- Recommendation: Fold into one demo-mode taxonomy/safety issue unless it is already fixed.
- Feedback: The demo mode bar is up even when I'm not in demo mode

### P2 - #31 Add separate issue type for demo mode

- Area: Dieter
- Status: in_progress; latest run: failed
- Recommendation: Fold into one demo-mode taxonomy/safety issue unless it is already fixed.
- Feedback: Demo mode needs to be it's own type of issue

## Condense Candidates

- #30, #31: Hide demo mode bar when not in demo mode; Add separate issue type for demo mode

## Standard Review Procedure

1. Pull a read-only DB snapshot while no schema migration is running.
2. Separate `running` issues from triage; do not edit them until the worker finishes.
3. Close or test `ready_for_review` issues before approving new work.
4. Retry failed runs only after classifying the failure as infrastructure, plan, test, or implementation.
5. Merge same-theme issues into the clearest parent issue and close/delete accidental duplicates.
6. Keep at most one P0/P1 Codex run active; leave speculative project/category cleanup as P2/P3.
7. Save the audit note, then queue the next issue from the top of the recommended queue.
