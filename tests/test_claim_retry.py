# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A reply that says something was done although no tool was called: one more try."""

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import (
    DATA_CHAT_LOGS,
    AssistantContent,
    ToolResultContent,
    async_get_chat_log,
)
from homeassistant.helpers import llm
from homeassistant.helpers.chat_session import async_get_chat_session

from custom_components.voice_assistant_manager.reminders import (
    DEFAULT_ACTION_FAILURE_LINE,
    DEFAULT_CANCEL_FAILURE_LINE,
    DEFAULT_FAILURE_LINE,
    RETRY_NOTES,
    looks_like_action_claim,
    used_a_tool,
)

from .router_helpers import MAIN_AGENT, _result, add_router, ask_in_pipeline, options_with
from .test_emotion_router import Pipeline, ask_streamed, sent, speech  # noqa: F401 - fixture
from .test_reminder_cancel import cancel_call
from .test_reminders import tool_call

REMINDERS = {"enabled": True}
FALSE_SET = "Of course. I've set a reminder for tomorrow at nine."
TRULY_SET = "There, your reminder for tomorrow at nine is set."


def lights_call() -> list:
    """Chat log entries of a call that turned a light on."""
    return [
        AssistantContent(
            agent_id=MAIN_AGENT, content=None,
            tool_calls=[llm.ToolInput(tool_name="HassTurnOn", tool_args={"name": "kitchen"}, id="l1")],
        ),
        ToolResultContent(
            agent_id=MAIN_AGENT, tool_call_id="l1", tool_name="HassTurnOn",
            result=llm.ToolResult(data={"success": True}),
        ),
    ]


