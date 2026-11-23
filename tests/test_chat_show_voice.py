# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for joining in and stopping a chat by voice, through any router."""

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import DATA_CHAT_LOGS, async_get_chat_log
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.chat_session import async_get_chat_session
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from custom_components.voice_assistant_manager.chat_show import (
    GUEST,
    REPLY_GRACE,
    SELF_WAKE_TTL,
    ChatLimits,
    ChatShowManager,
    is_stop_phrase,
)
from custom_components.voice_assistant_manager.conversation import LAST_RESORT_REPLY, PersonaRouter
from custom_components.voice_assistant_manager.reminders import DEFAULT_FAILURE_LINE

from .router_helpers import (
    MAIN_AGENT,
    MEMORY_AGENT,
    add_router,
    ask,
    options_with,
    router_entity_id,
)
from .test_chat_show import (  # noqa: F401 - show and router_pipeline are fixtures
    ADA_LINE,
    CHAT_RUN_TIMEOUT,
    JEEVES_LINE,
    JEEVES_WRITER,
    LISTEN_AFTER,
    LISTEN_FOR,
    SAT_ANNOUNCE,
    SATELLITE,
    add_speaker,
    gate,
    listening_speaker,
    router_options,
    router_pipeline,
    run_chat,
    show,
    start,
)

REFUSAL = "The answer is no."


async def chatting(show: SimpleNamespace) -> None:
    """Start a chat and hold it while its first line is spoken."""
    playing, show.release = gate(show, on_call=1)
    await start(show)
    await playing.wait()


def person_lines(show: SimpleNamespace) -> list[dict[str, str]]:
    return [line for line in show.manager.state()["lines"] if line["who"] == "Alex"]


def nothing_else_answered(show: SimpleNamespace) -> bool:
    return not show.agents.calls_to(MAIN_AGENT) and not show.agents.calls_to(MEMORY_AGENT)


@pytest.mark.parametrize(
    "text",
    ["stop", "Stop!", "ENOUGH", "quiet", "That's enough", "that’s enough.", "be quiet",
     "Shut up", "end the chat", "stop the chat please", "enough, thank you"],
)
def test_stop_phrases_are_recognised(text):
    assert is_stop_phrase(text)


@pytest.mark.parametrize(
    "text",
    ["", "stopwatch", "I like your hat", "please stop", "enoughness", "quietly now", "go on"],
)
def test_other_words_are_not_a_stop_phrase(text):
    assert not is_stop_phrase(text)


