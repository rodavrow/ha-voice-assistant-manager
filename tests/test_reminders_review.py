# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the findings of the pre-release review of reminder support."""

from types import SimpleNamespace

import pytest
from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import AssistantContent, ToolResultContent
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm

from custom_components.voice_assistant_manager.memory import has_memory_keyword, should_extract
from custom_components.voice_assistant_manager.reminders import (
    DEFAULT_FAILURE_LINE,
    has_alert_time,
    is_reminder_turn,
    is_unclear_remember_to,
    looks_like_confirmation,
    reminder_guidance,
    reminder_tool_outcome,
)

from .router_helpers import (
    MAIN_AGENT,
    REFUSAL_AGENT,
    FakeAgents,
    add_router,
    ask_in_pipeline,
    options_with,
    router_entity_id,
)
from .test_compose_announcement import SATELLITE, compose, pipeline, setup
from .test_reminders import REMINDERS_ON, TOOL, tool_call

CONFIRMED = "Certainly, your reminder is set."


# Finding 1: Home Assistant wraps a tool's result in llm.ToolResult.


def real_tool_result(status: str, *, error: bool = False) -> list:
    return [
        AssistantContent(
            agent_id=MAIN_AGENT,
            content=None,
            tool_calls=[llm.ToolInput(tool_name=TOOL, tool_args={}, id="c1")],
        ),
        ToolResultContent(
            agent_id=MAIN_AGENT,
            tool_call_id="c1",
            tool_name=TOOL,
            result=llm.ToolResult(data={"result": {"status": status}}, error=error),
        ),
    ]


def test_regression_review_1_real_tool_result_with_status_ok_is_ok():
    assert reminder_tool_outcome(real_tool_result("ok")) == "ok"


def test_regression_review_1_real_tool_result_with_status_error_is_error():
    assert reminder_tool_outcome(real_tool_result("error")) == "error"


def test_regression_review_1_a_tool_result_flagged_as_error_is_error():
    assert reminder_tool_outcome(real_tool_result("ok", error=True)) == "error"


async def test_regression_review_1_a_reminder_that_was_set_keeps_its_confirmation(
    hass: HomeAssistant,
):
    entry = await add_router(hass, options_with(reminders=REMINDERS_ON, memory={"enabled": False}))
    agents = FakeAgents(main_reply=CONFIRMED, main_log=real_tool_result("ok"))

    with agents.patched():
        result, _ = await ask_in_pipeline(hass, entry, "Remind me at 5 pm to call the plumber")

    assert result.response.speech["plain"]["speech"] == CONFIRMED


# Finding 2: a chat log the main agent did not write to shows nothing.


async def test_regression_review_2_reply_is_kept_when_the_agent_does_not_use_the_chat_log(
    hass: HomeAssistant,
):
    entry = await add_router(hass, options_with(reminders=REMINDERS_ON, memory={"enabled": False}))
    agents = FakeAgents(main_reply=CONFIRMED, main_writes_log=False)

    with agents.patched():
        result, log = await ask_in_pipeline(hass, entry, "Remind me at 5 pm to call the plumber")

    assert [entry.role for entry in log] == ["system", "user"]
    assert result.response.speech["plain"]["speech"] == CONFIRMED


# Finding 3: replies that are not a reminder confirmation must be left alone.


@pytest.mark.parametrize(
    "reply",
    [
        "I've set a timer for 10 minutes.",
        "I've added milk to your shopping list.",
        "The thermostat is set to 21 degrees.",
        "You asked me to remind you to call the plumber at 5 pm.",
        "Tell me when you would like the reminder.",
        "The reminder needs a time in the future.",
        "Consider it done.",
        "Done.",
    ],
)
def test_regression_review_3_other_replies_are_not_reminder_confirmations(reply):
    assert not looks_like_confirmation(reply)


