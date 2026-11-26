# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Emotion tags in chat show lines: never spoken or shown, told before each line's audio."""

from unittest.mock import patch

import pytest

from custom_components.voice_assistant_manager import emotion
from custom_components.voice_assistant_manager.chat_show import ChatLimits

from .test_avatar_tts import engines, sent  # noqa: F401 - fixtures
from .test_chat_show import (  # noqa: F401 - fixtures
    ADA_WRITER,
    PLAYER,
    chat_audio,
    fake_tts,
    player,
    run_chat,
    show,
)
from .test_stage import TV, cast  # noqa: F401 - fixtures

TWO_LINES = ChatLimits(max_lines=2, pause=0)
JEEVES_SAID = "Indeed, madam."
ADA_SAID = "Oh, Jeeves."


@pytest.fixture
def tagged(show):
    """Jeeves writes his lines happy; Ada's have no tag. Returns the event order."""
    show.agents.writer_reply = f"[happy] {JEEVES_SAID}"
    show.agents.replies[ADA_WRITER] = ADA_SAID
    order: list[str] = show.speakers.order
    with patch.object(
        emotion,
        "send_emotion",
        lambda _hass, entry, found, *_why: order.append(f"emotion {entry.title} {found}"),
    ):
        yield order


async def test_a_satellite_announces_each_line_without_its_tag(hass, show, tagged):
    await run_chat(hass, show, limits=TWO_LINES)

    assert show.speakers.messages == [JEEVES_SAID, ADA_SAID]
    assert [line["text"] for line in show.manager.state()["lines"]] == [JEEVES_SAID, ADA_SAID]


async def test_the_next_writer_reads_the_line_without_its_tag(hass, show, tagged):
    await run_chat(hass, show, limits=TWO_LINES)

    assert "[happy]" not in show.agents.calls[1]["text"]
    assert JEEVES_SAID in show.agents.calls[1]["text"]


async def test_each_line_tells_its_emotion_just_before_it_is_announced(hass, show, tagged):
    await run_chat(hass, show, limits=TWO_LINES)

    assert tagged == ["emotion Jeeves happy", "announce", "emotion Ada neutral", "announce"]


async def test_on_the_stage_each_line_sends_its_emotion_with_its_audio(
    hass, show, cast, tagged
):
    # The upload carries the emotion for exactly the stage's session, so no /set_emotion
    # is sent: it would also leave the emotion pending on a satellite showing the avatar.
    async def say(session_id, extension, audio, emotion=None):
        tagged.append(f"say {session_id} {emotion}")

    with patch.object(cast.live, "async_say", say), patch(
        "custom_components.voice_assistant_manager.stage._async_wait_spoken",
        lambda *args: _done(),
    ):
        await run_chat(hass, show, speaker=TV, limits=TWO_LINES)

    assert tagged == ["say session-jeeves happy", "say session-ada neutral"]


async def _done() -> None:
    return None


async def test_a_media_player_line_tells_its_emotion_before_it_plays(
    hass, show, player, tagged
):
    async def hook(number: int) -> None:
        tagged.append("play")
        attributes = hass.states.get(PLAYER).attributes
        hass.states.async_set(PLAYER, "playing", attributes)
        hass.loop.call_later(0.01, lambda: hass.states.async_set(PLAYER, "idle", attributes))

    show.speakers.hook = hook

    with patch("homeassistant.components.tts.generate_media_source_id", fake_tts):
        await run_chat(hass, show, speaker=PLAYER, limits=TWO_LINES)

    assert tagged == ["emotion Jeeves happy", "play", "emotion Ada neutral", "play"]
    assert all("[happy]" not in call["media_content_id"] for call in show.speakers.played)
