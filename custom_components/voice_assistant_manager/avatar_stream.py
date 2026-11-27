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
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import ulid

from . import avatar_devices, data_keys
from .avatar import MAX_CLIP_BYTES

_LOGGER = logging.getLogger(__name__)

CONF_STREAM: Final = avatar_devices.CONF_STREAM
# How long after a turn ended its speech may still start (a reply that is not streamed
# is made just after the router answered).
TURN_GRACE: Final = 2.0
# LiveTalking gets this long to say "ready"; the speech is never held up longer.
OPEN_TIMEOUT: Final = 0.3
# No first audio from the engine for this long ends the reply on the avatar, counted from
# the end of the assistant's turn: while it still answers (the main agent calls a tool
# before it says anything the engine can speak), the reply is held open as in a pause.
STALL_TIMEOUT: Final = 5.0
# Once it has spoken, a reply may pause: the main agent calls a tool (a web search) and
# the engine gets no text meanwhile. After HOLD_AFTER without audio LiveTalking is told
# to hold the reply open, again every HOLD_EVERY, and the satellite, which asks
# LiveTalking, then does not take the silence for a dropout. A pause of HOLD_LIMIT ends
# the reply on the avatar; LiveTalking has the same limit.
HOLD_AFTER: Final = 0.5
HOLD_EVERY: Final = 2.0
HOLD_LIMIT: Final = 90.0
# LiveTalking gets this long to take one piece of audio. One that stopped reading would
# otherwise keep the reply, its decoder and its socket for good.
SEND_TIMEOUT: Final = 5.0
# How often the socket is pinged, so a connection that died without a word is noticed.
HEARTBEAT: Final = 15.0
# No reply runs longer than this, whatever went wrong: several long pauses and a long
# answer fit in it, and nothing is left behind when one never ends.
RUN_LIMIT: Final = 600.0
# How much of what ffmpeg wrote about an error is kept for the log.
_ERROR_TAIL: Final = 500
PCM_RATE: Final = 16000
_READ_BYTES: Final = 3200  # 100 ms of 16 kHz mono 16-bit audio
_NO_SESSION: Final = ("", "unknown", "unavailable")
_TURNS: Final = data_keys.AVATAR_STREAM_TURNS
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
    turns: dict[str, list[_Turn]] = data_keys.domain_data(hass).setdefault(_TURNS, {})
    return turns


def turn_started(hass: HomeAssistant, entry_id: str, satellite: str | None) -> _Turn | None:
    """Note that this satellite asked the assistant. Returns what turn_ended wants back."""
    if not satellite:
        return None
    turn = _Turn(satellite)
    turns = _turns(hass).setdefault(entry_id, [])
    # Here too: an assistant without a streaming avatar never asks who is asking.
    _drop_old(turns)
    turns.append(turn)
    return turn


def _drop_old(turns: list[_Turn]) -> None:
    now = _now()
    turns[:] = [t for t in turns if t.ended is None or now - t.ended <= TURN_GRACE]


def forget_turns(hass: HomeAssistant, entry_id: str) -> None:
    """Drop what was noted for an assistant that was unloaded."""
    _turns(hass).pop(entry_id, None)


def turn_ended(turn: _Turn | None) -> None:
    if turn is not None:
        turn.ended = _now()


def answering(hass: HomeAssistant, entry_id: str) -> bool:
    """Whether a turn with this assistant is still running: more of its reply may come."""
    return any(turn.ended is None for turn in _turns(hass).get(entry_id, []))


