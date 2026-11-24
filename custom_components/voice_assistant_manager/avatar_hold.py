# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keeps a satellite's avatar on screen between the lines of a chat show.

A satellite in avatar mode shows the avatar only while it speaks; between two chat lines it
would go back to its own screen. Firmware with an "Avatar hold" switch keeps the avatar
that spoke last on screen, idle, while the switch is on. The device turns the switch off
by itself a minute after its last announcement, so a missed "off" never leaves it on.
"""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .avatar import esphome_entity

_LOGGER = logging.getLogger(__name__)

# The switch's name in the firmware: fixed English, on the satellite's own device.
HOLD_NAME = "Avatar hold"
CALL_TIMEOUT = 5


def hold_switch(hass: HomeAssistant, satellite: str) -> str | None:
    """The entity id of the satellite's "Avatar hold" switch, or None if it has none."""
    entities = er.async_get(hass)
    found = entities.async_get(satellite)
    if found is None or found.device_id is None:
        return None
    for entity in er.async_entries_for_device(entities, found.device_id):
        if entity.domain == "switch" and esphome_entity(entity.unique_id) == (
            "switch",
            HOLD_NAME,
        ):
            return entity.entity_id
    return None


async def async_set(hass: HomeAssistant, satellite: str, on: bool) -> None:
    """Turn the satellite's hold on or off. Never raises; nothing without the switch."""
    switch = hold_switch(hass, satellite)
    state = hass.states.get(switch) if switch is not None else None
    if switch is None or state is None or state.state == STATE_UNAVAILABLE:
        return
    if state.state == (STATE_ON if on else STATE_OFF):
        return
    try:
        async with asyncio.timeout(CALL_TIMEOUT):
            await hass.services.async_call(
                "switch", "turn_on" if on else "turn_off", {"entity_id": switch}, blocking=True
            )
    except (HomeAssistantError, TimeoutError, vol.Invalid) as err:
        # The chat goes on; the screen just goes back to its own between lines.
        _LOGGER.warning(
            "Could not turn %s %s: %s", switch, "on" if on else "off", err or type(err).__name__
        )
