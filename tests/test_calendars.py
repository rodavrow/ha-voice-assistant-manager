# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rules for announcing calendar events: when one is due, and whether to say it."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.voice_assistant_manager.calendars import (
    Occurrence,
    describe_when,
    due_at,
    event_key,
    matches_filter,
    owner_value,
    parse_occurrence,
    parse_time,
    should_announce,
)

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
CALENDAR = "calendar.waste"
NINE = time(9, 0)
CATCH_UP = timedelta(hours=3)


def all_day(day: date, days: int = 1, summary: str = "Avalex: GFT") -> Occurrence:
    return Occurrence(CALENDAR, summary, day, day + timedelta(days=days))


def timed(start: datetime, minutes: int = 5, summary: str = "Dentist") -> Occurrence:
    return Occurrence(CALENDAR, summary, start, start + timedelta(minutes=minutes))


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# Reading what calendar.get_events returns.


def test_an_all_day_event_is_read_from_its_dates():
    raw = {"start": "2026-11-23", "end": "2026-11-24", "summary": "Avalex: GFT"}

    assert parse_occurrence(CALENDAR, raw, AMSTERDAM) == all_day(date(2026, 11, 23))


def test_a_timed_event_is_read_with_its_time_zone():
    raw = {
        "start": "2026-11-21T18:55:00+01:00",
        "end": "2026-11-21T19:00:00+01:00",
        "summary": "Dentist",
    }

    event = parse_occurrence(CALENDAR, raw, AMSTERDAM)

    assert event is not None
    assert event.start == utc(2026, 11, 21, 17, 55)
    assert not event.all_day


def test_a_time_without_a_zone_is_local_time():
    raw = {"start": "2026-11-21T18:55:00", "end": "2026-11-21T19:00:00", "summary": "Dentist"}

    event = parse_occurrence(CALENDAR, raw, AMSTERDAM)

    assert event is not None
    assert event.start == utc(2026, 11, 21, 17, 55)


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"start": "soon", "end": "later", "summary": "Dentist"},
        {"start": "2026-11-23", "summary": "No end"},
        {"start": "2026-11-23", "end": "2026-11-24T10:00:00+01:00", "summary": "Mixed"},
        {"start": 5, "end": 6, "summary": "Numbers"},
        "not an event",
    ],
)
def test_an_event_that_cannot_be_read_is_skipped(raw):
    assert parse_occurrence(CALENDAR, raw, AMSTERDAM) is None


def test_an_event_without_a_summary_has_an_empty_one():
    raw = {"start": "2026-11-23", "end": "2026-11-24"}

    event = parse_occurrence(CALENDAR, raw, AMSTERDAM)

    assert event is not None
    assert event.summary == ""


def test_the_uid_is_kept_when_the_calendar_gives_one():
    raw = {"start": "2026-11-23", "end": "2026-11-24", "summary": "GFT", "uid": "abc"}

    event = parse_occurrence(CALENDAR, raw, AMSTERDAM)

    assert event is not None
    assert event.uid == "abc"


# The time of day for all-day events.


