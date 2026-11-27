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
import time
from collections.abc import Callable, Mapping
from typing import Any, Final, cast

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .avatar import CONF_AVATAR_ID, CONF_ENABLED, CONF_URL, SUBENTRY_TYPE, normalize_url
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# An emotion as LiveTalking knows it: a lower-case word of letters.
Emotion = str
NEUTRAL: Final[Emotion] = "neutral"
# What an assistant passes on when its Emotion tags setting is empty.
DEFAULT_EMOTIONS: Final[frozenset[str]] = frozenset(
    {NEUTRAL, "happy", "sad", "angry", "sarcastic"}
)
# The setting under Advanced: the tags an assistant passes on, separated by commas.
CONF_EMOTION_TAGS: Final = "emotion_tags"

_WORD = r"[^\W\d_]+"
# A tag is a word of letters, so a reply that starts with "[1]" keeps it.
_TAG = re.compile(rf"^\s*\[({_WORD})\]\s*")
_ONE_WORD = re.compile(_WORD)
# A tag's closing bracket comes within this many characters, or there is no tag. While
# a reply streams in, at most this much is held back to tell.
MAX_TAG_CHARS: Final = 20
# The longest word that fits in a tag at the very start of a reply: "[" word "]".
MAX_WORD_CHARS: Final = MAX_TAG_CHARS - 2
# Settings already warned about, so a bad list is warned about once, not on every reply.
_warned: set[str] = set()
# Telling LiveTalking must never hold up the reply.
SEND_TIMEOUT: Final = 0.3
_SENDER_DATA: Final = "emotion_sender"


def accepted_emotions(entry: Any) -> frozenset[str]:  # Any: a ConfigEntry, or a stand-in
    """The emotions the assistant passes on, from its Emotion tags setting.

    Words are separated by commas or spaces and lower-cased. One that cannot be a tag (not
    only letters, or too long) is left out, with one warning; with none left, or no
    setting, the five default emotions apply.
    """
    advanced = (getattr(entry, "options", None) or {}).get("advanced")
    raw = str((advanced if isinstance(advanced, Mapping) else {}).get(CONF_EMOTION_TAGS) or "")
    words = [word.lower() for word in re.split(r"[\s,]+", raw) if word]
    good = {w for w in words if _ONE_WORD.fullmatch(w) and len(w) <= MAX_WORD_CHARS}
    bad = sorted(set(words) - good)
    if bad and raw not in _warned:
        _warned.add(raw)
        _LOGGER.warning(
            "Emotion tags of %s: %s left out, a tag is one word of at most %d letters",
            getattr(entry, "title", "an assistant"), ", ".join(bad), MAX_WORD_CHARS,
        )
    return frozenset(good) or DEFAULT_EMOTIONS


def split_tag(
    text: str, accepted: frozenset[str] = DEFAULT_EMOTIONS, keep_unknown: bool = False
) -> tuple[Emotion, str]:
    """(emotion, the reply without its tag). No tag, or one not `accepted`, is neutral.

    Any word of letters in brackets at the very start is a tag and is taken off, with
    the spaces around it, whether it is accepted or not; the rest is left as it is.
    With `keep_unknown`, a tag that is not accepted stays: it is an audio tag for the
    voice (see voice_tags_on), not the avatar's emotion.
    """
    match = _TAG.match(text)
    if match is None or match.end(1) >= MAX_TAG_CHARS:
        return NEUTRAL, text
    word = match.group(1).lower()
    if word not in accepted:
        return NEUTRAL, text if keep_unknown else text[match.end() :]
    return word, text[match.end() :]


# Audio tags: directions for a voice that understands them, such as "[laughs]",
# "[whispering, fearful]" or "[out of breath after running up the stairs]". Brackets around
# text that starts with a letter and has no digit, so "[1]" and "[2b]" are never one.
MAX_AUDIO_TAG_CHARS: Final = 80
_AUDIO_TAG = re.compile(rf"\[[^\W\d_][^\[\]\d\n]{{0,{MAX_AUDIO_TAG_CHARS - 3}}}\]")
# The setting under Advanced: whether this assistant's voice is given audio tags.
CONF_VOICE_TAGS: Final = "voice_tags"
_SPOKEN: Final = "voice_tags_spoken"
# How long a reply's tagged text is kept for the voice that speaks it.
_SPOKEN_FOR: Final = 30.0


def voice_tags_on(entry: Any) -> bool:  # Any: a ConfigEntry, or a stand-in
    """Whether the assistant's voice understands audio tags and is given them."""
    advanced = (getattr(entry, "options", None) or {}).get("advanced")
    return isinstance(advanced, Mapping) and advanced.get(CONF_VOICE_TAGS) is True


