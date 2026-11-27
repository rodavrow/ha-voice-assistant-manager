# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keeps every chat participant's avatar session open from the first line to the last.

A satellite in avatar mode opens a LiveTalking session for a slot set to "Connect when
woken" only while that assistant speaks. In a chat the two assistants take turns, so each
line would close one session and open another, and LiveTalking's picture stutters on every
session it is running while a new one starts. So for the length of a chat, the slots of the
assistants in it are held at "Always connected" on the satellite showing it, and the chat
waits until each is connected before its first line.

The hold is temporary: the assistant's own setting is never written. avatar_devices' check
leaves a held select alone, and writes the setting back once the chat releases it, the
satellite returns from being away, or the hold's safety time has passed. A slot that cannot
be held (no room on the server, a refusal, no connection in time) is let go at once and
connects when woken, as it does outside a chat. Nothing here stops a chat from starting.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from . import avatar_devices
from .avatar import MAX_SESSIONS, device_sessions
from .avatar_devices import CONNECTION_OPTIONS, SlotEntities
from .avatar_hold import async_wait_available

_LOGGER = logging.getLogger(__name__)

ALWAYS = CONNECTION_OPTIONS["always"]
# How long each session is given to connect before the first line; the device itself gives
# up after 15 s.
CONNECT_WAIT = 15.0
# A hold outlasts the chat's own time limit by this much, so a chat whose end was missed
# cannot keep the sessions open.
SAFETY_MARGIN = timedelta(minutes=5)
# What the device's "Avatar state" sensor says.
CONNECTED = "connected"
CONNECTING = "connecting"
FAILED = "failed"


def safety_until(started: datetime, minutes: int) -> datetime:
    """Until when a chat that started then, allowed this many minutes, may hold sessions."""
    return started + timedelta(minutes=minutes) + SAFETY_MARGIN


def _state(hass: HomeAssistant, entity_id: str | None) -> str | None:
    state = hass.states.get(entity_id) if entity_id else None
    return state.state if state is not None else None


def _participants(
    hass: HomeAssistant, satellite: str, routers: Iterable[str]
) -> list[tuple[int, str, SlotEntities, bool]]:
    """(slot, assistant title, entities, whether its setting is "Connect when woken") for each
    slot of the satellite whose wake word uses an assistant in the chat with its avatar on."""
    wanted = set(routers)
    found: list[tuple[int, str, SlotEntities, bool]] = []
    on_satellite = avatar_devices.satellite_slots(hass).get(satellite) or {}
    for slot, entry_id in sorted(on_satellite.items()):
        if entry_id not in wanted:
            continue
        entry = hass.config_entries.async_get_entry(entry_id)
        wish = avatar_devices.wish_for(entry)
        entities = avatar_devices.slot_entities(hass, satellite, slot)
        title = entry.title if entry is not None else entry_id
        if not wish.on or entities is None or entities.connection is None:
            _LOGGER.debug(
                "Chat avatars: %s slot %d (%s) has no avatar to hold", satellite, slot, title
            )
            continue
        if _state(hass, entities.connection) in (None, STATE_UNAVAILABLE):
            _LOGGER.info(
                "Chat avatars: %s slot %d (%s) is away; it connects when woken",
                satellite, slot, title,
            )
            continue
        found.append((slot, title, entities, wish.connection != ALWAYS))
    return found


async def async_hold(
    hass: HomeAssistant,
    satellite: str,
    routers: Iterable[str],
    until: datetime,
    held: list[str],
) -> None:
    """Connect the chat's assistants on the satellite and wait until they are connected.

    Each select this sets to "Always connected" is added to `held` as soon as it is held, so
    a chat cancelled in the middle still releases it (async_release). Never raises, except
    when cancelled.
    """
    try:
        await _async_hold(hass, satellite, routers, until, held)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a bug here must not stop the chat
        _LOGGER.exception("Chat avatars: connecting the assistants failed; the chat goes on")


