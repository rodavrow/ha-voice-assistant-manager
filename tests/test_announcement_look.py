# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Showing which assistant speaks on devices that offer a character select."""

import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import announcement_look
from custom_components.voice_assistant_manager.announcement_look import (
    DEFAULT_OPTION,
    async_show_assistant,
    look_select,
    option_for,
    set_enabled,
    wake_slot,
)
from tests.router_helpers import add_router
from tests.router_helpers import router_entity_id as registered_router_id

SATELLITE = "assist_satellite.kitchen_satellite"
LOOK = "select.kitchen_kitchen_satellite_next_announcement_assistant"
OPTIONS = ["Default", "Assistant 1", "Assistant 2"]


def add_entity(
    hass: HomeAssistant,
    owner: MockConfigEntry,
    device_id: str,
    domain: str,
    object_id: str,
    state: str,
    attributes: dict[str, Any] | None = None,
    **kwargs: Any,
) -> str:
    entity = er.async_get(hass).async_get_or_create(
        domain,
        "esphome",
        f"{device_id}-{object_id}",
        suggested_object_id=object_id,
        device_id=device_id,
        config_entry=owner,
        **kwargs,
    )
    hass.states.async_set(entity.entity_id, state, attributes or {})
    return entity.entity_id


def add_device(
    hass: HomeAssistant,
    *,
    look: bool = True,
    options: list[str] | None = None,
    selects: tuple[tuple[str, str], ...] = (),
) -> str:
    """A satellite device, with the character select when `look`; returns its device id."""
    owner = MockConfigEntry(domain="esphome")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("esphome", "kitchen")}, name="Kitchen"
    )
    add_entity(hass, owner, device.id, "assist_satellite", "kitchen_satellite", "idle")
    if look:
        add_entity(
            hass,
            owner,
            device.id,
            "select",
            LOOK.removeprefix("select."),
            "Default",
            {"options": options if options is not None else OPTIONS},
        )
    for index, (key, option) in enumerate(selects):
        add_entity(
            hass,
            owner,
            device.id,
            "select",
            f"kitchen_pipeline_{index}",
            option,
            translation_key=key,
        )
    return device.id


