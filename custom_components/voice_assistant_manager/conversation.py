# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conversation entity that adds a per-speaker persona and delegates to another agent.

Settings come from the settings screen of the entry (its options). An entry
that has no saved settings yet uses the older "who is speaking" script named
in its data, if there is one.

Either source produces the same lookup result:
  persona_for_this_message  prompt text appended for this reply
  rule                      extra prompt text
  label                     optional name of the speaker. The user message is
                            stored in the shared history as "<label>: <text>"
  fixed_replies             optional list of lines. If not empty, one is spoken,
                            the real agent (and its tools) is never called and
                            the exchange is kept out of the shared history
  reply_agent               optional entity id of a separate agent that has no
                            tools. If set, that agent writes the reply from
                            persona_for_this_message alone, in a conversation
                            of its own. The real agent (and its tools) is never
                            called and the exchange is kept out of the shared
                            history. If that agent fails or is slow, one line
                            from fixed_replies is spoken instead.
"""

import asyncio
import dataclasses
import enum
import logging
import random
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import (
    DATA_CHAT_LOGS,
    UserContent,
    current_chat_log,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.util import dt as dt_util
from homeassistant.util import ulid

from . import avatar_stream, emotion
from .announce import safe_toolless_agent
from .chat_show import GUEST, get_manager, is_stop_phrase
from .const import REPLY_AGENT_TIMEOUT
from .wake_handoff import async_hand_off
from .favorites import async_get_favorites, favorites_on, find, lists_for, parse_play
from .favorites_context import Asker, forget, remember
from .favorites_play import (
    PLAYING_LINE,
    PLAYING_ON_LINE,
    PlayRefused,
    async_play,
    default_player,
    player_name,
    resolve_player,
)
from .memory import (
    REMEMBER_RULE,
    REMEMBER_RULE_REMINDERS,
    Exchange,
    MemorySettings,
    Person,
    active_memory_settings,
    async_run_extraction,
    entry_key,
    get_loaded_memory,
    has_memory_keyword,
    global_key,
    memory_block,
    router_agent_ids,
    shared_list_ok,
    should_extract,
)
from .panel import speaker_overrides
from .reminders import (
    CANCEL_TOOL,
    DEFAULT_CANCEL_FAILURE_LINE,
    DEFAULT_FAILURE_LINE,
    assistant_name,
    is_cancel_turn,
    is_reminder_exchange,
    is_reminder_turn,
    needs_honest_cancel_failure,
    needs_honest_failure,
    reminder_guidance,
    reminder_tool_outcome,
)
from .avatar import esphome_entity
from .speaker import (
    CONVERSATION_LIFETIME,
    Remembered,
    Role,
    follow_up_role,
    resolve_speaker,
)

_LOGGER = logging.getLogger(__name__)
SCRIPT_TIMEOUT = 8
LAST_RESORT_REPLY = "Sorry, I can't help you."
# Spoken when the main agent fails (its provider is down, say), unless set under Advanced.
DEFAULT_AGENT_DOWN_LINE = "Sorry, I can't think right now. Please try again in a moment."
CHAT_STOPPED_LINE = "Okay."
SETTINGS_KEY = {"owner": "owner", "wife": "partner"}
# How long after a satellite starts listening its voice match can still arrive, unless set
# under Advanced. LANovo publishes it 4.6-5.5 s after the wake word, whatever is said.
DEFAULT_MATCH_WITHIN = 6.0
# The text sensor a satellite reports on each new turn's wake word (LANovo, EchoLocal).
WAKE_WORD_SENSOR = "Last wake word"
OTHER_PERSON = {"owner": "wife", "wife": "owner"}
# How the settings screen calls each person, for one who has no name.
PERSON_NUMBER = {"owner": "Person 1", "wife": "Person 2"}


class _NoPlayer(enum.Enum):
    """A favorite's name matched, but its player was not found.

    Only the main agent may answer then (it can ask which speaker): a built-in command
    could play something else of the same name.
    """

    FOUND = enum.auto()


NO_PLAYER = _NoPlayer.FOUND


# How many of the person's earlier messages count when deciding whether a conversation
# is about a reminder: enough for "remind me to …" → "what time?" → "at five" → "yes".
EARLIER_TURNS = 2


def _earlier_user_texts(log, log_start, count):
    """The person's last `count` messages before the current one, newest first."""
    texts = []
    # The current message is the last entry before the turn started.
    for entry in reversed(log.content[: max(log_start - 1, 0)]):
        if entry.role == "user" and entry.content:
            texts.append(str(entry.content))
            if len(texts) == count:
                break
    return texts


def _lines(text):
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _leave_to_main_agent(result):
    """Built-in commands that the main agent answers better: state questions and music searches."""
    return result.intent.name in ("HassGetState", "HassMediaSearchAndPlay")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    async_add_entities([PersonaRouter(entry)])



def _silent_error(result: conversation.ConversationResult) -> bool:
    """An error answer with nothing to say, which a satellite would end in silence."""
    response = result.response
    if response.response_type is not intent.IntentResponseType.ERROR:
        return False
    return not str((response.speech.get("plain") or {}).get("speech") or "").strip()

