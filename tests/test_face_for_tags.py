# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The face of a reply that opens with an audio tag and no face tag."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry

from custom_components.voice_assistant_manager import avatar, emotion
from custom_components.voice_assistant_manager.chat_show import ChatLimits
from custom_components.voice_assistant_manager.emotion import (
    StreamedReply,
    face_table,
    split_face,
    unknown_faces,
)
from custom_components.voice_assistant_manager.status import router_status

from .router_helpers import FakeAgents, add_router, ask, options_with
from .test_chat_show import (  # noqa: F401 - fixtures
    ADA_WRITER,
    chat_audio,
    run_chat,
    show,
)
from .test_emotion_router import Pipeline, ask_streamed, speech, streaming_main_agent
from .test_panel import Page, page  # noqa: F401 - fixture

SIX = "neutral, happy, sad, angry, sarcastic, flirty"
FIVE = emotion.DEFAULT_EMOTIONS
VOICE_TAGS = {"voice_tags": True}


def entry_with(**advanced):
    return SimpleNamespace(title="Ada", options={"advanced": advanced})


def default_table(tags: str = "") -> dict[str, str]:
    return face_table(entry_with(emotion_tags=tags))


@pytest.fixture
def told():
    """What LiveTalking was told: (entry title, face, how it was chosen)."""
    calls: list[tuple[str, str, str]] = []
    with patch.object(
        emotion,
        "send_emotion",
        lambda _hass, entry, found, why="": calls.append((entry.title, found, why)),
    ):
        yield calls


# The table


def test_the_default_table_keeps_only_faces_on_the_assistants_list():
    table = default_table()

    assert table["mischievous"] == "sarcastic"
    assert table["drawn out"] == "sarcastic"
    assert table["excited"] == "happy"
    assert table["let down"] == "sad"
    assert table["shouts"] == "angry"
    # "laughing" and "flirty" are not among the five default emotions.
    assert "laughs" not in table
    assert "whispers" not in table


def test_a_face_on_the_assistants_list_brings_its_line_of_the_default_table():
    assert default_table(SIX)["whispers"] == "flirty"
    assert "laughs" not in default_table(SIX)


def test_a_custom_table_replaces_the_default():
    table = face_table(entry_with(face_for_tags="happy: Mischievous,  drawn   OUT \nsad: thoughtful"))

    assert table == {"mischievous": "happy", "drawn out": "happy", "thoughtful": "sad"}


def test_the_first_line_that_has_a_tag_wins():
    table = face_table(entry_with(face_for_tags="sad: smug\nhappy: smug, proud"))

    assert table == {"smug": "sad", "proud": "happy"}


def test_a_line_with_a_face_that_is_not_on_the_list_is_ignored_and_reported():
    entry = entry_with(face_for_tags="laughing: laughs\nhappy: proud\nbored: tired")

    assert face_table(entry) == {"proud": "happy"}
    assert unknown_faces(entry) == ["bored", "laughing"]


def test_the_default_table_reports_no_unknown_faces():
    assert unknown_faces(entry_with()) == []


@pytest.mark.parametrize("raw", ["no colon here", ": smug", "happy:", "\n\n"])
def test_a_line_that_is_not_a_mapping_is_ignored(raw):
    assert face_table(entry_with(face_for_tags=raw + "\nsad: sighs")) == {"sighs": "sad"}


# Choosing the face


def face(text: str, table=None, accepted=FIVE, keep=True):
    return split_face(text, accepted, default_table() if table is None else table, keep)


def test_a_face_tag_first_sets_the_face_and_is_not_passed_to_the_voice():
    assert face("[happy] [playful] Hi") == ("happy", "[playful] Hi", "face tag")


def test_an_opening_audio_tag_gives_its_mapped_face_and_the_text_is_unchanged():
    text = "[mischievous] Well, my dear friend, a certain degree of [smug] satisfaction."

    assert face(text) == ("sarcastic", text, "from audio tag [mischievous]")


