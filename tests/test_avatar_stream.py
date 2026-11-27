# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Streamed avatar replies: who asked, where to feed, and the feed to LiveTalking."""

import asyncio
import json
import shutil
import struct
import subprocess
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.components.tts.const import DATA_TTS_MANAGER
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from custom_components.voice_assistant_manager import avatar_stream
from custom_components.voice_assistant_manager.avatar import (
    device_sessions,
    present_device_sessions,
)
from custom_components.voice_assistant_manager.avatar_stream import (
    FeedTarget,
    ReplyFeed,
    asking_satellite,
    decoder_arguments,
    feed_target,
    turn_ended,
    turn_started,
)

from .router_helpers import FakeAgents, add_router, options_with, router_entity_id
from .test_avatar_devices import (  # noqa: F401 - fixtures
    MAC,
    SATELLITE,
    Device,
    add_entity,
    add_satellite_device,
    assistant_switch,
    device,
    reconcile,
    slots,
)
from .test_avatar_tts import (  # noqa: F401 - fixtures
    BASE,
    WAV_CHUNKS,
    add_avatar,
    engines,
    sent,
    speak,
)

SESSION_ENTITY = "sensor.kitchen_assistant_1_avatar_session"
FED_SWITCH = "switch.kitchen_assistant_1_avatar_fed"
OTHER_SATELLITE = "assist_satellite.office_satellite"


def text(payload: dict[str, Any]) -> aiohttp.WSMessage:
    return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(payload), None)


CLOSED = aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)


class FakeSocket:
    """LiveTalking's end of /stream_audio: says "ready", records what it is sent."""

    def __init__(self, first: dict[str, Any] | None) -> None:
        self.incoming: asyncio.Queue[aiohttp.WSMessage] = asyncio.Queue()
        self.sent: list[Any] = []
        self.closed = False
        if first is not None:
            self.incoming.put_nowait(text(first))

    async def receive(self) -> aiohttp.WSMessage:
        return await self.incoming.get()

    def __aiter__(self) -> "FakeSocket":
        return self

    async def __anext__(self) -> aiohttp.WSMessage:
        message = await self.incoming.get()
        if message.type is aiohttp.WSMsgType.CLOSED:
            raise StopAsyncIteration
        return message

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True
        self.incoming.put_nowait(CLOSED)

    def says(self, payload: dict[str, Any]) -> None:
        self.incoming.put_nowait(text(payload))