@pytest.fixture
def chosen(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Records select.select_option calls."""
    calls: list[dict[str, Any]] = []

    async def select_option(call: ServiceCall) -> None:
        calls.append(dict(call.data))

    hass.services.async_register("select", "select_option", select_option)
    return calls


# Which option.


def test_a_slot_names_its_assistant_option():
    assert option_for(1, OPTIONS) == "Assistant 2"


def test_the_first_slot_is_assistant_1():
    assert option_for(0, OPTIONS) == "Assistant 1"


def test_no_slot_is_the_default():
    assert option_for(None, OPTIONS) == DEFAULT_OPTION


def test_a_slot_the_device_does_not_offer_is_the_default():
    assert option_for(2, OPTIONS) == DEFAULT_OPTION


# Finding the select.


async def test_the_select_is_found_on_the_satellites_device(hass):
    add_device(hass)

    assert look_select(hass, SATELLITE) == LOOK


async def test_a_device_without_the_select_has_none(hass):
    add_device(hass, look=False)

    assert look_select(hass, SATELLITE) is None


async def test_a_satellite_not_in_the_registry_has_none(hass):
    assert look_select(hass, "assist_satellite.nowhere") is None


async def test_a_disabled_select_is_not_used(hass):
    add_device(hass)
    er.async_get(hass).async_update_entity(LOOK, disabled_by=er.RegistryEntryDisabler.USER)

    assert look_select(hass, SATELLITE) is None


async def test_a_select_on_another_device_is_not_used(hass):
    add_device(hass, look=False)
    owner = MockConfigEntry(domain="esphome")
    owner.add_to_hass(hass)
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("esphome", "lounge")}
    )
    add_entity(
        hass, owner, other.id, "select", "lounge_next_announcement_assistant", "Default"
    )

    assert look_select(hass, SATELLITE) is None


# Setting it.


async def test_the_speaking_assistants_character_is_chosen(hass, chosen):
    add_device(hass)
    router = await add_router(hass)

    with patch.object(announcement_look, "wake_slot", lambda hass, satellite, entry: 1):
        await async_show_assistant(hass, SATELLITE, router)

    assert chosen == [{"entity_id": LOOK, "option": "Assistant 2"}]


async def test_no_assistant_chooses_the_default(hass, chosen):
    add_device(hass)

    await async_show_assistant(hass, SATELLITE, None)

    assert chosen == [{"entity_id": LOOK, "option": "Default"}]


async def test_a_device_without_the_select_is_left_alone(hass, chosen):
    add_device(hass, look=False)
    router = await add_router(hass)

    await async_show_assistant(hass, SATELLITE, router)

    assert chosen == []


async def test_switched_off_the_select_is_never_touched(hass, chosen):
    add_device(hass)
    router = await add_router(hass)
    set_enabled(hass, False)

    await async_show_assistant(hass, SATELLITE, router)

    assert chosen == []


async def test_it_is_on_unless_switched_off(hass):
    assert announcement_look.is_enabled(hass) is True


async def test_an_unavailable_select_is_left_alone(hass, chosen):
    add_device(hass)
    hass.states.async_set(LOOK, "unavailable", {"options": OPTIONS})

    await async_show_assistant(hass, SATELLITE, None)

    assert chosen == []


async def test_a_media_player_is_left_alone(hass, chosen):
    await async_show_assistant(hass, "media_player.kitchen_speaker", None)

    assert chosen == []


async def test_a_failing_select_never_raises(hass, caplog):
    add_device(hass)

    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("device went away")

    hass.services.async_register("select", "select_option", broken)

    with caplog.at_level(logging.WARNING):
        await async_show_assistant(hass, SATELLITE, None)

    assert "device went away" in caplog.text


async def test_a_missing_select_service_never_raises(hass):
    add_device(hass)

    await async_show_assistant(hass, SATELLITE, None)


# The wake-word slot, with real pipelines.


@pytest.fixture
def without_native_audio_libraries(hass: HomeAssistant):
    """Stand in for the compiled voice-activity libraries assist_pipeline imports."""
    Path(hass.config.config_dir).mkdir(parents=True, exist_ok=True)
    stubs = {"pymicro_vad": MagicMock(), "pyspeex_noise": MagicMock()}
    with patch.dict(sys.modules, stubs):
        yield


async def create_pipeline(ws, name: str, engine: str) -> None:
    await ws.send_json_auto_id(
        {
            "type": "assist_pipeline/pipeline/create",
            "name": name,
            "conversation_engine": engine,
            "conversation_language": "en",
            "language": "en",
            "stt_engine": None,
            "stt_language": None,
            "tts_engine": None,
            "tts_language": None,
            "tts_voice": None,
            "wake_word_entity": None,
            "wake_word_id": None,
            "prefer_local_intents": True,
        }
    )
    response = await ws.receive_json()
    assert response["success"], response


@pytest.fixture
async def two_assistants(without_native_audio_libraries, hass, hass_ws_client):
    jeeves = await add_router(hass, title="Jeeves")
    ada = await add_router(hass, title="Ada")
    assert await async_setup_component(hass, "assist_pipeline", {})
    await hass.async_block_till_done()
    ws = await hass_ws_client(hass)
    await create_pipeline(ws, "Jeeves", registered_router_id(hass, jeeves))
    await create_pipeline(ws, "Ada", registered_router_id(hass, ada))
    return jeeves, ada


async def test_the_second_wake_word_is_slot_1(hass, two_assistants):
    jeeves, ada = two_assistants
    add_device(hass, selects=(("pipeline", "Jeeves"), ("pipeline_n", "Ada")))

    assert (wake_slot(hass, SATELLITE, jeeves), wake_slot(hass, SATELLITE, ada)) == (0, 1)


async def test_an_assistant_on_no_wake_word_has_no_slot(hass, two_assistants):
    jeeves, ada = two_assistants
    add_device(hass, selects=(("pipeline", "Jeeves"),))

    assert wake_slot(hass, SATELLITE, ada) is None
