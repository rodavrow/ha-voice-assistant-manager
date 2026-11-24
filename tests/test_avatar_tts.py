# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The avatar TTS entity: speech passes through unchanged, a finished copy goes to the avatar."""

import asyncio
from collections.abc import AsyncGenerator
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.components import tts
from homeassistant.components.tts.const import DATA_COMPONENT, DATA_TTS_MANAGER
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.voice_assistant_manager import avatar
from custom_components.voice_assistant_manager.const import DOMAIN

from .router_helpers import add_router

BASE = "http://livetalking.local:8010"
MP3 = b"ID3\x04\x00" + b"\x01" * 64
WAV_CHUNKS = [b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16, b"\x02" * 40, b"\x03" * 40]


class FakeEngine(tts.TextToSpeechEntity):
    """A cloud-like engine: whole MP3 clips, its own voices."""

    def __init__(self, entity_id: str, voices: list[str], languages: list[str]) -> None:
        self.entity_id = entity_id
        self._attr_name = entity_id
        self._attr_unique_id = entity_id
        self._attr_supported_languages = languages
        self._attr_default_language = languages[0]
        self._attr_supported_options = ["voice"]
        self._attr_default_options = {"voice": voices[0]}
        self._voices = voices
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.error: Exception | None = None

    def async_get_supported_voices(self, language: str) -> list[tts.Voice] | None:
        return [tts.Voice(voice, voice.title()) for voice in self._voices]

    async def async_get_tts_audio(self, message, language, options):
        self.calls.append((message, language, dict(options)))
        if self.error is not None:
            raise self.error
        return "mp3", MP3


