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
CANCEL_TOOL = "cancel_reminder"
LIST_TOOL = "list_reminders"
HOUSEHOLD = "household"
DEFAULT_FAILURE_LINE = "I could not set that reminder. Please ask me again."
DEFAULT_CANCEL_FAILURE_LINE = "I could not cancel that reminder. Please ask me again."
DEFAULT_LIST_FAILURE_LINE = "I could not look up the reminders. Please ask me again."
# Spoken when a reply still says something was done, after a second try, with no tool call.
DEFAULT_ACTION_FAILURE_LINE = "I could not do that. Please ask me again."
# Said right after a reply the voice had already started on, when it turned out to be
# wrong: before the honest line, and before a second answer that says something else.
SPOKE_TOO_SOON = "Sorry, scratch that, I spoke too soon."
PUT_RIGHT = "Sorry, let me put that right."

ToolOutcome = Literal["none", "ok", "error"]

_REMIND_SOMEONE = re.compile(r"\bremind (\w+)\b", re.IGNORECASE)
_SET_REMINDER = re.compile(
    r"\b(?:set|add|create|make|schedule) (?:a|an|another|the|me a|us a) reminder\b",
    re.IGNORECASE,
)
# "Stop reminding me …", "cancel the bins reminder", "delete the reminder to …".
_CANCEL_REMINDER = re.compile(
    r"\b(?:stop|quit) reminding\b"
    r"|\b(?:cancel|delete|remove|clear|drop|turn off)\b[^.?!]{0,40}?\breminders?\b",
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
# A reply claims a reminder was cancelled: "I've cancelled your pills reminder", "that
# reminder is removed", "I'll stop reminding you".
_CANCEL_CLAIM = re.compile(
    r"\breminders?\b[^.?!]*\b(?:cancell?ed|removed|deleted|stopped|cleared|turned off)\b"
    r"|\b(?:cancell?ed|removed|deleted|stopped|cleared|turned off)\b[^.?!]*\breminders?\b"
    r"|\bstop reminding\b",
    re.IGNORECASE,
)


# A question about reminders that exist: "what reminders are there on Wednesday", "do we
# have a reminder for the car", "when is my bins reminder", "tell me about the car reminder".
_LIST_REMINDERS = re.compile(
    r"\b(?:what|which|when|list|show|any|how many|tell me|check"
    r"|(?:do|did|have|are|is) (?:i|we|you|there))\b"
    r"[^.?!]{0,60}?\b(?:reminders?|reminded)\b",
    re.IGNORECASE,
)
# A question about what the assistant can do with reminders is not about the calendar.
_ABOUT_ABILITY = re.compile(
    r"\bhow (?:do|does|can|could|to|would|should)\b|\bwhat can you\b|\bare you able\b"
    r"|\bcan you (?:set|make|create|add|cancel|delete|remove|do)\b",
    re.IGNORECASE,
)
# A reply that says it could not look: no statement about which reminders there are.
_COULD_NOT = re.compile(
    r"\b(?:can(?:'|no)?t|cannot|could(?: not|n't)|unable|sorry|failed|error)\b",
    re.IGNORECASE,
)


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
    if _SET_REMINDER.search(text) or _CANCEL_REMINDER.search(text):
        return True
    if is_unclear_remember_to(text):
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


# A reply says something was done: "I've turned on the lights", "Done, it's locked".
_ACTION_CLAIM = re.compile(
    r"\bI(?:'ve| have)?(?: now| just| already)? (?:turned|switched|set|started|stopped|paused|"
    r"resumed|added|removed|deleted|cancell?ed|created|scheduled|sent|played|opened|closed|"
    r"locked|unlocked|dimmed|changed|adjusted|activated|enabled|disabled|booked|ordered|"
    r"updated|triggered|muted|skipped|queued)\b"
    r"|\b(?:is|are|it's|they're) now (?:on|off|open|closed|locked|unlocked|playing|paused|set)\b"
    r"|^\s*(?:done|all done|consider it done)\b",
    re.IGNORECASE,
)
# What the main agent is told when it is asked once more (see conversation.py).
RETRY_NOTES: dict[str, str] = {
    "reminder": (
        f"Note from the system, not from the person: your last reply said a reminder was "
        f"set, but you did not call the {REMINDER_TOOL} tool, so nothing was set. Call "
        f"{REMINDER_TOOL} now with the details of the person's last request, then tell "
        "them the result in your usual way. Do not mention this note."
    ),
    "cancel": (
        f"Note from the system, not from the person: your last reply said a reminder was "
        f"cancelled, but you did not call the {CANCEL_TOOL} tool, so nothing was cancelled. "
        f"Call {CANCEL_TOOL} now for the person's last request, then tell them the result "
        "in your usual way. Do not mention this note."
    ),
    "list": (
        f"Note from the system, not from the person: your last reply said which reminders "
        f"there are, but you did not call the {LIST_TOOL} tool, so you cannot know. Call "
        f"{LIST_TOOL} now for the person's last request, then answer from what it returns "
        "in your usual way. Do not mention this note."
    ),
    "failed": (
        "Note from the system, not from the person: your last reply said something was "
        "done, but every tool call you made failed, so nothing happened. Read the errors "
        "the tools returned. If the request can be done another way (another value, "
        "another tool), call the tool again now and then tell the person the result in "
        "your usual way. If it cannot, tell them it did not work and why, without saying "
        "anything was done. Do not mention this note."
    ),
    "action": (
        "Note from the system, not from the person: your last reply said something was "
        "done, but you called no tool, so nothing happened. If the person's last request "
        "needs an action, call the right tool now and then tell them the result in your "
        "usual way. If it needs none, answer it again without saying anything was done. "
        "Do not mention this note."
    ),
}


def looks_like_action_claim(reply: str) -> bool:
    """Whether a reply says something was done. Questions, failures and offers do not."""
    reply = reply or ""
    if not reply.strip() or "?" in reply or _NEGATIVE.search(reply):
        return False
    return any(
        (claim := _ACTION_CLAIM.search(sentence)) is not None
        and not _HYPOTHETICAL.search(sentence[: claim.start()])
        for sentence in _SENTENCE_END.split(reply)
    )


def used_a_tool(entries: Iterable[Any]) -> bool:
    """Whether any tool was called in these chat log entries of one turn."""
    return any(
        getattr(entry, "tool_calls", None) or getattr(entry, "tool_name", None)
        for entry in entries
    )


def _failed(result: Any) -> bool:
    """Whether a tool result says the call failed.

    Home Assistant marks a call it refused (ToolResult.error, or an "error" key); an
    intent answers with response_type error; a script of ours with status error.
    """
    if getattr(result, "error", False):
        return True
    data = getattr(result, "data", result)
    if not isinstance(data, Mapping):
        return False
    return "error" in data or data.get("response_type") == "error" or _status(data) == "error"


def a_tool_worked(entries: Iterable[Any]) -> bool:
    """Whether any tool call in these chat log entries of one turn got a result that is
    not a failure. A call without a result, or refused by Home Assistant, did nothing."""
    return any(
        getattr(entry, "tool_name", None) and not _failed(getattr(entry, "result", None))
        for entry in entries
        if getattr(entry, "role", "") == "tool_result" or hasattr(entry, "tool_call_id")
    )


def is_cancel_turn(text: str) -> bool:
    """Whether the person asked to cancel a reminder ("stop reminding me …")."""
    return bool(_CANCEL_REMINDER.search(text or ""))


def looks_like_cancellation(reply: str) -> bool:
    """Whether a reply claims a reminder was cancelled. Questions and failures do not."""
    reply = reply or ""
    if not reply.strip() or "?" in reply or _NEGATIVE.search(reply):
        return False
    return any(
        (claim := _CANCEL_CLAIM.search(sentence)) is not None
        and not _HYPOTHETICAL.search(sentence[: claim.start()])
        for sentence in _SENTENCE_END.split(reply)
    )


def is_list_turn(text: str) -> bool:
    """Whether the person asked which reminders there are, not to set or cancel one."""
    text = text or ""
    if is_reminder_request(text) or is_cancel_turn(text) or _REMEMBER_THAT.search(text):
        return False
    if _ABOUT_ABILITY.search(text):
        return False
    return bool(_LIST_REMINDERS.search(text))


def looks_like_listing(reply: str) -> bool:
    """Whether a reply to such a question says which reminders there are, or that none are.

    A question back and "I could not look that up" do not. "You have no reminders" does:
    it is a statement about the calendar like any other.
    """
    reply = reply or ""
    return bool(reply.strip()) and "?" not in reply and not _COULD_NOT.search(reply)


def needs_honest_cancel_failure(reply: str, outcome: ToolOutcome) -> bool:
    """Whether a reply confirms a cancellation that cancel_reminder did not make."""
    return outcome != "ok" and looks_like_cancellation(reply)


def reminder_tool_outcome(entries: Iterable[Any], tool: str = REMINDER_TOOL) -> ToolOutcome:
    """What happened to the tool (create_reminder unless told) in one turn's chat log.

    "ok" when a call returned status ok, "error" when it was called and none did,
    "none" when it was never called.
    """
    called = False
    for entry in entries:
        for call in getattr(entry, "tool_calls", None) or []:
            if str(getattr(call, "tool_name", "")).endswith(tool):
                called = True
        if str(getattr(entry, "tool_name", "")).endswith(tool):
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


def plain_name(name: object) -> str:
    """A name as it is compared: one space between words, case folded, "" for none."""
    return " ".join(str(name or "").split()).casefold()


def for_whom(name: str) -> str:
    """The create_reminder value for a person: their name from the settings, lower case."""
    return plain_name(name)


def _normalise(name: str) -> str:
    return plain_name(name)


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


def reminder_guidance(
    speaker_name: str, other_name: str, assistant: str = "", cancel: bool = True
) -> str:
    """Instructions for the main agent about reminders, for the person speaking now.

    `assistant` is this router's assistant name, which the tool wants back so the due
    reminder is spoken by the same assistant. Without `cancel`, the agent is told it
    cannot cancel reminders.
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
        f"- A repeating reminder (\"every day at 8\", \"every weekday\", \"every other "
        f"Friday\", \"on the 1st of every month\") is set with the same {REMINDER_TOOL} "
        "call plus repeat: daily, weekdays, weekends, weekly, monthly or yearly. Give "
        "at_time for the time of day, and on_day for when it starts if not the next time "
        "the clock shows at_time. With weekly, repeat_on lists the weekdays, for example "
        "\"monday, thursday\". repeat_every 2 means every other day, week, month or "
        "year. To end it, repeat_times is how many times, or repeat_until the last day "
        "(MM-DD); leave both out for no end. Never work out the dates yourself.",
        f"- Say a reminder is set only after {REMINDER_TOOL} returned status ok in this turn. "
        "If it returned an error, tell the person what its message says. Never say a "
        "reminder is set without calling the tool.",
    ]
    lines += [
        f"- Questions about reminders that exist (\"what reminders are there on "
        f"Wednesday\", \"do we have one for charging the car\") are answered by calling "
        f"the {LIST_TOOL} tool (script.{LIST_TOOL}), never from memory of the conversation. "
        "Pass keywords for what it is about, on_day for a day, and until_day too for a "
        "stretch of days; leave them out to get every reminder coming up, and pick what "
        "is relevant yourself. Report a reminder's settings exactly as the tool returns "
        "them. If it returns none, say there are none.",
        "- The tool shows the reminders of the person speaking and the household's."
        + (
            f" {other_name.strip()}'s reminders are private to them: say you cannot look "
            "those up."
            if other
            else ""
        ),
    ]
    if cancel:
        lines += [
            f"- You can cancel reminders. \"Stop reminding me …\" or \"cancel the … "
            f"reminder\" cancels one: call the {CANCEL_TOOL} tool (script.{CANCEL_TOOL}) "
            "with the reminder's title, or a word from it, and for_whom. A repeating "
            "reminder is cancelled as a whole. If it says several reminders match, ask "
            "which one and call it again with that title."
            + (
                f" Only the person speaking's own reminders and the household's can be "
                f"cancelled: {other_name.strip()}'s cannot, so say that and do not try."
                if other
                else ""
            ),
            f"- Say a reminder is cancelled only after {CANCEL_TOOL} returned status ok in "
            "this turn. If it returned an error, tell the person what its message says.",
        ]
    else:
        lines.append(
            "- You cannot cancel reminders. When asked to, say so, and that it can be "
            "removed in the reminder calendar in Home Assistant."
        )
    return "\n".join(lines)
