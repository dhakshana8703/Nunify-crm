"""Wall-clock recurrence for source runs, persisted as UTC due times."""

import calendar
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CADENCES = {"daily", "weekly", "monthly"}


def calendar_schedule(cadence, timezone_name, local_time, day):
    if cadence not in CADENCES:
        raise ValueError("Choose a daily, weekly, or monthly schedule.")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", local_time):
        raise ValueError("Enter a local time in 24-hour HH:MM format.")
    try:
        ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("Enter a valid IANA timezone, such as Asia/Kolkata.") from None
    if cadence == "weekly" and day not in range(7):
        raise ValueError("Choose a weekday for the weekly schedule.")
    if cadence == "monthly" and day not in range(1, 32):
        raise ValueError("Choose a day from 1 to 31 for the monthly schedule.")
    return {"cadence": cadence, "timezone": timezone_name, "local_time": local_time,
            "schedule_day": None if cadence == "daily" else day}


def next_calendar_run(config, after):
    zone = ZoneInfo(config["timezone"])
    local_day = after.astimezone(zone).date()
    wall_time = time.fromisoformat(config["local_time"])
    cadence = config["cadence"]
    day = config["schedule_day"]

    def at_date(date):
        # The first occurrence is used on a repeated hour. A skipped hour moves forward by the gap.
        return datetime.combine(date, wall_time, tzinfo=zone).astimezone(timezone.utc)

    if cadence == "daily":
        next_at = at_date(local_day)
        return next_at if next_at > after else at_date(local_day + timedelta(days=1))
    if cadence == "weekly":
        date = local_day + timedelta(days=(day - local_day.weekday()) % 7)
        next_at = at_date(date)
        return next_at if next_at > after else at_date(date + timedelta(days=7))

    def at_month(year, month):
        return at_date(local_day.replace(year=year, month=month, day=min(day, calendar.monthrange(year, month)[1])))

    next_at = at_month(local_day.year, local_day.month)
    if next_at > after:
        return next_at
    year, month_index = divmod(local_day.year * 12 + local_day.month, 12)
    return at_month(year, month_index + 1)
