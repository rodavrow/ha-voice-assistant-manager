# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Audio tags: directions for the voice, such as "[laughs]", never shown and only spoken
to a voice that takes them."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components import conversation

from custom_components.voice_assistant_manager import emotion
from custom_components.voice_assistant_manager.chat_show import ChatLimits
from custom_components.voice_assistant_manager.emotion import (
    AudioTagFilter,
    StreamedReply,
    remember_spoken,
    split_tag,
    strip_audio_tags,
    take_spoken,
)

from .router_helpers import FakeAgents, add_router, ask, options_with
from .test_avatar_tts import add_avatar, engines, speak  # noqa: F401 - fixtures
from .test_avatar_tts import sent as tts_sent  # noqa: F401 - fixture
from .test_chat_show import (  # noqa: F401 - fixtures
    ADA_WRITER,
    JEEVES_WRITER,
    chat_audio,
    fake_tts,
    router_options,
    run_chat,
    show,
)
from .test_compose_announcement import compose, setup
from .test_emotion_router import (  # noqa: F401 - fixtures
    Pipeline,
    ask_streamed,
    sent,
    speech,
    streaming_main_agent,
)
from .test_panel import Page, page  # noqa: F401 - fixture

VOICE_TAGS = {"voice_tags": True}

# (text, as it is shown)
SAMPLES = [
    ("[laughs] Oh, that is good.", "Oh, that is good."),
    ("Hello [laughs softly] there.", "Hello there."),
    ("So [sighs]. Fine.", "So. Fine."),
    ("[whispering, fearful] Don't move.", "Don't move."),
    ("[a tired detective who has heard it all before] Sit down.", "Sit down."),
    ("[hopeful] [nervous] Fine. Saturn.", "Fine. Saturn."),
    ("We won! [long pause] [triumphant] YES!", "We won! YES!"),
    ("End of line [sighs]", "End of line"),
    ("One [pause]\nTwo", "One\nTwo"),
    ("[British accent] Fancy a cup of tea?", "Fancy a cup of tea?"),
    # Not audio tags: a digit, an empty pair, an open bracket, one that is too long.
    ("[1] First step.", "[1] First step."),
    ("Take [2b] and [x1].", "Take [2b] and [x1]."),
    ("Nothing [] here.", "Nothing [] here."),
    ("Open [ never closed", "Open [ never closed"),
    ("Hi [" + "a" * 90 + "] there", "Hi [" + "a" * 90 + "] there"),
    ("No tags here.", "No tags here."),
    ("A  double  space stays.", "A  double  space stays."),
    ("", ""),
]


@pytest.mark.parametrize(("text", "shown"), SAMPLES)
def test_audio_tags_are_taken_out_and_no_gap_is_left(text, shown):
    assert strip_audio_tags(text) == shown


@pytest.mark.parametrize(("text", "shown"), SAMPLES)
@pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
def test_streaming_in_pieces_gives_the_same_as_the_whole_text(text, shown, size):
    tag_filter = AudioTagFilter()
    pieces = [text[start : start + size] for start in range(0, len(text), size)]

    passed = "".join(tag_filter.feed(piece) for piece in pieces) + tag_filter.flush()

    assert passed.rstrip(" ") == shown.rstrip(" ")


def test_streaming_holds_text_back_only_while_it_may_be_a_tag():
    tag_filter = AudioTagFilter()

    assert tag_filter.feed("Hello") == "Hello"
    assert tag_filter.feed(" [lau") == ""
    assert tag_filter.feed("ghs] there") == " there"


def test_streaming_passes_text_without_brackets_through_but_for_spaces_at_its_end():
    tag_filter = AudioTagFilter()

    assert tag_filter.feed("Hello ") == "Hello"
    assert tag_filter.feed("there") == " there"


# The leading tag: the avatar's face, or the voice's


def test_a_leading_tag_that_is_not_an_emotion_is_kept_for_a_voice_that_takes_tags():
    assert split_tag("[whispers] Come closer.", keep_unknown=True) == (
        "neutral", "[whispers] Come closer.",
    )
    assert split_tag("[whispers] Come closer.") == ("neutral", "Come closer.")


def test_a_leading_emotion_is_taken_off_also_for_a_voice_that_takes_tags():
    assert split_tag("[happy] [laughs] Good.", keep_unknown=True) == ("happy", "[laughs] Good.")


# The stream to the voice


class Listener:
    def __init__(self) -> None:
        self.deltas: list[dict] = []

    def __call__(self, chat_log, delta: dict) -> None:
        self.deltas.append(delta)

    @property
    def spoken(self) -> str:
        return "".join(d.get("content") or "" for d in self.deltas)


def streamed(pieces: list[str], *, voice_tags: bool) -> tuple[str, list[str]]:
    listener, found = Listener(), []
    stream = StreamedReply(listener, found.append, voice_tags=voice_tags)
    stream(None, {"role": "assistant"})
    for piece in pieces:
        stream(None, {"content": piece})
    stream.finish()
    return listener.spoken, found


