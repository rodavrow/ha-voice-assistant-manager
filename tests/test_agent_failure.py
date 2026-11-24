# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""When the main agent fails, the person hears a short line instead of a failed turn."""

import logging

from homeassistant.components import conversation
from homeassistant.helpers import intent

from custom_components.voice_assistant_manager.conversation import DEFAULT_AGENT_DOWN_LINE

from .router_helpers import MEMORY_AGENT, FakeAgents, add_router, ask, options_with
from .test_panel import Page, page  # noqa: F401 - fixture


def spoken(result: conversation.ConversationResult) -> str:
    return result.response.speech["plain"]["speech"]


async def run(hass, entry, agents: FakeAgents, text: str = "what's on today?"):
    with agents.patched():
        result = await ask(hass, entry, text)
        await hass.async_block_till_done(wait_background_tasks=True)
    return result


async def test_a_main_agent_that_raises_gets_the_default_line(hass):
    entry = await add_router(hass)
    agents = FakeAgents(main_raises=RuntimeError("Error processing stream start"))

    result = await run(hass, entry, agents)

    assert spoken(result) == DEFAULT_AGENT_DOWN_LINE
    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE


async def test_the_line_can_be_set_under_advanced(hass):
    entry = await add_router(hass, options_with(advanced={"agent_down_line": "My head hurts."}))
    agents = FakeAgents(main_raises=RuntimeError("down"))

    assert spoken(await run(hass, entry, agents)) == "My head hurts."


async def test_an_error_answer_without_words_gets_the_line(hass):
    entry = await add_router(hass)
    agents = FakeAgents(main_reply="", main_error=True)

    assert spoken(await run(hass, entry, agents)) == DEFAULT_AGENT_DOWN_LINE


async def test_an_error_answer_with_words_is_kept(hass):
    entry = await add_router(hass)
    agents = FakeAgents(main_reply="The API is busy.", main_error=True)

    result = await run(hass, entry, agents)

    assert result.response.response_type is intent.IntentResponseType.ERROR


async def test_the_failure_is_logged_with_its_cause(hass, caplog):
    entry = await add_router(hass)
    agents = FakeAgents(main_raises=RuntimeError("Error processing stream start"))

    await run(hass, entry, agents)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info]
    assert any("main agent" in r.getMessage() for r in errors)


async def test_no_memory_is_saved_from_a_failed_turn(hass):
    entry = await add_router(hass)
    agents = FakeAgents(main_raises=RuntimeError("down"))

    await run(hass, entry, agents, "remember that I like jazz")

    assert agents.calls_to(MEMORY_AGENT) == []


async def test_the_page_saves_the_line(hass, page):
    entry = await add_router(hass)
    settings = (await page.router(entry))["settings"]
    settings["advanced"] = {**settings["advanced"], "agent_down_line": "Give me a minute."}

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert entry.options["advanced"]["agent_down_line"] == "Give me a minute."