class FakeLive:
    """Stands in for the HTTP session: one socket per reply."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, dict[str, str]]] = []
        self.sockets: list[FakeSocket] = []
        self.first: dict[str, Any] | None = {"type": "ready"}
        self.refuses: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.events: list[str] = []
        self.decoders: list[FakeDecoder] = []

    async def ws_connect(self, url: str, *, params: dict[str, str], **kwargs: Any) -> FakeSocket:
        self.connect_options = kwargs
        if self.refuses is not None:
            raise self.refuses
        if self.gate is not None:
            await self.gate.wait()
        self.opened.append((url, dict(params)))
        self.events.append("opened")
        self.sockets.append(FakeSocket(self.first))
        return self.sockets[-1]

    @property
    def socket(self) -> FakeSocket:
        return self.sockets[-1]


class FakeDecoder:
    """Passes the audio on with a mark, so the tests see it went through the decoder."""

    def __init__(self) -> None:
        self._pcm: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.stopped = False

    async def async_write(self, audio: bytes) -> None:
        self._pcm.put_nowait(b"pcm:" + audio)

    async def async_close_input(self) -> None:
        self._pcm.put_nowait(None)

    async def output(self) -> AsyncIterator[bytes]:
        while (pcm := await self._pcm.get()) is not None:
            yield pcm

    async def async_stop(self) -> None:
        self.stopped = True


@pytest.fixture
def live():
    fake = FakeLive()

    async def start_decoder(hass, extension, options):
        fake.decoders.append(FakeDecoder())
        return fake.decoders[-1]

    with (
        patch.object(avatar_stream, "async_get_clientsession", return_value=fake),
        patch.object(avatar_stream, "async_start_decoder", start_decoder),
    ):
        yield fake


# Which satellite asked


def test_the_satellite_of_a_running_turn_is_the_one_asking(hass):
    turn_started(hass, "entry", SATELLITE)

    assert asking_satellite(hass, "entry") == SATELLITE
    assert asking_satellite(hass, "another entry") is None


def test_a_turn_that_just_ended_still_counts_and_an_older_one_does_not(hass):
    clock = [100.0]
    with patch.object(avatar_stream, "_now", lambda: clock[0]):
        turn_ended(turn_started(hass, "entry", SATELLITE))
        clock[0] += avatar_stream.TURN_GRACE

        assert asking_satellite(hass, "entry") == SATELLITE

        clock[0] += 0.1

        assert asking_satellite(hass, "entry") is None


def test_two_satellites_asking_the_same_assistant_are_not_told_apart(hass):
    turn_started(hass, "entry", SATELLITE)
    turn_started(hass, "entry", OTHER_SATELLITE)

    assert asking_satellite(hass, "entry") is None


def test_two_turns_of_one_satellite_are_that_satellite(hass):
    turn_ended(turn_started(hass, "entry", SATELLITE))
    turn_started(hass, "entry", SATELLITE)

    assert asking_satellite(hass, "entry") == SATELLITE


def test_a_request_without_a_satellite_is_nobody_asking(hass):
    assert turn_started(hass, "entry", None) is None
    turn_ended(None)

    assert asking_satellite(hass, "entry") is None


def test_regression_s13_old_turns_are_dropped_when_a_new_one_starts(hass):
    """An assistant without a streaming avatar never asks who is asking, so the turns
    must not pile up until then."""
    clock = [100.0]
    with patch.object(avatar_stream, "_now", lambda: clock[0]):
        for _ in range(50):
            turn_ended(turn_started(hass, "entry", SATELLITE))
            clock[0] += avatar_stream.TURN_GRACE + 1

        turn_started(hass, "entry", SATELLITE)

    assert len(avatar_stream._turns(hass)["entry"]) == 1


async def test_regression_s13_an_unloaded_assistant_leaves_no_turns_behind(hass):
    entry = await add_router(hass)
    turn_started(hass, entry.entry_id, SATELLITE)

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert entry.entry_id not in avatar_stream._turns(hass)


async def test_the_router_notes_the_satellite_while_it_answers(hass):
    entry = await add_router(hass)
    seen: list[str | None] = []
    agents = FakeAgents()
    answer = agents.converse

    async def converse(*args, **kwargs):
        seen.append(asking_satellite(hass, entry.entry_id))
        return await answer(*args, **kwargs)

    entity_id = router_entity_id(hass, entry)
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(entity_id)
    with patch.object(conversation, "async_converse", converse):
        await entity.async_process(
            conversation.ConversationInput(
                text="Hi", context=Context(), conversation_id=None, device_id=None,
                satellite_id=SATELLITE, language="en", agent_id=entity_id,
            )
        )

    assert seen == [SATELLITE]
    assert asking_satellite(hass, entry.entry_id) == SATELLITE  # within the grace


# Where to feed


def add_fed_entities(hass, slot: int = 1, *, session: str = "sess-1", fed: str | None = "on"):
    """The session sensor and, on newer firmware, the fed switch of a slot's sub-device."""
    devices = dr.async_get(hass)
    sub = next(d for d in devices.devices if d.name == f"kitchen assistant {slot}")
    owner = hass.config_entries.async_get_entry(sub.config_entry_id)
    index = slot + 3
    add_entity(
        hass, owner, sub.id, "sensor", f"{MAC}/{index}/text_sensor/Avatar session", session,
        named=f"kitchen_assistant_{slot}_avatar_session",
    )
    if fed is not None:
        add_entity(
            hass, owner, sub.id, "switch",
            f"{MAC}/{index}/switch/Avatar fed by Home Assistant", fed,
            named=f"kitchen_assistant_{slot}_avatar_fed",
        )


async def asked_router(hass, options=None):
    entry = await add_router(hass, options)
    turn_started(hass, entry.entry_id, SATELLITE)
    return entry


async def test_the_asking_satellites_session_is_the_target(hass, device):
    add_fed_entities(hass)
    entry = await asked_router(hass)

    with slots(entry, None):
        target = feed_target(hass, entry, BASE)

    assert target == FeedTarget(BASE, "sess-1", SESSION_ENTITY)