async def _async_hold(
    hass: HomeAssistant,
    satellite: str,
    routers: Iterable[str],
    until: datetime,
    held: list[str],
) -> None:
    room = MAX_SESSIONS - len(device_sessions(hass))
    waiting: list[tuple[int, str, SlotEntities, bool]] = []
    for slot, title, entities, override in _participants(hass, satellite, routers):
        if _state(hass, entities.state) not in (CONNECTED, CONNECTING):
            if room <= 0:
                _LOGGER.warning(
                    "Chat avatars: no room for %s slot %d (%s): LiveTalking takes %d sessions "
                    "and all are in use; it connects when woken",
                    satellite, slot, title, MAX_SESSIONS,
                )
                continue
            room -= 1
        select = entities.connection
        assert select is not None  # _participants keeps only slots with one  # noqa: S101
        if override:
            # Held before writing, so a check running meanwhile does not write it back.
            avatar_devices.hold_connection(hass, select, until)
            held.append(select)
            if _state(hass, select) != ALWAYS and not await avatar_devices.async_call(
                hass, "select", "select_option", select, {"option": ALWAYS}
            ):
                _LOGGER.warning(
                    "Chat avatars: %s slot %d (%s) refused %s; it connects when woken",
                    satellite, slot, title, ALWAYS,
                )
                await _async_let_go(hass, held, select)
                continue
        waiting.append((slot, title, entities, override))

    results = await asyncio.gather(
        *(_async_wait_connected(hass, entities.state) for _, _, entities, _ in waiting)
    )
    for (slot, title, entities, override), state in zip(waiting, results, strict=True):
        if state == CONNECTED:
            _LOGGER.info("Chat avatars: %s slot %d (%s) is connected", satellite, slot, title)
            continue
        if state is None:
            _LOGGER.info(
                "Chat avatars: %s slot %d (%s) has no state sensor; the chat does not wait for it",
                satellite, slot, title,
            )
            continue
        _LOGGER.warning(
            "Chat avatars: %s slot %d (%s) not connected (%s after %.0f s); it connects when "
            "woken",
            satellite, slot, title, state, CONNECT_WAIT,
        )
        if override and entities.connection is not None:
            await _async_let_go(hass, held, entities.connection)


async def _async_wait_connected(hass: HomeAssistant, sensor: str | None) -> str | None:
    """The sensor's state once connected or failed, or after CONNECT_WAIT; None without one."""
    if sensor is None or hass.states.get(sensor) is None:
        return None
    done = asyncio.Event()

    @callback
    def changed(event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        if new_state is not None and new_state.state in (CONNECTED, FAILED):
            done.set()

    unwatch = async_track_state_change_event(hass, [sensor], changed)
    try:
        if _state(hass, sensor) not in (CONNECTED, FAILED):
            async with asyncio.timeout(CONNECT_WAIT):
                await done.wait()
    except TimeoutError:
        pass
    finally:
        unwatch()
    return _state(hass, sensor)


async def _async_let_go(hass: HomeAssistant, held: list[str], select: str) -> None:
    """Release one select early and have its setting written back."""
    avatar_devices.release_connection(hass, select)
    if select in held:
        held.remove(select)
    await _async_write_back(hass)


async def async_release(hass: HomeAssistant, held: list[str]) -> None:
    """Let each held select follow its assistant's setting again, and write the settings back.

    A satellite that is away is waited for (avatar_hold.OFF_WAIT), as its select would come
    back still at "Always connected"; past that, the minute check puts it back. Never raises.
    """
    selects = list(held)
    held.clear()
    if not selects:
        return
    for select in selects:
        avatar_devices.release_connection(hass, select)
    _LOGGER.info("Chat avatars: released %s", ", ".join(selects))
    try:
        await asyncio.gather(
            *(
                async_wait_available(hass, select)
                for select in selects
                if _state(hass, select) == STATE_UNAVAILABLE
            )
        )
        await _async_write_back(hass)
    except Exception:  # noqa: BLE001 - the minute check is the safety net
        _LOGGER.exception("Chat avatars: writing the settings back failed; the next check will")


async def _async_write_back(hass: HomeAssistant) -> None:
    if not avatar_devices.async_request(hass):
        await avatar_devices.async_reconcile(hass)
