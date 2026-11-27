# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A chat show keeps every participant's avatar session open from its first line to its last."""

import asyncio
import logging
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from custom_components.voice_assistant_manager import avatar_chat, avatar_devices
from custom_components.voice_assistant_manager.avatar_devices import CONNECTION_OPTIONS
from custom_components.voice_assistant_manager.chat_show import ChatLimits

from .router_helpers import add_router, options_with
from .test_avatar_devices import (  # noqa: F401 - fixtures
    MAC,
    SATELLITE,
    add_entity,
    device,
    reconcile,
    slots,
)
from .test_avatar_tts import add_avatar, engines, sent  # noqa: F401 - fixtures
from .test_chat_show import gate, run_chat, show, start  # noqa: F401 - fixtures

TWO_LINES = ChatLimits(max_lines=2, pause=0)
ALWAYS = CONNECTION_OPTIONS["always"]
WOKEN = CONNECTION_OPTIONS["on_wake"]


def select(slot: int) -> str:
    return f"select.kitchen_assistant_{slot}_avatar_connection"


def state_sensor(slot: int) -> str:
    return f"sensor.kitchen_assistant_{slot}_avatar_state"


class Screen:
    """The satellite's LiveTalking links: a slot whose select reads "Always connected"
    connects (connecting, then `outcome`), and one set back to "Connect when woken" goes off."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.outcome: dict[int, str | None] = {1: "connected", 2: "connected"}
        devices = dr.async_get(hass)
        owner = next(iter(hass.config_entries.async_entries("esphome")))
        for slot in (1, 2):
            index = slot + 3
            sub = next(d for d in devices.devices if d.name == f"kitchen assistant {slot}")
            add_entity(
                hass, owner, sub.id, "sensor", f"{MAC}/{index}/text_sensor/Avatar state",
                "off", named=f"kitchen_assistant_{slot}_avatar_state",
            )
        async_track_state_change_event(hass, [select(1), select(2)], self._changed)

    @callback
    def _changed(self, event) -> None:
        slot = 1 if event.data["entity_id"] == select(1) else 2
        new = event.data["new_state"]
        if new is None:
            return
        if new.state == ALWAYS:
            self.hass.states.async_set(state_sensor(slot), "connecting")
            outcome = self.outcome[slot]
            if outcome is not None:
                self.hass.loop.call_soon(self.hass.states.async_set, state_sensor(slot), outcome)
        elif new.state == WOKEN:
            self.hass.states.async_set(state_sensor(slot), "off")


@pytest.fixture
def screen(hass, device) -> Screen:
    return Screen(hass)


async def two_assistants(hass, first: str = "on_wake", second: str = "on_wake"):
    jeeves = await add_router(hass, options_with(advanced={"avatar_connection": first}))
    ada = await add_router(hass, options_with(advanced={"avatar_connection": second}))
    await add_avatar(hass, jeeves, "tts.cloud_voice")
    await add_avatar(hass, ada, "tts.cloud_voice")
    return jeeves, ada


def later() -> object:
    return dt_util.utcnow() + timedelta(minutes=10)


async def hold(hass, routers, held: list[str], until=None) -> None:
    await avatar_chat.async_hold(hass, SATELLITE, routers, until or later(), held)
    await hass.async_block_till_done()


async def release(hass, held: list[str]) -> None:
    await avatar_chat.async_release(hass, held)
    await hass.async_block_till_done(wait_background_tasks=True)


# Connecting before the first line


async def test_each_participant_is_connected_before_the_chat_starts(
    hass, engines, sent, device, screen
):
    jeeves, ada = await two_assistants(hass)
    held: list[str] = []
    with slots(jeeves, ada):
        await reconcile(hass)
        device.calls.clear()
        await hold(hass, (jeeves.entry_id, ada.entry_id), held)

    assert device.calls == [
        f"select_option {select(1)} {ALWAYS}",
        f"select_option {select(2)} {ALWAYS}",
    ]
    assert hass.states.get(state_sensor(1)).state == "connected"
    assert hass.states.get(state_sensor(2)).state == "connected"
    assert held == [select(1), select(2)]


async def test_the_assistants_own_setting_is_never_changed(hass, engines, sent, device, screen):
    jeeves, ada = await two_assistants(hass)
    held: list[str] = []
    with slots(jeeves, ada):
        await hold(hass, (jeeves.entry_id, ada.entry_id), held)

    assert jeeves.options["advanced"]["avatar_connection"] == "on_wake"
    assert ada.options["advanced"]["avatar_connection"] == "on_wake"


async def test_the_minute_check_leaves_the_chat_override_in_place(
    hass, engines, sent, device, screen
):
    jeeves, ada = await two_assistants(hass)
    held: list[str] = []
    with slots(jeeves, ada):
        await reconcile(hass)
        await hold(hass, (jeeves.entry_id, ada.entry_id), held)
        device.calls.clear()
        await reconcile(hass)

    assert device.calls == []
    assert hass.states.get(select(1)).state == ALWAYS


async def test_an_assistant_not_in_the_chat_is_not_connected(
    hass, engines, sent, device, screen
):
    jeeves, ada = await two_assistants(hass)
    sam = await add_router(hass)
    held: list[str] = []
    with slots(jeeves, ada):
        await reconcile(hass)
        device.calls.clear()
        await hold(hass, (jeeves.entry_id, sam.entry_id), held)

    assert device.calls == [f"select_option {select(1)} {ALWAYS}"]
    assert hass.states.get(select(2)).state == WOKEN


async def test_another_satellite_is_not_touched(hass, engines, sent, device, screen):
    jeeves, ada = await two_assistants(hass)
    held: list[str] = []
    with slots(jeeves, ada):
        await reconcile(hass)
        device.calls.clear()
        await avatar_chat.async_hold(
            hass, "assist_satellite.office", (jeeves.entry_id, ada.entry_id), later(), held
        )

    assert device.calls == []
    assert held == []


async def test_an_always_connected_assistant_is_waited_for_but_not_overridden(
    hass, engines, sent, device, screen
):
    jeeves, ada = await two_assistants(hass, first="always")
    held: list[str] = []
    with slots(jeeves, ada):
        await reconcile(hass)
        device.calls.clear()
        await hold(hass, (jeeves.entry_id, ada.entry_id), held)

    assert device.calls == [f"select_option {select(2)} {ALWAYS}"]
    assert held == [select(2)]


# Ending the chat


async def test_the_end_of_the_chat_puts_each_setting_back(hass, engines, sent, device, screen):
    jeeves, ada = await two_assistants(hass, first="always")
    held: list[str] = []
    with slots(jeeves, ada):
        await reconcile(hass)
        await hold(hass, (jeeves.entry_id, ada.entry_id), held)
        device.calls.clear()
        await release(hass, held)

    assert device.calls == [f"select_option {select(2)} {WOKEN}"]
    assert hass.states.get(select(1)).state == ALWAYS
    assert hass.states.get(state_sensor(2)).state == "off"
    assert held == []


async def test_a_setting_changed_during_the_chat_is_the_one_put_back(
    hass, engines, sent, device, screen
):
    jeeves, ada = await two_assistants(hass)
    held: list[str] = []
    with slots(jeeves, ada):
        await reconcile(hass)
        await hold(hass, (jeeves.entry_id, ada.entry_id), held)
        hass.config_entries.async_update_entry(
            ada, options=options_with(advanced={"avatar_connection": "always"})
        )
        await hass.async_block_till_done()
        device.calls.clear()
        await release(hass, held)

    assert device.calls == [f"select_option {select(1)} {WOKEN}"]
    assert hass.states.get(select(2)).state == ALWAYS


async def test_a_satellite_away_at_the_end_is_put_back_when_it_returns(
    hass, engines, sent, device, screen
):
    jeeves, ada = await two_assistants(hass)
    held: list[str] = []
    with slots(jeeves, ada):
        await reconcile(hass)
        await hold(hass, (jeeves.entry_id, ada.entry_id), held)
        hass.states.async_set(select(1), "unavailable")
        device.calls.clear()
        task = asyncio.ensure_future(avatar_chat.async_release(hass, held))
        await asyncio.sleep(0.01)
        assert not task.done()  # Waiting for the satellite.
        hass.states.async_set(select(1), ALWAYS)
        await task
        await hass.async_block_till_done(wait_background_tasks=True)

    assert f"select_option {select(1)} {WOKEN}" in device.calls
    assert hass.states.get(select(1)).state == WOKEN


async def test_an_override_past_its_safety_time_is_undone_by_the_minute_check(
    hass, engines, sent, device, screen
):
    jeeves, ada = await two_assistants(hass)
    held: list[str] = []
    with slots(jeeves, ada):
        await reconcile(hass)
        await hold(hass, (jeeves.entry_id, ada.entry_id), held)
        avatar_devices.hold_connection(hass, select(1), dt_util.utcnow() - timedelta(seconds=1))
        device.calls.clear()
        await reconcile(hass)

    assert device.calls == [f"select_option {select(1)} {WOKEN}"]


def test_the_safety_time_outlasts_the_longest_chat():
    started = dt_util.utcnow()
    assert avatar_chat.safety_until(started, 5) == started + timedelta(minutes=5) + (
        avatar_chat.SAFETY_MARGIN
    )


# Falling back to today's behaviour


async def test_a_slot_that_does_not_connect_in_time_goes_back_to_its_setting(
    hass, engines, sent, device, screen, caplog
):
    jeeves, ada = await two_assistants(hass)
    screen.outcome[2] = None  # Stays connecting.
    held: list[str] = []
    with slots(jeeves, ada), patch.object(avatar_chat, "CONNECT_WAIT", 0.05):
        await reconcile(hass)
        with caplog.at_level(logging.WARNING):
            await hold(hass, (jeeves.entry_id, ada.entry_id), held)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert held == [select(1)]
    assert hass.states.get(select(2)).state == WOKEN
    assert any("slot 2" in r.message and "not connected" in r.message for r in caplog.records)


async def test_a_refused_connection_goes_back_to_its_setting_at_once(
    hass, engines, sent, device, screen, caplog
):
    jeeves, ada = await two_assistants(hass)
    screen.outcome[1] = "failed"
    held: list[str] = []
    with slots(jeeves, ada), patch.object(avatar_chat, "CONNECT_WAIT", 30):
        await reconcile(hass)
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(hold(hass, (jeeves.entry_id, ada.entry_id), held), 5)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert held == [select(2)]
    assert hass.states.get(select(1)).state == WOKEN
    assert any("slot 1" in r.message and "failed" in r.message for r in caplog.records)


async def test_no_session_is_opened_past_the_server_limit(
    hass, engines, sent, device, screen, caplog
):
    jeeves, ada = await two_assistants(hass)
    taken = frozenset(f"s{n}" for n in range(avatar_chat.MAX_SESSIONS - 1))
    held: list[str] = []
    with (
        slots(jeeves, ada),
        patch.object(avatar_chat, "device_sessions", return_value=taken),
        caplog.at_level(logging.WARNING),
    ):
        await reconcile(hass)
        device.calls.clear()
        await hold(hass, (jeeves.entry_id, ada.entry_id), held)

    assert device.calls == [f"select_option {select(1)} {ALWAYS}"]
    assert any("slot 2" in r.message and "sessions" in r.message for r in caplog.records)


async def test_a_satellite_whose_select_is_away_is_skipped(hass, engines, sent, device, screen):
    jeeves, ada = await two_assistants(hass)
    held: list[str] = []
    with slots(jeeves, ada):
        await reconcile(hass)
        hass.states.async_set(select(1), "unavailable")
        device.calls.clear()
        await hold(hass, (jeeves.entry_id, ada.entry_id), held)

    assert device.calls == [f"select_option {select(2)} {ALWAYS}"]


async def test_a_device_that_refuses_the_select_falls_back(hass, engines, sent, device, screen):
    jeeves, ada = await two_assistants(hass)
    held: list[str] = []
    with slots(jeeves, ada):
        await reconcile(hass)
        device.fail = True
        await hold(hass, (jeeves.entry_id, ada.entry_id), held)

    assert held == []
    assert not avatar_devices.connection_held(hass, select(1))


async def test_firmware_without_a_state_sensor_is_not_waited_for(hass, engines, sent, device):
    jeeves, ada = await two_assistants(hass)
    held: list[str] = []
    with slots(jeeves, ada), patch.object(avatar_chat, "CONNECT_WAIT", 30):
        await reconcile(hass)
        await asyncio.wait_for(hold(hass, (jeeves.entry_id, ada.entry_id), held), 5)

    assert held == [select(1), select(2)]


async def test_a_cancelled_wait_still_leaves_every_written_select_to_release(
    hass, engines, sent, device, screen
):
    jeeves, ada = await two_assistants(hass)
    screen.outcome = {1: None, 2: None}
    held: list[str] = []
    with slots(jeeves, ada):
        await reconcile(hass)
        task = hass.async_create_task(
            avatar_chat.async_hold(hass, SATELLITE, (jeeves.entry_id, ada.entry_id), later(), held)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await release(hass, held)

    assert hass.states.get(select(1)).state == WOKEN
    assert hass.states.get(select(2)).state == WOKEN


# In a chat show


class Recorder:
    """async_hold and async_release, recorded around the chat's lines."""

    def __init__(self, show) -> None:
        self.events: list[str] = []
        self.args: tuple = ()
        show.speakers.hook = self._line

    async def _line(self, number: int) -> None:
        self.events.append("line")

    async def hold(self, hass, satellite, routers, until, held) -> None:
        self.args = (satellite, tuple(routers), until)
        held.append("select.held")
        self.events.append("connect")

    async def release(self, hass, held) -> None:
        self.events.append(f"release {held}")
        held.clear()

    def patched(self):
        return (
            patch.object(avatar_chat, "async_hold", self.hold),
            patch.object(avatar_chat, "async_release", self.release),
        )


