"""askdieter.ai/trainer: the public landing page for Dieter Trainer, and the made-up run its example card shows.

The example is invented (no one's real data, nothing from Strava): an easy road loop out to a track, 6 x 400 m with
jogs, and home. It is drawn with the same card as the real screenshot view, so the page shows exactly what a run
looks like in Dieter.
"""

import math
from datetime import date

from . import runpage, speed

EASY, THRESHOLD, TOP = 3.0, 4.55, 6.1  # m/s: about 8:56, 5:54 and 4:24 per mile


def _line(a, b, n=12):
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def _arc(cx, cy, rx, ry, start, end, n=16):
    return [(cx + rx * math.cos(start + (end - start) * i / n), cy + ry * math.sin(start + (end - start) * i / n))
            for i in range(n + 1)]


def _points(pts):
    return " ".join(f"{x:.0f},{y:.0f}" for x, y in pts)


def sample_run():
    """The example run, shaped like runpage.detail() so trainer_share_card.html can draw it."""
    segments = []                         # (heat step, points)
    segments.append((0, _line((70, 430), (70, 230)) + _line((70, 230), (215, 205))[1:]))
    cx, cy, rx, ry = 330, 205, 115, 62    # the track: 6 reps of half a lap, each followed by a half-lap jog
    for rep in range(6):
        start = math.pi + rep * 2 * math.pi
        segments.append((6 + rep % 3 // 2, _arc(cx, cy, rx, ry, start, start + math.pi)))
        segments.append((1, _arc(cx, cy, rx, ry, start + math.pi, start + 2 * math.pi)))
    segments.append((2, _line((445, 205), (445, 430))))
    segments.append((0, _line((445, 430), (70, 430), 20)))
    path = []
    for _, pts in segments:
        path += pts
    return {
        "id": "", "title": "Track Tuesday", "day": date(2026, 11, 24), "time": "06:15",
        "miles": 6.2, "moving": "52:40", "pace": "8:30",
        "groups": [{"text": "6 × 400 at 4:31/mi, 2:00 jog"}],
        "speed_work": 410, "cs_pace": speed.pace(THRESHOLD),
        "map": {"w": 520, "h": 470, "path": _points(path), "fast": [],
                "heat": [{"step": step, "points": _points(pts)} for step, pts in segments],
                "key": runpage.heat_key(EASY, THRESHOLD, TOP), "start": path[0], "finish": path[-1]},
    }


SAMPLE_WEEK = {"speed_work": 520, "work_line": 600, "work_over": False, "how_over": ""}
