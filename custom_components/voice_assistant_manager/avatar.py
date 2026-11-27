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
import json
import logging
import wave
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import data_keys
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Where stage.py keeps the chat shown on a cast device, in hass.data[DOMAIN].
STAGE_DATA = data_keys.STAGE

SUBENTRY_TYPE = "avatar"
CONF_ENGINE = "engine"
CONF_URL = "url"
CONF_ENABLED = "enabled"
# The LiveTalking avatar a device asks for; empty for the server's default.
CONF_AVATAR_ID = "avatar_id"

# A clip bigger than this is not copied or sent: a few minutes of speech at most.
MAX_CLIP_BYTES = 10 * 1024 * 1024
# LiveTalking's answers are a few hundred bytes; an SDP answer is a few kilobytes.
MAX_REPLY_BYTES = 256 * 1024
# LiveTalking makes a session per viewer, and refuses one past its --max_session (4 on the
# household server). Satellites' own sessions count toward it too.
MAX_SESSIONS = 4
CONNECT_TIMEOUT = 2.0
TOTAL_TIMEOUT = 10.0

_RAW_EXTENSIONS = frozenset({"pcm", "raw"})
# The session a satellite in avatar mode holds (avatar_devices.py): it sends its own speech.
SESSION_NAME = "Avatar session"


def esphome_entity(unique_id: str | None) -> tuple[str, str] | None:
    """(ESPHome type, entity name) of an ESPHome entity's unique_id, or None.

    On satellites with sub-devices Home Assistant builds it as
    <mac>/<sub-device index>/<ESPHome type>/<entity name>, for example
    "ac:cc:fc:a4:13:ff/4/switch/Recognized voices only". The names are fixed English in the
    firmware and do not change when the user renames the entity; entity ids do.
    """
    parts = (unique_id or "").split("/", 3)
    if len(parts) != 4 or not parts[2] or not parts[3]:
        return None
    return parts[2], parts[3]


def stage_sessions(hass: HomeAssistant) -> frozenset[str]:
    """The sessions of a chat shown on a cast device (stage.py), which get their lines
    from the chat itself and never a copy."""
    stage = hass.data.get(DOMAIN, {}).get(STAGE_DATA)
    return stage.sessions() if stage is not None else frozenset()


# Where the last session each satellite reported is kept, in hass.data[DOMAIN].
_LAST_SESSIONS = data_keys.AVATAR_LAST_SESSIONS
_NOT_READABLE = ("unknown", "unavailable")


def _session_sensors(hass: HomeAssistant) -> list[str]:
    """The entity ids of the satellites' "Avatar session" sensors that are in use."""
    return [
        entity.entity_id
        for entity in er.async_get(hass).entities.values()
        if entity.domain == "sensor"
        and entity.disabled_by is None
        and (named_as := esphome_entity(entity.unique_id)) is not None
        and named_as[1] == SESSION_NAME
    ]


def _read_sessions(hass: HomeAssistant) -> tuple[frozenset[str], list[str]]:
    """(the satellites' sessions, the sensors whose session cannot be known).

    A sensor that cannot be read (its satellite is away, or Home Assistant has just
    started) says nothing about the session: the satellite may well still hold it. So the
    last session it reported counts until it reports again. One that was never read is
    unknown.
    """
    last: dict[str, str] = data_keys.domain_data(hass).setdefault(_LAST_SESSIONS, {})
    sessions: set[str] = set()
    unknown: list[str] = []
    for entity_id in _session_sensors(hass):
        state = hass.states.get(entity_id)
        if state is not None and state.state not in _NOT_READABLE:
            last[entity_id] = state.state
        if entity_id not in last:
            unknown.append(entity_id)
        elif last[entity_id]:
            sessions.add(last[entity_id])
    return frozenset(sessions), unknown


def device_sessions(hass: HomeAssistant) -> frozenset[str]:
    """The LiveTalking sessions satellites hold themselves, which a copy must skip."""
    return _read_sessions(hass)[0]


def unknown_device_sessions(hass: HomeAssistant) -> list[str]:
    """The session sensors of satellites whose session cannot be known right now.

    While there is one, a copy could land on a satellite's own session, which would say
    the reply a second time.
    """
    return _read_sessions(hass)[1]


class LiveTalkingError(Exception):
    """LiveTalking answered, but not in the shape this integration knows."""


def enabled_avatar(entry: ConfigEntry) -> Mapping[str, Any] | None:
    """The settings of the assistant's first avatar that is switched on, or None."""
    return next(
        (
            sub.data
            for sub in entry.subentries.values()
            if sub.subentry_type == SUBENTRY_TYPE and sub.data.get(CONF_ENABLED)
        ),
        None,
    )


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