def asking_satellite(hass: HomeAssistant, entry_id: str) -> str | None:
    """The satellite whose turn with this assistant runs now or just ended, or None.

    None too when two satellites are asking it: their replies cannot be told apart.
    """
    turns = _turns(hass).get(entry_id, [])
    _drop_old(turns)
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
        self._errors = b""
        # Read as it comes: a full pipe would stop ffmpeg.
        self._error_reader = asyncio.create_task(self._async_read_errors())

    async def _async_read_errors(self) -> None:
        if self._process.stderr is None:
            return
        while written := await self._process.stderr.read(_ERROR_TAIL):
            self._errors = (self._errors + written)[-_ERROR_TAIL:]

    async def async_write(self, audio: bytes) -> None:
        assert self._process.stdin is not None  # noqa: S101
        self._process.stdin.write(audio)
        await self._process.stdin.drain()

    async def async_close_input(self) -> None:
        assert self._process.stdin is not None  # noqa: S101
        with contextlib.suppress(OSError):
            self._process.stdin.close()
            await self._process.stdin.wait_closed()

    async def output(self) -> AsyncIterator[bytes]:
        assert self._process.stdout is not None  # noqa: S101
        while pcm := await self._process.stdout.read(_READ_BYTES):
            yield pcm

    async def async_stop(self) -> None:
        killed = self._process.returncode is None
        if killed:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
        await self._process.wait()
        with contextlib.suppress(asyncio.CancelledError, OSError):
            await self._error_reader
        if self._errors and not killed and self._process.returncode:
            _LOGGER.warning(
                "Avatar stream: ffmpeg ended with code %s: %s",
                self._process.returncode,
                self._errors.decode("utf-8", "replace").strip(),
            )


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
    # Only the pipe may be read: audio whose format ffmpeg works out by itself could be a
    # playlist that names a file or an address, which ffmpeg would otherwise open.
    return [
        "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "pipe", *source,
        "-i", "pipe:0",
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
        stderr=asyncio.subprocess.PIPE,
    )
    return _FfmpegDecoder(process)


# The feed


