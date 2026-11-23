# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for listing, adding and deleting one person's reminders on the My assistant page."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from homeassistant.components.calendar import CalendarEntity, CalendarEntityFeature, CalendarEvent
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockPlatform, mock_platform

from custom_components.voice_assistant_manager.mine import (
    MyRouter,
    ReminderRefused,
    async_add_reminder,
    async_delete_reminder,
    async_list_reminders,
    my_router,
)

from .test_mine import add_entry

NOW = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
ALEX = "calendar.alex_reminders"
SAM = "calendar.sam_reminders"
HOUSE = "calendar.house_reminders"
READ_ONLY = "calendar.read_only"
CREATE_AND_DELETE = CalendarEntityFeature.CREATE_EVENT | CalendarEntityFeature.DELETE_EVENT


@pytest.fixture(autouse=True)
def clock_at_now(freezer: Any) -> None:
    """Deleting looks ahead from the real clock, so the tests pin it to NOW."""
    freezer.move_to(NOW)


def at(hours: int) -> datetime:
    return NOW + timedelta(hours=hours)


def event(uid: str, summary: str, hours: int, recurrence_id: str | None = None) -> CalendarEvent:
    return CalendarEvent(
        start=at(hours),
        end=at(hours + 1),
        summary=summary,
        uid=uid,
        recurrence_id=recurrence_id,
    )


class StoredCalendar(CalendarEntity):
    """A calendar that keeps events with uids and records what was deleted from it."""

    def __init__(self, name: str, events: list[CalendarEvent], features: int) -> None:
        self._attr_name = name
        self._attr_supported_features = features
        self.events = events
        self.deleted: list[tuple[str, str | None]] = []
        self.deleted_ranges: list[str | None] = []

    @property
    def event(self) -> CalendarEvent | None:
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [
            found
            for found in self.events
            if found.end_datetime_local > start_date and found.start_datetime_local < end_date
        ]

    async def async_delete_event(
        self, uid: str, recurrence_id: str | None = None, recurrence_range: str | None = None
    ) -> None:
        self.deleted.append((uid, recurrence_id))
        self.deleted_ranges.append(recurrence_range)
        self.events = [
            found
            for found in self.events
            if not (found.uid == uid and found.recurrence_id == recurrence_id)
        ]


class BrokenCalendar(StoredCalendar):
    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        raise RuntimeError("the calendar server is down")


class HangingCalendar(StoredCalendar):
    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        await asyncio.Event().wait()  # never set: the calendar does not answer
        return []


async def setup_calendars(
    hass: HomeAssistant, *calendars: StoredCalendar
) -> dict[str, StoredCalendar]:
    async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
        async_add_entities(list(calendars))

    mock_platform(hass, "test.calendar", MockPlatform(async_setup_platform=async_setup_platform))
    assert await async_setup_component(hass, "calendar", {"calendar": {"platform": "test"}})
    await hass.async_block_till_done()
    return {calendar.entity_id: calendar for calendar in calendars}


def reminders_setup(hass: HomeAssistant, **reminders: Any) -> MyRouter:
    entry = add_entry(
        hass,
        "Kitchen",
        {"user": "u-alex", "name": "Alex"},
        {"user": "u-sam", "name": "Sam"},
        reminders,
    )
    mine = my_router(hass, "u-alex", entry.entry_id)
    assert mine is not None
    return mine


def register_script(hass: HomeAssistant) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def create_reminder(call: ServiceCall) -> dict[str, Any]:
        calls.append(dict(call.data))
        return {"status": "ok"}

    hass.services.async_register(
        "script", "create_reminder", create_reminder, supports_response=SupportsResponse.ONLY
    )
    return calls


def standard_calendars() -> tuple[StoredCalendar, ...]:
    return (
        StoredCalendar("Alex reminders", [event("a1", "Call the dentist", 2)], CREATE_AND_DELETE),
        StoredCalendar("Sam reminders", [event("s1", "Sam's secret", 3)], CREATE_AND_DELETE),
        StoredCalendar("House reminders", [event("h1", "Put the bins out", 4)], CREATE_AND_DELETE),
    )


async def test_lists_own_and_household_reminders_only(hass: HomeAssistant) -> None:
    await setup_calendars(hass, *standard_calendars())
    mine = reminders_setup(
        hass, owner_calendars=[ALEX], partner_calendars=[SAM], household_calendars=[HOUSE]
    )

    result = await async_list_reminders(hass, mine, NOW)

    assert [(r["title"], r["whose"], r["calendar_name"]) for r in result["reminders"]] == [
        ("Call the dentist", "mine", "Alex reminders"),
        ("Put the bins out", "household", "House reminders"),
    ]
    assert result["unreadable"] == []
    first = result["reminders"][0]
    assert (first["calendar"], first["uid"], first["can_delete"], first["all_day"]) == (
        ALEX,
        "a1",
        True,
        False,
    )
    assert first["start"] == at(2).isoformat()