# The setting under Advanced: which face an opening audio tag gives, one "face: tag, tag"
# per line. Used when a reply does not start with a face tag.
CONF_FACE_FOR_TAGS: Final = "face_for_tags"
DEFAULT_FACE_FOR_TAGS: Final = """\
sarcastic: mischievous, playful, smug, snappy, drawn out, disbelief
happy: excited, amazed, proud, triumphant, hopeful, content, intrigued, curious
laughing: laughs
sad: sighs, let down, tired, sad, softly
angry: shouts, bitter, groans, angry
flirty: whispers, hushed
"""
# How much of a reply's start is looked at for its opening tags.
MAX_OPENING_CHARS: Final = 200


def _tag_words(tag: str) -> str:
    return " ".join(tag.lower().split())


def _face_lines(raw: str) -> list[tuple[str, list[str]]]:
    """(face, its tags) of each "face: tag, tag" line, in order."""
    lines: list[tuple[str, list[str]]] = []
    for line in raw.splitlines():
        face, colon, tags = line.partition(":")
        face = face.strip().lower()
        if colon and face:
            lines.append((face, [w for tag in tags.split(",") if (w := _tag_words(tag))]))
    return lines


def _face_setting(entry: Any) -> str:
    advanced = (getattr(entry, "options", None) or {}).get("advanced")
    raw = (advanced if isinstance(advanced, Mapping) else {}).get(CONF_FACE_FOR_TAGS)
    return str(raw or "").strip()


def face_table(entry: Any) -> dict[str, str]:  # Any: a ConfigEntry, or a stand-in
    """audio tag (lower case) -> the face it gives, from the assistant's setting.

    A line whose face is not one of the assistant's emotions is left out; the first line
    that names a tag wins. An empty setting uses DEFAULT_FACE_FOR_TAGS.
    """
    accepted = accepted_emotions(entry)
    table: dict[str, str] = {}
    for face, tags in _face_lines(_face_setting(entry) or DEFAULT_FACE_FOR_TAGS):
        if face in accepted:
            for tag in tags:
                table.setdefault(tag, face)
    return table


def unknown_faces(entry: Any) -> list[str]:
    """The faces in the assistant's own table that are not on its Emotion tags list."""
    accepted = accepted_emotions(entry)
    faces = [face for face, _tags in _face_lines(_face_setting(entry))]
    return sorted({face for face in faces if face not in accepted})


def _opening_tag(text: str) -> tuple[bool, str | None]:
    """(whether the opening is complete, its first audio tag or None).

    The opening is the run of bracketed groups a reply starts with. It is complete once an
    audio tag is found, once other text starts, or once a group can no longer be a tag.
    """
    at = 0
    while True:
        while at < len(text) and text[at].isspace():
            at += 1
        if at >= len(text):
            return False, None
        if text[at] != "[":
            return True, None
        end = text.find("]", at)
        if end == -1:
            return len(text) - at >= MAX_AUDIO_TAG_CHARS, None
        group = text[at : end + 1]
        if _AUDIO_TAG.fullmatch(group):
            return True, group[1:-1]
        at = end + 1


def split_face(
    text: str,
    accepted: frozenset[str] = DEFAULT_EMOTIONS,
    table: Mapping[str, str] | None = None,
    keep_unknown: bool = False,
) -> tuple[Emotion, str, str]:
    """(face, the reply as split_tag leaves it, how the face was chosen).

    A face tag at the very start sets the face and is taken off. Without one, the first
    audio tag of the opening is looked up in `table`; the text is not changed for it.
    """
    found, rest = split_tag(text, accepted, keep_unknown)
    match = _TAG.match(text)
    if match is not None and match.end(1) < MAX_TAG_CHARS and found == match.group(1).lower():
        return found, rest, "face tag"
    _complete, tag = _opening_tag(text[:MAX_OPENING_CHARS])
    if tag is None:
        return NEUTRAL, rest, "no tag"
    words = _tag_words(tag)
    face = (table or {}).get(words) or (words if words in accepted else None)
    if face is None or face not in accepted:
        return NEUTRAL, rest, f"no match for [{tag}]"
    return face, rest, f"from audio tag [{tag}]"


def strip_audio_tags(text: str) -> str:
    """The text without its audio tags, as it is shown: no gap is left where one was."""
    if "[" not in text:
        return text
    cleaned = _AUDIO_TAG.sub("", text)
    if cleaned == text:
        return text
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([,.!?;:…])", r"\1", cleaned)
    cleaned = re.sub(r"(?m)^[ \t]+|[ \t]+$", "", cleaned)
    return cleaned


