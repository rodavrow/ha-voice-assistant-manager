# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for reminders: prompt guidance, the split from memory, and false confirmations."""

import pytest
from homeassistant.components.conversation.chat_log import (
    AssistantContent,
    ToolResultContent,
    UserContent,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from custom_components.voice_assistant_manager.memory import (
    EXTRACTION_INSTRUCTIONS,
    REMEMBER_RULE,
    REMEMBER_RULE_REMINDERS,
    async_get_memory,
    entry_key,
    has_memory_keyword,
    should_extract,
)
from custom_components.voice_assistant_manager.reminders import (
    DEFAULT_FAILURE_LINE,
    has_alert_time,
    is_reminder_request,
    is_reminder_turn,
    is_unclear_remember_to,
    looks_like_confirmation,
    reminder_guidance,
    reminder_tool_outcome,
)

from .router_helpers import (
    MAIN_AGENT,
    MEMORY_AGENT,
    FakeAgents,
    add_router,
    ask,
    ask_in_pipeline,
    options_with,
)

REMINDERS_ON = {"enabled": True}
TOOL = "script__create_reminder"


def tool_call(status: str | None, call_id: str = "c1") -> list:
    """Chat log entries of one create_reminder call. status None means no result came back."""
    args = {"title": "Call the plumber", "when": "2026-11-21 17:00:00", "for_whom": "alex"}
    entries: list = [
        AssistantContent(
            agent_id=MAIN_AGENT,
            content=None,
            tool_calls=[llm.ToolInput(tool_name=TOOL, tool_args=args, id=call_id)],
        )
    ]
    if status is not None:
        message = {"status": status}
        if status == "error":
            message["message"] = "That time is in the past."
        entries.append(
            ToolResultContent(
                agent_id=MAIN_AGENT,
                tool_call_id=call_id,
                tool_name=TOOL,
                # Home Assistant wraps what the script returned exactly like this.
                result=llm.ToolResult(data={"result": message}),
            )
        )
    return entries


# Recognising requests (pure).


@pytest.mark.parametrize(
    "text",
    [
        "Remind me at 5 pm to call the plumber",
        "remind us in 20 minutes to check the oven",
        "Please remind Sam tomorrow to call the plumber",
        "Set a reminder for 7 tomorrow morning",
        "Can you set another reminder for the bins",
    ],
)
def test_reminder_requests_are_recognised(text):
    assert is_reminder_request(text)


@pytest.mark.parametrize(
    "text", ["Remember that I like jazz", "What reminds you of summer", "Turn on the lights", ""]
)
def test_other_requests_are_not_reminder_requests(text):
    assert not is_reminder_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "at 5 pm",
        "at 17:30",
        "at noon",
        "at half past four",
        "in 20 minutes",
        "in an hour",
        "in two days",
        "tomorrow",
        "tonight",
        "this evening",
        "on Friday",
        "next week",
        "every Monday",
        "7am",
        "seven o'clock",
    ],
)
def test_alert_times_are_recognised(text):
    assert has_alert_time(f"Remind me {text} to call the plumber")


@pytest.mark.parametrize(
    "text", ["Remind Sam about the bins", "Remember that I park in the garden", "Remind me to breathe"]
)
def test_text_without_an_alert_time(text):
    assert not has_alert_time(text)


@pytest.mark.parametrize(
    ("text", "unclear"),
    [
        ("Remember to buy milk", True),
        ("Please remember to call the plumber", True),
        ("Remember to buy milk at 5 pm", False),
        ("Remember to tell Sam the plumber comes on Friday", False),
        ("Remember to let Sam know I am late", False),
        ("Remember that I like jazz", False),
        ("Remind me to buy milk", False),
    ],
)
def test_remember_to_do_something_without_a_time_is_unclear(text, unclear):
    assert is_unclear_remember_to(text) is unclear


@pytest.mark.parametrize(
    "reply",
    [
        "Certainly, your reminder is set for five.",
        "I'll remind you at 5 pm.",
        "Done. I will remind you tomorrow.",
        "Very good, I have set that reminder.",
        "Reminder created for Friday.",
        "I shall remind you at seven.",
    ],
)
def test_confirmations_are_recognised(reply):
    assert looks_like_confirmation(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "What time should I remind you?",
        "I couldn't set that reminder: that time is in the past.",
        "I was unable to create the reminder.",
        "Sorry, that did not work.",
        "",
    ],
)
def test_questions_and_failures_are_not_confirmations(reply):
    assert not looks_like_confirmation(reply)


