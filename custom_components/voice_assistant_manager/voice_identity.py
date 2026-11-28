# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is speaking: a turn's person from its user or its satellite's voice match.

One VoiceIdentity per router (conversation.PersonaRouter). It keeps, between turns, who
spoke last in each conversation, whether the last reply asked a question, and which
satellites gave no voice match, so a missing match is refused rather than guessed.
"""

import asyncio
import dataclasses
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .avatar import esphome_entity
from .const import DEFAULT_MATCH_WITHIN, PERSON_SECTION, WAKE_WORD_SENSOR
from .listening import listening_since
from .speaker import (
    CONVERSATION_LIFETIME,
    FOLLOW_UP_WINDOW,
    UTTERANCE_ALLOWANCE,
    Remembered,
    Role,
    follow_up_role,
    resolve_speaker,
    verdict_is_current,
)

_LOGGER = logging.getLogger(__name__)
type Input = conversation.ConversationInput
type Result = conversation.ConversationResult
# The entry's options as the settings screen saves them.
type Settings = Mapping[str, Any]
SETTINGS_KEY = PERSON_SECTION


class VoiceIdentity:
    """Works out the person of each turn for one router, and remembers what that needs."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        # conversation_id -> the person of its last voice turn (speaker.follow_up_role).
        self._spoken_by: dict[str, Remembered] = {}
        # Satellites whose last voice turn got no voice match, to warn once per outage.
        self._no_match: set[str] = set()
        # Satellites that recognise speakers but have no helper, to warn once each.
        self._no_helper: set[str] = set()
        # conversation_id -> when its last reply asked the person a question.
        self._asked: dict[str, datetime] = {}

    def no_match_from(self, device_id: str | None) -> bool:
        """Whether the satellite's last voice turn got no voice match."""
        return device_id in self._no_match

    async def _await_match(
        self, helper: str, within: float, since: datetime | None, wait_when_old: bool
    ) -> State | None:
        """The voice_speaker helper's state once this turn's voice match is in, or the
        time for it is over. None if the helper is gone.

        A satellite matches on a fixed stretch of audio after the wake word, so the match
        lands a fixed time after it however long the person speaks: after a long request
        it is already there, and nothing is waited for. It is waited for until "the match
        arrives within" seconds after the turn started:

        - while the helper says pending, after pending was last written, which the
          tracking automation does when the satellite starts listening. The last report
          counts, not the last change: a helper a follow-up left on pending is written
          pending again at the next wake word, which changes nothing but the report time;
        - with `wait_when_old`, while the helper still holds a match from before `since`,
          when this turn started. The automation writes no pending for a wake word that
          follows a reply closely, and none at all when it is off.
        """
        state = self.hass.states.get(helper)
        # A cap on the steps too, so a clock that stands still can never keep it waiting.
        for _ in range(max(int(within * 10) + 1, 0)):
            if state is None or self._is_current(state, since):
                break
            if state.state == "pending":
                started = state.last_reported
                if wait_when_old and since is not None:
                    # A pending an earlier turn left behind says nothing about this one.
                    started = max(started, since)
            elif wait_when_old and since is not None:
                started = since
            else:
                break
            remaining = (started + timedelta(seconds=within) - dt_util.utcnow()).total_seconds()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.1, remaining))
            state = self.hass.states.get(helper)
        return state

    def _is_current(self, state: State, since: datetime | None) -> bool:
        """Whether the helper holds a voice match made for the turn that started at `since`."""
        return verdict_is_current(
            state.state, state.last_reported, since, dt_util.utcnow(), self._match_lifetime()
        )

    def _match_within(self) -> float:
        advanced = self._entry.options.get("advanced") or {}
        try:
            return float(advanced.get("match_within", DEFAULT_MATCH_WITHIN))
        except (TypeError, ValueError):
            return DEFAULT_MATCH_WITHIN

    def _match_lifetime(self) -> timedelta:
        """How long ago a voice match may have been written and still be this turn's."""
        return timedelta(seconds=self._match_within()) + UTTERANCE_ALLOWANCE

    def _follow_up_window(self) -> timedelta:
        advanced = self._entry.options.get("advanced") or {}
        try:
            seconds = float(advanced.get("follow_up_window", FOLLOW_UP_WINDOW.total_seconds()))
        except (TypeError, ValueError):
            return FOLLOW_UP_WINDOW
        return timedelta(seconds=max(seconds, 0))

    async def who_is_speaking(self, user_input: Input, settings: Settings) -> tuple[Role, bool]:
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

    async def _voice_speaker(
        self,
        user_input: Input,
        advanced: Settings,
        user_map: list[tuple[Role, str | None]],
        no_helper_role: str | None,
    ) -> tuple[Role, bool]:
        """The role for a request without a user, by the satellite's voice match.

        Only a match made for this turn counts (speaker.verdict_is_current): one written
        after the satellite started listening or reported its wake word, and not long ago.
        The helper keeps the last match until something writes over it, so without that an
        earlier person's match would be taken for whoever speaks next. A turn without a
        match of its own is refused as not told apart.

        The answer to a question the assistant asked keeps its person unless a new voice
        match says otherwise (speaker.follow_up_role): a satellite listens for it without
        a wake word and often makes no match. Every other turn waits for its match until
        the deadline (_await_match), so an unknown voice's match still decides.
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
        helper = f"input_text.voice_speaker_{device_id or ''}"
        if self.hass.states.get(helper) is None:
            return self._without_helper(device_id, helper, user_map, no_helper_role)
        woke = self._wake_reported(device_id)
        woke_since = True if remembered is not None and woke and woke > remembered.at else None
        window = self._follow_up_window()
        # This turn started when the satellite began to listen, or reported its wake word.
        started = [
            when
            for when in (listening_since(self.hass, device_id, user_input.satellite_id), woke)
            if when is not None
        ]
        since = max(started) if started else None
        # An answer to the assistant's question brings no match: it is not waited for.
        answering = (
            follow_up_role(remembered, device_id, None, None, now, woke_since, window)
            is not None
        )
        # Not even while the helper says pending: the automation writes that whenever the
        # satellite starts to listen, and for an answer nothing follows it.
        within = 0.0 if answering else self._match_within()
        state = await self._await_match(helper, within, since, not answering)
        if state is None:
            return resolve_speaker(None, None, user_map, no_helper_role)
        now = dt_util.utcnow()
        current = self._is_current(state, since)
        kept = follow_up_role(
            remembered, device_id, state.state, state.last_changed, now, woke_since, window
        )
        if kept is not None and remembered is not None:
            role, matched, decided_by = kept, True, remembered.decided_by
        elif current:
            role, matched = resolve_speaker(state.state, None, user_map, no_helper_role)
            decided_by = state.last_changed
            self._note_match(device_id, True)
        else:
            # Pending, no role at all, or a match from an earlier turn: nobody was told
            # apart, so nobody is taken for a person.
            role, matched, decided_by = "stranger", False, None
            self._note_match(device_id, False)
        if conversation_id and device_id:
            if role in ("owner", "wife"):
                self._spoken_by[conversation_id] = Remembered(role, device_id, decided_by, now)
            else:
                # An unknown voice ends it: a later turn needs a voice match of its own.
                self._spoken_by.pop(conversation_id, None)
        return role, matched

    def _without_helper(
        self,
        device_id: str | None,
        helper: str,
        user_map: list[tuple[Role, str | None]],
        no_helper_role: str | None,
    ) -> tuple[Role, bool]:
        """The role for a voice request from a device that has no voice_speaker helper.

        "A device without speaker recognition is" decides, but not for a satellite that
        does recognise speakers: its helper is missing (the satellite was paired again and
        has a new device id, or the helper was never made), and assuming a person for it
        would give that person's access to everyone who speaks to it.
        """
        if not device_id or not self._recognises_speakers(device_id):
            return resolve_speaker(None, None, user_map, no_helper_role)
        if device_id not in self._no_helper:
            self._no_helper.add(device_id)
            _LOGGER.warning(
                "The satellite on device %s recognises speakers but has no helper %s, so "
                "its requests are refused. Make the helper, or run the setup guide again "
                "if the satellite was paired again",
                device_id, helper,
            )
        self._no_match.add(device_id)
        return "stranger", False

    def _recognises_speakers(self, device_id: str) -> bool:
        """Whether the device has the sensors a tracking automation reads."""
        registry = er.async_get(self.hass)
        return any(
            entity.entity_id.endswith(("_last_speaker", "_last_speaker_score"))
            for entity in er.async_entries_for_device(registry, device_id)
        )

    def _note_match(self, device_id: str | None, arrived: bool) -> None:
        """Keep which satellites gave no voice match on their last turn; warn once each."""
        if not device_id:
            return
        if arrived:
            self._no_match.discard(device_id)
        elif device_id not in self._no_match:
            self._no_match.add(device_id)
            _LOGGER.warning(
                "No voice match arrived from the satellite on device %s, so its requests "
                "are refused. Check that its speaker recognition sensors are available "
                "and that the automation that copies them into its helper runs",
                device_id,
            )

    def _wake_reported(self, device_id: str | None) -> datetime | None:
        """When the satellite last reported a wake word, or None when it does not.

        A report proves a new turn (wake word, Wake button, a listen after an announcement).
        No report proves nothing: Home Assistant drops an update with an unchanged value,
        so a second wake word with the same phrase never shows.
        """
        if not device_id:
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
            return state.last_reported
        return None

    def note_reply(self, user_input: Input, result: Result) -> None:
        """Keep whether the reply asked a question: the next turn is then its answer, not
        a built-in command (_answers_a_question), and on a voice turn without a voice
        match it is that person's (speaker.follow_up_role)."""
        key = user_input.conversation_id or ""
        now = dt_util.utcnow()
        self._asked = {
            asked: when
            for asked, when in self._asked.items()
            if asked != key and now - when <= CONVERSATION_LIFETIME
        }
        if key and result.continue_conversation:
            self._asked[key] = now
        seen = self._spoken_by.get(key)
        if seen is not None and seen.device_id == user_input.device_id:
            # The time to answer runs from here, not from when the request came in.
            self._spoken_by[key] = dataclasses.replace(
                seen, asked=bool(result.continue_conversation), at=dt_util.utcnow()
            )

    def answers_a_question(self, user_input: Input) -> bool:
        """Whether this turn answers a question the last reply in its conversation asked.

        Such an answer belongs to whoever asked. Read as a request of its own it can match
        a built-in command: "in five minutes to drink some water", the answer to "when,
        and what for?", is Home Assistant's "in <time> <command>" and set a timer.
        """
        asked = self._asked.get(user_input.conversation_id or "")
        return asked is not None and dt_util.utcnow() - asked <= CONVERSATION_LIFETIME

    async def _is_system_user(self, user_id: str) -> bool:
        """True for a user Home Assistant created for itself, which no one logs in as."""
        user = await self.hass.auth.async_get_user(user_id)
        return user is not None and user.system_generated

    def last_score(self, device_id: str | None) -> float | None:
        """Voice match score of the satellite's last request, if it reports one."""
        if not device_id:
            return None
        registry = er.async_get(self.hass)
        for entity in er.async_entries_for_device(registry, device_id):
            if entity.entity_id.endswith("_last_speaker_score"):
                state = self.hass.states.get(entity.entity_id)
                if state is None:
                    return None
                try:
                    return float(state.state)
                except (TypeError, ValueError):
                    return None
        return None