@pytest.mark.parametrize(
    "changes",
    [{"fed": "off"}, {"fed": None}, {"fed": "unavailable"}, {"session": ""},
     {"session": "unavailable"}, {"session": "unknown"}],
)
async def test_no_target_without_the_fed_switch_on_and_a_session(hass, device, changes):
    add_fed_entities(hass, **changes)
    entry = await asked_router(hass)

    with slots(entry, None):
        assert feed_target(hass, entry, BASE) is None


async def test_no_target_when_nobody_is_known_to_be_asking(hass, device):
    add_fed_entities(hass)
    entry = await add_router(hass)

    with slots(entry, None):
        assert feed_target(hass, entry, BASE) is None


async def test_no_target_when_the_slot_uses_another_assistant(hass, device):
    add_fed_entities(hass)
    entry = await asked_router(hass)

    with slots(None, None):
        assert feed_target(hass, entry, BASE) is None


async def test_no_target_when_two_slots_of_the_satellite_use_the_assistant(hass, device):
    add_fed_entities(hass, 1)
    add_fed_entities(hass, 2, session="sess-2")
    entry = await asked_router(hass)

    with slots(entry, entry):
        assert feed_target(hass, entry, BASE) is None


async def test_no_target_when_streaming_is_switched_off_in_the_settings(hass, device):
    add_fed_entities(hass)
    entry = await asked_router(hass, options_with(advanced={"avatar_stream": False}))

    with slots(entry, None):
        assert feed_target(hass, entry, BASE) is None


# The fed switch follows the setting


async def test_the_fed_switch_is_turned_on_with_avatar_mode(hass, engines, sent, device):
    add_fed_entities(hass, fed="off")
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    device.calls.clear()

    with slots(entry, None):
        await reconcile(hass)

    assert f"turn_on {FED_SWITCH} None" in device.calls
    assert device.calls.index(f"turn_on {FED_SWITCH} None") < device.calls.index(
        f"turn_on {assistant_switch(1)} None"
    )


async def test_the_fed_switch_is_turned_off_when_streaming_is_off(
    hass, engines, sent, device
):
    add_fed_entities(hass, fed="on")
    entry = await add_router(hass, options_with(advanced={"avatar_stream": False}))
    await add_avatar(hass, entry, "tts.cloud_voice")
    device.calls.clear()

    with slots(entry, None):
        await reconcile(hass)

    assert f"turn_off {FED_SWITCH} None" in device.calls
    assert f"turn_on {assistant_switch(1)} None" in device.calls


async def test_a_satellite_without_the_fed_switch_still_gets_avatar_mode(
    hass, engines, sent, device
):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    device.calls.clear()

    with slots(entry, None):
        await reconcile(hass)

    assert device.calls[-1] == f"turn_on {assistant_switch(1)} None"
    assert not [call for call in device.calls if "avatar_fed" in call]


# Decoding


def test_a_known_container_is_named_to_ffmpeg_and_decoded_to_16_khz_mono():
    arguments = decoder_arguments("MP3", {})

    assert arguments[arguments.index("-i") - 2 : arguments.index("-i")] == ["-f", "mp3"]
    assert arguments[arguments.index("-i") + 2 :] == [
        "-f", "s16le", "-ac", "1", "-ar", "16000", "-flush_packets", "1", "pipe:1",
    ]


def test_raw_audio_is_described_to_ffmpeg_from_the_request():
    arguments = decoder_arguments(
        "pcm", {"preferred_sample_rate": 22050, "preferred_sample_channels": 2}
    )

    assert arguments[arguments.index("-i") - 6 : arguments.index("-i")] == [
        "-f", "s16le", "-ar", "22050", "-ac", "2",
    ]


def test_raw_audio_without_a_sample_rate_cannot_be_decoded():
    assert decoder_arguments("raw", {}) is None


def test_an_unknown_format_is_left_for_ffmpeg_to_recognise():
    arguments = decoder_arguments("opus", {})

    assert "-f" not in arguments[: arguments.index("-i")]
    assert "-probesize" not in arguments


def test_regression_stream_opening_ffmpeg_never_gets_the_flag_that_drops_it():
    # "-fflags nobuffer" made ffmpeg discard the audio it read while working out the
    # stream: the first 1.26 s of every streamed reply.
    for extension in ("mp3", "wav", "pcm", "opus"):
        assert "nobuffer" not in decoder_arguments(extension, {"preferred_sample_rate": 16000})


