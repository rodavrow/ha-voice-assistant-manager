# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Emotion tags: a reply starts with one, such as "[happy]", for the avatar's face.

The tag is read from the start of the reply and never spoken, shown or logged as part of
it. For an assistant with an avatar, LiveTalking is told the emotion before the reply's
audio reaches it. It is told by avatar id rather than session, because a satellite in
avatar mode feeds its own session and that session is not always known here. Telling it
never delays or breaks the reply: a LiveTalking without /set_emotion answers 404, which is
harmless.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any, Final, Literal, cast, get_args

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .avatar import CONF_AVATAR_ID, CONF_ENABLED, CONF_URL, SUBENTRY_TYPE, normalize_url
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

Emotion = Literal["neutral", "happy", "sad", "angry", "sarcastic"]
EMOTIONS: Final[frozenset[str]] = frozenset(get_args(Emotion))
NEUTRAL: Final[Emotion] = "neutral"

# A tag is a word of letters, so a reply that starts with "[1]" keeps it.
_TAG = re.compile(r"^\s*\[([^\W\d_]+)\]\s*")
# A tag's closing bracket comes within this many characters, or there is no tag. While
# a reply streams in, at most this much is held back to tell.
MAX_TAG_CHARS: Final = 20
# Telling LiveTalking must never hold up the reply.
SEND_TIMEOUT: Final = 0.3
_SENDER_DATA: Final = "emotion_sender"


def _emotion_of(word: str) -> Emotion:
    word = word.lower()
    return cast(Emotion, word) if word in EMOTIONS else NEUTRAL


def split_tag(text: str) -> tuple[Emotion, str]:
    """(emotion, the reply without its tag). No tag, or one not known, is neutral.

    Any word of letters in brackets at the very start is a tag and is taken off, with
    the spaces around it; the rest is left as it is.
    """
    match = _TAG.match(text)
    if match is None or match.end(1) >= MAX_TAG_CHARS:
        return NEUTRAL, text
    return _emotion_of(match.group(1)), text[match.end() :]


class TagFilter:
    """Takes the tag off one streamed message: feed it each piece, then flush it.

    Text is held back only while it may still be a tag: until the closing bracket or
    MAX_TAG_CHARS characters, and not at all once the first character that is not a
    space is not "[". From then on every piece passes through unchanged, apart from the
    spaces right after a tag. The pieces passed on add up to split_tag of the whole.
    """

    def __init__(self) -> None:
        self._held = ""
        self._after_tag = False
        self.emotion: Emotion | None = None

    def feed(self, piece: str) -> str:
        if self.emotion is not None:
            return self._passed(piece)
        self._held += piece
        start = self._held.lstrip()
        undecided = (not start or start.startswith("[")) and "]" not in self._held
        if undecided and len(self._held) < MAX_TAG_CHARS:
            return ""
        return self._decide()

    def flush(self) -> str:
        """What is still held back, at the end of the message."""
        return self._decide() if self.emotion is None and self._held else ""

    def _decide(self) -> str:
        self.emotion, rest = split_tag(self._held)
        self._after_tag = rest != self._held
        self._held = ""
        return self._passed(rest)

    def _passed(self, piece: str) -> str:
        if not self._after_tag:
            return piece
        piece = piece.lstrip()
        self._after_tag = not piece
        return piece


DeltaListener = Callable[[Any, dict[str, Any]], None]  # Any: Home Assistant's ChatLog


