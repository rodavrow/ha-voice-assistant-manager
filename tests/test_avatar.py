# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The avatar helpers: LiveTalking address, clip preparation and the LiveTalking client."""

import asyncio
import io
import logging
import wave

import aiohttp
import pytest
from homeassistant.helpers import entity_registry as er
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

    async def play(self, extension, audio, skip=frozenset(), avatar_id=""):
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


async def test_sessions_of_satellites_are_skipped(hass, aioclient_mock):
    """A satellite in avatar mode sends its own speech to its session: never twice."""
    aioclient_mock.get(SESSIONS, json=sessions_reply("console", "kitchen-device"))
    aioclient_mock.post(INTERRUPT, json={"code": 0})
    aioclient_mock.post(HUMANAUDIO, json={"code": 0})
    session = er.async_get(hass).async_get_or_create(
        "sensor", "esphome", "aa:bb:cc:dd:ee:ff/4/text_sensor/Avatar session"
    )
    hass.states.async_set(session.entity_id, "kitchen-device")
    sender = avatar.AvatarSender(hass, "tts.ada_avatar")

    await sender.async_send(BASE, "mp3", MP3, {})

    sent_to = [data for method, _url, data, _h in aioclient_mock.mock_calls if method == "POST"]
    assert sent_to[0] == {"sessionid": "console"}
    assert len(sent_to) == 2


# Only the same avatar's viewers get the copy


def avatar_sessions(*pairs: tuple[str, str]) -> dict:
    """A sessions reply: (session id, the avatar it shows)."""
    return {
        "code": 0,
        "data": {"sessions": [{"sessionid": s, "avatar_id": a} for s, a in pairs]},
    }


def posted_sessions(aioclient_mock) -> list[str]:
    return [
        data["sessionid"]
        for method, url, data, _h in aioclient_mock.mock_calls
        if method.lower() == "post" and str(url).endswith("/interrupt_talk")
    ]


async def test_the_copy_only_reaches_viewers_of_the_same_avatar(hass, aioclient_mock):
    aioclient_mock.get(
        SESSIONS, json=avatar_sessions(("s1", "ada"), ("o1", "jeeves"), ("s2", "ada"))
    )
    aioclient_mock.post(INTERRUPT, json={"code": 0})
    aioclient_mock.post(HUMANAUDIO, json={"code": 0})
    sender = avatar.AvatarSender(hass, "tts.ada_avatar")

    await sender.async_send(BASE, "mp3", MP3, {}, avatar_id="ada")

    assert posted_sessions(aioclient_mock) == ["s1", "s2"]


async def test_without_an_avatar_id_every_viewer_gets_the_copy(hass, aioclient_mock):
    aioclient_mock.get(SESSIONS, json=avatar_sessions(("s1", "ada"), ("o1", "jeeves")))
    aioclient_mock.post(INTERRUPT, json={"code": 0})
    aioclient_mock.post(HUMANAUDIO, json={"code": 0})
    sender = avatar.AvatarSender(hass, "tts.ada_avatar")

    await sender.async_send(BASE, "mp3", MP3, {}, avatar_id="")

    assert posted_sessions(aioclient_mock) == ["s1", "o1"]


async def test_each_session_is_logged_with_why_it_was_skipped(hass, aioclient_mock, caplog):
    aioclient_mock.get(SESSIONS, json=avatar_sessions(("own", "ada"), ("o1", "jeeves")))
    session = er.async_get(hass).async_get_or_create(
        "sensor", "esphome", "aa:bb:cc:dd:ee:ff/4/text_sensor/Avatar session"
    )
    hass.states.async_set(session.entity_id, "own")
    sender = avatar.AvatarSender(hass, "tts.ada_avatar")
    caplog.set_level(logging.DEBUG, logger=avatar.__name__)

    await sender.async_send(BASE, "mp3", MP3, {}, avatar_id="ada")

    text = "\n".join(r.getMessage() for r in caplog.records if r.name == avatar.__name__)
    assert "own" in text and "satellite" in text
    assert "o1" in text and "jeeves" in text


# The chat stage's calls


OFFER = f"{BASE}/offer"
IS_SPEAKING = f"{BASE}/is_speaking"


async def test_offer_opens_a_session_for_the_avatar(hass, aioclient_mock):
    aioclient_mock.post(OFFER, json={"sdp": "answer", "type": "answer", "sessionid": 7})
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    assert await client.async_offer("offer", "ada") == ("answer", "7")
    assert aioclient_mock.mock_calls[0][2] == {"sdp": "offer", "type": "offer", "avatar": "ada"}


