# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unloading and removing an assistant (__init__.py)."""

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.voice_assistant_manager import async_unload_entry
from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.memory import async_get_memory, entry_key

from .router_helpers import add_router, router_entity_id


async def test_an_unloaded_assistant_no_longer_answers(hass: HomeAssistant):
    entry = await add_router(hass)
    entity_id = router_entity_id(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert entry.state is ConfigEntryState.NOT_LOADED
    state = hass.states.get(entity_id)
    assert state is None or state.state == "unavailable"


async def test_an_unloaded_assistant_can_be_loaded_again(hass: HomeAssistant):
    entry = await add_router(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)

    assert await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.LOADED


async def test_when_the_platforms_do_not_unload_the_assistant_stays_loaded(hass: HomeAssistant):
    entry = await add_router(hass)

    with (
        patch.object(hass.config_entries, "async_unload_platforms", return_value=False),
        patch(f"custom_components.{DOMAIN}.async_stop_watcher") as stopped,
    ):
        unloaded = await async_unload_entry(hass, entry)

    assert unloaded is False
    stopped.assert_not_called()
    assert entry.entry_id in hass.data[DOMAIN]["loaded_entries"]


async def test_removing_an_assistant_forgets_what_only_it_remembered(hass: HomeAssistant):
    entry = await add_router(hass)
    memory = await async_get_memory(hass)
    await memory.async_add(entry_key(entry.entry_id, "owner"), "Alex likes tea", source="told")
    assert memory.items(entry_key(entry.entry_id, "owner"))

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert memory.items(entry_key(entry.entry_id, "owner")) == []
    assert er.async_get(hass).async_get_entity_id("conversation", DOMAIN, entry.entry_id) is None
