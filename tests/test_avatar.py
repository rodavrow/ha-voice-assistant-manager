# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The avatar helpers: LiveTalking address, clip preparation and the LiveTalking client."""

import asyncio
import io
import logging
import wave

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.voice_assistant_manager import avatar

BASE = "http://livetalking.local:8010"
SESSIONS = f"{BASE}/api/admin/sessions"
INTERRUPT = f"{BASE}/interrupt_talk"
HUMANAUDIO = f"{BASE}/humanaudio"
MP3 = b"ID3\x04\x00" + b"\x00" * 32
MPEG_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 32
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
PCM = b"\x01\x02" * 800


def sessions_reply(*ids: str) -> dict:
    return {"code": 0, "data": {"sessions": [{"sessionid": i, "speaking": False} for i in ids]}}


def form_fields(form: aiohttp.FormData) -> dict[str, tuple[str | None, bytes | str]]:
    """name -> (filename, value) of a multipart form, as it will be sent."""
    fields = {}
    for options, _headers, value in form._fields:
        fields[options["name"]] = (options.get("filename"), value)
    return fields


# Address


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://livetalking.local:8010", BASE),
        ("http://livetalking.local:8010/", BASE),
        ("  https://avatar.example/  ", "https://avatar.example"),
        ("http://10.0.0.5:8010/sub/", "http://10.0.0.5:8010/sub"),
    ],
)
def test_normalize_url_keeps_http_addresses_without_the_trailing_slash(url, expected):
    assert avatar.normalize_url(url) == expected


@pytest.mark.parametrize(
    "url",
    ["", "livetalking.local:8010", "ftp://host", "file:///etc/passwd", "http://", "http:///x",
     "http://host?x=1", "http://user:pw@host"],
)
def test_normalize_url_refuses_anything_but_a_plain_http_address(url):
    assert avatar.normalize_url(url) is None


# Clips


@pytest.mark.parametrize(("extension", "audio"), [("mp3", MP3), ("mp3", MPEG_FRAME), ("wav", WAV)])
def test_a_clip_in_a_container_is_sent_unchanged(extension, audio):
    assert avatar.prepare_clip(extension, audio, {}) == (extension, audio)


def test_raw_pcm_is_wrapped_as_wav_with_the_rate_from_the_options():
    options = {
        "preferred_sample_rate": 22050,
        "preferred_sample_channels": 1,
        "preferred_sample_bytes": 2,
    }

    extension, audio = avatar.prepare_clip("pcm", PCM, options)

    assert extension == "wav"
    with wave.open(io.BytesIO(audio)) as clip:
        assert (clip.getframerate(), clip.getnchannels(), clip.getsampwidth()) == (22050, 1, 2)
        assert clip.readframes(clip.getnframes()) == PCM


def test_raw_pcm_without_a_known_rate_is_not_sent():
    assert avatar.prepare_clip("raw", PCM, {}) is None


def test_raw_pcm_detected_from_the_bytes_not_the_engine_wav_header_is_kept():
    """A 'pcm' extension on bytes that already carry a WAV header: sent as the WAV it is."""
    assert avatar.prepare_clip("pcm", WAV, {}) == ("wav", WAV)


def test_an_empty_or_huge_clip_is_not_sent():
    assert avatar.prepare_clip("mp3", b"", {}) is None
    assert avatar.prepare_clip("mp3", b"ID3" + b"\x00" * avatar.MAX_CLIP_BYTES, {}) is None


# LiveTalking client


async def test_play_interrupts_then_sends_the_clip_to_every_session(hass, aioclient_mock):
    aioclient_mock.get(SESSIONS, json=sessions_reply("one", "two"))
    aioclient_mock.post(INTERRUPT, json={"code": 0})
    aioclient_mock.post(HUMANAUDIO, json={"code": 0})
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    played = await client.async_play("mp3", MP3)

    assert played == 2
    calls = [
        (method.lower(), str(url)) for method, url, _data, _headers in aioclient_mock.mock_calls
    ]
    assert calls == [
        ("get", SESSIONS),
        ("post", INTERRUPT),
        ("post", HUMANAUDIO),
        ("post", INTERRUPT),
        ("post", HUMANAUDIO),
    ]
    assert aioclient_mock.mock_calls[1][2] == {"sessionid": "one"}
    assert form_fields(aioclient_mock.mock_calls[2][2]) == {
        "sessionid": (None, "one"),
        "file": ("speech.mp3", MP3),
    }
    assert form_fields(aioclient_mock.mock_calls[4][2])["sessionid"] == (None, "two")


async def test_play_with_no_sessions_sends_nothing(hass, aioclient_mock):
    aioclient_mock.get(SESSIONS, json=sessions_reply())
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    assert await client.async_play("mp3", MP3) == 0
    assert aioclient_mock.call_count == 1


async def test_play_sends_to_at_most_max_sessions(hass, aioclient_mock):
    ids = [f"s{n}" for n in range(avatar.MAX_SESSIONS + 3)]
    aioclient_mock.get(SESSIONS, json=sessions_reply(*ids))
    aioclient_mock.post(INTERRUPT, json={"code": 0})
    aioclient_mock.post(HUMANAUDIO, json={"code": 0})
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    assert await client.async_play("mp3", MP3) == avatar.MAX_SESSIONS