def _true_wav(audio: bytes) -> bytes:
    """The WAV with the sizes in its header set to what it really holds.

    An engine that streams a WAV writes the header before it knows the length, as a guess
    or a placeholder. LiveTalking reads as many samples as the header says, so a header
    that says too few cut the end of the clip. Anything that cannot be read is left as is.
    """
    at = 12  # After "RIFF", its size and "WAVE": the first chunk.
    while at + 8 <= len(audio):
        size = int.from_bytes(audio[at + 4 : at + 8], "little")
        if audio[at : at + 4] == b"data":
            real = len(audio) - (at + 8)
            riff = len(audio) - 8
            if size == real and int.from_bytes(audio[4:8], "little") == riff:
                return audio
            _LOGGER.debug(
                "Avatar clip: the WAV header said %d bytes of audio, the clip has %d", size, real
            )
            return b"".join(
                (audio[:4], riff.to_bytes(4, "little"), audio[8 : at + 4],
                 real.to_bytes(4, "little"), audio[at + 8 :])
            )
        at += 8 + size + (size & 1)
    return audio


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
    if kind == "wav":
        return kind, _true_wav(audio)
    if extension.lower() not in _RAW_EXTENSIONS:
        return (kind or extension), audio
    # Raw samples can start with bytes that look like an MPEG frame (FF FF is -1), so for
    # raw audio only a WAV header counts.
    rate = options.get("preferred_sample_rate")
    if not rate:
        return None
    channels = int(options.get("preferred_sample_channels") or 1)
    width = int(options.get("preferred_sample_bytes") or 2)
    return "wav", _wrap_wav(audio, int(rate), channels, width)


class LiveTalkingClient:
    """The LiveTalking calls an avatar and the chat stage need. Raises on any failure."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self._base_url = base_url
        self._timeout = aiohttp.ClientTimeout(total=TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT)

    async def async_play(
        self,
        extension: str,
        audio: bytes,
        skip: frozenset[str] = frozenset(),
        avatar_id: str = "",
    ) -> int:
        """Stop what each viewer's avatar says and play the clip. Returns the viewer count.

        Sessions in `skip` are left alone, and with an `avatar_id` so are sessions showing
        another avatar (see choose_viewers).
        """
        session_ids = choose_viewers(await self._async_sessions(), skip, avatar_id)
        for session_id in session_ids:
            await self._async_post("/interrupt_talk", json={"sessionid": session_id})
            form = aiohttp.FormData()
            form.add_field("sessionid", session_id)
            form.add_field("file", audio, filename=f"speech.{extension}")
            await self._async_post("/humanaudio", data=form)
        return len(session_ids)

    async def async_offer(self, sdp: str, avatar_id: str = "") -> tuple[str, str]:
        """Open a session for a viewer's WebRTC offer: (answer SDP, session id)."""
        body: dict[str, str] = {"sdp": sdp, "type": "offer"}
        if avatar_id:
            body["avatar"] = avatar_id
        payload = await self._async_post_json("/offer", body)
        answer, kind = payload.get("sdp"), payload.get("type")
        session_id = payload.get("sessionid")
        if isinstance(session_id, int) and not isinstance(session_id, bool):
            session_id = str(session_id)
        if not isinstance(answer, str) or not answer or kind != "answer":
            raise LiveTalkingError("offer reply has no answer")
        if not isinstance(session_id, str) or not session_id:
            raise LiveTalkingError("offer reply has no session id")
        return answer, session_id

    async def async_say(
        self, session_id: str, extension: str, audio: bytes, emotion: str | None = None
    ) -> None:
        """Stop what one session says and have it say the clip, with the emotion if given.

        LiveTalking applies an upload's emotion to that upload's audio only; one that does
        not know emotions ignores the field.
        """
        await self._async_post("/interrupt_talk", json={"sessionid": session_id})
        form = aiohttp.FormData()
        form.add_field("sessionid", session_id)
        if emotion is not None:
            form.add_field("emotion", emotion)
        form.add_field("file", audio, filename=f"speech.{extension}")
        await self._async_post("/humanaudio", data=form)

    async def async_speaking(self, session_id: str) -> bool:
        """Whether the session is still speaking. It turns false a little before the last
        of the audio has played."""
        payload = await self._async_post_json("/is_speaking", {"sessionid": session_id})
        if payload.get("code") != 0 or not isinstance(payload.get("data"), bool):
            raise LiveTalkingError("is_speaking reply is not a success")
        return bool(payload["data"])

    async def _async_post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with self._session.post(
            f"{self._base_url}{path}", json=body, timeout=self._timeout, allow_redirects=False
        ) as response:
            response.raise_for_status()
            payload = await _async_json(response, path)
        if not isinstance(payload, dict):
            raise LiveTalkingError(f"{path} reply is not an object")
        return payload

    async def _async_sessions(self) -> list[tuple[str, str]]:
        async with self._session.get(
            f"{self._base_url}/api/admin/sessions", timeout=self._timeout, allow_redirects=False
        ) as response:
            response.raise_for_status()
            payload = await _async_json(response, "sessions")
        return _sessions(payload)

    async def _async_post(self, path: str, **kwargs: Any) -> None:
        async with self._session.post(
            f"{self._base_url}{path}", timeout=self._timeout, allow_redirects=False, **kwargs
        ) as response:
            response.raise_for_status()


