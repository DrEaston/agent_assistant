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

- **Strides and stride speed (built 2026-11-25, `apps/trainer/speed.py`).** Each run's second-by-second speed from
  Strava is read once (one extra request per run; results kept in `tr_run_speed`, not the stream). Strides are bursts
  of 6-60 s above max(1.4 x the run's usual speed, usual + 1 m/s); each keeps its best 5 s. Planned strides that day
  are ticked. Stride speed (the middle of a week's stride top speeds) is the Overview's "getting speedier" number:
  last 4 weeks against the 8 before, in seconds per mile.

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
