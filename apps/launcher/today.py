"""Today: the home page. One glance at what matters now, from every app.

api.py gathers the pieces (it still owns most app data) and calls render(); this module only shapes and shows them.
"""

from datetime import date, datetime
from pathlib import Path

from fastapi.responses import HTMLResponse

from core import ui

APP = {"id": "today", "title": "Dieter", "nav_label": "Today", "accent": "today"}
_templates = ui.templates(Path(__file__).parent / "templates")


def greeting(now):
    if now.hour < 5 or now.hour >= 21:
        return "Good evening"
    if now.hour < 12:
        return "Good morning"
    if now.hour < 17:
        return "Good afternoon"
    return "Good evening"


def long_date(day):
    return f"{day.strftime('%A, %B')} {day.day}"


def thanksgiving_card(plan, dishes, today):
    """Days to go and dinner time while Thanksgiving is coming up; None once it's past or with no plan."""
    if not plan or not plan.get("dinner_at"):
        return None
    try:
        dinner = datetime.fromisoformat(str(plan["dinner_at"])[:19])
    except ValueError:
        return None
    days = (dinner.date() - today).days
    if days < 0:
        return None
    when = dinner.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    if days == 0:
        countdown = "Today's the day"
    elif days == 1:
        countdown = "Tomorrow"
    else:
        countdown = f"{days} days to go"
    return {"countdown": countdown, "when": f"{dinner.strftime('%A')} · {when}", "is_today": days == 0,
            "dishes": len(dishes or [])}


def miles(meters):
    try:
        return f"{float(meters) / 1609.34:.1f} mi"
    except (TypeError, ValueError):
        return ""


def short_date(value):
    try:
        when = date.fromisoformat(str(value)[:10])
    except ValueError:
        return ""
    return f"{when.strftime('%b')} {when.day}"


def render(*, user_name, now, thanksgiving=None, planned_meals=(), shopping=None, todos=(), focus=None,
           last_workout=None, playlist_drafts=0, is_guest=False, report_url=None, studio=None, logbook=None,
           media=None):
    workout = None
    if last_workout:
        bits = [short_date(last_workout.get("started_at")), miles(last_workout.get("distance_meters"))]
        workout = {"title": last_workout.get("title") or "Workout", "detail": " · ".join(b for b in bits if b)}
    context = {
        "app": APP,
        "greeting": greeting(now),
        "first_name": (user_name or "").split(" ")[0],
        "today_label": long_date(now.date() if isinstance(now, datetime) else now),
        "thanksgiving": thanksgiving,
        "planned_meals": list(planned_meals),
        "shopping": shopping,
        "todos": list(todos),
        "focus": focus,
        "workout": workout,
        "playlist_drafts": playlist_drafts,
        "is_guest": is_guest,
        "report_url": report_url,
        # Studio reports (admin only): shown while any fix waits to be checked or any report is open.
        "studio": studio if studio and (studio.get("to_check") or studio.get("open")) else None,
        # Logbook (core/logbook): proposals from ChatGPT or Claude waiting for an OK, and tables never verified.
        "logbook": logbook,
        # Media (core/media): pictures ChatGPT or Claude sent, waiting for his OK.
        "media": media,
        "page_url": "/",
    }
    return HTMLResponse(_templates.get_template("today.html").render(**context))
