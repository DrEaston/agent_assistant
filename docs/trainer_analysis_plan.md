# Trainer analysis: injuries, fitness and speed over time

Builds on Trainer's own training log (`apps/trainer/strava.py`: every session from Strava and entered in Dieter,
kept with full detail). The Log page shows weekly and monthly totals, not every session; analysis reads the log.

Curtis's goals: break 16:00 for 5K, bring threshold pace from ~5:59/mi toward 5:40/mi, ~40 mi/week, stay healthy.

## New tables (additive, `apps/trainer/`)

| Table | Holds | Filled by |
| --- | --- | --- |
| `tr_injuries` | area, side, kind (niggle, strain, tendon, bone), started_on, resolved_on, worst pain 0–10, diagnosed by, what he thinks caused it, status | him, in chat or on a page |
| `tr_body_checks` | date, area, pain at rest / during / after (0–10), effect on training (none, modified, skipped), note, injury id, who logged it (claude, chatgpt, dieter, him) | chat ("hip's a 3 after today's run"), quick form |
| `tr_rehab` | exercises for an injury, linked to strength sessions in the plan; done dates | plan + check-offs |
| `tr_daily_load` | per day: run miles, minutes, hard-effort minutes (HR above threshold), an HR-based load score (TRIMP-style), strides, strength and Pilates sessions | computed from the log, nightly and on new activities |
| `tr_efforts` | each lap, rep and best effort: distance, time, pace, HR, context (workout, race, strides, long run) | Strava laps and best efforts in activity details |
| `tr_markers` | per week: easy pace at a fixed HR (aerobic efficiency), threshold pace (Tuesday laps), HR drift on long runs, fitness (42-day load), fatigue (7-day load), form (fitness − fatigue) | computed weekly |

Derived numbers are labelled as estimates wherever they are (load score, 5K estimate, fitness/fatigue).

## Injuries over time

- **Timeline**: each injury drawn over weekly load, so the weeks before each flare-up are visible.
- **Talk it through**: Claude/ChatGPT ask about known trouble spots after hard sessions and log a body check
  (connector tools: `trainer_log_body_check`, `trainer_injuries`, `trainer_load_summary`). Rising pain across checks
  is called out.
- **Recurrence, honestly**: a handful of past injuries is too few for a real prediction model. Start with personal
  warning signs compared with his own history: weekly mileage ramp (> ~10–15%), the last 7 days well above the
  42-day average, back-to-back hard days, missed strength sessions, shoe mileage, pain trend in body checks. A
  "watch" card on Today when several line up ("before both hip flare-ups, mileage rose 25% in 3 weeks; you're at
  +18%"). Becomes a model (logistic or time-to-event) only once there are enough injuries and check-ins.
- Never medical advice: rising pain → see a physio.

## Fitness progression

- Fitness / fatigue / form from daily load (Banister-style), shown weekly.
- Aerobic efficiency: easy-run pace at the same HR, trend over months.
- HR drift (decoupling) on long runs.
- Weekly volume: miles, time, strides done, strength and Pilates.

## Speed progression

- **Speed measures (built 2026-11-25, `apps/trainer/speed.py`).** Each run's streams (time, speed, distance) are read
  once from Strava and kept as a summary in `tr_run_speed`: fast stretches (6 s-8 min above the run's usual speed,
  named stride / 200 / 400 / ... / mile / rep), best efforts 400 m-5K, best 5 s, and a 0.1 m/s speed histogram so
  scores can be recomputed when critical speed changes. Weekly: critical speed and D' (linear distance-time fit over
  2-20 min bests in the last 26 weeks), speed work = D' used (sum of (v - CS) x t above CS, also in "tanks"),
  top-speed running (metres at >= 90% of best 5 s), stride speed. Why not heart rate or rTSS: HR lags short reps,
  and rTSS's squared weighting isn't derived; the critical speed model is the established basis for "faster counts
  more". The plain two-parameter model is used with no sprint correction (published corrections disagree). Planned
  strides are ticked when found. Runs from the last 52 weeks are read a few per Trainer visit and 25 per Sync now.
  **Critical speed, by itself** (2026-11-25: the first version read 6:47 from reps-plus-jogs): best efforts must be
  steady; a run within 1% of a standard distance counts as it (GPS 5K = 4,997 m); CS from the best 10-40 min effort
  with a typical D' of 150 m, or from that plus a hard 2-7 min effort when the pair gives D' 80-400 m. Race times on
  Usual week are an optional override.
  **The line** (Curtis, after 18 x 400 in 81-83 s with 30 s rest hurt): each week's speed work and top-speed running
  against 1.5 x the average of the 4 weeks before (acute:chronic workload ratio; debated, shown as a guide), floors
  2 x D' and 500 m. A sudden block goes over; building up over a few weeks keeps under.

- Threshold pace from Tuesday's workout laps toward 5:40/mi.
- Best efforts over time (400 m, 1 km, mile, 5K) from Strava best efforts and races.
- Rep pace vs. HR for the same session type.
- A 5K estimate from recent workouts and races, labelled as an estimate, tracked toward sub-16.

## Research angle (OpenAI Residency 2027)

An n-of-1 study with hypotheses written down before looking, e.g. "a weekly mileage ramp over 15% raises my body-check
pain scores the following week", tested on the log and checks as they accumulate.

## Order of work

1. Injuries and body checks, with the connector tools (chat logging first).
2. Daily load, efforts and weekly markers, computed from the log; fitness and speed charts.
3. Warning signs and the Today "watch" card.
4. Hypotheses and the study write-up.
