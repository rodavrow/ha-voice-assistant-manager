# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A request a wake word started goes to that wake word's assistant, whatever conversation
Home Assistant kept for the satellite."""

import logging
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import wake_handoff

from .router_helpers import FakeAgents, add_router, router_entity_id
from .test_setup_guide import create_pipeline, without_native_audio_libraries  # noqa: F401

MAC = "aa:bb:cc:dd:ee:ff"
WAKE_SENSOR = "sensor.living_room_last_wake_word"
FIRST, SECOND = "Hey Jeeves", "Hey Ada"


def add(hass, owner, device_id, domain, unique_id, state, object_id) -> str:
    entity = er.async_get(hass).async_get_or_create(
        domain, "esphome", unique_id, device_id=device_id, config_entry=owner,
        suggested_object_id=object_id,
    )
    hass.states.async_set(entity.entity_id, state)
    return entity.entity_id


@pytest.fixture
async def home(without_native_audio_libraries, hass: HomeAssistant, hass_ws_client):  # noqa: F811
    """A satellite with two wake words: the first for Jeeves, the second for Ada."""
    jeeves = await add_router(hass, title="Jeeves")
    ada = await add_router(hass, title="Ada")
    assert await async_setup_component(hass, "assist_pipeline", {})
    await hass.async_block_till_done()
    ws = await hass_ws_client(hass)
    await create_pipeline(ws, "Jeeves pipeline", router_entity_id(hass, jeeves))
    await create_pipeline(ws, "Ada pipeline", router_entity_id(hass, ada))
    await create_pipeline(ws, "Plain pipeline", "conversation.home_assistant")
    owner = MockConfigEntry(domain="esphome")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("esphome", MAC)}, name="Living room"
    )
    selects = {
        f"{MAC}-wake_word": (FIRST, "living_room_wake_word"),
        f"{MAC}-wake_word_2": (SECOND, "living_room_wake_word_2"),
        f"{MAC}-pipeline": ("Jeeves pipeline", "living_room_assistant"),
        f"{MAC}-pipeline_2": ("Ada pipeline", "living_room_assistant_2"),
    }
    ids = {
        unique_id: add(hass, owner, device.id, "select", unique_id, state, object_id)
        for unique_id, (state, object_id) in selects.items()
    }
    add(
        hass, owner, device.id, "sensor", f"{MAC}/0/text_sensor/Last wake word", "",
        "living_room_last_wake_word",
    )
    agents = FakeAgents()
    agents.replies[router_entity_id(hass, ada)] = "I am Ada."
    agents.replies[router_entity_id(hass, jeeves)] = "I am Jeeves."
    with agents.patched():
        yield SimpleNamespace(
            jeeves=router_entity_id(hass, jeeves),
            ada=router_entity_id(hass, ada),
            device=device.id,
            selects=ids,
            agents=agents,
        )


def woke(hass: HomeAssistant, phrase: str) -> None:
    hass.states.async_set(WAKE_SENSOR, phrase, force_update=True)


def request(home, conversation_id: str | None = "kept", device_id: Any = ...) -> Any:
    return SimpleNamespace(
        text="Who are you?",
        context=Context(),
        conversation_id=conversation_id,
        device_id=home.device if device_id is ... else device_id,
        satellite_id="assist_satellite.living_room",
        language="en",
        extra_system_prompt=None,
    )


async def hand(hass, own: str, user_input) -> Any:
    return await wake_handoff.async_hand_off(hass, own, user_input)


# Handing over


async def test_regression_wake_word_of_the_other_assistant_is_answered_by_it(
    hass, home, caplog
):
    """Seen: one assistant asked something, and the other's wake word minutes later
    reached the first, as Home Assistant kept its conversation for the satellite."""
    woke(hass, SECOND)

    with caplog.at_level(logging.INFO):
        result = await hand(hass, home.jeeves, request(home))

    assert result.response.speech["plain"]["speech"] == "I am Ada."
    (call,) = home.agents.calls_to(home.ada)
    # In a conversation of its own, not the one the other assistant left.
    assert call["conversation_id"] is None
    assert call["device_id"] == home.device
    # The satellite goes on in its own conversation.
    assert result.conversation_id == "kept"
    assert any("handing it over" in r.getMessage() for r in caplog.records)


async def test_its_follow_ups_go_there_too(hass, home):
    woke(hass, SECOND)
    await hand(hass, home.jeeves, request(home))

    # A follow-up carries no wake word, and Home Assistant sends it to the same assistant.
    result = await hand(hass, home.jeeves, request(home))

    assert result.response.speech["plain"]["speech"] == "I am Ada."
    first, second = home.agents.calls_to(home.ada)
    # In the conversation the assistant answered the first time in.
    assert first["conversation_id"] is None
    assert second["conversation_id"] == "fake"


async def test_the_assistant_handed_to_does_not_hand_it_back(hass, home):
    woke(hass, SECOND)
    await hand(hass, home.jeeves, request(home))

    assert await hand(hass, home.ada, request(home, conversation_id=None)) is None


async def test_a_wake_word_of_this_assistant_is_answered_here(hass, home):
    woke(hass, FIRST)

    assert await hand(hass, home.jeeves, request(home)) is None
    assert home.agents.calls == []


async def test_a_new_wake_word_for_this_assistant_ends_a_hand_over(hass, home):
    woke(hass, SECOND)
    await hand(hass, home.jeeves, request(home))

    woke(hass, FIRST)
    assert await hand(hass, home.jeeves, request(home)) is None
    # And its follow-up stays here as well.
    assert await hand(hass, home.jeeves, request(home)) is None


async def test_without_a_new_wake_word_nothing_changes(hass, home):
    woke(hass, SECOND)
    await hand(hass, home.ada, request(home))

    # No wake word since that request: a listen after a chat line, say.
    assert await hand(hass, home.jeeves, request(home, conversation_id="other")) is None


async def test_an_old_wake_word_does_not_count(hass, home, freezer):
    woke(hass, SECOND)
    freezer.tick(wake_handoff.FRESH + timedelta(seconds=1))

    assert await hand(hass, home.jeeves, request(home)) is None


async def test_a_hand_over_is_forgotten_with_the_conversation(hass, home, freezer):
    woke(hass, SECOND)
    await hand(hass, home.jeeves, request(home))

    freezer.tick(wake_handoff.LIFETIME + timedelta(seconds=1))
    assert await hand(hass, home.jeeves, request(home)) is None


async def test_a_wake_word_for_an_agent_of_another_integration_is_left_alone(hass, home):
    hass.states.async_set(home.selects[f"{MAC}-pipeline_2"], "Plain pipeline")
    woke(hass, SECOND)

    assert await hand(hass, home.jeeves, request(home)) is None


async def test_a_request_without_a_device_is_left_alone(hass, home):
    woke(hass, SECOND)

    assert await hand(hass, home.jeeves, request(home, device_id=None)) is None


async def test_a_phrase_on_no_wake_word_is_left_alone(hass, home):
    woke(hass, "Hey Nobody")

    assert await hand(hass, home.jeeves, request(home)) is None


async def test_the_preferred_pipeline_is_followed(hass, home):
    from homeassistant.components import assist_pipeline  # noqa: PLC0415

    hass.states.async_set(home.selects[f"{MAC}-pipeline_2"], "preferred")
    preferred = assist_pipeline.async_get_pipeline(hass)
    woke(hass, SECOND)

    agent = wake_handoff.woken_agent(hass, home.device, SECOND)
    assert agent == preferred.conversation_engine


def test_the_woken_agent_is_the_one_on_that_wake_words_pipeline(hass, home):
    assert wake_handoff.woken_agent(hass, home.device, SECOND) == home.ada
    assert wake_handoff.woken_agent(hass, home.device, FIRST) == home.jeeves


# Through the assistant


async def test_the_assistant_hands_the_request_over_before_anything_else(hass, home):
    woke(hass, SECOND)
    agent = conversation.async_get_agent(hass, home.jeeves)

    result = await agent.async_process(
        conversation.ConversationInput(
            text="Who are you?",
            context=Context(),
            conversation_id="kept",
            device_id=home.device,
            satellite_id=None,
            language="en",
            agent_id=home.jeeves,
        )
    )

    assert result.response.speech["plain"]["speech"] == "I am Ada."
    assert [call["agent_id"] for call in home.agents.calls] == [home.ada]
