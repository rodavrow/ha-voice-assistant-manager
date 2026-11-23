# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reminders: telling them apart from memory, and guarding against false confirmations.

The reminders themselves live in Home Assistant: a script exposed to the main agent as
the create_reminder tool. This module only holds the rules the router applies around it.
No Home Assistant state is read here, so the rules can be tested alone.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

# Home Assistant names a script tool "script__<name>". Older versions used the bare name.
REMINDER_TOOL = "create_reminder"
HOUSEHOLD = "household"
DEFAULT_FAILURE_LINE = "I could not set that reminder. Please ask me again."

ToolOutcome = Literal["none", "ok", "error"]

_REMIND_SOMEONE = re.compile(r"\bremind (\w+)\b", re.IGNORECASE)
_SET_REMINDER = re.compile(
    r"\b(?:set|add|create|make|schedule) (?:a|an|another|the|me a|us a) reminder\b",
    re.IGNORECASE,
)
_REMEMBER_TO = re.compile(r"\bremember to (\w+)", re.IGNORECASE)
_REMEMBER_THAT = re.compile(r"\b(?:remember|note|keep in mind) that\b", re.IGNORECASE)
# "Remember to tell Sam …" is a message for the other person, which memory keeps.
_PASS_ON_VERBS = {"tell", "let", "remind", "mention", "pass", "inform", "ask"}
# "Remember to always use the blue towels" is a standing preference, which memory keeps.
_STANDING = re.compile(
    r"\bremember to (?:always|never|not|use|call me|address|refer to|speak|be)\b",
    re.IGNORECASE,
)
_NUMBER = (
    r"(?:\d+|a couple of|a few|several|half an?|an?|one|two|three|four|five|six|seven|eight|"
    r"nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|ninety)"
)
_DAY = r"(?:mon|tues|wednes|thurs|fri|satur|sun)day"
_ALERT_TIME = re.compile(
    rf"\bat (?:\d|(?:noon|midnight|half|quarter|one|two|three|four|five|six|seven|eight|nine|"
    rf"ten|eleven|twelve|lunchtime|dinnertime|bedtime|sunrise|sunset)\b)"
    rf"|\bin {_NUMBER} (?:second|minute|hour|day|week|month)s?\b"
    rf"|\bin the (?:morning|afternoon|evening)\b"
    rf"|\b(?:tomorrow|tonight|today|this (?:morning|afternoon|evening)|noon|midnight)\b"
    rf"|\b(?:on|next|every|this) {_DAY}\b|\bnext (?:week|month|year)\b"
    rf"|\bevery (?:day|week|morning|evening|night)\b|\bon the \d{{1,2}}(?:st|nd|rd|th)\b"
    rf"|\b\d{{1,2}}(?::\d{{2}})? ?(?:am|pm)\b|\bo'clock\b",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"\b(?:not|never|unable|sorry|failed|fail|error|afraid|apologi[sz]e)\b|n't\b|\bcannot\b",
    re.IGNORECASE,
)
# A reply only counts as a confirmation when it says so about a reminder. "Done" or
# "I've set a timer" could be about anything else the main agent did.
_CLAIM = re.compile(
    r"\breminders?\b[^.?!]*\b(?:set|created|scheduled|added|saved|booked)\b"
    r"|\b(?:set|created|scheduled|added|saved|booked)\b[^.?!]*\breminders?\b"
    r"|\b(?:will|shall) remind\b|'ll remind\b",
    re.IGNORECASE,
)
# A sentence about what could happen is not a claim that it did: "If you want a reminder
# set, I handle it", "I can set reminders", "you could ask me to set one".
_HYPOTHETICAL = re.compile(
    r"^\s*(?:if|should|whenever|in case)\b"
    r"|\bif you (?:want|like|wish|need|ever)\b"
    r"|\b(?:i|we|you) (?:can|could|might|may)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[.!])\s+")


def has_alert_time(text: str) -> bool:
    """Whether the text names a moment to be alerted at ("at 5 pm", "in an hour")."""
    return bool(_ALERT_TIME.search(text or ""))


def _remember_to_verb(text: str) -> str | None:
    """The verb after "remember to", unless it is a message to pass on or a preference."""
    match = _REMEMBER_TO.search(text)
    if match is None or match.group(1).casefold() in _PASS_ON_VERBS or _STANDING.search(text):
        return None
    return match.group(1)


def is_unclear_remember_to(text: str) -> bool:
    """ "Remember to <do something>" with no time: a reminder or a memory? Ask first."""
    text = text or ""
    return _remember_to_verb(text) is not None and not has_alert_time(text)


def is_reminder_request(text: str) -> bool:
    """Whether the person asked to be reminded, or for a reminder to be set."""
    text = text or ""
    if _SET_REMINDER.search(text) or _REMIND_SOMEONE.search(text):
        return True
    return _remember_to_verb(text) is not None and has_alert_time(text)


def is_reminder_turn(text: str) -> bool:
    """Whether memory must leave this request alone because it is about a reminder.

    "Remind me/us …" and "set a reminder" always are. "Remind Sam …" only with a time:
    without one it is a message for Sam, which memory keeps. "Remember to …" with a time
    is a reminder, and without one it is unclear, so nothing is saved either way.
    "Remember that …" with no time is always a memory, whatever else it mentions.
    """
    text = text or ""
    if _REMEMBER_THAT.search(text) and not has_alert_time(text):
        return False
    if _SET_REMINDER.search(text) or is_unclear_remember_to(text):
        return True
    reminded = _REMIND_SOMEONE.search(text)
    if reminded is not None:
        return reminded.group(1).casefold() in ("me", "us") or has_alert_time(text)
    return is_reminder_request(text)


