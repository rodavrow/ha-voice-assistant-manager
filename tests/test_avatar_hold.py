# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A chat show asks a satellite with an "Avatar hold" switch to keep the avatar on screen."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import avatar_hold
from custom_components.voice_assistant_manager.chat_show import ChatLimits

from .test_chat_show import (  # noqa: F401 - fixtures
    PLAYER,
    SATELLITE,
    chat_audio,
    gate,
    player,
    run_chat,
    show,
    start,
)

HOLD = "switch.kitchen_satellite_avatar_hold"
TWO_LINES = ChatLimits(max_lines=2, pause=0)


class FakeHold:
    """The satellite's hold switch: records each call and what was spoken in between."""

    def __init__(self, hass: HomeAssistant, show: SimpleNamespace, state: str = "off") -> None:
        self.hass = hass
        self.events: list[str] = []
        self.fails = False
        self.after_line: str | None = None
        owner = MockConfigEntry(domain="esphome")
        owner.add_to_hass(hass)
        er.async_get(hass).async_get_or_create(
            "switch",
            "esphome",
            "aa:bb:cc:dd:ee:ff/0/switch/Avatar hold",
            config_entry=owner,
            device_id=show.device,
            suggested_object_id="kitchen_satellite_avatar_hold",
        )
        hass.states.async_set(HOLD, state)
        hass.services.async_register("switch", "turn_on", self._turn("on"))
        hass.services.async_register("switch", "turn_off", self._turn("off"))
        show.speakers.hook = self._line

    def _turn(self, state: str):
        async def turn(call: ServiceCall) -> None:
            if self.fails:
                raise HomeAssistantError("the device did not answer")
            self.events.append(f"hold {state}")
            self.hass.states.async_set(call.data["entity_id"], state)

        return turn

    async def _line(self, number: int) -> None:
        self.events.append("line")
        if self.after_line is not None:
            self.hass.states.async_set(HOLD, self.after_line)


@pytest.fixture
def hold(hass, show) -> FakeHold:
    return FakeHold(hass, show)


async def test_the_hold_is_on_for_the_chat_and_off_after_it(hass, show, hold):
    await run_chat(hass, show, limits=TWO_LINES)

    assert hold.events == ["hold on", "line", "line", "hold off"]


async def test_a_hold_the_satellite_let_go_of_is_asked_for_again(hass, show, hold):
    hold.after_line = "off"

    await run_chat(hass, show, limits=TWO_LINES)

    assert hold.events == ["hold on", "line", "hold on", "line"]


async def test_the_hold_is_let_go_when_the_chat_is_stopped_mid_line(hass, show, hold):
    playing, release = gate(show)
    await start(show)
    await playing.wait()

    show.manager._cancel(show.manager._chat)
    await hass.async_block_till_done(wait_background_tasks=True)
    release.set()

    assert hold.events == ["hold on", "hold off"]


async def test_a_satellite_without_the_switch_just_chats(hass, show):
    await run_chat(hass, show, limits=TWO_LINES)

    assert len(show.speakers.announced) == 2
    assert show.manager.state()["ended"] == "chat_done"


async def test_a_hold_that_cannot_be_set_does_not_end_the_chat(hass, show, hold, caplog):
    hold.fails = True

    await run_chat(hass, show, limits=TWO_LINES)

    assert show.manager.state()["ended"] == "chat_done"
    assert any(
        r.levelno == logging.WARNING and HOLD in r.getMessage() for r in caplog.records
    )


async def test_an_unavailable_hold_switch_is_left_alone(hass, show, hold):
    hass.states.async_set(HOLD, "unavailable")

    with patch.object(avatar_hold, "OFF_WAIT", 0.01):
        await run_chat(hass, show, limits=TWO_LINES)

    assert hold.events == ["line", "line"]


async def test_a_media_player_has_no_hold(hass, show, player, hold):
    await run_chat(hass, show, speaker=PLAYER, limits=TWO_LINES)

    assert [event for event in hold.events if event != "line"] == []


def test_only_a_switch_named_avatar_hold_on_the_satellites_own_device_counts(hass, show):
    other = MockConfigEntry(domain="esphome")
    other.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "switch", "esphome", "aa:bb:cc:dd:ee:ff/0/switch/Wake on motion", device_id=show.device
    )
    er.async_get(hass).async_get_or_create(
        "switch", "esphome", "11:22:33:44:55:66/0/switch/Avatar hold", config_entry=other
    )

    assert avatar_hold.hold_switch(hass, SATELLITE) is None


async def test_regression_the_hold_is_turned_off_once_a_dropped_switch_is_back(hass, show, hold):
    """Seen: the satellite dropped as the chat ended, came back with the hold still on."""
    hass.states.async_set(HOLD, "unavailable")

    turning_off = hass.async_create_task(avatar_hold.async_set(hass, SATELLITE, False))
    await asyncio.sleep(0)
    hass.states.async_set(HOLD, "on")
    await turning_off

    assert hold.events == ["hold off"]


async def test_a_switch_that_stays_away_is_given_up_on(hass, show, hold):
    hass.states.async_set(HOLD, "unavailable")

    with patch.object(avatar_hold, "OFF_WAIT", 0.01):
        await avatar_hold.async_set(hass, SATELLITE, False)

    assert hold.events == []


async def test_turning_the_hold_on_never_waits_for_an_unavailable_switch(hass, show, hold):
    hass.states.async_set(HOLD, "unavailable")

    await asyncio.wait_for(avatar_hold.async_set(hass, SATELLITE, True), timeout=1)

    assert hold.events == []
