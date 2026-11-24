# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Satellites that can show an avatar follow their assistants' avatars.

Some satellite firmware has an avatar mode per wake-word slot: the device then shows the
assistant's LiveTalking stream and plays its speech through it. It offers these entities
on each assistant's sub-device, N being the slot (1 for the first wake word):

    switch  …_avatar_mode_N        avatar mode on or off
    text    …_avatar_url_N         the LiveTalking address
    text    …_avatar_id_N          the LiveTalking avatar to ask for (empty: the default)
    select  …_avatar_connection_N  "Always connected" or "Connect when woken"
    sensor  …_avatar_session_N     the device's LiveTalking session, while it has one

A slot whose wake word uses an assistant with an avatar switched on gets avatar mode on,
with that avatar's settings; every other slot gets it off. Satellites without these
entities are never touched. Nothing here raises: a device that refuses is logged.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval

from .avatar import (
    CONF_AVATAR_ID,
    CONF_ENABLED,
    CONF_URL,
    SUBENTRY_TYPE,
    device_sessions,
    object_ids,
)
from .const import DOMAIN
from .setup_guide import find_satellites

_LOGGER = logging.getLogger(__name__)

# The connection setting under Advanced, and the option the device's select offers for it.
CONNECTION_OPTIONS = {"always": "Always connected", "on_wake": "Connect when woken"}
DEFAULT_CONNECTION = "on_wake"
CALL_TIMEOUT = 5.0
RECHECK = timedelta(minutes=1)
_DATA = "avatar_devices"


@dataclass(frozen=True)
class SlotWish:
    """What one wake-word slot of a satellite should be set to."""

    on: bool
    url: str = ""
    avatar_id: str = ""
    connection: str = CONNECTION_OPTIONS[DEFAULT_CONNECTION]

    @classmethod
    def off(cls) -> SlotWish:
        return cls(on=False)


@dataclass(frozen=True)
class SlotEntities:
    switch: str
    url: str | None
    avatar_id: str | None
    connection: str | None


def wish_for(entry: ConfigEntry | None) -> SlotWish:
    """The wish for a slot whose wake word uses this assistant (None: no assistant)."""
    if entry is None:
        return SlotWish.off()
    avatar = next(
        (
            sub.data
            for sub in entry.subentries.values()
            if sub.subentry_type == SUBENTRY_TYPE and sub.data.get(CONF_ENABLED)
        ),
        None,
    )
    if avatar is None or not avatar.get(CONF_URL):
        return SlotWish.off()
    advanced = entry.options.get("advanced") or {}
    connection = advanced.get("avatar_connection") or DEFAULT_CONNECTION
    return SlotWish(
        on=True,
        url=str(avatar[CONF_URL]),
        avatar_id=str(avatar.get(CONF_AVATAR_ID) or ""),
        connection=CONNECTION_OPTIONS.get(connection, CONNECTION_OPTIONS[DEFAULT_CONNECTION]),
    )


def satellite_slots(hass: HomeAssistant) -> dict[str, list[str | None]]:
    """satellite -> the assistant (config entry id) of each wake-word slot, None for others.

    A satellite whose wake words are not known (assist_pipeline not set up) is left out,
    so it is never touched.
    """
    slots: dict[str, list[str | None]] = {}
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        for satellite in find_satellites(hass, entry):
            words = satellite["wake_words"]
            if not words:
                continue
            row = slots.setdefault(satellite["entity_id"], [None] * len(words))
            for index, word in enumerate(words):
                if word["uses_router"] and index < len(row) and row[index] is None:
                    row[index] = entry.entry_id
    return slots


