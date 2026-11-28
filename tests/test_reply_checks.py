# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""reply_checks on their own: the decisions, without the router around them."""

from homeassistant.components.conversation.chat_log import AssistantContent
from homeassistant.helpers import intent

from custom_components.voice_assistant_manager.reply_checks import (
    reminder_trouble,
    unbacked_claim,
)

from .router_helpers import MAIN_AGENT
from .test_claim_retry import FALSE_SET, lights_call
from .test_reminder_cancel import cancel_call
from .test_reminders import tool_call

REMIND_ME = "Remind me tomorrow at nine to call the plumber"


def reply(text: str) -> intent.IntentResponse:
    response = intent.IntentResponse(language="en")
    response.async_set_speech(text)
    return response


def said(text: str) -> list:
    """The chat log entry of a reply without any tool call."""
    return [AssistantContent(agent_id=MAIN_AGENT, content=text)]


def no_earlier() -> list[str]:
    return []


def must_not_be_read() -> list[str]:
    raise AssertionError("the earlier turns were read")


# reminder_trouble


def test_a_turn_whose_tool_calls_cannot_be_seen_is_never_corrected():
    fix, about = reminder_trouble(REMIND_ME, reply(FALSE_SET), [], False, no_earlier)

    assert (fix, about) == (None, True)


def test_a_reminder_confirmed_without_a_call_needs_the_reminder_line():
    fix, about = reminder_trouble(
        REMIND_ME, reply(FALSE_SET), said(FALSE_SET), True, no_earlier
    )

    assert (fix, about) == ("reminder", True)


def test_a_reminder_the_tool_set_is_left_alone():
    turn = [*tool_call("ok"), *said(FALSE_SET)]

    fix, about = reminder_trouble(REMIND_ME, reply(FALSE_SET), turn, True, no_earlier)

    assert (fix, about) == (None, True)


def test_an_earlier_turn_makes_a_short_answer_a_reminder_exchange():
    fix, _ = reminder_trouble(
        "Yes please", reply(FALSE_SET), said(FALSE_SET), True, lambda: [REMIND_ME]
    )

    assert fix == "reminder"


def test_a_reply_that_mentions_reminders_outside_a_reminder_exchange_is_left_alone():
    text = "I can set reminders for you."

    fix, about = reminder_trouble("What can you do", reply(text), said(text), True, no_earlier)

    assert (fix, about) == (None, False)


def test_a_cancellation_confirmed_without_a_call_needs_the_cancel_line():
    text = "I've cancelled the reminder to take the pills."

    fix, about = reminder_trouble(
        "Stop reminding me to take the pills", reply(text), said(text), True, no_earlier
    )

    assert (fix, about) == ("cancel", True)


def test_a_cancellation_the_tool_made_is_left_alone():
    text = "I've cancelled the reminder to take the pills."
    turn = [*cancel_call("ok"), *said(text)]

    fix, _ = reminder_trouble(
        "Stop reminding me to take the pills", reply(text), turn, True, no_earlier
    )

    assert fix is None


def test_a_listing_without_a_look_up_needs_the_list_line():
    text = "You have one reminder, tomorrow at nine."

    fix, about = reminder_trouble(
        "Which reminders do I have", reply(text), said(text), True, no_earlier
    )

    assert (fix, about) == ("list", True)


def test_a_listing_from_another_tool_is_left_alone():
    text = "You have one reminder, tomorrow at nine."
    turn = [*lights_call(), *said(text)]

    fix, _ = reminder_trouble("Which reminders do I have", reply(text), turn, True, no_earlier)

    assert fix is None


# unbacked_claim

LIGHT_ON = "I've turned on the kitchen light."


def test_a_claim_after_a_tool_that_worked_is_backed():
    turn = [*lights_call(), *said(LIGHT_ON)]

    assert unbacked_claim("Kitchen light on", reply(LIGHT_ON), turn, False, no_earlier) is None


def test_an_action_claimed_without_a_call_is_an_action_claim():
    claim = unbacked_claim("Kitchen light on", reply(LIGHT_ON), said(LIGHT_ON), False, no_earlier)

    assert claim == "action"


def test_an_action_claimed_after_every_call_failed_is_a_failed_claim():
    turn = [*tool_call("error"), *said(LIGHT_ON)]

    assert unbacked_claim("Kitchen light on", reply(LIGHT_ON), turn, False, no_earlier) == "failed"


def test_a_failed_reminder_call_is_left_to_the_reminder_check():
    turn = [*tool_call("error"), *said(FALSE_SET)]

    assert unbacked_claim(REMIND_ME, reply(FALSE_SET), turn, True, no_earlier) is None


def test_a_reminder_confirmed_without_a_call_is_a_reminder_claim():
    claim = unbacked_claim(REMIND_ME, reply(FALSE_SET), said(FALSE_SET), True, no_earlier)

    assert claim == "reminder"


def test_a_message_to_remember_is_never_a_claim():
    text = "Noted, I'll remember that."

    claim = unbacked_claim(
        "Remember that the bins go out on Friday", reply(text), said(text), True, no_earlier
    )

    assert claim is None


def test_the_earlier_turns_are_not_read_while_reminders_are_off():
    claim = unbacked_claim(
        "Kitchen light on", reply(LIGHT_ON), said(LIGHT_ON), False, must_not_be_read
    )

    assert claim == "action"