def test_a_named_format_starts_decoding_without_waiting_for_more_audio():
    arguments = decoder_arguments("mp3", {})

    assert arguments[arguments.index("-probesize") + 1] == "32"
    assert arguments[arguments.index("-analyzeduration") + 1] == "0"


def _tone(seconds: float, hertz: int) -> str:
    return f"sine=frequency={hertz}:duration={seconds}:sample_rate=44100"


def _hertz(samples: tuple[int, ...]) -> float:
    crossings = sum(1 for a, b in zip(samples, samples[1:], strict=False) if (a < 0) != (b < 0))
    return crossings / 2 / (len(samples) / avatar_stream.PCM_RATE)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs the ffmpeg program")
async def test_regression_stream_opening_real_ffmpeg_keeps_the_first_second(tmp_path):
    """A clip fed in small pieces, as an engine streams it, comes out whole from its start."""
    clip = tmp_path / "clip.mp3"
    subprocess.run(  # noqa: ASYNC221, S603 - a fixture made once, with fixed arguments
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", _tone(1, 880),
         "-f", "lavfi", "-i", _tone(2, 440), "-filter_complex", "[0][1]concat=n=2:v=0:a=1",
         "-b:a", "128k", str(clip)],
        check=True,
    )
    mp3 = clip.read_bytes()
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", *decoder_arguments("mp3", {}),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )

    async def feed() -> None:
        for start in range(0, len(mp3), 417):  # one MP3 frame at a time
            process.stdin.write(mp3[start : start + 417])
            await process.stdin.drain()
        process.stdin.close()

    _, pcm = await asyncio.gather(feed(), process.stdout.read(-1))
    await process.wait()

    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    assert len(samples) / avatar_stream.PCM_RATE == pytest.approx(3.0, abs=0.1)
    assert _hertz(samples[:1600]) == pytest.approx(880, abs=40)
    assert _hertz(samples[8000:9600]) == pytest.approx(880, abs=40)
    assert _hertz(samples[-8000:-6400]) == pytest.approx(440, abs=40)


# The feed


@pytest.fixture
def target(hass) -> FeedTarget:
    hass.states.async_set(SESSION_ENTITY, "sess-1")
    return FeedTarget(BASE, "sess-1", SESSION_ENTITY)


async def run(hass, feed: ReplyFeed) -> None:
    await asyncio.wait_for(feed.async_run(), timeout=5)


async def test_opening_asks_livetalking_for_the_session_with_a_new_reply_id(hass, live, target):
    feed = ReplyFeed(hass, target, "mp3", {})

    assert await feed.async_open() is True

    assert live.opened == [
        (f"{BASE}/stream_audio", {"sessionid": "sess-1", "reply_id": feed.reply_id})
    ]
    assert feed.reply_id != ReplyFeed(hass, target, "mp3", {}).reply_id


@pytest.mark.parametrize(
    "refusal",
    [aiohttp.ClientConnectionError(), aiohttp.WSServerHandshakeError(None, (), status=409)],
)
async def test_a_refused_open_is_not_fed(hass, live, target, refusal):
    live.refuses = refusal

    assert await ReplyFeed(hass, target, "mp3", {}).async_open() is False


async def test_an_open_that_is_not_answered_in_time_is_not_fed(hass, live, target):
    live.first = None

    with patch.object(avatar_stream, "OPEN_TIMEOUT", 0.01):
        assert await ReplyFeed(hass, target, "mp3", {}).async_open() is False

    assert live.socket.closed


async def test_an_answer_other_than_ready_is_not_fed(hass, live, target):
    live.first = {"type": "busy"}

    assert await ReplyFeed(hass, target, "mp3", {}).async_open() is False
    assert live.socket.closed


async def test_the_audio_is_decoded_and_sent_in_order_then_the_end(hass, live, target):
    feed = ReplyFeed(hass, target, "mp3", {})
    await feed.async_open()
    feed.write(b"one")
    feed.write(b"two")
    feed.finish()

    await run(hass, feed)

    assert live.socket.sent == [b"pcm:one", b"pcm:two", {"type": "end"}]
    assert live.socket.closed
    assert live.decoders[0].stopped


