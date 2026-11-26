# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repeating reminders: the repeat rule, and the actions that set and cancel them."""

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import recurring
from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.recurring import RepeatError, plan_repeat
from custom_components.voice_assistant_manager.reminder_actions import note_asker

from .router_helpers import add_router

ZONE = ZoneInfo("Europe/Amsterdam")
# A Thursday.
THURSDAY_8 = datetime(2026, 11, 26, 8, 0, tzinfo=ZONE)


@pytest.mark.parametrize(
    ("repeat", "options", "first", "rule"),
    [
        ("daily", {}, THURSDAY_8, "FREQ=DAILY"),
        ("DAILY", {}, THURSDAY_8, "FREQ=DAILY"),
        ("daily", {"every": 2}, THURSDAY_8, "FREQ=DAILY;INTERVAL=2"),
        ("weekdays", {}, THURSDAY_8, "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
        ("weekends", {}, THURSDAY_8 + timedelta(days=2), "FREQ=WEEKLY;BYDAY=SA,SU"),
        ("weekly", {}, THURSDAY_8, "FREQ=WEEKLY"),
        ("weekly", {"on_days": ["monday", "thursday"]}, THURSDAY_8, "FREQ=WEEKLY;BYDAY=MO,TH"),
        ("weekly", {"on_days": "Mon, Fri"}, THURSDAY_8 + timedelta(days=1),
         "FREQ=WEEKLY;BYDAY=MO,FR"),
        ("weekly", {"on_days": "friday", "every": 2}, THURSDAY_8 + timedelta(days=1),
         "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"),
        ("monthly", {}, THURSDAY_8, "FREQ=MONTHLY"),
        ("yearly", {}, THURSDAY_8, "FREQ=YEARLY"),
        ("daily", {"times": 10}, THURSDAY_8, "FREQ=DAILY;COUNT=10"),
        ("daily", {"until": "2026-12-31"}, THURSDAY_8, "FREQ=DAILY;UNTIL=20261231T235959"),
        ("daily", {"until": "12-31"}, THURSDAY_8, "FREQ=DAILY;UNTIL=20261231T235959"),
        # A month and day before the first reminder is next year's.
        ("weekly", {"until": "01-15"}, THURSDAY_8, "FREQ=WEEKLY;UNTIL=20270115T235959"),
        ("daily", {"until": "11-26"}, THURSDAY_8, "FREQ=DAILY;UNTIL=20261126T235959"),
    ],
)
def test_the_repeat_becomes_a_calendar_rule(repeat, options, first, rule):
    planned = plan_repeat(THURSDAY_8, repeat, **options)

    assert (planned.first, planned.rrule) == (first, rule)


def test_the_first_reminder_keeps_the_time_of_day_when_it_moves_to_a_later_day():
    planned = plan_repeat(datetime(2026, 11, 28, 7, 30, tzinfo=ZONE), "weekdays")

    assert planned.first == datetime(2026, 11, 30, 7, 30, tzinfo=ZONE)


@pytest.mark.parametrize(
    ("repeat", "options", "message"),
    [
        ("hourly", {}, "repeat must be one of"),
        ("", {}, "repeat must be one of"),
        ("daily", {"every": 0}, "every must be"),
        ("daily", {"every": 100}, "every must be"),
        ("daily", {"on_days": "monday"}, "on_days only goes with weekly"),
        ("weekly", {"on_days": "someday"}, "on_days must be weekday names"),
        ("daily", {"times": 0}, "times must be"),
        ("daily", {"times": 1000}, "times must be"),
        ("daily", {"times": 3, "until": "12-31"}, "not both"),
        ("daily", {"until": "31-12"}, "until must be"),
        ("daily", {"until": "2026-11-25"}, "until is before the first reminder"),
    ],
)
def test_a_repeat_that_cannot_be_set_says_why(repeat, options, message):
    with pytest.raises(RepeatError, match=message):
        plan_repeat(THURSDAY_8, repeat, **options)


def test_the_repeat_names_are_the_ones_the_script_offers():
    assert recurring.REPEATS == ("daily", "weekdays", "weekends", "weekly", "monthly", "yearly")


# The actions, on a real Local Calendar


CALENDAR = "calendar.alex_reminders"


@pytest.fixture
async def calendar(hass: HomeAssistant, freezer):
    """A Local Calendar kept in memory, on a Thursday at 07:00, with the actions set up."""
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    freezer.move_to(datetime(2026, 11, 26, 7, 0, tzinfo=ZONE))
    files: dict[str, str] = {}

    def load(store):
        return files.get(str(store._path), "")

    def store(store, content):
        files[str(store._path)] = content

    with (
        patch("homeassistant.components.local_calendar.store.LocalCalendarStore._load", load),
        patch("homeassistant.components.local_calendar.store.LocalCalendarStore._store", store),
    ):
        entry = MockConfigEntry(domain="local_calendar", data={"calendar_name": "Alex reminders"})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await add_router(hass)
        yield files


async def call(hass, service, context=None, **data):
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True, context=context
    )