async def _async_json(response: aiohttp.ClientResponse, what: str) -> Any:
    """The JSON of an answer, read up to MAX_REPLY_BYTES. Raises LiveTalkingError."""
    body, stream = bytearray(), response.content
    # One read gives what has arrived so far, not the whole answer: read until the end.
    while chunk := await stream.read(MAX_REPLY_BYTES + 1 - len(body)):
        body += chunk
        if len(body) > MAX_REPLY_BYTES:
            raise LiveTalkingError(f"{what} reply is larger than {MAX_REPLY_BYTES} bytes")
    try:
        return json.loads(body)
    except ValueError as err:
        raise LiveTalkingError(f"{what} reply is not JSON") from err


def choose_viewers(
    sessions: list[tuple[str, str]], skip: frozenset[str], avatar_id: str
) -> list[str]:
    """The sessions a copy goes to, at most MAX_SESSIONS; each skipped one is logged.

    A session in `skip` belongs to a satellite in avatar mode, which sends its own speech
    to it. With an `avatar_id`, a session showing another avatar never gets the copy, so
    one assistant's speech never comes out of another's face. Without one (the server's
    default avatar) every other session does.
    """
    chosen: list[str] = []
    for session_id, shows in sessions:
        if session_id in skip:
            _LOGGER.debug("Avatar copy: session %s skipped, a satellite speaks to it", session_id)
        elif avatar_id and shows != avatar_id:
            _LOGGER.debug(
                "Avatar copy: session %s skipped, it shows avatar %s, not %s",
                session_id, shows or "(default)", avatar_id,
            )
        elif len(chosen) >= MAX_SESSIONS:
            _LOGGER.debug(
                "Avatar copy: session %s skipped, %d sessions at most", session_id, MAX_SESSIONS
            )
        else:
            _LOGGER.debug("Avatar copy: session %s gets the clip", session_id)
            chosen.append(session_id)
    return chosen


def _sessions(payload: Any) -> list[tuple[str, str]]:  # Any: parsed JSON from another server
    """(session id, avatar id or "") of each viewer. Raises LiveTalkingError on any surprise."""
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise LiveTalkingError("sessions reply is not a success")
    data = payload.get("data")
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, list):
        raise LiveTalkingError("sessions reply has no session list")
    found: list[tuple[str, str]] = []
    for item in sessions:
        session_id = item.get("sessionid") if isinstance(item, dict) else None
        # Some LiveTalking versions number their sessions.
        if isinstance(session_id, int) and not isinstance(session_id, bool):
            session_id = str(session_id)
        if not isinstance(session_id, str) or not session_id:
            raise LiveTalkingError("sessions reply has a session without an id")
        shows = item.get("avatar_id")
        found.append((session_id, shows if isinstance(shows, str) else ""))
    return found


class AvatarSender:
    """Sends finished clips for one avatar, one at a time and in order.

    Never raises; warns once per outage.
    """

    def __init__(self, hass: HomeAssistant, name: str) -> None:
        self._hass = hass
        self._name = name
        self._failing = False
        self._warned_format = False
        self._warned_unknown = False
        # Each clip interrupts the avatar, so an earlier clip must never land after a later one.
        self._turn = asyncio.Lock()

    async def async_send(
        self,
        base_url: str,
        extension: str,
        audio: bytes,
        options: Mapping[str, Any],
        avatar_id: str = "",
    ) -> None:
        try:
            clip = prepare_clip(extension, audio, options)
        except (wave.Error, ValueError, TypeError) as err:
            _LOGGER.debug("Avatar %s: clip not sent, bad sample format (%s)", self._name, err)
            return
        if clip is None:
            self._log_skipped(extension, len(audio))
            return
        if unknown := unknown_device_sessions(self._hass):
            # Fail closed: saying a reply twice on a satellite is worse than a viewer
            # elsewhere missing a copy of it.
            _LOGGER.log(
                logging.DEBUG if self._warned_unknown else logging.WARNING,
                "Avatar %s: no copy sent, the LiveTalking session of %s is not known (the"
                " satellite is away, or Home Assistant has just started); the speech"
                " itself is not affected",
                self._name, ", ".join(unknown),
            )
            self._warned_unknown = True
            return
        self._warned_unknown = False
        client = LiveTalkingClient(async_get_clientsession(self._hass), base_url)
        async with self._turn:
            try:
                async with asyncio.timeout(TOTAL_TIMEOUT):
                    played = await client.async_play(
                        *clip, device_sessions(self._hass) | stage_sessions(self._hass),
                        avatar_id,
                    )
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