async def test_an_interrupt_stops_the_sending_and_sends_no_end(hass, live, target):
    feed = ReplyFeed(hass, target, "mp3", {})
    await feed.async_open()
    feed.write(b"one")
    live.socket.says({"type": "cancelled"})

    await run(hass, feed)
    feed.write(b"two")

    assert {"type": "end"} not in live.socket.sent
    assert b"pcm:two" not in live.socket.sent
    assert live.socket.closed


async def test_an_aborted_reply_sends_no_end(hass, live, target):
    feed = ReplyFeed(hass, target, "mp3", {})
    await feed.async_open()
    feed.abort()

    await run(hass, feed)

    assert live.socket.sent == []
    assert live.socket.closed


async def test_an_engine_that_never_starts_ends_the_reply_cleanly(hass, live, target):
    feed = ReplyFeed(hass, target, "mp3", {})
    await feed.async_open()

    with patch.object(avatar_stream, "STALL_TIMEOUT", 0.01):
        await run(hass, feed)

    assert live.socket.sent == [{"type": "end"}]


HOLD = {"type": "hold"}


async def until_sent(live, wanted, count: int = 1) -> None:
    async def wait() -> None:
        while live.socket.sent.count(wanted) < count:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=5)


async def test_regression_a_pause_mid_reply_holds_the_reply_open_and_goes_on(hass, live, target):
    # Seen in the house: a web search left 6.5 s without audio between two stretches of
    # one reply. The reply ended on the avatar and the answer played without lips.
    feed = ReplyFeed(hass, target, "mp3", {})
    await feed.async_open()
    feed.write(b"one")

    with (
        patch.object(avatar_stream, "HOLD_AFTER", 0.01),
        patch.object(avatar_stream, "HOLD_EVERY", 0.01),
    ):
        running = asyncio.create_task(feed.async_run())
        await until_sent(live, HOLD, 3)
        feed.write(b"two")
        feed.finish()
        await asyncio.wait_for(running, timeout=5)

    sent = live.socket.sent
    assert [item for item in sent if item != HOLD] == [b"pcm:one", b"pcm:two", {"type": "end"}]
    assert sent.index(b"pcm:one") < sent.index(HOLD) < sent.index(b"pcm:two")
    assert not live.socket.closed or sent[-1] == {"type": "end"}


async def test_a_pause_that_never_ends_closes_the_reply_at_the_limit(hass, live, target):
    feed = ReplyFeed(hass, target, "mp3", {})
    await feed.async_open()
    feed.write(b"one")

    with (
        patch.object(avatar_stream, "HOLD_AFTER", 0.01),
        patch.object(avatar_stream, "HOLD_EVERY", 0.01),
        patch.object(avatar_stream, "HOLD_LIMIT", 0.05),
    ):
        await run(hass, feed)

    assert live.socket.sent[0] == b"pcm:one"
    assert live.socket.sent[-1] == {"type": "end"}
    assert HOLD in live.socket.sent
    assert live.socket.closed


async def test_audio_that_keeps_coming_is_never_held(hass, live, target):
    feed = ReplyFeed(hass, target, "mp3", {})
    await feed.async_open()
    feed.write(b"one")
    feed.write(b"two")
    feed.finish()

    with patch.object(avatar_stream, "HOLD_AFTER", 0.01):
        await run(hass, feed)

    assert HOLD not in live.socket.sent


async def test_an_interrupt_during_a_pause_stops_the_holding(hass, live, target):
    feed = ReplyFeed(hass, target, "mp3", {})
    await feed.async_open()
    feed.write(b"one")

    with (
        patch.object(avatar_stream, "HOLD_AFTER", 0.01),
        patch.object(avatar_stream, "HOLD_EVERY", 0.01),
    ):
        running = asyncio.create_task(feed.async_run())
        await until_sent(live, HOLD)
        live.socket.says({"type": "cancelled"})
        await asyncio.wait_for(running, timeout=5)
        held = live.socket.sent.count(HOLD)
        await asyncio.sleep(0.05)

    assert live.socket.sent.count(HOLD) == held
    assert {"type": "end"} not in live.socket.sent
    assert live.socket.closed


