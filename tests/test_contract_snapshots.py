# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Snapshots of the contracts that are not Python names.

The two pages, saved automations and the main agent's prompt depend on these exactly. Unit
tests check what each means; these catch a change nobody meant to make.
"""

from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockUser

from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.reminders import reminder_guidance
from custom_components.voice_assistant_manager.setup_guide import (
    Satellite,
    tracking_automation,
)

from .router_helpers import MAIN_AGENT, FakeAgents, add_router, ask, options_with
from .snapshot_helpers import assert_matches, stable
from .test_mine_api import Mine, alex_router, fill_memory, mine  # noqa: F401 - fixture
from .test_panel import Page, page  # noqa: F401 - fixture

KITCHEN = Satellite(
    entity_id="assist_satellite.kitchen_satellite",
    device_id="kitchen-device",
    name="Kitchen satellite",
    helper="input_text.voice_speaker_kitchen-device",
    helper_exists=True,
    speaker_sensor="sensor.kitchen_satellite_last_speaker",
    score_sensor="sensor.kitchen_satellite_last_speaker_score",
    tracked=False,
    wake_words=[],
)


def names_of(hass: HomeAssistant, *entries: Any, **more: str) -> dict[str, str]:
    found = {entry.entry_id: f"entry {number}" for number, entry in enumerate(entries, 1)}
    return {**found, **more}


async def test_the_websocket_commands_and_services_keep_their_names(hass: HomeAssistant):
    await add_router(hass)

    commands = sorted(
        name for name in hass.data["websocket_api"] if name.startswith(f"{DOMAIN}/")
    )
    services = sorted(hass.services.async_services().get(DOMAIN, {}))

    assert_matches("names.json", {"websocket_commands": commands, "services": services})


async def test_the_settings_page_get_reply(hass: HomeAssistant, page: Page):  # noqa: F811
    hass.states.async_set(MAIN_AGENT, "unknown")
    entry = await add_router(hass)

    reply = await page.ok("get")

    reply["users"] = [{**user, "id": "<user>"} for user in reply.get("users", [])]
    assert_matches("panel_get.json", stable(reply, names_of(hass, entry)))


async def test_the_settings_page_chat_and_favourites_replies(
    hass: HomeAssistant, page: Page  # noqa: F811
):
    hass.states.async_set(MAIN_AGENT, "unknown")
    entry = await add_router(hass)

    replies = {
        "chat/state": await page.ok("chat/state"),
        "favorites/get": await page.ok("favorites/get"),
        "guide/satellites": await page.ok("guide/satellites", entry_id=entry.entry_id),
    }

    assert_matches("panel_other.json", stable(replies, names_of(hass, entry)))


async def test_the_my_assistant_page_replies(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser  # noqa: F811
):
    entry = await alex_router(hass, hass_read_only_user)
    await fill_memory(hass, entry)

    replies = {
        "get": await mine.ok("get"),
        "favorites/get": await mine.ok("favorites/get"),
    }

    names = names_of(hass, entry, **{hass_read_only_user.id: "user"})
    assert_matches("mine.json", stable(replies, names))


async def test_the_prompt_the_main_agent_gets_for_a_person(hass: HomeAssistant):
    entry = await add_router(
        hass,
        options_with(
            reminders={"enabled": True, "assistant": "jeeves", "cancel": True},
            owner={"user": "u-alex"},
        ),
    )
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, "What is on today?", user_id="u-alex")

    assert_matches("main_prompt.txt", agents.main_prompt)


def test_the_reminder_guidance_text():
    with_cancel = reminder_guidance("Alex", "Sam", "jeeves")
    without = reminder_guidance("Alex", "Sam", "jeeves", cancel=False)

    assert_matches("reminder_guidance.txt", f"{with_cancel}\n\n=== without cancel ===\n\n{without}")


def test_the_tracking_automation_the_setup_guide_writes():
    automation_id, config = tracking_automation(KITCHEN, {"owner": "Alex", "partner": "Sam"})

    assert_matches("tracking_automation.json", {"id": automation_id, "config": config})