class AudioTagFilter:
    """Takes the audio tags out of one streamed message: feed it each piece, then flush.

    Text is held back only from a "[" until it is clear whether a tag follows: its closing
    bracket, a character a tag cannot have, or MAX_AUDIO_TAG_CHARS characters. Spaces at
    the end of a piece wait for the next one, so no gap is left where a tag was. The
    pieces passed on add up to strip_audio_tags of the whole (but for spaces at its end).
    """

    def __init__(self) -> None:
        self._held = ""
        # Spaces not passed on yet, the last character that was, and whether a tag was
        # taken out since.
        self._space = ""
        self._last = ""
        self._gap = False

    def feed(self, piece: str) -> str:
        out: list[str] = []
        for char in piece:
            if not self._held:
                if char == "[":
                    self._held = char
                else:
                    out.append(self._emit(char))
                continue
            self._held += char
            if char == "]":
                tag, self._held = self._held, ""
                if _AUDIO_TAG.fullmatch(tag):
                    self._gap = True
                else:
                    out.append(self._emit(tag))
            elif char == "[":
                out.append(self._emit(self._held[:-1]))
                self._held = "["
            elif char.isdigit() or char == "\n" or len(self._held) >= MAX_AUDIO_TAG_CHARS:
                held, self._held = self._held, ""
                out.append(self._emit(held))
        return "".join(out)

    def flush(self) -> str:
        held, self._held = self._held, ""
        rest = self._emit(held)
        space, self._space = self._space, ""
        return rest if self._gap else rest + space

    def _emit(self, text: str) -> str:
        if not text:
            return ""
        lead, self._space = self._space, ""
        if self._gap:
            # Where a tag was: one space at most, and none before punctuation or at the
            # start of a line.
            spaced = bool(lead) or text[0] in " \t"
            text = text.lstrip(" \t")
            if not text:
                self._space = " " if spaced else ""
                return ""
            self._gap = False
            keep = spaced and text[0] not in ",.!?;:…\n" and self._last not in ("", "\n")
            lead = " " if keep else ""
        body = text.rstrip(" \t")
        self._space = text[len(body) :]
        if not body:
            self._space = lead + self._space
            return ""
        self._last = body[-1]
        return lead + body


def remember_spoken(hass: HomeAssistant, entry_id: str, shown: str, spoken: str) -> None:
    """Keep a reply's text with its audio tags for the voice that is about to speak it.

    A reply that is not streamed reaches the voice as the text that is shown, without
    tags; the assistant's avatar voice asks for the tagged text back (take_spoken).
    """
    if shown != spoken:
        hass.data.setdefault(DOMAIN, {}).setdefault(_SPOKEN, {})[entry_id] = (
            shown, spoken, time.monotonic(),
        )


def take_spoken(hass: HomeAssistant, entry_id: str, shown: str) -> str:
    """The text to speak for `shown`: with its audio tags when it is the reply just given
    by this assistant, otherwise `shown` itself."""
    kept = hass.data.get(DOMAIN, {}).get(_SPOKEN, {}).get(entry_id)
    if kept is None or kept[0].strip() != shown.strip():
        return shown
    del hass.data[DOMAIN][_SPOKEN][entry_id]
    return kept[1] if time.monotonic() - kept[2] <= _SPOKEN_FOR else shown


class TagFilter:
    """Reads the face off one streamed message: feed it each piece, then flush it.

    Text is held back only while the opening of the message may still hold a tag: not at
    all once it starts with other text, and at most until the first audio tag closes or
    MAX_OPENING_CHARS characters. From then on every piece passes through unchanged, apart
    from the spaces right after a face tag. The pieces passed on add up to split_tag of
    the whole; `emotion` and `why` are split_face's.
    """

    def __init__(
        self,
        accepted: frozenset[str] = DEFAULT_EMOTIONS,
        keep_unknown: bool = False,
        table: Mapping[str, str] | None = None,
    ) -> None:
        self._accepted = accepted
        self._keep_unknown = keep_unknown
        self._table = table
        self._held = ""
        self._after_tag = False
        self.emotion: Emotion | None = None
        self.why = ""

    def feed(self, piece: str) -> str:
        if self.emotion is not None:
            return self._passed(piece)
        self._held += piece
        complete, _tag = _opening_tag(self._held)
        if not complete and len(self._held) < MAX_OPENING_CHARS:
            return ""
        return self._decide()

    def flush(self) -> str:
        """What is still held back, at the end of the message."""
        return self._decide() if self.emotion is None and self._held else ""

    def _decide(self) -> str:
        self.emotion, rest, self.why = split_face(
            self._held, self._accepted, self._table, self._keep_unknown
        )
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


# A voice pipeline starts speaking a reply while it streams in once it has been given
# more than this many characters, or some text and then a tool call (Home Assistant's
# assist_pipeline, STREAM_RESPONSE_CHARS). Before that, it speaks the final reply.
SPEAKS_AFTER_CHARS: Final = 60


