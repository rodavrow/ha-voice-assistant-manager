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

    router = await add_router(hass, options_with(advanced={"wait_seconds": 3}))
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


def test_the_follow_up_window_is_short():
    """A separate wake word a little later is not a follow-up: it needs its own match."""
    assert FOLLOW_UP_WINDOW <= timedelta(seconds=20)
