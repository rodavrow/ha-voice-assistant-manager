# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The example scripts in examples/ load in Home Assistant and do what the docs say."""

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml

EXAMPLES = Path(__file__).parent.parent / "examples"
ZONE = "Europe/Amsterdam"


async def _setup_script(hass: HomeAssistant, name: str) -> None:
    config = load_yaml(EXAMPLES / f"{name}.yaml")
    assert await async_setup_component(hass, "script", {"script": {name: config}})
    await hass.async_block_till_done()


def _capture(hass: HomeAssistant, domain: str, service: str) -> list[ServiceCall]:
    calls: list[ServiceCall] = []

    async def handler(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register(domain, service, handler, supports_response=SupportsResponse.NONE)
    return calls


@pytest.fixture
async def reminder(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> list[ServiceCall]:
    """create_reminder loaded, at Wednesday 2026-11-18 14:30 in Amsterdam."""
    await hass.config.async_set_time_zone(ZONE)
    freezer.move_to(datetime(2026, 11, 18, 14, 30, tzinfo=ZoneInfo(ZONE)))
    calls = _capture(hass, "calendar", "create_event")
    await _setup_script(hass, "create_reminder")
    return calls


async def _remind(hass: HomeAssistant, **fields: Any) -> dict[str, Any]:
    data = {"title": "Call the plumber", "for_whom": "alex", "assistant": "jeeves", **fields}
    response = await hass.services.async_call(
        "script", "create_reminder", data, blocking=True, return_response=True
    )
    assert isinstance(response, dict)
    return response


def _start(call: ServiceCall) -> datetime:
    start = call.data["start_date_time"]
    return start if isinstance(start, datetime) else datetime.fromisoformat(str(start))


def _targets(call: ServiceCall) -> list[str]:
    targets = call.data["entity_id"]
    return [targets] if isinstance(targets, str) else list(targets)


def _local(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=ZoneInfo(ZONE))


async def test_create_reminder_in_minutes(hass: HomeAssistant, reminder: list[ServiceCall]) -> None:
    response = await _remind(hass, in_minutes=20)

    assert response["status"] == "ok"
    assert _start(reminder[0]) == _local(2026, 11, 18, 14, 50)


async def test_create_reminder_event_lasts_five_minutes(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    await _remind(hass, in_minutes=20)

    end = datetime.fromisoformat(str(reminder[0].data["end_date_time"]))
    assert end == _local(2026, 11, 18, 14, 55)


async def test_create_reminder_tells_the_agent_when_it_is_due(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    response = await _remind(hass, at_time="17:00", on_day="friday")

    assert response["due"] == "Friday 2026-11-20 17:00"


async def test_create_reminder_writes_to_the_announce_calendar_of_the_person(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    await _remind(hass, in_minutes=5)

    assert _targets(reminder[0]) == ["calendar.alex_reminders_announce"]
    assert reminder[0].data["summary"] == "Call the plumber"
    assert "assistant: jeeves" in reminder[0].data["description"]


async def test_create_reminder_without_announce_uses_the_quiet_calendar(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    await _remind(hass, in_minutes=5, announce=False)

    assert _targets(reminder[0]) == ["calendar.alex_reminders"]


async def test_create_reminder_at_a_time_later_today(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    await _remind(hass, at_time="17:00")

    assert _start(reminder[0]) == _local(2026, 11, 18, 17, 0)


async def test_create_reminder_at_a_time_already_past_means_tomorrow(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    await _remind(hass, at_time="09:00")

    assert _start(reminder[0]) == _local(2026, 11, 19, 9, 0)


async def test_create_reminder_tomorrow(hass: HomeAssistant, reminder: list[ServiceCall]) -> None:
    await _remind(hass, at_time="18:00", on_day="tomorrow")

    assert _start(reminder[0]) == _local(2026, 11, 19, 18, 0)


async def test_create_reminder_on_a_weekday(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    await _remind(hass, at_time="10:00", on_day="Friday")

    assert _start(reminder[0]) == _local(2026, 11, 20, 10, 0)


async def test_create_reminder_on_todays_weekday_after_the_time_means_next_week(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    await _remind(hass, at_time="09:00", on_day="wednesday")

    assert _start(reminder[0]) == _local(2026, 11, 25, 9, 0)


async def test_create_reminder_on_a_date_this_year(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    await _remind(hass, at_time="08:00", on_day="12-24")

    assert _start(reminder[0]) == _local(2026, 12, 24, 8, 0)


async def test_create_reminder_on_a_date_already_past_means_next_year(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    await _remind(hass, at_time="08:00", on_day="03-01")

    assert _start(reminder[0]) == _local(2027, 3, 1, 8, 0)


async def test_create_reminder_keeps_the_wall_clock_across_a_clock_change(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, reminder: list[ServiceCall]
) -> None:
    freezer.move_to(_local(2027, 3, 26, 12, 0))

    await _remind(hass, at_time="09:00", on_day="monday")

    assert _start(reminder[0]) == _local(2027, 3, 29, 9, 0)


async def test_create_reminder_without_a_time_is_an_error(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    response = await _remind(hass)

    assert response["status"] == "error"
    assert "in_minutes" in response["message"]
    assert reminder == []


async def test_create_reminder_with_a_bad_time_is_an_error(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    response = await _remind(hass, at_time="five o'clock")

    assert response["status"] == "error"
    assert reminder == []


async def test_create_reminder_with_a_bad_day_is_an_error(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    response = await _remind(hass, at_time="10:00", on_day="someday")

    assert response["status"] == "error"
    assert reminder == []


async def test_create_reminder_for_an_unknown_person_is_an_error(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    response = await _remind(hass, in_minutes=5, for_whom="visitor")

    assert response["status"] == "error"
    assert reminder == []


async def test_create_reminder_with_both_minutes_and_time_is_an_error(
    hass: HomeAssistant, reminder: list[ServiceCall]
) -> None:
    response = await _remind(hass, in_minutes=5, at_time="17:00")

    assert response["status"] == "error"
    assert reminder == []


@pytest.fixture
async def announce(hass: HomeAssistant) -> list[ServiceCall]:
    calls = _capture(hass, "assist_satellite", "announce")
    await _setup_script(hass, "calendar_announce")
    return calls


async def _announce(hass: HomeAssistant, line: str) -> None:
    data = {
        "owner": "household", "title": "Avalex: GFT", "line": line, "assistant": "jeeves",
        "calendar": "calendar.waste", "start": "2026-11-23", "end": "2026-11-24",
        "all_day": True, "kind": "calendar",
    }
    await hass.services.async_call("script", "calendar_announce", data, blocking=True)


async def test_calendar_announce_speaks_the_line(
    hass: HomeAssistant, announce: list[ServiceCall]
) -> None:
    await _announce(hass, "Alex, Sam, the green bin goes out tomorrow.")

    assert announce[0].data["message"] == "Alex, Sam, the green bin goes out tomorrow."


async def test_calendar_announce_falls_back_to_the_title(
    hass: HomeAssistant, announce: list[ServiceCall]
) -> None:
    await _announce(hass, "")

    assert announce[0].data["message"] == "Avalex: GFT"
