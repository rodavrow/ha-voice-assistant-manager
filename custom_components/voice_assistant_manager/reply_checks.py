# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks on a finished turn: does the reply claim something its tool calls do not back?

Plain decisions from the person's words, the reply and the turn's chat log entries. The
router (conversation.py) does the logging, the retry and the honest reply.
"""

from collections.abc import Callable, Sequence
from typing import Any, Literal

from homeassistant.helpers import intent

from .helpers import speech_text
from .memory import has_memory_keyword
from .reminders import (
    CANCEL_TOOL,
    LIST_TOOL,
    REMINDER_TOOL,
    a_tool_worked,
    is_cancel_turn,
    is_list_turn,
    is_reminder_exchange,
    is_reminder_turn,
    looks_like_action_claim,
    looks_like_cancellation,
    looks_like_confirmation,
    looks_like_listing,
    needs_honest_cancel_failure,
    needs_honest_failure,
    reminder_tool_outcome,
    used_a_tool,
)

# Which honest line a reminder reply needs: it listed reminders without looking them up,
# confirmed a cancellation it did not make, or confirmed a reminder it did not set.
type ReminderFix = Literal["list", "cancel", "reminder"]
# What a reply says was done without a tool call that did it ("failed": every call
# failed). "reminder", "cancel" and "list" only when reminders are on.
type Claim = Literal["reminder", "cancel", "list", "action", "failed"]
# The person's words in the turns before this one, read only when needed.
type EarlierTexts = Callable[[], list[str]]


def reminder_trouble(
    text: str,
    response: intent.IntentResponse,
    turn: Sequence[Any],
    visible: bool,
    earlier: EarlierTexts,
) -> tuple[ReminderFix | None, bool]:
    """(the honest line the reply needs or None, whether the turn was about a reminder).

    `turn` holds the chat log entries the main agent wrote this turn and `visible` says
    whether it wrote any; without them nothing is corrected. Entries are any chat log
    content, hence Any.
    """
    outcome = reminder_tool_outcome(turn)
    cancelled = reminder_tool_outcome(turn, CANCEL_TOOL)
    listed = reminder_tool_outcome(turn, LIST_TOOL)
    looking_up = (
        outcome == "none" and cancelled == "none" and (listed != "none" or is_list_turn(text))
    )
    about_reminder = (
        outcome != "none" or cancelled != "none" or looking_up or is_reminder_turn(text)
    )
    if not visible:
        return None, about_reminder
    reply = speech_text(response)
    if looking_up:
        # A look-up: its answer says when a reminder is set for, which is no claim that
        # one was set. It must come from the tool, though.
        if listed == "ok" or not looks_like_listing(reply):
            return None, about_reminder
        if listed == "none" and used_a_tool(turn):
            # It looked somewhere else (a calendar tool, say): not made up.
            return None, about_reminder
        return "list", True
    if cancelled != "none" or is_cancel_turn(text):
        # A cancellation: its reply mentions the reminder and when it was set for, which
        # is no claim that one was set.
        if not needs_honest_cancel_failure(reply, cancelled):
            return None, about_reminder
        return "cancel", True
    # Only an exchange about a reminder can falsely confirm one. A reply to "what can you
    # do?" that mentions reminders is not a claim that one was set.
    recent = [text, *earlier()]
    if outcome == "none" and not is_reminder_exchange(recent):
        return None, about_reminder
    if not needs_honest_failure(reply, outcome, has_memory_keyword(text)):
        return None, about_reminder
    return "reminder", True


def unbacked_claim(
    text: str,
    response: intent.IntentResponse,
    turn: Sequence[Any],
    reminders: bool,
    earlier: EarlierTexts,
) -> Claim | None:
    """What the reply says was done although no tool call did it this turn, or None.

    `turn` holds the chat log entries the main agent wrote this turn (any chat log
    content, hence Any). A message to remember is the memory's business, which has no
    tool.
    """
    if a_tool_worked(turn) or has_memory_keyword(text):
        return None
    reply = speech_text(response)
    if used_a_tool(turn):
        # Every call failed: a reply that says it was done is no truer than one after no
        # call at all. A reminder tool that failed has its own check, which answers in its
        # own words.
        if reminders and any(
            reminder_tool_outcome(turn, tool) != "none"
            for tool in (REMINDER_TOOL, CANCEL_TOOL, LIST_TOOL)
        ):
            return None
        return "failed" if looks_like_action_claim(reply) else None
    if reminders:
        if is_cancel_turn(text) and looks_like_cancellation(reply):
            return "cancel"
        if is_list_turn(text) and looks_like_listing(reply):
            return "list"
        if is_reminder_exchange([text, *earlier()]) and looks_like_confirmation(reply):
            return "reminder"
    return "action" if looks_like_action_claim(reply) else None
