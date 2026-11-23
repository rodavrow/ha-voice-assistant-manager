# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Show which assistant speaks, on devices that offer a character select.

Some satellite firmware offers a select whose entity id ends in
`_next_announcement_assistant`, with the options "Default", "Assistant 1", "Assistant 2".
The option chosen applies to the next announcement only. Before a line is spoken here, the
speaking assistant's wake-word slot on that satellite picks the option. Devices without the
select are never touched, and the switch on the Voice Manager page turns it all off.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .setup_guide import find_satellites

_LOGGER = logging.getLogger(__name__)

SELECT_SUFFIX = "_next_announcement_assistant"
DEFAULT_OPTION = "Default"
# How long a select is given to take the option before the line goes on without it.
SELECT_TIMEOUT = 3.0
_SATELLITE_PREFIX = "assist_satellite."
# In hass.data[DOMAIN]: False when switched off on the page. Kept here, not in the panel
# store's state, because the panel module imports the chat show, which imports this one.
_DATA_ENABLED = "announcement_look"


def is_enabled(hass: HomeAssistant) -> bool:
    """Whether the character is chosen before announcements; on unless switched off."""
    return hass.data.get(DOMAIN, {}).get(_DATA_ENABLED, True) is not False


def set_enabled(hass: HomeAssistant, enabled: bool) -> None:
    hass.data.setdefault(DOMAIN, {})[_DATA_ENABLED] = enabled


def option_for(slot: int | None, options: Sequence[str]) -> str:
    """The option for a wake-word slot (0 is the first): "Assistant N", else the default."""
    if slot is None:
        return DEFAULT_OPTION
    wanted = f"Assistant {slot + 1}"
    return wanted if wanted in options else DEFAULT_OPTION


def look_select(hass: HomeAssistant, satellite: str) -> str | None:
    """The character select on the satellite's device, or None when it has none."""
    registry = er.async_get(hass)
    entity = registry.async_get(satellite)
    if entity is None or entity.device_id is None:
        return None
    for candidate in er.async_entries_for_device(registry, entity.device_id):
        if candidate.domain == "select" and candidate.entity_id.endswith(SELECT_SUFFIX):
            return candidate.entity_id
    return None


def wake_slot(hass: HomeAssistant, satellite: str, entry: ConfigEntry) -> int | None:
    """The first wake-word slot on the satellite whose pipeline uses this assistant."""
    for found in find_satellites(hass, entry):
        if found["entity_id"] != satellite:
            continue
        for slot, word in enumerate(found["wake_words"]):
            if word["uses_router"]:
                return slot
    return None


async def async_show_assistant(
    hass: HomeAssistant, satellite: str, entry: ConfigEntry | None
) -> None:
    """Choose the character for the satellite's next announcement. Never raises.

    `entry` is the assistant about to speak; None shows the device's default character.
    """
    if not satellite.startswith(_SATELLITE_PREFIX) or not is_enabled(hass):
        return
    try:
        await _async_choose(hass, satellite, entry)
    except TimeoutError:
        _LOGGER.warning("The character select of %s did not answer in time", satellite)
    except Exception:  # noqa: BLE001 - any platform may offer the select; the line goes on
        # The line is still spoken, with the device's default character.
        _LOGGER.warning("Could not choose the character on %s", satellite, exc_info=True)


async def _async_choose(hass: HomeAssistant, satellite: str, entry: ConfigEntry | None) -> None:
    select = look_select(hass, satellite)
    state = hass.states.get(select) if select is not None else None
    if select is None or state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return
    options = state.attributes.get("options") or []
    option = option_for(wake_slot(hass, satellite, entry) if entry else None, options)
    async with asyncio.timeout(SELECT_TIMEOUT):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": select, "option": option}, blocking=True
        )