# Reading the tool calls of a turn (pure).


def test_outcome_is_none_without_a_reminder_tool_call():
    other = AssistantContent(
        agent_id=MAIN_AGENT,
        content=None,
        tool_calls=[llm.ToolInput(tool_name="HassTurnOn", tool_args={}, id="x")],
    )

    assert reminder_tool_outcome([UserContent(content="hi"), other]) == "none"


def test_outcome_is_ok_when_the_tool_returned_status_ok():
    assert reminder_tool_outcome(tool_call("ok")) == "ok"


def test_outcome_is_error_when_the_tool_returned_an_error():
    assert reminder_tool_outcome(tool_call("error")) == "error"


def test_outcome_is_error_when_the_tool_was_called_but_nothing_came_back():
    assert reminder_tool_outcome(tool_call(None)) == "error"


def test_outcome_is_ok_when_a_retry_succeeded():
    assert reminder_tool_outcome(tool_call("error", "c1") + tool_call("ok", "c2")) == "ok"


def test_outcome_reads_a_bare_script_name_and_a_flat_result():
    entries = [
        AssistantContent(
            agent_id=MAIN_AGENT,
            content=None,
            tool_calls=[llm.ToolInput(tool_name="create_reminder", tool_args={}, id="c1")],
        ),
        ToolResultContent(
            agent_id=MAIN_AGENT, tool_call_id="c1", tool_name="create_reminder", result={"status": "ok"}
        ),
    ]

    assert reminder_tool_outcome(entries) == "ok"


# Guidance for the main agent.


def test_guidance_tells_the_main_agent_which_tool_arguments_to_pass():
    guidance = reminder_guidance("Alex", "Sam", "jeeves")

    assert 'assistant "jeeves"' in guidance
    assert "your own assistant name" in guidance
    assert 'starts with the name of the person who said it and a colon ("Alex: …")' in guidance
    assert 'for_whom is that name in lower case: "alex" on this turn' in guidance
    assert 'for_whom "household"' in guidance
    assert "script.create_reminder" in guidance


def test_guidance_maps_the_speaker_to_for_whom():
    guidance = reminder_guidance("Alex", "Sam", "jeeves")

    assert "The person speaking now is Alex." in guidance
    assert '"alex" on this turn' in guidance
    assert 'for_whom "household"' in guidance
    assert 'for_whom "sam"' in guidance
    assert "create_reminder" in guidance
    assert "status ok" in guidance


def test_guidance_explains_repeating_reminders():
    guidance = reminder_guidance("Alex", "Sam", "jeeves")

    assert "repeat" in guidance
    for kind in ("daily", "weekdays", "weekends", "weekly", "monthly", "yearly"):
        assert kind in guidance
    assert "repeat_on" in guidance and "repeat_every" in guidance
    assert "repeat_times" in guidance and "repeat_until" in guidance


def test_guidance_explains_cancelling_and_when_to_say_it_is_cancelled():
    guidance = reminder_guidance("Alex", "Sam", "jeeves")

    assert "script.cancel_reminder" in guidance
    assert "cancelled only after cancel_reminder returned status ok" in guidance


@pytest.mark.parametrize(
    "text",
    [
        "Stop reminding me to take my pills",
        "Cancel the reminder about the bins",
        "cancel my pills reminder",
        "Delete the reminder to water the plants",
        "Don't remind me about the bins any more",
        "Remove the bins reminder",
    ],
)
def test_cancelling_a_reminder_is_a_reminder_turn(text):
    assert is_reminder_turn(text)


@pytest.mark.parametrize(
    "text",
    ["Remind me every weekday at 7 to stretch", "Remind us every other Friday to pay rent"],
)
def test_a_repeating_reminder_is_a_reminder_turn(text):
    assert is_reminder_turn(text)