async def test_reminders_are_sorted_by_start_across_calendars(hass: HomeAssistant) -> None:
    all_day = CalendarEvent(
        start=date(2026, 11, 23), end=date(2026, 11, 24), summary="Bin day", uid="d1"
    )
    await setup_calendars(
        hass,
        StoredCalendar("Alex reminders", [event("a1", "Late call", 30)], CREATE_AND_DELETE),
        StoredCalendar(
            "House reminders",
            [all_day, event("h1", "Early errand", 2)],
            CREATE_AND_DELETE,
        ),
    )
    mine = reminders_setup(hass, owner_calendars=[ALEX], household_calendars=[HOUSE])

    result = await async_list_reminders(hass, mine, NOW)

    assert [r["title"] for r in result["reminders"]] == ["Early errand", "Bin day", "Late call"]


async def test_the_other_persons_reminders_are_never_listed(hass: HomeAssistant) -> None:
    await setup_calendars(hass, *standard_calendars())
    mine = reminders_setup(hass, owner_calendars=[ALEX], partner_calendars=[SAM])

    result = await async_list_reminders(hass, mine, NOW)

    assert "Sam's secret" not in [r["title"] for r in result["reminders"]]
    assert SAM not in [r["calendar"] for r in result["reminders"]]


async def test_a_calendar_listed_twice_shows_each_event_once(hass: HomeAssistant) -> None:
    await setup_calendars(hass, *standard_calendars())
    mine = reminders_setup(hass, owner_calendars=[ALEX, ALEX], household_calendars=[ALEX, HOUSE])

    result = await async_list_reminders(hass, mine, NOW)

    assert [r["title"] for r in result["reminders"]] == ["Call the dentist", "Put the bins out"]


async def test_a_missing_calendar_is_named_and_the_rest_still_listed(
    hass: HomeAssistant,
) -> None:
    await setup_calendars(hass, *standard_calendars())
    mine = reminders_setup(hass, owner_calendars=["calendar.gone", ALEX])

    result = await async_list_reminders(hass, mine, NOW)

    assert result["unreadable"] == ["calendar.gone"]
    assert [r["title"] for r in result["reminders"]] == ["Call the dentist"]


async def test_a_calendar_that_fails_is_named_and_the_rest_still_listed(
    hass: HomeAssistant,
) -> None:
    broken = BrokenCalendar("Broken", [], CREATE_AND_DELETE)
    await setup_calendars(hass, *standard_calendars(), broken)
    mine = reminders_setup(hass, owner_calendars=["calendar.broken", ALEX])

    result = await async_list_reminders(hass, mine, NOW)

    assert result["unreadable"] == ["calendar.broken"]
    assert [r["title"] for r in result["reminders"]] == ["Call the dentist"]


