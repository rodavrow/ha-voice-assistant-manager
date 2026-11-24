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

from collections.abc import AsyncGenerator, Mapping
from typing import Any

from homeassistant.components import tts
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

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
            self._send_copy(extension, data, options)
        return extension, data

    async def async_stream_tts_audio(self, request: tts.TTSAudioRequest) -> tts.TTSAudioResponse:
        engine = self._required_engine()
        response = await engine.internal_async_stream_tts_audio(request)
        return tts.TTSAudioResponse(
            response.extension,
            self._copying(response.extension, response.data_gen, request.options),
        )

    async def _copying(
        self, extension: str, chunks: AsyncGenerator[bytes], options: Mapping[str, Any]
    ) -> AsyncGenerator[bytes]:
        """Pass each chunk on at once; send the copy only when the clip ended normally."""
        copy: list[bytes] | None = []
        size = 0
        async for chunk in chunks:
            yield chunk
            if copy is None:
                continue
            size += len(chunk)
            if size > MAX_CLIP_BYTES:
                copy = None
            else:
                copy.append(chunk)
        if copy:
            self._send_copy(extension, b"".join(copy), options)

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


async def _single(message: str) -> AsyncGenerator[str]:
    yield message