async def test_offer_for_the_default_avatar_names_none(hass, aioclient_mock):
    aioclient_mock.post(OFFER, json={"sdp": "answer", "type": "answer", "sessionid": "s1"})
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    await client.async_offer("offer")
    assert aioclient_mock.mock_calls[0][2] == {"sdp": "offer", "type": "offer"}


@pytest.mark.parametrize(
    "reply",
    [
        {"sdp": "", "type": "answer", "sessionid": "s1"},
        {"sdp": "answer", "type": "offer", "sessionid": "s1"},
        {"sdp": "answer", "type": "answer"},
        {"sdp": "answer", "type": "answer", "sessionid": True},
    ],
)
async def test_offer_refuses_an_unexpected_reply(hass, aioclient_mock, reply):
    aioclient_mock.post(OFFER, json=reply)
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    with pytest.raises(avatar.LiveTalkingError):
        await client.async_offer("offer")


async def test_say_goes_to_one_session_only(hass, aioclient_mock):
    aioclient_mock.post(INTERRUPT, json={"code": 0})
    aioclient_mock.post(HUMANAUDIO, json={"code": 0})
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    await client.async_say("s1", "mp3", MP3)

    assert [str(url) for _m, url, _d, _h in aioclient_mock.mock_calls] == [INTERRUPT, HUMANAUDIO]
    assert form_fields(aioclient_mock.mock_calls[1][2])["sessionid"] == (None, "s1")


async def test_say_sends_the_emotion_with_the_clip_when_given(hass, aioclient_mock):
    aioclient_mock.post(INTERRUPT, json={"code": 0})
    aioclient_mock.post(HUMANAUDIO, json={"code": 0})
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    await client.async_say("s1", "mp3", MP3, emotion="sad")

    assert form_fields(aioclient_mock.mock_calls[1][2])["emotion"] == (None, "sad")


async def test_say_sends_no_emotion_when_none_is_given(hass, aioclient_mock):
    aioclient_mock.post(INTERRUPT, json={"code": 0})
    aioclient_mock.post(HUMANAUDIO, json={"code": 0})
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    await client.async_say("s1", "mp3", MP3)

    assert "emotion" not in form_fields(aioclient_mock.mock_calls[1][2])


@pytest.mark.parametrize(("data", "speaking"), [(True, True), (False, False)])
async def test_speaking_asks_the_session(hass, aioclient_mock, data, speaking):
    aioclient_mock.post(IS_SPEAKING, json={"code": 0, "data": data})
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    assert await client.async_speaking("s1") is speaking
    assert aioclient_mock.mock_calls[0][2] == {"sessionid": "s1"}


@pytest.mark.parametrize("reply", [{"code": 1}, {"code": 0, "data": "yes"}, ["no"]])
async def test_speaking_refuses_an_unexpected_reply(hass, aioclient_mock, reply):
    aioclient_mock.post(IS_SPEAKING, json=reply)
    client = avatar.LiveTalkingClient(async_get_clientsession(hass), BASE)

    with pytest.raises(avatar.LiveTalkingError):
        await client.async_speaking("s1")


# A streamed WAV's header


def wav_clip(samples: int, *, claimed: int | None = None, extra_chunk: bool = False) -> bytes:
    """A 16 kHz mono 16-bit WAV with `samples` samples, whose header claims `claimed` bytes."""
    data = b"\x01\x02" * samples
    claimed_bytes = len(data) if claimed is None else claimed
    fmt = b"fmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (
        (1).to_bytes(2, "little") + (16000).to_bytes(4, "little")
        + (32000).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    )
    extra = b"LIST" + (4).to_bytes(4, "little") + b"INFO" if extra_chunk else b""
    body = b"WAVE" + fmt + extra + b"data" + claimed_bytes.to_bytes(4, "little") + data
    riff = (4 + len(fmt) + len(extra) + 8 + claimed_bytes) & 0xFFFFFFFF
    return b"RIFF" + riff.to_bytes(4, "little") + body


def wav_frames(audio: bytes) -> int:
    with wave.open(io.BytesIO(audio)) as clip:
        return clip.getnframes()


@pytest.mark.parametrize("claimed", [200, 0, 0xFFFFFFFF])
def test_regression_clip_end_a_wav_header_with_the_wrong_length_is_corrected(claimed):
    # LiveTalking reads as many samples as the header says: a streamed WAV whose header
    # says fewer than it has lost the end of the line.
    clip = wav_clip(16000, claimed=claimed)

    _extension, sent = avatar.prepare_clip("wav", clip, {})

    assert wav_frames(sent) == 16000
    assert len(sent) == len(clip)
    assert int.from_bytes(sent[4:8], "little") == len(sent) - 8