async def test_main_agent_gets_the_guidance_for_person_1(hass: HomeAssistant):
    entry = await add_router(hass, options_with(reminders=REMINDERS_ON, memory={"enabled": False}))
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, "Remind me at 5 pm to call the plumber")

    assert agents.main_prompt == (
        "The person speaking now is Alex.\n\n"
        "The person speaking is Alex.\n\n"
        f"Names are labels.\n\n{reminder_guidance('Alex', 'Sam', 'kitchen')}"
    )


async def test_assistant_name_from_the_settings_is_passed_to_the_main_agent(hass: HomeAssistant):
    entry = await add_router(
        hass, options_with(reminders={**REMINDERS_ON, "assistant": "Jeeves"})
    )
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, "Remind me at 5 pm to call the plumber")

    assert 'assistant "jeeves"' in agents.main_prompt


async def test_remember_rule_sends_timed_reminders_to_the_tool_when_both_are_on(
    hass: HomeAssistant,
):
    entry = await add_router(hass, options_with(reminders=REMINDERS_ON))
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, "What should I listen to tonight?")

    assert f"Names are labels.\n{REMEMBER_RULE}\n{REMEMBER_RULE_REMINDERS}" in agents.main_prompt
    assert "script.create_reminder" in REMEMBER_RULE_REMINDERS
    assert "status ok" in REMEMBER_RULE_REMINDERS
    assert "error" in REMEMBER_RULE_REMINDERS


async def test_remember_rule_is_unchanged_when_reminders_are_off(hass: HomeAssistant):
    entry = await add_router(hass, options_with())
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, "What should I listen to tonight?")

    assert agents.main_prompt.endswith(f"Names are labels.\n{REMEMBER_RULE}")


async def test_main_agent_gets_the_guidance_for_person_2(hass: HomeAssistant):
    entry = await add_router(
        hass, options_with(reminders=REMINDERS_ON, advanced={"no_helper_role": "wife"})
    )
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, "Remind me at 5 pm to call the plumber")

    assert "The person speaking now is Sam." in agents.main_prompt
    assert 'for_whom is that name in lower case: "sam" on this turn' in agents.main_prompt


async def test_prompt_is_unchanged_when_reminders_are_off(hass: HomeAssistant):
    entry = await add_router(hass, options_with(memory={"enabled": False}))
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, "Remind me at 5 pm to call the plumber")

    assert agents.main_prompt == (
        "The person speaking now is Alex.\n\nThe person speaking is Alex.\n\nNames are labels."
    )


async def test_unknown_voice_asking_for_a_reminder_never_reaches_an_agent(hass: HomeAssistant):
    entry = await add_router(
        hass, options_with(reminders=REMINDERS_ON, advanced={"no_helper_role": "stranger"})
    )
    agents = FakeAgents()

    with agents.patched():
        result = await ask(hass, entry, "Remind me at 5 pm to call the plumber")
        await hass.async_block_till_done(wait_background_tasks=True)

    assert result.response.speech["plain"]["speech"] == "The answer is no."
    assert agents.calls == []


# Reminders and memory stay apart.


async def memory_calls_after(hass, text, *, reminders=True, main_log=None) -> list:
    options = options_with(reminders=REMINDERS_ON) if reminders else options_with()
    entry = await add_router(hass, options)
    agents = FakeAgents(
        main_reply="What would you like?",
        memory_reply='{"add": [{"text": "Wants a reminder.", "source": "told"}], "remove": []}',
        main_log=main_log or [],
    )
    with agents.patched():
        if main_log is None:
            await ask(hass, entry, text)
        else:
            await ask_in_pipeline(hass, entry, text)
        await hass.async_block_till_done(wait_background_tasks=True)
    memory = await async_get_memory(hass)
    saved = memory.items(entry_key(entry.entry_id, "owner"))
    return [agents.calls_to(MEMORY_AGENT), saved]


@pytest.mark.parametrize(
    "text",
    [
        "Remind me at 5 pm to call the plumber",
        "Remind us in 20 minutes to check the oven",
        "Remind me to call the plumber",
        "Remind Sam tomorrow to call the plumber",
        "Set a reminder for 7 tomorrow morning",
        "Remember to buy milk at 5 pm",
    ],
)
async def test_a_reminder_request_is_never_sent_to_memory(hass: HomeAssistant, text):
    calls, saved = await memory_calls_after(hass, text)

    assert calls == []
    assert saved == []