@pytest.fixture
def recorder(show):
    rec = Recorder(show)
    hold_patch, release_patch = rec.patched()
    with hold_patch, release_patch:
        yield rec


async def test_a_chat_connects_its_assistants_before_the_first_line_and_releases_after(
    hass, show, recorder
):
    await run_chat(hass, show, limits=TWO_LINES)

    assert recorder.events == ["connect", "line", "line", "release ['select.held']"]
    satellite, routers, until = recorder.args
    assert satellite == SATELLITE
    assert routers == (show.jeeves.entry_id, show.ada.entry_id)
    chat = show.manager._chat
    assert until == avatar_chat.safety_until(chat.started, TWO_LINES.minutes)


async def test_a_chat_stopped_mid_line_still_releases(hass, show, recorder):
    playing, release_line = gate(show)
    await start(show)
    await playing.wait()

    show.manager._cancel(show.manager._chat)
    await hass.async_block_till_done(wait_background_tasks=True)
    release_line.set()

    assert recorder.events == ["connect", "release ['select.held']"]


async def test_a_chat_that_fails_still_releases(hass, show, recorder):
    show.agents.writer_raises = RuntimeError("the writer is down")

    await run_chat(hass, show, limits=TWO_LINES)

    assert recorder.events[0] == "connect"
    assert recorder.events[-1] == "release ['select.held']"


# A chat started before the minute check has seen a changed pipeline


async def test_regression_chat_turns_avatar_mode_on_for_a_wake_word_not_yet_checked(
    hass, engines, sent, device, screen
):
    """A wake word given to an assistant moments ago still has avatar mode off: the minute
    check has not run. Holding its select alone connects nothing, so every line of the chat
    opened a session of its own."""
    jeeves, ada = await two_assistants(hass)
    held: list[str] = []
    with slots(jeeves, ada):
        await hold(hass, (jeeves.entry_id, ada.entry_id), held)

    mode_on = [call for call in device.calls if call.startswith("turn_on switch.")]
    assert len(mode_on) == 2, device.calls
    first_select = device.calls.index(f"select_option {select(1)} {ALWAYS}")
    assert all(device.calls.index(call) < first_select for call in mode_on)
    assert held == [select(1), select(2)]