async def test_regression_review_3_a_relayed_error_without_a_negative_word_is_kept(
    hass: HomeAssistant,
):
    entry = await add_router(hass, options_with(reminders=REMINDERS_ON, memory={"enabled": False}))
    relayed = "The reminder needs a time in the future."
    agents = FakeAgents(main_reply=relayed, main_log=tool_call("error"))

    with agents.patched():
        result, _ = await ask_in_pipeline(hass, entry, "Remind me at 5 pm to call the plumber")

    assert result.response.speech["plain"]["speech"] == relayed


# Finding 4: a false confirmation is caught whatever the person's last words were.


@pytest.mark.parametrize(
    "reply",
    [
        "Done, your reminder is set for five.",
        "Reminder set.",
        "I have scheduled the reminder for 5 pm.",
        "Very well, I'll remind you at five.",
    ],
)
def test_regression_review_4_explicit_claims_are_confirmations(reply):
    assert looks_like_confirmation(reply)


@pytest.mark.parametrize(
    "request_text",
    # Each names a time to be alerted at, so the exchange counts as about a reminder.
    # "Yes please" with a reminder asked for a turn earlier is tested in test_reminders.py.
    ["At five", "Put a reminder in for 5 pm", "Wake me at seven tomorrow"],
)
async def test_regression_review_4_false_confirmation_is_caught_whatever_was_asked(
    hass: HomeAssistant, request_text
):
    entry = await add_router(hass, options_with(reminders=REMINDERS_ON, memory={"enabled": False}))
    agents = FakeAgents(main_reply="Done, your reminder is set for five.")

    with agents.patched():
        result, _ = await ask_in_pipeline(hass, entry, request_text)

    assert result.response.speech["plain"]["speech"] == DEFAULT_FAILURE_LINE


# Finding 5: memory requests must not be dropped as reminders.


@pytest.mark.parametrize(
    "text",
    [
        "Remember to call me Al",
        "Remember to always use the blue towels",
        "Remember to never play jazz",
        "Remember to use Celsius",
        "Remember that I want you to remind me of things gently",
        "Note that I asked you to remind me about jazz",
    ],
)
def test_regression_review_5_standing_preferences_are_memory_not_reminders(text):
    assert not is_unclear_remember_to(text)
    assert not is_reminder_turn(text)
    assert has_memory_keyword(text)
    assert should_extract(text, auto_save=False)


# Finding 6: the announcement agent may not be one that can control the home.


async def test_regression_review_6_writer_that_can_control_the_home_is_refused(
    hass: HomeAssistant,
):
    _, pipelines = await setup(hass)
    hass.states.async_set(
        REFUSAL_AGENT,
        "unknown",
        {"supported_features": int(conversation.ConversationEntityFeature.CONTROL)},
    )
    agents = FakeAgents()

    assert await compose(hass, pipelines, agents) == {"text": ""}
    assert agents.calls == []


async def test_regression_review_6_the_built_in_agent_is_refused_as_writer(hass: HomeAssistant):
    _, pipelines = await setup(
        hass, options_with(reminders={"agent": conversation.HOME_ASSISTANT_AGENT})
    )
    agents = FakeAgents()

    assert await compose(hass, pipelines, agents) == {"text": ""}
    assert agents.calls == []


async def test_regression_review_6_writer_without_control_is_accepted(hass: HomeAssistant):
    _, pipelines = await setup(hass)
    hass.states.async_set(REFUSAL_AGENT, "unknown", {"supported_features": 0})
    agents = FakeAgents(writer_reply="Alex, the plumber, at 5 pm.")

    assert await compose(hass, pipelines, agents) == {"text": "Alex, the plumber, at 5 pm."}


# Finding 8: more ways to name a time.


@pytest.mark.parametrize(
    "text",
    [
        "in fifteen minutes",
        "in forty minutes",
        "in a couple of hours",
        "in a few minutes",
        "in the morning",
        "at lunchtime",
        "on the 5th",
    ],
)
def test_regression_review_8_more_alert_times_are_recognised(text):
    assert has_alert_time(f"Remind Sam to take the bins out {text}")


