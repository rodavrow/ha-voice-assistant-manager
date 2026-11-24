# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Avatars: send a copy of an assistant's speech to a LiveTalking lip-sync server.

The speech itself is made by the TTS entity in tts.py; this module only prepares a
finished clip and plays it on every LiveTalking viewer. Nothing here may delay or break
the speech the satellite plays, so the sender never raises.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import wave
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

SUBENTRY_TYPE = "avatar"
CONF_ENGINE = "engine"
CONF_URL = "url"
CONF_ENABLED = "enabled"
# The LiveTalking avatar a device asks for; empty for the server's default.
CONF_AVATAR_ID = "avatar_id"

# A clip bigger than this is not copied or sent: a few minutes of speech at most.
MAX_CLIP_BYTES = 10 * 1024 * 1024
# LiveTalking makes a session per viewer; more than this is not a household setup.
MAX_SESSIONS = 8
CONNECT_TIMEOUT = 2.0
TOTAL_TIMEOUT = 10.0

_RAW_EXTENSIONS = frozenset({"pcm", "raw"})
# The session a satellite in avatar mode holds (avatar_devices.py): it sends its own speech.
_SESSION_OBJECT = re.compile(r"^avatar_session_\d+$")


def object_ids(unique_id: str | None) -> frozenset[str]:
    """The parts of an ESPHome unique_id that can be the device's object id.

    Home Assistant builds it as <mac>-<platform>-<object_id>, possibly with "@<device>"
    after the object id for an entity of a sub-device. Entity ids are not used: they come
    from names and change when the user renames something.
    """
    return frozenset(part.split("@", 1)[0] for part in (unique_id or "").split("-"))


def device_sessions(hass: HomeAssistant) -> frozenset[str]:
    """The LiveTalking sessions satellites hold themselves, which a copy must skip."""
    sessions = set()
    for entity in er.async_get(hass).entities.values():
        if entity.domain != "sensor":
            continue
        if not any(_SESSION_OBJECT.match(part) for part in object_ids(entity.unique_id)):
            continue
        state = hass.states.get(entity.entity_id)
        if state is not None and state.state not in ("", "unknown", "unavailable"):
            sessions.add(state.state)
    return frozenset(sessions)


class LiveTalkingError(Exception):
    """LiveTalking answered, but not in the shape this integration knows."""


def normalize_url(url: str) -> str | None:
    """The LiveTalking base address without a trailing slash, or None if it is not one.

    Only a plain http(s) address with a host: no credentials, query or fragment.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _container(audio: bytes) -> str | None:
    """The audio container the bytes start with, if one LiveTalking can read."""
    if audio.startswith(b"RIFF") and audio[8:12] == b"WAVE":
        return "wav"
    if audio.startswith(b"ID3"):
        return "mp3"
    if len(audio) > 1 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0:
        # MPEG audio frame sync; layer bits 00 mean an AAC (ADTS) stream instead.
        return "mp3" if audio[1] & 0x06 else "aac"
    if audio.startswith(b"fLaC"):
        return "flac"
    if audio.startswith(b"OggS"):
        return "ogg"
    return None


def _wrap_wav(pcm: bytes, rate: int, channels: int, width: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as clip:
        clip.setnchannels(channels)
        clip.setsampwidth(width)
        clip.setframerate(rate)
        clip.writeframes(pcm)
    return buffer.getvalue()


def prepare_clip(
    extension: str, audio: bytes, options: Mapping[str, Any]
) -> tuple[str, bytes] | None:
    """(extension, bytes) to upload for a finished clip, or None to send nothing.

    The format is told from the bytes, not from the engine. A clip in a container is sent
    as it is. Raw PCM is wrapped as WAV, with the sample format from the request's
    preferred_sample_* options; without a sample rate it cannot be played, so it is not
    sent. Anything else is sent as it is, for LiveTalking to try.
    """
    if not audio or len(audio) > MAX_CLIP_BYTES:
        return None
    kind = _container(audio)
    if extension.lower() not in _RAW_EXTENSIONS:
        return (kind or extension), audio
    if kind == "wav":
        return kind, audio
    # Raw samples can start with bytes that look like an MPEG frame (FF FF is -1), so for
    # raw audio only a WAV header counts.
    rate = options.get("preferred_sample_rate")
    if not rate:
        return None
    channels = int(options.get("preferred_sample_channels") or 1)
    width = int(options.get("preferred_sample_bytes") or 2)
    return "wav", _wrap_wav(audio, int(rate), channels, width)


class LiveTalkingClient:
    """The three LiveTalking calls an avatar needs. Raises on any failure."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self._base_url = base_url
        self._timeout = aiohttp.ClientTimeout(total=TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT)

    async def async_play(
        self, extension: str, audio: bytes, skip: frozenset[str] = frozenset()
    ) -> int:
        """Stop what each viewer's avatar says and play the clip. Returns the viewer count.

        Sessions in `skip` are left alone.
        """
        session_ids = await self._async_sessions(skip)
        for session_id in session_ids:
            await self._async_post("/interrupt_talk", json={"sessionid": session_id})
            form = aiohttp.FormData()
            form.add_field("sessionid", session_id)
            form.add_field("file", audio, filename=f"speech.{extension}")
            await self._async_post("/humanaudio", data=form)
        return len(session_ids)

    async def _async_sessions(self, skip: frozenset[str]) -> list[str]:
        async with self._session.get(
            f"{self._base_url}/api/admin/sessions", timeout=self._timeout
        ) as response:
            response.raise_for_status()
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise LiveTalkingError("sessions reply is not JSON") from err
        return [sid for sid in _session_ids(payload) if sid not in skip][:MAX_SESSIONS]

    async def _async_post(self, path: str, **kwargs: Any) -> None:
        async with self._session.post(
            f"{self._base_url}{path}", timeout=self._timeout, **kwargs
        ) as response:
            response.raise_for_status()