@pytest.mark.parametrize(
    "reply",
    [
        {"code": 1, "msg": "no"},
        {"code": 0, "data": {"sessions": "nope"}},
        {"code": 0, "data": {"sessions": [{"speaking": True}]}},
        {"code": 0, "data": {"sessions": [{"sessionid": 5.5}]}},
        {"code": 0, "data": {"sessions": [{"sessionid": True}]}},
        ["not", "a", "dict"],
    ],
)
async def test_play_refuses_an_unexpected_sessions_reply(hass, aioclient_mock, reply):
    aioclient_mock.get(SESSIONS, json=reply)
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    with pytest.raises(avatar.LiveTalkingError):
        await client.async_play("mp3", MP3)
    assert aioclient_mock.call_count == 1


async def test_play_raises_on_an_http_error(hass, aioclient_mock):
    aioclient_mock.get(SESSIONS, status=500)
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    with pytest.raises(aiohttp.ClientError):
        await client.async_play("mp3", MP3)


# Sender: never raises, warns once


async def test_send_never_raises_and_warns_once_per_outage(hass, aioclient_mock, caplog):
    aioclient_mock.get(SESSIONS, exc=aiohttp.ClientConnectionError("refused"))
    sender = avatar.AvatarSender(hass, "tts.ada_avatar")
    caplog.set_level(logging.DEBUG, logger=avatar.__name__)

    await sender.async_send(BASE, "mp3", MP3, {})
    await sender.async_send(BASE, "mp3", MP3, {})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "tts.ada_avatar" in warnings[0].getMessage()


async def test_send_warns_again_after_livetalking_came_back(hass, aioclient_mock, caplog):
    sender = avatar.AvatarSender(hass, "tts.ada_avatar")
    aioclient_mock.get(SESSIONS, exc=TimeoutError())
    await sender.async_send(BASE, "mp3", MP3, {})
    aioclient_mock.clear_requests()
    aioclient_mock.get(SESSIONS, json=sessions_reply())
    await sender.async_send(BASE, "mp3", MP3, {})
    aioclient_mock.clear_requests()
    aioclient_mock.get(SESSIONS, exc=TimeoutError())

    await sender.async_send(BASE, "mp3", MP3, {})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


async def test_send_skips_a_clip_it_cannot_prepare_without_calling_livetalking(
    hass, aioclient_mock
):
    sender = avatar.AvatarSender(hass, "tts.ada_avatar")

    await sender.async_send(BASE, "raw", PCM, {})

    assert aioclient_mock.call_count == 0


# Review fixes


def test_raw_pcm_that_looks_like_an_mpeg_frame_is_still_raw():
    """Leading samples of -1 are FF FF, which is also an MPEG frame sync."""
    silence = b"\xff\xff" * 400

    assert avatar.prepare_clip("pcm", silence, {}) is None
    extension, audio = avatar.prepare_clip("pcm", silence, {"preferred_sample_rate": 16000})
    assert extension == "wav"
    assert audio.startswith(b"RIFF")


async def test_a_reply_that_is_not_json_warns_once(hass, aioclient_mock, caplog):
    aioclient_mock.get(SESSIONS, text="<html>not here</html>")
    sender = avatar.AvatarSender(hass, "tts.ada_avatar")

    await sender.async_send(BASE, "mp3", MP3, {})
    await sender.async_send(BASE, "mp3", MP3, {})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


async def test_a_sample_format_wave_cannot_write_is_skipped_without_raising(
    hass, aioclient_mock
):
    sender = avatar.AvatarSender(hass, "tts.ada_avatar")
    options = {"preferred_sample_rate": 16000, "preferred_sample_bytes": 7}

    await sender.async_send(BASE, "pcm", PCM, options)

    assert aioclient_mock.call_count == 0


async def test_numeric_session_ids_are_sent_as_text(hass, aioclient_mock):
    aioclient_mock.get(SESSIONS, json={"code": 0, "data": {"sessions": [{"sessionid": 482913}]}})
    aioclient_mock.post(INTERRUPT, json={"code": 0})
    aioclient_mock.post(HUMANAUDIO, json={"code": 0})
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    assert await client.async_play("mp3", MP3) == 1
    assert aioclient_mock.mock_calls[1][2] == {"sessionid": "482913"}


async def test_clips_reach_livetalking_in_the_order_they_were_made(hass, monkeypatch):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    played: list[bytes] = []

    async def play(self, extension, audio):
        if audio == MP3:
            first_started.set()
            await release_first.wait()
        played.append(audio)
        return 1

    monkeypatch.setattr(avatar.LiveTalkingClient, "async_play", play)
    sender = avatar.AvatarSender(hass, "tts.ada_avatar")
    later = b"ID3" + b"\x09" * 40

    first = hass.async_create_task(sender.async_send(BASE, "mp3", MP3, {}))
    await first_started.wait()
    second = hass.async_create_task(sender.async_send(BASE, "mp3", later, {}))
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.gather(first, second)

    assert played == [MP3, later]
