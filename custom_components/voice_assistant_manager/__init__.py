# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Voice Assistant Manager: picks a persona per speaker, then hands over to a real agent."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from . import avatar_devices, avatar_stream
from .announce import async_register_services
from .avatar import SUBENTRY_TYPE as AVATAR
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
from .reminder_actions import async_register_reminder_actions
from .stage_api import async_register_stage_commands

PLATFORMS = [Platform.CONVERSATION]
# Per loaded entry: the platforms it set up (so unloading matches) and its avatars.
_LOADED = "loaded_entries"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services and the settings page, which belong to no one router entry."""
    async_register_services(hass)
    async_register_reminder_actions(hass)
    async_register_chat_services(hass)
    await async_setup_panel(hass)
    async_register_commands(hass)
    async_register_mine_commands(hass)
    async_register_stage_commands(hass)
    async_register_favorites_api(hass)
    avatar_devices.async_setup(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await async_get_memory(hass)
    platforms = _platforms(entry)
    hass.data.setdefault(DOMAIN, {}).setdefault(_LOADED, {})[entry.entry_id] = (
        platforms,
        _avatars(entry),
    )
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    await async_start_watcher(hass, entry)
    avatar_devices.async_request(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    loaded = hass.data.get(DOMAIN, {}).get(_LOADED, {})
    platforms = loaded[entry.entry_id][0] if entry.entry_id in loaded else PLATFORMS
    if not await hass.config_entries.async_unload_platforms(entry, platforms):
        return False
    loaded.pop(entry.entry_id, None)
    avatar_stream.forget_turns(hass, entry.entry_id)
    await async_stop_watcher(hass, entry)
    return True


def _platforms(entry: ConfigEntry) -> list[Platform]:
    """The conversation agent, plus text to speech only when the assistant has an avatar."""
    if any(sub.subentry_type == AVATAR for sub in entry.subentries.values()):
        return [*PLATFORMS, Platform.TTS]
    return list(PLATFORMS)


def _avatars(entry: ConfigEntry) -> dict[str, str]:
    """subentry id -> name of the entry's avatars."""
    return {
        sub_id: sub.title
        for sub_id, sub in entry.subentries.items()
        if sub.subentry_type == AVATAR
    }


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when an avatar was added, removed or renamed, so its entity follows.

    Other avatar settings are read when speaking, and other changes need no reload.
    """
    loaded = hass.data.get(DOMAIN, {}).get(_LOADED, {}).get(entry.entry_id)
    if loaded is not None and loaded[1] != _avatars(entry):
        hass.config_entries.async_schedule_reload(entry.entry_id)
    # Satellites follow the avatar's settings and switch, and the session lifetime.
    avatar_devices.async_request(hass)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget what this assistant remembered. Lists shared by every assistant stay."""
    memory = await async_get_memory(hass)
    await memory.async_remove_entry(entry.entry_id)
    await async_remove_watcher_data(hass, entry)