class PersonaRouter(conversation.ConversationEntity):
    _attr_supports_streaming = True

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_name = entry.title
        self._attr_unique_id = entry.entry_id
        self._last_fixed: str | None = None
        # The unsafe agent last logged for each use ("refusal", "memory"), to log it once.
        self._unsafe_logged: dict[str, str] = {}
        # conversation_id -> the person of its last voice turn (speaker.follow_up_role).
        self._spoken_by: dict[str, Remembered] = {}

    @property
    def supported_languages(self):
        return MATCH_ALL

    def _main_agent(self):
        saved = self._entry.options.get("agents")
        chosen = saved.get("agent") if isinstance(saved, Mapping) else None
        return chosen or self._entry.data["agent"]

    def _turn_log(self, user_input):
        """Return the chat log of the running turn if its last entry is this user message."""
        log = current_chat_log.get()
        if log is None or log.conversation_id != user_input.conversation_id:
            log = self.hass.data.get(DATA_CHAT_LOGS, {}).get(user_input.conversation_id or "")
        if log is None or not log.turn_active or len(log.content) < 2:
            return None
        last = log.content[-1]
        if last.role != "user" or last.content != user_input.text:
            return None
        return log

    def _speak(
        self, user_input: conversation.ConversationInput, text: str
    ) -> conversation.ConversationResult:
        """Speak a line without calling the real agent and keep the exchange out of the shared history."""
        try:
            log = self._turn_log(user_input)
            if log is not None:
                del log.content[-1]
                log.turn_start = min(log.turn_start, len(log.content))
        except Exception:  # noqa: BLE001 - tidying the history must never block the reply
            _LOGGER.exception("Could not keep the reply out of the history")
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(text)
        return conversation.ConversationResult(
            response=response,
            conversation_id=user_input.conversation_id or ulid.ulid_now(),
            continue_conversation=False,
        )

    def _fixed_reply(self, user_input, replies):
        """Answer with one of the fixed lines, without calling the real agent."""
        choices = [r for r in replies if r != self._last_fixed] or replies
        text = random.choice(choices)
        self._last_fixed = text
        return self._speak(user_input, text)

    async def _reply_agent_text(self, user_input, agent_id, persona):
        """Let a separate agent without tools write the reply, in a conversation of its own."""
        try:
            async with asyncio.timeout(REPLY_AGENT_TIMEOUT):
                result = await conversation.async_converse(
                    self.hass,
                    text=user_input.text,
                    conversation_id=None,
                    context=user_input.context,
                    language=user_input.language,
                    agent_id=agent_id,
                    extra_system_prompt=persona or None,
                )
            response = result.response
            if response.response_type == intent.IntentResponseType.ERROR:
                _LOGGER.warning("Reply agent returned an error, using a fixed line")
                return None
            speech = (response.speech.get("plain") or {}).get("speech") or ""
            return str(speech).strip() or None
        except Exception:  # noqa: BLE001 - a failed reply agent must never block the reply
            _LOGGER.exception("Reply agent failed, using a fixed line")
            return None

    def _labelled_text(self, user_input, label) -> str:
        """Store the user message as '<label>: <text>' and return that text."""
        text = user_input.text
        label = str(label or "").strip()
        if not label:
            return text
        labelled = f"{label}: {text.strip()}"
        try:
            log = self._turn_log(user_input)
            if log is not None:
                old = log.content[-1]
                log.content[-1] = UserContent(content=labelled, attachments=old.attachments)
        except Exception:  # noqa: BLE001 - labelling must never block the reply
            _LOGGER.exception("Could not label the user message")
            return text
        return labelled

    async def _helper_state(self, device_id, advanced):
        """The voice_speaker helper's state on the satellite, or None if it has none.

        While the helper says pending it waits for the voice match, until "the match
        arrives within" seconds after pending was last written, which is when the
        satellite started listening. A satellite matches on a fixed stretch of audio after
        the wake word, so the match lands a fixed time after it however long the person
        speaks: after a long request it is already there, and nothing is waited for.

        The last report counts, not the last change: a helper a follow-up left on pending
        is written pending again at the next wake word, which changes nothing but the
        report time.
        """
        helper = f"input_text.voice_speaker_{device_id or ''}"
        state = self.hass.states.get(helper)
        if state is None:
            return None
        try:
            within = float(advanced.get("match_within", DEFAULT_MATCH_WITHIN))
        except (TypeError, ValueError):
            within = DEFAULT_MATCH_WITHIN
        # A cap on the steps too, so a clock that stands still can never keep it waiting.
        for _ in range(max(int(within * 10) + 1, 0)):
            if state.state != "pending":
                break
            deadline = state.last_reported + timedelta(seconds=within)
            remaining = (deadline - dt_util.utcnow()).total_seconds()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.1, remaining))
            state = self.hass.states.get(helper)
            if state is None:
                return None
        return state

    async def _who_is_speaking(self, user_input, settings) -> tuple[Role, bool]:
        """Return the role and whether a voice match decided it."""
        advanced = settings.get("advanced") or {}
        user_id = user_input.context.user_id if user_input.context else None
        user_map: list[tuple[Role, str | None]] = [
            (role, (settings.get(SETTINGS_KEY[role]) or {}).get("user"))
            for role in ("owner", "wife")
        ]
        no_helper_role = advanced.get("no_helper_role")
        if user_id and await self._is_system_user(user_id):
            # Home Assistant's own hidden users (the Voice over IP phone, add-ons) cannot be
            # chosen for a person, so they count as a device without speaker recognition.
            # A helper is not trusted for them either: an add-on can name any device.
            return resolve_speaker(None, None, user_map, no_helper_role)
        # A request with a user goes by that user alone (see resolve_speaker), so the
        # helper of a device it names is neither read nor waited for.
        if user_id:
            return resolve_speaker(None, user_id, user_map, no_helper_role)
        return await self._voice_speaker(user_input, advanced, user_map, no_helper_role)

    async def _voice_speaker(self, user_input, advanced, user_map, no_helper_role):
        """The role for a request without a user, by the satellite's voice match.

        A follow-up in the same conversation keeps its person unless a new voice match
        says otherwise (speaker.follow_up_role), so it never depends on the satellite's
        timing. Every turn waits for the match until its deadline (_helper_state), so a new wake
        word's match, an unknown voice's too, still decides.
        """
        now = dt_util.utcnow()
        conversation_id = user_input.conversation_id
        device_id = user_input.device_id
        self._spoken_by = {
            key: seen
            for key, seen in self._spoken_by.items()
            if now - seen.at <= CONVERSATION_LIFETIME
        }
        remembered = self._spoken_by.get(conversation_id or "")
        if remembered is not None and remembered.device_id != device_id:
            remembered = None
        woke_since = self._woke_since(device_id, remembered)
        state = await self._helper_state(device_id, advanced)
        if state is None:
            return resolve_speaker(None, None, user_map, no_helper_role)
        kept = follow_up_role(
            remembered, device_id, state.state, state.last_changed, now, woke_since
        )
        if kept is not None:
            role, matched, decided_by = kept, True, remembered.decided_by
        else:
            role, matched = resolve_speaker(state.state, None, user_map, no_helper_role)
            decided_by = state.last_changed
        if conversation_id and device_id:
            if role in ("owner", "wife"):
                self._spoken_by[conversation_id] = Remembered(role, device_id, decided_by, now)
            else:
                # An unknown voice ends it: a later turn needs a voice match of its own.
                self._spoken_by.pop(conversation_id, None)
        return role, matched

    def _woke_since(self, device_id, remembered: Remembered | None) -> bool | None:
        """True when the satellite reported a wake word since the remembered turn, else None.

        A report proves a new turn (wake word, Wake button, a listen after an announcement).
        No report proves nothing: Home Assistant drops an update with an unchanged value,
        so a second wake word with the same phrase never shows.
        """
        if remembered is None or not device_id:
            return None
        registry = er.async_get(self.hass)
        for entity in er.async_entries_for_device(registry, device_id):
            if entity.domain != "sensor":
                continue
            named = esphome_entity(entity.unique_id)
            if (named is None or named[1] != WAKE_WORD_SENSOR) and not entity.entity_id.endswith(
                "_last_wake_word"
            ):
                continue
            state = self.hass.states.get(entity.entity_id)
            if state is None or state.state in ("unavailable", "unknown"):
                return None
            return True if state.last_reported > remembered.at else None
        return None

    async def _is_system_user(self, user_id: str) -> bool:
        """True for a user Home Assistant created for itself, which no one logs in as."""
        user = await self.hass.auth.async_get_user(user_id)
        return user is not None and user.system_generated

    def _safe_agent(self, use: str, agent: str) -> str | None:
        """The agent if it is safe to give text from someone unknown, else None.

        An unsafe one is logged once per agent, not on every request.
        """
        safe = safe_toolless_agent(self.hass, self._entry, agent)
        if safe is not None:
            self._unsafe_logged.pop(use, None)
        elif self._unsafe_logged.get(use) != agent:
            self._unsafe_logged[use] = agent
            if use == "refusal":
                _LOGGER.error(
                    "The agent that writes refusals, %s, is missing, is an assistant or can "
                    "control the home, so unknown voices get fixed lines",
                    agent,
                )
            else:
                _LOGGER.warning(
                    "The memory agent %s is missing, is an assistant or can control the "
                    "home, so nothing new is saved; remembered facts are still used",
                    agent,
                )
        return safe

    def _memory_settings(self) -> MemorySettings | None:
        """Memory settings of this router, or None when memory is not active."""
        return active_memory_settings(
            self._entry.options,
            (self._main_agent(), self.entity_id, *router_agent_ids(self.hass)),
        )

    def _memory_saves(self, settings: MemorySettings) -> bool:
        """Whether the memory agent may be asked what to save. Remembered facts stay in use."""
        return self._safe_agent("memory", settings.agent) is not None

    def _person_name(self, person) -> str:
        section = self._entry.options.get(SETTINGS_KEY[person]) or {}
        return str(section.get("name") or "").strip()

    def _memory_block(self, who, name, settings: MemorySettings) -> str:
        """What this assistant remembers about one person. Reads memory only, no I/O."""
        try:
            memory = get_loaded_memory(self.hass)
            if memory is None:
                return ""

            def lists(person, shareable_only=False):
                found = [memory.items(entry_key(self._entry.entry_id, person))]
                # Only while the person is the same in every router using the shared lists.
                shared = settings.use_global and shared_list_ok(self.hass, person)
                found.append(memory.items(global_key(person)) if shared else [])
                if shareable_only:
                    found = [[i for i in items if not i.get("private")] for items in found]
                return found

            blocks = [memory_block(name, *lists(who), max_items=settings.max_items)]
            if settings.shared:
                # What the other named person told this assistant, unless they asked
                # to keep it private.
                other = OTHER_PERSON[who]
                blocks.append(
                    memory_block(
                        self._person_name(other),
                        *lists(other, shareable_only=True),
                        max_items=settings.max_items,
                        other=True,
                    )
                )
            return "\n\n".join(block for block in blocks if block)
        except Exception:  # noqa: BLE001 - memory must never cost the person their persona
            _LOGGER.exception("Could not read memory, answering without it")
            return ""

    def _reminder_settings(self) -> dict:
        """The Reminders section of the settings, or {} when reminders are off."""
        section = self._entry.options.get("reminders") or {}
        return dict(section) if section.get("enabled") else {}

    def _reminder_guidance(self, who, name) -> str:
        """Reminder rules for the main agent, or "" when reminders are off."""
        if not self._reminder_settings():
            return ""
        assistant = assistant_name(self._entry.title, self._reminder_settings().get("assistant"))
        return reminder_guidance(
            name,
            self._person_name(OTHER_PERSON[who]),
            assistant,
            cancel=bool(self._reminder_settings().get("cancel", True)),
        )

    def _guard_reminder(self, user_input, result, log, log_start):
        """Check a finished turn for reminder trouble.

        Returns the result to give back and whether the turn was about a reminder, so
        memory leaves it alone. When the turn's tool calls can be seen and the reply
        confirms a reminder that create_reminder did not set, the reply is replaced by
        an honest line, in the result and in the history.
        """
        try:
            # A log the main agent did not write to shows nothing about its tools.
            visible = (
                log is not None and log_start is not None and len(log.content) > log_start
            )
            turn = log.content[log_start:] if visible else []
            outcome = reminder_tool_outcome(turn)
            cancelled = reminder_tool_outcome(turn, CANCEL_TOOL)
            about_reminder = (
                outcome != "none" or cancelled != "none" or is_reminder_turn(user_input.text)
            )
            if not visible:
                return result, about_reminder
            reply = str((result.response.speech.get("plain") or {}).get("speech") or "")
            if cancelled != "none" or is_cancel_turn(user_input.text):
                # A cancellation: its reply mentions the reminder and when it was set for,
                # which is no claim that one was set.
                if not needs_honest_cancel_failure(reply, cancelled):
                    return result, about_reminder
                _LOGGER.warning(
                    "The main agent confirmed a cancellation it did not make, correcting it"
                )
                return self._honest_reply(
                    user_input, result, log, log_start, DEFAULT_CANCEL_FAILURE_LINE
                ), True
            # Only an exchange about a reminder can falsely confirm one. A reply to "what
            # can you do?" that mentions reminders is not a claim that one was set.
            recent = [user_input.text, *_earlier_user_texts(log, log_start, EARLIER_TURNS)]
            if outcome == "none" and not is_reminder_exchange(recent):
                return result, about_reminder
            passing_on = has_memory_keyword(user_input.text)
            if not needs_honest_failure(reply, outcome, passing_on):
                return result, about_reminder
            _LOGGER.warning("The main agent confirmed a reminder it did not set, correcting it")
            line = str(self._reminder_settings().get("failure_line") or "").strip()
            line = line or DEFAULT_FAILURE_LINE
            return self._honest_reply(user_input, result, log, log_start, line), True
        except Exception:  # noqa: BLE001 - the check must never cost the person their reply
            _LOGGER.exception("Could not check the reminder reply")
            return result, False

    @staticmethod
    def _honest_reply(user_input, result, log, log_start, line):
        """The result with `line` as its reply, in the history too."""
        for index in range(len(log.content) - 1, log_start - 1, -1):
            entry = log.content[index]
            if entry.role == "assistant" and entry.content:
                log.content[index] = dataclasses.replace(entry, content=line)
                break
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(line)
        return conversation.ConversationResult(
            response=response,
            conversation_id=result.conversation_id,
            continue_conversation=False,
        )

    def _start_memory_pass(self, user_input, resp, result) -> None:
        """Let the memory agent look at a finished turn, in the background.

        Only for a recognised person whose turn the main agent answered. The reply
        never waits for this.
        """
        try:
            if not resp or resp.get("who") not in ("owner", "wife"):
                return
            settings = self._memory_settings()
            memory = get_loaded_memory(self.hass)
            if settings is None or memory is None or not self._memory_saves(settings):
                return
            if result.response.response_type == intent.IntentResponseType.ERROR:
                return
            if not should_extract(user_input.text, auto_save=settings.auto_save):
                return
            exchange = Exchange(
                entry_id=self._entry.entry_id,
                person=resp["who"],
                name=str(resp.get("label") or ""),
                other_name=self._person_name(OTHER_PERSON[resp["who"]]),
                user_text=user_input.text,
                reply_text=emotion.split_tag(
                    str((result.response.speech.get("plain") or {}).get("speech") or "")
                )[1],
                language=user_input.language,
                context=user_input.context,
            )
            # Tied to the entry, so the pass is cancelled when the router is removed.
            self._entry.async_create_background_task(
                self.hass,
                async_run_extraction(self.hass, memory, settings, exchange),
                name=f"{self.entity_id} memory pass",
            )
        except Exception:  # noqa: BLE001 - memory must never block the reply
            _LOGGER.exception("Could not start the memory pass")

    def _last_score(self, device_id):
        """Voice match score of the satellite's last request, if it reports one."""
        if not device_id:
            return None
        registry = er.async_get(self.hass)
        for entity in er.async_entries_for_device(registry, device_id):
            if entity.entity_id.endswith("_last_speaker_score"):
                state = self.hass.states.get(entity.entity_id)
                try:
                    return float(state.state)
                except (AttributeError, TypeError, ValueError):
                    return None
        return None

    async def _lookup_from_settings(self, user_input, settings):
        """Work out who is speaking and what to do with them, from the settings screen."""
        advanced = settings.get("advanced") or {}
        who, matched = await self._who_is_speaking(user_input, settings)
        rule = str(advanced.get("rule") or "").strip()

        if who != "stranger":
            person = settings.get(SETTINGS_KEY[who]) or {}
            name = str(person.get("name") or "").strip()
            memory = ""
            memory_settings = self._memory_settings()
            if memory_settings is not None:
                memory = self._memory_block(who, name, memory_settings)
                # The rule promises a save, so it is only given while the memory agent is safe.
                if self._memory_saves(memory_settings):
                    timed = REMEMBER_RULE_REMINDERS if self._reminder_settings() else ""
                    rule = "\n".join(part for part in (rule, REMEMBER_RULE, timed) if part)
            return {
                "who": who,
                "persona_for_this_message": str(person.get("persona") or "").strip(),
                "memory": memory,
                "rule": rule,
                "reminders": self._reminder_guidance(who, name),
                "label": name,
                "fixed_replies": [],
                "reply_agent": "",
            }

        strangers = settings.get("strangers") or {}
        persona = str(strangers.get("persona") or "").strip()
        lines = _lines(strangers.get("fixed_replies"))
        near = settings.get("near_miss") or {}
        if matched and near.get("enabled"):
            score = self._last_score(user_input.device_id)
            try:
                lowest = float(near.get("min_score", 0.5))
            except (TypeError, ValueError):
                lowest = 0.5
            if score is not None and score >= lowest:
                who = "near_miss"
                persona = str(near.get("persona") or "").strip() or persona
                lines = _lines(near.get("fixed_replies")) or lines
        reply_agent = ""
        if strangers.get("mode", "ai") == "ai":
            reply_agent = str(strangers.get("reply_agent") or "").strip()
        return {
            "who": who,
            "persona_for_this_message": persona,
            "rule": rule,
            "label": "",
            # Never empty for an unknown voice, so the real agent stays out of reach.
            "fixed_replies": lines or [LAST_RESORT_REPLY],
            "reply_agent": reply_agent,
        }

    async def _try_built_in(self, user_input, log, local):
        """Run the request through the built-in commands, using the given history."""
        intent_filter = _leave_to_main_agent if local.get("leave_questions", True) else None
        response = await conversation.async_handle_intents(
            self.hass, user_input, log, intent_filter=intent_filter
        )
        if response is None:
            _LOGGER.debug(f"No built-in command for: {user_input.text}")
            return None
        _LOGGER.debug(f"Built-in command handled: {user_input.text}")
        speech = (response.speech.get("plain") or {}).get("speech") or ""
        log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=conversation.HOME_ASSISTANT_AGENT, content=speech)
        )
        return conversation.ConversationResult(response=response, conversation_id=log.conversation_id)

    async def _in_turn_log(self, user_input, log, text, answer):
        """Run `answer` with the turn's chat log, opening one when no voice assistant did.

        `answer` takes the log and returns a result, or None when it did not answer. In a
        log opened here, an answered request is stored as `text`, the labelled message.
        """
        if log is not None:
            return await answer(log)
        from homeassistant.components.conversation.chat_log import async_get_chat_log
        from homeassistant.helpers.chat_session import async_get_chat_session

        with (
            async_get_chat_session(
                self.hass, user_input.conversation_id, context=user_input.context
            ) as session,
            async_get_chat_log(self.hass, session, user_input) as own_log,
        ):
            place = len(own_log.content) - 1
            handled = await answer(own_log)
            if handled is not None and text != user_input.text:
                old = own_log.content[place]
                if old.role == "user":
                    own_log.content[place] = UserContent(content=text, attachments=old.attachments)
            return handled

    async def _local_command(self, user_input, log, local, text):
        """Let Home Assistant's built-in commands handle the request, if one matches exactly.

        Only called for a recognised speaker. Returns None when nothing matched,
        so the main agent answers instead.
        """
        if not local.get("enabled", True):
            return None
        try:
            return await self._in_turn_log(
                user_input,
                log,
                text,
                lambda turn_log: self._try_built_in(user_input, turn_log, local),
            )
        except Exception:  # noqa: BLE001 - the main agent can still answer
            _LOGGER.exception("Built-in command handling failed, asking the main agent")
            return None

    async def _favorite_reply(self, user_input, who: Person, log, text):
        """Play a favorite the request names exactly, and answer it, or None.

        None leaves the request to the built-in commands and the main agent: it is not
        `play <favorite>` or favorites are off here. An unexpected error is logged and also
        gives None. NO_PLAYER means a favorite matched but its player cannot be found: the
        request goes to the main agent alone, which can ask which one.
        """
        if not favorites_on(self._entry):
            return None
        try:
            found = await self._favorite_and_player(user_input, who)
            if found is None or found is NO_PLAYER:
                return found
            favorite, entity_id, named = found
            try:
                await async_play(self.hass, favorite, entity_id, user_input.context)
            except PlayRefused as refused:
                reply = refused.reply
            else:
                reply = (
                    PLAYING_ON_LINE.format(
                        name=favorite["name"], player=player_name(self.hass, entity_id)
                    )
                    if named
                    else PLAYING_LINE.format(name=favorite["name"])
                )
        except Exception:  # noqa: BLE001 - the request goes on as if it were no favorite
            _LOGGER.exception("Could not play a favorite, handling the request as usual")
            return None
        return await self._answer_in_history(user_input, log, text, reply)

    async def _favorite_and_player(self, user_input, who: Person):
        """(favorite, player entity id, whether the player was named), NO_PLAYER or None.

        The first reading of the request whose name is a favorite decides; when its player
        cannot be found, it is NO_PLAYER. None: no reading names a favorite.
        """
        pairs = parse_play(user_input.text)
        if not pairs:
            return None
        store = await async_get_favorites(self.hass)
        lists = [store.items(key) for key in lists_for(self.hass, who)]
        for name, player in pairs:
            favorite = find(lists, name)
            if favorite is None:
                continue
            try:
                if player is not None:
                    entity_id = resolve_player(self.hass, player)
                else:
                    entity_id = default_player(
                        self.hass, user_input.device_id, speaker_overrides(self.hass)
                    )
            except PlayRefused as refused:
                _LOGGER.debug("Favorite %r found, but no player: %s", name, refused.reply)
                return NO_PLAYER
            return favorite, entity_id, player is not None
        return None

    async def _answer_in_history(self, user_input, log, text, reply):
        """Speak `reply` and keep the exchange in the history, as a built-in command is.

        A failure to write the history is logged; the reply is still given.
        """

        async def answer(turn_log):
            turn_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(agent_id=self.entity_id, content=reply)
            )
            return self._spoken(user_input, reply, turn_log.conversation_id)

        try:
            return await self._in_turn_log(user_input, log, text, answer)
        except Exception:  # noqa: BLE001 - the favorite plays, so the reply must be given
            _LOGGER.exception("Could not keep the favorite in the history")
        return self._spoken(user_input, reply, user_input.conversation_id or ulid.ulid_now())

    @staticmethod
    def _spoken(user_input, reply: str, conversation_id: str):
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(reply)
        return conversation.ConversationResult(
            response=response, conversation_id=conversation_id
        )

    async def _lookup_from_script(self, user_input):
        """Older way: ask the 'who is speaking' script named in the entry data.

        The script is only given the device, and Home Assistant lets any logged-in user
        name any device in a typed request, so a request with a user is refused as an
        unknown voice instead: only saved settings can judge it by its user.
        """
        script = self._entry.data.get("script")
        if not script:
            return None
        if user_input.context and user_input.context.user_id:
            return {
                "who": "stranger",
                "persona_for_this_message": "",
                "rule": "",
                "label": "",
                "fixed_replies": [LAST_RESORT_REPLY],
                "reply_agent": "",
            }
        async with asyncio.timeout(SCRIPT_TIMEOUT):
            return await self.hass.services.async_call(
                "script",
                script.split(".", 1)[1],
                {"device": user_input.device_id or ""},
                blocking=True,
                return_response=True,
            )

    async def _chat_show_reply(self, user_input, resp):
        """Answer a request made while a chat show runs on this device, or None.

        Every turn from the chat's satellite belongs to the chat, whoever speaks: the
        assistants in a chat have no tools and treat what people say as conversation, so
        an unknown voice reaches nothing. This holds for a short grace after the chat
        ended too, since the satellite may still be listening for a reply to the last
        line. Only the chat that runs on this device is stopped or joined; a grace never
        touches a chat that has started elsewhere since.

        - A request the speaker sent after waking on its own chat line (the assistants
          say each other's names) gets an empty answer: it neither stops nor joins.
        - Anyone can stop the chat, and is answered "Okay.".
        - Other words join the chat with the person's name, or as a guest when the voice
          was not recognised. They get an empty answer, so the satellite says nothing and
          the next assistant answers them. A fragment, or a turn after the chat ended,
          joins nothing.

        Nothing here reaches the main agent, memory or the shared history.
        """
        manager = get_manager(self.hass)
        if not manager.is_running_on(user_input.device_id):
            return None
        if manager.is_self_wake(user_input.device_id):
            # The speaker woke on its own chat line and heard the rest of it: answer with
            # nothing (the voice pipeline skips text-to-speech then) and let the chat go on.
            return self._chat_turn(user_input, "self-wake ignored", "")
        live = manager.is_live_on(user_input.device_id)
        if is_stop_phrase(user_input.text):
            if live:
                await manager.async_stop("stopped")
            return self._chat_turn(user_input, "stop", CHAT_STOPPED_LINE)
        if not live:
            return self._chat_turn(user_input, "after the chat, ignored", "")
        if manager.is_fragment(user_input.text):
            return self._chat_turn(user_input, "fragment dropped", "")
        who = resp.get("who") if resp else None
        if who in ("owner", "wife"):
            manager.add_person_line(self._person_name(who) or "Someone", user_input.text)
            return self._chat_turn(user_input, "joined as a recognised person", "")
        manager.add_person_line(GUEST, user_input.text)
        return self._chat_turn(user_input, "joined as a guest", "")

    def _chat_turn(self, user_input, kind: str, reply: str):
        # What was said is personal and stays out of the log; how it was taken does not.
        _LOGGER.info("Chat show turn on device %s: %s", user_input.device_id, kind)
        return self._speak(user_input, reply)

    def _chat_show_fallback(self, user_input):
        """After an unexpected chat show error: refuse while a chat may run here, else None.

        When even that cannot be told, a chat may be running, so the request is refused:
        the main agent stays out of reach of a chat.
        """
        try:
            running = get_manager(self.hass).is_running_on(user_input.device_id)
        except Exception:  # noqa: BLE001 - fail closed, see above
            _LOGGER.exception("Could not tell whether a chat show runs here, refusing")
            running = True
        return self._speak(user_input, LAST_RESORT_REPLY) if running else None

    def _main_conversation_id(self, user_input):
        """The conversation for the main agent: the request's, unless a chat line is in it.

        A satellite that spoke a chat line with start_conversation keeps that conversation,
        with the line in its history, for later requests. The main agent then answers in a
        conversation of its own, so it never sees the line. When this cannot be told, it
        gets a new conversation: never one that may hold a chat line.
        """
        try:
            return get_manager(self.hass).main_conversation_id(user_input.conversation_id)
        except Exception:  # noqa: BLE001 - fail closed, see above
            _LOGGER.exception("Could not tell whether a chat line is in the conversation")
            return None

    def _after_clean_conversation(self, user_input, result):
        """After the main agent answered in a conversation of its own instead of this one.

        Remembers that conversation for the next request and returns its chat log and where
        this turn starts in it, for the reminder check. The result keeps the request's
        conversation, the one the caller goes on with.
        """
        try:
            get_manager(self.hass).remember_main_conversation(
                user_input.conversation_id, result.conversation_id
            )
        except Exception:  # noqa: BLE001 - only the next request starts afresh
            _LOGGER.exception("Could not remember the main agent's conversation")
        log = self.hass.data.get(DATA_CHAT_LOGS, {}).get(result.conversation_id or "")
        # The turn's first entry is the request; what the main agent added follows it.
        log_start = log.turn_start + 1 if log is not None else None
        if user_input.conversation_id is not None:
            result = dataclasses.replace(result, conversation_id=user_input.conversation_id)
        return result, log, log_start

    def _agent_down_line(self) -> str:
        advanced = self._entry.options.get("advanced") or {}
        return str(advanced.get("agent_down_line") or "").strip() or DEFAULT_AGENT_DOWN_LINE

    def _main_prompt(self, resp: dict[str, Any], extra: str | None) -> str:
        """The extra prompt for the main agent, never empty.

        Home Assistant keeps the previous turn's extra prompt when a turn gives none
        (ChatLog.async_provide_llm_data), so an empty one would let the last speaker's
        persona carry over. The first line always names the person speaking now.
        """
        name = str(resp.get("label") or "").strip()
        if not name:
            name = PERSON_NUMBER.get(str(resp.get("who") or ""), "someone without a name")
        parts = [
            f"The person speaking now is {name}.",
            resp.get("persona_for_this_message"),
            resp.get("memory"),
            resp.get("rule"),
            resp.get("reminders"),
            extra,
        ]
        return "\n\n".join(str(p) for p in parts if p)

    async def async_process(self, user_input):
        """Find out who is speaking, then let the right agent answer.

        When that cannot be worked out, the request is refused: the lookup failed, the
        router has neither saved settings nor a "who is speaking" script, or the script
        answered with nothing.

        A request that a wake word of another of these assistants started, but Home
        Assistant sent here, goes to that assistant (wake_handoff.py).
        """
        handed = await async_hand_off(self.hass, self.entity_id, user_input)
        if handed is not None:
            # The assistant it went to took its reply's emotion tag off itself.
            return handed
        log = current_chat_log.get()
        stream = None
        if log is not None and log.delta_listener is not None:
            # What a voice pipeline speaks and shows while the reply streams in.
            stream = emotion.StreamedReply(
                log.delta_listener, self._tell_emotion, emotion.accepted_emotions(self._entry)
            )
            log.delta_listener = stream
        # The avatar's voice asks which satellite this reply is for (avatar_stream.py).
        turn = avatar_stream.turn_started(
            self.hass, self._entry.entry_id, user_input.satellite_id
        )
        try:
            result = await self._async_answer(user_input)
        finally:
            avatar_stream.turn_ended(turn)
            if stream is not None:
                stream.finish()
                if log.delta_listener is stream:
                    log.delta_listener = stream.listener
        return self._without_tag(result, stream)

    def _tell_emotion(self, found: emotion.Emotion) -> None:
        emotion.send_emotion(self.hass, self._entry, found)

    def _without_tag(
        self, result: conversation.ConversationResult, stream: emotion.StreamedReply | None
    ) -> conversation.ConversationResult:
        """The result with the emotion tag off its speech; the avatar is told the emotion.

        A streamed reply told it as soon as it was known. Nothing is told for a reply with
        nothing to say: a chat show's silent turn must not change the face of its next line.
        The history keeps the tag, so the agent sees how it answered before.
        """
        try:
            plain = result.response.speech.get("plain") or {}
            said = str(plain.get("speech") or "")
            found, spoken = emotion.split_tag(said, emotion.accepted_emotions(self._entry))
            if spoken != said:
                result.response.async_set_speech(spoken, extra_data=plain.get("extra_data"))
            told = stream is not None and stream.emotion is not None
            if not told and spoken.strip():
                self._tell_emotion(found)
        except Exception:  # noqa: BLE001 - the reply always goes ahead
            _LOGGER.exception("Could not read the reply's emotion tag")
        return result

    async def _async_answer(self, user_input):
        resp = None
        try:
            settings = self._entry.options
            if settings:
                resp = await self._lookup_from_settings(user_input, settings)
            else:
                resp = await self._lookup_from_script(user_input)
                if not isinstance(resp, dict) or not any(resp.values()):
                    resp = None
        except Exception:  # noqa: BLE001 - a failed lookup is refused below
            resp = None
            _LOGGER.exception("Could not work out who is speaking, refusing")

        try:
            chat_reply = await self._chat_show_reply(user_input, resp)
        except Exception:  # noqa: BLE001 - a chat show bug must not block every request
            _LOGGER.exception("The chat show check failed")
            chat_reply = self._chat_show_fallback(user_input)
        if chat_reply is not None:
            return chat_reply
        if not resp:
            # Nobody could be identified: fail closed, the main agent stays out of reach.
            return self._speak(user_input, LAST_RESORT_REPLY)

        main_agent = self._main_agent()
        replies = resp.get("fixed_replies")
        if isinstance(replies, str):
            replies = [replies]
        if isinstance(replies, list):
            replies = [str(r).strip() for r in replies if str(r).strip()]
        else:
            replies = []

        reply_agent = str(resp.get("reply_agent") or "").strip()
        if reply_agent:
            # From here on the real agent, and with it every tool, is out of reach.
            written = None
            if self._safe_agent("refusal", reply_agent) is not None:
                written = await self._reply_agent_text(
                    user_input, reply_agent, resp.get("persona_for_this_message")
                )
            if written:
                return self._speak(user_input, written)
            return self._fixed_reply(user_input, replies or [LAST_RESORT_REPLY])

        if replies:
            return self._fixed_reply(user_input, replies)
        log = self._turn_log(user_input) or current_chat_log.get()
        text = self._labelled_text(user_input, resp.get("label"))
        who = resp.get("who")
        if who in ("owner", "wife"):
            played = await self._favorite_reply(user_input, who, log, text)
            if played is not None and played is not NO_PLAYER:
                return played
            if played is None:
                handled = await self._local_command(
                    user_input, log, self._entry.options.get("local") or {}, text
                )
                if handled is not None:
                    return handled

        conversation_id = self._main_conversation_id(user_input)
        log_start = len(log.content) if log is not None else None
        context_id = user_input.context.id if user_input.context else None
        # Only this request's own record is forgotten: a router whose main agent is this
        # one may have remembered its asker under the same context.
        remembered: str | None = None
        if context_id and who in ("owner", "wife"):
            # The favorites tools read who is asking from the request's context.
            remembered = context_id
            remember(
                self.hass, context_id, Asker(self._entry.entry_id, who, user_input.device_id)
            )
        try:
            result = await conversation.async_converse(
                self.hass,
                text=text,
                conversation_id=conversation_id,
                context=user_input.context,
                language=user_input.language,
                agent_id=main_agent,
                device_id=user_input.device_id,
                satellite_id=user_input.satellite_id,
                extra_system_prompt=self._main_prompt(resp, user_input.extra_system_prompt),
            )
        except Exception:  # noqa: BLE001 - the person hears a line, not a failed turn
            _LOGGER.exception("The main agent %s failed, answering with a short line", main_agent)
            return self._speak(user_input, self._agent_down_line())
        finally:
            if remembered:
                forget(self.hass, remembered)
        if _silent_error(result):
            _LOGGER.error(
                "The main agent %s answered with an error and no words, answering with a "
                "short line",
                main_agent,
            )
            return self._speak(user_input, self._agent_down_line())
        if conversation_id != user_input.conversation_id:
            result, log, log_start = self._after_clean_conversation(user_input, result)
        reminders = bool(resp.get("reminders"))
        if reminders:
            result, about_reminder = self._guard_reminder(user_input, result, log, log_start)
            if about_reminder:
                # A reminder lives in Home Assistant. Memory never sees the request.
                return result
        self._start_memory_pass(user_input, resp, result)
        return result