class StreamedReply:
    """A chat log delta listener that passes each delta on without the reply's tag.

    The deltas are what a voice pipeline speaks and shows while the reply streams in.
    Each assistant message has its tag taken off; the first message with text decides
    the reply's emotion, which `on_emotion` gets once, as soon as it is known. Call
    finish() when the reply has ended, to pass on text still held back.
    """

    def __init__(self, listener: DeltaListener, on_emotion: Callable[[Emotion], None]) -> None:
        self.listener = listener
        self._on_emotion = on_emotion
        self._filter: TagFilter | None = None
        self._chat_log: Any = None
        self.emotion: Emotion | None = None

    def __call__(self, chat_log: Any, delta: dict[str, Any]) -> None:
        self._chat_log = chat_log
        if (role := delta.get("role")) is not None:
            self._flush()
            self._filter = TagFilter() if role == "assistant" else None
        content = delta.get("content")
        if self._filter is not None and isinstance(content, str) and content:
            passed = self._filter.feed(content)
            self._note(self._filter.emotion)
            if not passed and delta.keys() == {"content"}:
                return
            delta = {**delta, "content": passed}
        self.listener(chat_log, delta)

    def finish(self) -> None:
        self._flush()

    def _flush(self) -> None:
        if self._filter is None:
            return
        passed = self._filter.flush()
        self._note(self._filter.emotion)
        if passed:
            self.listener(self._chat_log, {"content": passed})

    def _note(self, emotion: Emotion | None) -> None:
        if emotion is None or self.emotion is not None:
            return
        self.emotion = emotion
        self._on_emotion(emotion)


def avatar_targets(entry: ConfigEntry) -> list[tuple[str, str]]:
    """(LiveTalking address, avatar id) of each avatar of the assistant that is switched on.

    An avatar without an id shows LiveTalking's default avatar, which cannot be named.
    """
    targets: list[tuple[str, str]] = []
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE or not subentry.data.get(CONF_ENABLED):
            continue
        url = normalize_url(str(subentry.data.get(CONF_URL) or ""))
        avatar_id = str(subentry.data.get(CONF_AVATAR_ID) or "")
        if url is None:
            continue
        if not avatar_id:
            _LOGGER.debug("Emotion: %s has no avatar id, LiveTalking is not told", subentry.title)
            continue
        targets.append((url, avatar_id))
    return targets


class EmotionSender:
    """Posts emotions to LiveTalking. Never raises; warns once per outage of an address."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._failing: set[str] = set()

    async def async_send(self, base_url: str, avatar_id: str, emotion: Emotion) -> None:
        try:
            async with async_get_clientsession(self._hass).post(
                f"{base_url}/set_emotion",
                json={"avatar_id": avatar_id, "emotion": emotion},
                timeout=aiohttp.ClientTimeout(total=SEND_TIMEOUT),
            ) as response:
                if response.status == 404:
                    _LOGGER.debug("Emotion: LiveTalking has no /set_emotion yet")
                    self._failing.discard(base_url)
                    return
                response.raise_for_status()
        except (aiohttp.ClientError, TimeoutError) as err:
            self._log_failure(base_url, err)
            return
        except Exception as err:  # noqa: BLE001 - the reply must never fail because of this
            self._log_failure(base_url, err, unexpected=True)
            return
        self._failing.discard(base_url)
        _LOGGER.debug("Emotion: %s told to look %s", avatar_id, emotion)

    def _log_failure(self, base_url: str, err: Exception, *, unexpected: bool = False) -> None:
        first = base_url not in self._failing
        self._failing.add(base_url)
        _LOGGER.log(
            logging.WARNING if first else logging.DEBUG,
            "Emotion: LiveTalking /set_emotion could not be reached or answered oddly (%s);"
            " the reply itself is not affected",
            type(err).__name__,
            exc_info=err if unexpected and first else None,
        )


def _sender(hass: HomeAssistant) -> EmotionSender:
    data = hass.data.setdefault(DOMAIN, {})
    sender = data.get(_SENDER_DATA)
    if sender is None:
        sender = data[_SENDER_DATA] = EmotionSender(hass)
    return cast(EmotionSender, sender)


def send_emotion(hass: HomeAssistant, entry: ConfigEntry, emotion: Emotion) -> None:
    """Tell each avatar of the assistant the emotion, in the background. Never raises."""
    try:
        targets = avatar_targets(entry)
        if not targets:
            return
        sender = _sender(hass)
        for base_url, avatar_id in targets:
            send = sender.async_send(base_url, avatar_id, emotion)
            try:
                # Started at once, so the call is on its way before the reply's audio is made.
                entry.async_create_background_task(
                    hass, send, f"{DOMAIN} emotion {avatar_id}", eager_start=True
                )
            except BaseException:
                send.close()
                raise
    except Exception:  # noqa: BLE001 - the reply must never fail because of this
        _LOGGER.exception("Emotion: could not tell LiveTalking the emotion")