async def test_remember_to_do_something_without_a_time_is_not_saved(hass: HomeAssistant):
    calls, saved = await memory_calls_after(hass, "Remember to buy milk")

    assert calls == []
    assert saved == []


@pytest.mark.parametrize(
    "text",
    [
        "Remember that I like jazz",
        "Note that the bins go out on Monday",
        "Remind Sam about the bins",
        "Remember to tell Sam the plumber comes on Friday",
    ],
)
async def test_memory_requests_still_go_to_memory(hass: HomeAssistant, text):
    calls, saved = await memory_calls_after(hass, text)

    assert len(calls) == 1
    assert len(saved) == 1


async def test_a_turn_that_called_the_reminder_tool_is_never_sent_to_memory(hass: HomeAssistant):
    calls, saved = await memory_calls_after(
        hass, "Wake me for the plumber at seven tomorrow morning please", main_log=tool_call("ok")
    )

    assert calls == []
    assert saved == []


async def test_memory_agent_is_told_to_leave_reminders_alone(hass: HomeAssistant):
    calls, _ = await memory_calls_after(hass, "Remember that I like jazz")

    assert "reminder" in calls[0]["extra_system_prompt"].lower()


async def test_a_timed_reminder_is_not_sent_to_memory_even_when_reminders_are_off(
    hass: HomeAssistant,
):
    calls, saved = await memory_calls_after(
        hass, "Remind me at 5 pm to call the plumber", reminders=False
    )

    assert calls == []
    assert saved == []


def test_extraction_instructions_never_save_a_timed_reminder():
    assert "Never save a timed reminder request" in EXTRACTION_INSTRUCTIONS
    assert "confirmation of one" in EXTRACTION_INSTRUCTIONS


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Remind Sam the plumber comes Friday", True),
        ("Remind Sam about the bins", True),
        ("Remind Sam at 6 to call the plumber", False),
        ("Remind me tomorrow to call the plumber", False),
        ("Remind us in an hour to check the oven", False),
        ("Remember to buy milk", False),
        ("Remember that I like jazz", True),
    ],
)
def test_memory_keywords_leave_timed_reminders_alone(text, expected):
    assert has_memory_keyword(text) is expected
    assert should_extract(text, auto_save=True) is expected


# No false confirmations.


async def reply_after(hass, text, main_reply, main_log, earlier=None, **reminder_settings):
    entry = await add_router(
        hass,
        options_with(reminders={**REMINDERS_ON, **reminder_settings}, memory={"enabled": False}),
    )
    agents = FakeAgents(main_reply=main_reply, main_log=main_log)
    with agents.patched():
        result, log = await ask_in_pipeline(hass, entry, text, earlier)
    return result.response.speech["plain"]["speech"], log


async def test_confirmation_without_a_tool_call_is_replaced_by_an_honest_line(
    hass: HomeAssistant,
):
    said, log = await reply_after(
        hass, "Remind me at 5 pm to call the plumber", "Certainly, your reminder is set.", []
    )

    assert said == DEFAULT_FAILURE_LINE
    assert log[-1].content == DEFAULT_FAILURE_LINE
    assert "your reminder is set" not in [entry.content for entry in log]


async def test_the_honest_line_can_be_changed_in_the_settings(hass: HomeAssistant):
    said, _ = await reply_after(
        hass,
        "Remind me at 5 pm to call the plumber",
        "Certainly, your reminder is set.",
        [],
        failure_line="Regrettably, that reminder did not take.",
    )

    assert said == "Regrettably, that reminder did not take."


async def test_confirmation_after_a_successful_tool_call_is_kept(hass: HomeAssistant):
    said, _ = await reply_after(
        hass,
        "Remind me at 5 pm to call the plumber",
        "Certainly, your reminder is set.",
        tool_call("ok"),
    )

    assert said == "Certainly, your reminder is set."


async def test_confirmation_after_a_failed_tool_call_is_replaced(hass: HomeAssistant):
    said, _ = await reply_after(
        hass,
        "Remind me at 5 pm to call the plumber",
        "Certainly, your reminder is set.",
        tool_call("error"),
    )

    assert said == DEFAULT_FAILURE_LINE