async def test_a_session_that_changed_gets_no_more_audio(hass, live, target):
    feed = ReplyFeed(hass, target, "mp3", {})
    await feed.async_open()
    hass.states.async_set(SESSION_ENTITY, "sess-2")
    feed.write(b"one")
    feed.finish()

    await run(hass, feed)

    assert live.socket.sent == []


async def test_audio_that_cannot_be_decoded_is_not_opened_on_livetalking(hass, live, target):
    async def no_decoder(hass_, extension, options):
        return None

    with patch.object(avatar_stream, "async_start_decoder", no_decoder):
        opened = await ReplyFeed(hass, target, "raw", {}).async_open()

    assert opened is False
    assert live.opened == []


async def test_a_refused_open_stops_the_decoder_it_started(hass, live, target):
    live.refuses = aiohttp.ClientConnectionError()

    assert await ReplyFeed(hass, target, "mp3", {}).async_open() is False

    assert live.decoders[-1].stopped


# A reply that cannot go on must end, not wait (S4 of the 2026-11-27 review)


class StuckDecoder(FakeDecoder):
    """A decoder whose input stops being read after the first audio, as ffmpeg's does
    when nobody reads its output any more."""

    def __init__(self, when_stuck: Callable[[], None]) -> None:
        super().__init__()
        self._when_stuck = when_stuck

    async def async_write(self, audio: bytes) -> None:
        await super().async_write(audio)
        self._when_stuck()
        await asyncio.Event().wait()


def use_decoder(decoder: FakeDecoder):
    async def start(hass_, extension, options):
        return decoder

    return patch.object(avatar_stream, "async_start_decoder", start)


async def test_regression_s4_a_session_that_changes_while_the_decoder_is_stuck_ends_the_reply(
    hass, live, target
):
    decoder = StuckDecoder(lambda: hass.states.async_set(SESSION_ENTITY, "sess-2"))
    with use_decoder(decoder):
        feed = ReplyFeed(hass, target, "mp3", {})
        await feed.async_open()
        feed.write(b"the whole clip in one piece")
        feed.finish()

        await asyncio.wait_for(feed.async_run(), timeout=1)

    assert decoder.stopped
    assert live.socket.closed
    assert live.socket.sent == []


async def test_regression_s4_a_decoder_that_fails_ends_the_reply_and_is_logged(
    hass, live, target, caplog
):
    class BrokenDecoder(FakeDecoder):
        async def async_write(self, audio: bytes) -> None:
            raise BrokenPipeError("ffmpeg went away")

    decoder = BrokenDecoder()
    with use_decoder(decoder):
        feed = ReplyFeed(hass, target, "mp3", {})
        await feed.async_open()
        feed.write(b"one")

        await asyncio.wait_for(feed.async_run(), timeout=1)

    assert decoder.stopped
    assert live.socket.closed
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(feed.reply_id in r.getMessage() and r.exc_info for r in warnings)


async def test_regression_s4_a_livetalking_that_stops_reading_ends_the_reply(hass, live, target):
    feed = ReplyFeed(hass, target, "mp3", {})
    await feed.async_open()

    async def never(data: bytes) -> None:
        await asyncio.Event().wait()

    live.socket.send_bytes = never  # type: ignore[method-assign]
    feed.write(b"one")
    feed.finish()

    with patch.object(avatar_stream, "SEND_TIMEOUT", 0.01):
        await asyncio.wait_for(feed.async_run(), timeout=1)

    assert live.decoders[-1].stopped
    assert live.socket.closed
    assert {"type": "end"} not in live.socket.sent


async def test_regression_s4_a_reply_never_runs_longer_than_the_limit(hass, live, target):
    class EndlessDecoder(FakeDecoder):
        async def async_close_input(self) -> None:
            """Never says its output ended."""

    decoder = EndlessDecoder()
    with use_decoder(decoder), patch.object(avatar_stream, "RUN_LIMIT", 0.05):
        feed = ReplyFeed(hass, target, "mp3", {})
        await feed.async_open()
        feed.write(b"one")
        feed.finish()

        await asyncio.wait_for(feed.async_run(), timeout=1)

    assert decoder.stopped
    assert live.socket.closed


async def test_regression_s4_an_open_that_is_cancelled_closes_its_socket(hass, live, target):
    live.first = None
    feed = ReplyFeed(hass, target, "mp3", {})

    with patch.object(avatar_stream, "OPEN_TIMEOUT", 5):
        opening = asyncio.create_task(feed.async_open())
        while not live.sockets:
            await asyncio.sleep(0)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening

    assert live.socket.closed
    assert live.decoders[-1].stopped


