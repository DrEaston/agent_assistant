"""The coach writes a workout in plain words; AI turns it into sets Trainer can use.

AI only does the reading: "6x mile @ T w/ 90s jog, then 8x200 fast" becomes
[{reps: 6, metres: 1609, intensity: "threshold", rest: "90 s jog"}, {reps: 8, metres: 200, intensity: "sprint"}].
The paces are never the AI's: each athlete's page works them out from those sets with the speed formulas
(coach.targets, from the athlete's own speed model). What the AI returns is checked by coach.clean_sets like
anything a coach typed, and anything it can't turn into a set (warm-up, cool-down, drills) is kept as notes.
"""

import json
import os
import re

from llm_provider import get_llm_provider

from . import coach

MAX_TEXT = 1500

PROMPT = f"""You read a running coach's workout and turn it into JSON for a training app. Reply with JSON only:
{{"title": short name for the workout (max 60 characters),
  "sets": [{{"reps": whole number, "metres": rep distance in metres, "intensity": one effort below,
            "rest": the recovery between reps in a few words, or ""}}],
  "notes": everything else the athlete should know (warm-up, cool-down, drills, cues), in the coach's words, or ""}}

Efforts (use exactly these words):
- "easy": easy, recovery, jog
- "threshold": threshold, tempo, T pace, LT, lactate threshold
- "10k", "5k", "3k", "mile": that race pace (1500 pace or mile pace = "mile"; CV = "10k")
- "sprint": strides, sprints, fast, all out, max, flys
- "feel": anything else, or no effort given

Rules:
- One set per repeated block, in order. "6 x 1 mile" is reps 6, metres 1609. A single run ("2 mile tempo") is reps 1.
- Distances: 1 mile = 1609, 2 miles = 3218, 1 km = 1000; "200s" = 200 metres. Between 50 and 20000 metres.
- A block by time or by feel without a distance (e.g. "3 x 10 min tempo", "hill sprints") is not a set: put it in notes.
- At most {coach.MAX_SETS} sets. Don't invent anything the coach didn't write; never add paces or times."""


def _json(text):
    """The first JSON object in the model's reply."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("no JSON in the reply")
    return json.loads(match.group(0))


FIX_PROMPT = PROMPT + """

You are now changing a workout you already read. The user message has the workout as JSON and the coach's change.
Apply only that change and keep everything else exactly as it is. Reply with the whole updated workout as JSON in
the same shape."""


def _ask(system, content, provider, what):
    try:
        provider = provider or get_llm_provider(model=os.getenv("TRAINER_READER_MODEL") or None)
        return _json(provider.chat([{"role": "user", "content": content}], system, max_completion_tokens=800))
    except Exception as exc:
        print(f"Warning: the workout reader failed ({what}): {exc!r}")
        raise coach.Invalid("Dieter couldn't read that just now. Try again in a minute.") from None


def _checked(data):
    """The AI's JSON, checked like anything a coach typed: {title, sets, notes}."""
    raw_sets = data.get("sets") if isinstance(data.get("sets"), list) else []
    rows = [dict(s, intensity=s.get("intensity") if s.get("intensity") in coach.INTENSITIES else "feel")
            for s in raw_sets if isinstance(s, dict)][:coach.MAX_SETS]
    if not rows:
        raise coach.Invalid("Dieter didn't find any reps with a distance in that, like 6 x 1 mile or 8 x 200 m.")
    sets = coach.clean_sets(rows)
    title = " ".join(str(data.get("title") or "").split())[:120]
    notes = " ".join(str(data.get("notes") or "").split())[:500]
    return {"title": title, "sets": sets, "notes": notes}


def read(text, provider=None):
    """{title, sets, notes} from the coach's words. Raises coach.Invalid with a plain message when it can't."""
    text = " ".join(str(text or "").split())[:MAX_TEXT]
    if not text:
        raise coach.Invalid("Write the workout first, like: 6 x 1 mile at threshold, 90 s jog; 8 x 200 m fast.")
    return _checked(_ask(PROMPT, text, provider, "read"))


def revise(workout, change, provider=None):
    """The workout on screen with the coach's spoken or typed change applied ("make the rest 60 seconds")."""
    change = " ".join(str(change or "").split())[:500]
    if not change:
        raise coach.Invalid("Say or type what to change, like: make the rest 60 seconds.")
    content = json.dumps({"workout": {"title": workout.get("title", ""), "sets": workout.get("sets", []),
                                      "notes": workout.get("notes", "")}, "change": change})
    return _checked(_ask(FIX_PROMPT, content, provider, "fix"))
