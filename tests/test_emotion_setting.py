# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Emotion tags setting: which tags an assistant passes on to its avatar."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.voice_assistant_manager import emotion
from custom_components.voice_assistant_manager.chat_show import ChatLimits

from .router_helpers import FakeAgents, add_router, ask, options_with
from .test_avatar_tts import engines, sent as tts_sent  # noqa: F401 - fixtures
from .test_chat_show import ADA_WRITER, chat_audio, run_chat, show  # noqa: F401 - fixtures
from .test_compose_announcement import compose, setup
from .test_panel import Page, page  # noqa: F401 - fixture


def entry_with(tags):
    advanced = {} if tags is None else {"emotion_tags": tags}
    return SimpleNamespace(title="Ada", options={"advanced": advanced})


# Reading the setting


@pytest.mark.parametrize("tags", [None, "", "  ", ",, ,"])
def test_without_a_list_the_five_emotions_are_accepted(tags):
    assert emotion.accepted_emotions(entry_with(tags)) == emotion.DEFAULT_EMOTIONS


def test_the_five_emotions_are_the_default():
    assert emotion.DEFAULT_EMOTIONS == {"neutral", "happy", "sad", "angry", "sarcastic"}


def test_the_list_is_split_on_commas_and_spaces_and_lower_cased():
    accepted = emotion.accepted_emotions(entry_with(" Happy,sad  excited\nBored "))

    assert accepted == {"happy", "sad", "excited", "bored"}


def test_an_entry_that_cannot_be_a_tag_is_ignored_and_warned_about_once(caplog):
    entry = entry_with("happy, 2b, x_y, a-b, " + "a" * 19)

    with caplog.at_level(logging.WARNING):
        first = emotion.accepted_emotions(entry)
        again = emotion.accepted_emotions(entry)

    assert first == again == {"happy"}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Ada" in warnings[0].getMessage()


def test_the_longest_word_that_fits_in_a_tag_is_accepted():
    word = "a" * 18

    assert emotion.accepted_emotions(entry_with(word)) == {word}
    assert emotion.split_tag(f"[{word}] Hi.", frozenset({word})) == (word, "Hi.")


def test_a_list_of_only_bad_entries_falls_back_to_the_five_emotions():
    assert emotion.accepted_emotions(entry_with("1, 2")) == emotion.DEFAULT_EMOTIONS


def test_split_tag_passes_on_only_an_accepted_emotion():
    accepted = frozenset({"excited"})

    assert emotion.split_tag("[Excited] Wow.", accepted) == ("excited", "Wow.")
    assert emotion.split_tag("[happy] Wow.", accepted) == ("neutral", "Wow.")


def test_the_stream_filter_passes_on_only_an_accepted_emotion():
    tag_filter = emotion.TagFilter(frozenset({"excited"}))

    assert tag_filter.feed("[excited] Wow") == "Wow"
    assert tag_filter.emotion == "excited"


# Where it is used


@pytest.fixture
def told():
    calls: list[tuple[str, str]] = []
    with patch.object(
        emotion, "send_emotion", lambda _hass, entry, found, *_why: calls.append((entry.title, found))
    ):
        yield calls


async def test_a_reply_passes_on_a_tag_from_the_assistants_list(hass, told):
    entry = await add_router(hass, options_with(advanced={"emotion_tags": "excited, happy"}))
    agents = FakeAgents(main_reply="[excited] We won!")

    with agents.patched():
        await ask(hass, entry, "Score?")

    assert told == [("Kitchen assistant", "excited")]


async def test_a_reply_with_a_tag_not_on_the_list_is_neutral(hass, told):
    entry = await add_router(hass)
    # Not on the Emotion tags list, and not in the Face for audio tags table either.
    agents = FakeAgents(main_reply="[thrilled] We won!")

    with agents.patched():
        await ask(hass, entry, "Score?")

    assert told == [("Kitchen assistant", "neutral")]


async def test_a_chat_line_passes_on_a_tag_from_its_assistants_list(hass, show, told):
    options = dict(show.jeeves.options)
    options["advanced"] = {**options.get("advanced", {}), "emotion_tags": "dour"}
    hass.config_entries.async_update_entry(show.jeeves, options=options)
    show.agents.writer_reply = "[dour] Quite so."
    show.agents.replies[ADA_WRITER] = "[dour] Hardly."

    await run_chat(hass, show, limits=ChatLimits(max_lines=2, pause=0))

    assert told == [("Jeeves", "dour"), ("Ada", "neutral")]
    assert show.speakers.messages == ["Quite so.", "Hardly."]


async def test_an_announcement_passes_on_a_tag_from_its_assistants_list(hass, told):
    _, pipelines = await setup(
        hass,
        options_with(reminders={"agent": "conversation.refusal_writer"},
                     advanced={"emotion_tags": "urgent"}),
    )
    agents = FakeAgents(writer_reply="[urgent] Alex, the plumber at 5 pm!")

    await compose(hass, pipelines, agents)

    assert told == [("Kitchen assistant", "urgent")]


async def test_the_page_saves_the_list(hass, page):
    entry = await add_router(hass)
    settings = (await page.router(entry))["settings"]
    settings["advanced"] = {**settings["advanced"], "emotion_tags": "happy, excited"}

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert entry.options["advanced"]["emotion_tags"] == "happy, excited"
