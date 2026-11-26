"""Trainer pages Dieter can open (core/connector/pages.py)."""

from core.connector.pages import Page

PAGES = [
    Page("trainer.home", "Trainer", "This week at a glance, weekly miles, fitness, and the Speed section (recent runs, "
         "speed points, threshold pace, safe limits, week by week).", "/apps/trainer"),
    Page("trainer.run", "Run", "One run: what was fast, its speed points, route map, speed chart, best steady efforts "
         "(run_id from trainer_speed or trainer_run).", "/apps/trainer/run/{run_id}", {"run_id": "number"}),
    Page("trainer.speed_how", "How these numbers work", "Plain-words explainer of threshold pace, speed points, the "
         "safe limit and near-sprint running.", "/apps/trainer/speed/how"),
    Page("trainer.speed_week", "Speed week", "One week's runs with their speed points and the week's safe limit "
         "(monday: that week's Monday).", "/apps/trainer/speed/{monday}", {"monday": "date"}),
    Page("trainer.week", "This week", "The planned week day by day: runs, strides, weights, Pilates.", "/apps/trainer/week"),
    Page("trainer.log", "Training log", "Every session from Strava and added by hand.", "/apps/trainer/log"),
    Page("trainer.usual", "Usual week", "His usual week, miles target and race times.", "/apps/trainer/usual"),
]
