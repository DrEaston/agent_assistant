"""Trainer's "today" in his time zone. The server runs on UTC, so a plain date.today() turns Sunday evening in
Phoenix into Monday and moves "this week" a day early. Weeks are Monday to Sunday in his local time.

APP_TIMEZONE is set on the live service (America/Phoenix); without it (tests, local runs) this is UTC."""

import os
from datetime import datetime
from zoneinfo import ZoneInfo


def today():
    try:
        return datetime.now(ZoneInfo(os.getenv("APP_TIMEZONE") or "UTC")).date()
    except Exception:
        return datetime.utcnow().date()
