# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rules for announcing events from calendars: when one is due, and whether to say it.

Nothing here touches Home Assistant. calendar_watch.py does the polling, the storing
and the speaking.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from .reminders import HOUSEHOLD, for_whom

KIND_CALENDAR = "calendar"
EVENT_CALENDAR_DUE = "voice_assistant_manager_calendar_due"
DEFAULT_ALL_DAY_TIME = "09:00:00"
DEFAULT_CATCH_UP_HOURS = 3
DAY_SAME = "same"
DAY_BEFORE = "before"
# A check on time is at most a minute late, which must always count as on time.
MIN_LATE = timedelta(minutes=2)

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December",
)  # fmt: skip


@dataclass(frozen=True)
class Occurrence:
    """One event of one calendar. All-day events have dates, the others aware datetimes."""

    calendar: str
    summary: str
    start: date | datetime
    end: date | datetime
    uid: str = ""

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


def _moment(value: Any, zone: tzinfo) -> date | datetime | None:
    """A date for "2026-11-23", an aware datetime for a time, None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        if "T" not in value and " " not in value.strip():
            return date.fromisoformat(value.strip())
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def parse_occurrence(calendar: str, raw: Any, zone: tzinfo) -> Occurrence | None:
    """Read one event as calendar.get_events returns it, or None if it cannot be read."""
    if not isinstance(raw, Mapping):
        return None
    start = _moment(raw.get("start"), zone)
    end = _moment(raw.get("end"), zone)
    if start is None or end is None or isinstance(start, datetime) != isinstance(end, datetime):
        return None
    return Occurrence(
        calendar, str(raw.get("summary") or ""), start, end, str(raw.get("uid") or "")
    )


def parse_time(value: Any) -> time | None:
    """A time of day from "19:00" or "19:00:00", or None."""
    parts = str(value or "").strip().split(":")
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        return None
    try:
        return time(int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def _local(day: date, at: time, zone: tzinfo) -> datetime:
    """A time on the clock of a day, whatever daylight saving does that day."""
    return datetime.combine(day, at, tzinfo=zone)


def start_at(event: Occurrence, zone: tzinfo) -> datetime:
    """When the event starts. An all-day event starts at local midnight."""
    if isinstance(event.start, datetime):
        return event.start
    return _local(event.start, time(0, 0), zone)


def end_at(event: Occurrence, zone: tzinfo) -> datetime:
    """When the event is over. An all-day event ends at local midnight after its last day."""
    if isinstance(event.end, datetime):
        return event.end
    return _local(event.end, time(0, 0), zone)


def due_at(event: Occurrence, row: Mapping[str, Any], default_time: time, zone: tzinfo) -> datetime:
    """The moment to announce an event.

    A timed event: its start, less the calendar's lead time. An all-day event: the
    calendar's time of day (or the default) on its first day, or on the day before.
    """
    if isinstance(event.start, datetime):
        return event.start - timedelta(minutes=float(row.get("lead_minutes") or 0))
    day = event.start
    if row.get("all_day_day") == DAY_BEFORE:
        day -= timedelta(days=1)
    return _local(day, parse_time(row.get("all_day_time")) or default_time, zone)


def event_key(event: Occurrence) -> str:
    """What makes an announcement the same one: the calendar, the event and its start.

    calendar.get_events leaves out the uid, so the summary and the end stand in for it
    when there is none.
    """
    identity = event.uid or f"{event.summary}|{event.end.isoformat()}"
    return f"{event.calendar}|{identity}|{event.start.isoformat()}"


def matches_filter(summary: str, contains: str | None) -> bool:
    """Whether the event passes the calendar's "only events containing" filter."""
    wanted = str(contains or "").strip().casefold()
    return not wanted or wanted in str(summary or "").casefold()


def late_limit(catch_up: timedelta) -> timedelta:
    """How late an event may be and still be announced."""
    return max(catch_up, MIN_LATE)


def should_announce(
    due: datetime,
    end: datetime,
    now: datetime,
    last_check: datetime | None,
    catch_up: timedelta,
) -> bool:
    """Whether an event that was not announced yet is announced at this check.

    Not before it is due, and not when it is later than the catch-up limit (a check on
    time is at most a minute late, which always counts). An event that became due since
    the last check is announced even when it is over by now: that is the catch-up after
    a restart. One that was already due at the last check was added afterwards, and is
    only announced while it is still going on.
    """
    late = now - due
    if late < timedelta(0) or late >= late_limit(catch_up):
        return False
    if last_check is not None and due > last_check:
        return True
    return now < end


def _day_name(day: date, today: date) -> str:
    named = f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]}"
    if day == today:
        return f"today ({named})"
    if day == today + timedelta(days=1):
        return f"tomorrow ({named})"
    return named


def describe_when(event: Occurrence, now: datetime, zone: tzinfo) -> str:
    """When the event is, in local time and in words, for the agent that writes the line."""
    today = now.astimezone(zone).date()
    if isinstance(event.start, datetime):
        start = event.start.astimezone(zone)
        return f"{_day_name(start.date(), today)} at {start:%H:%M}"
    text = f"{_day_name(event.start, today)}, all day"
    days = 1 if isinstance(event.end, datetime) else (event.end - event.start).days
    return f"{text}, for {days} days" if days > 1 else text


def owner_value(options: Mapping[str, Any], who: str | None) -> str:
    """The "owner" a calendar's events go out with: a person's lower-case name or household."""
    if who in ("owner", "partner"):
        return for_whom((options.get(who) or {}).get("name") or "") or HOUSEHOLD
    return HOUSEHOLD