class StreamedReply:
    """A chat log delta listener that passes each delta on without the reply's tag.

    The deltas are what a voice pipeline speaks and shows while the reply streams in.
    Each assistant message has its tag taken off; the first message with text decides
    the reply's emotion, which `on_emotion` gets once, as soon as it is known. Call
    finish() when the reply has ended, to pass on text still held back.

    `speaking` says whether the voice has started on the reply, which then cannot be
    taken back; say() adds words to it. While muted, nothing is passed on: a second
    answer to the same request is not spoken on top of the first.
    """

    def __init__(
        self,
        listener: DeltaListener,
        on_emotion: Callable[[Emotion], None],
        accepted: frozenset[str] = DEFAULT_EMOTIONS,
        voice_tags: bool = False,
        table: Mapping[str, str] | None = None,
    ) -> None:
        self.listener = listener
        self._accepted = accepted
        self._table = table
        # How the face was chosen, for the log (split_face).
        self.why = ""
        # With voice tags the deltas keep their audio tags: they are what the voice is
        # given. Without, every audio tag is taken out, so no voice reads one aloud.
        self._voice_tags = voice_tags
        self._audio: AudioTagFilter | None = None
        self._on_emotion = on_emotion
        self._filter: TagFilter | None = None
        self._chat_log: Any = None
        self.emotion: Emotion | None = None
        self._muted = False
        self._sent = 0
        self.speaking = False

    def __call__(self, chat_log: Any, delta: dict[str, Any]) -> None:
        self._chat_log = chat_log
        if self._muted:
            return
        if (role := delta.get("role")) is not None:
            self._flush()
            speaks = role == "assistant"
            self._filter = (
                TagFilter(self._accepted, self._voice_tags, self._table) if speaks else None
            )
            self._audio = AudioTagFilter() if speaks and not self._voice_tags else None
        content = delta.get("content")
        if self._filter is not None and isinstance(content, str) and content:
            passed = self._filter.feed(content)
            if self._audio is not None:
                passed = self._audio.feed(passed)
            self._note(self._filter.emotion, self._filter.why)
            if not passed and delta.keys() == {"content"}:
                return
            delta = {**delta, "content": passed}
        self._pass(delta)

    def finish(self) -> None:
        """The reply has ended: pass on what is still held back."""
        self._flush()

    def mute(self) -> None:
        """Close the message so far and pass nothing more on, until unmute()."""
        self._flush()
        self._filter = self._audio = None
        self._muted = True

    def unmute(self) -> None:
        self._muted = False

    def say(self, text: str) -> None:
        """Add words to a reply the voice is already speaking, after what it was given."""
        self._flush()
        self._filter = self._audio = None
        self._pass({"content": f" {text}"})

    def _pass(self, delta: dict[str, Any]) -> None:
        content = delta.get("content")
        if isinstance(content, str):
            self._sent += len(content)
        if self._sent > SPEAKS_AFTER_CHARS or (self._sent and delta.get("tool_calls")):
            self.speaking = True
        self.listener(self._chat_log, delta)

    def _flush(self) -> None:
        if self._filter is None:
            return
        passed = self._filter.flush()
        if self._audio is not None:
            passed = self._audio.feed(passed) + self._audio.flush()
        self._note(self._filter.emotion, self._filter.why)
        if passed:
            self._pass({"content": passed})

    def _note(self, emotion: Emotion | None, why: str) -> None:
        if emotion is None or self.emotion is not None:
            return
        self.emotion, self.why = emotion, why
        self._on_emotion(emotion)


def log_face_with_clip(name: str, emotion: Emotion, why: str = "") -> None:
    """Note a face that went to LiveTalking with a clip, not through /set_emotion.

    On the chat stage each line's face travels with its audio, so no call is made here;
    the log line is the same as for a call, so a face is never sent unseen.
    """
    _LOGGER.debug(
        "Emotion: %s told to look %s with its line on the stage%s",
        name, emotion, f" ({why})" if why else "",
    )


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

    async def async_send(
        self, base_url: str, avatar_id: str, emotion: Emotion, why: str = ""
    ) -> None:
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
        _LOGGER.debug(
            "Emotion: %s told to look %s%s", avatar_id, emotion, f" ({why})" if why else ""
        )

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


def send_emotion(
    hass: HomeAssistant, entry: ConfigEntry, emotion: Emotion, why: str = ""
) -> None:
    """Tell each avatar of the assistant the emotion, in the background. Never raises.

    `why` says how the face was chosen (split_face), for the log.
    """
    try:
        targets = avatar_targets(entry)
        if not targets:
            return
        sender = _sender(hass)
        for base_url, avatar_id in targets:
            send = sender.async_send(base_url, avatar_id, emotion, why)
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
