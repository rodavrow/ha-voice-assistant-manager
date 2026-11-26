# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Looking reminders up: a person sees their own and the household's, never the other's."""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.reminder_actions import (
    ASKER_LIFETIME,
    asker,
    note_asker,
)

from .router_helpers import add_router

ZONE = ZoneInfo("Europe/Amsterdam")
PEOPLE = ("alex", "sam", "household")
REMINDERS = [
    {"for_whom": who, "calendar": [f"calendar.{who}_reminders"], "spoken": True}
    for who in PEOPLE
]


@pytest.fixture
async def calendars(hass: HomeAssistant, freezer):
    """A Local Calendar per person, on a Thursday at 07:00, each with reminders."""
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    freezer.move_to(datetime(2026, 11, 26, 7, 0, tzinfo=ZONE))
    files: dict[str, str] = {}
    with (
        patch(
            "homeassistant.components.local_calendar.store.LocalCalendarStore._load",
            lambda store: files.get(str(store._path), ""),
        ),
        patch(
            "homeassistant.components.local_calendar.store.LocalCalendarStore._store",
            lambda store, content: files.__setitem__(str(store._path), content),
        ),
    ):
        for who in PEOPLE:
            entry = MockConfigEntry(
                domain="local_calendar", data={"calendar_name": f"{who.capitalize()} reminders"}
            )
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
        await add_router(hass)
        await one_off(hass, "alex", "Charge the car", "2026-12-02 08:00:00")
        await one_off(hass, "sam", "Charge the car for the trip", "2026-12-02 09:00:00")
        await one_off(hass, "household", "Bins out", "2026-11-27 19:00:00")
        await hass.services.async_call(
            DOMAIN, "create_reminder_event",
            {"calendar": "calendar.alex_reminders", "summary": "Take the pills",
             "description": "assistant: ada", "start": "2026-11-26T18:00:00+01:00",
             "repeat": "weekdays", "times": 10},
            blocking=True, return_response=True,
        )
        yield


async def one_off(hass, who, title, start):
    await hass.services.async_call(
        "calendar", "create_event",
        {"entity_id": f"calendar.{who}_reminders", "summary": title,
         "start_date_time": start, "end_date_time": start.replace(":00:00", ":05:00")},
        blocking=True,
    )


async def look_up(hass, who=None, context=None, **data):
    context = context or Context()
    note_asker(hass, context, who)
    return await hass.services.async_call(
        DOMAIN, "list_reminder_events", {"reminders": REMINDERS, **data},
        blocking=True, return_response=True, context=context,
    )


def titles(answer) -> list[str]:
    return [found["title"] for found in answer["reminders"]]


async def test_a_person_sees_their_own_and_the_household_reminders(hass, calendars):
    answer = await look_up(hass, "alex")

    assert answer["status"] == "ok"
    assert titles(answer) == ["Take the pills", "Bins out", "Charge the car"]
    assert answer["looked_at"] == ["alex", "household"]
    assert "message" not in answer


async def test_a_person_never_sees_the_other_persons_reminders(hass, calendars):
    answer = await look_up(hass, "alex", keywords="car")

    assert [(r["title"], r["for_whom"]) for r in answer["reminders"]] == [
        ("Charge the car", "alex")
    ]


async def test_the_other_person_sees_theirs_instead(hass, calendars):
    answer = await look_up(hass, "sam", keywords="car")

    assert titles(answer) == ["Charge the car for the trip"]


async def test_when_who_is_asking_is_not_known_only_the_household_is_shown(hass, calendars):
    answer = await look_up(hass, None)

    assert titles(answer) == ["Bins out"]
    assert answer["looked_at"] == ["household"]
    assert "not known" in answer["message"]


async def test_a_name_the_script_does_not_know_gets_the_household_only(hass, calendars):
    answer = await look_up(hass, "robin")

    assert titles(answer) == ["Bins out"]


async def test_a_note_for_another_turn_does_not_count(hass, calendars):
    note_asker(hass, Context(), "alex")

    answer = await hass.services.async_call(
        DOMAIN, "list_reminder_events", {"reminders": REMINDERS},
        blocking=True, return_response=True, context=Context(),
    )

    assert titles(answer) == ["Bins out"]


async def test_a_context_used_again_does_not_keep_the_earlier_person(hass, calendars):
    context = Context()
    note_asker(hass, context, "alex")

    answer = await look_up(hass, None, context=context)

    assert titles(answer) == ["Bins out"]


