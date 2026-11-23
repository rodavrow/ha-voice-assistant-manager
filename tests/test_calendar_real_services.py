# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The calendar watcher against Home Assistant's own calendar and script integrations.

The other tests stand in for calendar.get_events and script.turn_on. Here both are the
real ones, so the shape of the request and of the answer is Home Assistant's.
"""

from datetime import UTC, date, datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockPlatform, async_capture_events, mock_platform

from custom_components.voice_assistant_manager.calendar_watch import get_watcher

from .router_helpers import FakeAgents, add_router
from .test_calendar_watch import LINE, SCRIPT, WASTE, WASTE_ROW, calendar_options

FAMILY_ROW = {"calendar": WASTE, "for": "owner"}


class WasteCalendar(CalendarEntity):
    """A calendar entity with a fixed list of events."""

    _attr_name = "Waste"

    def __init__(self, events: list[CalendarEvent]) -> None:
        self._events = events

    @property
    def event(self) -> CalendarEvent | None:
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [
            event
            for event in self._events
            if event.end_datetime_local > start_date and event.start_datetime_local < end_date
        ]


async def setup_home(hass: HomeAssistant, events: list[CalendarEvent]) -> list:
    """A real calendar.waste, and a real script that reports what it was called with."""
    await hass.config.async_set_time_zone("Europe/Amsterdam")

    async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
        async_add_entities([WasteCalendar(events)])

    mock_platform(hass, "test.calendar", MockPlatform(async_setup_platform=async_setup_platform))
    assert await async_setup_component(hass, "calendar", {"calendar": {"platform": "test"}})
    received = {
        name: f"{{{{ {name} }}}}"
        for name in (
            "owner", "title", "line", "assistant", "calendar", "start", "end", "all_day", "kind",
        )
    }
    sequence = [{"event": "script_received", "event_data": received}]
    assert await async_setup_component(
        hass, "script", {"script": {SCRIPT.split(".")[1]: {"sequence": sequence}}}
    )
    await hass.async_block_till_done()
    assert hass.states.get(WASTE) is not None
    return async_capture_events(hass, "script_received")


async def test_a_real_all_day_event_reaches_a_real_script(hass: HomeAssistant):
    events = [
        CalendarEvent(start=date(2026, 11, 23), end=date(2026, 11, 24), summary="Avalex: GFT")
    ]
    received = await setup_home(hass, events)
    entry = await add_router(hass, calendar_options(WASTE_ROW))

    with FakeAgents(writer_reply=LINE).patched():
        await get_watcher(hass, entry.entry_id).async_check(
            datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
        )
        await hass.async_block_till_done()

    assert [event.data for event in received] == [
        {
            "owner": "household",
            "title": "Avalex: GFT",
            "line": LINE,
            "assistant": "kitchen",
            "calendar": WASTE,
            "start": "2026-11-23",
            "end": "2026-11-24",
            "all_day": True,
            "kind": "calendar",
        }
    ]


async def test_a_real_timed_event_reaches_a_real_script(hass: HomeAssistant):
    events = [
        CalendarEvent(
            start=datetime(2026, 11, 21, 17, 55, tzinfo=UTC),
            end=datetime(2026, 11, 21, 18, 0, tzinfo=UTC),
            summary="Dentist",
        )
    ]
    received = await setup_home(hass, events)
    entry = await add_router(hass, calendar_options(FAMILY_ROW))

    with FakeAgents(writer_reply=LINE).patched():
        await get_watcher(hass, entry.entry_id).async_check(
            datetime(2026, 11, 21, 17, 55, 20, tzinfo=UTC)
        )
        await hass.async_block_till_done()

    (event,) = received
    assert event.data["owner"] == "alex"
    assert event.data["title"] == "Dentist"
    assert event.data["all_day"] is False
    assert datetime.fromisoformat(event.data["start"]) == datetime(2026, 11, 21, 17, 55, tzinfo=UTC)


async def test_a_real_event_that_is_not_due_reaches_nobody(hass: HomeAssistant):
    events = [
        CalendarEvent(start=date(2026, 11, 23), end=date(2026, 11, 24), summary="Avalex: GFT")
    ]
    received = await setup_home(hass, events)
    entry = await add_router(hass, calendar_options(WASTE_ROW))

    with FakeAgents(writer_reply=LINE).patched():
        await get_watcher(hass, entry.entry_id).async_check(
            datetime(2026, 11, 22, 17, 0, tzinfo=UTC)
        )
        await hass.async_block_till_done()

    assert received == []
