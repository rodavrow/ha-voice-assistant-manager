# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The chat stage: a chat show on a Google Cast device shows the avatars talking.

When a chat runs on a cast media player (a Nest Hub, a Chromecast or Google TV) and both
assistants have an avatar, the device is cast the stage dashboard with Home Assistant's own
cast.show_lovelace_view. Its one card (frontend/stage.js) opens a LiveTalking session per
avatar, through this module, since a cast page on https may not call LiveTalking on http
itself. The speaking avatar fills the screen and the other waits, idle, in an inset.

Each line goes to the speaking avatar's session only, and the device plays its voice from
the same stream as the lips, so the line's audio is not also played on the device. A line
ends when LiveTalking says that session stopped speaking. Whatever goes wrong (the stage is
not set up, no room on LiveTalking, the page does not connect in time, a session drops), the
chat goes on as before: the line is played as audio on the device.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
import wave
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .avatar import (
    CONF_AVATAR_ID,
    CONF_ENABLED,
    CONF_URL,
    MAX_SESSIONS,
    STAGE_DATA,
    SUBENTRY_TYPE,
    LiveTalkingClient,
    LiveTalkingError,
    device_sessions,
    normalize_url,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The dashboard the stage is cast from, and its card. Set up from the Voice Manager page.
DASHBOARD = "vam-chat-stage"
VIEW = "stage"
CARD = "vam-chat-stage-card"
SCRIPT_URL = f"/{DOMAIN}/stage.js"
CAST_PLATFORM = "cast"

# How long the device has to open the dashboard and connect both avatars: launching the
# cast app and logging in takes several seconds.
CONNECT_WAIT = 45.0
# How long LiveTalking may take to start speaking a line; the first line of a new session
# can take over ten seconds.
FIRST_SOUND = 15.0
POLL = 0.3
# How far the device's sound is behind LiveTalking: is_speaking turns false when LiveTalking
# has sent the last of a line, while it is still on its way and in the device's buffer.
TAIL = 1.0
# How long the avatars stay on screen after the chat, before the streams close and the cast
# is stopped: the last line is still finishing on the device.
LINGER = 3.0
CALL_TIMEOUT = 10.0
# The cast app the stage runs in, as the cast media player names it.
CAST_APP = "Home Assistant"


@dataclass
class StageSettings:
    """Kept with the Voice Manager page (panel.py)."""

    enabled: bool = True
    captions: bool = False

    @staticmethod
    def from_dict(data: Any) -> StageSettings:
        if not isinstance(data, Mapping):
            return StageSettings()
        return StageSettings(
            enabled=data.get("enabled", True) is not False,
            captions=data.get("captions", False) is True,
        )


def settings(hass: HomeAssistant) -> StageSettings:
    panel = hass.data.get(DOMAIN, {}).get("panel")
    saved = getattr(panel, "chat_stage", None)
    return saved if isinstance(saved, StageSettings) else StageSettings()


def is_cast_player(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether the media player is a Google Cast device (not a group of them: a group plays
    audio only)."""
    entity = er.async_get(hass).async_get(entity_id)
    return (
        entity is not None
        and entity.domain == "media_player"
        and entity.platform == CAST_PLATFORM
        and hass.states.get(entity_id) is not None
    )


def setup_problem(hass: HomeAssistant) -> str:
    """"" when the stage dashboard and its card are set up, else what is missing."""
    from homeassistant.components.lovelace.const import LOVELACE_DATA  # noqa: PLC0415

    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        return "no_dashboards"
    if DASHBOARD not in lovelace.dashboards:
        return "no_dashboard"
    resources = lovelace.resources
    if getattr(lovelace, "resource_mode", "storage") == "storage":
        items = resources.async_items() if hasattr(resources, "async_items") else []
        if not any(str(item.get("url", "")).startswith(SCRIPT_URL) for item in items):
            return "no_resource"
    return ""


@dataclass
class Actor:
    """One assistant on the stage."""

    key: str
    name: str
    url: str
    avatar_id: str
    session: str = ""
    connected: asyncio.Event = field(default_factory=asyncio.Event)

    def public(self) -> dict[str, str]:
        return {"key": self.key, "name": self.name}


@dataclass
class Show:
    """The chat on the stage."""

    token: str
    speaker: str
    actors: dict[str, Actor]
    captions: bool
    speaking: str = ""
    text: str = ""
    # The page lost a session, or a line could not be spoken: the rest of the chat is audio.
    broken: bool = False

    def client(self, hass: HomeAssistant, actor: Actor) -> LiveTalkingClient:
        return LiveTalkingClient(async_get_clientsession(hass), actor.url)


Listener = Callable[[dict[str, Any]], None]


class Stage:
    """The chat on the stage, if any, and the cards watching it."""

    def __init__(self) -> None:
        self.show: Show | None = None
        self._listeners: set[Listener] = set()

    def sessions(self) -> frozenset[str]:
        show = self.show
        if show is None:
            return frozenset()
        return frozenset(actor.session for actor in show.actors.values() if actor.session)

    def snapshot(self) -> dict[str, Any]:
        show = self.show
        if show is None:
            return {"event": "idle"}
        return {
            "event": "start",
            "token": show.token,
            "actors": [actor.public() for actor in show.actors.values()],
            "captions": show.captions,
            "speaking": show.speaking,
            "text": show.text if show.captions else "",
        }

    @callback
    def listen(self, listener: Listener) -> Callable[[], None]:
        self._listeners.add(listener)
        listener(self.snapshot())

        @callback
        def stop() -> None:
            self._listeners.discard(listener)

        return stop

    @callback
    def publish(self, message: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            listener(message)


def get_stage(hass: HomeAssistant) -> Stage:
    data = hass.data.setdefault(DOMAIN, {})
    stage = data.get(STAGE_DATA)
    if stage is None:
        stage = data[STAGE_DATA] = Stage()
    return stage


def _actor(entry: ConfigEntry, name: str) -> Actor | None:
    """The assistant's avatar as a stage actor, or None if it has none that is on."""
    avatar = next(
        (
            sub.data
            for sub in entry.subentries.values()
            if sub.subentry_type == SUBENTRY_TYPE and sub.data.get(CONF_ENABLED)
        ),
        None,
    )
    url = normalize_url(str(avatar.get(CONF_URL) or "")) if avatar else None
    if avatar is None or url is None:
        return None
    return Actor(entry.entry_id, name, url, str(avatar.get(CONF_AVATAR_ID) or ""))


def _refusal(hass: HomeAssistant, speaker: str, actors: list[Actor | None]) -> str:
    """Why this chat is not shown on the stage, as a log line, or "" when it is."""
    if not is_cast_player(hass, speaker):
        return ""  # Not a cast device: nothing to say, it never had a stage.
    if not settings(hass).enabled:
        return "it is switched off on the Voice Manager page"
    if any(actor is None for actor in actors):
        return "both assistants need an avatar that is on"
    if problem := setup_problem(hass):
        return f"the stage is not set up ({problem}); set it up under Chat show"
    room = MAX_SESSIONS - len(device_sessions(hass))
    if room < len(actors):
        return f"LiveTalking has room for {MAX_SESSIONS} sessions and {room} are free"
    return ""


async def async_open(
    hass: HomeAssistant, speaker: str, entries: Iterable[ConfigEntry], names: Iterable[str]
) -> Show | None:
    """Cast the stage to the speaker and wait until both avatars are connected.

    None when the chat is not shown there; the chat then plays as audio. Never raises,
    except when cancelled.
    """
    stage = get_stage(hass)
    actors = [_actor(entry, name) for entry, name in zip(entries, names, strict=True)]
    if not is_cast_player(hass, speaker):
        return None
    if why := _refusal(hass, speaker, actors):
        _LOGGER.info("Chat stage: not shown on %s, %s; the chat plays as audio", speaker, why)
        return None
    cast = [actor for actor in actors if actor is not None]
    show = Show(
        token=uuid.uuid4().hex,
        speaker=speaker,
        actors={actor.key: actor for actor in cast},
        captions=settings(hass).captions,
    )
    stage.show = show
    stage.publish(stage.snapshot())
    try:
        async with asyncio.timeout(CALL_TIMEOUT):
            await hass.services.async_call(
                "cast",
                "show_lovelace_view",
                {"entity_id": speaker, "dashboard_path": DASHBOARD, "view_path": VIEW},
                blocking=True,
            )
        async with asyncio.timeout(CONNECT_WAIT):
            await asyncio.gather(*(actor.connected.wait() for actor in cast))
    except (HomeAssistantError, TimeoutError) as err:
        missing = [actor.name for actor in cast if not actor.connected.is_set()]
        _LOGGER.warning(
            "Chat stage: %s did not show the avatars (%s); the chat plays as audio",
            speaker,
            f"{', '.join(missing)} not connected after {CONNECT_WAIT:.0f} s"
            if isinstance(err, TimeoutError)
            else err,
        )
        _end(stage, show)
        return None
    except asyncio.CancelledError:
        _end(stage, show)
        raise
    _LOGGER.info("Chat stage: %s shows %s", speaker, " and ".join(a.name for a in cast))
    return show


@callback
def _end(stage: Stage, show: Show) -> None:
    if stage.show is show:
        stage.show = None
        stage.publish({"event": "end", "token": show.token})


def clip_seconds(extension: str, audio: bytes) -> float | None:
    """How long a clip (as prepare_clip gives it) plays, or None when that is not known."""
    if extension == "wav":
        with contextlib.suppress(wave.Error, EOFError, ValueError):
            with wave.open(io.BytesIO(audio)) as wav:
                frame = wav.getsampwidth() * wav.getnchannels()
                rate = wav.getframerate()
                if not frame or not rate:
                    return None
                # A streamed WAV header often claims more samples than the clip has.
                samples = len(audio) - (audio.find(b"data") + 8)
                frames = min(wav.getnframes(), samples // frame)
                return frames / rate
        return None
    if extension == "mp3":
        try:
            from mutagen import MutagenError  # noqa: PLC0415 - Home Assistant's tts has it
            from mutagen.mp3 import MP3  # noqa: PLC0415
        except ImportError:
            return None
        with contextlib.suppress(MutagenError, ValueError):
            length = float(MP3(io.BytesIO(audio)).info.length)
            return length if length > 0 else None
    return None


async def async_say(
    hass: HomeAssistant, show: Show, key: str, line: str, extension: str, audio: bytes,
    timeout: float, emotion: str | None = None,
) -> bool:
    """Have the assistant's avatar say the line, and return once it has.

    False when it could not be sent: the caller plays it as audio, and the stage is given
    up for the rest of the chat.
    """
    actor = show.actors.get(key)
    if actor is None or not actor.session or show.broken:
        return False
    stage = get_stage(hass)
    show.speaking, show.text = key, line
    stage.publish(
        {"event": "line", "token": show.token, "speaking": key,
         "text": line if show.captions else ""}
    )
    client = show.client(hass, actor)
    try:
        await client.async_say(actor.session, extension, audio, emotion=emotion)
        await _async_wait_spoken(client, actor, timeout, clip_seconds(extension, audio))
    except (aiohttp.ClientError, TimeoutError, LiveTalkingError) as err:
        _LOGGER.warning(
            "Chat stage: %s could not say a line on %s (%s); the chat plays as audio",
            actor.name, show.speaker, type(err).__name__,
        )
        show.broken = True
        return False
    return True


async def _async_wait_spoken(
    client: LiveTalkingClient, actor: Actor, timeout: float, length: float | None = None
) -> None:
    """Wait until the session has started and then stopped speaking, and the device has
    played the line: at least the clip's `length` after it started, then TAIL. At most
    `timeout` after it started, or the clip's length if that is longer."""
    limit = max(timeout, length or 0.0)
    sent = time.monotonic()
    began: float | None = None
    while True:
        now = time.monotonic()
        speaking = await client.async_speaking(actor.session)
        if speaking and began is None:
            began = now
        if began is None and now - sent > FIRST_SOUND:
            _LOGGER.warning(
                "Chat stage: %s did not start speaking within %.0f s", actor.name, FIRST_SOUND
            )
            return
        if began is not None and (not speaking or now - began > limit):
            # Speaking started at most one poll before it was seen, so this is never short.
            left = began + length - now if length else 0.0
            _LOGGER.debug(
                "Chat stage: %s's line: clip %s, speaking seen %.1f s after it was sent,"
                " %s %.1f s later, waiting %.1f s more",
                actor.name, f"{length:.1f} s" if length else "of unknown length",
                began - sent, "stopped" if not speaking else "still speaking",
                now - began, max(left, 0.0) + TAIL,
            )
            await asyncio.sleep(max(left, 0.0) + TAIL)
            return
        await asyncio.sleep(POLL)


async def async_close(hass: HomeAssistant, show: Show) -> None:
    """End the stage a moment after the last line: the page closes its sessions, and the
    cast stops, if the device still shows it. Never raises."""
    stage = get_stage(hass)
    try:
        await asyncio.sleep(LINGER)
    finally:
        _end(stage, show)
    if stage.show is not None and stage.show.speaker == show.speaker:
        return  # A new chat shows the stage there now.
    state = hass.states.get(show.speaker)
    app = str(state.attributes.get("app_name") or "") if state is not None else ""
    if CAST_APP not in app:
        return  # Something else is on the device now.
    try:
        async with asyncio.timeout(CALL_TIMEOUT):
            await hass.services.async_call(
                "media_player", "turn_off", {"entity_id": show.speaker}, blocking=True
            )
    except (HomeAssistantError, TimeoutError) as err:
        _LOGGER.debug("Chat stage: could not stop the cast on %s: %s", show.speaker, err)


# What the card asks for (stage_api.py)


class StageRefused(Exception):
    """The card asked about a chat that is not on the stage."""


def _current(hass: HomeAssistant, token: str, key: str) -> tuple[Show, Actor]:
    show = get_stage(hass).show
    actor = show.actors.get(key) if show is not None and show.token == token else None
    if show is None or actor is None:
        raise StageRefused("no such chat on the stage")
    return show, actor


async def async_offer(hass: HomeAssistant, token: str, key: str, sdp: str) -> str:
    """Open a LiveTalking session for the card's offer, for one assistant: the answer SDP.

    Raises StageRefused, or LiveTalking's error.
    """
    show, actor = _current(hass, token, key)
    async with asyncio.timeout(CALL_TIMEOUT):
        answer, session = await show.client(hass, actor).async_offer(sdp, actor.avatar_id)
    if actor.session and actor.session != session:
        _LOGGER.debug("Chat stage: %s has a new session %s", actor.name, session)
    actor.session = session
    actor.connected.clear()
    _LOGGER.debug("Chat stage: %s opened session %s", actor.name, session)
    return answer


@callback
def connected(hass: HomeAssistant, token: str, key: str, up: bool) -> None:
    """The card reports one assistant's stream connected, or lost."""
    show, actor = _current(hass, token, key)
    if up:
        actor.connected.set()
        return
    actor.connected.clear()
    if not show.broken:
        _LOGGER.warning(
            "Chat stage: %s lost the stream of %s; the chat plays as audio",
            show.speaker, actor.name,
        )
    show.broken = True
