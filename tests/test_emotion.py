# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Emotion tags: reading and stripping the tag, and telling LiveTalking the emotion."""

import logging
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigSubentry
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import avatar, emotion
from custom_components.voice_assistant_manager.const import DOMAIN

BASE = "http://livetalking.local:8010"
SET_EMOTION = f"{BASE}/set_emotion"

# (reply, emotion, what is spoken)
SAMPLES = [
    ("[happy] Hello there.", "happy", "Hello there."),
    ("[HAPPY] Hello.", "happy", "Hello."),
    ("  \n[sad]   Oh no.", "sad", "Oh no."),
    ("[angry]No!", "angry", "No!"),
    ("[sarcastic] Great.", "sarcastic", "Great."),
    ("[neutral] Fine.", "neutral", "Fine."),
    ("[excited] Wow.", "neutral", "Wow."),
    ("[happy]", "happy", ""),
    ("Hello there.", "neutral", "Hello there."),
    ("", "neutral", ""),
    ("   ", "neutral", "   "),
    ("Hello [happy] there.", "neutral", "Hello [happy] there."),
    ("[very happy] Hi.", "neutral", "[very happy] Hi."),
    ("[happy Hi.", "neutral", "[happy Hi."),
    ("[] Hi.", "neutral", "[] Hi."),
    # The closing bracket must come within the first 20 characters.
    ("[abcdefghijklmnopqrstuvwxyz] Hi.", "neutral", "[abcdefghijklmnopqrstuvwxyz] Hi."),
    ("[abcdefghijklmnopqr] Hi.", "neutral", "Hi."),
    ("  [abcdefghijklmnop] Hi.", "neutral", "Hi."),
    ("[abcdefghijklmnopqrs] Hi.", "neutral", "[abcdefghijklmnopqrs] Hi."),
    (" " * 25 + "[happy] Hi.", "neutral", " " * 25 + "[happy] Hi."),
    ("[happy] [sad] Hi.", "happy", "[sad] Hi."),
    # Only a word of letters is a tag: a number or code at the start stays in the reply.
    ("[1] First step.", "neutral", "[1] First step."),
    ("[2b] Second.", "neutral", "[2b] Second."),
    ("[x_y] Hi.", "neutral", "[x_y] Hi."),
    ("[Fröhlich] Hallo.", "neutral", "Hallo."),
]


@pytest.mark.parametrize(("reply", "expected_emotion", "spoken"), SAMPLES)
def test_split_tag_reads_and_strips_the_leading_tag(reply, expected_emotion, spoken):
    assert emotion.split_tag(reply) == (expected_emotion, spoken)


def _streamed(chunks: list[str]) -> tuple[str | None, str]:
    tag_filter = emotion.TagFilter()
    passed = "".join(tag_filter.feed(chunk) for chunk in chunks) + tag_filter.flush()
    return tag_filter.emotion, passed


@pytest.mark.parametrize(("reply", "expected_emotion", "spoken"), SAMPLES)
def test_streaming_one_character_at_a_time_gives_the_same_as_the_whole_reply(
    reply, expected_emotion, spoken
):
    found, passed = _streamed(list(reply))

    assert passed == spoken
    assert found == (expected_emotion if reply else None)


@pytest.mark.parametrize(("reply", "expected_emotion", "spoken"), SAMPLES)
def test_streaming_in_two_chunks_at_every_split_gives_the_same_as_the_whole_reply(
    reply, expected_emotion, spoken
):
    for cut in range(len(reply) + 1):
        found, passed = _streamed([reply[:cut], reply[cut:]])

        assert (passed, found) == (spoken, expected_emotion if reply else None), cut


def test_streaming_decides_at_once_when_the_reply_does_not_start_with_a_bracket():
    tag_filter = emotion.TagFilter()

    assert tag_filter.feed("He") == "He"
    assert tag_filter.emotion == "neutral"


def test_streaming_holds_text_back_until_the_closing_bracket():
    tag_filter = emotion.TagFilter()

    assert tag_filter.feed(" [hap") == ""
    assert tag_filter.emotion is None
    assert tag_filter.feed("py] He") == "He"
    assert tag_filter.emotion == "happy"