async def test_a_person_stopping_ends_the_chat_with_okay(hass, show):
    await chatting(show)

    result = await ask(hass, show.ada, "Stop", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == "Okay."
    assert show.manager.state()["ended"] == "chat_stopped"
    assert nothing_else_answered(show)
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_an_unknown_voice_can_stop_the_chat(hass, show):
    third = await add_router(hass, options_with(advanced={"no_helper_role": "stranger"}))
    await chatting(show)

    result = await ask(hass, third, "that's enough", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == "Okay."
    assert show.manager.state()["ended"] == "chat_stopped"
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_person_joins_in_and_nothing_else_answers(hass, show):
    await chatting(show)

    result = await ask(hass, show.jeeves, "I think cats are better", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == ""
    assert [line["text"] for line in person_lines(show)] == ["I think cats are better"]
    assert show.manager.state()["ended"] == ""
    assert nothing_else_answered(show)
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_person_line_is_capped_at_200_characters(hass, show):
    await chatting(show)

    long_turn = "cats are better " * 30

    await ask(hass, show.jeeves, long_turn, device_id=show.device)

    assert person_lines(show)[0]["text"] == long_turn.strip()[:200]
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


def guest_lines(show: SimpleNamespace) -> list[str]:
    return [line["text"] for line in show.manager.state()["lines"] if line["who"] == GUEST]


async def test_an_unknown_voice_joins_in_as_a_guest(hass, show):
    third = await add_router(hass, options_with(advanced={"no_helper_role": "stranger"}))
    await chatting(show)

    result = await ask(hass, third, "Can I say something too?", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == ""
    assert guest_lines(show) == ["Can I say something too?"]
    assert person_lines(show) == []
    assert show.manager.state()["ended"] == ""
    assert nothing_else_answered(show)
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_guest_never_gets_the_stranger_reply(hass, show):
    third = await add_router(hass, options_with(advanced={"no_helper_role": "stranger"}))
    await chatting(show)
    calls_before = len(show.agents.calls)

    await ask(hass, third, "Guys, am I part of it?", device_id=show.device)

    assert len(show.agents.calls) == calls_before
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_after_the_grace_an_unknown_voice_gets_the_stranger_rules_again(
    hass, show, freezer
):
    third = await add_router(hass, options_with(advanced={"no_helper_role": "stranger"}))
    await run_chat(hass, show, limits=ChatLimits(max_lines=2, pause=0))
    freezer.tick(timedelta(seconds=REPLY_GRACE + 1))

    result = await ask(hass, third, "Can I say something too?", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == REFUSAL
    assert nothing_else_answered(show)


@pytest.mark.parametrize(
    "text",
    ["I'm", "Hey Ada, I'm", "Hey Ada, I’m", "Jeeves, and the", "Well I think the",
     "Hey Ada.", "Hey Ada?"],
)
async def test_a_fragment_is_dropped_and_the_chat_goes_on(hass, show, text):
    await chatting(show)

    result = await ask(hass, show.jeeves, text, device_id=show.device)

    assert result.response.speech["plain"]["speech"] == ""
    assert person_lines(show) == []
    assert show.manager.state()["ended"] == ""
    assert nothing_else_answered(show)
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize(
    "text",
    [
        "No way.",
        "Hey Ada, I disagree.",
        "Me too!",
        "Who are you talking with?",
        "Where is it from?",
        "Oh my!",
        "Why?",
        "我觉得猫更好",
    ],
)
async def test_a_short_complete_turn_joins_in(hass, show, text):
    await chatting(show)

    await ask(hass, show.jeeves, text, device_id=show.device)

    assert [line["text"] for line in person_lines(show)] == [text]
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_each_turn_is_logged_by_kind_without_what_was_said(hass, show, caplog):
    third = await add_router(hass, options_with(advanced={"no_helper_role": "stranger"}))
    await chatting(show)

    with caplog.at_level(logging.INFO):
        await ask(hass, third, "Can I say something too?", device_id=show.device)
        await ask(hass, show.jeeves, "Hey Ada, I'm", device_id=show.device)

    assert "joined as a guest" in caplog.text
    assert "fragment dropped" in caplog.text
    assert "say something" not in caplog.text
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_another_device_is_handled_normally(hass, show):
    other = add_speaker(hass, "assist_satellite.office_satellite", "idle", 1)
    await chatting(show)

    result = await ask(hass, show.jeeves, "Stop", device_id=other)

    assert result.response.speech["plain"]["speech"] != "Okay."
    assert show.manager.state()["ended"] == ""
    assert person_lines(show) == []
    assert len(show.agents.calls_to(MAIN_AGENT)) == 1
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_it_works_through_a_third_router_that_is_not_in_the_chat(hass, show):
    third = await add_router(hass, router_options("conversation.third_writer"), title="Third")
    await chatting(show)

    result = await ask(hass, third, "hello there", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == ""
    assert [line["text"] for line in person_lines(show)] == ["hello there"]
    assert nothing_else_answered(show)
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_without_a_chat_the_request_goes_to_the_main_agent(hass, show):
    result = await ask(hass, show.jeeves, "Stop", device_id=show.device)

    assert result.response.speech["plain"]["speech"] != "Okay."
    assert len(show.agents.calls_to(MAIN_AGENT)) == 1


@pytest.mark.parametrize(
    "lookup", [{"who": "near_miss"}, {"who": "stranger"}, {}], ids=["near-miss", "stranger", "none"]
)
async def test_any_voice_that_is_not_a_person_joins_as_a_guest(hass, show, lookup):
    await chatting(show)

    with patch.object(PersonaRouter, "_lookup_from_settings", return_value=lookup):
        result = await ask(hass, show.jeeves, "I think cats are better", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == ""
    assert guest_lines(show) == ["I think cats are better"]
    assert nothing_else_answered(show)
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_router_without_saved_settings_lets_an_unknown_voice_join_as_a_guest(
    hass, show
):
    """The "who is speaking" script path: an empty answer is nobody identified."""
    await chatting(show)

    with patch.object(PersonaRouter, "_lookup_from_script", return_value={}):
        bare = await add_router(hass, {})
        result = await ask(hass, bare, "I think cats are better", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == ""
    assert guest_lines(show) == ["I think cats are better"]
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_failed_lookup_during_a_chat_joins_as_a_guest(hass, show):
    await chatting(show)

    with patch.object(
        PersonaRouter, "_lookup_from_settings", side_effect=RuntimeError("lookup broke")
    ):
        result = await ask(hass, show.jeeves, "turn on the lights", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == ""
    assert guest_lines(show) == ["turn on the lights"]
    assert show.manager.state()["ended"] == ""
    assert nothing_else_answered(show)
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_regression_chat_a_stop_phrase_works_when_the_lookup_failed(hass, show):
    await chatting(show)

    with patch.object(
        PersonaRouter, "_lookup_from_settings", side_effect=RuntimeError("lookup broke")
    ):
        result = await ask(hass, show.jeeves, "stop", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == "Okay."
    assert show.manager.state()["ended"] == "chat_stopped"
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_regression_chat_a_late_reply_after_the_chat_ended_never_reaches_the_main_agent(
    hass, show, freezer
):
    """The satellite may still listen after the last line, or after a stop."""
    await run_chat(hass, show, limits=ChatLimits(max_lines=2, pause=0))

    result = await ask(hass, show.jeeves, "turn on the lights", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == ""
    assert person_lines(show) == []
    assert nothing_else_answered(show)


async def test_regression_chat_a_late_stop_after_the_chat_ended_is_answered_okay(
    hass, show
):
    await run_chat(hass, show, limits=ChatLimits(max_lines=2, pause=0))

    result = await ask(hass, show.ada, "stop", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == "Okay."
    assert show.manager.state()["ended"] == "chat_done"
    assert nothing_else_answered(show)


async def test_after_the_grace_the_device_is_handled_normally_again(hass, show, freezer):
    await run_chat(hass, show, limits=ChatLimits(max_lines=2, pause=0))
    freezer.tick(timedelta(seconds=REPLY_GRACE + 1))

    await ask(hass, show.jeeves, "turn on the lights", device_id=show.device)

    assert len(show.agents.calls_to(MAIN_AGENT)) == 1


async def test_regression_chat_an_unexpected_error_during_a_chat_fails_closed(hass, show):
    await chatting(show)

    with patch.object(ChatShowManager, "add_person_line", side_effect=RuntimeError("broke")):
        result = await ask(hass, show.jeeves, "turn on the lights", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == LAST_RESORT_REPLY
    assert nothing_else_answered(show)
    show.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_regression_chat_an_unexpected_error_without_a_chat_lets_the_request_through(
    hass, show
):
    with patch.object(
        ChatShowManager, "is_running_on", side_effect=[RuntimeError("broke"), False]
    ):
        result = await ask(hass, show.jeeves, "turn on the lights", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == "Noted."
    assert len(show.agents.calls_to(MAIN_AGENT)) == 1


async def test_regression_chat_when_it_cannot_tell_whether_a_chat_runs_it_refuses(hass, show):
    with patch.object(ChatShowManager, "is_running_on", side_effect=RuntimeError("broke")):
        result = await ask(hass, show.jeeves, "turn on the lights", device_id=show.device)

    assert result.response.speech["plain"]["speech"] == LAST_RESORT_REPLY
    assert nothing_else_answered(show)


# A chat on another satellite.

OFFICE = "assist_satellite.office_satellite"


async def test_regression_chat_a_new_chat_elsewhere_keeps_the_grace_of_the_last_speaker(
    hass, show
):
    """A chat that starts on another satellite must not end this satellite's grace."""
    await run_chat(hass, show, limits=ChatLimits(max_lines=2, pause=0))
    add_speaker(hass, OFFICE, "idle", SAT_ANNOUNCE)
    # The two lines of the first chat were calls 1 and 2.
    playing, show.release = gate(show, on_call=3)
    await start(show, speaker=OFFICE)
    await asyncio.wait_for(playing.wait(), timeout=CHAT_RUN_TIMEOUT)

    reply = await ask(hass, show.jeeves, "turn on the lights", device_id=show.device)
    stop = await ask(hass, show.ada, "stop", device_id=show.device)

    assert reply.response.speech["plain"]["speech"] == ""
    assert stop.response.speech["plain"]["speech"] == "Okay."
    # Neither touches the chat in the office.
    assert show.manager.state()["speaker"] == OFFICE
    assert show.manager.state()["ended"] == ""
    assert person_lines(show) == []
    assert nothing_else_answered(show)
    show.release.set()
    await asyncio.wait_for(
        hass.async_block_till_done(wait_background_tasks=True), timeout=CHAT_RUN_TIMEOUT
    )


# After a chat that listened: the satellite's conversation holds a chat line.


async def ask_on_satellite(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    text: str,
    conversation_id: str | None,
    device_id: str,
) -> tuple[conversation.ConversationResult, str]:
    """Ask the router as a satellite's pipeline does, in the satellite's conversation.

    Returns the result and the conversation the pipeline ran in: Home Assistant starts a new
    one when the satellite's has expired.
    """
    entity_id = router_entity_id(hass, entry)
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(entity_id)
    assert entity is not None
    context = Context()
    with async_get_chat_session(hass, conversation_id, context=context) as session:
        user_input = conversation.ConversationInput(
            text=text,
            context=context,
            conversation_id=session.conversation_id,
            device_id=device_id,
            satellite_id=None,
            language="en",
            agent_id=entity_id,
        )
        with async_get_chat_log(hass, session, user_input):
            result = await entity.async_process(user_input)
        return result, session.conversation_id


def said(content: list[Any]) -> list[str]:
    return [str(item.content) for item in content if getattr(item, "content", None)]


async def listened_chat(hass: HomeAssistant, show: SimpleNamespace) -> str:
    """Run a chat whose second line listened; returns the satellite's conversation id."""
    listening_speaker(hass)
    show.agents.main_uses_session = True
    await run_chat(hass, show, limits=ChatLimits(max_lines=3, pause=0, listen=True))
    chat_conversation = show.speakers.conversations[SATELLITE]
    # As in Home Assistant, the line is in that conversation's history.
    assert ADA_LINE in said(hass.data[DATA_CHAT_LOGS][chat_conversation].content)
    return chat_conversation


def tick(hass: HomeAssistant, request: pytest.FixtureRequest, minutes: float) -> None:
    """Let time pass; Home Assistant forgets a conversation unused for five minutes.

    The clock is frozen only from the first tick on: a chat that listens needs it running.
    """
    request.getfixturevalue("freezer").tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)


async def test_regression_chat_a_request_after_a_listened_chat_reaches_the_main_agent_without_it(
    hass, show, router_pipeline, request
):
    """The main agent, with its tools, never sees a chat line in its history."""
    chat_conversation = await listened_chat(hass, show)
    tick(hass, request, 0.5)

    result, _ = await ask_on_satellite(
        hass, show.jeeves, "turn on the lights", chat_conversation, show.device
    )

    assert result.response.speech["plain"]["speech"] == "Noted."
    (call,) = show.agents.calls_to(MAIN_AGENT)
    assert call["conversation_id"] != chat_conversation
    (history,) = show.agents.main_histories
    assert ADA_LINE not in said(history)
    assert JEEVES_LINE not in said(history)
    assert ADA_LINE not in (call.get("extra_system_prompt") or "")


async def test_regression_chat_a_follow_up_after_a_listened_chat_stays_in_the_clean_conversation(
    hass, show, router_pipeline, request
):
    """The main agent still knows its last answer, and the satellite keeps its conversation."""
    chat_conversation = await listened_chat(hass, show)
    tick(hass, request, 0.5)

    first, _ = await ask_on_satellite(
        hass, show.jeeves, "turn on the lights", chat_conversation, show.device
    )
    await ask_on_satellite(hass, show.jeeves, "and the fan", chat_conversation, show.device)

    first_call, second_call = show.agents.calls_to(MAIN_AGENT)
    assert first_call["conversation_id"] is None
    assert second_call["conversation_id"] not in (None, chat_conversation)
    second_history = said(show.agents.main_histories[1])
    assert "Alex: turn on the lights" in second_history
    assert ADA_LINE not in second_history
    assert first.conversation_id == chat_conversation


async def test_regression_chat_a_conversation_kept_in_use_never_shows_the_chat_line(
    hass, show, router_pipeline, request
):
    """The satellite keeps its conversation while it is used, also beyond five minutes."""
    chat_conversation = await listened_chat(hass, show)
    for _ in range(3):
        tick(hass, request, 4)
        _, used = await ask_on_satellite(
            hass, show.jeeves, "turn on the lights", chat_conversation, show.device
        )
        assert used == chat_conversation

    for history in show.agents.main_histories:
        assert ADA_LINE not in said(history)
    main_calls = show.agents.calls_to(MAIN_AGENT)
    assert len(main_calls) == 3
    assert all(call["conversation_id"] != chat_conversation for call in main_calls)


async def test_once_the_chat_conversation_expired_the_satellite_conversation_is_used(
    hass, show, router_pipeline, request
):
    chat_conversation = await listened_chat(hass, show)
    tick(hass, request, 6)

    _, used = await ask_on_satellite(
        hass, show.jeeves, "turn on the lights", chat_conversation, show.device
    )

    assert used != chat_conversation
    (call,) = show.agents.calls_to(MAIN_AGENT)
    assert call["conversation_id"] == used


async def test_another_satellite_keeps_its_conversation_after_a_listened_chat(
    hass, show, router_pipeline, request
):
    office = add_speaker(hass, OFFICE, "idle", SAT_ANNOUNCE)
    _, office_conversation = await ask_on_satellite(hass, show.jeeves, "hello", None, office)
    await listened_chat(hass, show)
    tick(hass, request, 0.5)

    await ask_on_satellite(hass, show.jeeves, "and now?", office_conversation, office)

    assert [call["conversation_id"] for call in show.agents.calls_to(MAIN_AGENT)] == [
        office_conversation,
        office_conversation,
    ]


async def test_a_chat_without_listening_leaves_the_satellite_conversation_alone(
    hass, show, request
):
    show.agents.main_uses_session = True
    _, before = await ask_on_satellite(hass, show.jeeves, "hello", None, show.device)
    await run_chat(hass, show, limits=ChatLimits(max_lines=3, pause=0))
    tick(hass, request, 0.5)

    await ask_on_satellite(hass, show.jeeves, "and now?", before, show.device)

    assert show.agents.calls_to(MAIN_AGENT)[-1]["conversation_id"] == before
    assert "Alex: hello" in said(show.agents.main_histories[-1])


async def test_the_reminder_check_still_works_in_the_clean_conversation(
    hass, show, router_pipeline, request
):
    """The main agent answers in a conversation of its own; its tool calls are seen there."""
    hass.config_entries.async_update_entry(
        show.jeeves,
        options=options_with(
            reminders={"agent": JEEVES_WRITER, "enabled": True},
            chat={"character": "A dry, put-upon butler."},
            memory={"enabled": False},
        ),
    )
    await hass.async_block_till_done()
    chat_conversation = await listened_chat(hass, show)
    show.agents.main_reply = "Certainly, your reminder is set."
    tick(hass, request, 0.5)

    result, _ = await ask_on_satellite(
        hass, show.jeeves, "Remind me at 5 pm to call the plumber", chat_conversation, show.device
    )

    assert show.agents.calls_to(MAIN_AGENT)[0]["conversation_id"] != chat_conversation
    assert result.response.speech["plain"]["speech"] == DEFAULT_FAILURE_LINE


async def test_when_it_cannot_tell_whether_a_chat_line_is_there_the_main_agent_starts_afresh(
    hass, show
):
    _, conversation_id = await ask_on_satellite(hass, show.jeeves, "hello", None, show.device)

    with patch.object(
        ChatShowManager, "main_conversation_id", side_effect=RuntimeError("broke")
    ):
        result, _ = await ask_on_satellite(
            hass, show.jeeves, "and now?", conversation_id, show.device
        )

    assert result.response.speech["plain"]["speech"] == "Noted."
    assert show.agents.calls_to(MAIN_AGENT)[-1]["conversation_id"] is None


# The speaker wakes on its own chat line: the assistants say each other's names, which are
# the wake words. Home Assistant sets a satellite responding while it announces and idle when
# the announcement returns (assist_satellite/entity.py); a wake word heard during the line
# sets it listening (STT_START) while the announcement still plays.


def set_satellite(hass: HomeAssistant, state: str) -> None:
    current = hass.states.get(SATELLITE)
    attributes = current.attributes if current is not None else {}
    hass.states.async_set(SATELLITE, state, attributes)


class WakingSatellite:
    """Plays each line as Home Assistant does; the wake word fires on the lines in `wakes`.

    `hold` is a call held while it plays (after the wake, if it wakes) until `release`.
    `ended` holds the calls that have finished playing.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        show: SimpleNamespace,
        wakes: tuple[int, ...] = (),
        hold: int | None = None,
    ) -> None:
        self.hass = hass
        self.wakes = wakes
        self.hold = hold
        self.playing = asyncio.Event()
        self.release = asyncio.Event()
        self.ended: list[int] = []
        show.speakers.hook = self.hook

    async def hook(self, number: int) -> None:
        set_satellite(self.hass, "responding")
        if number in self.wakes:
            set_satellite(self.hass, "listening")
        if number == self.hold:
            self.playing.set()
            await self.release.wait()
        set_satellite(self.hass, "idle")
        self.ended.append(number)


async def until(predicate: Callable[[], bool]) -> None:
    """Let the chat run until the predicate holds."""

    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=CHAT_RUN_TIMEOUT)


async def finish(hass: HomeAssistant) -> None:
    await asyncio.wait_for(
        hass.async_block_till_done(wait_background_tasks=True), timeout=CHAT_RUN_TIMEOUT
    )


def speech(result: conversation.ConversationResult) -> str:
    return str(result.response.speech["plain"]["speech"])


ALL_LINES = [JEEVES_LINE, ADA_LINE, JEEVES_LINE, ADA_LINE]


async def test_regression_chat_self_wake_during_a_line_is_ignored(hass, show):
    satellite = WakingSatellite(hass, show, wakes=(1,), hold=1)
    await start(show)
    await asyncio.wait_for(satellite.playing.wait(), timeout=CHAT_RUN_TIMEOUT)

    result = await ask(hass, show.jeeves, "Ada, what do you think", device_id=show.device)

    assert speech(result) == ""
    assert person_lines(show) == []
    assert nothing_else_answered(show)
    assert show.manager.state()["ended"] == ""
    satellite.release.set()
    await finish(hass)
    assert show.speakers.messages == ALL_LINES
    assert show.manager.state()["ended"] == "chat_done"


async def test_regression_chat_self_wake_with_a_stop_phrase_does_not_stop_the_chat(hass, show):
    satellite = WakingSatellite(hass, show, wakes=(1,), hold=1)
    await start(show)
    await asyncio.wait_for(satellite.playing.wait(), timeout=CHAT_RUN_TIMEOUT)

    result = await ask(hass, show.ada, "stop the chat, Jeeves", device_id=show.device)

    assert speech(result) == ""
    assert show.manager.state()["ended"] == ""
    satellite.release.set()
    await finish(hass)
    assert show.speakers.messages == ALL_LINES
    assert show.manager.state()["ended"] == "chat_done"


async def test_regression_chat_self_wake_heard_after_the_line_ended_is_ignored(hass, show):
    """The request arrives once the satellite has heard the rest of the line."""
    satellite = WakingSatellite(hass, show, wakes=(1,))
    await start(show)
    await until(lambda: 1 in satellite.ended)

    result = await ask(hass, show.jeeves, "what do you think", device_id=show.device)

    assert speech(result) == ""
    assert person_lines(show) == []
    assert nothing_else_answered(show)
    # The next line waited for the woken satellite's request.
    assert show.speakers.messages == [JEEVES_LINE]
    await finish(hass)
    assert show.speakers.messages == ALL_LINES
    assert show.manager.state()["ended"] == "chat_done"


async def test_regression_chat_self_wake_right_after_a_line_is_ignored(hass, show):
    """Within a moment of the line's end the wake word still comes from the line."""

    async def hook(number: int) -> None:
        set_satellite(hass, "responding")
        set_satellite(hass, "idle")
        if number == 1:
            hass.loop.call_soon(set_satellite, hass, "listening")

    show.speakers.hook = hook
    await start(show)
    await until(lambda: hass.states.get(SATELLITE).state == "listening")

    result = await ask(hass, show.jeeves, "Ada, what do you think", device_id=show.device)
    set_satellite(hass, "idle")

    assert speech(result) == ""
    assert person_lines(show) == []
    assert nothing_else_answered(show)
    await finish(hass)
    assert show.speakers.messages == ALL_LINES
    assert show.manager.state()["ended"] == "chat_done"


async def wake_in_the_pause(
    hass: HomeAssistant,
    show: SimpleNamespace,
    request: pytest.FixtureRequest,
    wakes: tuple[int, ...] = (),
) -> None:
    """Start a chat with a long pause and wake the satellite in it, past the margin.

    `wakes`: lines that woke the satellite before, whose request never came.
    """
    satellite = WakingSatellite(hass, show, wakes=wakes)
    await start(show, limits=ChatLimits(max_lines=4, pause=10))
    await until(lambda: 1 in satellite.ended)
    for _ in range(10):  # Let the loop leave the announcement and start the pause.
        await asyncio.sleep(0)
    request.getfixturevalue("freezer").tick(timedelta(seconds=3))
    set_satellite(hass, "listening")


async def test_regression_chat_self_wake_check_still_lets_a_person_join_in_the_pause(
    hass, show, request
):
    await wake_in_the_pause(hass, show, request)

    result = await ask(hass, show.jeeves, "I think cats are better", device_id=show.device)

    assert speech(result) == ""
    assert [line["text"] for line in person_lines(show)] == ["I think cats are better"]
    assert nothing_else_answered(show)
    set_satellite(hass, "idle")
    await show.manager.async_stop()
    await finish(hass)


async def test_regression_chat_self_wake_check_still_lets_stop_work_in_the_pause(
    hass, show, request
):
    await wake_in_the_pause(hass, show, request)

    result = await ask(hass, show.ada, "stop", device_id=show.device)

    assert speech(result) == "Okay."
    assert show.manager.state()["ended"] == "chat_stopped"
    set_satellite(hass, "idle")
    await finish(hass)


async def test_regression_chat_self_wake_leaves_another_device_alone(hass, show):
    other = add_speaker(hass, OFFICE, "idle", SAT_ANNOUNCE)
    satellite = WakingSatellite(hass, show, wakes=(1,), hold=1)
    await start(show)
    await asyncio.wait_for(satellite.playing.wait(), timeout=CHAT_RUN_TIMEOUT)

    elsewhere = await ask(hass, show.jeeves, "Stop", device_id=other)
    woken = await ask(hass, show.jeeves, "Ada, what do you think", device_id=show.device)

    assert speech(elsewhere) == "Noted."
    assert len(show.agents.calls_to(MAIN_AGENT)) == 1
    assert speech(woken) == ""
    assert show.manager.state()["ended"] == ""
    satellite.release.set()
    await finish(hass)


async def test_regression_chat_self_wake_ignores_one_request_only(hass, show):
    satellite = WakingSatellite(hass, show, wakes=(1,), hold=1)
    await start(show)
    await asyncio.wait_for(satellite.playing.wait(), timeout=CHAT_RUN_TIMEOUT)

    first = await ask(hass, show.jeeves, "Ada, what do you think", device_id=show.device)
    second = await ask(hass, show.jeeves, "I like cheese", device_id=show.device)

    assert speech(first) == ""
    assert speech(second) == ""
    satellite.release.set()
    await finish(hass)


async def test_regression_chat_self_wake_during_a_listening_line_is_ignored(
    hass, show, router_pipeline
):
    """A wake during a line spoken with start_conversation: the satellite was responding."""
    listening_speaker(hass)
    answers: list[conversation.ConversationResult] = []

    async def start_hook(number: int) -> None:
        set_satellite(hass, "responding")
        set_satellite(hass, "listening")
        answers.append(await ask(hass, show.jeeves, "Jeeves, really", device_id=show.device))

    show.speakers.start_hook = start_hook

    await run_chat(hass, show, limits=ChatLimits(max_lines=3, pause=0, listen=True))

    assert [speech(answer) for answer in answers] == [""]
    assert person_lines(show) == []
    assert nothing_else_answered(show)
    assert show.manager.state()["ended"] == "chat_done"


async def test_regression_chat_self_wake_check_lets_a_reply_after_a_listening_line_join_in(
    hass, show, router_pipeline
):
    """The satellite listening after start_conversation is the person's turn, not a wake."""
    listening_speaker(hass)
    answers: list[conversation.ConversationResult] = []

    async def reply() -> None:
        answers.append(await ask(hass, show.jeeves, "Tell me more!", device_id=show.device))

    async def start_hook(number: int) -> None:
        set_satellite(hass, "responding")
        hass.loop.call_later(
            LISTEN_AFTER + LISTEN_FOR / 2, lambda: hass.async_create_task(reply())
        )

    show.speakers.start_hook = start_hook

    await run_chat(hass, show, limits=ChatLimits(max_lines=3, pause=0, listen=True))

    assert [speech(answer) for answer in answers] == [""]
    assert [line["text"] for line in person_lines(show)] == ["Tell me more!"]


async def test_regression_chat_self_wake_request_is_waited_for_from_the_end_of_the_line(
    hass, show, request
):
    """The wake came 10 s before a long line ended; the request comes 4 s after its end.

    Home Assistant sets the satellite idle when the line ends while it still listens, so
    only waiting for the request keeps the next line from cancelling that pipeline.
    """
    satellite = WakingSatellite(hass, show, wakes=(1,), hold=1)
    await start(show)
    await asyncio.wait_for(satellite.playing.wait(), timeout=CHAT_RUN_TIMEOUT)
    freezer = request.getfixturevalue("freezer")
    freezer.tick(timedelta(seconds=10))
    satellite.release.set()
    await until(lambda: 1 in satellite.ended)
    for _ in range(10):  # Let the loop leave the announcement.
        await asyncio.sleep(0)
    freezer.tick(timedelta(seconds=4))

    result = await ask(hass, show.jeeves, "what do you think", device_id=show.device)

    assert speech(result) == ""
    assert show.speakers.messages == [JEEVES_LINE]
    await finish(hass)
    assert show.speakers.messages == ALL_LINES
    assert show.manager.state()["ended"] == "chat_done"


async def test_regression_chat_self_wake_from_the_last_line_is_ignored_after_the_grace(
    hass, show, freezer
):
    """A request from a wake on the last line may come after the grace has run out."""
    WakingSatellite(hass, show, wakes=(4,))
    await run_chat(hass, show)
    freezer.tick(timedelta(seconds=REPLY_GRACE + 1))

    result = await ask(hass, show.jeeves, "what do you think", device_id=show.device)

    assert speech(result) == ""
    assert nothing_else_answered(show)


async def test_regression_chat_self_wake_mark_expires(hass, show, request):
    """A request long after the wake is a person's again, by the usual chat rules."""
    satellite = WakingSatellite(hass, show, wakes=(1,), hold=1)
    await start(show)
    await asyncio.wait_for(satellite.playing.wait(), timeout=CHAT_RUN_TIMEOUT)
    request.getfixturevalue("freezer").tick(timedelta(seconds=SELF_WAKE_TTL + 1))

    result = await ask(hass, show.jeeves, "I think cats are better", device_id=show.device)

    assert speech(result) == ""
    assert [line["text"] for line in person_lines(show)] == ["I think cats are better"]
    satellite.release.set()
    await finish(hass)


async def test_regression_chat_self_wake_mark_is_cleared_by_a_person_waking_in_the_pause(
    hass, show, request
):
    """The woken line's request never came; a person then wakes the satellite to join in."""
    await wake_in_the_pause(hass, show, request, wakes=(1,))

    result = await ask(hass, show.jeeves, "I think cats are better", device_id=show.device)

    assert speech(result) == ""
    assert [line["text"] for line in person_lines(show)] == ["I think cats are better"]
    set_satellite(hass, "idle")
    await show.manager.async_stop()
    await finish(hass)
