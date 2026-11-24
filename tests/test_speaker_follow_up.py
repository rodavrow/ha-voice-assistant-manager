# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A follow-up in a voice conversation keeps its speaker, whatever the satellite's timing."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager.speaker import FOLLOW_UP_WINDOW

from .router_helpers import MAIN_AGENT, FakeAgents, add_router, router_entity_id

CONVERSATION = "01CONVERSATIONKITCHEN0000"


def add_satellite(hass: HomeAssistant, name: str = "Kitchen satellite") -> str:
    owner = MockConfigEntry(domain="test")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("test", name)}, name=name
    )
    return device.id


def match(hass: HomeAssistant, device_id: str, value: str) -> None:
    """What the tracking automation writes: pending on a wake, then the voice match."""
    hass.states.async_set(f"input_text.voice_speaker_{device_id}", value)


def voice_turn(
    device_id: str | None, conversation_id: str | None = CONVERSATION, text: str = "hi"
) -> conversation.ConversationInput:
    return conversation.ConversationInput(
        text=text,
        context=Context(),
        conversation_id=conversation_id,
        device_id=device_id,
        satellite_id=None,
        language="en",
        agent_id=None,
    )


async def speaker(hass, entry, turn) -> str:
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(router_entity_id(hass, entry))
    role, _ = await entity._who_is_speaking(turn, entry.options)
    return role


@pytest.fixture
async def router(hass):
    return await add_router(hass)


async def test_a_follow_up_without_a_new_match_keeps_the_speaker(hass, router):
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    assert await speaker(hass, router, voice_turn(kitchen)) == "owner"

    match(hass, kitchen, "pending")  # the listen came too late for the 2-second rule

    assert await speaker(hass, router, voice_turn(kitchen)) == "owner"


async def test_a_new_match_for_the_other_person_switches(hass, router):
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))

    match(hass, kitchen, "pending")
    match(hass, kitchen, "partner")

    assert await speaker(hass, router, voice_turn(kitchen)) == "wife"


async def test_a_new_stranger_match_refuses_and_is_not_forgotten(hass, router):
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))
    match(hass, kitchen, "stranger")
    assert await speaker(hass, router, voice_turn(kitchen)) == "stranger"

    match(hass, kitchen, "pending")

    assert await speaker(hass, router, voice_turn(kitchen)) == "stranger"


async def test_a_conversation_that_began_with_a_stranger_does_not_become_a_person(
    hass, router
):
    kitchen = add_satellite(hass)
    match(hass, kitchen, "stranger")
    await speaker(hass, router, voice_turn(kitchen))

    match(hass, kitchen, "pending")

    assert await speaker(hass, router, voice_turn(kitchen)) == "stranger"


async def test_another_satellite_in_the_same_conversation_needs_its_own_match(hass, router):
    kitchen = add_satellite(hass)
    office = add_satellite(hass, "Office satellite")
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))

    match(hass, office, "pending")

    assert await speaker(hass, router, voice_turn(office)) == "stranger"


async def test_another_conversation_needs_its_own_match(hass, router):
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))

    match(hass, kitchen, "pending")

    assert await speaker(hass, router, voice_turn(kitchen, "01OTHERCONVERSATION000000")) == (
        "stranger"
    )


async def test_a_turn_after_the_follow_up_window_needs_its_own_match(hass, router, freezer):
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))

    freezer.tick(FOLLOW_UP_WINDOW + timedelta(seconds=1))
    match(hass, kitchen, "pending")

    assert await speaker(hass, router, voice_turn(kitchen)) == "stranger"


async def test_each_follow_up_extends_the_window(hass, router, freezer):
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))
    for _ in range(3):
        freezer.tick(FOLLOW_UP_WINDOW - timedelta(seconds=5))
        match(hass, kitchen, "pending")
        assert await speaker(hass, router, voice_turn(kitchen)) == "owner"


async def test_a_turn_without_a_conversation_is_never_carried(hass, router):
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen, None))

    match(hass, kitchen, "pending")

    assert await speaker(hass, router, voice_turn(kitchen, None)) == "stranger"


async def test_the_follow_up_is_answered_with_the_speakers_persona(hass, router):
    """End to end: the main agent gets the follow-up labelled with the person's name."""
    kitchen = add_satellite(hass)
    agents = FakeAgents()
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(router_entity_id(hass, router))
    with patch(
        "custom_components.voice_assistant_manager.conversation.conversation.async_converse",
        agents.converse,
    ):
        match(hass, kitchen, "owner")
        await entity.async_process(voice_turn(kitchen, text="hello"))
        match(hass, kitchen, "pending")
        await entity.async_process(voice_turn(kitchen, text="and tomorrow?"))

    main_calls = [call for call in agents.calls if call.get("agent_id") == MAIN_AGENT]
    assert main_calls[-1]["text"] == "Alex: and tomorrow?"


async def test_a_late_stranger_match_on_a_follow_up_is_still_caught(hass):
    """A follow-up waits the full "wait for the voice match" time: a stranger whose match
    lands after a second is refused, not given the conversation's person."""
    import asyncio

    from .router_helpers import options_with

    router = await add_router(hass, options_with(advanced={"match_within": 6}))
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))
    match(hass, kitchen, "pending")

    async def late_match() -> None:
        await asyncio.sleep(1.5)
        match(hass, kitchen, "stranger")

    task = hass.async_create_task(late_match())
    role = await speaker(hass, router, voice_turn(kitchen))
    await task

    assert role == "stranger"