def _session_ids(payload: Any) -> list[str]:  # Any: parsed JSON from another server
    """The viewer session ids of a sessions reply. Raises LiveTalkingError on any surprise."""
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise LiveTalkingError("sessions reply is not a success")
    data = payload.get("data")
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, list):
        raise LiveTalkingError("sessions reply has no session list")
    ids = []
    for item in sessions:
        session_id = item.get("sessionid") if isinstance(item, dict) else None
        # Some LiveTalking versions number their sessions.
        if isinstance(session_id, int) and not isinstance(session_id, bool):
            session_id = str(session_id)
        if not isinstance(session_id, str) or not session_id:
            raise LiveTalkingError("sessions reply has a session without an id")
        ids.append(session_id)
    return ids


class AvatarSender:
    """Sends finished clips for one avatar, one at a time and in order.

    Never raises; warns once per outage.
    """

    def __init__(self, hass: HomeAssistant, name: str) -> None:
        self._hass = hass
        self._name = name
        self._failing = False
        self._warned_format = False
        # Each clip interrupts the avatar, so an earlier clip must never land after a later one.
        self._turn = asyncio.Lock()

    async def async_send(
        self, base_url: str, extension: str, audio: bytes, options: Mapping[str, Any]
    ) -> None:
        try:
            clip = prepare_clip(extension, audio, options)
        except (wave.Error, ValueError, TypeError) as err:
            _LOGGER.debug("Avatar %s: clip not sent, bad sample format (%s)", self._name, err)
            return
        if clip is None:
            self._log_skipped(extension, len(audio))
            return
        client = LiveTalkingClient(async_get_clientsession(self._hass), base_url)
        async with self._turn:
            try:
                async with asyncio.timeout(TOTAL_TIMEOUT):
                    played = await client.async_play(*clip, device_sessions(self._hass))
            except (aiohttp.ClientError, TimeoutError, LiveTalkingError) as err:
                self._log_failure(err)
                return
            except Exception as err:  # noqa: BLE001 - the speech must never fail because of this
                self._log_failure(err, unexpected=True)
                return
        self._failing = False
        _LOGGER.debug("Avatar %s: clip sent to %d viewer(s)", self._name, played)

    def _log_failure(self, err: Exception, *, unexpected: bool = False) -> None:
        first = not self._failing
        self._failing = True
        _LOGGER.log(
            logging.WARNING if first else logging.DEBUG,
            "Avatar %s: LiveTalking could not be reached or answered oddly (%s); the speech"
            " itself is not affected",
            self._name,
            type(err).__name__,
            exc_info=err if unexpected and first else None,
        )

    def _log_skipped(self, extension: str, size: int) -> None:
        if extension.lower() in _RAW_EXTENSIONS and size and not self._warned_format:
            self._warned_format = True
            _LOGGER.warning(
                "Avatar %s: the engine returned raw audio without a sample rate, so the"
                " avatar cannot play it",
                self._name,
            )
            return
        _LOGGER.debug("Avatar %s: clip of %d bytes not sent", self._name, size)