class ReplyFeed:
    """One reply on its way to a LiveTalking session. Never raises.

    async_open first; when it returned True, write each piece of audio as it comes and
    finish at the end (or abort when the speech failed). The sending runs in async_run.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        target: FeedTarget,
        extension: str,
        options: Mapping[str, Any],
        answering: Callable[[], bool] | None = None,
    ) -> None:
        self._hass = hass
        # Whether the assistant's turn still runs; None for a clip that is already whole.
        self._answering = answering
        self._target = target
        self._extension = extension
        self._options = dict(options)
        self.reply_id = ulid.ulid_now()
        self._socket: aiohttp.ClientWebSocketResponse | None = None
        self._decoder: Decoder | None = None
        self._audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._size = 0
        self._stopped = False
        # For the log: what was forwarded, to compare with what LiveTalking says it got.
        self._opened = _now()
        self._sent_bytes = 0

    async def async_open(self) -> bool:
        """Open the reply on LiveTalking. False when it did not say "ready" in time.

        The decoder is started first: a satellite that is told a reply is open leaves the
        speaking to its avatar, so nothing is opened for audio that cannot be decoded.
        Whatever ends this without "ready", a cancellation too, leaves nothing open.
        """
        ready = False
        try:
            self._decoder = await async_start_decoder(
                self._hass, self._extension, self._options
            )
            if self._decoder is None:
                _LOGGER.debug("Avatar stream: this audio cannot be decoded, not opened")
                return False
            async with asyncio.timeout(OPEN_TIMEOUT):
                self._socket = await async_get_clientsession(self._hass).ws_connect(
                    f"{self._target.base_url}/stream_audio",
                    params={"sessionid": self._target.session_id, "reply_id": self.reply_id},
                    heartbeat=HEARTBEAT,
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
        finally:
            if not ready:
                await self._async_release()
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
        """Decode and send until the reply ends, then close it.

        It ends when the audio was sent, and as soon as it cannot go on: LiveTalking
        cancelled or closed it, the session changed, a step failed, or RUN_LIMIT passed.
        It never waits for the decoder alone, whose input stops being read once nobody
        takes its output.
        """
        decoder = self._decoder
        tasks: list[asyncio.Task[None]] = []
        try:
            if decoder is None or self._socket is None:
                _LOGGER.debug("Avatar stream: reply %s was not opened, nothing sent", self.reply_id)
                return
            decode = asyncio.create_task(self._async_decode(decoder))
            send = asyncio.create_task(self._async_send(decoder))
            # The listener ends only when LiveTalking cancels or closes the reply.
            listen = asyncio.create_task(self._async_listen())
            tasks = [decode, send, listen]
            async with asyncio.timeout(RUN_LIMIT):
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if _ended_well(decode) and not send.done() and not listen.done():
                    # All of the audio is with the decoder: what it still gives is sent.
                    await asyncio.wait([send, listen], return_when=asyncio.FIRST_COMPLETED)
        except TimeoutError:
            _LOGGER.warning(
                "Avatar stream: reply %s did not end within %d s, closing it",
                self.reply_id, RUN_LIMIT,
            )
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
            for step, outcome in zip(
                ("decoding", "sending", "listening"),
                await asyncio.gather(*tasks, return_exceptions=True),
                strict=False,
            ):
                if isinstance(outcome, Exception):
                    _LOGGER.warning(
                        "Avatar stream: reply %s stopped, %s failed",
                        self.reply_id, step, exc_info=outcome,
                    )
            await self._async_release()

    async def _async_decode(self, decoder: Decoder) -> None:
        """Hand the engine's audio to the decoder until it ends, stalls or the session goes.

        A pause after the first audio holds the reply open (see HOLD_AFTER).
        """
        last_audio: float | None = None  # None until the engine gave any
        answered = self._opened  # When the assistant was last seen still answering
        holding = False
        while True:
            if last_audio is None:
                wait = min(HOLD_EVERY, STALL_TIMEOUT)
            else:
                wait = HOLD_EVERY if holding else HOLD_AFTER
            try:
                async with asyncio.timeout(wait):
                    audio = await self._audio.get()
            except TimeoutError:
                if self._stopped or self._session_changed():
                    break
                now = _now()
                if last_audio is not None:
                    paused, over = now - last_audio, now - last_audio >= HOLD_LIMIT
                else:
                    if self._answering is not None and self._answering():
                        answered = now
                    paused = now - self._opened
                    over = now - answered >= STALL_TIMEOUT or paused >= HOLD_LIMIT
                if over:
                    _LOGGER.debug(
                        "Avatar stream: no audio for %.1f s, ending the reply", paused
                    )
                    break
                if not holding:
                    _LOGGER.debug(
                        "Avatar stream: reply %s %s, holding it open",
                        self.reply_id,
                        "paused" if last_audio is not None else "has no audio yet",
                    )
                holding = True
                await self._async_hold()
                continue
            if audio is None or self._stopped or self._session_changed():
                break
            if holding:
                _LOGGER.debug(
                    "Avatar stream: reply %s goes on after a pause of %.1f s",
                    self.reply_id, _now() - (last_audio or _now()),
                )
            holding, last_audio = False, _now()
            await decoder.async_write(audio)
        await decoder.async_close_input()

    async def _async_hold(self) -> None:
        """Tell LiveTalking the reply is paused, not over. A failure ends nothing here:
        the listener notices a closed socket."""
        if self._socket is None:
            return
        with contextlib.suppress(aiohttp.ClientError, ConnectionError, RuntimeError):
            await self._socket.send_json({"type": "hold"})

    async def _async_send(self, decoder: Decoder) -> None:
        assert self._socket is not None  # noqa: S101
        async for pcm in decoder.output():
            if self._stopped or self._session_changed():
                return
            if not self._sent_bytes:
                _LOGGER.debug(
                    "Avatar stream: reply %s first audio forwarded %.2f s after it opened",
                    self.reply_id, _now() - self._opened,
                )
            try:
                async with asyncio.timeout(SEND_TIMEOUT):
                    await self._socket.send_bytes(pcm)
            except TimeoutError:
                _LOGGER.warning(
                    "Avatar stream: LiveTalking took no audio of reply %s for %d s, "
                    "ending it there",
                    self.reply_id, SEND_TIMEOUT,
                )
                self._stopped = True
                return
            self._sent_bytes += len(pcm)
        if not self._stopped:
            await self._socket.send_json({"type": "end"})

    async def _async_listen(self) -> None:
        """Returns when LiveTalking cancels the reply (an interrupt) or closes the stream."""
        assert self._socket is not None  # noqa: S101
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

    async def _async_release(self) -> None:
        """Stop the decoder and close the socket, whichever of them there is."""
        decoder, self._decoder = self._decoder, None
        if decoder is not None:
            with contextlib.suppress(Exception):
                await decoder.async_stop()
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()


def _ended_well(task: asyncio.Task[None]) -> bool:
    return task.done() and not task.cancelled() and task.exception() is None