async def test_a_note_is_dropped_after_a_while(hass, calendars, freezer):
    context = Context()
    note_asker(hass, context, "alex")

    freezer.tick(ASKER_LIFETIME.total_seconds() + 1)

    assert asker(hass, context) is None


async def test_nobody_can_ask_as_the_household_to_be_someone(hass, calendars):
    context = Context()

    note_asker(hass, context, "Household")

    assert asker(hass, context) is None


async def test_a_day_narrows_it_down(hass, calendars):
    answer = await look_up(hass, "alex", on_day="wednesday")

    assert titles(answer) == ["Charge the car", "Take the pills"]
    assert answer["from"] == "Wednesday 2026-12-02 00:00"
    assert answer["until"] == "Wednesday 2026-12-02 23:59"


async def test_a_repeating_reminder_is_reported_once_with_its_exact_settings(hass, calendars):
    answer = await look_up(hass, "alex", keywords="pills", on_day="today", until_day="sunday")

    assert answer["reminders"] == [
        {
            "title": "Take the pills",
            "for_whom": "alex",
            "when": "Thursday 2026-11-26 18:00",
            "repeats": "every weekday",
            "ends": "after 10 times in all",
            "rule": "FREQ=WEEKLY;COUNT=10;BYDAY=MO,TU,WE,TH,FR",
            "times_in_range": 2,
            "duration_minutes": 5,
            "spoken": True,
            "assistant": "ada",
        }
    ]


async def test_nothing_found_is_ok_with_an_empty_list(hass, calendars):
    answer = await look_up(hass, "alex", keywords="dentist")

    assert (answer["status"], answer["count"], answer["reminders"]) == ("ok", 0, [])


async def test_a_day_that_cannot_be_read_is_an_error_with_the_reason(hass, calendars):
    answer = await look_up(hass, "alex", on_day="someday")

    assert answer["status"] == "error"
    assert "today, tomorrow" in answer["message"]


async def test_empty_fields_from_a_script_are_left_out(hass, calendars):
    answer = await look_up(hass, "alex", keywords="", on_day="", until_day="")

    assert answer["count"] == 3


async def test_a_calendar_that_does_not_exist_is_an_error(hass, calendars):
    context = Context()
    note_asker(hass, context, "alex")

    answer = await hass.services.async_call(
        DOMAIN, "list_reminder_events",
        {"reminders": [{"for_whom": "alex", "calendar": ["calendar.nope"]}]},
        blocking=True, return_response=True, context=context,
    )

    assert answer == {"status": "error", "message": "calendar.nope is not a calendar."}


async def test_a_user_who_may_not_read_the_calendar_is_refused(
    hass, calendars, hass_read_only_user
):
    hass_read_only_user.groups = []
    context = Context(user_id=hass_read_only_user.id)

    with pytest.raises(Unauthorized):
        await look_up(hass, "alex", context=context)


async def test_the_example_script_shows_the_person_speaking_their_own_and_the_household(
    hass, calendars
):
    from pathlib import Path  # noqa: PLC0415

    from homeassistant.setup import async_setup_component  # noqa: PLC0415
    from homeassistant.util.yaml import load_yaml  # noqa: PLC0415

    for who in PEOPLE:
        entry = MockConfigEntry(
            domain="local_calendar",
            data={"calendar_name": f"{who.capitalize()} reminders announce"},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await one_off_in(hass, "calendar.sam_reminders_announce", "Call the garage about the car")
    script = load_yaml(Path(__file__).parent.parent / "examples" / "list_reminders.yaml")
    assert await async_setup_component(hass, "script", {"script": {"list_reminders": script}})
    # The main agent's tool call runs the script with the turn's context.
    context = Context()
    note_asker(hass, context, "Alex")

    answer = await hass.services.async_call(
        "script", "list_reminders", {"keywords": "car", "on_day": "wednesday"},
        blocking=True, return_response=True, context=context,
    )

    assert answer["status"] == "ok"
    assert [(r["title"], r["for_whom"], r["spoken"]) for r in answer["reminders"]] == [
        ("Charge the car", "alex", False)
    ]


async def one_off_in(hass, calendar, title):
    await hass.services.async_call(
        "calendar", "create_event",
        {"entity_id": calendar, "summary": title,
         "start_date_time": "2026-12-02 10:00:00", "end_date_time": "2026-12-02 10:05:00"},
        blocking=True,
    )
