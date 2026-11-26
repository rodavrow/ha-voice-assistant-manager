# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A turn that asks about reminders: who is asking, the instructions and the reply check."""

import pytest
from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import (
    AssistantContent,
    ToolResultContent,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm

from custom_components.voice_assistant_manager.reminder_actions import asker, note_asker
from custom_components.voice_assistant_manager.reminders import (
    DEFAULT_FAILURE_LINE,
    DEFAULT_LIST_FAILURE_LINE,
    is_list_turn,
    looks_like_listing,
    reminder_guidance,
)

from .router_helpers import (
    MAIN_AGENT,
    FakeAgents,
    add_router,
    ask_in_pipeline,
    options_with,
    router_entity_id,
)
from .test_speaker_follow_up import add_satellite, match

LIST = "script__list_reminders"
FOUND = "You have one: charge the car, set for Wednesday at 8 am, every week."


def list_call(status: str | None) -> list:
    """Chat log entries of one list_reminders call. None: no result came back."""
    entries: list = [
        AssistantContent(
            agent_id=MAIN_AGENT,
            content=None,
            tool_calls=[llm.ToolInput(tool_name=LIST, tool_args={"keywords": "car"}, id="l1")],
        )
    ]
    if status is not None:
        entries.append(
            ToolResultContent(
                agent_id=MAIN_AGENT, tool_call_id="l1", tool_name=LIST,
                result=llm.ToolResult(data={"result": {"status": status, "reminders": []}}),
            )
        )
    return entries


async def reply_after(hass, text, main_reply, main_log):
    entry = await add_router(
        hass, options_with(reminders={"enabled": True}, memory={"enabled": False})
    )
    agents = FakeAgents(main_reply=main_reply, main_log=main_log)
    with agents.patched():
        result, _ = await ask_in_pipeline(hass, entry, text)
    return result.response.speech["plain"]["speech"], agents


# Which requests ask about reminders


@pytest.mark.parametrize(
    "text",
    [
        "What reminders are there on Wednesday?",
        "Do we have any reminders for charging the car",
        "do I have a reminder about the dentist?",
        "List my reminders",
        "Which reminders are coming up this week",
        "When is my reminder for the bins?",
        "Is there a reminder set for tomorrow",
        "Have we got reminders for the weekend?",
        "Tell me about the car reminder",
        "What have you got me down to be reminded of on Friday?",
    ],
)
def test_a_question_about_reminders_is_a_look_up(text):
    assert is_list_turn(text)


@pytest.mark.parametrize(
    "text",
    [
        "Remind me to charge the car on Wednesday at 8",
        "Set a reminder for tomorrow at 7",
        "Cancel the bins reminder",
        "What is the weather on Wednesday?",
        "Remember that the reminder calendar is the blue one",
        "How do reminders work?",
        "What can you do with reminders?",
        "Can you set reminders that repeat?",
        "",
    ],
)
def test_other_requests_are_not_a_look_up(text):
    assert not is_list_turn(text)


@pytest.mark.parametrize(
    ("reply", "states"),
    [
        (FOUND, True),
        ("You have no reminders on Wednesday.", True),
        ("Nothing about the car, I'm afraid to say none exist.", True),
        ("Which day do you mean?", False),
        ("Sorry, I could not look that up.", False),
        ("I can't see your reminders right now.", False),
        ("I am unable to check reminders.", False),
        ("", False),
    ],
)
def test_a_reply_that_says_what_reminders_there_are(reply, states):
    assert looks_like_listing(reply) is states


# The reply check


async def test_an_answer_from_a_look_up_is_left_alone(hass: HomeAssistant):
    said, _ = await reply_after(
        hass, "What reminders are there on Wednesday?", FOUND, list_call("ok")
    )

    assert said == FOUND


async def test_regression_a_looked_up_reminder_is_not_taken_for_a_false_confirmation(
    hass: HomeAssistant,
):
    # "on Wednesday" and "reminder ... set" made the check for false "reminder set"
    # replies fire on an honest answer about a reminder that exists.
    said, _ = await reply_after(
        hass, "And on Wednesday at 8?", "Your reminder to charge the car is set for then.",
        list_call("ok"),
    )

    assert said == "Your reminder to charge the car is set for then."
    assert said != DEFAULT_FAILURE_LINE


async def test_an_answer_about_reminders_without_looking_is_asked_again_then_refused(
    hass: HomeAssistant,
):
    said, agents = await reply_after(hass, "Do we have a reminder for charging the car?", FOUND, [])

    assert said == DEFAULT_LIST_FAILURE_LINE
    notes = [call["text"] for call in agents.calls_to(MAIN_AGENT)]
    assert len(notes) == 2
    assert "list_reminders" in notes[1]


async def test_saying_there_are_none_without_looking_is_refused_too(hass: HomeAssistant):
    said, _ = await reply_after(
        hass, "What reminders are there on Wednesday?", "You have no reminders on Wednesday.", []
    )

    assert said == DEFAULT_LIST_FAILURE_LINE


async def test_an_answer_after_a_failed_look_up_is_refused(hass: HomeAssistant):
    said, _ = await reply_after(
        hass, "What reminders are there on Wednesday?", FOUND, list_call("error")
    )

    assert said == DEFAULT_LIST_FAILURE_LINE


async def test_an_answer_from_another_tool_is_left_alone(hass: HomeAssistant):
    other = [
        AssistantContent(
            agent_id=MAIN_AGENT, content=None,
            tool_calls=[llm.ToolInput(tool_name="calendar_get_events", tool_args={}, id="g1")],
        ),
        ToolResultContent(
            agent_id=MAIN_AGENT, tool_call_id="g1", tool_name="calendar_get_events",
            result={"events": []},
        ),
    ]

    said, _ = await reply_after(hass, "What reminders are there on Wednesday?", FOUND, other)

    assert said == FOUND


async def test_saying_the_look_up_failed_is_left_alone(hass: HomeAssistant):
    reply = "Sorry, I could not look that up: that day is already over."

    said, _ = await reply_after(
        hass, "What reminders were there yesterday?", reply, list_call("error")
    )

    assert said == reply


async def test_a_question_back_is_left_alone(hass: HomeAssistant):
    said, agents = await reply_after(
        hass, "List my reminders", "For this week, or all of them?", []
    )

    assert said == "For this week, or all of them?"
    assert len(agents.calls_to(MAIN_AGENT)) == 1


# The instructions


def test_the_main_agent_is_told_to_look_reminders_up_and_whose_it_sees():
    guidance = reminder_guidance("Alex", "Sam", "jeeves")

    assert "script.list_reminders" in guidance
    assert "never from memory" in guidance
    assert "Sam's reminders" in guidance


# Who is asking


async def turn(hass, entry, context, device_id=None) -> None:
    entity_id = router_entity_id(hass, entry)
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(entity_id)
    await entity.async_process(
        conversation.ConversationInput(
            text="What reminders do I have?", context=context, conversation_id=None,
            device_id=device_id, satellite_id=None, language="en", agent_id=entity_id,
        )
    )


async def test_the_assistant_notes_the_person_speaking_for_the_turn(hass: HomeAssistant):
    entry = await add_router(hass)
    kitchen = add_satellite(hass)
    match(hass, kitchen, "partner")
    context = Context()

    with FakeAgents().patched():
        await turn(hass, entry, context, kitchen)

    assert asker(hass, context) == "sam"


async def test_an_unknown_voice_is_noted_as_nobody(hass: HomeAssistant):
    entry = await add_router(hass)
    kitchen = add_satellite(hass)
    match(hass, kitchen, "stranger")
    context = Context()
    note_asker(hass, context, "alex")

    with FakeAgents().patched():
        await turn(hass, entry, context, kitchen)

    assert asker(hass, context) is None
