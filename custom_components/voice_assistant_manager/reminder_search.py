# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Look reminders up by day and keyword. Pure logic with no Home Assistant imports.

The calendar gives one entry per time a reminder is due. These functions turn the day a
person named into a range, keep what matches their words, and report each reminder once,
with its settings as they are: when it is next due, how it repeats and when that ends.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from .recurring import WEEKDAYS

# How far ahead is looked when no day is named.
WINDOW: Final = timedelta(days=731)
MAX_REMINDERS: Final = 25
_DAY_HELP: Final = "A day must be today, tomorrow, a weekday name, MM-DD or YYYY-MM-DD."
# Words a person says around what they look for, which no title needs to contain.
_FILLER: Final = frozenset(
    "a an the to for of on in about my our your me us any all reminder reminders".split()
)
_CODES: Final = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_UNITS: Final = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}
# How much of a reminder's notes is read. A calendar lets anyone who can write it store far
# more, and every look-up reads every reminder's notes.
MAX_NOTES: Final = 2000
_ASSISTANT_LABEL: Final = "assistant:"


class SearchError(ValueError):
    """The look-up cannot be made. The message is for the agent, which tells the person."""


@dataclass(frozen=True)
class Occurrence:
    """One time a reminder is due, as the calendar gives it."""

    key: str  # the same for every time of one reminder
    title: str
    description: str
    for_whom: str
    spoken: bool | None  # None when the script did not say
    start: datetime
    end: datetime
    rrule: str | None
    all_day: bool = False


def resolve_day(text: str, today: date) -> date:
    """The date a person means by today, tomorrow, a weekday name, MM-DD or YYYY-MM-DD.

    A weekday is the next such day, today included; MM-DD is the next such date.
    """
    word = str(text or "").strip().lower()
    if word == "today":
        return today
    if word == "tomorrow":
        return today + timedelta(days=1)
    if len(word) >= 3 and word.isalpha():
        for number, name in enumerate(WEEKDAYS):
            if name.startswith(word):
                return today + timedelta(days=(number - today.weekday()) % 7)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", word):
            return date.fromisoformat(word)
        if re.fullmatch(r"\d{2}-\d{2}", word):
            found = date.fromisoformat(f"{today.year}-{word}")
            return found if found >= today else found.replace(year=today.year + 1)
    except ValueError as err:
        raise SearchError(_DAY_HELP) from err
    raise SearchError(_DAY_HELP)


def day_range(
    on_day: str | None, until_day: str | None, now: datetime
) -> tuple[datetime, datetime, bool]:
    """The stretch of time to look in, and whether the person named it.

    Nothing named: from now on, WINDOW ahead. One day: that day. With `until_day`: up to
    and including that day, counted from the first day (today when none is named). It
    never starts before now: a reminder that is past is not coming up.
    """
    first_word, last_word = str(on_day or "").strip(), str(until_day or "").strip()
    if not first_word and not last_word:
        return now, now + WINDOW, False
    first = resolve_day(first_word, now.date()) if first_word else now.date()
    last = resolve_day(last_word, first) if last_word else first
    start = max(datetime.combine(first, time.min, tzinfo=now.tzinfo), now)
    end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=now.tzinfo)
    if end <= start:
        raise SearchError("That day is already over.")
    return start, end, True


def _stem(word: str) -> str:
    """A word without a common ending, so "charging" finds "charge" and "bins" "bin"."""
    for ending in ("ing", "ed", "es", "s", "e"):
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


def matches(keywords: str, title: str, notes: str) -> bool:
    """Whether every keyword is found in the title or the notes, whatever the case."""
    words = [w for w in re.findall(r"\w+", str(keywords or "").casefold()) if w not in _FILLER]
    text = f"{title} {notes}".casefold()
    return all(_stem(word) in text for word in words)


