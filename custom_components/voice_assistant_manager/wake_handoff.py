# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hands a request to the assistant whose wake word started it, when Home Assistant did not.

A satellite with two wake words runs a pipeline per wake word, each with its own assistant.
Home Assistant keeps one conversation per satellite, whichever wake word was said
(assist_satellite entity.py, _conversation_id), and when an assistant's last reply asked to
go on (continue_conversation), the next request in that conversation goes to that assistant,
whatever pipeline runs (assist_pipeline, continue_conversation_agent). So within five
minutes of one assistant asking something, the other wake word reaches the first assistant.

The satellite says which wake word it heard in its "Last wake word" sensor, and Home
Assistant chose the pipeline by that phrase. So when a request reaches one of these
assistants right after a wake word whose pipeline uses another of them, it is handed to
that one, in a conversation of its own, and the conversation is remembered: its follow-ups
(which carry no wake word) go there too, until another wake word says otherwise. Requests
without a fresh wake word, and assistants of other integrations, are left as they are.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .avatar import esphome_entity
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

WAKE_WORD_SENSOR = "Last wake word"
# A wake word this long before a request still started it: speech, then the voice match.
FRESH = timedelta(seconds=60)
# As long as Home Assistant keeps a conversation going without use (chat_session.py).
LIFETIME = timedelta(minutes=5)
# The selects of each wake word and its pipeline: <mac>-wake_word, <mac>-wake_word_2, ...
_WAKE_SELECT = re.compile(r"-wake_word(?:_(\d+))?$")
_PIPELINE_SELECT = re.compile(r"-pipeline(?:_(\d+))?$")
_DATA = "wake_handoff"


@dataclass
class _Handed:
    """A conversation whose requests go to another assistant."""

    agent_id: str
    device_id: str
    # The conversation that assistant answers in; None until it has answered once.
    conversation_id: str | None
    at: datetime


@dataclass
class _State:
    # Satellite device id -> when one of these assistants last had a request from it.
    last_request: dict[str, datetime] = dataclasses.field(default_factory=dict)
    # The satellite's conversation id -> where its requests go.
    handed: dict[str, _Handed] = dataclasses.field(default_factory=dict)


def _state(hass: HomeAssistant) -> _State:
    data = hass.data.setdefault(DOMAIN, {})
    if _DATA not in data:
        data[_DATA] = _State()
    state: _State = data[_DATA]
    return state


def fresh_wake_word(hass: HomeAssistant, device_id: str, since: datetime | None) -> str | None:
    """The phrase the satellite last woke on, if it reported it after `since` and within
    FRESH of now; else None."""
    registry = er.async_get(hass)
    now = dt_util.utcnow()
    for entity in er.async_entries_for_device(registry, device_id):
        if entity.domain != "sensor":
            continue
        named = esphome_entity(entity.unique_id)
        if (named is None or named[1] != WAKE_WORD_SENSOR) and not entity.entity_id.endswith(
            "_last_wake_word"
        ):
            continue
        state = hass.states.get(entity.entity_id)
        if state is None or state.state in ("", "unknown", "unavailable"):
            return None
        reported = state.last_reported
        if now - reported > FRESH or (since is not None and reported <= since):
            return None
        return state.state
    return None


def woken_agent(hass: HomeAssistant, device_id: str, phrase: str) -> str | None:
    """The conversation agent of the pipeline on the wake word with this phrase, as Home
    Assistant picks it: the first wake word select showing the phrase, its pipeline select."""
    from homeassistant.components import assist_pipeline  # noqa: PLC0415

    selects = [
        entity
        for entity in er.async_entries_for_device(er.async_get(hass), device_id)
        if entity.domain == "select" and entity.unique_id
    ]

    def slot(entity: er.RegistryEntry, pattern: re.Pattern[str]) -> int | None:
        match = pattern.search(entity.unique_id)
        return int(match.group(1) or 1) if match else None

    woken = sorted(
        number
        for entity in selects
        if (number := slot(entity, _WAKE_SELECT)) is not None
        and (state := hass.states.get(entity.entity_id)) is not None
        and state.state == phrase
    )
    if not woken:
        return None
    pipeline_select = next(
        (entity for entity in selects if slot(entity, _PIPELINE_SELECT) == woken[0]), None
    )
    chosen = hass.states.get(pipeline_select.entity_id) if pipeline_select else None
    if chosen is None or chosen.state in ("unknown", "unavailable"):
        return None
    pipeline: assist_pipeline.Pipeline | None
    if chosen.state == "preferred":
        pipeline = assist_pipeline.async_get_pipeline(hass)
    else:
        pipeline = next(
            (p for p in assist_pipeline.async_get_pipelines(hass) if p.name == chosen.state),
            None,
        )
    return pipeline.conversation_engine if pipeline is not None else None


def _is_router(hass: HomeAssistant, entity_id: str) -> bool:
    entity = er.async_get(hass).async_get(entity_id)
    return entity is not None and entity.platform == DOMAIN and entity.domain == "conversation"


def _target(hass: HomeAssistant, own: str, user_input: Any) -> _Handed | None:
    """Where this request should go instead of `own`, or None to answer it here."""
    device_id = user_input.device_id
    conversation_id = user_input.conversation_id
    if not device_id:
        return None
    state = _state(hass)
    now = dt_util.utcnow()
    state.handed = {
        key: handed for key, handed in state.handed.items() if now - handed.at <= LIFETIME
    }
    previous = state.last_request.get(device_id)
    state.last_request[device_id] = now
    phrase = fresh_wake_word(hass, device_id, previous)
    if phrase is not None:
        agent = woken_agent(hass, device_id, phrase)
        if agent is None or agent == own or not _is_router(hass, agent):
            # Woken for this assistant, or for one of another integration: as it is.
            if conversation_id:
                state.handed.pop(conversation_id, None)
            return None
        _LOGGER.info(
            "%s woke on %r, whose assistant is %s, but the request reached %s (Home "
            "Assistant kept the other assistant's conversation); handing it over",
            device_id, phrase, agent, own,
        )
        handed = _Handed(agent, device_id, None, now)
        if conversation_id:
            state.handed[conversation_id] = handed
        return handed
    earlier = state.handed.get(conversation_id or "")
    if earlier is None or earlier.device_id != device_id or earlier.agent_id == own:
        return None
    earlier.at = now
    return earlier


async def async_hand_off(
    hass: HomeAssistant, own: str, user_input: Any
) -> conversation.ConversationResult | None:
    """The answer of the assistant this request belongs to, or None to answer it here."""
    try:
        handed = _target(hass, own, user_input)
    except Exception:  # noqa: BLE001 - never block a request over this
        _LOGGER.exception("Could not tell which wake word started a request")
        return None
    if handed is None:
        return None
    result = await conversation.async_converse(
        hass,
        user_input.text,
        handed.conversation_id,
        user_input.context,
        language=user_input.language,
        agent_id=handed.agent_id,
        device_id=user_input.device_id,
        satellite_id=getattr(user_input, "satellite_id", None),
        extra_system_prompt=user_input.extra_system_prompt,
    )
    handed.conversation_id = result.conversation_id
    # The satellite goes on in its own conversation; the answer keeps it.
    if user_input.conversation_id is not None:
        result = dataclasses.replace(result, conversation_id=user_input.conversation_id)
    return result