class Scripted:
    """A main agent that gives the next (reply, tool entries) of a script on each call."""

    def __init__(self, *answers: tuple[str, list] | Exception) -> None:
        self.answers = list(answers)
        self.texts: list[str] = []

    async def converse(self, hass, text, conversation_id, context, **kwargs):
        if kwargs.get("agent_id") != MAIN_AGENT:
            return _result("{}")
        self.texts.append(text)
        answer = self.answers[min(len(self.texts), len(self.answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        reply, entries = answer
        log = hass.data[DATA_CHAT_LOGS][conversation_id]
        if log.content[-1].role != "user" or log.content[-1].content != text:
            log.async_add_user_content(conversation.UserContent(content=text))
        log.content.extend(entries)
        log.content.append(AssistantContent(agent_id=MAIN_AGENT, content=reply))
        result = _result(reply)
        result.conversation_id = conversation_id
        return result


async def asked(hass, agent: Scripted, text: str, **reminders: Any):
    options = options_with(
        reminders={**REMINDERS, **reminders} if reminders is not None else {},
        memory={"enabled": False},
    )
    entry = await add_router(hass, options)
    with patch.object(conversation, "async_converse", agent.converse):
        result, log = await ask_in_pipeline(hass, entry, text)
    return speech(result), log


def texts(log: list) -> list[str]:
    """The words of the entries of a chat log that have any."""
    return [entry.content for entry in log if getattr(entry, "content", None)]


# What counts as saying something was done


@pytest.mark.parametrize(
    "reply",
    [
        "I've turned on the kitchen lights.",
        "Okay. I have set the thermostat to 21 degrees.",
        "Done! The door is locked.",
        "I just added milk to the shopping list.",
        "The hall light is now on.",
        "I locked the front door.",
        "All done.",
    ],
)
def test_a_reply_that_says_something_was_done_is_a_claim(reply):
    assert looks_like_action_claim(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "The kitchen lights are on.",
        "Shall I turn on the lights?",
        "I can turn on the lights if you like.",
        "I could not turn on the lights.",
        "I haven't turned anything on.",
        "If you want it locked, I've got that covered.",
        "It is 21 degrees in the living room.",
        "I've been thinking about that too.",
        "",
    ],
)
def test_an_answer_question_offer_or_failure_is_not_a_claim(reply):
    assert not looks_like_action_claim(reply)


def test_a_turn_with_a_tool_call_or_result_used_a_tool():
    assert used_a_tool(lights_call())
    assert not used_a_tool([AssistantContent(agent_id=MAIN_AGENT, content="Hello")])


# Reminders


async def test_a_reminder_claimed_without_the_tool_is_asked_for_once_more(hass):
    agent = Scripted((FALSE_SET, []), (TRULY_SET, tool_call("ok")))

    said, log = await asked(hass, agent, "Remind me tomorrow at 9 to send the notes")

    assert said == TRULY_SET
    assert agent.texts[1] == RETRY_NOTES["reminder"]
    assert len(agent.texts) == 2


async def test_the_history_reads_as_if_the_second_answer_had_been_the_first(hass):
    agent = Scripted((FALSE_SET, []), (TRULY_SET, tool_call("ok")))

    _said, log = await asked(hass, agent, "Remind me tomorrow at 9 to send the notes")

    contents = texts(log)
    assert FALSE_SET not in contents
    assert RETRY_NOTES["reminder"] not in contents
    assert contents[-1] == TRULY_SET
    assert used_a_tool(log)


async def test_a_reminder_claimed_twice_without_the_tool_is_an_honest_failure(hass):
    agent = Scripted((FALSE_SET, []))

    said, _log = await asked(hass, agent, "Remind me tomorrow at 9 to send the notes")

    assert said == DEFAULT_FAILURE_LINE
    assert len(agent.texts) == 2  # asked once more, and only once


async def test_a_cancellation_claimed_without_the_tool_is_asked_for_once_more(hass):
    agent = Scripted(
        ("Done, I've cancelled your pills reminder.", []),
        ("Your pills reminder is cancelled.", cancel_call("ok")),
    )

    said, _log = await asked(hass, agent, "Cancel my pills reminder")

    assert said == "Your pills reminder is cancelled."
    assert agent.texts[1] == RETRY_NOTES["cancel"]


async def test_a_cancellation_claimed_twice_without_the_tool_is_an_honest_failure(hass):
    agent = Scripted(("Done, I've cancelled your pills reminder.", []))

    said, _log = await asked(hass, agent, "Cancel my pills reminder")

    assert said == DEFAULT_CANCEL_FAILURE_LINE
    assert len(agent.texts) == 2


async def test_a_reminder_set_with_the_tool_is_not_asked_for_again(hass):
    agent = Scripted((TRULY_SET, tool_call("ok")))

    said, _log = await asked(hass, agent, "Remind me tomorrow at 9 to send the notes")

    assert said == TRULY_SET
    assert len(agent.texts) == 1


async def test_a_failed_second_try_leaves_the_honest_failure(hass):
    agent = Scripted((FALSE_SET, []), RuntimeError("provider down"))

    said, log = await asked(hass, agent, "Remind me tomorrow at 9 to send the notes")

    assert said == DEFAULT_FAILURE_LINE
    assert RETRY_NOTES["reminder"] not in texts(log)


# Any other tool


async def test_an_action_claimed_without_a_tool_is_asked_for_once_more(hass):
    agent = Scripted(
        ("I've turned on the kitchen lights.", []),
        ("The kitchen lights are on for you.", lights_call()),
    )

    said, log = await asked(hass, agent, "Turn on the kitchen lights")

    assert said == "The kitchen lights are on for you."
    assert agent.texts[1] == RETRY_NOTES["action"]
    assert "I've turned on the kitchen lights." not in texts(log)


async def test_an_action_claimed_twice_without_a_tool_is_a_refusal(hass):
    agent = Scripted(
        ("I've turned on the kitchen lights.", []),
        ("I've turned them on, as you asked.", []),
    )

    said, log = await asked(hass, agent, "Turn on the kitchen lights")

    assert said == DEFAULT_ACTION_FAILURE_LINE
    assert len(agent.texts) == 2  # asked once more, and only once
    assert texts(log)[-1] == DEFAULT_ACTION_FAILURE_LINE


async def test_a_second_answer_that_claims_nothing_is_spoken(hass):
    agent = Scripted(
        ("I've turned on the kitchen lights.", []),
        ("Which lights do you mean, the kitchen or the hall?", []),
    )

    said, _log = await asked(hass, agent, "Turn on the lights")

    assert said == "Which lights do you mean, the kitchen or the hall?"


async def test_an_action_with_a_tool_call_is_not_asked_for_again(hass):
    agent = Scripted(("I've turned on the kitchen lights.", lights_call()))

    said, _log = await asked(hass, agent, "Turn on the kitchen lights")

    assert said == "I've turned on the kitchen lights."
    assert len(agent.texts) == 1


@pytest.mark.parametrize(
    ("text", "reply"),
    [
        ("Which lights are on?", "The kitchen lights are on."),
        ("Turn on the lights", "Which lights do you mean, the kitchen or the hall?"),
        ("Remember that I like the lights warm", "I've set that aside for you."),
    ],
)
async def test_an_answer_a_question_and_a_memory_are_not_asked_for_again(hass, text, reply):
    agent = Scripted((reply, []))

    said, _log = await asked(hass, agent, text)

    assert said == reply
    assert len(agent.texts) == 1


async def test_a_failed_second_try_is_a_refusal_and_leaves_no_note(hass):
    agent = Scripted(("I've turned on the kitchen lights.", []), RuntimeError("provider down"))

    said, log = await asked(hass, agent, "Turn on the kitchen lights")

    assert said == DEFAULT_ACTION_FAILURE_LINE
    assert RETRY_NOTES["action"] not in texts(log)
    assert "I've turned on the kitchen lights." not in texts(log)


async def test_it_works_with_reminders_off(hass):
    agent = Scripted(
        ("I've turned on the kitchen lights.", []),
        ("The kitchen lights are on for you.", lights_call()),
    )
    entry = await add_router(hass, options_with(memory={"enabled": False}))

    with patch.object(conversation, "async_converse", agent.converse):
        result, _log = await ask_in_pipeline(hass, entry, "Turn on the kitchen lights")

    assert speech(result) == "The kitchen lights are on for you."


# The voice


def streaming_script(*answers: tuple[list[str], list]):
    """A main agent that streams the next (pieces, tool entries) on each call."""
    texts: list[str] = []

    async def converse(hass, text, conversation_id, context, **kwargs):
        if kwargs.get("agent_id") != MAIN_AGENT:
            return _result("{}")
        texts.append(text)
        pieces, entries = answers[min(len(texts), len(answers)) - 1]
        user_input = conversation.ConversationInput(
            text=text, context=context, conversation_id=conversation_id, device_id=None,
            satellite_id=None, language="en", agent_id=MAIN_AGENT,
        )

        async def deltas():
            yield {"role": "assistant"}
            for piece in pieces:
                yield {"content": piece}

        with (
            async_get_chat_session(hass, conversation_id, context=context) as session,
            async_get_chat_log(hass, session, user_input) as log,
        ):
            log.content.extend(entries)
            async for _content in log.async_add_delta_content_stream(MAIN_AGENT, deltas()):
                pass
        result = _result("".join(pieces))
        result.conversation_id = conversation_id
        return result

    converse.texts = texts
    return converse


async def test_the_voice_only_hears_the_second_answer(hass, sent):  # noqa: F811
    entry = await add_router(
        hass, options_with(reminders=REMINDERS, memory={"enabled": False})
    )
    pipeline = Pipeline(sent)
    agent = streaming_script(
        (["Of course. I've set a reminder for you tomorrow ", "at nine to send the notes."], []),
        (["There, your reminder for tomorrow at nine ", "to send the notes is set."], tool_call("ok")),
    )

    with patch.object(conversation, "async_converse", agent):
        result, _listener, _content = await ask_streamed(
            hass, entry, "Remind me tomorrow at 9 to send the notes", pipeline
        )

    assert pipeline.spoken == "There, your reminder for tomorrow at nine to send the notes is set."
    assert speech(result) == pipeline.spoken
    assert len(agent.texts) == 2


async def test_the_voice_hears_the_refusal_when_the_second_try_fails(hass, sent):  # noqa: F811
    entry = await add_router(hass, options_with(memory={"enabled": False}))
    pipeline = Pipeline(sent)
    first = ["I've turned on the kitchen lights, ", "and they are at full brightness for you."]
    calls: list[str] = []
    streamed = streaming_script((first, []))

    async def converse(hass_, text, conversation_id, context, **kwargs):
        if kwargs.get("agent_id") == MAIN_AGENT:
            calls.append(text)
            if len(calls) == 2:
                raise RuntimeError("provider down")
        return await streamed(hass_, text, conversation_id, context, **kwargs)

    with patch.object(conversation, "async_converse", converse):
        result, _listener, _content = await ask_streamed(
            hass, entry, "Turn on the kitchen lights", pipeline
        )

    assert pipeline.spoken == DEFAULT_ACTION_FAILURE_LINE
    assert speech(result) == DEFAULT_ACTION_FAILURE_LINE
