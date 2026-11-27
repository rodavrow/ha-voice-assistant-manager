# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Talking to an assistant's avatar from the Voice Manager page.

The page opens one LiveTalking session for the assistant's avatar, through here, as the
chat stage does, and sends the microphone into Home Assistant's own voice pipeline for
that assistant. The turn that follows is an ordinary one, asked by the logged-in user
without a satellite. Its reply is streamed into the page's session (avatar_stream.py) the
way a display is fed, so the page plays the voice from the same stream as the lips.

One talk per assistant. A talk is the user's who opened it: only their turns without a
satellite are fed to it, and only they can close it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import data_keys
from .avatar import (
    CONF_AVATAR_ID,
    CONF_URL,
    MAX_SESSIONS,
    LiveTalkingClient,
    LiveTalkingError,
    enabled_avatar,
    normalize_url,
    present_device_sessions,
    stage_sessions,
)

_LOGGER = logging.getLogger(__name__)

CALL_TIMEOUT = 10.0
# What a turn asked on the page is noted under where a satellite's entity id would be.
_ASKER = "talk:"


class TalkRefused(Exception):
    """A talk cannot be opened. `key` names the reason, as a text of en.json's errors."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True)
class Talk:
    """One user talking to one assistant's avatar on a page."""

    user_id: str
    base_url: str
    session: str


def _talks(hass: HomeAssistant) -> dict[str, Talk]:
    """The open talks, by the assistant's entry id."""
    talks: dict[str, Talk] = data_keys.domain_data(hass).setdefault(data_keys.TALKS, {})
    return talks


def sessions(hass: HomeAssistant) -> frozenset[str]:
    """The LiveTalking sessions of the open talks."""
    return frozenset(talk.session for talk in _talks(hass).values())


async def async_open(hass: HomeAssistant, entry: ConfigEntry, user_id: str, sdp: str) -> str:
    """Open the assistant's avatar for the page's offer: the answer SDP.

    Opening again as the same user replaces their session. Raises TalkRefused.
    """
    talks = _talks(hass)
    avatar = enabled_avatar(entry)
    url = normalize_url(str(avatar.get(CONF_URL) or "")) if avatar else None
    if avatar is None or url is None:
        raise TalkRefused("talk_no_avatar")
    current = talks.get(entry.entry_id)
    if current is not None and current.user_id != user_id:
        raise TalkRefused("talk_busy")
    others = sessions(hass) - ({current.session} if current else set())
    taken = len(present_device_sessions(hass)) + len(stage_sessions(hass)) + len(others)
    if taken >= MAX_SESSIONS:
        raise TalkRefused("talk_no_room")
    client = LiveTalkingClient(async_get_clientsession(hass), url)
    try:
        async with asyncio.timeout(CALL_TIMEOUT):
            answer, session = await client.async_offer(sdp, str(avatar.get(CONF_AVATAR_ID) or ""))
    except (aiohttp.ClientError, TimeoutError, LiveTalkingError) as err:
        _LOGGER.warning("Talk: LiveTalking refused a session (%s)", type(err).__name__)
        raise TalkRefused("talk_livetalking") from err
    talks[entry.entry_id] = Talk(user_id, url, session)
    _LOGGER.debug("Talk: %s opened session %s", entry.title, session)
    return answer


@callback
def close(hass: HomeAssistant, entry_id: str, user_id: str | None = None) -> None:
    """End the talk with this assistant: the user's own, or whoever's when none is named.
    The page closes its stream itself, which ends the session on LiveTalking."""
    talks = _talks(hass)
    talk = talks.get(entry_id)
    if talk is not None and user_id in (None, talk.user_id):
        del talks[entry_id]


def asking(
    hass: HomeAssistant, entry_id: str, user_id: str | None, satellite: str | None
) -> str | None:
    """What to note a turn under so its reply reaches the page (avatar_stream.turn_started):
    set when the one talking to this assistant asked without a satellite, else None."""
    talk = _talks(hass).get(entry_id)
    if satellite or talk is None or not user_id or talk.user_id != user_id:
        return None
    return f"{_ASKER}{entry_id}"


def target(hass: HomeAssistant, asker: str) -> Talk | None:
    """The talk a turn was noted under, if it is still open."""
    if not asker.startswith(_ASKER):
        return None
    return _talks(hass).get(asker.removeprefix(_ASKER))


def async_get_pipelines(hass: HomeAssistant) -> list[Any]:
    """Home Assistant's voice pipelines; none when they are not set up here."""
    try:
        # Only when someone talks: the component brings in what a server without voice
        # pipelines has not installed.
        from homeassistant.components import assist_pipeline  # noqa: PLC0415

        return list(assist_pipeline.async_get_pipelines(hass))
    except (ImportError, KeyError):
        return []


def pipeline_for(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """The voice pipeline the page talks through: one that hears, asks this assistant and
    speaks with one of its avatar voices, so that the reply reaches the avatar. None when
    there is no such pipeline."""
    registry = er.async_get(hass)
    own = er.async_entries_for_config_entry(registry, entry.entry_id)
    agents = {item.entity_id for item in own if item.domain == "conversation"}
    voices = {item.entity_id for item in own if item.domain == "tts"}
    for pipeline in async_get_pipelines(hass):
        if (
            pipeline.conversation_engine in agents
            and pipeline.tts_engine in voices
            and pipeline.stt_engine
        ):
            return str(pipeline.id)
    return None
