# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Streamed avatar replies: feed a satellite's LiveTalking session while the speech is made.

A satellite in avatar mode used to wait for the whole clip, download it and upload it to
LiveTalking before the avatar spoke. Here the speech goes to the satellite's session as
the engine makes it, so the avatar starts while the rest is still on its way. The normal
audio still goes to the satellite unchanged, as its backup.

It only happens when all of this holds, and otherwise the satellite speaks as before:

- it is known which satellite asked (Home Assistant does not tell a TTS entity, so the
  assistant's router notes each turn; two satellites asking at once are not told apart);
- that satellite's slot for this assistant has a session and its switch "Avatar fed by
  Home Assistant" on, which only firmware that follows the session by itself has;
- LiveTalking answers "ready" on /stream_audio within OPEN_TIMEOUT.

The stream is opened before the first audio reaches Home Assistant, so a satellite that
sees its backup arrive can ask LiveTalking whether a reply is open on its session. The
audio is decoded to 16 kHz mono PCM with ffmpeg. Nothing here raises or holds up the speech.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import ulid

from . import avatar_devices
from .avatar import MAX_CLIP_BYTES
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_STREAM: Final = avatar_devices.CONF_STREAM
# How long after a turn ended its speech may still start (a reply that is not streamed
# is made just after the router answered).
TURN_GRACE: Final = 2.0
# LiveTalking gets this long to say "ready"; the speech is never held up longer.
OPEN_TIMEOUT: Final = 0.3
# No audio from the engine for this long ends the reply on the avatar.
STALL_TIMEOUT: Final = 5.0
PCM_RATE: Final = 16000
_READ_BYTES: Final = 3200  # 100 ms of 16 kHz mono 16-bit audio
_NO_SESSION: Final = ("", "unknown", "unavailable")
_TURNS: Final = "avatar_stream_turns"
_FFMPEG_FORMATS: Final = {"mp3": "mp3", "wav": "wav", "ogg": "ogg", "flac": "flac"}
_RAW: Final = ("pcm", "raw")


def _now() -> float:
    return time.monotonic()


# Which satellite asked


@dataclass
class _Turn:
    satellite: str
    ended: float | None = None


def _turns(hass: HomeAssistant) -> dict[str, list[_Turn]]:
    turns: dict[str, list[_Turn]] = hass.data.setdefault(DOMAIN, {}).setdefault(_TURNS, {})
    return turns


def turn_started(hass: HomeAssistant, entry_id: str, satellite: str | None) -> _Turn | None:
    """Note that this satellite asked the assistant. Returns what turn_ended wants back."""
    if not satellite:
        return None
    turn = _Turn(satellite)
    _turns(hass).setdefault(entry_id, []).append(turn)
    return turn


def turn_ended(turn: _Turn | None) -> None:
    if turn is not None:
        turn.ended = _now()


def asking_satellite(hass: HomeAssistant, entry_id: str) -> str | None:
    """The satellite whose turn with this assistant runs now or just ended, or None.

    None too when two satellites are asking it: their replies cannot be told apart.
    """
    turns = _turns(hass).get(entry_id, [])
    now = _now()
    turns[:] = [t for t in turns if t.ended is None or now - t.ended <= TURN_GRACE]
    satellites = {turn.satellite for turn in turns}
    return satellites.pop() if len(satellites) == 1 else None


# Where to feed


@dataclass(frozen=True)
class FeedTarget:
    """The LiveTalking session of the satellite that asked."""

    base_url: str
    session_id: str
    # The satellite's "Avatar session" sensor: when it changes, the session is gone.
    session_entity: str


def feed_target(hass: HomeAssistant, entry: ConfigEntry, base_url: str) -> FeedTarget | None:
    """Where this assistant's reply can be fed now, or None to leave it to the satellite."""
    advanced = entry.options.get("advanced")
    if isinstance(advanced, Mapping) and advanced.get(CONF_STREAM) is False:
        return None
    satellite = asking_satellite(hass, entry.entry_id)
    if satellite is None:
        return None
    found: list[FeedTarget] = []
    for slot, entry_id in avatar_devices.satellite_slots(hass).get(satellite, {}).items():
        if entry_id != entry.entry_id:
            continue
        entities = avatar_devices.slot_entities(hass, satellite, slot)
        if entities is None or entities.fed is None or entities.session is None:
            continue
        fed, session = hass.states.get(entities.fed), hass.states.get(entities.session)
        if fed is None or fed.state != "on" or session is None:
            continue
        if session.state not in _NO_SESSION:
            found.append(FeedTarget(base_url, session.state, entities.session))
    # Two wake words of one satellite on the same assistant: which one asked is not known.
    return found[0] if len(found) == 1 else None


# Decoding


class Decoder(Protocol):
    """Turns the engine's audio into 16 kHz mono 16-bit PCM, as it arrives."""

    async def async_write(self, audio: bytes) -> None: ...

    async def async_close_input(self) -> None: ...

    def output(self) -> AsyncIterator[bytes]: ...

    async def async_stop(self) -> None: ...


class _FfmpegDecoder:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    async def async_write(self, audio: bytes) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(audio)
        await self._process.stdin.drain()

    async def async_close_input(self) -> None:
        assert self._process.stdin is not None
        with contextlib.suppress(OSError):
            self._process.stdin.close()
            await self._process.stdin.wait_closed()

    async def output(self) -> AsyncIterator[bytes]:
        assert self._process.stdout is not None
        while pcm := await self._process.stdout.read(_READ_BYTES):
            yield pcm

    async def async_stop(self) -> None:
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
        await self._process.wait()


def decoder_arguments(extension: str, options: Mapping[str, Any]) -> list[str] | None:
    """ffmpeg's arguments to decode this audio from its input to PCM on its output.

    None for raw audio without a sample rate, which cannot be decoded.
    """
    extension = extension.lower()
    source: list[str] = []
    if extension in _RAW:
        rate = options.get("preferred_sample_rate")
        if not rate:
            return None
        width = int(options.get("preferred_sample_bytes") or 2)
        channels = int(options.get("preferred_sample_channels") or 1)
        source = ["-f", f"s{width * 8}le", "-ar", str(int(rate)), "-ac", str(channels)]
    elif extension in _FFMPEG_FORMATS:
        source = ["-f", _FFMPEG_FORMATS[extension]]
    if source:
        # The format is known, so ffmpeg need not read ahead to work it out: without
        # these it holds back the first 1.4 s of an MP3 before it gives any audio.
        source = ["-probesize", "32", "-analyzeduration", "0", *source]
    # Never "-fflags nobuffer": it makes ffmpeg throw away what it read while working the
    # stream out, which was the first 1.26 s of every reply.
    return [
        "-hide_banner", "-loglevel", "error", *source, "-i", "pipe:0",
        "-f", "s16le", "-ac", "1", "-ar", str(PCM_RATE), "-flush_packets", "1", "pipe:1",
    ]


async def async_start_decoder(
    hass: HomeAssistant, extension: str, options: Mapping[str, Any]
) -> Decoder | None:
    """Start ffmpeg for this audio, or None when it cannot be decoded here."""
    from homeassistant.components.ffmpeg import (  # noqa: PLC0415 - only when streaming
        get_ffmpeg_manager,
    )

    arguments = decoder_arguments(extension, options)
    if arguments is None:
        return None
    process = await asyncio.create_subprocess_exec(
        get_ffmpeg_manager(hass).binary,
        *arguments,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return _FfmpegDecoder(process)


# The feed


class ReplyFeed:
    """One reply on its way to a LiveTalking session. Never raises.

    async_open first; when it returned True, write each piece of audio as it comes and
    finish at the end (or abort when the speech failed). The sending runs in async_run.
    """

    def __init__(
        self, hass: HomeAssistant, target: FeedTarget, extension: str, options: Mapping[str, Any]
    ) -> None:
        self._hass = hass
        self._target = target
        self._extension = extension
        self._options = dict(options)
        self.reply_id = ulid.ulid_now()
        self._socket: aiohttp.ClientWebSocketResponse | None = None
        self._audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._size = 0
        self._stopped = False
        # For the log: what was forwarded, to compare with what LiveTalking says it got.
        self._opened = _now()
        self._sent_bytes = 0

    async def async_open(self) -> bool:
        """Open the reply on LiveTalking. False when it did not say "ready" in time."""
        try:
            async with asyncio.timeout(OPEN_TIMEOUT):
                self._socket = await async_get_clientsession(self._hass).ws_connect(
                    f"{self._target.base_url}/stream_audio",
                    params={"sessionid": self._target.session_id, "reply_id": self.reply_id},
                )
                message = await self._socket.receive()
            ready = (
                message.type is aiohttp.WSMsgType.TEXT
                and message.json().get("type") == "ready"
            )
        except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError) as err:
            # A refusal (no such session, another reply open) or a LiveTalking without
            # /stream_audio: the satellite speaks the reply itself.
            _LOGGER.debug("Avatar stream: not opened (%s)", type(err).__name__)
            ready = False
        if not ready:
            await self._async_close()
        return ready

    def write(self, audio: bytes) -> None:
        if self._stopped:
            return
        if not self._size:
            _LOGGER.debug(
                "Avatar stream: reply %s first %d bytes from the engine %.2f s after it opened",
                self.reply_id, len(audio), _now() - self._opened,
            )
        self._size += len(audio)
        if self._size > MAX_CLIP_BYTES:
            self.finish()
            return
        self._audio.put_nowait(audio)

    def finish(self) -> None:
        """The speech ended: the avatar says what it got, then the reply closes."""
        if not self._stopped:
            self._audio.put_nowait(None)

    def abort(self) -> None:
        """The speech failed or the reply was cancelled: nothing more is sent."""
        self._stopped = True
        self._audio.put_nowait(None)

    async def async_run(self) -> None:
        """Decode and send until the reply ends, then close it."""
        decoder: Decoder | None = None
        tasks: list[asyncio.Task[None]] = []
        try:
            decoder = await async_start_decoder(self._hass, self._extension, self._options)
            if decoder is None or self._socket is None:
                _LOGGER.debug("Avatar stream: this audio cannot be decoded, nothing sent")
                return
            tasks = [
                asyncio.create_task(self._async_decode(decoder)),
                asyncio.create_task(self._async_send(decoder)),
                asyncio.create_task(self._async_listen()),
            ]
            # The listener ends only when LiveTalking cancels or closes the reply.
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if not tasks[2].done():
                await asyncio.wait(tasks[:2])
        except Exception:  # noqa: BLE001 - the speech itself must never fail because of this
            _LOGGER.warning("Avatar stream: stopped by an unexpected error", exc_info=True)
        finally:
            self._stopped = True
            _LOGGER.debug(
                "Avatar stream: reply %s closed %.1f s after it opened; %d bytes from the"
                " engine, %.2f s of audio forwarded",
                self.reply_id, _now() - self._opened, self._size,
                self._sent_bytes / (PCM_RATE * 2),
            )
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if decoder is not None:
                with contextlib.suppress(Exception):
                    await decoder.async_stop()
            await self._async_close()

    async def _async_decode(self, decoder: Decoder) -> None:
        """Hand the engine's audio to the decoder until it ends, stalls or the session goes."""
        while True:
            try:
                async with asyncio.timeout(STALL_TIMEOUT):
                    audio = await self._audio.get()
            except TimeoutError:
                _LOGGER.debug("Avatar stream: no audio for %s s, ending the reply", STALL_TIMEOUT)
                break
            if audio is None or self._stopped or self._session_changed():
                break
            await decoder.async_write(audio)
        await decoder.async_close_input()

    async def _async_send(self, decoder: Decoder) -> None:
        assert self._socket is not None
        async for pcm in decoder.output():
            if self._stopped or self._session_changed():
                return
            if not self._sent_bytes:
                _LOGGER.debug(
                    "Avatar stream: reply %s first audio forwarded %.2f s after it opened",
                    self.reply_id, _now() - self._opened,
                )
            await self._socket.send_bytes(pcm)
            self._sent_bytes += len(pcm)
        if not self._stopped:
            await self._socket.send_json({"type": "end"})

    async def _async_listen(self) -> None:
        """Returns when LiveTalking cancels the reply (an interrupt) or closes the stream."""
        assert self._socket is not None
        async for message in self._socket:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            with contextlib.suppress(ValueError, AttributeError):
                if message.json().get("type") == "cancelled":
                    _LOGGER.debug("Avatar stream: reply %s was interrupted", self.reply_id)
                    break
        self._stopped = True

    def _session_changed(self) -> bool:
        state = self._hass.states.get(self._target.session_entity)
        changed = state is None or state.state != self._target.session_id
        if changed:
            self._stopped = True
        return changed

    async def _async_close(self) -> None:
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