def test_streaming_gives_up_on_a_tag_after_20_characters():
    tag_filter = emotion.TagFilter()

    assert tag_filter.feed("[" + "a" * 18) == ""
    assert tag_filter.feed("a") == "[" + "a" * 19
    assert tag_filter.emotion == "neutral"


def test_streaming_passes_everything_after_the_decision_through_unchanged():
    tag_filter = emotion.TagFilter()
    tag_filter.feed("[sad] ")

    assert tag_filter.feed("  [angry] ") == "[angry] "


def test_nothing_streamed_decides_nothing():
    tag_filter = emotion.TagFilter()

    assert tag_filter.flush() == ""
    assert tag_filter.emotion is None


# Delta listener


class Listener:
    def __init__(self) -> None:
        self.deltas: list[dict] = []

    def __call__(self, chat_log, delta: dict) -> None:
        self.deltas.append(delta)

    @property
    def spoken(self) -> str:
        return "".join(d.get("content") or "" for d in self.deltas)


def test_the_tag_never_reaches_the_listener_and_the_emotion_is_reported_once():
    listener, found = Listener(), []
    stream = emotion.StreamedReply(listener, found.append)

    for delta in (
        {"role": "assistant"}, {"content": "[hap"}, {"content": "py] Hello"},
        {"content": " there."},
    ):
        stream(None, delta)
    stream.finish()

    assert listener.spoken == "Hello there."
    assert found == ["happy"]
    assert stream.emotion == "happy"


def test_held_text_is_passed_on_before_the_next_message_starts():
    listener = Listener()
    stream = emotion.StreamedReply(listener, lambda _e: None)

    stream(None, {"role": "assistant", "content": "[hap"})
    stream(None, {"role": "tool_result", "tool_call_id": "1", "tool_name": "x", "result": {}})

    assert listener.deltas[0] == {"role": "assistant", "content": ""}
    assert listener.deltas[1] == {"content": "[hap"}
    assert listener.deltas[2]["role"] == "tool_result"


def test_held_text_is_passed_on_when_the_reply_ends():
    listener = Listener()
    stream = emotion.StreamedReply(listener, lambda _e: None)
    stream(None, {"role": "assistant"})
    stream(None, {"content": "[ok"})

    stream.finish()

    assert listener.spoken == "[ok"
    assert stream.emotion == "neutral"


def test_each_message_of_the_reply_has_its_tag_stripped_but_the_first_emotion_counts():
    listener, found = Listener(), []
    stream = emotion.StreamedReply(listener, found.append)

    for delta in (
        {"role": "assistant", "content": "[sad] Let me look."},
        {"tool_calls": ["call"]},
        {"role": "tool_result", "tool_call_id": "1", "tool_name": "x", "result": {}},
        {"role": "assistant", "content": "[happy] Found it."},
    ):
        stream(None, delta)
    stream.finish()

    assert "[" not in listener.spoken
    assert found == ["sad"]


def test_a_message_with_only_tool_calls_does_not_decide_the_emotion():
    listener, found = Listener(), []
    stream = emotion.StreamedReply(listener, found.append)

    for delta in (
        {"role": "assistant", "tool_calls": ["call"]},
        {"role": "tool_result", "tool_call_id": "1", "tool_name": "x", "result": {}},
        {"role": "assistant", "content": "[angry] No."},
    ):
        stream(None, delta)
    stream.finish()

    assert found == ["angry"]


def test_a_delta_left_with_nothing_to_pass_on_is_not_passed_on():
    listener = Listener()
    stream = emotion.StreamedReply(listener, lambda _e: None)

    stream(None, {"role": "assistant"})
    stream(None, {"content": "[happy]"})

    assert listener.deltas == [{"role": "assistant"}]


def test_the_listener_gets_the_chat_log_it_was_called_with():
    seen = []
    stream = emotion.StreamedReply(lambda log, _d: seen.append(log), lambda _e: None)
    log = object()

    stream(log, {"role": "assistant", "content": "[ha"})
    stream.finish()

    assert seen == [log, log]


# Sending


def add_avatar(
    entry: MockConfigEntry, hass, *, url: str = BASE, avatar_id: str = "ada", enabled: bool = True
) -> None:
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data={
                avatar.CONF_ENGINE: "tts.engine",
                avatar.CONF_URL: url,
                avatar.CONF_ENABLED: enabled,
                avatar.CONF_AVATAR_ID: avatar_id,
            },
            subentry_type=avatar.SUBENTRY_TYPE,
            title="Ada avatar",
            unique_id=None,
        ),
    )