class FakeStreamingEngine(FakeEngine):
    """A local engine that streams WAV in chunks."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.fail_after_first_chunk = False

    async def async_stream_tts_audio(self, request: tts.TTSAudioRequest) -> tts.TTSAudioResponse:
        message = "".join([chunk async for chunk in request.message_gen])
        self.calls.append((message, request.language, dict(request.options)))

        async def chunks() -> AsyncGenerator[bytes]:
            for index, chunk in enumerate(WAV_CHUNKS):
                if index == 1 and self.fail_after_first_chunk:
                    raise HomeAssistantError("engine broke")
                yield chunk

        return tts.TTSAudioResponse("wav", chunks())


@pytest.fixture
async def engines(hass: HomeAssistant, tmp_path) -> dict[str, FakeEngine]:
    with patch("homeassistant.components.tts._init_tts_cache_dir", return_value=str(tmp_path)):
        assert await async_setup_component(hass, "tts", {})
    cloud = FakeEngine("tts.cloud_voice", ["warm", "bright"], ["en-GB", "en-US"])
    local = FakeStreamingEngine("tts.local_voice", ["calm"], ["en-GB", "nl-NL"])
    await hass.data[DATA_COMPONENT].async_add_entities([cloud, local])
    await hass.async_block_till_done()
    return {"cloud": cloud, "local": local}


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, str, bytes, dict[str, Any]]]:
    """What the avatar sender was asked to send: (url, extension, audio, options)."""
    calls: list[tuple[str, str, bytes, dict[str, Any]]] = []

    async def record(self, base_url, extension, audio, options, avatar_id=""):
        calls.append((base_url, extension, audio, dict(options)))

    monkeypatch.setattr(avatar.AvatarSender, "async_send", record)
    return calls


async def add_avatar(
    hass: HomeAssistant, entry, engine: str, *, enabled: bool = True, title: str = "Ada avatar"
) -> str:
    subentry = ConfigSubentry(
        data=MappingProxyType(
            {avatar.CONF_ENGINE: engine, avatar.CONF_URL: BASE, avatar.CONF_ENABLED: enabled}
        ),
        subentry_type=avatar.SUBENTRY_TYPE,
        title=title,
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    # The entry's update listener reloads it, which adds the entity.
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("tts", DOMAIN, subentry.subentry_id)
    assert entity_id is not None
    return entity_id


def set_avatar(hass: HomeAssistant, entry, **changes: Any) -> None:
    subentry = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(entry, subentry, data={**subentry.data, **changes})


async def speak(
    hass: HomeAssistant, engine: str, message: str = "Hello there", *, streamed: bool = False,
    options: dict[str, Any] | None = None, fmt: str | None = None,
) -> tuple[str, bytes]:
    """Speak as a satellite would. `fmt` is the format asked for; HA converts to it."""
    if fmt is not None:
        options = {**(options or {}), "preferred_format": fmt}
    stream = hass.data[DATA_TTS_MANAGER].async_create_result_stream(
        engine, use_file_cache=False, options=options
    )
    if streamed:

        async def words() -> AsyncGenerator[str]:
            for word in message.split(" "):
                yield word + " "

        stream.async_set_message_stream(words())
    else:
        stream.async_set_message(message)
    audio = b"".join([chunk async for chunk in stream.async_stream_result()])
    await hass.async_block_till_done()
    return stream.extension, audio


# Speech passes through unchanged


async def test_an_mp3_engine_speaks_through_the_avatar_unchanged(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")

    extension, audio = await speak(hass, avatar_id, options={"voice": "bright"})

    assert (extension, audio) == ("mp3", MP3)
    assert engines["cloud"].calls == [("Hello there", "en-GB", {"voice": "bright"})]


async def test_a_streaming_wav_engine_speaks_through_the_avatar_unchanged(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.local_voice")

    extension, audio = await speak(hass, avatar_id, "Hello there", streamed=True, fmt="wav")

    assert (extension, audio) == ("wav", b"".join(WAV_CHUNKS))
    assert engines["local"].calls[0][0].strip() == "Hello there"


async def test_the_avatar_streams_exactly_when_its_engine_does(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    entity = tts.get_engine_instance(hass, avatar_id)
    assert entity.async_supports_streaming_input() is False

    set_avatar(hass, entry, engine="tts.local_voice")

    assert entity.async_supports_streaming_input() is True


# A finished copy goes to the avatar


async def test_a_finished_mp3_clip_is_sent_to_the_avatar_unconverted(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")

    await speak(hass, avatar_id)

    assert [(url, ext, audio) for url, ext, audio, _ in sent] == [(BASE, "mp3", MP3)]


async def test_a_streamed_clip_is_sent_once_complete(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.local_voice")

    await speak(hass, avatar_id, streamed=True, fmt="wav")

    assert [(ext, audio) for _, ext, audio, _ in sent] == [("wav", b"".join(WAV_CHUNKS))]


async def test_a_clip_the_engine_failed_part_way_is_not_sent(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.local_voice")
    engines["local"].fail_after_first_chunk = True

    with pytest.raises(HomeAssistantError):
        await speak(hass, avatar_id, streamed=True, fmt="wav")

    assert sent == []


async def test_a_disabled_avatar_sends_nothing_but_still_speaks(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice", enabled=False)

    assert await speak(hass, avatar_id) == ("mp3", MP3)
    assert sent == []


async def test_switching_the_engine_needs_no_reload(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    entity = tts.get_engine_instance(hass, avatar_id)

    set_avatar(hass, entry, engine="tts.local_voice")
    await hass.async_block_till_done()

    assert tts.get_engine_instance(hass, avatar_id) is entity
    assert entity.supported_languages == ["en-GB", "nl-NL"]
    assert [voice.voice_id for voice in entity.async_get_supported_voices("en-GB")] == ["calm"]
    assert entity.default_options == {"voice": "calm"}
    assert await speak(hass, avatar_id, fmt="wav") == ("wav", b"".join(WAV_CHUNKS))
    assert engines["cloud"].calls == []


async def test_the_avatar_offers_its_engines_languages_and_voices(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    entity = tts.get_engine_instance(hass, avatar_id)

    assert entity.supported_languages == ["en-GB", "en-US"]
    assert entity.default_language == "en-GB"
    assert entity.supported_options == ["voice"]
    voices = entity.async_get_supported_voices("en-GB")
    assert [voice.voice_id for voice in voices] == ["warm", "bright"]


# Nothing about the avatar can break or delay speech


async def test_unreachable_livetalking_does_not_affect_speech(hass, engines, aioclient_mock):
    aioclient_mock.get(f"{BASE}/api/admin/sessions", exc=aiohttp.ClientConnectionError())
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")

    assert await speak(hass, avatar_id) == ("mp3", MP3)
    assert aioclient_mock.call_count == 1


async def test_a_slow_avatar_does_not_delay_speech(hass, engines, monkeypatch):
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow(self, *args):
        started.set()
        await release.wait()

    monkeypatch.setattr(avatar.AvatarSender, "async_send", slow)
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    stream = hass.data[DATA_TTS_MANAGER].async_create_result_stream(
        avatar_id, use_file_cache=False
    )
    stream.async_set_message("Hello there")

    audio = b"".join([chunk async for chunk in stream.async_stream_result()])

    assert audio == MP3
    await started.wait()
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_an_engine_error_reaches_home_assistant_unchanged(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    engines["cloud"].error = HomeAssistantError("quota used up")

    with pytest.raises(HomeAssistantError, match="quota used up"):
        await speak(hass, avatar_id)
    assert sent == []


async def test_a_missing_engine_offers_no_language_so_home_assistant_refuses(
    hass, engines, sent
):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    set_avatar(hass, entry, engine="tts.gone")

    with pytest.raises(HomeAssistantError) as raised:
        await speak(hass, avatar_id)
    assert raised.value.translation_key == "language_not_supported"
    assert engines["cloud"].calls == []
    assert sent == []


async def test_an_engine_that_vanished_mid_request_fails_as_not_found(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    entity = tts.get_engine_instance(hass, avatar_id)
    set_avatar(hass, entry, engine="tts.gone")

    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_get_tts_audio("Hello", "en-GB", {})
    assert raised.value.translation_key == "provider_not_found"


async def test_an_avatar_never_wraps_another_avatar(hass, engines, sent):
    entry = await add_router(hass)
    first = await add_avatar(hass, entry, "tts.cloud_voice")
    entity = tts.get_engine_instance(hass, first)
    set_avatar(hass, entry, engine=first)

    with pytest.raises(HomeAssistantError):
        await entity.async_get_tts_audio("Hello", "en-GB", {})


# Without an avatar, nothing changes


async def test_an_assistant_without_an_avatar_has_no_tts_entity(hass):
    entry = await add_router(hass)

    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert [entity.domain for entity in entities] == ["conversation"]
    assert "tts" not in hass.config.components


async def test_removing_the_avatar_removes_its_entity(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    subentry_id = next(iter(entry.subentries))

    hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(avatar_id) is None
    assert hass.states.get(avatar_id) is None
    assert entry.state is ConfigEntryState.LOADED
    unloaded = hass.config_entries.async_unload_platforms
    with patch.object(hass.config_entries, "async_unload_platforms", wraps=unloaded) as unload:
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert unload.call_args.args[1] == [Platform.CONVERSATION]


# The Add avatar and Reconfigure dialogs


async def start_flow(hass, entry, source: str = "user", subentry_id: str | None = None):
    context: dict[str, Any] = {"source": source}
    if subentry_id is not None:
        context["subentry_id"] = subentry_id
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, avatar.SUBENTRY_TYPE), context=context
    )


AVATAR_FORM = {
    "name": "Ada avatar",
    avatar.CONF_ENGINE: "tts.cloud_voice",
    avatar.CONF_URL: "http://livetalking.local:8010/",
    avatar.CONF_ENABLED: True,
}


async def test_adding_an_avatar_creates_its_tts_entity(hass, engines, sent):
    entry = await add_router(hass)

    result = await start_flow(hass, entry)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], AVATAR_FORM)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = entry.subentries.values()
    assert subentry.title == "Ada avatar"
    assert dict(subentry.data) == {
        avatar.CONF_ENGINE: "tts.cloud_voice",
        avatar.CONF_URL: BASE,
        avatar.CONF_ENABLED: True,
        avatar.CONF_AVATAR_ID: "",
    }
    assert hass.states.get("tts.ada_avatar") is not None


@pytest.mark.parametrize("url", ["livetalking.local", "ftp://livetalking.local"])
async def test_the_add_dialog_refuses_an_address_that_is_not_http(hass, engines, url):
    entry = await add_router(hass)

    result = await start_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**AVATAR_FORM, avatar.CONF_URL: url}
    )

    assert result["errors"] == {avatar.CONF_URL: "invalid_url"}
    assert entry.subentries == {}


async def test_the_add_dialog_refuses_an_avatar_as_the_engine(hass, engines, sent):
    entry = await add_router(hass)
    first = await add_avatar(hass, entry, "tts.cloud_voice")

    result = await start_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**AVATAR_FORM, "name": "Second", avatar.CONF_ENGINE: first}
    )

    assert result["errors"] == {avatar.CONF_ENGINE: "engine_is_avatar"}


async def test_reconfigure_switches_the_engine_without_a_new_entity(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    entity = tts.get_engine_instance(hass, avatar_id)
    subentry_id = next(iter(entry.subentries))

    result = await start_flow(hass, entry, "reconfigure", subentry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {**AVATAR_FORM, avatar.CONF_ENGINE: "tts.local_voice", avatar.CONF_ENABLED: False},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data[avatar.CONF_ENGINE] == "tts.local_voice"
    assert entry.subentries[subentry_id].data[avatar.CONF_ENABLED] is False
    assert tts.get_engine_instance(hass, avatar_id) is entity


async def test_saving_assistant_settings_does_not_reload_it(hass, engines, sent):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_entry(entry, options={**entry.options, "extra": 1})
        set_avatar(hass, entry, enabled=False)
        await hass.async_block_till_done()

    reload.assert_not_called()


async def test_renaming_an_avatar_renames_its_entity(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    subentry = next(iter(entry.subentries.values()))

    hass.config_entries.async_update_subentry(entry, subentry, title="Ada face")
    await hass.async_block_till_done()

    assert hass.states.get(avatar_id).name == "Ada face"



async def test_the_add_dialog_refuses_an_engine_that_does_not_exist(hass, engines):
    entry = await add_router(hass)

    result = await start_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**AVATAR_FORM, avatar.CONF_ENGINE: "tts.nope"}
    )

    assert result["errors"] == {avatar.CONF_ENGINE: "engine_missing"}


async def test_reconfigure_refuses_the_avatar_itself_as_its_engine(hass, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    subentry_id = next(iter(entry.subentries))

    result = await start_flow(hass, entry, "reconfigure", subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**AVATAR_FORM, avatar.CONF_ENGINE: avatar_id}
    )

    assert result["errors"] == {avatar.CONF_ENGINE: "engine_is_avatar"}
    assert entry.subentries[subentry_id].data[avatar.CONF_ENGINE] == "tts.cloud_voice"


async def test_a_send_still_running_is_cancelled_when_the_assistant_unloads(
    hass, engines, monkeypatch
):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hang(self, *args):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(avatar.AvatarSender, "async_send", hang)
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    await speak(hass, avatar_id)
    await started.wait()

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert cancelled.is_set()