def test_an_audio_tag_mapped_to_a_face_that_is_not_on_the_list_is_neutral():
    assert face("[laughs] Oh no") == ("neutral", "[laughs] Oh no", "no match for [laughs]")


def test_an_audio_tag_that_is_in_no_line_is_neutral():
    assert face("[thoughtful] Hmm") == ("neutral", "[thoughtful] Hmm", "no match for [thoughtful]")


def test_plain_text_is_neutral():
    assert face("Hello") == ("neutral", "Hello", "no tag")


def test_a_tag_with_a_digit_is_skipped_and_the_next_tag_is_used():
    text = "[pause 2] [smug] Told you."

    assert face(text) == ("sarcastic", text, "from audio tag [smug]")


def test_only_the_opening_of_the_reply_counts():
    text = "Well. [smug] Told you."

    assert face(text) == ("neutral", text, "no tag")


def test_a_tag_is_matched_whole_in_any_case_and_with_spaces_trimmed():
    assert face("[Drawn  OUT] Suuure.")[0] == "sarcastic"
    assert face("[smug, tired] Fine.")[0] == "neutral"


def test_an_opening_tag_that_is_itself_an_emotion_on_the_list_gives_that_face():
    # Too far in to be read as the face tag, so it stays in the text for the voice.
    text = " " * 25 + "[sad] Oh."

    assert face(text) == ("sad", text, "from audio tag [sad]")


def test_with_tags_off_the_face_is_still_chosen_from_the_opening_tag():
    found, _rest, why = face("[mischievous] Well.", keep=False)

    assert (found, why) == ("sarcastic", "from audio tag [mischievous]")


# The stream


class Listener:
    def __init__(self) -> None:
        self.deltas: list[dict] = []

    def __call__(self, chat_log, delta: dict) -> None:
        self.deltas.append(delta)

    @property
    def spoken(self) -> str:
        return "".join(d.get("content") or "" for d in self.deltas)


@pytest.mark.parametrize("size", [1, 3, 9, 1000])
def test_a_streamed_reply_without_a_face_tag_reaches_the_voice_byte_for_byte(size):
    text = "[mischievous] Well, my dear friend, I must confess to a certain [smug] satisfaction…"
    listener, found = Listener(), []
    stream = StreamedReply(listener, found.append, FIVE, True, default_table())

    stream(None, {"role": "assistant"})
    for start in range(0, len(text), size):
        stream(None, {"content": text[start : start + size]})
    stream.finish()

    assert listener.spoken == text
    assert found == ["sarcastic"]
    assert stream.why == "from audio tag [mischievous]"


def test_the_face_is_known_before_any_text_is_passed_on():
    order: list[str] = []

    def listener(_log, delta: dict) -> None:
        if delta.get("content"):
            order.append("text")

    stream = StreamedReply(
        listener, lambda found: order.append(f"face {found}"), FIVE, True, default_table()
    )

    stream(None, {"role": "assistant", "content": "[mischie"})
    stream(None, {"content": "vous] Well"})
    stream.finish()

    assert order == ["face sarcastic", "text"]


# Through the router


async def test_a_reply_that_opens_with_an_audio_tag_tells_the_mapped_face(hass, told):
    entry = await add_router(hass, options_with(advanced=VOICE_TAGS))
    agents = FakeAgents(main_reply="[mischievous] Well, well.")

    with agents.patched():
        result = await ask(hass, entry, "Hi")

    assert told == [("Kitchen assistant", "sarcastic", "from audio tag [mischievous]")]
    assert speech(result) == "Well, well."
    assert emotion.take_spoken(hass, entry.entry_id, "Well, well.") == "[mischievous] Well, well."


async def test_a_streamed_reply_tells_the_mapped_face_and_feeds_the_voice_unchanged(hass, told):
    entry = await add_router(hass, options_with(advanced=VOICE_TAGS))
    pipeline = Pipeline(told)
    reply = ["[mischie", "vous] Well, my dear friend, I must confess ", "to a certain [smug] satisfaction."]

    with patch.object(conversation, "async_converse", streaming_main_agent(reply)):
        await ask_streamed(hass, entry, "Hi", pipeline)

    assert pipeline.spoken == "".join(reply)
    assert told == [("Kitchen assistant", "sarcastic", "from audio tag [mischievous]")]