def looks_like_confirmation(reply: str) -> bool:
    """Whether a reply claims a reminder was set.

    Questions, failures and hypotheticals do not: a sentence whose claim comes after
    "if you want", "I can" and the like is about what could happen.
    """
    reply = reply or ""
    if not reply.strip() or "?" in reply or _NEGATIVE.search(reply):
        return False
    return any(_claims_in(sentence) for sentence in _SENTENCE_END.split(reply))


def _claims_in(sentence: str) -> bool:
    """Whether one sentence claims a reminder, with no hypothetical before the claim."""
    claim = _CLAIM.search(sentence)
    return claim is not None and not _HYPOTHETICAL.search(sentence[: claim.start()])


def _status(result: Any) -> str | None:
    """The "status" of a tool result, however deeply the tool wrapped it."""
    if isinstance(result, Mapping):
        if isinstance(result.get("status"), str):
            return str(result["status"])
        for value in result.values():
            if (found := _status(value)) is not None:
                return found
    return None


def reminder_tool_outcome(entries: Iterable[Any]) -> ToolOutcome:
    """What happened to create_reminder in the chat log entries of one turn.

    "ok" when a call returned status ok, "error" when it was called and none did,
    "none" when it was never called.
    """
    called = False
    for entry in entries:
        for call in getattr(entry, "tool_calls", None) or []:
            if str(getattr(call, "tool_name", "")).endswith(REMINDER_TOOL):
                called = True
        if str(getattr(entry, "tool_name", "")).endswith(REMINDER_TOOL):
            called = True
            # Home Assistant wraps what the script returned in llm.ToolResult(data, error).
            result = getattr(entry, "result", None)
            failed = bool(getattr(result, "error", False))
            if not failed and _status(getattr(result, "data", result)) == "ok":
                return "ok"
    return "error" if called else "none"


def is_reminder_exchange(texts: Iterable[str]) -> bool:
    """Whether a conversation is about a reminder, judged from what the person said.

    `texts` are the person's latest messages. Any one that asks for a reminder, or names
    a moment to be alerted at ("at five", "tomorrow at seven"), makes it one. Looking
    back a turn or two covers the confirmation that follows "what time?".
    """
    return any(is_reminder_turn(text) or has_alert_time(text) for text in texts)


def needs_honest_failure(reply: str, outcome: ToolOutcome, memory_request: bool = False) -> bool:
    """Whether a reply confirms a reminder that create_reminder did not set.

    Only call it for an exchange about a reminder (see is_reminder_exchange): a reply
    that merely mentions reminders, in a conversation about something else, is left
    alone. A message to pass on ("remind Sam about the bins") is memory's business, so
    "I'll remind Sam" is true there.
    """
    return outcome != "ok" and not memory_request and looks_like_confirmation(reply)


def for_whom(name: str) -> str:
    """The create_reminder value for a person: their name from the settings, lower case."""
    return " ".join(str(name or "").split()).casefold()


def _normalise(name: str) -> str:
    return " ".join(str(name or "").split()).casefold()


def assistant_name(title: str, setting: str | None) -> str:
    """The name a router goes by as an assistant: the setting, or the title's first word.

    A router titled "Jeeves (by speaker)" is the assistant "jeeves" unless told otherwise.
    """
    chosen = _normalise(setting or "")
    if chosen:
        return chosen
    words = _normalise(title).split()
    return words[0] if words else ""


def assistant_aliases(title: str, setting: str | None) -> set[str]:
    """Every name that picks this router as an assistant.

    With a name in the settings, only that name. Otherwise the title, the title without
    a trailing "(…)", and the title's first word.
    """
    chosen = _normalise(setting or "")
    if chosen:
        return {chosen}
    full = _normalise(title)
    without_brackets = _normalise(re.sub(r"\s*\([^)]*\)\s*$", "", str(title or "")))
    return {name for name in (full, without_brackets, assistant_name(title, None)) if name}


def reminder_guidance(speaker_name: str, other_name: str, assistant: str = "") -> str:
    """Instructions for the main agent about reminders, for the person speaking now.

    `assistant` is this router's assistant name, which the tool wants back so the due
    reminder is spoken by the same assistant.
    """
    speaker, other = for_whom(speaker_name), for_whom(other_name)
    lines = [
        "Reminders:",
        f'- "Remind me/us … at/in <time>" is a reminder. Set it by calling the '
        f"{REMINDER_TOOL} tool (script.{REMINDER_TOOL}). That is the only way to set one.",
    ]
    if assistant:
        lines.append(f'- Pass assistant "{assistant}": that is your own assistant name.')
    if speaker:
        shown = speaker_name.strip()
        lines.append(
            f"- The person speaking now is {shown}. Every user message starts with the "
            f'name of the person who said it and a colon ("{shown}: …"). For "remind me", '
            f'for_whom is that name in lower case: "{speaker}" on this turn.'
        )
    else:
        lines.append(
            "- You do not know the name of the person speaking: ask who the reminder is for."
        )
    lines.append(
        f'- Use for_whom "{HOUSEHOLD}" for "remind us" or when it is for everyone.'
        + (f' Use for_whom "{other}" when it is for {other_name.strip()}.' if other else "")
    )
    lines += [
        '- "Remember that …" or "note that …" with no time to be alerted at is something to '
        f"remember, never a reminder. Do not call {REMINDER_TOOL} for it.",
        '- "Remember to <do something>" with no time is unclear. Ask whether they want a '
        "reminder, and when."
        + (
            f' "Remember to tell {other_name.strip()} …" is a message to remember, '
            "not a reminder."
            if other
            else ""
        ),
        f"- Say a reminder is set only after {REMINDER_TOOL} returned status ok in this turn. "
        "If it returned an error, tell the person what its message says. Never say a "
        "reminder is set without calling the tool.",
    ]
    return "\n".join(lines)