@pytest.mark.parametrize(
    ("value", "expected"),
    [("19:00", time(19, 0)), ("07:30:00", time(7, 30)), (" 6:05 ", time(6, 5))],
)
def test_a_time_of_day_is_read(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize("value", ["", None, "25:00", "seven", "19", "19:60", "-1:00"])
def test_a_time_of_day_that_cannot_be_read_is_none(value):
    assert parse_time(value) is None


# When an event is due.


def test_a_timed_event_is_due_at_its_start():
    start = utc(2026, 11, 21, 17, 55)

    assert due_at(timed(start), {}, NINE, AMSTERDAM) == start


def test_a_timed_event_is_due_the_lead_time_before_its_start():
    start = utc(2026, 11, 21, 17, 55)

    assert due_at(timed(start), {"lead_minutes": 15}, NINE, AMSTERDAM) == utc(2026, 11, 21, 17, 40)


def test_an_all_day_event_is_due_at_the_default_time_on_the_day():
    event = all_day(date(2026, 11, 23))

    assert due_at(event, {}, NINE, AMSTERDAM) == utc(2026, 11, 23, 8, 0)


def test_an_all_day_event_is_due_at_the_time_of_its_own_calendar():
    event = all_day(date(2026, 11, 23))

    assert due_at(event, {"all_day_time": "07:30"}, NINE, AMSTERDAM) == utc(2026, 11, 23, 6, 30)


def test_a_calendar_time_that_cannot_be_read_falls_back_to_the_default():
    event = all_day(date(2026, 11, 23))

    assert due_at(event, {"all_day_time": "soon"}, NINE, AMSTERDAM) == utc(2026, 11, 23, 8, 0)


def test_an_all_day_event_can_be_due_the_evening_before():
    event = all_day(date(2026, 11, 23))
    row = {"all_day_time": "19:00", "all_day_day": "before"}

    assert due_at(event, row, NINE, AMSTERDAM) == utc(2026, 11, 22, 18, 0)


def test_the_lead_time_does_not_apply_to_all_day_events():
    event = all_day(date(2026, 11, 23))

    assert due_at(event, {"lead_minutes": 30}, NINE, AMSTERDAM) == utc(2026, 11, 23, 8, 0)


def test_a_multi_day_event_is_due_at_the_time_of_its_first_day():
    event = all_day(date(2026, 11, 23), days=3)

    assert due_at(event, {"all_day_time": "19:00"}, NINE, AMSTERDAM) == utc(2026, 11, 23, 18, 0)


# The clocks go back in Amsterdam on Sunday 25 October 2026 (UTC+2 to UTC+1).


def test_evening_before_the_clocks_go_back_is_still_summer_time():
    event = all_day(date(2026, 10, 25))
    row = {"all_day_time": "19:00", "all_day_day": "before"}

    assert due_at(event, row, NINE, AMSTERDAM) == utc(2026, 10, 24, 17, 0)


def test_evening_of_the_day_the_clocks_go_back_is_winter_time():
    event = all_day(date(2026, 10, 26))
    row = {"all_day_time": "19:00", "all_day_day": "before"}

    assert due_at(event, row, NINE, AMSTERDAM) == utc(2026, 10, 25, 18, 0)


def test_evening_before_the_clocks_go_forward_is_still_winter_time():
    # Sunday 29 March 2026: UTC+1 to UTC+2.
    event = all_day(date(2026, 3, 29))
    row = {"all_day_time": "19:00", "all_day_day": "before"}

    assert due_at(event, row, NINE, AMSTERDAM) == utc(2026, 3, 28, 18, 0)


# One announcement per event.


def test_the_key_names_the_calendar_the_uid_and_the_start():
    event = Occurrence(CALENDAR, "GFT", date(2026, 11, 23), date(2026, 11, 24), uid="abc")

    assert event_key(event) == "calendar.waste|abc|2026-11-23"


def test_without_a_uid_the_key_uses_the_summary_and_the_end():
    event = all_day(date(2026, 11, 23), summary="GFT")

    assert event_key(event) == "calendar.waste|GFT|2026-11-24|2026-11-23"


def test_the_same_event_at_another_start_has_another_key():
    first = timed(utc(2026, 11, 21, 17, 55))
    moved = timed(utc(2026, 11, 21, 18, 55))

    assert event_key(first) != event_key(moved)


# The text filter.


@pytest.mark.parametrize("contains", ["", "  ", "gft", "AVALEX", " Avalex: "])
def test_filter_matches(contains):
    assert matches_filter("Avalex: GFT", contains)


def test_filter_leaves_out_other_events():
    assert not matches_filter("Avalex: Restafval", "gft")


# Whether a due event is still announced.


def announce(due, *, now, last_check, end=None) -> bool:
    return should_announce(due, end or due + timedelta(minutes=5), now, last_check, CATCH_UP)


def test_an_event_that_became_due_since_the_last_check_is_announced():
    due = utc(2026, 11, 21, 18, 0)

    assert announce(due, now=due + timedelta(seconds=20), last_check=due - timedelta(seconds=40))


def test_an_event_that_is_not_due_yet_is_not_announced():
    due = utc(2026, 11, 21, 18, 0)

    assert not announce(due, now=due - timedelta(seconds=1), last_check=due - timedelta(minutes=1))


def test_an_event_due_exactly_now_is_announced():
    due = utc(2026, 11, 21, 18, 0)

    assert announce(due, now=due, last_check=due - timedelta(minutes=1))


def test_after_a_restart_an_event_less_than_the_limit_late_is_caught_up():
    due = utc(2026, 11, 21, 18, 0)

    assert announce(
        due, now=due + timedelta(hours=2, minutes=59), last_check=due - timedelta(minutes=10)
    )


def test_after_a_restart_an_event_later_than_the_limit_is_dropped():
    due = utc(2026, 11, 21, 18, 0)

    assert not announce(due, now=due + timedelta(hours=3), last_check=due - timedelta(minutes=10))


def test_an_event_added_after_it_was_due_is_announced_while_it_is_going_on():
    # Created at 18:57 for 18:55 to 19:00: the 18:56 check could not have seen it.
    due = utc(2026, 11, 21, 18, 55)

    assert announce(due, now=utc(2026, 11, 21, 18, 57), last_check=utc(2026, 11, 21, 18, 56))


def test_an_event_added_after_it_was_over_is_not_announced():
    due = utc(2026, 11, 21, 18, 55)

    assert not announce(due, now=utc(2026, 11, 21, 19, 30), last_check=utc(2026, 11, 21, 19, 29))


def test_on_the_first_check_of_a_calendar_only_events_still_going_on_are_announced():
    due = utc(2026, 11, 21, 18, 55)

    assert announce(due, now=utc(2026, 11, 21, 18, 57), last_check=None)
    assert not announce(due, now=utc(2026, 11, 21, 19, 30), last_check=None)


def test_a_catch_up_limit_of_zero_still_announces_on_time():
    due = utc(2026, 11, 21, 18, 0)

    assert should_announce(
        due, due + timedelta(minutes=5), due + timedelta(seconds=30),
        due - timedelta(seconds=30), timedelta(0),
    )


# Saying when it is.


NOW = utc(2026, 11, 21, 18, 0)  # Saturday 21 November, 19:00 in Amsterdam


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (all_day(date(2026, 11, 21)), "today (Saturday 21 November), all day"),
        (all_day(date(2026, 11, 22)), "tomorrow (Sunday 22 November), all day"),
        (all_day(date(2026, 11, 25)), "Wednesday 25 November, all day"),
        (
            all_day(date(2026, 11, 22), days=3),
            "tomorrow (Sunday 22 November), all day, for 3 days",
        ),
        (timed(utc(2026, 11, 21, 18, 15)), "today (Saturday 21 November) at 19:15"),
        (timed(utc(2026, 11, 22, 8, 0)), "tomorrow (Sunday 22 November) at 09:00"),
        (timed(utc(2026, 11, 20, 8, 0)), "Friday 20 November at 09:00"),
    ],
)
def test_when_is_said_in_local_time_relative_to_today(event, expected):
    assert describe_when(event, NOW, AMSTERDAM) == expected


# Who it is for.


OPTIONS = {"owner": {"name": "Alex"}, "partner": {"name": "Sam"}}


@pytest.mark.parametrize(
    ("who", "expected"),
    [("owner", "alex"), ("partner", "sam"), ("household", "household"), ("", "household")],
)
def test_owner_is_the_lower_case_name_of_the_person_or_household(who, expected):
    assert owner_value(OPTIONS, who) == expected


def test_a_person_without_a_name_becomes_the_household():
    assert owner_value({"owner": {}}, "owner") == "household"
