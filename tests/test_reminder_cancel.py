# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cancelling reminders: the reply check, and the setting that allows it."""

from homeassistant.components.conversation.chat_log import (
    AssistantContent,
    ToolResultContent,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from custom_components.voice_assistant_manager.reminders import (
    DEFAULT_CANCEL_FAILURE_LINE,
    DEFAULT_FAILURE_LINE,
    reminder_guidance,
)

from .router_helpers import (
    MAIN_AGENT,
    FakeAgents,
    add_router,
    ask_in_pipeline,
    options_with,
)
from .test_panel import Page, page  # noqa: F401 - fixture

CANCEL = "script__cancel_reminder"


def cancel_call(status: str | None) -> list:
    """Chat log entries of one cancel_reminder call. None: no result came back."""
    entries: list = [
        AssistantContent(
            agent_id=MAIN_AGENT,
            content=None,
            tool_calls=[
                llm.ToolInput(
                    tool_name=CANCEL, tool_args={"title": "pills", "for_whom": "alex"}, id="c1"
                )
            ],
        )
    ]
    if status is not None:
        message = {"status": status, "title": "Take the pills", "removed": 1}
        entries.append(
            ToolResultContent(
                agent_id=MAIN_AGENT, tool_call_id="c1", tool_name=CANCEL,
                result=llm.ToolResult(data={"result": message}),
            )
        )
    return entries


async def reply_after(hass, text, main_reply, main_log, **reminders):
    entry = await add_router(
        hass,
        options_with(reminders={"enabled": True, **reminders}, memory={"enabled": False}),
    )
    agents = FakeAgents(main_reply=main_reply, main_log=main_log)
    with agents.patched():
        result, log = await ask_in_pipeline(hass, entry, text)
    return result.response.speech["plain"]["speech"], log


CONFIRMED = "Done, I've removed your reminder to take the pills that was set for 8 am."


async def test_regression_a_cancelled_reminder_is_not_reported_as_a_failure_to_set_one(
    hass: HomeAssistant,
):
    said, log = await reply_after(
        hass, "Stop reminding me to take my pills", CONFIRMED, cancel_call("ok")
    )

    assert said == CONFIRMED
    assert log[-1].content == CONFIRMED


async def test_a_cancellation_claimed_without_the_tool_is_corrected(hass: HomeAssistant):
    said, _ = await reply_after(
        hass, "Cancel my pills reminder", "Okay, I've cancelled your pills reminder.", []
    )

    assert said == DEFAULT_CANCEL_FAILURE_LINE
    assert said != DEFAULT_FAILURE_LINE


async def test_a_cancellation_claimed_after_the_tool_failed_is_corrected(hass: HomeAssistant):
    said, _ = await reply_after(
        hass, "Cancel my pills reminder", "Done, that reminder is removed.", cancel_call("error")
    )

    assert said == DEFAULT_CANCEL_FAILURE_LINE


async def test_an_honest_answer_about_a_cancellation_is_left_alone(hass: HomeAssistant):
    reply = "I found two reminders about feeding. Which one should I cancel?"

    said, _ = await reply_after(hass, "Cancel the feeding reminder", reply, cancel_call("error"))

    assert said == reply


async def test_a_reminder_set_falsely_is_still_corrected_with_the_set_line(hass: HomeAssistant):
    said, _ = await reply_after(
        hass, "Remind me at 5 pm to call the plumber", "Certainly, your reminder is set.", []
    )

    assert said == DEFAULT_FAILURE_LINE


# The setting


def test_cancelling_is_allowed_by_default():
    guidance = reminder_guidance("Alex", "Sam", "jeeves")

    assert "script.cancel_reminder" in guidance


def test_without_cancelling_the_agent_is_told_it_cannot_and_where_to_do_it():
    guidance = reminder_guidance("Alex", "Sam", "jeeves", cancel=False)

    assert "cancel_reminder" not in guidance
    assert "You cannot cancel reminders" in guidance
    assert "calendar" in guidance


async def test_the_main_agent_gets_the_guidance_without_cancelling_when_it_is_off(
    hass: HomeAssistant,
):
    entry = await add_router(
        hass, options_with(reminders={"enabled": True, "cancel": False}, memory={"enabled": False})
    )
    agents = FakeAgents()

    with agents.patched():
        await ask_in_pipeline(hass, entry, "Remind me at 5 pm to call the plumber")

    assert "You cannot cancel reminders" in agents.main_prompt
    assert "cancel_reminder" not in agents.main_prompt


async def test_the_page_saves_the_setting(hass: HomeAssistant, page):
    entry = await add_router(hass, options_with(reminders={"enabled": True}))
    settings = (await page.router(entry))["settings"]
    settings["reminders"] = {**settings["reminders"], "cancel": False}

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert entry.options["reminders"]["cancel"] is False