def _days_in_words(codes: list[str]) -> str:
    names = [WEEKDAYS[_CODES.index(code)].capitalize() for code in codes]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def describe_repeat(rrule: str | None) -> tuple[str, str]:
    """How a calendar rule (RFC 5545 RRULE) repeats and when it ends, in words.

    ("no", "") for a reminder that does not repeat. A rule with parts this does not know
    is not guessed at: it is named as it is.
    """
    if not rrule:
        return "no", ""
    parts = dict(part.split("=", 1) for part in rrule.split(";") if "=" in part)
    ends = "never"
    if parts.get("COUNT", "").isdigit():
        ends = f"after {int(parts['COUNT'])} times in all"
    elif re.match(r"\d{8}", parts.get("UNTIL", "")):
        until = parts["UNTIL"]
        ends = f"on {until[:4]}-{until[4:6]}-{until[6:8]}"
    unit = _UNITS.get(parts.get("FREQ", ""))
    every = parts.get("INTERVAL", "1")
    days = [code for code in parts.get("BYDAY", "").split(",") if code]
    known = set(parts) <= {"FREQ", "INTERVAL", "BYDAY", "COUNT", "UNTIL"}
    plain_days = all(code in _CODES for code in days) and (not days or unit == "week")
    if unit is None or not known or not every.isdigit() or not plain_days:
        return f"by the calendar rule {rrule}", ends
    repeats = f"every {unit}" if int(every) == 1 else f"every {int(every)} {unit}s"
    ordered = sorted(set(days), key=_CODES.index)
    if ordered == list(_CODES[:5]) and int(every) == 1:
        repeats = "every weekday"
    elif ordered == list(_CODES[5:]) and int(every) == 1:
        repeats = "every Saturday and Sunday"
    elif ordered:
        repeats += f" on {_days_in_words(ordered)}"
    return repeats, ends


def _split_description(description: str) -> tuple[str, str]:
    """The assistant that took the reminder, and the notes that are left."""
    assistant, notes = "", []
    for line in str(description or "")[:MAX_NOTES].splitlines():
        line = line.strip()
        if line[: len(_ASSISTANT_LABEL)].lower() == _ASSISTANT_LABEL and not assistant:
            assistant = line[len(_ASSISTANT_LABEL) :].strip()
        elif line:
            notes.append(line)
    return assistant, "\n".join(notes)


def _settings(times: list[Occurrence], ranged: bool) -> dict[str, Any]:
    """One reminder's settings, from the times it is due in the range (soonest first)."""
    first = times[0]
    assistant, notes = _split_description(first.description)
    repeats, ends = describe_repeat(first.rrule)
    day = first.start.strftime("%A %Y-%m-%d")
    found: dict[str, Any] = {
        "title": first.title,
        "for_whom": first.for_whom,
        "when": f"{day}, all day" if first.all_day else f"{day} {first.start:%H:%M}",
        "repeats": repeats,
    }
    if first.rrule:
        found["ends"] = ends
        found["rule"] = first.rrule
        if ranged:
            found["times_in_range"] = len(times)
    if not first.all_day:
        found["duration_minutes"] = round((first.end - first.start).total_seconds() / 60)
    if first.spoken is not None:
        found["spoken"] = first.spoken
    if assistant:
        found["assistant"] = assistant
    if notes:
        found["notes"] = notes
    return found


def summarise(
    occurrences: Iterable[Occurrence],
    keywords: str,
    *,
    ranged: bool,
    limit: int = MAX_REMINDERS,
) -> tuple[list[dict[str, Any]], bool]:
    """Each matching reminder once, soonest first, and whether more were left out."""
    by_reminder: dict[str, list[Occurrence]] = {}
    for occurrence in sorted(occurrences, key=lambda o: o.start):
        notes = _split_description(occurrence.description)[1]
        if matches(keywords, occurrence.title, notes):
            by_reminder.setdefault(occurrence.key, []).append(occurrence)
    found = [_settings(times, ranged) for times in by_reminder.values()]
    return found[:limit], len(found) > limit
