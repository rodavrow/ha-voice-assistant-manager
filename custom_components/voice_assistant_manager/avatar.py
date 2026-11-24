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
import wave
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

SUBENTRY_TYPE = "avatar"
CONF_ENGINE = "engine"
CONF_URL = "url"
CONF_ENABLED = "enabled"

# A clip bigger than this is not copied or sent: a few minutes of speech at most.
MAX_CLIP_BYTES = 10 * 1024 * 1024
# LiveTalking makes a session per viewer; more than this is not a household setup.
MAX_SESSIONS = 8
CONNECT_TIMEOUT = 2.0
TOTAL_TIMEOUT = 10.0

_RAW_EXTENSIONS = frozenset({"pcm", "raw"})


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
    if (kind := _container(audio)) is not None:
        return kind, audio
    if extension.lower() not in _RAW_EXTENSIONS:
        return extension, audio
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

    async def async_play(self, extension: str, audio: bytes) -> int:
        """Stop what each viewer's avatar says and play the clip. Returns the viewer count."""
        session_ids = await self._async_sessions()
        for session_id in session_ids:
            await self._async_post("/interrupt_talk", json={"sessionid": session_id})
            form = aiohttp.FormData()
            form.add_field("sessionid", session_id)
            form.add_field("file", audio, filename=f"speech.{extension}")
            await self._async_post("/humanaudio", data=form)
        return len(session_ids)

    async def _async_sessions(self) -> list[str]:
        async with self._session.get(
            f"{self._base_url}/api/admin/sessions", timeout=self._timeout
        ) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
        return _session_ids(payload)[:MAX_SESSIONS]

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
        if not isinstance(session_id, str) or not session_id:
            raise LiveTalkingError("sessions reply has a session without an id")
        ids.append(session_id)
    return ids


class AvatarSender:
    """Sends finished clips for one avatar. Never raises; warns once per outage."""

    def __init__(self, hass: HomeAssistant, name: str) -> None:
        self._hass = hass
        self._name = name
        self._failing = False
        self._warned_format = False

    async def async_send(
        self, base_url: str, extension: str, audio: bytes, options: Mapping[str, Any]
    ) -> None:
        clip = prepare_clip(extension, audio, options)
        if clip is None:
            self._log_skipped(extension, len(audio))
            return
        client = LiveTalkingClient(async_get_clientsession(self._hass), base_url)
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT):
                played = await client.async_play(*clip)
        except (aiohttp.ClientError, TimeoutError, LiveTalkingError) as err:
            self._log_failure(err)
            return
        except Exception as err:  # noqa: BLE001 - the speech must never fail because of this
            _LOGGER.warning("Avatar %s: unexpected error sending a clip", self._name, exc_info=err)
            return
        self._failing = False
        _LOGGER.debug("Avatar %s: clip sent to %d viewer(s)", self._name, played)

    def _log_failure(self, err: Exception) -> None:
        level = logging.DEBUG if self._failing else logging.WARNING
        self._failing = True
        _LOGGER.log(
            level,
            "Avatar %s: LiveTalking could not be reached or answered oddly (%s); the speech"
            " itself is not affected",
            self._name,
            type(err).__name__,
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
