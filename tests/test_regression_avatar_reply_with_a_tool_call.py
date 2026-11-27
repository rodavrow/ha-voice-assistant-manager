# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A reply in which the main agent calls a tool (a web search) before it says more.

Seen in the house: the assistant said one opening sentence and searched for four seconds.
The engine held the sentence back until more text came, the avatar's reply was closed
after five seconds without audio, and the satellite played the whole clip much later.
"""

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import patch

from custom_components.voice_assistant_manager import avatar_stream
from custom_components.voice_assistant_manager.avatar_stream import (
    ReplyFeed,
    answering,
    turn_ended,
    turn_started,
)
from custom_components.voice_assistant_manager.tts import released_on_pause

from .test_avatar_stream import (  # noqa: F401 - fixtures
    HOLD,
    live,
    run,
    target,
    until_sent,
)

END = {"type": "end"}


# The avatar's reply stays open while the assistant is still answering


def test_an_assistant_is_answering_until_its_turn_ended(hass):
    turn = turn_started(hass, "jeeves", "assist_satellite.kitchen")
    assert answering(hass, "jeeves") is True

    turn_ended(turn)

    assert answering(hass, "jeeves") is False
    assert answering(hass, "ada") is False


async def test_regression_no_audio_yet_holds_the_reply_while_the_turn_runs(hass, live, target):
    feed = ReplyFeed(hass, target, "mp3", {}, answering=lambda: True)
    await feed.async_open()

    with (
        patch.object(avatar_stream, "STALL_TIMEOUT", 0.01),
        patch.object(avatar_stream, "HOLD_EVERY", 0.01),
    ):
        running = asyncio.create_task(feed.async_run())
        await until_sent(live, HOLD, 3)
        feed.write(b"answer")
        feed.finish()
        await asyncio.wait_for(running, timeout=5)

    assert [item for item in live.socket.sent if item != HOLD] == [b"pcm:answer", END]


async def test_no_audio_after_the_turn_ended_closes_the_reply(hass, live, target):
    still = {"answering": True}
    feed = ReplyFeed(hass, target, "mp3", {}, answering=lambda: still["answering"])
    await feed.async_open()

    with (
        patch.object(avatar_stream, "STALL_TIMEOUT", 0.01),
        patch.object(avatar_stream, "HOLD_EVERY", 0.01),
    ):
        running = asyncio.create_task(feed.async_run())
        await until_sent(live, HOLD, 2)
        still["answering"] = False
        await asyncio.wait_for(running, timeout=5)

    assert live.socket.sent[-1] == END
    assert live.socket.closed


async def test_a_turn_that_never_ends_closes_the_reply_at_the_limit(hass, live, target):
    feed = ReplyFeed(hass, target, "mp3", {}, answering=lambda: True)
    await feed.async_open()

    with (
        patch.object(avatar_stream, "STALL_TIMEOUT", 0.01),
        patch.object(avatar_stream, "HOLD_EVERY", 0.01),
        patch.object(avatar_stream, "HOLD_LIMIT", 0.05),
    ):
        await run(hass, feed)

    assert HOLD in live.socket.sent
    assert live.socket.sent[-1] == END


# The opening sentence goes to the engine when the text pauses


class Text:
    """Text for the engine, given piece by piece when the test says so."""

    def __init__(self) -> None:
        self._pieces: asyncio.Queue[str | None] = asyncio.Queue()

    def say(self, piece: str | None) -> None:
        self._pieces.put_nowait(piece)

    async def pieces(self) -> AsyncGenerator[str]:
        while (piece := await self._pieces.get()) is not None:
            yield piece


async def test_regression_a_finished_sentence_is_released_when_the_text_pauses():
    text = Text()
    out = released_on_pause(text.pieces(), pause=0.01)
    text.say("Let me see what I can ")
    text.say("find for you.")

    got = [await anext(out), await anext(out), await asyncio.wait_for(anext(out), 5)]
    text.say("Here is the news. ")
    text.say(None)
    got += [piece async for piece in out]

    assert got == ["Let me see what I can ", "find for you.", "\n\n", "Here is the news. "]


async def test_a_sentence_is_released_only_once_per_pause():
    text = Text()
    out = released_on_pause(text.pieces(), pause=0.01)
    text.say("One moment!")

    got = [await anext(out), await asyncio.wait_for(anext(out), 5)]
    following = asyncio.ensure_future(anext(out))
    await asyncio.sleep(0.05)  # Several pauses long: nothing more may come.
    assert not following.done()
    text.say("Done.")
    got.append(await following)
    await out.aclose()

    assert got == ["One moment!", "\n\n", "Done."]


async def test_text_that_stops_in_the_middle_of_a_sentence_is_left_alone():
    text = Text()
    out = released_on_pause(text.pieces(), pause=0.01)
    text.say("Let me see what I can")

    first = await anext(out)
    following = asyncio.ensure_future(anext(out))
    await asyncio.sleep(0.05)
    assert not following.done()
    text.say(" find.")
    text.say(None)
    rest = [await following] + [piece async for piece in out]

    assert [first, *rest] == ["Let me see what I can", " find."]


async def test_text_that_keeps_coming_is_passed_on_unchanged():
    async def pieces() -> AsyncGenerator[str]:
        for piece in ("Hello. ", "How are ", "you?"):
            yield piece

    assert [p async for p in released_on_pause(pieces(), pause=5)] == [
        "Hello. ", "How are ", "you?",
    ]


async def test_closing_the_text_early_leaves_no_task_behind():
    before = len(asyncio.all_tasks())
    text = Text()
    out = released_on_pause(text.pieces(), pause=0.01)
    text.say("Wait.")
    assert [await anext(out), await asyncio.wait_for(anext(out), 5)] == ["Wait.", "\n\n"]

    await out.aclose()
    await asyncio.sleep(0)

    assert len(asyncio.all_tasks()) == before