async def test_the_stream_is_opened_with_a_heartbeat(hass, live, target):
    await ReplyFeed(hass, target, "mp3", {}).async_open()

    assert live.connect_options == {"heartbeat": avatar_stream.HEARTBEAT}


# Through the avatar's voice


@pytest.fixture
async def fed_avatar(hass, engines, sent, device, live):
    """An assistant with a streaming avatar voice, asked by the kitchen satellite."""
    add_fed_entities(hass)
    entry = await add_router(hass)
    avatar_voice = await add_avatar(hass, entry, "tts.local_voice")
    turn_started(hass, entry.entry_id, SATELLITE)
    with slots(entry, None):
        yield avatar_voice


async def test_a_streamed_reply_is_fed_to_the_session_and_spoken_unchanged(
    hass, fed_avatar, live
):
    _extension, audio = await speak(hass, fed_avatar, streamed=True, fmt="wav")
    await hass.async_block_till_done(wait_background_tasks=True)

    assert audio == b"".join(WAV_CHUNKS)
    assert live.opened[0][1]["sessionid"] == "sess-1"
    assert live.socket.sent == [*(b"pcm:" + chunk for chunk in WAV_CHUNKS), {"type": "end"}]


async def test_the_stream_is_open_before_the_first_audio_is_passed_on(hass, fed_avatar, live):
    live.gate = asyncio.Event()
    hass.loop.call_later(0.02, live.gate.set)
    stream = hass.data[DATA_TTS_MANAGER].async_create_result_stream(
        fed_avatar, use_file_cache=False, options={"preferred_format": "wav"}
    )

    async def words():
        yield "Hello there"

    stream.async_set_message_stream(words())
    with patch.object(avatar_stream, "OPEN_TIMEOUT", 1):
        async for _chunk in stream.async_stream_result():
            live.events.append("audio")
            break

    assert live.events[:2] == ["opened", "audio"]


async def test_a_reply_livetalking_refuses_is_still_spoken_unchanged(hass, fed_avatar, live):
    live.refuses = aiohttp.ClientConnectionError()

    _extension, audio = await speak(hass, fed_avatar, streamed=True, fmt="wav")

    assert audio == b"".join(WAV_CHUNKS)
    assert live.sockets == []


async def test_a_reply_for_a_satellite_without_the_switch_is_not_fed(hass, fed_avatar, live):
    hass.states.async_set(FED_SWITCH, "off")

    _extension, audio = await speak(hass, fed_avatar, streamed=True, fmt="wav")

    assert audio == b"".join(WAV_CHUNKS)
    assert live.opened == []


async def test_an_engine_that_fails_mid_reply_aborts_the_feed(hass, fed_avatar, live, engines):
    engines["local"].fail_after_first_chunk = True

    with pytest.raises(HomeAssistantError):
        await speak(hass, fed_avatar, streamed=True, fmt="wav")
    await hass.async_block_till_done(wait_background_tasks=True)

    assert {"type": "end"} not in live.socket.sent
    assert live.socket.closed


async def test_a_whole_clip_from_an_engine_that_does_not_stream_is_fed_in_one_piece(
    hass, engines, sent, device, live
):
    add_fed_entities(hass)
    entry = await add_router(hass)
    avatar_voice = await add_avatar(hass, entry, "tts.cloud_voice")
    turn_started(hass, entry.entry_id, SATELLITE)

    with slots(entry, None):
        _extension, audio = await speak(hass, avatar_voice)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert live.socket.sent == [b"pcm:" + audio, {"type": "end"}]


# The sessions of displays that are there now


async def test_regression_a_session_last_reported_by_an_away_display_is_not_present(
    hass, device
):
    add_fed_entities(hass, 1, session="sess-1")
    assert present_device_sessions(hass) == device_sessions(hass) == frozenset({"sess-1"})

    hass.states.async_set("sensor.kitchen_assistant_1_avatar_session", "unavailable")

    assert device_sessions(hass) == frozenset({"sess-1"})  # A copy still skips it.
    assert present_device_sessions(hass) == frozenset()