def test_without_wake_reports_the_window_is_a_minute():
    assert timedelta(seconds=60) == FOLLOW_UP_WINDOW


# Satellites that report each new turn's wake word, as LANovo and EchoLocal do


def add_wake_sensor(hass: HomeAssistant, device_id: str) -> str:
    """The satellite's "Last wake word" text sensor, as Home Assistant registers it."""
    from homeassistant.helpers import entity_registry as er

    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        "esphome",
        "aa:bb:cc:dd:ee:ff/0/text_sensor/Last wake word",
        suggested_object_id="kitchen_last_wake_word",
        device_id=device_id,
    )
    return entity.entity_id


def woke(hass: HomeAssistant, sensor: str) -> None:
    """A new turn: the satellite reports its wake word again, even when it is the same."""
    hass.states.async_set(sensor, "hey_ada")


async def test_a_follow_up_with_the_wake_word_unchanged_keeps_the_person(hass, router):
    kitchen = add_satellite(hass)
    sensor = add_wake_sensor(hass, kitchen)
    woke(hass, sensor)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))

    match(hass, kitchen, "pending")

    assert await speaker(hass, router, voice_turn(kitchen)) == "owner"


async def test_a_new_wake_word_needs_its_own_match(hass, router):
    kitchen = add_satellite(hass)
    sensor = add_wake_sensor(hass, kitchen)
    woke(hass, sensor)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))

    woke(hass, sensor)  # someone woke it again; no voice match arrived
    match(hass, kitchen, "pending")

    assert await speaker(hass, router, voice_turn(kitchen)) == "stranger"


async def test_a_new_wake_word_with_the_other_person_switches(hass, router):
    kitchen = add_satellite(hass)
    sensor = add_wake_sensor(hass, kitchen)
    woke(hass, sensor)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))

    woke(hass, sensor)
    match(hass, kitchen, "partner")

    assert await speaker(hass, router, voice_turn(kitchen)) == "wife"


async def test_regression_a_repeated_wake_word_still_waits_for_the_match(hass):
    """Home Assistant drops a text sensor update with an unchanged value, so a second
    "Hey Ada" does not show in "Last wake word". 0.5.2b3 took that for a follow-up, did not
    wait, and answered a stranger whose voice match landed a moment later as the person."""
    import asyncio

    from .router_helpers import options_with

    router = await add_router(hass, options_with(advanced={"match_within": 6}))
    kitchen = add_satellite(hass)
    sensor = add_wake_sensor(hass, kitchen)
    woke(hass, sensor)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))
    match(hass, kitchen, "pending")  # someone woke it again with the same phrase

    async def late_match() -> None:
        await asyncio.sleep(1.3)
        match(hass, kitchen, "stranger")

    task = hass.async_create_task(late_match())
    role = await speaker(hass, router, voice_turn(kitchen))
    await task

    assert role == "stranger"



# The wait is anchored to when listening started: the satellite's match lands a fixed time
# after the wake word (LANovo: 4.6-5.5 s), however long the person speaks.


def match_since(hass: HomeAssistant, device_id: str, value: str, seconds_ago: float) -> None:
    """The helper as the tracking automation left it `seconds_ago` (when listening began)."""
    import time

    hass.states.async_set(
        f"input_text.voice_speaker_{device_id}", value, timestamp=time.time() - seconds_ago
    )


async def test_after_the_match_deadline_there_is_no_wait(hass):
    import time

    from .router_helpers import options_with

    router = await add_router(hass, options_with(advanced={"match_within": 6}))
    kitchen = add_satellite(hass)
    match_since(hass, kitchen, "pending", 7)  # a long request: listening began 7 s ago

    started = time.monotonic()
    role = await speaker(hass, router, voice_turn(kitchen))

    assert role == "stranger"
    assert time.monotonic() - started < 0.5


async def test_a_short_follow_up_waits_only_for_what_is_left_of_the_deadline(hass):
    import time

    from .router_helpers import options_with

    router = await add_router(hass, options_with(advanced={"match_within": 6}))
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))
    match_since(hass, kitchen, "pending", 5.5)  # the follow-up began 5.5 s ago

    started = time.monotonic()
    role = await speaker(hass, router, voice_turn(kitchen))

    assert role == "owner"
    assert time.monotonic() - started < 1.2


async def test_a_match_landing_before_the_deadline_still_decides(hass):
    """A stranger's match lands about 5 s after the wake word, after they stopped speaking."""
    import asyncio

    from .router_helpers import options_with

    router = await add_router(hass, options_with(advanced={"match_within": 6}))
    kitchen = add_satellite(hass)
    match(hass, kitchen, "owner")
    await speaker(hass, router, voice_turn(kitchen))
    match_since(hass, kitchen, "pending", 3.5)  # a short request, 3.5 s after the wake word

    async def late_match() -> None:
        await asyncio.sleep(1.3)
        match(hass, kitchen, "stranger")

    task = hass.async_create_task(late_match())
    role = await speaker(hass, router, voice_turn(kitchen))
    await task

    assert role == "stranger"
