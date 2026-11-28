# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for S1 and S2 of the 2026-11-27 review: whose voice a turn is.

S1: someone who woke a satellite right after another person's turn could be taken for
that person. The helper still held the earlier voice match, and a follow-up kept the
earlier person for any turn within a minute.
S2: a voice match was believed however old it was, so a tracking automation that stopped
writing "pending" made every voice the last person matched.

Now a voice match counts only when it was written for this turn: after the satellite
started listening (or reported its wake word), and not long ago. And the earlier person is
kept on a turn without a match only when the assistant's last reply asked a question.
"""

import asyncio
import dataclasses
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.voice_assistant_manager.conversation import UNSURE_LINE
from custom_components.voice_assistant_manager.speaker import (
    Remembered,
    follow_up_role,
    verdict_is_current,
)

from .router_helpers import (
    MAIN_AGENT,
    FakeAgents,
    add_router,
    options_with,
    router_entity_id,
)
from .test_speaker_follow_up import (
    CONVERSATION,
    LATE,
    add_satellite,
    add_wake_sensor,
    asked_a_question,
    match,
    speaker,
    voice_turn,
    woke,
)

T0 = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
WAITING = {"match_within": 1}


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def helper_since(hass: HomeAssistant, device_id: str, value: str, seconds_ago: float) -> None:
    hass.states.async_set(
        f"input_text.voice_speaker_{device_id}", value, timestamp=time.time() - seconds_ago
    )


def add_satellite_entity(hass: HomeAssistant, device_id: str) -> str:
    """The device's assist_satellite entity, whose state says when it listens."""
    entity = er.async_get(hass).async_get_or_create(
        "assist_satellite", "test", f"satellite-{device_id}", device_id=device_id
    )
    hass.states.async_set(entity.entity_id, "idle")
    return entity.entity_id


async def listens(hass: HomeAssistant, satellite: str) -> None:
    """A new turn starts: the satellite heard its wake word and listens."""
    hass.states.async_set(satellite, "idle")
    hass.states.async_set(satellite, "listening")
    await hass.async_block_till_done()


async def said(hass: HomeAssistant, entry, device_id: str, agents: FakeAgents) -> str:
    entity_id = router_entity_id(hass, entry)
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(entity_id)
    with agents.patched():
        result = await entity.async_process(
            conversation.ConversationInput(
                text="What is on today?", context=Context(), conversation_id=CONVERSATION,
                device_id=device_id, satellite_id=None, language="en", agent_id=entity_id,
            )
        )
    return str((result.response.speech.get("plain") or {}).get("speech") or "")


# S1: the earlier person is kept only after a question


ALEX = Remembered(role="owner", device_id="kitchen", decided_by=T0, at=T0)


def test_a_turn_without_a_match_does_not_keep_the_person_when_nothing_was_asked():
    assert follow_up_role(ALEX, "kitchen", "pending", at(5), at(8), None) is None


def test_a_turn_without_a_match_keeps_the_person_after_a_question():
    asked = dataclasses.replace(ALEX, asked=True)

    assert follow_up_role(asked, "kitchen", "pending", at(5), at(8), None) == "owner"


@pytest.mark.parametrize(("window", "kept"), [(0, None), (5, None), (10, "owner")])
def test_how_long_a_question_may_be_answered_is_a_setting_and_zero_switches_it_off(
    window, kept
):
    asked = dataclasses.replace(ALEX, asked=True)

    role = follow_up_role(
        asked, "kitchen", "pending", at(5), at(8), None, timedelta(seconds=window)
    )

    assert role == kept


async def test_regression_s1_the_next_turn_is_not_the_earlier_person_without_a_match(hass):
    router = await add_router(hass)
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    assert await speaker(hass, router, voice_turn(kitchen)) == "owner"

    match(hass, kitchen, "pending")  # a new wake word; its voice match never arrives

    assert await speaker(hass, router, voice_turn(kitchen)) == "stranger"


async def test_the_answer_to_a_question_is_from_the_person_who_was_asked(hass):
    router = await add_router(hass)
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))
    asked_a_question(hass, router)

    match(hass, kitchen, "pending")

    assert await speaker(hass, router, voice_turn(kitchen)) == "owner"