def test_a_wav_with_a_correct_header_is_sent_unchanged():
    clip = wav_clip(16000)

    assert avatar.prepare_clip("wav", clip, {}) == ("wav", clip)


def test_a_wav_header_is_corrected_past_other_chunks():
    clip = wav_clip(8000, claimed=20, extra_chunk=True)

    _extension, sent = avatar.prepare_clip("wav", clip, {})

    assert wav_frames(sent) == 8000


def test_a_wav_without_a_data_chunk_is_sent_unchanged():
    assert avatar.prepare_clip("wav", WAV, {}) == ("wav", WAV)


# A satellite's session while its sensor cannot be read


SESSION_SENSOR = "sensor.kitchen_assistant_1_avatar_session"


def add_session_sensor(hass, state: str | None, *, disabled: bool = False) -> None:
    entry = er.async_get(hass).async_get_or_create(
        "sensor", "esphome", "aa:bb:cc:dd:ee:ff/4/text_sensor/Avatar session",
        suggested_object_id="kitchen_assistant_1_avatar_session",
        disabled_by=er.RegistryEntryDisabler.USER if disabled else None,
    )
    assert entry.entity_id == SESSION_SENSOR
    if state is not None:
        hass.states.async_set(SESSION_SENSOR, state)


def test_regression_double_clip_a_session_is_still_skipped_while_its_sensor_is_unavailable(hass):
    # Seen in the house: after a restart of Home Assistant the satellite's sensors were
    # unavailable while it kept its LiveTalking session, so its avatar got each reply from
    # the satellite and a copy from here, and said it two or three times.
    add_session_sensor(hass, "abc-1")
    assert avatar.device_sessions(hass) == frozenset({"abc-1"})

    hass.states.async_set(SESSION_SENSOR, "unavailable")

    assert avatar.device_sessions(hass) == frozenset({"abc-1"})
    assert avatar.unknown_device_sessions(hass) == []


@pytest.mark.parametrize("state", ["unavailable", "unknown", None])
def test_a_session_sensor_that_was_never_read_is_an_unknown_session(hass, state):
    add_session_sensor(hass, state)

    assert avatar.device_sessions(hass) == frozenset()
    assert avatar.unknown_device_sessions(hass) == [SESSION_SENSOR]


def test_a_satellite_that_says_it_has_no_session_has_none(hass):
    add_session_sensor(hass, "abc-1")
    avatar.device_sessions(hass)
    hass.states.async_set(SESSION_SENSOR, "")
    assert avatar.device_sessions(hass) == frozenset()

    hass.states.async_set(SESSION_SENSOR, "unavailable")

    assert avatar.device_sessions(hass) == frozenset()
    assert avatar.unknown_device_sessions(hass) == []


def test_a_disabled_session_sensor_is_no_unknown_session(hass):
    add_session_sensor(hass, None, disabled=True)

    assert avatar.unknown_device_sessions(hass) == []


async def test_regression_double_clip_no_copy_is_sent_while_a_session_is_unknown(
    hass, aioclient_mock, caplog
):
    add_session_sensor(hass, "unavailable")
    aioclient_mock.get(SESSIONS, json=sessions_reply("live-on-the-satellite"))
    aioclient_mock.post(INTERRUPT, json={"code": 0})
    aioclient_mock.post(HUMANAUDIO, json={"code": 0})
    sender = avatar.AvatarSender(hass, "tts.ada_avatar")

    await sender.async_send(BASE, "mp3", MP3, {})
    await sender.async_send(BASE, "mp3", MP3, {})

    assert aioclient_mock.call_count == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert SESSION_SENSOR in warnings[0].getMessage()


async def test_copies_go_out_again_once_the_session_is_known(hass, aioclient_mock):
    add_session_sensor(hass, "unavailable")
    aioclient_mock.get(SESSIONS, json=sessions_reply("abc-1", "viewer"))
    aioclient_mock.post(INTERRUPT, json={"code": 0})
    aioclient_mock.post(HUMANAUDIO, json={"code": 0})
    sender = avatar.AvatarSender(hass, "tts.ada_avatar")
    await sender.async_send(BASE, "mp3", MP3, {})

    hass.states.async_set(SESSION_SENSOR, "abc-1")
    await sender.async_send(BASE, "mp3", MP3, {})

    uploads = [c for c in aioclient_mock.mock_calls if str(c[1]) == HUMANAUDIO]
    assert [form_fields(c[2])["sessionid"][1] for c in uploads] == ["viewer"]
