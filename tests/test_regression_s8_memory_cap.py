# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for S8 of the 2026-11-27 review: remembered facts have an end.

A fact a person asked to remember ("told") is never dropped for a newer one, and adding
one had no upper limit, so "remember ..." said often enough grew a list, and with it every
prompt, without end. Lists shared by every assistant also outlived the last assistant.
"""

import logging

import pytest
from homeassistant.core import HomeAssistant

from custom_components.voice_assistant_manager.favorites import async_get_favorites
from custom_components.voice_assistant_manager.memory import (
    MAX_MAX_ITEMS,
    async_get_memory,
    entry_key,
    global_key,
)

from .router_helpers import add_router

KEY = "entry:kitchen:owner"


async def filled(hass: HomeAssistant, source: str = "told"):
    memory = await async_get_memory(hass)
    for number in range(MAX_MAX_ITEMS):
        await memory.async_add(KEY, f"Fact number {number}", source)  # type: ignore[arg-type]
    return memory


async def test_a_full_list_of_told_facts_takes_no_more(hass: HomeAssistant, caplog):
    memory = await filled(hass)

    with caplog.at_level(logging.WARNING):
        added = await memory.async_add(KEY, "One fact too many", "told")

    assert added is None
    assert len(memory.items(KEY)) == MAX_MAX_ITEMS
    assert "One fact too many" not in [item["text"] for item in memory.items(KEY)]
    assert "full" in caplog.text
    assert "One fact too many" not in caplog.text


async def test_a_full_list_makes_room_for_a_told_fact_by_dropping_a_judged_one(
    hass: HomeAssistant,
):
    memory = await filled(hass, "judged")

    added = await memory.async_add(KEY, "Alex is allergic to nuts", "told")

    texts = [item["text"] for item in memory.items(KEY)]
    assert added is not None
    assert len(texts) == MAX_MAX_ITEMS
    assert "Alex is allergic to nuts" in texts
    assert "Fact number 0" not in texts


@pytest.mark.parametrize("max_items", [None, MAX_MAX_ITEMS * 5])
async def test_no_setting_lets_a_list_grow_past_the_most_it_may_hold(
    hass: HomeAssistant, max_items
):
    memory = await filled(hass)

    await memory.async_add(KEY, "One fact too many", "told", max_items=max_items)

    assert len(memory.items(KEY)) == MAX_MAX_ITEMS


async def test_removing_the_last_assistant_deletes_the_lists_shared_by_all(hass: HomeAssistant):
    entry = await add_router(hass)
    memory = await async_get_memory(hass)
    favorites = await async_get_favorites(hass)
    await memory.async_add(global_key("owner"), "Alex is vegetarian", "told")
    await favorites.async_add("household", {"name": "Jazz", "url": "https://example.com/jazz"})
    await favorites.async_add("person:owner", {"name": "News", "url": "https://example.com/n"})

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert memory.items(global_key("owner")) == []
    assert favorites.items("household") == []
    assert favorites.items("person:owner") == []


async def test_removing_one_of_two_assistants_keeps_the_lists_shared_by_all(
    hass: HomeAssistant,
):
    entry = await add_router(hass)
    other = await add_router(hass, title="Office assistant")
    memory = await async_get_memory(hass)
    favorites = await async_get_favorites(hass)
    await memory.async_add(global_key("owner"), "Alex is vegetarian", "told")
    await memory.async_add(entry_key(other.entry_id, "owner"), "Alex works upstairs", "told")
    await favorites.async_add("household", {"name": "Jazz", "url": "https://example.com/jazz"})

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert [item["text"] for item in memory.items(global_key("owner"))] == ["Alex is vegetarian"]
    assert len(memory.items(entry_key(other.entry_id, "owner"))) == 1
    assert len(favorites.items("household")) == 1
