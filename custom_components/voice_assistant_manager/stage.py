# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The chat stage: a chat show on a Google Cast device shows the avatars talking.

When a chat runs on a cast media player (a Nest Hub, a Chromecast or Google TV) and both
assistants have an avatar, the device is cast the stage dashboard with Home Assistant's own
cast.show_lovelace_view. Its one card (frontend/stage.js) opens a LiveTalking session per
avatar, through this module, since a cast page on https may not call LiveTalking on http
itself. The speaking avatar fills the screen and the other waits, idle, in an inset.

A chat started on "This device" in the Voice Manager page is shown by that page instead:
its speaker is the page (page_speaker), nothing is cast, and only that page is told of it.
Such a chat has nowhere else to play, so where a cast chat falls back to audio it ends.

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
import re
import time
import uuid
import wave
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import data_keys
from .avatar import (
    CONF_AVATAR_ID,
    CONF_URL,
    MAX_SESSIONS,
    STAGE_DATA,
    LiveTalkingClient,
    LiveTalkingError,
    enabled_avatar,
    normalize_url,
    present_device_sessions,
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
# The same for the Voice Manager page, which is open already.
PAGE_CONNECT_WAIT = 20.0
# How long the page may be away (its websocket dropped) before its chat ends.
PAGE_GONE_AFTER = 8.0
PAGE_POLL = 0.05
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


# The speaker of a chat shown by the Voice Manager page: "page:" and the id the page made
# up for itself, so that only it is told of the chat.
_PAGE = re.compile(r"page:([0-9a-f]{16,64})")
VIEWER = r"^[0-9a-f]{16,64}$"


def page_speaker(viewer: str) -> str:
    return f"page:{viewer}"


def viewer_of(speaker: str) -> str:
    """The page a chat's speaker stands for, or "" when it is a device."""
    match = _PAGE.fullmatch(speaker)
    return match.group(1) if match else ""


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
    panel = hass.data.get(DOMAIN, {}).get(data_keys.PANEL)
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
    # The page that shows it, or "" for the card cast to the speaker.
    viewer: str = ""
    speaking: str = ""
    text: str = ""
    # The page lost a session, or a line could not be spoken: the rest of the chat is audio,
    # or the chat ends when a page shows it.
    broken: bool = False

    def client(self, hass: HomeAssistant, actor: Actor) -> LiveTalkingClient:
        return LiveTalkingClient(async_get_clientsession(hass), actor.url)


Listener = Callable[[dict[str, Any]], None]


class Stage:
    """The chat on the stage, if any, and the cards watching it.

    A card watches as a viewer: the Voice Manager page under the id it made up, the card
    cast to a device as "". Each is told only of the chat that is shown by it.
    """

    def __init__(self) -> None:
        self.show: Show | None = None
        self._listeners: dict[Listener, str] = {}

    def sessions(self) -> frozenset[str]:
        show = self.show
        if show is None:
            return frozenset()
        return frozenset(actor.session for actor in show.actors.values() if actor.session)

    def snapshot(self, viewer: str = "") -> dict[str, Any]:
        show = self.show
        if show is None or show.viewer != viewer:
            return {"event": "idle"}
        return {
            "event": "start",
            "token": show.token,
            "actors": [actor.public() for actor in show.actors.values()],
            "captions": show.captions,
            "speaking": show.speaking,
            "text": show.text if show.captions else "",
        }

    def watched(self, viewer: str) -> bool:
        """Whether a card is watching as this viewer."""
        return viewer in self._listeners.values()

    @callback
    def listen(self, listener: Listener, viewer: str = "") -> Callable[[], None]:
        self._listeners[listener] = viewer
        listener(self.snapshot(viewer))

        @callback
        def stop() -> None:
            self._listeners.pop(listener, None)

        return stop

    @callback
    def publish(self, show: Show, message: dict[str, Any]) -> None:
        """Tell the cards that show this chat."""
        for listener, viewer in list(self._listeners.items()):
            if viewer == show.viewer:
                listener(message)


def get_stage(hass: HomeAssistant) -> Stage:
    data = data_keys.domain_data(hass)
    stage: Stage | None = data.get(STAGE_DATA)
    if stage is None:
        stage = data[STAGE_DATA] = Stage()
    return stage


def _actor(entry: ConfigEntry, name: str) -> Actor | None:
    """The assistant's avatar as a stage actor, or None if it has none that is on."""
    avatar = enabled_avatar(entry)
    url = normalize_url(str(avatar.get(CONF_URL) or "")) if avatar else None
    if avatar is None or url is None:
        return None
    return Actor(entry.entry_id, name, url, str(avatar.get(CONF_AVATAR_ID) or ""))


def _room(hass: HomeAssistant) -> int:
    """How many sessions LiveTalking has free, going by the displays that are there now:
    one that was unplugged holds nothing on LiveTalking."""
    return MAX_SESSIONS - len(present_device_sessions(hass))


def page_refusal(hass: HomeAssistant, speaker: str, entries: Iterable[ConfigEntry]) -> str:
    """Why a chat cannot be shown by the page that is its speaker, as an error key, or "".

    Asked before the chat starts: the person is told, since such a chat cannot play as audio.
    """
    if any(_actor(entry, entry.title) is None for entry in entries):
        return "chat_page_no_avatar"
    if _room(hass) < 2:
        return "chat_page_no_room"
    if not get_stage(hass).watched(viewer_of(speaker)):
        return "chat_page_gone"
    return ""


async def async_page_watching(hass: HomeAssistant, show: Show) -> bool:
    """Whether the page still watches its chat, waiting PAGE_GONE_AFTER for one that is away:
    a websocket that dropped for a moment subscribes again."""
    stage = get_stage(hass)
    waited = 0.0
    while not stage.watched(show.viewer):
        if waited >= PAGE_GONE_AFTER:
            return False
        await asyncio.sleep(PAGE_POLL)
        waited += PAGE_POLL
    return True


def _refusal(hass: HomeAssistant, speaker: str, actors: list[Actor | None]) -> str:
    """Why this chat is not shown on the stage, as a log line, or "" when it is."""
    if viewer_of(speaker):
        return ""  # Asked before the chat started (page_refusal).
    if not is_cast_player(hass, speaker):
        return ""  # Not a cast device: nothing to say, it never had a stage.
    if not settings(hass).enabled:
        return "it is switched off on the Voice Manager page"
    if any(actor is None for actor in actors):
        return "both assistants need an avatar that is on"
    if problem := setup_problem(hass):
        return f"the stage is not set up ({problem}); set it up under Chat show"
    room = _room(hass)
    if room < len(actors):
        return f"LiveTalking has room for {MAX_SESSIONS} sessions and {room} are free"
    return ""


async def async_open(
    hass: HomeAssistant, speaker: str, entries: Iterable[ConfigEntry], names: Iterable[str]
) -> Show | None:
    """Cast the stage to the speaker, or show it on the page that is the speaker, and wait
    until both avatars are connected.

    None when the chat is not shown there; the chat then plays as audio, or ends when its
    speaker is a page. Never raises, except when cancelled.
    """
    stage = get_stage(hass)
    actors = [_actor(entry, name) for entry, name in zip(entries, names, strict=True)]
    viewer = viewer_of(speaker)
    if not viewer and not is_cast_player(hass, speaker):
        return None
    if viewer and any(actor is None for actor in actors):
        return None  # An avatar was switched off since the chat was let start.
    if why := _refusal(hass, speaker, actors):
        # A warning: the person sees a chat without the avatars and needs to know why.
        _LOGGER.warning(
            "Chat stage: not shown on %s, %s; the chat plays as audio", speaker, why
        )
        return None
    cast = [actor for actor in actors if actor is not None]
    show = Show(
        token=uuid.uuid4().hex,
        speaker=speaker,
        actors={actor.key: actor for actor in cast},
        captions=settings(hass).captions,
        viewer=viewer,
    )
    stage.show = show
    stage.publish(show, stage.snapshot(viewer))
    wait = PAGE_CONNECT_WAIT if viewer else CONNECT_WAIT
    try:
        if not viewer:
            async with asyncio.timeout(CALL_TIMEOUT):
                await hass.services.async_call(
                    "cast",
                    "show_lovelace_view",
                    {"entity_id": speaker, "dashboard_path": DASHBOARD, "view_path": VIEW},
                    blocking=True,
                )
        async with asyncio.timeout(wait):
            await asyncio.gather(*(actor.connected.wait() for actor in cast))
    except (HomeAssistantError, TimeoutError) as err:
        missing = [actor.name for actor in cast if not actor.connected.is_set()]
        _LOGGER.warning(
            "Chat stage: %s did not show the avatars (%s); the chat %s",
            _where(show),
            f"{', '.join(missing)} not connected after {wait:.0f} s"
            if isinstance(err, TimeoutError)
            else err,
            _then(show),
        )
        _end(stage, show)
        return None
    except asyncio.CancelledError:
        _end(stage, show)
        raise
    _LOGGER.info("Chat stage: %s shows %s", _where(show), " and ".join(a.name for a in cast))
    return show


def _where(show: Show) -> str:
    """The stage's place for a log line: a page's id says nothing to a person."""
    return "the Voice Manager page" if show.viewer else show.speaker


def _then(show: Show) -> str:
    """What becomes of a chat whose stage failed, for a log line."""
    return "ends" if show.viewer else "plays as audio"


@callback
def _end(stage: Stage, show: Show) -> None:
    if stage.show is show:
        stage.show = None
        stage.publish(show, {"event": "end", "token": show.token})


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
                return float(frames / rate)
        return None
    if extension == "mp3":
        try:
            from mutagen import MutagenError  # noqa: PLC0415 - Home Assistant's tts has it
            from mutagen.mp3 import MP3  # noqa: PLC0415
        except ImportError:
            return None
        with contextlib.suppress(MutagenError, ValueError):
            # mutagen has no type information; a file it could read always has `info`.
            info: Any = MP3(io.BytesIO(audio)).info  # type: ignore[no-untyped-call]
            length = float(info.length)
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
        show,
        {"event": "line", "token": show.token, "speaking": key,
         "text": line if show.captions else ""}
    )
    client = show.client(hass, actor)
    try:
        await client.async_say(actor.session, extension, audio, emotion=emotion)
        await _async_wait_spoken(client, actor, timeout, clip_seconds(extension, audio))
    except (aiohttp.ClientError, TimeoutError, LiveTalkingError) as err:
        _LOGGER.warning(
            "Chat stage: %s could not say a line on %s (%s); the chat %s",
            actor.name, _where(show), type(err).__name__, _then(show),
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
    if show.viewer:
        return  # Nothing was cast.
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
            "Chat stage: %s lost the stream of %s; the chat %s",
            _where(show), actor.name, _then(show),
        )
    show.broken = True