def test_regression_review_8_at_tennis_is_not_a_time():
    assert not has_alert_time("Remember to pick up my racket at tennis")


# Findings 9 to 12: details of finding the pipeline.


async def test_regression_review_9_a_wildcard_language_is_not_passed_on(hass: HomeAssistant):
    entry, pipelines = await setup(hass)
    pipelines.all = [
        SimpleNamespace(
            name="Kitchen pipeline",
            conversation_engine=router_entity_id(hass, entry),
            conversation_language="*",
        )
    ]
    agents = FakeAgents()

    await compose(hass, pipelines, agents)

    assert agents.calls[0]["language"] is None


async def test_regression_review_10_primary_select_is_found_by_unique_id_after_a_rename(
    hass: HomeAssistant,
):
    entry, pipelines = await setup(hass, pipeline_name=None)
    registry = er.async_get(hass)
    sat = registry.async_get(SATELLITE)
    registry.async_get_or_create(
        "select", "test", "aabbcc-pipeline", device_id=sat.device_id,
        suggested_object_id="keuken_assistent",
    )
    hass.states.async_set("select.keuken_assistent", "Kitchen pipeline")
    agents = FakeAgents(writer_reply="Alex, the plumber, at 5 pm.")

    assert await compose(hass, pipelines, agents) == {"text": "Alex, the plumber, at 5 pm."}


async def test_regression_review_11_unknown_pipeline_name_falls_back_to_the_preferred_one(
    hass: HomeAssistant,
):
    entry, pipelines = await setup(hass, pipeline_name="Deleted pipeline")
    pipelines.preferred = pipeline("Preferred pipeline", router_entity_id(hass, entry))
    agents = FakeAgents(writer_reply="Alex, the plumber, at 5 pm.")

    assert await compose(hass, pipelines, agents) == {"text": "Alex, the plumber, at 5 pm."}


async def test_regression_review_12_an_entity_that_is_not_a_satellite_is_rejected(
    hass: HomeAssistant,
):
    _, pipelines = await setup(hass)
    hass.states.async_set("light.kitchen", "on")

    with pytest.raises(Exception):  # noqa: B017, PT011 - schema validation error
        await compose(hass, pipelines, FakeAgents(), satellite="light.kitchen")


# Finding 14: a speaker without a name.


def test_regression_review_14_guidance_without_a_speaker_name_says_to_ask():
    guidance = reminder_guidance("", "Sam", "jeeves")

    assert "ask who the reminder is for" in guidance


# Finding 15: the real conversation call, not a stand-in.


async def test_regression_review_15_a_writer_that_does_not_exist_returns_empty_text(
    hass: HomeAssistant,
):
    """Nothing is patched at the agent boundary here: the real async_converse runs and fails."""
    _, pipelines = await setup(
        hass, options_with(reminders={"agent": "conversation.does_not_exist"})
    )
    all_pipelines, preferred = pipelines.patched()

    with all_pipelines, preferred:
        response = await hass.services.async_call(
            "voice_assistant_manager",
            "compose_announcement",
            {"satellite": SATELLITE, "kind": "reminder", "text": "Stretch", "recipient": "alex"},
            blocking=True,
            return_response=True,
        )

    assert response == {"text": ""}


# A message to pass on is memory's business, so "I'll remind Sam" is true.


async def test_passing_on_a_message_is_not_a_false_reminder_confirmation(hass: HomeAssistant):
    entry = await add_router(hass, options_with(reminders=REMINDERS_ON))
    agents = FakeAgents(main_reply="Very good, I'll remind Sam about the bins.")

    with agents.patched():
        result, _ = await ask_in_pipeline(hass, entry, "Remind Sam about the bins")
        await hass.async_block_till_done(wait_background_tasks=True)

    assert result.response.speech["plain"]["speech"] == "Very good, I'll remind Sam about the bins."