def test_a_voice_that_takes_tags_gets_them_in_the_stream_without_the_face_tag():
    spoken, found = streamed(["[happy] [lau", "ghs] Oh, that", " is [sighs] good."], voice_tags=True)

    assert spoken == "[laughs] Oh, that is [sighs] good."
    assert found == ["happy"]


def test_a_voice_that_does_not_take_tags_never_gets_one_in_the_stream():
    spoken, found = streamed(["[happy] [lau", "ghs] Oh, that", " is [sighs] good."], voice_tags=False)

    assert spoken == "Oh, that is good."
    assert found == ["happy"]


def test_a_reply_that_starts_with_an_audio_tag_keeps_it_for_the_voice_and_is_neutral():
    spoken, found = streamed(["[whispers] Come", " closer."], voice_tags=True)

    assert spoken == "[whispers] Come closer."
    assert found == ["neutral"]


# Through the router


async def test_a_streamed_reply_gives_the_voice_its_tags_and_shows_none(hass, sent):
    entry = await add_router(hass, options_with(advanced=VOICE_TAGS))
    pipeline = Pipeline(sent)
    reply = ["[happy] [laughs] Oh, that is ", "a good one. [sighs] I needed that today, ", "thanks."]

    with patch.object(conversation, "async_converse", streaming_main_agent(reply)):
        result, _listener, _content = await ask_streamed(hass, entry, "Joke?", pipeline)

    assert pipeline.spoken == "[laughs] Oh, that is a good one. [sighs] I needed that today, thanks."
    assert speech(result) == "Oh, that is a good one. I needed that today, thanks."
    assert sent == [("Kitchen assistant", "happy")]


async def test_without_the_setting_a_streamed_reply_has_no_tags_anywhere(hass, sent):
    entry = await add_router(hass)
    pipeline = Pipeline(sent)
    reply = ["[happy] [laughs] Oh, that is ", "a good one. [sighs] Thanks."]

    with patch.object(conversation, "async_converse", streaming_main_agent(reply)):
        result, _listener, _content = await ask_streamed(hass, entry, "Joke?", pipeline)

    assert pipeline.spoken == "Oh, that is a good one. Thanks."
    assert speech(result) == "Oh, that is a good one. Thanks."


@pytest.mark.parametrize("advanced", [VOICE_TAGS, {}])
async def test_a_reply_is_never_shown_with_an_audio_tag(hass, sent, advanced):
    entry = await add_router(hass, options_with(advanced=advanced))
    agents = FakeAgents(main_reply="[sad] [sighs] I know. [softly] It will pass.")

    with agents.patched():
        result = await ask(hass, entry, "Bad day")

    assert speech(result) == "I know. It will pass."
    assert sent == [("Kitchen assistant", "sad")]


async def test_a_bracket_with_a_digit_is_still_shown(hass, sent):
    entry = await add_router(hass, options_with(advanced=VOICE_TAGS))
    agents = FakeAgents(main_reply="[1] Open the lid. [2] Pour.")

    with agents.patched():
        result = await ask(hass, entry, "How?")

    assert speech(result) == "[1] Open the lid. [2] Pour."


# A reply that is not streamed: the avatar voice asks for its tags back


def test_the_tagged_text_is_given_back_once_for_the_text_that_is_shown(hass):
    remember_spoken(hass, "entry", "Oh, good.", "[laughs] Oh, good.")

    assert take_spoken(hass, "another", "Oh, good.") == "Oh, good."
    assert take_spoken(hass, "entry", "Something else.") == "Something else."
    assert take_spoken(hass, "entry", "Oh, good.") == "[laughs] Oh, good."
    assert take_spoken(hass, "entry", "Oh, good.") == "Oh, good."


def test_a_tagged_text_that_is_too_old_is_not_given_back(hass):
    clock = [100.0]
    with patch.object(emotion.time, "monotonic", lambda: clock[0]):
        remember_spoken(hass, "entry", "Oh, good.", "[laughs] Oh, good.")
        clock[0] += 31

        assert take_spoken(hass, "entry", "Oh, good.") == "Oh, good."


def test_a_reply_without_tags_is_not_kept(hass):
    remember_spoken(hass, "entry", "Plain.", "Plain.")

    assert take_spoken(hass, "entry", "Plain.") == "Plain."


@pytest.mark.parametrize(("engine", "streamed_text"), [("cloud", False), ("local", True)])
async def test_a_short_reply_is_spoken_with_its_tags_by_the_avatar_voice(
    hass, engines, tts_sent, sent, engine, streamed_text
):
    entry = await add_router(hass, options_with(advanced=VOICE_TAGS))
    voice = await add_avatar(hass, entry, f"tts.{engine}_voice")
    agents = FakeAgents(main_reply="[happy] [laughs] Good one.")
    with agents.patched():
        result = await ask(hass, entry, "Joke?")

    await speak(hass, voice, speech(result), fmt="wav" if engine == "local" else None)

    assert speech(result) == "Good one."
    assert engines[engine].calls[0][0].strip() == "[laughs] Good one."


