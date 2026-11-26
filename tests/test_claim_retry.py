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
    a_tool_worked,
    DEFAULT_ACTION_FAILURE_LINE,
    DEFAULT_CANCEL_FAILURE_LINE,
    DEFAULT_FAILURE_LINE,
    PUT_RIGHT,
    RETRY_NOTES,
    SPOKE_TOO_SOON,
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


FIRST_LONG = ["Of course. I've set a reminder for you tomorrow ", "at nine to send the notes."]
SECOND_LONG = ["There, your reminder for tomorrow at nine ", "to send the notes is set."]


async def spoken_after(hass, sent, text, *answers, reminders=REMINDERS):  # noqa: F811
    entry = await add_router(hass, options_with(reminders=reminders, memory={"enabled": False}))
    pipeline = Pipeline(sent)
    agent = streaming_script(*answers)
    with patch.object(conversation, "async_converse", agent):
        result, _listener, _content = await ask_streamed(hass, entry, text, pipeline)
    return pipeline.spoken, speech(result), agent.texts


async def test_a_second_answer_that_makes_the_first_true_is_not_spoken_on_top_of_it(hass, sent):  # noqa: F811
    spoken, shown, texts_ = await spoken_after(
        hass, sent, "Remind me tomorrow at 9 to send the notes",
        (FIRST_LONG, []), (SECOND_LONG, tool_call("ok")),
    )

    # The voice was already saying the reminder is set, and now it is.
    assert spoken == "".join(FIRST_LONG)
    assert shown == "".join(SECOND_LONG)
    assert len(texts_) == 2


async def test_a_second_answer_that_says_something_else_follows_the_first(hass, sent):  # noqa: F811
    second = ["I couldn't set that one: nine tomorrow morning ", "is already taken by another reminder."]

    spoken, shown, _texts = await spoken_after(
        hass, sent, "Remind me tomorrow at 9 to send the notes",
        (FIRST_LONG, []), (second, tool_call("error")),
    )

    assert shown == "".join(second)
    assert spoken == f"{''.join(FIRST_LONG)} {PUT_RIGHT} {''.join(second)}"


async def test_a_second_try_that_still_calls_no_tool_is_followed_by_the_truth(hass, sent):  # noqa: F811
    spoken, shown, texts_ = await spoken_after(
        hass, sent, "Remind me tomorrow at 9 to send the notes", (FIRST_LONG, []),
    )

    assert shown == DEFAULT_FAILURE_LINE
    assert spoken == f"{''.join(FIRST_LONG)} {SPOKE_TOO_SOON} {DEFAULT_FAILURE_LINE}"
    assert len(texts_) == 2


async def test_an_action_the_voice_already_claimed_is_followed_by_the_truth(hass, sent):  # noqa: F811
    first = ["I've turned on the kitchen lights, ", "and they are at full brightness for you."]

    spoken, shown, _texts = await spoken_after(
        hass, sent, "Turn on the kitchen lights", (first, []), reminders={},
    )

    assert shown == DEFAULT_ACTION_FAILURE_LINE
    assert spoken == f"{''.join(first)} {SPOKE_TOO_SOON} {DEFAULT_ACTION_FAILURE_LINE}"


async def test_an_action_done_on_the_second_try_is_not_spoken_twice(hass, sent):  # noqa: F811
    first = ["I've turned on the kitchen lights, ", "and they are at full brightness for you."]
    second = ["I've turned the kitchen lights on for you, ", "at full brightness as well."]

    spoken, shown, _texts = await spoken_after(
        hass, sent, "Turn on the kitchen lights",
        (first, []), (second, lights_call()), reminders={},
    )

    assert spoken == "".join(first)
    assert shown == "".join(second)


async def test_a_short_false_claim_the_voice_had_not_started_on_is_just_replaced(hass, sent):  # noqa: F811
    spoken, shown, _texts = await spoken_after(
        hass, sent, "Turn on the kitchen lights",
        (["I've turned ", "them on."], []), reminders={},
    )

    assert shown == DEFAULT_ACTION_FAILURE_LINE
    assert spoken == "I've turned them on."


