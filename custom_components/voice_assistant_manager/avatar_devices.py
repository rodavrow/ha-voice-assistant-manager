# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Satellites that can show an avatar follow their assistants' avatars.

Some satellite firmware has an avatar mode per wake-word slot: the device then shows the
assistant's LiveTalking stream and plays its speech through it. It offers these entities
on the sub-device of each slot, named "<device> assistant N" (N = 1 for the first wake
word), and they are found by the ESPHome type and name in their unique_id:

    switch       "Avatar mode"        avatar mode on or off
    text         "Avatar URL"         the LiveTalking address
    text         "Avatar ID"          the LiveTalking avatar to ask for (empty: the default)
    select       "Avatar connection"  "Always connected" or "Connect when woken"
    text_sensor  "Avatar session"     the device's LiveTalking session, while it has one

A slot whose wake word uses an assistant with an avatar switched on gets avatar mode on,
with that avatar's settings; a slot whose wake word uses one of these assistants without
one gets it off. A slot that uses no assistant of this integration is only switched off
when this integration switched it on (remembered across restarts), so avatar mode set by
hand elsewhere stays. Satellites without these entities are never touched. Nothing here
raises: a device that refuses is logged once.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .avatar import CONF_AVATAR_ID, CONF_ENABLED, CONF_URL, SUBENTRY_TYPE, esphome_entity
from .const import DOMAIN
from .setup_guide import find_satellites

_LOGGER = logging.getLogger(__name__)

# The connection setting under Advanced, and the option the device's select offers for it.
CONNECTION_OPTIONS = {"always": "Always connected", "on_wake": "Connect when woken"}
DEFAULT_CONNECTION = "on_wake"
CALL_TIMEOUT = 5.0
RECHECK = timedelta(minutes=1)
_DATA = "avatar_devices"
_OWNED = "avatar_devices_owned"
_WARNED = "avatar_devices_warned"
_STORE_KEY = f"{DOMAIN}.avatar_devices"
# Home Assistant's wake-word select of slot N: unique_id <mac>-pipeline, <mac>-pipeline_N.
_WAKE_SELECT = re.compile(r"-pipeline(?:_(\d+))?$")
# While an assistant sets up or unloads, its slots look unused: change nothing then.
_BUSY = (ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.UNLOAD_IN_PROGRESS)
# The sub-device of a slot: its own name (not the one the user gave it) ends in this.
_SLOT_DEVICE = re.compile(r"\bassistant\s+(\d+)$", re.IGNORECASE)
# kind -> (ESPHome type, entity name) as the firmware names them; never translated.
ENTITY_NAMES = {
    "mode": ("switch", "Avatar mode"),
    "url": ("text", "Avatar URL"),
    "id": ("text", "Avatar ID"),
    "connection": ("select", "Avatar connection"),
}


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


def satellite_slots(hass: HomeAssistant) -> dict[str, dict[int, str | None]]:
    """satellite -> {slot: the assistant (config entry id) its wake word uses, or None}.

    The slot (1-based) comes from the key of the wake word's select, not from the order of
    the selects, so a missing or disabled first select does not shift the second.
    A satellite whose wake words are not known (assist_pipeline not set up) is left out.
    """
    registry = er.async_get(hass)
    slots: dict[str, dict[int, str | None]] = {}
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        for satellite in find_satellites(hass, entry):
            if not satellite["wake_words"]:
                _LOGGER.debug("Avatar check: %s has no known wake words", satellite["entity_id"])
            for word in satellite["wake_words"]:
                select = registry.async_get(word["select"])
                match = _WAKE_SELECT.search(select.unique_id) if select else None
                if match is None:
                    continue
                slot = int(match.group(1) or 1)
                row = slots.setdefault(satellite["entity_id"], {})
                if word["uses_router"]:
                    row[slot] = row.get(slot) or entry.entry_id
                else:
                    row.setdefault(slot, None)
    return slots


def slot_entities(hass: HomeAssistant, satellite: str, slot: int) -> SlotEntities | None:
    """The avatar entities of one slot (1-based): on the satellite's sub-device named
    "… assistant <slot>", found by type and name in their unique_id (avatar.esphome_entity)."""
    entities = er.async_get(hass)
    found = entities.async_get(satellite)
    if found is None or found.device_id is None:
        return None
    sub_device = next(
        (
            device.id
            for device in dr.async_get(hass).devices
            if device.via_device_id == found.device_id
            and (match := _SLOT_DEVICE.search(device.name or ""))
            and int(match.group(1)) == slot
        ),
        None,
    )
    if sub_device is None:
        return None
    named: dict[str, str] = {}
    for entity in er.async_entries_for_device(entities, sub_device):
        kind = next(
            (k for k, name in ENTITY_NAMES.items() if esphome_entity(entity.unique_id) == name),
            None,
        )
        if kind is not None:
            named[kind] = entity.entity_id
    if "mode" not in named or "url" not in named:
        return None  # Never avatar mode without a place for the address.
    return SlotEntities(named["mode"], named.get("url"), named.get("id"), named.get("connection"))