def slot_entities(hass: HomeAssistant, satellite: str, slot: int) -> SlotEntities | None:
    """The avatar entities of one slot (1-based) on the satellite or its sub-devices,
    found by the object id in their unique_id (see avatar.object_ids)."""
    entities = er.async_get(hass)
    found = entities.async_get(satellite)
    if found is None or found.device_id is None:
        return None
    device_ids = {found.device_id} | {
        device.id
        for device in dr.async_get(hass).devices.values()
        if device.via_device_id == found.device_id
    }
    named: dict[str, str] = {}
    for device_id in device_ids:
        for entity in er.async_entries_for_device(entities, device_id):
            parts = object_ids(entity.unique_id)
            for kind, domain in (
                ("mode", "switch"),
                ("url", "text"),
                ("id", "text"),
                ("connection", "select"),
            ):
                if entity.domain == domain and f"avatar_{kind}_{slot}" in parts:
                    named[kind] = entity.entity_id
    if "mode" not in named:
        return None
    return SlotEntities(named["mode"], named.get("url"), named.get("id"), named.get("connection"))


async def async_reconcile(hass: HomeAssistant) -> None:
    """Bring every satellite slot in line with its assistant's avatar. Never raises."""
    for satellite, assistants in satellite_slots(hass).items():
        for index, entry_id in enumerate(assistants):
            entities = slot_entities(hass, satellite, index + 1)
            if entities is None:
                continue
            entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
            await _async_apply(hass, entities, wish_for(entry))


async def _async_apply(hass: HomeAssistant, entities: SlotEntities, wish: SlotWish) -> None:
    if not wish.on:
        if _state(hass, entities.switch) == "on":
            await _async_call(hass, "switch", "turn_off", entities.switch, {})
        return
    settings = (
        ("text", "set_value", entities.url, "value", wish.url),
        ("text", "set_value", entities.avatar_id, "value", wish.avatar_id),
        ("select", "select_option", entities.connection, "option", wish.connection),
    )
    for domain, service, entity_id, field, value in settings:
        if entity_id is None or _state(hass, entity_id) == value:
            continue
        if not await _async_call(hass, domain, service, entity_id, {field: value}):
            return  # Never turn avatar mode on with settings the device did not take.
    if _state(hass, entities.switch) != "on":
        await _async_call(hass, "switch", "turn_on", entities.switch, {})


def _state(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    return state.state if state is not None else None


async def _async_call(
    hass: HomeAssistant, domain: str, service: str, entity_id: str, data: dict[str, Any]
) -> bool:
    try:
        async with asyncio.timeout(CALL_TIMEOUT):
            await hass.services.async_call(
                domain, service, {"entity_id": entity_id, **data}, blocking=True
            )
    except (HomeAssistantError, TimeoutError) as err:
        _LOGGER.warning("Could not set %s for the avatar: %s", entity_id, err or type(err).__name__)
        return False
    return True


# When to reconcile


class _Runner:
    """Runs reconciles one at a time; a request while one runs makes one more run after it."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._task: asyncio.Task[None] | None = None
        self._again = False

    @callback
    def request(self) -> None:
        if self._task is not None and not self._task.done():
            self._again = True
            return
        self._task = self._hass.async_create_task(self._run(), f"{DOMAIN} avatar devices")

    async def _run(self) -> None:
        while True:
            self._again = False
            try:
                await async_reconcile(self._hass)
            except Exception:  # noqa: BLE001 - a bug here must not stop later checks
                _LOGGER.exception("Updating the satellites' avatar mode failed")
            if not self._again:
                return


@callback
def async_request(hass: HomeAssistant) -> None:
    """Reconcile soon, for example after an avatar or an assistant changed."""
    runner = hass.data.get(DOMAIN, {}).get(_DATA)
    if runner is not None:
        runner.request()


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Reconcile when Home Assistant has started, when avatar entities appear, and every
    minute, which also catches wake words and pipelines that changed."""
    runner = _Runner(hass)
    hass.data.setdefault(DOMAIN, {})[_DATA] = runner

    @callback
    def registry_changed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if event.data["action"] == "create" and event.data["entity_id"].startswith("switch."):
            runner.request()

    @callback
    def started(_event: Event) -> None:
        runner.request()

    @callback
    def tick(_now: Any) -> None:
        runner.request()

    stop_listening = hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_changed)
    stop_timer = async_track_time_interval(hass, tick, RECHECK)

    @callback
    def stopping(_event: Event) -> None:
        stop_listening()
        stop_timer()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, started)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stopping)
