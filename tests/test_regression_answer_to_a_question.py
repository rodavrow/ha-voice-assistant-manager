# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression: the answer to the assistant's question is not a built-in command.

Asked to set a reminder, the main agent asked "when, and what for?". The answer, "In five
minutes to drink some water", matches Home Assistant's built-in "in <time> <command>", so
with built-in commands first it set a timer and the main agent never got its answer.
"""

from unittest.mock import patch

from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant

from .router_helpers import (
    MAIN_AGENT,
    FakeAgents,
    add_router,
    options_with,
    router_entity_id,
)

ANSWER = "In five minutes to drink some water."


async def say(hass: HomeAssistant, entry, text: str, conversation_id: str = "chat-1"):
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(router_entity_id(hass, entry))
    return await entity.async_process(
        conversation.ConversationInput(
            text=text,
            context=Context(),
            conversation_id=conversation_id,
            device_id=None,
            satellite_id=None,
            language="en",
            agent_id=entity.entity_id,
        )
    )


def asking(agents: FakeAgents, asks: bool) -> dict[str, bool]:
    """The main agent's replies end with a question (or not), as Home Assistant marks them.

    The returned dict's "asks" can be changed between turns.
    """
    answer = agents.converse
    now = {"asks": asks}

    async def converse(*args, **kwargs):
        result = await answer(*args, **kwargs)
        if kwargs.get("agent_id") == MAIN_AGENT:
            result.continue_conversation = now["asks"]
        return result

    agents.converse = converse  # type: ignore[method-assign]
    return now


async def test_regression_the_answer_to_a_question_goes_to_the_main_agent(hass: HomeAssistant):
    entry = await add_router(hass, options_with(local={"enabled": True}))
    agents = FakeAgents()
    asking(agents, True)

    with (
        agents.patched(),
        patch.object(conversation, "async_handle_intents", return_value=None) as built_in,
    ):
        await say(hass, entry, "Set a reminder")
        built_in.reset_mock()
        await say(hass, entry, ANSWER)

    built_in.assert_not_called()
    assert len(agents.calls_to(MAIN_AGENT)) == 2


async def test_after_a_reply_that_asked_nothing_built_in_commands_come_first(
    hass: HomeAssistant,
):
    entry = await add_router(hass, options_with(local={"enabled": True}))
    agents = FakeAgents()
    asking(agents, False)

    with (
        agents.patched(),
        patch.object(conversation, "async_handle_intents", return_value=None) as built_in,
    ):
        await say(hass, entry, "Tell me a joke")
        built_in.reset_mock()
        await say(hass, entry, ANSWER)

    built_in.assert_called_once()


async def test_a_question_in_one_conversation_does_not_reach_another(hass: HomeAssistant):
    entry = await add_router(hass, options_with(local={"enabled": True}))
    agents = FakeAgents()
    asking(agents, True)

    with (
        agents.patched(),
        patch.object(conversation, "async_handle_intents", return_value=None) as built_in,
    ):
        await say(hass, entry, "Set a reminder", "chat-1")
        built_in.reset_mock()
        await say(hass, entry, ANSWER, "chat-2")

    built_in.assert_called_once()


async def test_a_question_is_answered_once(hass: HomeAssistant):
    entry = await add_router(hass, options_with(local={"enabled": True}))
    agents = FakeAgents()
    main = asking(agents, True)

    with (
        agents.patched(),
        patch.object(conversation, "async_handle_intents", return_value=None) as built_in,
    ):
        await say(hass, entry, "Set a reminder")
        main["asks"] = False
        await say(hass, entry, ANSWER)
        built_in.reset_mock()
        await say(hass, entry, "Turn on the kitchen light")

    built_in.assert_called_once()