@pytest.fixture
def entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Ada", data={"agent": "conversation.x"})
    entry.add_to_hass(hass)
    return entry


async def test_the_emotion_is_posted_for_each_enabled_avatar(hass, entry, aioclient_mock):
    add_avatar(entry, hass)
    add_avatar(entry, hass, url="http://other.local:8010/", avatar_id="bob")
    add_avatar(entry, hass, url="http://off.local:8010", avatar_id="off", enabled=False)
    aioclient_mock.post(SET_EMOTION, json={"code": 0})
    aioclient_mock.post("http://other.local:8010/set_emotion", json={"code": 0})

    emotion.send_emotion(hass, entry, "happy")
    await hass.async_block_till_done()

    sent = {(str(url), str(data)) for _m, url, data, _h in aioclient_mock.mock_calls}
    assert sent == {
        (SET_EMOTION, str({"avatar_id": "ada", "emotion": "happy"})),
        ("http://other.local:8010/set_emotion", str({"avatar_id": "bob", "emotion": "happy"})),
    }


@pytest.mark.parametrize(
    "changes", [{"avatar_id": ""}, {"url": "not an address"}, {"url": ""}]
)
async def test_an_avatar_without_an_id_or_address_gets_no_call(
    hass, entry, aioclient_mock, changes
):
    add_avatar(entry, hass, **changes)

    emotion.send_emotion(hass, entry, "happy")
    await hass.async_block_till_done()

    assert aioclient_mock.call_count == 0


async def test_an_assistant_without_an_avatar_gets_no_call(hass, entry, aioclient_mock):
    emotion.send_emotion(hass, entry, "sad")
    await hass.async_block_till_done()

    assert aioclient_mock.call_count == 0


async def test_the_call_gives_up_after_300_ms(hass, entry):
    add_avatar(entry, hass)
    timeouts = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def raise_for_status(self):
            return None

    class Session:
        def post(self, url, *, json, timeout):
            timeouts.append(timeout)
            return Response()

    with patch.object(emotion, "async_get_clientsession", return_value=Session()):
        emotion.send_emotion(hass, entry, "sad")
        await hass.async_block_till_done()

    assert [t.total for t in timeouts] == [0.3]


async def test_a_server_without_set_emotion_is_harmless_and_not_warned_about(
    hass, entry, aioclient_mock, caplog
):
    add_avatar(entry, hass)
    aioclient_mock.post(SET_EMOTION, status=404)

    with caplog.at_level(logging.DEBUG):
        emotion.send_emotion(hass, entry, "happy")
        await hass.async_block_till_done()

    assert aioclient_mock.call_count == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    "failure", [TimeoutError(), aiohttp.ClientConnectionError(), RuntimeError("bug")]
)
async def test_a_failure_is_warned_about_once_per_outage(hass, entry, aioclient_mock, caplog, failure):
    add_avatar(entry, hass)
    aioclient_mock.post(SET_EMOTION, exc=failure)

    for _ in range(3):
        emotion.send_emotion(hass, entry, "happy")
        await hass.async_block_till_done()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "set_emotion" in warnings[0].getMessage()


async def test_a_success_ends_the_outage(hass, entry, aioclient_mock, caplog):
    add_avatar(entry, hass)
    aioclient_mock.post(SET_EMOTION, exc=TimeoutError())
    emotion.send_emotion(hass, entry, "happy")
    await hass.async_block_till_done()
    aioclient_mock.clear_requests()
    aioclient_mock.post(SET_EMOTION, json={"code": 0})
    emotion.send_emotion(hass, entry, "happy")
    await hass.async_block_till_done()
    aioclient_mock.clear_requests()
    aioclient_mock.post(SET_EMOTION, exc=TimeoutError())

    emotion.send_emotion(hass, entry, "happy")
    await hass.async_block_till_done()

    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 2


async def test_sending_never_raises_even_when_the_entry_cannot_start_a_task(hass, entry):
    add_avatar(entry, hass)

    with patch.object(entry, "async_create_background_task", side_effect=RuntimeError("x")):
        emotion.send_emotion(hass, entry, "happy")