# Every tool call failed


def failed_colour_call(number: int = 1) -> list:
    """Chat log entries of a light call that Home Assistant refused, as it leaves them."""
    return [
        AssistantContent(
            agent_id=MAIN_AGENT, content=None,
            tool_calls=[
                llm.ToolInput(
                    tool_name="HassLightSet", tool_args={"name": "lamp", "color": "mauve"},
                    id=f"f{number}",
                )
            ],
        ),
        ToolResultContent(
            agent_id=MAIN_AGENT, tool_call_id=f"f{number}", tool_name="HassLightSet",
            result=llm.ToolResult(
                data={"error": "InvalidSlotInfo", "error_text": "Unknown color"}, error=True
            ),
        ),
    ]


def test_a_turn_whose_tool_calls_all_failed_had_no_tool_that_worked():
    assert a_tool_worked(lights_call())
    assert not a_tool_worked(failed_colour_call())
    assert not a_tool_worked([AssistantContent(agent_id=MAIN_AGENT, content="Hello")])
    assert a_tool_worked([*failed_colour_call(1), *lights_call()])


def test_a_call_that_never_got_a_result_did_not_work():
    assert not a_tool_worked(lights_call()[:1])


@pytest.mark.parametrize(
    "result",
    [
        {"error": "InvalidSlotInfo"},
        llm.ToolResult(data={"response_type": "error", "speech": {}}),
        llm.ToolResult(data={"result": {"status": "error", "message": "No such light."}}),
    ],
)
def test_other_ways_a_tool_says_it_failed(result):
    entries = [
        ToolResultContent(agent_id=MAIN_AGENT, tool_call_id="x", tool_name="t", result=result)
    ]

    assert not a_tool_worked(entries)


async def test_regression_an_action_claimed_after_every_tool_call_failed_is_asked_again(hass):
    # Seen in the house: three HassLightSet calls were refused ("Unknown color") and the
    # assistant said it had changed the lights. A failed call counted as "a tool was
    # used", so the reply was not checked.
    agent = Scripted(
        ("I've changed the lights to mauve.",
         [*failed_colour_call(1), *failed_colour_call(2), *failed_colour_call(3)]),
        ("I've set the lights to purple instead.", lights_call()),
    )

    said, log = await asked(hass, agent, "Make the lights mauve")

    assert said == "I've set the lights to purple instead."
    assert agent.texts[1] == RETRY_NOTES["failed"]
    assert "I've changed the lights to mauve." not in texts(log)


async def test_an_action_claimed_twice_after_failed_tool_calls_is_a_refusal(hass):
    agent = Scripted(
        ("I've changed the lights to mauve.", failed_colour_call(1)),
        ("Done, the lights are now set to mauve.", failed_colour_call(2)),
    )

    said, log = await asked(hass, agent, "Make the lights mauve")

    assert said == DEFAULT_ACTION_FAILURE_LINE
    assert len(agent.texts) == 2
    assert texts(log)[-1] == DEFAULT_ACTION_FAILURE_LINE


async def test_saying_why_it_failed_on_the_second_try_is_spoken(hass):
    agent = Scripted(
        ("I've changed the lights to mauve.", failed_colour_call(1)),
        ("Sorry, the lights do not know the colour mauve.", []),
    )

    said, _log = await asked(hass, agent, "Make the lights mauve")

    assert said == "Sorry, the lights do not know the colour mauve."


async def test_an_honest_reply_after_a_failed_tool_call_is_left_alone(hass):
    agent = Scripted(("Sorry, I could not change the colour.", failed_colour_call()))

    said, _log = await asked(hass, agent, "Make the lights mauve")

    assert said == "Sorry, I could not change the colour."
    assert len(agent.texts) == 1


async def test_one_call_that_worked_among_failed_ones_backs_the_reply(hass):
    agent = Scripted(
        ("I've changed the lights.", [*failed_colour_call(1), *lights_call()]),
    )

    said, _log = await asked(hass, agent, "Make the lights mauve")

    assert said == "I've changed the lights."
    assert len(agent.texts) == 1
