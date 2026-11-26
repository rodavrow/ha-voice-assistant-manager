# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repeating reminders: the repeat rule for a calendar event.

Home Assistant's calendar.create_event action cannot make an event repeat; only its
calendar page can, through the calendar entity. The actions in reminder_actions.py do the
same for script.create_reminder and script.cancel_reminder. This module only holds the
rule, so it can be tested alone.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

REPEATS: Final = ("daily", "weekdays", "weekends", "weekly", "monthly", "yearly")
WEEKDAYS: Final = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_CODES: Final = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_FREQ: Final = {
    "daily": "DAILY", "weekdays": "WEEKLY", "weekends": "WEEKLY", "weekly": "WEEKLY",
    "monthly": "MONTHLY", "yearly": "YEARLY",
}
_FIXED_DAYS: Final = {"weekdays": (0, 1, 2, 3, 4), "weekends": (5, 6)}
MAX_EVERY: Final = 99
MAX_TIMES: Final = 999


class RepeatError(ValueError):
    """The repeat cannot be set. The message is for the agent, which tells the person."""


@dataclass(frozen=True)
class Repeat:
    """When the first reminder is, and the calendar rule (RFC 5545 RRULE) for the rest."""

    first: datetime
    rrule: str


def _weekdays(on_days: Sequence[str] | str) -> tuple[int, ...]:
    names = re.split(r"[\s,]+", on_days) if isinstance(on_days, str) else list(on_days)
    found: list[int] = []
    for name in (str(n).strip().lower() for n in names):
        if not name:
            continue
        day = next((i for i, full in enumerate(WEEKDAYS) if len(name) >= 3
                    and full.startswith(name)), None)
        if day is None:
            raise RepeatError("on_days must be weekday names, for example monday, thursday.")
        if day not in found:
            found.append(day)
    return tuple(sorted(found))


def _until(text: str, first: datetime) -> date:
    text = text.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            last = date.fromisoformat(text)
        elif re.fullmatch(r"\d{2}-\d{2}", text):
            last = date.fromisoformat(f"{first.year}-{text}")
            if last < first.date():
                last = last.replace(year=first.year + 1)
        else:
            raise ValueError(text)
    except ValueError as err:
        raise RepeatError("until must be MM-DD or YYYY-MM-DD.") from err
    if last < first.date():
        raise RepeatError("until is before the first reminder.")
    return last


def plan_repeat(
    start: datetime,
    repeat: str,
    *,
    every: int = 1,
    on_days: Sequence[str] | str = (),
    times: int | None = None,
    until: str | None = None,
) -> Repeat:
    """The first reminder at or after `start`, and the rule that repeats it.

    weekdays and weekends, and weekly with on_days, move the first reminder to the first
    of those days, at the same time of day. It ends after `times` reminders or on the day
    `until` (MM-DD: the next such date), or never. Raises RepeatError for anything else.
    """
    kind = str(repeat or "").strip().lower()
    if kind not in REPEATS:
        raise RepeatError(f"repeat must be one of {', '.join(REPEATS)}.")
    if not 1 <= every <= MAX_EVERY:
        raise RepeatError(f"every must be a whole number from 1 to {MAX_EVERY}.")
    days = _weekdays(on_days)
    if days and kind != "weekly":
        raise RepeatError("on_days only goes with weekly.")
    days = _FIXED_DAYS.get(kind, days)
    if times is not None and not 1 <= times <= MAX_TIMES:
        raise RepeatError(f"times must be a whole number from 1 to {MAX_TIMES}.")
    if times is not None and until:
        raise RepeatError("Pass times or until, not both.")
    first = start
    if days:
        ahead = min((day - start.weekday()) % 7 for day in days)
        first = start + timedelta(days=ahead)
    parts = [f"FREQ={_FREQ[kind]}"]
    if every > 1:
        parts.append(f"INTERVAL={every}")
    if days:
        parts.append("BYDAY=" + ",".join(_CODES[day] for day in days))
    if times is not None:
        parts.append(f"COUNT={times}")
    elif until:
        # Floating, in local time, as Home Assistant's Local Calendar keeps it.
        parts.append(f"UNTIL={_until(until, first).strftime('%Y%m%d')}T235959")
    return Repeat(first, ";".join(parts))