async def test_one_question_keeps_the_person_for_one_answer(hass):
    router = await add_router(hass)
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))
    asked_a_question(hass, router)
    match(hass, kitchen, "pending")
    await speaker(hass, router, voice_turn(kitchen))

    assert await speaker(hass, router, voice_turn(kitchen)) == "stranger"


@pytest.mark.parametrize(("asks", "second"), [(True, "Noted."), (False, UNSURE_LINE)])
async def test_the_router_knows_from_the_main_agents_reply_whether_it_asked(
    hass, asks, second
):
    entry = await add_router(hass)
    kitchen = add_satellite(hass)
    agents = FakeAgents()
    answer = agents.converse

    async def converse(*args, **kwargs):
        result = await answer(*args, **kwargs)
        if kwargs.get("agent_id") == MAIN_AGENT:
            result.continue_conversation = asks
        return result

    agents.converse = converse  # type: ignore[method-assign]
    match(hass, kitchen, "owner")
    assert await said(hass, entry, kitchen, agents) == "Noted."

    match(hass, kitchen, "pending")

    assert await said(hass, entry, kitchen, agents) == second


async def test_the_setting_zero_never_keeps_the_person(hass):
    router = await add_router(hass, options_with(advanced={"follow_up_window": 0}))
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))
    asked_a_question(hass, router)

    match(hass, kitchen, "pending")

    assert await speaker(hass, router, voice_turn(kitchen)) == "stranger"


# S1: a wake word right after a reply, when the automation writes no "pending"


async def test_regression_s1_a_wake_right_after_a_reply_waits_for_its_own_match(hass):
    """The helper still says the earlier person. The satellite started listening after
    that match was written, so it is not this turn's: the new one is waited for."""
    router = await add_router(hass, options_with(advanced=WAITING))
    kitchen = add_satellite(hass)
    satellite = add_satellite_entity(hass, kitchen)
    await listens(hass, satellite)
    match(hass, kitchen, "owner")
    assert await speaker(hass, router, voice_turn(kitchen)) == "owner"

    await listens(hass, satellite)  # someone else wakes it; no "pending" is written

    async def late_match() -> None:
        await asyncio.sleep(LATE)
        match(hass, kitchen, "stranger")

    task = hass.async_create_task(late_match())
    role = await speaker(hass, router, voice_turn(kitchen))
    await task

    assert role == "stranger"


async def test_regression_s1_a_wake_right_after_a_reply_without_a_match_is_refused(hass):
    entry = await add_router(hass)
    kitchen = add_satellite(hass)
    satellite = add_satellite_entity(hass, kitchen)
    agents = FakeAgents()
    await listens(hass, satellite)
    match(hass, kitchen, "owner")
    assert await said(hass, entry, kitchen, agents) == "Noted."

    await listens(hass, satellite)

    assert await said(hass, entry, kitchen, agents) == UNSURE_LINE
    assert len(agents.calls_to(MAIN_AGENT)) == 1


async def test_a_match_written_after_listening_started_is_this_turns(hass):
    router = await add_router(hass)
    kitchen = add_satellite(hass)
    satellite = add_satellite_entity(hass, kitchen)
    await listens(hass, satellite)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))

    await listens(hass, satellite)
    match(hass, kitchen, "pending")
    match(hass, kitchen, "partner")

    assert await speaker(hass, router, voice_turn(kitchen)) == "wife"


async def test_the_same_person_matched_again_is_that_person(hass):
    """The second match changes nothing in the helper but when it was reported."""
    router = await add_router(hass)
    kitchen = add_satellite(hass)
    satellite = add_satellite_entity(hass, kitchen)
    await listens(hass, satellite)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))

    await listens(hass, satellite)
    match(hass, kitchen, "owner")

    assert await speaker(hass, router, voice_turn(kitchen)) == "owner"


async def test_regression_s1_a_match_older_than_the_reported_wake_word_is_not_this_turns(hass):
    router = await add_router(hass)
    kitchen = add_satellite(hass)
    sensor = add_wake_sensor(hass, kitchen)
    match(hass, kitchen, "owner")

    woke(hass, sensor)
    hass.states.async_set(sensor, "hey_jeeves")  # a wake word reported after the match

    assert await speaker(hass, router, voice_turn(kitchen, "another conversation")) == "stranger"


