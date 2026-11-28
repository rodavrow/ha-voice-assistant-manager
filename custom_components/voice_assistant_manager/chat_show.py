# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The chat show: two assistants talk to each other on one speaker, for fun.

Each line is written by that router's agent without tools (announce.writer_agent), in a
conversation of its own, so no tool, no main agent, no memory and no shared history is ever
involved. The topic and lines from people are passed as conversation data in the message,
never in the system prompt. Every chat ends: a line limit, a time limit, a stop, an error,
or the router unloading or Home Assistant stopping.
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypedDict

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import DATA_CHAT_LOGS, AssistantContent
from homeassistant.components.media_player import MediaPlayerEntityFeature, MediaType
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE
from homeassistant.core import (
    CALLBACK_TYPE,
    Context,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import announcement_look, avatar_chat, avatar_hold, data_keys, emotion, stage
from .announce import writer_agent
from .avatar import prepare_clip

# Re-exported: tests and older imports use these from here.
from .chat_text import GUEST as GUEST
from .chat_text import LIMIT_RANGES as LIMIT_RANGES
from .chat_text import ChatLimits as ChatLimits
from .chat_text import ChatLine as ChatLine
from .chat_text import chat_instructions as chat_instructions
from .chat_text import chat_message as chat_message
from .chat_text import fit_line as fit_line
from .chat_text import is_fragment as is_fragment
from .chat_text import is_stop_phrase as is_stop_phrase
from .const import DOMAIN
from .helpers import speech_text
from .memory import clean_text
from .reminders import assistant_name
from .setup_guide import Satellite, find_satellites

_LOGGER = logging.getLogger(__name__)

DATA_MANAGER = data_keys.CHAT_SHOW
MAX_TOPIC_LENGTH = 200
MAX_PERSON_LINE_LENGTH = 200
# Seconds an agent gets to write one line.
LINE_TIMEOUT = 20
# How long a busy satellite is waited for, checking every BUSY_POLL seconds.
BUSY_POLL = 0.5
BUSY_WAIT = 20.0
# How long a satellite whose connection dropped is waited for. A satellite that sends its
# entities again (after a firmware update, say) is away for a fraction of a second.
RETURN_WAIT = 15.0
# How long a line takes to say, per word: a satellite that dropped while a line played goes
# on playing it, but says it is idle, so the rest of the line is waited for by the clock.
SPOKEN_SECONDS_PER_WORD = 0.4
# A satellite announcement blocks until it has played; this only stops a hung one.
ANNOUNCE_TIMEOUT_BASE = 30.0
# How long a media player gets to finish a line: base plus seconds per word.
PLAY_TIMEOUT_BASE = 5.0
PLAY_TIMEOUT_PER_WORD = 0.6
# AssistSatelliteEntityFeature.ANNOUNCE (assist_satellite/const.py). Importing
# assist_satellite would import the whole audio pipeline just for this number.
SATELLITE_ANNOUNCE = 1
# AssistSatelliteEntityFeature.START_CONVERSATION.
SATELLITE_START_CONVERSATION = 2
# After a line that opens the microphone: how long the satellite is given to start
# listening (Home Assistant sets it idle first), then how long to be idle again.
LISTEN_START_WAIT = 2.0
LISTEN_WAIT = 10.0
# How long the speaker's device stays in the chat after it ended: a reply to the last line,
# or one after a stop, still gets nothing or "Okay." and never reaches the main agent.
REPLY_GRACE = LISTEN_START_WAIT + LISTEN_WAIT + 5.0
SATELLITE_IDLE = "idle"
# AssistSatelliteState: listening from the pipeline's STT_START, responding while Home
# Assistant announces (assist_satellite entity.py).
SATELLITE_LISTENING = "listening"
SATELLITE_RESPONDING = "responding"
# The assistants say each other's names, which are wake words, so the speaker may wake on
# its own line. Listening that starts while a line plays, or this many seconds after it,
# is such a self-wake: its request is answered with silence and the chat goes on.
SELF_WAKE_MARGIN = 1.5
# How long after the woken line ended the next line waits for the satellite's request,
# and how long after the wake that request is recognised as one (also after the grace).
# The line plays on through a wake, so the request may come long after the wake itself.
SELF_WAKE_WAIT = LISTEN_WAIT
SELF_WAKE_TTL = 20.0
# After this many lines in a row cut by a self-wake, say so in the log once.
SELF_WAKE_WARN_AFTER = 3
PLAYER_PLAYING = "playing"
TTS_VOICE = "voice"  # tts.ATTR_VOICE
TTS_FORMAT = "preferred_format"  # tts.ATTR_PREFERRED_FORMAT
# What a line on the stage is made as. LiveTalking reads a WAV's length exactly; an MP3
# that Home Assistant converted on the fly has no length in it, and LiveTalking's decoder
# guessed it short and dropped the end of the line.
STAGE_FORMAT = "wav"
# How long a chat voice may take to make a line's audio before the line is spoken without.
CHAT_VOICE_TIMEOUT = 20
# Not a chat end reason: the conversation could not start, so the line is announced.
_LISTEN_UNAVAILABLE = "listen_unavailable"
# The reasons a chat ends as meant; any other is a failure.
_NORMAL_ENDS = frozenset({"chat_done", "chat_time_up", "chat_stopped"})

class ChatState(TypedDict):
    """The chat as the page shows it. `ended` is "" while running, else the reason key.

    `assistants` are the names shown and used in the lines; `assistant_ids` the routers'
    entry ids, in the same order.
    """

    running: bool
    speaker: str
    assistants: list[str]
    assistant_ids: list[str]
    topic: str
    lines: list[dict[str, str]]
    ended: str


class ChatRefused(Exception):
    """A chat could not start. `key` is an error key under options.error."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def character_of(entry: ConfigEntry) -> str:
    """How this assistant behaves in a chat: its chat character, else its announcement text."""
    character = str((entry.options.get("chat") or {}).get("character") or "").strip()
    if character:
        return character
    return str((entry.options.get("reminders") or {}).get("persona") or "").strip()


def assistant_display_name(entry: ConfigEntry) -> str:
    """The name the router goes by as an assistant, capitalised: "Jeeves", "Mary Ann".

    Falls back to the router title when the title gives no name at all.
    """
    setting = (entry.options.get("reminders") or {}).get("assistant")
    name = assistant_name(entry.title, setting)
    return " ".join(word[:1].upper() + word[1:] for word in name.split()) or entry.title


def play_timeout(words: int) -> float:
    """Seconds a media player gets to finish a line of this many words."""
    return PLAY_TIMEOUT_BASE + PLAY_TIMEOUT_PER_WORD * words


def _loaded_router(hass: HomeAssistant, entry_id: str) -> ConfigEntry | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        return None
    return entry


def _features(hass: HomeAssistant, entity_id: str) -> int:
    state = hass.states.get(entity_id)
    try:
        return int(state.attributes.get("supported_features", 0)) if state else 0
    except (TypeError, ValueError):
        return 0


def can_speak(hass: HomeAssistant, speaker: str) -> bool:
    """Whether the entity exists and can announce (satellite) or play media (media player)."""
    if hass.states.get(speaker) is None:
        return False
    domain = speaker.split(".", 1)[0]
    if domain == "assist_satellite":
        return bool(_features(hass, speaker) & SATELLITE_ANNOUNCE)
    if domain == "media_player":
        return bool(_features(hass, speaker) & MediaPlayerEntityFeature.PLAY_MEDIA)
    return False


def _router_satellites(hass: HomeAssistant) -> list[Satellite]:
    """Each satellite once per loaded router, with whether its wake words use that router."""
    return [
        satellite
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        for satellite in find_satellites(hass, entry)
    ]


def voice_satellites(hass: HomeAssistant) -> set[str]:
    """Satellites with a wake word whose pipeline uses a loaded router: people can join in."""
    return {
        satellite["entity_id"]
        for satellite in _router_satellites(hass)
        if any(word["uses_router"] for word in satellite["wake_words"])
    }


def listens_through_router(hass: HomeAssistant, speaker: str) -> bool:
    """Whether what the satellite hears after start_conversation reaches a loaded router.

    That conversation runs on the satellite's first pipeline (the one its first pipeline
    select names), so that pipeline must use a router; any other would hand the reply to
    an agent with tools.
    """
    return any(
        satellite["entity_id"] == speaker
        and bool(satellite["wake_words"])
        and satellite["wake_words"][0]["uses_router"]
        for satellite in _router_satellites(hass)
    )


def can_listen(hass: HomeAssistant, speaker: str) -> bool:
    """Whether the speaker is a satellite that can start a conversation a router hears."""
    return (
        speaker.startswith("assist_satellite.")
        and hass.states.get(speaker) is not None
        and bool(_features(hass, speaker) & SATELLITE_START_CONVERSATION)
        and listens_through_router(hass, speaker)
    )


def _is_busy_error(err: BaseException) -> bool:
    """Whether the satellite refused because it is busy (assist_satellite SatelliteBusyError).

    Matched by name for the same reason as SATELLITE_ANNOUNCE.
    """
    return any(cls.__name__ == "SatelliteBusyError" for cls in type(err).__mro__)


def _tts_media_id(
    hass: HomeAssistant, line: str, engine: str | None, voice: str, fmt: str | None = None
) -> str | None:
    """A text-to-speech media source id for the line, or None if it cannot be made.

    `fmt` asks Home Assistant for the audio in that format, whatever the engine makes.
    """
    from homeassistant.components import tts  # noqa: PLC0415 - only needed when speaking

    options: dict[str, Any] | None = {TTS_VOICE: voice} if voice else None
    if fmt:
        options = {**(options or {}), TTS_FORMAT: fmt}
    try:
        return tts.generate_media_source_id(hass, line, engine, None, options)
    except HomeAssistantError as err:
        _LOGGER.warning("No chat voice from %s: %s", engine or "the default engine", err)
        return None


async def _async_chat_media_id(
    hass: HomeAssistant, line: str, engine: str, voice: str, fmt: str | None = None
) -> str | None:
    """A media source id for the line in the chat voice, or None if it makes no audio.

    The audio is made here, before the line is announced: Home Assistant hands a speaker an
    empty file when an engine fails (a voice it does not have, a provider that is down), and
    the line would be silent. Home Assistant keeps the audio, so it is made only once.
    """
    from homeassistant.components import tts  # noqa: PLC0415 - only needed when speaking

    media_id = _tts_media_id(hass, line, engine, voice, fmt)
    if media_id is None:
        return None
    try:
        async with asyncio.timeout(CHAT_VOICE_TIMEOUT):
            _extension, audio = await tts.async_get_media_source_audio(hass, media_id)
    except TimeoutError:
        reason = f"no audio within {CHAT_VOICE_TIMEOUT} s"
    except Exception as err:  # noqa: BLE001 - an engine may raise anything; the line still plays
        reason = f"{type(err).__name__}, see the text-to-speech error in the log"
    else:
        if audio:
            return media_id
        reason = "the audio is empty"
    _LOGGER.warning(
        "The chat voice %s of %s made no audio (%s), so this line is spoken without it. "
        "Check the engine and voice under Chat show on the Voice Manager page",
        voice or "(the engine's default)",
        engine,
        reason,
    )
    return None


def _chat_voice(entry: ConfigEntry) -> tuple[str, str]:
    chat = entry.options.get("chat") or {}
    return str(chat.get("tts_engine") or "").strip(), str(chat.get("voice") or "").strip()


@dataclass
class _Chat:
    """One chat, running or ended."""

    routers: tuple[str, str]
    # The assistants' names, as in assistant_display_name.
    names: tuple[str, str]
    speaker: str
    topic: str
    limits: ChatLimits
    started: datetime
    lines: list[ChatLine] = field(default_factory=list)
    ended: str = ""
    # True while a line is being spoken, so a stop lets it finish.
    speaking: bool = False
    task: asyncio.Task[None] | None = None
    unsubscribe: list[CALLBACK_TYPE] = field(default_factory=list)
    # The line being or last spoken on a satellite: whether it opens the microphone, whether
    # the satellite was seen responding (playing it), and when it finished playing.
    line_listens: bool = False
    line_audible: bool = False
    line_finished: datetime | None = None
    # Whether the satellite woke on the line being or last spoken.
    line_woken: bool = False
    # The satellite went unavailable while the line was being spoken.
    line_dropped: bool = False
    # When the satellite last woke on its own line, until that request is answered.
    self_wake_at: datetime | None = None
    wake_answered: asyncio.Event = field(default_factory=asyncio.Event)
    woken_in_a_row: int = 0
    # The avatar connection selects this chat holds at "Always connected" (avatar_chat).
    avatar_selects: list[str] = field(default_factory=list)
    # The chat shown on a cast device, with the avatars talking (stage.py), if it is.
    on_stage: stage.Show | None = None
    # The emotion of the line being or last spoken, from its tag (emotion.py).
    line_emotion: emotion.Emotion = emotion.NEUTRAL
    # That line as its chat voice is given it: with audio tags, for a voice that takes them.
    line_voiced: str = ""
    # How that line's emotion was chosen, for the log (emotion.split_face).
    line_why: str = ""

    def add(self, who: str, text: str) -> None:
        self.lines.append(ChatLine(who, text, dt_util.utcnow().isoformat()))


@dataclass(frozen=True)
class _Prepared:
    """A line written ahead, and how many lines the chat had when it was written."""

    heard: int
    line: str = ""
    emotion: emotion.Emotion = emotion.NEUTRAL
    voiced: str = ""
    why: str = ""


class ChatShowManager:
    """Runs at most one chat at a time."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._chat: _Chat | None = None
        # Until when each speaker of an ended chat stays in it (REPLY_GRACE), kept apart
        # from the current chat so a chat elsewhere does not end it.
        self._grace: dict[str, datetime] = {}
        # Conversations a line spoken with start_conversation opened: Home Assistant keeps
        # that line in their history, and the satellite keeps using them for up to
        # CONVERSATION_TIMEOUT after its last request. Each maps to the conversation the
        # main agent gets instead (None until it has one).
        self._chat_conversations: dict[str, str | None] = {}
        # Watches the last chat's satellite for self-wakes. It outlives the chat, so a wake
        # just after the last line is still seen, and is replaced when the next chat starts.
        self._unwatch_wakes: CALLBACK_TYPE | None = None

    async def async_start(
        self, a: str, b: str, speaker: str, topic: str, limits: ChatLimits
    ) -> None:
        """Start a chat between routers `a` and `b` (entry ids) on a speaker entity.

        Raises ChatRefused with the reason when it cannot start.
        """
        if self._chat is not None and self._chat.task is not None and not self._chat.task.done():
            raise ChatRefused("chat_running")
        if a == b:
            raise ChatRefused("chat_same_assistant")
        first, second = _loaded_router(self.hass, a), _loaded_router(self.hass, b)
        if first is None or second is None:
            raise ChatRefused("chat_no_router")
        if writer_agent(self.hass, first) is None or writer_agent(self.hass, second) is None:
            raise ChatRefused("chat_no_writer")
        if stage.viewer_of(speaker):
            if why := stage.page_refusal(self.hass, speaker, (first, second)):
                raise ChatRefused(why)
        elif not can_speak(self.hass, speaker):
            raise ChatRefused("chat_bad_speaker")

        chat = _Chat(
            routers=(a, b),
            names=(assistant_display_name(first), assistant_display_name(second)),
            speaker=speaker,
            topic=clean_text(topic)[:MAX_TOPIC_LENGTH],
            limits=limits,
            started=dt_util.utcnow(),
        )
        self._chat = chat
        self._watch(chat, (first, second))
        self._watch_wakes(chat)
        _LOGGER.info("Chat show started: %s and %s on %s", first.title, second.title, speaker)
        chat.task = self.hass.async_create_background_task(
            self._async_run(chat), name=f"{DOMAIN} chat show"
        )

    def _watch(self, chat: _Chat, entries: tuple[ConfigEntry, ConfigEntry]) -> None:
        """Cancel the chat when either router unloads (reload, removal) or Home Assistant stops."""

        @callback
        def router_changed() -> None:
            if any(entry.state is not ConfigEntryState.LOADED for entry in entries):
                self._cancel(chat)

        @callback
        def home_assistant_stopping(event: Event) -> None:
            self._cancel(chat)

        chat.unsubscribe = [entry.async_on_state_change(router_changed) for entry in entries]
        chat.unsubscribe.append(
            self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, home_assistant_stopping)
        )

    def _watch_wakes(self, chat: _Chat) -> None:
        """Watch a satellite speaker for listening that its own chat line set off."""
        if self._unwatch_wakes is not None:
            self._unwatch_wakes()
            self._unwatch_wakes = None
        if not chat.speaker.startswith("assist_satellite."):
            return

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            old_state, new_state = event.data["old_state"], event.data["new_state"]
            if new_state is None:
                return
            if new_state.state == STATE_UNAVAILABLE:
                if chat.speaking and chat.line_finished is None:
                    chat.line_dropped = True
            elif new_state.state == SATELLITE_RESPONDING:
                if chat.speaking and chat.line_finished is None:
                    chat.line_audible = True
            elif new_state.state == SATELLITE_LISTENING and (
                old_state is None or old_state.state != SATELLITE_LISTENING
            ):
                self._note_wake(chat, old_state.state if old_state is not None else None)

        self._unwatch_wakes = async_track_state_change_event(
            self.hass, [chat.speaker], changed
        )

    def _note_wake(self, chat: _Chat, old_state: str | None) -> None:
        """The satellite started listening: mark it a self-wake, or clear an earlier mark.

        Home Assistant shows a satellite responding while it plays a line and idle once the
        line has ended, also before the call returns (assist_satellite entity.py). Before
        the line plays, or once a line spoken with start_conversation has ended, listening
        is a person's turn; while a line plays, or just after an announced one, it is the
        line's own words that woke it.
        """
        now = dt_util.utcnow()
        if chat.speaking and chat.line_finished is None:
            own = chat.line_audible and not (
                chat.line_listens and old_state == SATELLITE_IDLE
            )
        else:
            own = (
                not chat.line_listens
                and chat.line_finished is not None
                and now - chat.line_finished <= timedelta(seconds=SELF_WAKE_MARGIN)
            )
        if not own:
            # A wake of its own: whatever an earlier self-wake left is no longer coming.
            self._answer_wake(chat)
            return
        _LOGGER.debug("%s woke on its own chat line", chat.speaker)
        chat.line_woken = True
        chat.self_wake_at = now
        chat.wake_answered.clear()

    @staticmethod
    def _answer_wake(chat: _Chat) -> None:
        chat.self_wake_at = None
        chat.wake_answered.set()

    def is_self_wake(self, device_id: str | None) -> bool:
        """Whether this request comes from the chat's satellite waking on its own line.

        True once per self-wake: the mark is used up, so the next request is a person's.
        """
        chat = self._chat
        if chat is None or chat.self_wake_at is None or not device_id:
            return False
        if self._device_of(chat.speaker) != device_id:
            return False
        fresh = self._fresh_wake(chat, dt_util.utcnow())
        self._answer_wake(chat)
        return fresh

    @staticmethod
    def _fresh_wake(chat: _Chat, now: datetime) -> bool:
        """Whether the chat holds a self-wake whose request may still come."""
        woke = chat.self_wake_at
        return woke is not None and now - woke <= timedelta(seconds=SELF_WAKE_TTL)

    @callback
    def _cancel(self, chat: _Chat) -> None:
        """End the chat now, even in the middle of a line."""
        self._end(chat, "chat_stopped")
        if chat.task is not None and not chat.task.done():
            chat.task.cancel()

    def _end(self, chat: _Chat, reason: str) -> None:
        """Record why the chat ended. The first reason stays."""
        if chat.ended:
            return
        chat.ended = reason
        # A failure shows without debug logging; a chat that ran its course does not.
        level = logging.INFO if reason in _NORMAL_ENDS else logging.WARNING
        _LOGGER.log(level, "Chat show on %s ended: %s", chat.speaker, reason)

    async def async_stop(self, reason: str = "stopped") -> None:
        """End the running chat with the reason key chat_<reason>.

        A line that is being spoken is allowed to finish; otherwise the chat ends at once.
        """
        chat = self._chat
        if chat is None or chat.ended:
            return
        self._end(chat, f"chat_{reason}")
        task = chat.task
        if task is not None and not task.done() and not chat.speaking:
            task.cancel()
            await asyncio.wait({task})

    def is_live_on(self, device_id: str | None) -> bool:
        """Whether the current chat runs, not yet ended, on this device's speaker.

        Unlike is_running_on, not during a grace: a late reply there joins no chat.
        """
        chat = self._chat
        if chat is None or chat.ended or chat.task is None or chat.task.done():
            return False
        return device_id is not None and self._device_of(chat.speaker) == device_id

    def is_fragment(self, text: str) -> bool:
        """Whether a person's turn in the current chat is too short or cut off to use."""
        chat = self._chat
        return is_fragment(text, chat.names if chat is not None else ())

    def add_person_line(self, name: str, text: str) -> None:
        """Add what a person said to the running chat; the next assistant answers it."""
        chat = self._chat
        line = clean_text(text)[:MAX_PERSON_LINE_LENGTH]
        if chat is None or chat.ended or not line:
            return
        chat.add(clean_text(name), line)

    def is_running_on(self, device_id: str | None) -> bool:
        """Whether a chat runs, or ended at most REPLY_GRACE ago, on this device's speaker.

        A stopped chat still counts while its last line is spoken and for the grace after,
        because the satellite may still be listening then.
        """
        if not device_id:
            return False
        now = dt_util.utcnow()
        self._grace = {speaker: until for speaker, until in self._grace.items() if until > now}
        speakers = set(self._grace)
        chat = self._chat
        if chat is not None and chat.task is not None and not chat.task.done():
            speakers.add(chat.speaker)
        if chat is not None and self._fresh_wake(chat, now):
            # A request from a wake on the last line may come after the grace.
            speakers.add(chat.speaker)
        return any(self._device_of(speaker) == device_id for speaker in speakers)

    def is_running(self) -> bool:
        """Whether a chat runs, or ended at most REPLY_GRACE ago, on any speaker.

        Its lines are heard by other satellites too, and may wake them.
        """
        now = dt_util.utcnow()
        chat = self._chat
        if chat is not None and chat.task is not None and not chat.task.done():
            return True
        return any(until > now for until in self._grace.values())

    def _device_of(self, entity_id: str) -> str | None:
        entity = er.async_get(self.hass).async_get(entity_id)
        return entity.device_id if entity is not None else None

    def main_conversation_id(self, conversation_id: str | None) -> str | None:
        """The conversation the main agent gets for a request in this conversation.

        The same one, unless a chat line spoken with start_conversation is in its history:
        then a conversation without it, the one the main agent used last time for it, or
        None for a new one. A conversation Home Assistant has forgotten is forgotten here.
        """
        logs = self.hass.data.get(DATA_CHAT_LOGS) or {}
        self._chat_conversations = {
            chat_id: clean
            for chat_id, clean in self._chat_conversations.items()
            if chat_id in logs
        }
        if conversation_id is None or conversation_id not in self._chat_conversations:
            return conversation_id
        return self._chat_conversations[conversation_id]

    def remember_main_conversation(self, conversation_id: str | None, used: str | None) -> None:
        """Keep the conversation the main agent answered in, for the next request."""
        if conversation_id in self._chat_conversations and used != conversation_id:
            self._chat_conversations[conversation_id] = used

    def _note_listened_line(self, speaker: str, line: str) -> None:
        """Remember the conversations whose history holds this line, spoken by the speaker.

        Home Assistant writes the start message into a new conversation as the satellite's
        words (assist_satellite entity.py async_internal_start_conversation).
        """
        for conversation_id, log in (self.hass.data.get(DATA_CHAT_LOGS) or {}).items():
            if any(
                isinstance(content, AssistantContent)
                and content.agent_id == speaker
                and content.content == line
                for content in log.content
            ):
                self._chat_conversations.setdefault(conversation_id, None)

    def state(self) -> ChatState:
        """The running chat, else the last one, else an empty state.

        A stopped chat stays running until its last line has finished; `ended` already
        holds the reason then.
        """
        chat = self._chat
        if chat is None:
            return ChatState(
                running=False,
                speaker="",
                assistants=[],
                assistant_ids=[],
                topic="",
                lines=[],
                ended="",
            )
        return ChatState(
            running=chat.task is not None and not chat.task.done(),
            speaker=chat.speaker,
            assistants=list(chat.names),
            assistant_ids=list(chat.routers),
            topic=chat.topic,
            lines=[{"who": line.who, "text": line.text, "at": line.at} for line in chat.lines],
            ended=chat.ended,
        )

    async def _async_run(self, chat: _Chat) -> None:
        try:
            # Every assistant's avatar on this speaker connected before the first line, and
            # kept so until the last: a session opening between lines stutters the others.
            await avatar_chat.async_hold(
                self.hass,
                chat.speaker,
                chat.routers,
                avatar_chat.safety_until(chat.started, chat.limits.minutes),
                chat.avatar_selects,
            )
            # A cast device shows the avatars instead, when it can.
            pair = self._chat_pair(chat)
            if pair is not None:
                chat.on_stage = await stage.async_open(self.hass, chat.speaker, pair, chat.names)
            if stage.viewer_of(chat.speaker) and chat.on_stage is None:
                # A chat on the page has nowhere else to play.
                self._end(chat, "chat_page_not_connected")
                return
            await self._async_talk(chat)
        except asyncio.CancelledError:
            self._end(chat, "chat_stopped")
            raise
        except Exception:
            _LOGGER.exception("The chat show failed")
            self._end(chat, "chat_failed")
        finally:
            self._grace[chat.speaker] = dt_util.utcnow() + timedelta(seconds=REPLY_GRACE)
            # A task of its own: this one may be cancelled, and may not wait for a device.
            self.hass.async_create_background_task(
                avatar_hold.async_set(self.hass, chat.speaker, False),
                name=f"{DOMAIN} avatar hold off",
            )
            self.hass.async_create_background_task(
                avatar_chat.async_release(self.hass, chat.avatar_selects),
                name=f"{DOMAIN} chat avatars release",
            )
            if chat.on_stage is not None:
                self.hass.async_create_background_task(
                    stage.async_close(self.hass, chat.on_stage), name=f"{DOMAIN} chat stage end"
                )
            for unsubscribe in chat.unsubscribe:
                unsubscribe()
            chat.unsubscribe = []

    async def _async_talk(self, chat: _Chat) -> None:
        prepared: list[asyncio.Task[_Prepared]] = []
        try:
            await self._async_lines(chat, prepared)
        finally:
            for task in prepared:
                task.cancel()

    async def _async_lines(self, chat: _Chat, prepared: list[asyncio.Task[_Prepared]]) -> None:
        """Write and speak the lines in turn. While a line is spoken the next one is
        prepared (`prepared` holds that task), so the silence between two lines is the
        pause and not also the time to write a line and make its audio."""
        limits = chat.limits
        deadline = chat.started + timedelta(minutes=limits.minutes)
        spoken = 0
        while not chat.ended:
            if spoken >= limits.max_lines:
                self._end(chat, "chat_done")
                return
            if dt_util.utcnow() >= deadline:
                self._end(chat, "chat_time_up")
                return
            turn = spoken % 2
            entry = _loaded_router(self.hass, chat.routers[turn])
            if entry is None or _loaded_router(self.hass, chat.routers[1 - turn]) is None:
                self._end(chat, "chat_no_router")
                return
            writer = writer_agent(self.hass, entry)
            if writer is None:
                self._end(chat, "chat_no_writer")
                return
            own, other = chat.names[turn], chat.names[1 - turn]
            heard = len(chat.lines)
            began = time.monotonic()
            ready = await prepared.pop() if prepared else None
            use_prepared = ready is not None and bool(ready.line) and ready.heard == heard
            if ready is not None and use_prepared:
                line, chat.line_emotion, chat.line_voiced, chat.line_why = (
                    ready.line, ready.emotion, ready.voiced, ready.why,
                )
            else:
                # Nothing prepared, or someone joined in since: it must answer them.
                (
                    line, chat.line_emotion, chat.line_voiced, chat.line_why,
                ) = await self._async_write(chat, entry, own, other, writer)
            _LOGGER.debug(
                "Chat line %d (%s): %s, waited %.1f s for it",
                spoken + 1, own, "prepared" if use_prepared else "written now",
                time.monotonic() - began,
            )
            # Set by another task while the line was written, which mypy cannot know.
            if chat.ended:
                return  # type: ignore[unreachable]
            if not line:
                self._end(chat, "chat_agent_failed")
                return
            if not await self._async_ready(chat):
                return
            if len(chat.lines) != heard:
                # Someone joined in while the line was written or the satellite was busy
                # with them: write it again so it answers them. Not counted as a line.
                continue
            chat.add(own, line)
            spoken += 1
            # The second assistant ends a round: that is where a person may reply. Never
            # after the last line: the reply would come when no chat runs any more.
            listen = (
                turn == 1
                and spoken < limits.max_lines
                and limits.listen
                and can_listen(self.hass, chat.speaker)
            )
            if spoken < limits.max_lines and dt_util.utcnow() < deadline:
                prepared.append(
                    self.hass.async_create_background_task(
                        self._async_prepare(chat, spoken % 2), f"{DOMAIN} chat next line"
                    )
                )
            began = time.monotonic()
            failure = await self._async_speak(chat, entry, line, listen)
            _LOGGER.debug(
                "Chat line %d (%s): %d words spoken in %.1f s",
                spoken, own, len(line.split()), time.monotonic() - began,
            )
            if failure:
                self._end(chat, failure)
                return
            self._count_woken(chat)
            if not chat.ended and spoken < limits.max_lines:
                await asyncio.sleep(limits.pause)

    async def _async_prepare(self, chat: _Chat, turn: int) -> _Prepared:
        """Write the line after the one being spoken, and make its audio. Never raises.

        It is only used when nobody joined in meanwhile; otherwise it is thrown away and
        the line is written again, so it answers them.
        """
        heard = len(chat.lines)
        try:
            entry = _loaded_router(self.hass, chat.routers[turn])
            writer = writer_agent(self.hass, entry, quiet=True) if entry is not None else None
            if entry is None or writer is None:
                return _Prepared(heard)
            line, found, voiced, why = await self._async_write(
                chat, entry, chat.names[turn], chat.names[1 - turn], writer
            )
            if line:
                await self._async_warm_voice(chat, entry, voiced)
            return _Prepared(heard, line, found, voiced, why)
        except Exception:  # noqa: BLE001 - the line is then written when its turn comes
            _LOGGER.debug("The next chat line could not be prepared", exc_info=True)
            return _Prepared(heard)

    async def _async_warm_voice(self, chat: _Chat, entry: ConfigEntry, voiced: str) -> None:
        """Make the line's audio now; Home Assistant keeps it for when it is spoken.

        `voiced` is the line as the chat voice is given it. A satellite without a chat
        voice makes the audio itself, so nothing is made here.
        """
        from homeassistant.components import tts  # noqa: PLC0415 - only needed when speaking

        engine, voice = _chat_voice(entry)
        # The same request as when the line is spoken, or the audio would be made twice.
        fmt = STAGE_FORMAT if chat.on_stage is not None and not chat.on_stage.broken else None
        if engine:
            await _async_chat_media_id(self.hass, voiced, engine, voice, fmt)
        elif not chat.speaker.startswith("assist_satellite."):
            # Home Assistant's default engine is not the assistant's voice: no tags.
            media_id = _tts_media_id(
                self.hass, emotion.strip_audio_tags(voiced), None, "", fmt
            )
            if media_id is not None:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(CHAT_VOICE_TIMEOUT):
                        await tts.async_get_media_source_audio(self.hass, media_id)

    async def _async_ready(self, chat: _Chat) -> bool:
        """Wait until a satellite speaker is idle. False when the chat ended meanwhile.

        A satellite busy with a person joining in is waited for here, before the line is
        spoken, so what they said is answered by this line rather than the one after.
        """
        if not chat.speaker.startswith("assist_satellite."):
            return True
        if chat.self_wake_at is not None:
            # Home Assistant sets the satellite idle when the line ends, while it may still
            # be listening; the next line would cancel that pipeline. The line plays on
            # through the wake, so the wait counts from its end: SELF_WAKE_WAIT after it,
            # but never past SELF_WAKE_TTL after the wake. A request that never comes holds
            # up one line at most.
            woke = chat.self_wake_at
            ended = max(woke, chat.line_finished or woke)
            deadline = min(
                ended + timedelta(seconds=SELF_WAKE_WAIT),
                woke + timedelta(seconds=SELF_WAKE_TTL),
            )
            remaining = (deadline - dt_util.utcnow()).total_seconds()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(max(remaining, 0.0)):
                    await chat.wake_answered.wait()
        if not await self._async_wait_idle(chat.speaker):
            self._end(chat, self._not_idle_reason(chat.speaker))
        return not chat.ended

    @staticmethod
    def _count_woken(chat: _Chat) -> None:
        """Count lines in a row that woke the satellite. The line and time limits end it."""
        if not chat.line_woken:
            chat.woken_in_a_row = 0
            return
        chat.woken_in_a_row += 1
        if chat.woken_in_a_row == SELF_WAKE_WARN_AFTER:
            _LOGGER.warning(
                "%s woke on %s chat lines in a row; the chat goes on. A wake word that "
                "differs from the assistants' names avoids this",
                chat.speaker,
                SELF_WAKE_WARN_AFTER,
            )

    def _unavailable(self, speaker: str) -> bool:
        state = self.hass.states.get(speaker)
        return state is None or state.state == STATE_UNAVAILABLE

    def _not_idle_reason(self, satellite: str) -> str:
        """Why a satellite that never became idle ends the chat: gone, or busy."""
        return "chat_speaker_failed" if self._unavailable(satellite) else "chat_speaker_busy"

    async def _async_write(
        self, chat: _Chat, entry: ConfigEntry, own_name: str, other_name: str, writer: str
    ) -> tuple[str, emotion.Emotion, str, str]:
        """The assistant's next line, capped at the word limit, or "" when it failed; the
        emotion its tag gave it; the line as its chat voice is given it; and how the
        emotion was chosen.

        Neither tag is ever part of the line. The voiced line keeps its audio tags when
        the assistant's voice takes them and the line was not cut to the word limit.
        """
        words = chat.limits.words
        rules = chat_instructions(own_name, other_name, words)
        prompt = "\n\n".join(part for part in (character_of(entry), rules) if part)
        try:
            async with asyncio.timeout(LINE_TIMEOUT):
                result = await conversation.async_converse(
                    self.hass,
                    text=chat_message(chat.topic, chat.lines),
                    # A conversation of its own: nothing reaches the shared history.
                    conversation_id=None,
                    context=Context(),
                    agent_id=writer,
                    extra_system_prompt=prompt,
                )
        except TimeoutError:
            _LOGGER.warning("%s took longer than %s s to write a chat line", writer, LINE_TIMEOUT)
            return "", emotion.NEUTRAL, "", ""
        except Exception:  # noqa: BLE001 - any agent failure ends the chat with a reason
            _LOGGER.warning("%s could not write a chat line", writer, exc_info=True)
            return "", emotion.NEUTRAL, "", ""
        if result.response.response_type == intent.IntentResponseType.ERROR:
            _LOGGER.warning("%s answered a chat line with an error", writer)
            return "", emotion.NEUTRAL, "", ""
        speech = speech_text(result.response)
        voice_tags = emotion.voice_tags_on(entry)
        found, voiced, why = emotion.split_face(
            clean_text(speech),
            emotion.accepted_emotions(entry),
            emotion.face_table(entry),
            keep_unknown=voice_tags,
        )
        prefix = f"{own_name}:"
        if voiced.casefold().startswith(prefix.casefold()):
            voiced = voiced[len(prefix) :].strip()
        shown = clean_text(emotion.strip_audio_tags(voiced))
        line = fit_line(shown, words)
        # A line cut to the word limit is spoken as it is shown, without its tags.
        return line, found, voiced if voice_tags and line == shown else line, why

    async def _async_speak(
        self, chat: _Chat, entry: ConfigEntry, line: str, listen: bool = False
    ) -> str:
        """Speak the line on the chat's speaker. Returns "" or the reason the chat must end.

        With `listen` (a satellite that can start a conversation a router hears) the
        satellite listens after the line; what a person answers arrives through the
        joining-in path.
        """
        if stage.viewer_of(chat.speaker):
            return await self._async_page_line(chat, entry, line)
        if not await self._async_wait_back(chat.speaker):
            _LOGGER.warning("%s is unavailable, ending the chat", chat.speaker)
            return "chat_speaker_failed"
        if chat.speaker.startswith("assist_satellite."):
            if listen:
                failure = await self._async_announce(chat, entry, line, listen=True)
                if failure != _LISTEN_UNAVAILABLE:
                    return failure
            return await self._async_announce(chat, entry, line)
        if chat.on_stage is not None and not chat.on_stage.broken:
            if await self._async_stage_line(chat, entry, line):
                return ""
            # Played as audio from now on, which takes the cast device off the stage.
        return await self._async_play(chat, entry, line)

    async def _async_page_line(self, chat: _Chat, entry: ConfigEntry, line: str) -> str:
        """Speak the line on the page that shows the chat. "" or the reason the chat must end:
        a page that is gone, or lost a stream, leaves nowhere to play it."""
        show = chat.on_stage
        if (
            show is None
            or show.broken
            or not await stage.async_page_watching(self.hass, show)
            or not await self._async_stage_line(chat, entry, line)
        ):
            _LOGGER.warning("The page showing the chat is gone, ending the chat")
            return "chat_page_gone"
        return ""

    async def _async_stage_line(self, chat: _Chat, entry: ConfigEntry, line: str) -> bool:
        """Have the assistant's avatar say the line on the stage. False when it could not."""
        from homeassistant.components import tts  # noqa: PLC0415 - only needed when speaking

        assert chat.on_stage is not None  # noqa: S101
        engine, voice = _chat_voice(entry)
        media_id = (
            await _async_chat_media_id(
                self.hass, chat.line_voiced or line, engine, voice, STAGE_FORMAT
            )
            if engine
            else None
        )
        media_id = media_id or _tts_media_id(self.hass, line, None, "", STAGE_FORMAT)
        if media_id is None:
            return False
        try:
            async with asyncio.timeout(CHAT_VOICE_TIMEOUT):
                extension, audio = await tts.async_get_media_source_audio(self.hass, media_id)
            clip = prepare_clip(extension, audio, {})
        except Exception as err:  # noqa: BLE001 - an engine may raise anything
            _LOGGER.warning("No audio for a chat line on the stage: %s", type(err).__name__)
            return False
        if clip is None:
            _LOGGER.warning("The chat voice gave audio the avatar cannot play")
            return False
        chat.speaking = True
        emotion.log_face_with_clip(entry.title, chat.line_emotion, chat.line_why)
        try:
            # With the clip, not /set_emotion: that would also leave the emotion pending on
            # any other session showing this avatar, such as a satellite's.
            return await stage.async_say(
                self.hass, chat.on_stage, entry.entry_id, line, *clip,
                timeout=play_timeout(len(line.split())), emotion=chat.line_emotion,
            )
        finally:
            chat.speaking = False

    async def _async_wait_idle(self, satellite: str, limit: float = BUSY_WAIT) -> bool:
        """Wait until the satellite is idle, at most `limit` seconds. False if it is gone."""
        waited = 0.0
        while True:
            state = self.hass.states.get(satellite)
            if state is not None and state.state == SATELLITE_IDLE:
                return True
            if waited >= limit or self._unavailable(satellite):
                return False
            await asyncio.sleep(BUSY_POLL)
            waited += BUSY_POLL

    async def _async_wait_back(self, speaker: str) -> bool:
        """Wait until a speaker that is unavailable is back, at most RETURN_WAIT seconds."""
        waited = 0.0
        while self._unavailable(speaker):
            if waited >= RETURN_WAIT:
                return False
            await asyncio.sleep(BUSY_POLL)
            waited += BUSY_POLL
        return True

    @staticmethod
    async def _async_let_finish(line: str, called: datetime) -> None:
        """Wait out what is left of a line that began playing at about `called`."""
        takes = SPOKEN_SECONDS_PER_WORD * len(line.split())
        left = takes - (dt_util.utcnow() - called).total_seconds()
        if left > 0:
            await asyncio.sleep(left)

    async def _after_failed_line(
        self,
        chat: _Chat,
        line: str,
        err: HomeAssistantError,
        listen: bool,
        called: datetime,
        attempt: int,
    ) -> str | None:
        """What a chat line whose announcement raised comes to.

        "" when it counts as spoken and the chat goes on, the reason the chat must end
        (or _LISTEN_UNAVAILABLE), or None when the line is to be tried again: the
        satellite was busy, or dropped its connection before the line played and is back.
        """
        if chat.line_woken and not _is_busy_error(err):
            # The announcement failed after the satellite woke on it: the line
            # counts as spoken and the chat goes on.
            _LOGGER.debug("%s failed a chat line it woke on: %s", chat.speaker, err)
            return ""
        if chat.line_dropped and not _is_busy_error(err):
            if not await self._async_wait_back(chat.speaker):
                _LOGGER.warning(
                    "%s dropped its connection during a chat line and did not come "
                    "back in %s s: %s",
                    chat.speaker, RETURN_WAIT, err,
                )
                return "chat_speaker_failed"
            if chat.line_audible:
                # It goes on playing the line it had started.
                _LOGGER.warning(
                    "%s dropped its connection while a chat line played and is "
                    "back; the chat goes on: %s",
                    chat.speaker, err,
                )
                await self._async_let_finish(line, called)
                return ""
            _LOGGER.warning(
                "%s dropped its connection before a chat line played and is back; "
                "the line is tried again: %s",
                chat.speaker, err,
            )
            return None
        if not _is_busy_error(err):
            if listen:
                _LOGGER.warning(
                    "%s could not start a conversation, announcing instead: %s",
                    chat.speaker,
                    err,
                )
                return _LISTEN_UNAVAILABLE
            _LOGGER.warning("Could not announce a chat line on %s: %s", chat.speaker, err)
            return "chat_speaker_failed"
        _LOGGER.debug("%s is busy (attempt %s)", chat.speaker, attempt)
        return None

    async def _async_announce(
        self, chat: _Chat, entry: ConfigEntry, line: str, listen: bool = False
    ) -> str:
        """Announce the line, or with `listen` start a conversation with it.

        Returns "" or the reason the chat must end. A conversation that cannot start returns
        _LISTEN_UNAVAILABLE, so the caller announces the line instead. The conversation gets
        an empty extra_system_prompt: without one, Home Assistant makes the line the system
        prompt of whatever the satellite hears next.
        """
        engine, voice = _chat_voice(entry)
        media_id = (
            await _async_chat_media_id(self.hass, chat.line_voiced or line, engine, voice)
            if engine
            else None
        )
        service = "start_conversation" if listen else "announce"
        data: dict[str, Any] = {"entity_id": chat.speaker, "preannounce": False}
        if listen:
            data["extra_system_prompt"] = ""
        # The text goes along with a chat voice too, so the satellite can show it.
        data["start_message" if listen else "message"] = line
        if media_id:
            data["start_media_id" if listen else "media_id"] = media_id
        timeout = ANNOUNCE_TIMEOUT_BASE + PLAY_TIMEOUT_PER_WORD * len(line.split())
        for attempt in (1, 2):
            if chat.ended:
                # Stopped while the satellite was busy: the line is not tried again.
                return ""
            if not await self._async_wait_idle(chat.speaker):
                return self._not_idle_reason(chat.speaker)
            # Before every line, not only the first: the device lets go of the hold by
            # itself when the next line is long in coming.
            await avatar_hold.async_set(self.hass, chat.speaker, True)
            # Just before the line: the device's choice holds for one announcement only.
            await announcement_look.async_show_assistant(
                self.hass, chat.speaker, entry, self._chat_pair(chat)
            )
            watch = _ListenWatch()
            unwatch = async_track_state_change_event(self.hass, [chat.speaker], watch.changed)
            chat.line_listens, chat.line_audible, chat.line_woken = listen, False, False
            chat.line_finished, chat.line_dropped = None, False
            called = dt_util.utcnow()
            returned = False
            try:
                chat.speaking = True
                emotion.send_emotion(self.hass, entry, chat.line_emotion, chat.line_why)
                async with asyncio.timeout(timeout):
                    await self.hass.services.async_call(
                        "assist_satellite", service, data, blocking=True
                    )
                returned = True
                chat.line_finished = dt_util.utcnow()
                if listen:
                    watch.returned = True
                    await self._async_wait_listened(chat.speaker, watch)
                return ""
            except HomeAssistantError as err:
                outcome = await self._after_failed_line(chat, line, err, listen, called, attempt)
                if outcome is not None:
                    return outcome
            except TimeoutError:
                if chat.line_woken:
                    _LOGGER.debug("%s woke and never finished a chat line", chat.speaker)
                    return ""
                _LOGGER.warning("%s did not finish a chat line in %s s", chat.speaker, timeout)
                return "chat_speaker_failed"
            finally:
                if not returned and chat.line_audible:
                    chat.line_finished = dt_util.utcnow()
                chat.speaking = False
                unwatch()
                if listen:
                    self._note_listened_line(chat.speaker, line)
        return "chat_speaker_busy"

    def _chat_pair(self, chat: _Chat) -> tuple[ConfigEntry, ConfigEntry] | None:
        """The chat's two assistants in chat order, or None if one is no longer loaded."""
        first, second = (_loaded_router(self.hass, key) for key in chat.routers)
        return (first, second) if first is not None and second is not None else None

    async def _async_wait_listened(self, satellite: str, watch: "_ListenWatch") -> None:
        """Wait for the satellite to start listening (briefly), then to be idle again."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(LISTEN_START_WAIT):
                await watch.listening.wait()
        await self._async_wait_idle(satellite, LISTEN_WAIT)

    async def _async_play(self, chat: _Chat, entry: ConfigEntry, line: str) -> str:
        engine, voice = _chat_voice(entry)
        media_id = (
            await _async_chat_media_id(self.hass, chat.line_voiced or line, engine, voice)
            if engine
            else None
        )
        # No chat voice, or one that cannot be made: Home Assistant's default engine.
        media_id = media_id or _tts_media_id(self.hass, line, None, "")
        if media_id is None:
            return "chat_speaker_failed"
        data: dict[str, Any] = {
            "entity_id": chat.speaker,
            "media_content_id": media_id,
            "media_content_type": MediaType.MUSIC,
        }
        if _features(self.hass, chat.speaker) & MediaPlayerEntityFeature.MEDIA_ANNOUNCE:
            data["announce"] = True
        timeout = play_timeout(len(line.split()))
        finished = asyncio.Event()
        # Only a change to playing shows the line started: music that was already playing
        # before it is no sign, and stopping that music must not end the line.
        started = False

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            nonlocal started
            old_state, new_state = event.data["old_state"], event.data["new_state"]
            playing = new_state is not None and new_state.state == PLAYER_PLAYING
            was_playing = old_state is not None and old_state.state == PLAYER_PLAYING
            if playing and not was_playing:
                started = True
            elif started and not playing:
                finished.set()

        unsubscribe = async_track_state_change_event(self.hass, [chat.speaker], changed)
        chat.speaking = True
        emotion.send_emotion(self.hass, entry, chat.line_emotion, chat.line_why)
        try:
            async with asyncio.timeout(timeout):
                await self.hass.services.async_call(
                    "media_player", "play_media", data, blocking=True
                )
                await finished.wait()
        except HomeAssistantError as err:
            _LOGGER.warning("Could not play a chat line on %s: %s", chat.speaker, err)
            return "chat_speaker_failed"
        except TimeoutError:
            # Some players never report that they finished: go on after the line's time.
            _LOGGER.debug("%s did not report a chat line ended in %s s", chat.speaker, timeout)
        finally:
            chat.speaking = False
            unsubscribe()
        return ""


class _ListenWatch:
    """Sees a satellite leave idle after start_conversation, when it opens its microphone.

    Home Assistant sets the satellite idle as the call returns (assist_satellite entity.py)
    and the device starts its pipeline only after that, so leaving idle counts only after
    that idle, or after the call returned.
    """

    def __init__(self) -> None:
        self.listening = asyncio.Event()
        self.returned = False
        self._idle_seen = False

    @callback
    def changed(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        if new_state is None:
            return
        if new_state.state == SATELLITE_IDLE:
            self._idle_seen = True
        elif self.returned or self._idle_seen:
            self.listening.set()


def get_manager(hass: HomeAssistant) -> ChatShowManager:
    """The one chat show manager of this Home Assistant."""
    data = data_keys.domain_data(hass)
    manager: ChatShowManager | None = data.get(DATA_MANAGER)
    if manager is None:
        manager = ChatShowManager(hass)
        data[DATA_MANAGER] = manager
    return manager
