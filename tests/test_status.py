# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""status.router_status on its own: the lines the settings page shows for one assistant.

test_panel.py covers most lines through the page; these are the ones it does not reach.
"""

from homeassistant.core import HomeAssistant

from custom_components.voice_assistant_manager.status import router_status

from .router_helpers import MAIN_AGENT, add_router, options_with


def lines(hass: HomeAssistant, entry, settings) -> list[tuple[str, str, str]]:
    return [(line["level"], line["section"], line["text"]) for line in router_status(
        hass, entry, settings
    )]


async def test_an_assistant_without_a_main_agent_is_an_error(hass: HomeAssistant):
    entry = await add_router(hass)
    hass.config_entries.async_update_entry(entry, data={"name": entry.title, "agent": ""})

    found = lines(hass, entry, options_with(agents={"agent": ""}))

    assert ("error", "assistant", "No main agent is chosen.") in found


async def test_memory_switched_on_without_an_agent_is_warned_about(hass: HomeAssistant):
    entry = await add_router(hass)

    found = lines(hass, entry, options_with(memory={"agent": ""}))

    assert (
        "warn", "memory", "Memory is on, but no agent decides what to save."
    ) in found


async def test_memory_switched_off_is_only_mentioned(hass: HomeAssistant):
    entry = await add_router(hass)

    found = lines(hass, entry, options_with(memory={"enabled": False}))

    assert ("info", "memory", "Memory is off.") in found


async def test_settings_that_are_not_sections_do_not_break_the_status(hass: HomeAssistant):
    entry = await add_router(hass)
    hass.states.async_set(MAIN_AGENT, "unknown")

    found = router_status(hass, entry, {"memory": "on", "agents": None})

    assert ("info", "memory", "Memory is off.") in [
        (line["level"], line["section"], line["text"]) for line in found
    ]
