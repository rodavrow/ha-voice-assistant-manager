# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Emotion tags in announcements and calendar lines: never in the text, told to the avatar."""

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.voice_assistant_manager import announce, emotion

from .router_helpers import FakeAgents
from .test_compose_announcement import compose, setup


@pytest.fixture
def sent():
    calls: list[tuple[str, str]] = []
    with patch.object(
        emotion, "send_emotion", lambda _hass, entry, found, *_why: calls.append((entry.title, found))
    ):
        yield calls


async def test_an_announcement_is_returned_without_its_tag(hass: HomeAssistant, sent):
    _, pipelines = await setup(hass)
    agents = FakeAgents(writer_reply="[sarcastic] Alex, do call the plumber at 5 pm.")

    response = await compose(hass, pipelines, agents)

    assert response == {"text": "Alex, do call the plumber at 5 pm."}
    assert sent == [("Kitchen assistant", "sarcastic")]


async def test_an_announcement_without_a_tag_is_neutral(hass: HomeAssistant, sent):
    _, pipelines = await setup(hass)
    agents = FakeAgents(writer_reply="Alex, call the plumber at 5 pm.")

    await compose(hass, pipelines, agents)

    assert sent == [("Kitchen assistant", "neutral")]


async def test_an_announcement_that_is_only_a_tag_returns_no_text_and_tells_nothing(
    hass: HomeAssistant, sent
):
    _, pipelines = await setup(hass)
    agents = FakeAgents(writer_reply="[happy]")

    response = await compose(hass, pipelines, agents)

    assert response == {"text": ""}
    assert sent == []


async def test_a_calendar_line_is_returned_without_its_tag(hass: HomeAssistant, sent):
    entry, _ = await setup(hass)
    agents = FakeAgents(writer_reply="[happy] Alex, the dentist is at 3 pm.")

    with agents.patched():
        line = await announce.async_compose_calendar_line(
            hass, entry, "alex", "Dentist", "today at 3 pm", ""
        )

    assert line == "Alex, the dentist is at 3 pm."
    assert sent == [("Kitchen assistant", "happy")]
