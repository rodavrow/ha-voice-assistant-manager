# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Voice Assistant Manager: picks a persona per speaker, then hands over to a real agent."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .announce import async_register_services
from .calendar_watch import (
    async_remove_watcher_data,
    async_start_watcher,
    async_stop_watcher,
)
from .const import DOMAIN
from .favorites_llm import async_register_favorites_api
from .memory import async_get_memory
from .mine_api import async_register_mine_commands
from .panel import async_setup_panel
from .panel_api import async_register_chat_services, async_register_commands

PLATFORMS = [Platform.CONVERSATION]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services and the settings page, which belong to no one router entry."""
    async_register_services(hass)
    async_register_chat_services(hass)
    await async_setup_panel(hass)
    async_register_commands(hass)
    async_register_mine_commands(hass)
    async_register_favorites_api(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await async_get_memory(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_start_watcher(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await async_stop_watcher(hass, entry)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget what this assistant remembered. Lists shared by every assistant stay."""
    memory = await async_get_memory(hass)
    await memory.async_remove_entry(entry.entry_id)
    await async_remove_watcher_data(hass, entry)