async def test_a_reply_with_a_face_tag_is_as_before(hass, told):
    entry = await add_router(hass, options_with(advanced=VOICE_TAGS))
    agents = FakeAgents(main_reply="[happy] [playful] Hi")

    with agents.patched():
        await ask(hass, entry, "Hi")

    assert told == [("Kitchen assistant", "happy", "face tag")]
    assert emotion.take_spoken(hass, entry.entry_id, "Hi") == "[playful] Hi"


async def test_with_tags_off_the_face_is_chosen_and_the_tags_are_stripped(hass, told):
    entry = await add_router(hass)
    agents = FakeAgents(main_reply="[mischievous] Well, [smug] well.")

    with agents.patched():
        result = await ask(hass, entry, "Hi")

    assert told == [("Kitchen assistant", "sarcastic", "from audio tag [mischievous]")]
    assert speech(result) == "Well, well."
    assert emotion.take_spoken(hass, entry.entry_id, "Well, well.") == "Well, well."


async def test_a_custom_table_is_used_by_the_router(hass, told):
    entry = await add_router(
        hass, options_with(advanced={"face_for_tags": "sad: mischievous"})
    )
    agents = FakeAgents(main_reply="[mischievous] Well.")

    with agents.patched():
        await ask(hass, entry, "Hi")

    assert told[0][1:] == ("sad", "from audio tag [mischievous]")


async def test_a_chat_line_that_opens_with_an_audio_tag_gets_the_mapped_face(hass, show, told):
    show.agents.writer_reply = "[smug] Quite so, madam."
    show.agents.replies[ADA_WRITER] = "[thoughtful] Hmm."

    await run_chat(hass, show, limits=ChatLimits(max_lines=2, pause=0))

    assert told == [
        ("Jeeves", "sarcastic", "from audio tag [smug]"),
        ("Ada", "neutral", "no match for [thoughtful]"),
    ]
    assert show.speakers.messages == ["Quite so, madam.", "Hmm."]


# The log and the page


async def test_the_log_says_how_the_face_was_chosen(hass, aioclient_mock, caplog):
    entry = await add_router(hass)
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data={avatar.CONF_ENGINE: "tts.x", avatar.CONF_URL: "http://livetalking.local:8010",
                  avatar.CONF_ENABLED: True, avatar.CONF_AVATAR_ID: "ada_2"},
            subentry_type=avatar.SUBENTRY_TYPE, title="Ada avatar", unique_id=None,
        ),
    )
    await hass.async_block_till_done()
    aioclient_mock.post("http://livetalking.local:8010/set_emotion", json={"code": 0})

    with caplog.at_level(logging.DEBUG, logger=emotion.__name__):
        emotion.send_emotion(hass, entry, "sarcastic", "from audio tag [mischievous]")
        await hass.async_block_till_done()

    assert "Emotion: ada_2 told to look sarcastic (from audio tag [mischievous])" in caplog.text


async def test_the_page_warns_about_a_face_that_is_not_on_the_list(hass):
    entry = await add_router(
        hass, options_with(advanced={"face_for_tags": "laughing: laughs\nhappy: proud"})
    )

    lines = router_status(hass, entry, entry.options)

    warning = [line for line in lines if "Face for audio tags" in line["text"]]
    assert [line["level"] for line in warning] == ["warn"]
    assert "laughing" in warning[0]["text"]


async def test_the_page_has_no_warning_for_the_default_table(hass):
    entry = await add_router(hass)

    lines = router_status(hass, entry, entry.options)

    assert not [line for line in lines if "Face for audio tags" in line["text"]]


async def test_the_page_saves_the_table(hass, page):
    entry = await add_router(hass)
    settings = (await page.router(entry))["settings"]
    settings["advanced"] = {**settings["advanced"], "face_for_tags": "sad: thoughtful"}

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert face_table(entry) == {"thoughtful": "sad"}