async def test_regression_security_a_calendar_that_hangs_is_named_within_the_timeout(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.voice_assistant_manager import mine  # noqa: PLC0415

    monkeypatch.setattr(mine, "FETCH_TIMEOUT", 0, raising=False)  # frozen clock: 0 fires at once
    hanging = HangingCalendar("Hanging", [], CREATE_AND_DELETE)
    await setup_calendars(hass, *standard_calendars(), hanging)
    mine_router = reminders_setup(hass, owner_calendars=["calendar.hanging", ALEX])

    task = asyncio.ensure_future(async_list_reminders(hass, mine_router, NOW))
    for _ in range(100):  # turns of the loop, not time: the clock is frozen
        await asyncio.sleep(0)
    if not task.done():
        task.cancel()
    assert task.done() and not task.cancelled(), "the listing did not return"
    result = task.result()

    assert result["unreadable"] == ["calendar.hanging"]
    assert [r["title"] for r in result["reminders"]] == ["Call the dentist"]


async def test_add_for_me_calls_the_script_with_my_name(hass: HomeAssistant) -> None:
    calls = register_script(hass)
    mine = reminders_setup(hass, assistant="Jeeves")

    response = await async_add_reminder(hass, mine, "me", "Call the dentist", True, 15, None, None)

    assert response == {"status": "ok"}
    assert calls == [
        {
            "title": "Call the dentist",
            "for_whom": "alex",
            "assistant": "jeeves",
            "announce": True,
            "in_minutes": 15,
        }
    ]


async def test_add_for_the_household(hass: HomeAssistant) -> None:
    calls = register_script(hass)
    mine = reminders_setup(hass)

    await async_add_reminder(hass, mine, "household", "Bins", False, None, "18:30", "tomorrow")

    assert calls[0]["for_whom"] == "household"
    assert calls[0]["assistant"] == "kitchen"
    assert calls[0]["announce"] is False


async def test_add_passes_only_the_time_fields_given(hass: HomeAssistant) -> None:
    calls = register_script(hass)
    mine = reminders_setup(hass)

    await async_add_reminder(hass, mine, "me", "Tea", True, None, "18:30", None)
    await async_add_reminder(hass, mine, "me", "Tea", True, None, None, None)

    assert "in_minutes" not in calls[0]
    assert "on_day" not in calls[0]
    assert calls[0]["at_time"] == "18:30"
    assert not {"in_minutes", "at_time", "on_day"} & calls[1].keys()


async def test_add_without_the_script_is_refused(hass: HomeAssistant) -> None:
    mine = reminders_setup(hass)

    with pytest.raises(ReminderRefused) as refused:
        await async_add_reminder(hass, mine, "me", "Tea", True, 5, None, None)

    assert refused.value.key == "mine_no_script"


async def test_delete_removes_an_own_event(hass: HomeAssistant) -> None:
    calendars = await setup_calendars(hass, *standard_calendars())
    mine = reminders_setup(hass, owner_calendars=[ALEX], household_calendars=[HOUSE])

    await async_delete_reminder(hass, mine, ALEX, "a1", None)

    assert calendars[ALEX].deleted == [("a1", None)]


async def test_delete_of_another_calendars_event_is_refused(hass: HomeAssistant) -> None:
    calendars = await setup_calendars(hass, *standard_calendars())
    mine = reminders_setup(hass, owner_calendars=[ALEX], partner_calendars=[SAM])

    with pytest.raises(ReminderRefused) as refused:
        await async_delete_reminder(hass, mine, SAM, "s1", None)

    assert refused.value.key == "mine_cannot_delete"
    assert calendars[SAM].deleted == []


async def test_delete_of_an_unknown_uid_is_refused(hass: HomeAssistant) -> None:
    calendars = await setup_calendars(hass, *standard_calendars())
    mine = reminders_setup(hass, owner_calendars=[ALEX])

    with pytest.raises(ReminderRefused) as refused:
        await async_delete_reminder(hass, mine, ALEX, "nope", None)

    assert refused.value.key == "mine_cannot_delete"
    assert calendars[ALEX].deleted == []


async def test_delete_one_occurrence_of_a_repeating_reminder(hass: HomeAssistant) -> None:
    repeating = [
        event("r1", "Water the plants", 24, recurrence_id="20261123"),
        event("r1", "Water the plants", 48, recurrence_id="20261124"),
    ]
    calendars = await setup_calendars(
        hass, StoredCalendar("Alex reminders", repeating, CREATE_AND_DELETE)
    )
    mine = reminders_setup(hass, owner_calendars=[ALEX])

    await async_delete_reminder(hass, mine, ALEX, "r1", "20261124")

    assert calendars[ALEX].deleted == [("r1", "20261124")]
    # No range: only that one occurrence goes, never "this and the ones after it".
    assert calendars[ALEX].deleted_ranges == [None]
    assert [found.recurrence_id for found in calendars[ALEX].events] == ["20261123"]


async def test_delete_of_a_wrong_occurrence_is_refused(hass: HomeAssistant) -> None:
    repeating = [event("r1", "Water the plants", 24, recurrence_id="20261123")]
    calendars = await setup_calendars(
        hass, StoredCalendar("Alex reminders", repeating, CREATE_AND_DELETE)
    )
    mine = reminders_setup(hass, owner_calendars=[ALEX])

    with pytest.raises(ReminderRefused):
        await async_delete_reminder(hass, mine, ALEX, "r1", "20269999")

    assert calendars[ALEX].deleted == []


async def test_delete_on_a_calendar_without_delete_support_is_refused(
    hass: HomeAssistant,
) -> None:
    read_only = StoredCalendar(
        "Read only", [event("x1", "Fixed", 2)], CalendarEntityFeature.CREATE_EVENT
    )
    await setup_calendars(hass, read_only)
    mine = reminders_setup(hass, owner_calendars=[READ_ONLY])

    result = await async_list_reminders(hass, mine, NOW)
    with pytest.raises(ReminderRefused) as refused:
        await async_delete_reminder(hass, mine, READ_ONLY, "x1", None)

    assert result["reminders"][0]["can_delete"] is False
    assert refused.value.key == "mine_cannot_delete"
    assert read_only.deleted == []
