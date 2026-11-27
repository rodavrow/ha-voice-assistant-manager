# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Avatar TTS entities: speak through another TTS entity and copy the speech to an avatar.

Each avatar of an assistant is one entity. It hands every request to the TTS entity chosen
in its settings, with the same language and options, and returns that entity's audio
unchanged. Once a clip is complete, a copy is sent to LiveTalking in the background. The
settings are read at call time, so switching the engine, the address or the switch needs
no reload.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncGenerator, Mapping
from typing import Any, Final

from homeassistant.components import tts
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import avatar_stream, emotion
from .avatar import (
    CONF_AVATAR_ID,
    CONF_ENABLED,
    CONF_ENGINE,
    CONF_URL,
    MAX_CLIP_BYTES,
    SUBENTRY_TYPE,
    AvatarSender,
    normalize_url,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Text that stops this long after a finished sentence is given to the engine as it is.
SENTENCE_PAUSE: Final = 0.4
_SENTENCE_END: Final = re.compile(r"[.!?…][\"')\]]*\s*$")
# A blank line: what engines that speak sentence by sentence take as the end of one.
_RELEASE: Final = "\n\n"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE:
            async_add_entities(
                [AvatarTTSEntity(entry, subentry.subentry_id, subentry.title)],
                config_subentry_id=subentry.subentry_id,
            )


class AvatarTTSEntity(tts.TextToSpeechEntity):
    """Speaks through the avatar's engine; copies each finished clip to the avatar."""

    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, subentry_id: str, title: str) -> None:
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = subentry_id
        self._attr_name = title
        self._sender: AvatarSender | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._sender = AvatarSender(self.hass, self.entity_id)

    # What the engine offers, read at call time

    @property
    def supported_languages(self) -> list[str]:
        engine = self._engine()
        return list(engine.supported_languages) if engine else []

    @property
    def default_language(self) -> str:
        engine = self._engine()
        return engine.default_language if engine else self.hass.config.language

    @property
    def supported_options(self) -> list[str] | None:
        engine = self._engine()
        return engine.supported_options if engine else None

    @property
    def default_options(self) -> Mapping[str, Any] | None:
        engine = self._engine()
        return engine.default_options if engine else None

    @callback
    def async_get_supported_voices(self, language: str) -> list[tts.Voice] | None:
        engine = self._engine()
        return engine.async_get_supported_voices(language) if engine else None

    def async_supports_streaming_input(self) -> bool:
        engine = self._engine()
        return engine.async_supports_streaming_input() if engine else False

    # Speech

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> tts.TtsAudioType:
        engine = self._required_engine()
        # A reply that was not streamed comes as the text that is shown: the voice gets
        # it back with its audio tags, when this assistant just gave that reply.
        message = emotion.take_spoken(self.hass, self._entry.entry_id, message)
        data: bytes | None
        if engine.async_supports_streaming_input():
            # Only when the engine changed to a streaming one since this request started.
            response = await engine.internal_async_stream_tts_audio(
                tts.TTSAudioRequest(language, options, _single(message))
            )
            data = b"".join([chunk async for chunk in response.data_gen])
            extension: str | None = response.extension
        else:
            extension, data = await engine.async_internal_get_tts_audio(
                message, language, options
            )
        if extension and data:
            # A whole clip: fed in one piece, which still saves the satellite's download
            # and upload of it.
            feed = await self._async_open_feed(extension, options)
            if feed is not None:
                feed.write(data)
                feed.finish()
            self._send_copy(extension, data, options)
        return extension, data

    async def async_stream_tts_audio(self, request: tts.TTSAudioRequest) -> tts.TTSAudioResponse:
        engine = self._required_engine()
        request = tts.TTSAudioRequest(
            request.language,
            request.options,
            released_on_pause(self._voiced(request.message_gen)),
        )
        response = await engine.internal_async_stream_tts_audio(request)
        return tts.TTSAudioResponse(
            response.extension,
            self._copying(response.extension, response.data_gen, request.options),
        )

    async def _voiced(self, pieces: AsyncGenerator[str]) -> AsyncGenerator[str]:
        """The text for the engine. A reply that was not streamed arrives as one piece,
        the text that is shown: it is given its audio tags back (emotion.take_spoken)."""
        first = True
        async for piece in pieces:
            if first:
                first = False
                piece = emotion.take_spoken(self.hass, self._entry.entry_id, piece)
            yield piece

    async def _copying(
        self, extension: str, chunks: AsyncGenerator[bytes], options: Mapping[str, Any]
    ) -> AsyncGenerator[bytes]:
        """Pass each chunk on at once; send the copy only when the clip ended normally.

        The asking satellite's avatar is fed each chunk as it comes (avatar_stream.py). Its
        stream is opened while the engine makes the first chunk, and before that chunk is
        passed on, so the satellite can tell that its avatar is being fed.
        """
        copy: list[bytes] | None = []
        size = 0
        opening: asyncio.Task[avatar_stream.ReplyFeed | None] | None = self.hass.async_create_task(
            self._async_open_feed(extension, options), f"{DOMAIN} avatar stream open"
        )
        feed: avatar_stream.ReplyFeed | None = None
        try:
            async for chunk in chunks:
                if opening is not None:
                    feed, opening = await opening, None
                if feed is not None:
                    feed.write(chunk)
                yield chunk
                if copy is None:
                    continue
                size += len(chunk)
                if size > MAX_CLIP_BYTES:
                    copy = None
                else:
                    copy.append(chunk)
        except BaseException:
            # The engine failed or the request was dropped: the avatar says no more of it.
            if opening is not None:
                opening.cancel()
            if feed is not None:
                feed.abort()
            raise
        if opening is not None:
            feed = await opening
        if feed is not None:
            feed.finish()
        if copy:
            self._send_copy(extension, b"".join(copy), options)

    async def _async_open_feed(
        self, extension: str, options: Mapping[str, Any]
    ) -> avatar_stream.ReplyFeed | None:
        """Open the asking satellite's avatar for this reply, or None. Never raises."""
        try:
            settings = self._settings()
            url = normalize_url(str(settings.get(CONF_URL) or ""))
            if not settings.get(CONF_ENABLED) or url is None:
                return None
            target = avatar_stream.feed_target(self.hass, self._entry, url)
            if target is None:
                return None
            entry_id = self._entry.entry_id
            feed = avatar_stream.ReplyFeed(
                self.hass, target, extension, options,
                answering=lambda: avatar_stream.answering(self.hass, entry_id),
            )
            if not await feed.async_open():
                return None
            # Cancelled with the assistant when it unloads.
            self._entry.async_create_background_task(
                self.hass, feed.async_run(), f"{DOMAIN} avatar stream {self.entity_id}"
            )
        except Exception:  # noqa: BLE001 - the speech must never fail because of this
            _LOGGER.warning("Avatar stream: could not be opened", exc_info=True)
            return None
        return feed

    def _send_copy(self, extension: str, audio: bytes, options: Mapping[str, Any]) -> None:
        settings = self._settings()
        url = normalize_url(str(settings.get(CONF_URL) or ""))
        if not settings.get(CONF_ENABLED) or url is None or self._sender is None:
            return
        # Not started eagerly, so none of the sending runs in the speech path; cancelled with
        # the assistant when it unloads.
        self._entry.async_create_background_task(
            self.hass,
            self._sender.async_send(
                url, extension, audio, dict(options), str(settings.get(CONF_AVATAR_ID) or "")
            ),
            f"{DOMAIN} avatar clip {self.entity_id}",
            eager_start=False,
        )

    # Settings

    def _settings(self) -> Mapping[str, Any]:
        subentry = self._entry.subentries.get(self._subentry_id)
        return subentry.data if subentry is not None else {}

    def _engine(self) -> tts.TextToSpeechEntity | None:
        """The TTS entity this avatar speaks through, if it exists and is not an avatar."""
        engine_id = self._settings().get(CONF_ENGINE)
        if not engine_id or self.hass is None:
            return None
        engine = tts.get_engine_instance(self.hass, str(engine_id))
        if not isinstance(engine, tts.TextToSpeechEntity) or isinstance(engine, AvatarTTSEntity):
            return None
        return engine

    def _required_engine(self) -> tts.TextToSpeechEntity:
        if (engine := self._engine()) is None:
            # The error Home Assistant gives for an engine that does not exist.
            raise HomeAssistantError(
                translation_domain=tts.DOMAIN,
                translation_key="provider_not_found",
                translation_placeholders={"engine": str(self._settings().get(CONF_ENGINE))},
            )
        return engine


async def released_on_pause(
    pieces: AsyncGenerator[str], pause: float = SENTENCE_PAUSE
) -> AsyncGenerator[str]:
    """The same text, with a blank line added when it pauses after a finished sentence.

    An engine that speaks sentence by sentence keeps the last sentence until the next one
    starts. When the main agent says "let me look that up" and then calls a tool, that
    sentence would wait for the tool. The blank line makes the engine speak it at once.
    """
    tail = ""
    coming: asyncio.Future[str] | None = None
    try:
        while True:
            coming = asyncio.ensure_future(anext(pieces))
            if _SENTENCE_END.search(tail):
                done, _ = await asyncio.wait({coming}, timeout=pause)
                if not done:
                    tail = ""
                    yield _RELEASE
            try:
                piece = await coming
            except StopAsyncIteration:
                return
            tail = (tail + piece)[-8:]
            yield piece
    finally:
        if coming is not None and not coming.done():
            coming.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await coming
        await pieces.aclose()


async def _single(message: str) -> AsyncGenerator[str]:
    yield message