# S2: an old voice match is not believed


def test_a_match_is_current_when_it_is_recent_and_no_older_than_the_turn():
    age = timedelta(seconds=36)

    assert verdict_is_current("owner", at(10), None, at(40), age)
    assert verdict_is_current("stranger", at(10), at(10), at(40), age)
    assert not verdict_is_current("owner", at(10), None, at(47), age)
    assert not verdict_is_current("owner", at(10), at(11), at(12), age)
    assert not verdict_is_current("pending", at(10), None, at(11), age)
    assert not verdict_is_current(None, at(10), None, at(11), age)


async def test_regression_s2_a_match_from_long_ago_is_not_believed(hass, caplog):
    """The tracking automation stopped writing "pending": the helper keeps the last match."""
    entry = await add_router(hass)
    kitchen = add_satellite(hass)
    helper_since(hass, kitchen, "owner", 600)
    agents = FakeAgents()

    first = await said(hass, entry, kitchen, agents)
    second = await said(hass, entry, kitchen, agents)

    assert (first, second) == (UNSURE_LINE, UNSURE_LINE)
    assert agents.calls_to(MAIN_AGENT) == []
    assert caplog.text.count("No voice match arrived") == 1


async def test_a_match_from_a_long_request_is_still_believed(hass):
    """The match lands about five seconds after the wake word; the request may go on."""
    entry = await add_router(hass)
    kitchen = add_satellite(hass)
    helper_since(hass, kitchen, "owner", 20)

    assert await said(hass, entry, kitchen, FakeAgents()) == "Noted."


# Found by the review of these fixes


async def test_regression_s1_the_time_to_answer_runs_from_the_reply_not_from_the_request(
    hass, freezer
):
    """A slow agent and a long spoken question used up the time the person had to answer."""
    router = await add_router(hass)
    kitchen = add_satellite(hass)
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(router_entity_id(hass, router))
    match(hass, kitchen, "owner")
    turn = voice_turn(kitchen)
    await speaker(hass, router, turn)
    freezer.tick(40)
    entity._voices.note_reply(turn, SimpleNamespace(continue_conversation=True))

    freezer.tick(40)
    match(hass, kitchen, "pending")

    assert await speaker(hass, router, voice_turn(kitchen)) == "owner"


async def test_regression_s1_a_pending_left_by_an_earlier_turn_does_not_end_the_wait(hass):
    """The earlier turn got no match, so the helper still says pending from then. The
    automation writes no new pending for a wake word that follows closely, and this turn's
    match was not waited for: the person was refused a moment before it arrived."""
    router = await add_router(hass, options_with(advanced=WAITING))
    kitchen = add_satellite(hass)
    satellite = add_satellite_entity(hass, kitchen)
    helper_since(hass, kitchen, "pending", 5)
    await listens(hass, satellite)

    async def late_match() -> None:
        await asyncio.sleep(LATE)
        match(hass, kitchen, "owner")

    task = hass.async_create_task(late_match())
    role = await speaker(hass, router, voice_turn(kitchen))
    await task

    assert role == "owner"


async def test_regression_the_answer_to_a_question_does_not_wait_for_a_match(hass):
    """Seen in the house: a satellite listening for the answer to a question makes no voice
    match, but the tracking automation had written "pending" when it started to listen. The
    router then waited out the rest of the six seconds a match may take before it asked
    the main agent, on every short answer."""
    router = await add_router(hass, options_with(advanced={"match_within": 6}))
    kitchen = add_satellite(hass)
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(router_entity_id(hass, router))
    match(hass, kitchen, "owner")
    turn = voice_turn(kitchen)
    await speaker(hass, router, turn)
    entity._voices.note_reply(turn, SimpleNamespace(continue_conversation=True))
    match(hass, kitchen, "pending")

    began = time.monotonic()
    who = await speaker(hass, router, voice_turn(kitchen))

    assert who == "owner"
    assert time.monotonic() - began < 1