# A reply that only mentions reminders is not a false confirmation.

CAPABILITIES = (
    "If you want a reminder set, the forecast checked, or the garden lights dimmed, "
    "I handle it better than anyone."
)


async def test_regression_reminder_mention_a_reply_about_what_it_can_do_is_kept(
    hass: HomeAssistant,
):
    said, _ = await reply_after(hass, "What can you do in a house?", CAPABILITIES, [])

    assert said == CAPABILITIES


@pytest.mark.parametrize(
    "reply",
    [
        CAPABILITIES,
        "I can set reminders for you.",
        "You could ask me to set a reminder for the bins.",
        "If you like, I'll remind you tomorrow.",
        "Should you need one, I will remind you at five.",
    ],
)
def test_regression_reminder_mention_hypothetical_sentences_are_not_confirmations(reply):
    assert not looks_like_confirmation(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "Your reminder is set. If you want another, just ask.",
        "Your reminder is set, you can change it later.",
        "I'll remind you at five, and you could snooze it then.",
    ],
)
def test_regression_reminder_mention_a_claim_beside_a_hypothetical_still_counts(reply):
    assert looks_like_confirmation(reply)


async def test_regression_reminder_mention_a_claim_outside_a_reminder_exchange_is_kept(
    hass: HomeAssistant,
):
    said, _ = await reply_after(
        hass, "What can you do in a house?", "Certainly, your reminder is set.", []
    )

    assert said == "Certainly, your reminder is set."


async def test_regression_reminder_mention_the_follow_up_after_what_time_is_still_checked(
    hass: HomeAssistant,
):
    said, _ = await reply_after(
        hass,
        "Yes please",
        "Done, your reminder is set for five.",
        [],
        earlier=[
            ("Remind me to call the plumber", "What time should I remind you?"),
            ("At five, I think", "Five in the afternoon?"),
        ],
    )

    assert said == DEFAULT_FAILURE_LINE


async def test_regression_reminder_mention_three_turns_back_is_too_far(hass: HomeAssistant):
    said, _ = await reply_after(
        hass,
        "Yes please",
        "Done, your reminder is set for five.",
        [],
        earlier=[
            ("Remind me to call the plumber", "What time should I remind you?"),
            ("Actually, what is the weather like?", "Sunny."),
            ("And the lights?", "They are on."),
        ],
    )

    assert said == "Done, your reminder is set for five."


async def test_a_relayed_error_is_kept(hass: HomeAssistant):
    said, _ = await reply_after(
        hass,
        "Remind me at 5 pm to call the plumber",
        "I couldn't set that reminder: that time is in the past.",
        tool_call("error"),
    )

    assert said == "I couldn't set that reminder: that time is in the past."


async def test_a_question_back_is_kept(hass: HomeAssistant):
    said, _ = await reply_after(
        hass, "Remind me to call the plumber", "What time should I remind you?", []
    )

    assert said == "What time should I remind you?"


async def test_a_reply_to_something_else_is_kept(hass: HomeAssistant):
    said, _ = await reply_after(hass, "Turn the heating up a bit please", "Consider it done.", [])

    assert said == "Consider it done."


async def test_replies_are_left_alone_when_reminders_are_off(hass: HomeAssistant):
    entry = await add_router(hass, options_with(memory={"enabled": False}))
    agents = FakeAgents(main_reply="Certainly, your reminder is set.")

    with agents.patched():
        result, _ = await ask_in_pipeline(hass, entry, "Remind me at 5 pm to call the plumber")

    assert result.response.speech["plain"]["speech"] == "Certainly, your reminder is set."


async def test_replies_are_left_alone_when_the_tool_calls_cannot_be_seen(hass: HomeAssistant):
    entry = await add_router(hass, options_with(reminders=REMINDERS_ON, memory={"enabled": False}))
    agents = FakeAgents(main_reply="Certainly, your reminder is set.")

    with agents.patched():
        result = await ask(hass, entry, "Remind me at 5 pm to call the plumber")

    assert result.response.speech["plain"]["speech"] == "Certainly, your reminder is set."