async def test_a_voice_without_the_setting_speaks_a_short_reply_without_tags(
    hass, engines, tts_sent, sent
):
    entry = await add_router(hass)
    voice = await add_avatar(hass, entry, "tts.cloud_voice")
    agents = FakeAgents(main_reply="[happy] [laughs] Good one.")
    with agents.patched():
        result = await ask(hass, entry, "Joke?")

    await speak(hass, voice, speech(result))

    assert engines["cloud"].calls[0][0] == "Good one."


async def test_other_text_through_the_avatar_voice_is_spoken_as_it_is(
    hass, engines, tts_sent, sent
):
    entry = await add_router(hass, options_with(advanced=VOICE_TAGS))
    voice = await add_avatar(hass, entry, "tts.cloud_voice")
    agents = FakeAgents(main_reply="[laughs] Good one.")
    with agents.patched():
        await ask(hass, entry, "Joke?")

    await speak(hass, voice, "The washing machine is done.")

    assert engines["cloud"].calls[0][0] == "The washing machine is done."


# Chat show lines


def spoken_text(media_id: str) -> str:
    return media_id.split("message=", 1)[1].split("&options=", 1)[0]


async def chat_with_tags(hass, show, *, voice_tags: bool, engine: str = "tts.ada"):
    advanced = {"advanced": {**(show.ada.options.get("advanced") or {}), "voice_tags": voice_tags}}
    hass.config_entries.async_update_entry(
        show.ada,
        options={**router_options(ADA_WRITER, tts_engine=engine, voice="bright"), **advanced},
    )
    show.agents.replies[ADA_WRITER] = "[happy] [laughs] Oh, Jeeves. [sighs] Every single day."
    with patch("homeassistant.components.tts.generate_media_source_id", fake_tts):
        await run_chat(hass, show, limits=ChatLimits(max_lines=2, pause=0))
    return show.speakers.announced[1]


async def test_a_chat_line_is_shown_without_tags_and_voiced_with_them(hass, show):
    ada = await chat_with_tags(hass, show, voice_tags=True)

    assert ada["message"] == "Oh, Jeeves. Every single day."
    assert spoken_text(ada["media_id"]) == "[laughs] Oh, Jeeves. [sighs] Every single day."
    assert show.manager.state()["lines"][1]["text"] == "Oh, Jeeves. Every single day."


async def test_a_chat_line_of_a_voice_that_takes_no_tags_has_none(hass, show):
    ada = await chat_with_tags(hass, show, voice_tags=False)

    assert ada["message"] == "Oh, Jeeves. Every single day."
    assert spoken_text(ada["media_id"]) == "Oh, Jeeves. Every single day."


async def test_the_next_writer_reads_a_chat_line_without_its_tags(hass, show):
    hass.config_entries.async_update_entry(
        show.jeeves,
        options={**show.jeeves.options, "advanced": {**show.jeeves.options["advanced"], **VOICE_TAGS}},
    )
    show.agents.writer_reply = "[laughs] Indeed, madam."

    await run_chat(hass, show, limits=ChatLimits(max_lines=2, pause=0))

    assert "[laughs]" not in show.agents.calls_to(ADA_WRITER)[-1]["text"]
    assert "Indeed, madam." in show.agents.calls_to(ADA_WRITER)[-1]["text"]


async def test_a_chat_line_cut_to_the_word_limit_is_voiced_as_it_is_shown(hass, show):
    advanced = {"advanced": {**(show.ada.options.get("advanced") or {}), **VOICE_TAGS}}
    hass.config_entries.async_update_entry(
        show.ada,
        options={**router_options(ADA_WRITER, tts_engine="tts.ada", voice="bright"), **advanced},
    )
    show.agents.replies[ADA_WRITER] = (
        "[laughs] One two three. Four five six seven eight nine ten eleven twelve."
    )
    with patch("homeassistant.components.tts.generate_media_source_id", fake_tts):
        await run_chat(hass, show, limits=ChatLimits(max_lines=2, pause=0, words=5))

    ada = show.speakers.announced[1]
    assert ada["message"] == "One two three."
    assert spoken_text(ada["media_id"]) == "One two three."


# Announcements


async def test_an_announcement_never_has_an_audio_tag(hass, sent):
    _, pipelines = await setup(
        hass,
        options_with(reminders={"agent": "conversation.refusal_writer"}, advanced=VOICE_TAGS),
    )
    agents = FakeAgents(writer_reply="[happy] [clears throat] Alex, the plumber at 5 pm.")

    response = await compose(hass, pipelines, agents)

    assert response == {"text": "Alex, the plumber at 5 pm."}


# The setting


def test_the_setting_is_off_unless_it_is_switched_on():
    assert emotion.voice_tags_on(SimpleNamespace(options={})) is False
    assert emotion.voice_tags_on(SimpleNamespace(options={"advanced": {"voice_tags": False}})) is False
    assert emotion.voice_tags_on(SimpleNamespace(options={"advanced": VOICE_TAGS})) is True


async def test_the_page_saves_the_setting(hass, page):
    entry = await add_router(hass)
    settings = (await page.router(entry))["settings"]
    settings["advanced"] = {**settings["advanced"], "voice_tags": True}

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert emotion.voice_tags_on(entry) is True