async def events(hass, days=21):
    start = dt_util.now()
    response = await hass.services.async_call(
        "calendar", "get_events",
        {"entity_id": CALENDAR, "start_date_time": start,
         "end_date_time": start + timedelta(days=days)},
        blocking=True, return_response=True,
    )
    return [(e["summary"], e["start"]) for e in response[CALENDAR]["events"]]


async def test_a_weekday_reminder_repeats_on_every_weekday(hass, calendar):
    response = await call(
        hass, "create_reminder_event", calendar=CALENDAR, summary="Take the pills",
        description="assistant: ada", start="2026-11-28T08:00:00+01:00", repeat="weekdays",
    )

    assert response == {
        "status": "ok", "first": "Monday 2026-11-30 08:00",
        "rrule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    }
    found = await events(hass, days=8)
    assert [start for _summary, start in found] == [
        "2026-11-30T08:00:00+01:00", "2026-12-01T08:00:00+01:00", "2026-12-02T08:00:00+01:00",
        "2026-12-03T08:00:00+01:00",
    ]


async def test_a_reminder_with_a_count_ends_after_that_many(hass, calendar):
    await call(
        hass, "create_reminder_event", calendar=CALENDAR, summary="Water the plants",
        start="2026-11-26T18:00:00+01:00", repeat="daily", times=3,
    )

    assert len(await events(hass)) == 3


async def test_a_reminder_until_a_day_ends_on_that_day(hass, calendar):
    await call(
        hass, "create_reminder_event", calendar=CALENDAR, summary="Water the plants",
        start="2026-11-26T18:00:00+01:00", repeat="daily", until="11-30",
    )

    found = await events(hass)
    assert found[-1][1] == "2026-11-30T18:00:00+01:00"
    assert len(found) == 5


async def test_empty_optional_fields_from_a_script_are_left_out(hass, calendar):
    response = await call(
        hass, "create_reminder_event", calendar=CALENDAR, summary="Bins",
        start="2026-11-26T19:00:00+01:00", repeat="weekly", every="", on_days="", times="",
        until="",
    )

    assert response["rrule"] == "FREQ=WEEKLY"


async def test_a_repeat_that_cannot_be_set_answers_with_why_and_sets_nothing(hass, calendar):
    response = await call(
        hass, "create_reminder_event", calendar=CALENDAR, summary="Bins",
        start="2026-11-26T19:00:00+01:00", repeat="hourly",
    )

    assert response["status"] == "error"
    assert "repeat must be one of" in response["message"]
    assert await events(hass) == []


async def test_a_calendar_that_does_not_exist_answers_with_an_error(hass, calendar):
    response = await call(
        hass, "create_reminder_event", calendar="calendar.nobody", summary="Bins",
        start="2026-11-26T19:00:00+01:00", repeat="daily",
    )

    assert response["status"] == "error"
    assert "calendar.nobody" in response["message"]


async def test_a_user_who_may_not_control_the_calendar_is_refused(
    hass, calendar, hass_read_only_user
):
    with pytest.raises(Unauthorized):
        await call(
            hass, "create_reminder_event", Context(user_id=hass_read_only_user.id),
            calendar=CALENDAR, summary="Bins", start="2026-11-26T19:00:00+01:00",
            repeat="daily",
        )
    with pytest.raises(Unauthorized):
        await call(
            hass, "cancel_reminder_event", Context(user_id=hass_read_only_user.id),
            calendar=[CALENDAR], title="Bins",
        )


async def create(hass, summary, repeat="daily", start="2026-11-26T18:00:00+01:00"):
    await call(
        hass, "create_reminder_event", calendar=CALENDAR, summary=summary, start=start,
        repeat=repeat,
    )


async def one_off(hass, summary):
    await hass.services.async_call(
        "calendar", "create_event",
        {"entity_id": CALENDAR, "summary": summary,
         "start_date_time": "2026-11-27 09:00:00", "end_date_time": "2026-11-27 09:05:00"},
        blocking=True,
    )


async def test_cancelling_removes_the_whole_repeating_reminder(hass, calendar):
    await create(hass, "Take the pills")
    await create(hass, "Water the plants")

    response = await call(
        hass, "cancel_reminder_event", calendar=[CALENDAR], title="take the PILLS"
    )

    assert response == {"status": "ok", "removed": 1, "title": "Take the pills"}
    assert {summary for summary, _start in await events(hass)} == {"Water the plants"}


async def test_cancelling_removes_a_one_off_reminder_too(hass, calendar):
    await one_off(hass, "Call the plumber")

    response = await call(hass, "cancel_reminder_event", calendar=[CALENDAR], title="plumber")

    assert response["status"] == "ok"
    assert await events(hass) == []


async def test_a_title_that_matches_several_reminders_asks_which(hass, calendar):
    await create(hass, "Feed the cat")
    await create(hass, "Feed the fish")

    response = await call(hass, "cancel_reminder_event", calendar=[CALENDAR], title="feed")

    assert response["status"] == "error"
    assert "Feed the cat" in response["message"] and "Feed the fish" in response["message"]
    assert len({summary for summary, _start in await events(hass)}) == 2


async def test_a_title_that_matches_nothing_says_so(hass, calendar):
    response = await call(hass, "cancel_reminder_event", calendar=[CALENDAR], title="dentist")

    assert response["status"] == "error"
    assert "dentist" in response["message"]


async def test_an_exact_title_wins_over_titles_that_contain_it(hass, calendar):
    await create(hass, "Feed the cat")
    await create(hass, "Feed the cat at noon")

    response = await call(
        hass, "cancel_reminder_event", calendar=[CALENDAR], title="feed the cat"
    )

    assert response["title"] == "Feed the cat"
    assert {summary for summary, _start in await events(hass)} == {"Feed the cat at noon"}


# End to end: the example scripts, VAM's actions and a Local Calendar


async def test_the_example_scripts_set_and_cancel_a_repeating_reminder(hass, calendar):
    from homeassistant.setup import async_setup_component  # noqa: PLC0415
    from homeassistant.util.yaml import load_yaml  # noqa: PLC0415

    from .test_examples import EXAMPLES  # noqa: PLC0415

    spoken = MockConfigEntry(
        domain="local_calendar", data={"calendar_name": "Alex reminders announce"}
    )
    spoken.add_to_hass(hass)
    assert await hass.config_entries.async_setup(spoken.entry_id)
    scripts = {name: load_yaml(EXAMPLES / f"{name}.yaml")
               for name in ("create_reminder", "cancel_reminder")}
    assert await async_setup_component(hass, "script", {"script": scripts})

    created = await hass.services.async_call(
        "script", "create_reminder",
        {"title": "Take the pills", "for_whom": "alex", "assistant": "ada",
         "at_time": "08:00", "repeat": "weekdays"},
        blocking=True, return_response=True,
    )

    assert created["status"] == "ok"
    assert created["due"] == "Thursday 2026-11-26 08:00"
    week = await hass.services.async_call(
        "calendar", "get_events",
        {"entity_id": "calendar.alex_reminders_announce", "duration": {"days": 7}},
        blocking=True, return_response=True,
    )
    assert len(week["calendar.alex_reminders_announce"]["events"]) == 5

    # Alex asks an assistant: a person cancels only their own and the household's.
    turn = Context()
    note_asker(hass, turn, "Alex")
    cancelled = await hass.services.async_call(
        "script", "cancel_reminder", {"title": "pills", "for_whom": "alex"},
        blocking=True, return_response=True, context=turn,
    )

    assert cancelled == {"status": "ok", "removed": 1, "title": "Take the pills"}
    week = await hass.services.async_call(
        "calendar", "get_events",
        {"entity_id": "calendar.alex_reminders_announce", "duration": {"days": 7}},
        blocking=True, return_response=True,
    )
    assert week["calendar.alex_reminders_announce"]["events"] == []
