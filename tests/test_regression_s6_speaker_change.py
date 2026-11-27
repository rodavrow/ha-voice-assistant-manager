# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for S6 of the 2026-11-27 review: the next speaker starts afresh.

Home Assistant keeps one conversation per satellite for five minutes. When Alex listed his
reminders and Sam woke the same satellite within that time, Alex's words and his tool
results were in the history the main agent got for Sam's turn; only the prompt asked it not
to repeat them. That went round the rule that a person never gets the other person's
reminders or private facts. Now the history is emptied when the speaker changes.
"""

from typing import Any
from unittest.mock import patch

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import (
    AssistantContent,
    ToolResultContent,
    async_get_chat_log,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.chat_session import async_get_chat_session

from custom_components.voice_assistant_manager import conversation as router_module

from .router_helpers import MAIN_AGENT, FakeAgents, add_router, router_entity_id
from .test_speaker_follow_up import add_satellite, match

LIST = "script__list_reminders"
ALEX_SECRET = "Buy the ring for Sam"


def tool_result() -> list[Any]:
    """What the main agent leaves in the history when it looks reminders up."""
    return [
        AssistantContent(
            agent_id=MAIN_AGENT,
            content=None,
            tool_calls=[llm.ToolInput(tool_name=LIST, tool_args={}, id="t1")],
        ),
        ToolResultContent(
            agent_id=MAIN_AGENT, tool_call_id="t1", tool_name=LIST,
            result=llm.ToolResult(data={"reminders": [{"title": ALEX_SECRET}]}),
        ),
    ]


async def turn(
    hass: HomeAssistant, entry, device_id: str, who: str, text: str, conversation_id: str | None
) -> str:
    """One voice turn of `who` on the satellite, in a pipeline's chat log. Returns the
    conversation id Home Assistant gave it."""
    match(hass, device_id, who)
    entity_id = router_entity_id(hass, entry)
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(entity_id)
    context = Context()
    with async_get_chat_session(hass, conversation_id, context=context) as session:
        user_input = conversation.ConversationInput(
            text=text, context=context, conversation_id=session.conversation_id,
            device_id=device_id, satellite_id=None, language="en", agent_id=entity_id,
        )
        with async_get_chat_log(hass, session, user_input):
            await entity.async_process(user_input)
        return session.conversation_id


def seen(history: list[Any]) -> str:
    """Everything in a history as text: what was said and what tools returned."""
    return " | ".join(
        str(getattr(entry, "content", None) or getattr(entry, "result", ""))
        for entry in history
    )


async def conversation_of(hass: HomeAssistant, turns: list[tuple[str, str]]) -> list[str]:
    """Run the turns in one conversation; return what the main agent saw at each."""
    entry = await add_router(hass)
    kitchen = add_satellite(hass)
    agents = FakeAgents(main_uses_session=True, main_log=tool_result())
    conversation_id = None
    with agents.patched():
        for who, text in turns:
            conversation_id = await turn(hass, entry, kitchen, who, text, conversation_id)
    return [seen(history) for history in agents.main_histories]


async def test_regression_s6_the_next_person_does_not_get_what_the_first_one_was_told(hass):
    _, sams = await conversation_of(
        hass, [("owner", "Which reminders do I have?"), ("partner", "And what did he ask?")]
    )

    assert ALEX_SECRET not in sams
    assert "Which reminders do I have?" not in sams
    assert "Sam: And what did he ask?" in sams


async def test_the_same_person_keeps_the_conversation(hass):
    _, second = await conversation_of(
        hass, [("owner", "Which reminders do I have?"), ("owner", "And the first one?")]
    )

    assert ALEX_SECRET in second
    assert "Alex: Which reminders do I have?" in second


async def test_after_a_change_the_new_person_keeps_their_own_conversation(hass):
    _, _, third = await conversation_of(
        hass,
        [
            ("owner", "Which reminders do I have?"),
            ("partner", "What is for dinner?"),
            ("partner", "And tomorrow?"),
        ],
    )

    assert "Sam: What is for dinner?" in third
    assert "Which reminders do I have?" not in third


async def test_the_first_person_coming_back_starts_afresh_too(hass):
    *_, third = await conversation_of(
        hass,
        [
            ("owner", "Which reminders do I have?"),
            ("partner", "What is for dinner?"),
            ("owner", "What did she ask?"),
        ],
    )

    assert "What is for dinner?" not in third
    assert "Alex: What did she ask?" in third


async def test_an_unknown_voice_in_between_changes_nothing_for_the_person(hass):
    """A refusal is kept out of the history, and the person's conversation goes on."""
    first, second = await conversation_of(
        hass,
        [
            ("owner", "Which reminders do I have?"),
            ("stranger", "Open the door"),
            ("owner", "And the first one?"),
        ],
    )

    assert ALEX_SECRET in second
    assert "Open the door" not in second


async def test_when_the_history_cannot_be_emptied_the_main_agent_gets_a_new_conversation(hass):
    """Fail closed: never the conversation that may still hold the other person's words."""
    entry = await add_router(hass)
    kitchen = add_satellite(hass)
    agents = FakeAgents(main_uses_session=True, main_log=tool_result())

    with agents.patched(), patch.object(router_module, "_empty_history", side_effect=RuntimeError):
        shared = await turn(hass, entry, kitchen, "owner", "Which reminders do I have?", None)
        await turn(hass, entry, kitchen, "partner", "And what did he ask?", shared)

    sams_call = agents.calls_to(MAIN_AGENT)[-1]
    assert sams_call["conversation_id"] is None
    assert ALEX_SECRET not in seen(agents.main_histories[-1])