class _Owned:
    """The avatar-mode switches (registry ids) this integration switched on, kept on disk."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, list[str]]] = Store(hass, 1, _STORE_KEY)
        self.ids: set[str] = set()
        self._saved: set[str] = set()
        self._loaded = False

    async def async_load(self) -> None:
        if not self._loaded:
            data = await self._store.async_load() or {}
            self.ids = set(data.get("owned") or [])
            self._saved = set(self.ids)
            self._loaded = True

    async def async_save(self) -> None:
        if self.ids != self._saved:
            await self._store.async_save({"owned": sorted(self.ids)})
            self._saved = set(self.ids)


async def async_reconcile(hass: HomeAssistant) -> None:
    """Bring every satellite slot in line with its assistant's avatar. Never raises."""
    if any(entry.state in _BUSY for entry in hass.config_entries.async_entries(DOMAIN)):
        _LOGGER.debug("Avatar check skipped: an assistant is setting up or unloading")
        return  # The next request or the minute check catches up.
    data = hass.data.setdefault(DOMAIN, {})
    owned: _Owned = data.setdefault(_OWNED, _Owned(hass))
    await owned.async_load()
    registry = er.async_get(hass)
    seen: set[str] = set()
    found = satellite_slots(hass)
    if not found:
        _LOGGER.debug("Avatar check: no satellite with known wake words")
    for satellite, assistants in found.items():
        for slot, entry_id in assistants.items():
            entities = slot_entities(hass, satellite, slot)
            switch = registry.async_get(entities.switch) if entities else None
            if entities is None or switch is None:
                _LOGGER.debug(
                    "Avatar check: %s slot %d has no avatar entities (sub-device "
                    "'… assistant %d' with 'Avatar mode' and 'Avatar URL')",
                    satellite, slot, slot,
                )
                continue
            seen.add(switch.id)
            entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
            if entry is None and switch.id not in owned.ids:
                _LOGGER.debug(
                    "Avatar check: %s slot %d uses no assistant of this integration "
                    "(its wake word's pipeline has another conversation agent); left alone",
                    satellite, slot,
                )
                continue  # Not ours: avatar mode set by hand for another assistant stays.
            wish = wish_for(entry)
            _LOGGER.debug(
                "Avatar check: %s slot %d uses %s: avatar mode %s",
                satellite, slot, entry.title if entry else "no assistant",
                "on" if wish.on else "off",
            )
            _remember(owned, switch.id, await _async_apply(hass, entities, wish))
    # Switched on by us, but no slot uses one of our assistants any more (removed, say).
    # Only when wake words can be known: otherwise every slot would look unused.
    orphans = owned.ids - seen if "assist_pipeline" in hass.config.components else set()
    for switch_id in orphans:
        switch = registry.async_get(switch_id)
        if switch is None:
            owned.ids.discard(switch_id)
            continue
        _remember(owned, switch_id, await _async_turn_off(hass, switch.entity_id))
    await owned.async_save()


def _remember(owned: _Owned, switch_id: str, now_on: bool | None) -> None:
    if now_on is True:
        owned.ids.add(switch_id)
    elif now_on is False:
        owned.ids.discard(switch_id)


async def _async_apply(
    hass: HomeAssistant, entities: SlotEntities, wish: SlotWish
) -> bool | None:
    """Apply a wish. Returns whether avatar mode is now on, None when that is unknown."""
    if not wish.on:
        return await _async_turn_off(hass, entities.switch)
    settings = (
        ("text", "set_value", entities.url, "value", wish.url),
        ("text", "set_value", entities.avatar_id, "value", wish.avatar_id),
        ("select", "select_option", entities.connection, "option", wish.connection),
    )
    for domain, service, entity_id, field, value in settings:
        if entity_id is None or _state(hass, entity_id) == value:
            continue
        if not await _async_call(hass, domain, service, entity_id, {field: value}):
            return None  # Never turn avatar mode on with settings the device did not take.
    state = _state(hass, entities.switch)
    if state == "on":
        return True
    if state == "off" and await _async_call(hass, "switch", "turn_on", entities.switch, {}):
        return True
    return None


async def _async_turn_off(hass: HomeAssistant, switch: str) -> bool | None:
    state = _state(hass, switch)
    if state == "off":
        return False
    if state == "on" and await _async_call(hass, "switch", "turn_off", switch, {}):
        return False
    return None  # Unavailable, or the device refused: try again at the next check.


def _state(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    return state.state if state is not None else None


async def _async_call(
    hass: HomeAssistant, domain: str, service: str, entity_id: str, data: dict[str, Any]
) -> bool:
    warned: set[str] = hass.data.setdefault(DOMAIN, {}).setdefault(_WARNED, set())
    try:
        async with asyncio.timeout(CALL_TIMEOUT):
            await hass.services.async_call(
                domain, service, {"entity_id": entity_id, **data}, blocking=True
            )
    except (HomeAssistantError, TimeoutError, vol.Invalid) as err:
        # Once per entity until it works again: the check runs every minute.
        level = logging.DEBUG if entity_id in warned else logging.WARNING
        warned.add(entity_id)
        _LOGGER.log(
            level, "Could not set %s for the avatar: %s", entity_id, err or type(err).__name__
        )
        return False
    warned.discard(entity_id)
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
    def started(_hass: HomeAssistant) -> None:
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

    async_at_started(hass, started)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stopping)
