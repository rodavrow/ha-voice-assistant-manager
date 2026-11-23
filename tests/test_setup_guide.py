# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Finding satellites, their speaker sensors and their pipelines for the setup guide."""

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import setup_guide
from tests.router_helpers import add_router
from tests.router_helpers import router_entity_id as registered_router_id

SATELLITE = "assist_satellite.kitchen_satellite"


async def create_pipeline(ws, name: str, engine: str) -> str:
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
    return str(response["result"]["id"])


def add_entity(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    device_id: str,
    domain: str,
    object_id: str,
    state: str,
    **kwargs: Any,
) -> str:
    entity = er.async_get(hass).async_get_or_create(
        domain,
        "test",
        f"{device_id}-{object_id}",
        suggested_object_id=object_id,
        device_id=device_id,
        config_entry=entry,
        **kwargs,
    )
    hass.states.async_set(entity.entity_id, state)
    return entity.entity_id


@pytest.fixture
async def router(hass: HomeAssistant) -> MockConfigEntry:
    return await add_router(hass)


@pytest.fixture
def without_native_audio_libraries(hass: HomeAssistant):
    """Stand in for the two compiled voice-activity libraries assist_pipeline imports.

    They have no wheel for this Python and the pipelines here never process audio. Also makes
    the config folder, which the text-to-speech cache needs to exist when a test runs alone.
    """
    Path(hass.config.config_dir).mkdir(parents=True, exist_ok=True)
    stubs = {"pymicro_vad": MagicMock(), "pyspeex_noise": MagicMock()}
    with patch.dict(sys.modules, stubs):
        yield


@pytest.fixture
async def pipelines(
    without_native_audio_libraries,
    hass: HomeAssistant,
    router: MockConfigEntry,
    hass_ws_client,
) -> Any:
    assert await async_setup_component(hass, "assist_pipeline", {})
    await hass.async_block_till_done()
    ws = await hass_ws_client(hass)
    router_id = registered_router_id(hass, router)
    return {
        "ws": ws,
        "router": await create_pipeline(ws, "Jeeves", router_id),
        "other": await create_pipeline(ws, "Plain", "conversation.home_assistant"),
    }


def add_satellite(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    sensors: bool = True,
    selects: tuple[tuple[str, str], ...] = (),
    name: str = "Kitchen satellite",
) -> str:
    slug = name.replace(" ", "_").lower()
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", name)}, name=name
    )
    add_entity(hass, entry, device.id, "assist_satellite", slug, "idle")
    if sensors:
        add_entity(hass, entry, device.id, "sensor", "kitchen_last_speaker", "Alex")
        add_entity(hass, entry, device.id, "sensor", "kitchen_last_speaker_score", "0.9")
    for index, (key, option) in enumerate(selects):
        add_entity(
            hass,
            entry,
            device.id,
            "select",
            f"kitchen_pipeline_{index}",
            option,
            translation_key=key,
        )
    return device.id


def only(hass: HomeAssistant, entry: MockConfigEntry) -> setup_guide.Satellite:
    (satellite,) = setup_guide.find_satellites(hass, entry)
    return satellite


async def test_a_satellite_is_found_with_its_helper_and_sensors(hass, router):
    device_id = add_satellite(hass, router)
    hass.states.async_set(f"input_text.voice_speaker_{device_id}", "")

    satellite = only(hass, router)

    assert satellite["entity_id"] == SATELLITE
    assert satellite["device_id"] == device_id
    assert satellite["name"] == "Kitchen satellite"
    assert satellite["helper"] == f"input_text.voice_speaker_{device_id}"
    assert satellite["helper_exists"] is True
    assert satellite["speaker_sensor"] == "sensor.kitchen_last_speaker"
    assert satellite["score_sensor"] == "sensor.kitchen_last_speaker_score"
    assert satellite["tracked"] is False


async def test_a_missing_helper_is_reported(hass, router):
    add_satellite(hass, router)

    assert only(hass, router)["helper_exists"] is False


async def test_a_satellite_without_speaker_sensors_has_none(hass, router):
    add_satellite(hass, router, sensors=False)

    satellite = only(hass, router)

    assert satellite["speaker_sensor"] is None
    assert satellite["score_sensor"] is None


async def _automation_using(hass: HomeAssistant, sensor: str) -> None:
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "trigger": {"platform": "state", "entity_id": sensor},
                "action": {"event": "noop"},
            }
        },
    )
    await hass.async_block_till_done()


async def test_a_satellite_is_tracked_when_an_automation_uses_its_score_sensor(hass, router):
    add_satellite(hass, router)
    await _automation_using(hass, "sensor.kitchen_last_speaker_score")

    assert only(hass, router)["tracked"] is True


async def test_a_satellite_is_tracked_when_an_automation_uses_its_speaker_sensor(hass, router):
    add_satellite(hass, router)
    await _automation_using(hass, "sensor.kitchen_last_speaker")

    assert only(hass, router)["tracked"] is True


async def test_wake_words_name_their_pipeline_and_whether_it_uses_this_router(
    hass, router, pipelines
):
    add_satellite(hass, router, selects=(("pipeline", "Jeeves"), ("pipeline_n", "Plain")))

    first, second = only(hass, router)["wake_words"]

    assert first == {
        "select": "select.kitchen_pipeline_0",
        "pipeline_id": pipelines["router"],
        "pipeline_name": "Jeeves",
        "preferred": False,
        "uses_router": True,
        "prefer_local": True,
    }
    assert second["pipeline_id"] == pipelines["other"]
    assert second["uses_router"] is False


async def test_wake_words_are_ordered_pipeline_first(hass, router, pipelines):
    add_satellite(hass, router, selects=(("pipeline_n", "Plain"), ("pipeline", "Jeeves")))

    names = [word["pipeline_name"] for word in only(hass, router)["wake_words"]]

    assert names == ["Jeeves", "Plain"]


async def test_a_wake_word_set_to_preferred_resolves_to_the_preferred_pipeline(
    hass, router, pipelines
):
    await pipelines["ws"].send_json_auto_id(
        {"type": "assist_pipeline/pipeline/set_preferred", "pipeline_id": pipelines["router"]}
    )
    assert (await pipelines["ws"].receive_json())["success"]
    add_satellite(hass, router, selects=(("pipeline", "preferred"),))

    (word,) = only(hass, router)["wake_words"]

    assert word["preferred"] is True
    assert word["pipeline_id"] == pipelines["router"]
    assert word["uses_router"] is True


async def test_an_option_naming_no_known_pipeline_has_no_pipeline_id(hass, router, pipelines):
    add_satellite(hass, router, selects=(("pipeline", "Deleted one"),))

    (word,) = only(hass, router)["wake_words"]

    assert word["pipeline_id"] is None
    assert word["pipeline_name"] == "Deleted one"
    assert word["uses_router"] is False


async def test_a_satellite_without_a_pipeline_select_has_no_wake_words(hass, router, pipelines):
    add_satellite(hass, router)

    assert only(hass, router)["wake_words"] == []


async def test_satellites_are_found_without_assist_pipeline(hass, router):
    add_satellite(hass, router, selects=(("pipeline", "Jeeves"),))

    assert only(hass, router)["wake_words"] == []


async def test_satellites_are_sorted_by_name(hass, router):
    add_satellite(hass, router, name="Study satellite")
    add_satellite(hass, router, name="Attic satellite")

    names = [item["name"] for item in setup_guide.find_satellites(hass, router)]

    assert names == ["Attic satellite", "Study satellite"]


async def test_router_entity_id_is_the_routers_conversation_entity(hass, router):
    assert setup_guide.router_entity_id(hass, router) == registered_router_id(hass, router)


async def test_router_entity_id_is_none_when_the_entity_is_not_registered(hass):
    entry = MockConfigEntry(domain="voice_assistant_manager")
    entry.add_to_hass(hass)

    assert setup_guide.router_entity_id(hass, entry) is None


NAMES = {"owner": "Alex", "partner": "Sam"}


async def test_the_helper_payload_is_accepted_and_makes_the_expected_helper(hass, hass_ws_client):
    assert await async_setup_component(hass, "input_text", {})
    ws = await hass_ws_client(hass)
    device_id = "0123456789abcdef0123456789abcdef"

    await ws.send_json_auto_id(
        {"type": "input_text/create", **setup_guide.helper_payload(device_id)}
    )
    response = await ws.receive_json()

    assert response["success"], response
    assert hass.states.get(f"input_text.voice_speaker_{device_id}") is not None


async def _setup_automation_config(hass: HomeAssistant) -> None:
    """Automations that reload from the test config dir, where the endpoint writes them."""
    config_dir = Path(hass.config.config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "configuration.yaml").write_text("automation: !include automations.yaml")
    (config_dir / "automations.yaml").write_text("[]")
    assert await async_setup_component(hass, "automation", {})
    assert await async_setup_component(hass, "config", {})


async def _config_with_tracking(
    hass: HomeAssistant, router: MockConfigEntry, hass_client
) -> tuple[str, setup_guide.Satellite]:
    """Set up config and automation, post the tracking automation, return its id."""
    device_id = add_satellite(hass, router)
    assert await async_setup_component(
        hass, "input_text", {"input_text": {f"voice_speaker_{device_id}": {"max": 20}}}
    )
    await _setup_automation_config(hass)
    satellite = only(hass, router)
    automation_id, config = setup_guide.tracking_automation(satellite, NAMES)
    client = await hass_client()
    response = await client.post(f"/api/config/automation/config/{automation_id}", json=config)
    assert response.status == 200, await response.text()
    await hass.async_block_till_done()
    return automation_id, satellite


async def test_the_tracking_automation_is_accepted(hass, router, hass_client):
    automation_id, _ = await _config_with_tracking(hass, router, hass_client)

    entity_id = er.async_get(hass).async_get_entity_id("automation", "automation", automation_id)
    assert entity_id is not None
    assert hass.states.get(entity_id) is not None


async def _speak(
    hass: HomeAssistant, satellite: setup_guide.Satellite, freezer, speaker: str
) -> str:
    """One turn: idle for a while, wake word, then the voice match; return the helper."""
    hass.states.async_set(satellite["entity_id"], "idle")
    freezer.tick(10)
    hass.states.async_set(satellite["entity_id"], "listening")
    await hass.async_block_till_done()
    assert hass.states.get(satellite["helper"]).state == "pending"
    hass.states.async_set(satellite["speaker_sensor"], speaker)
    hass.states.async_set(satellite["score_sensor"], str(len(speaker) / 10))
    await hass.async_block_till_done()
    return hass.states.get(satellite["helper"]).state


@pytest.mark.parametrize(
    ("speaker", "role"),
    [("Alex", "owner"), ("Sam", "partner"), ("Visitor", "stranger")],
)
async def test_the_tracking_automation_writes_pending_then_the_role(
    hass, router, hass_client, freezer, speaker, role
):
    _, satellite = await _config_with_tracking(hass, router, hass_client)

    assert await _speak(hass, satellite, freezer, speaker) == role


async def test_a_follow_up_listen_keeps_the_last_speaker(hass, router, hass_client, freezer):
    _, satellite = await _config_with_tracking(hass, router, hass_client)
    assert await _speak(hass, satellite, freezer, "Sam") == "partner"

    hass.states.async_set(satellite["entity_id"], "idle")
    freezer.tick(1)
    hass.states.async_set(satellite["entity_id"], "listening")
    await hass.async_block_till_done()

    assert hass.states.get(satellite["helper"]).state == "partner"


async def test_voice_match_names_match_regardless_of_case_and_spaces(hass, router, hass_client):
    device_id = add_satellite(hass, router)
    assert await async_setup_component(
        hass, "input_text", {"input_text": {f"voice_speaker_{device_id}": {"max": 20}}}
    )
    await _setup_automation_config(hass)
    satellite = only(hass, router)
    automation_id, config = setup_guide.tracking_automation(
        satellite, {"owner": "  ALEX ", "partner": "Sam"}
    )
    client = await hass_client()
    assert (
        await client.post(f"/api/config/automation/config/{automation_id}", json=config)
    ).status == 200
    await hass.async_block_till_done()

    hass.states.async_set(satellite["speaker_sensor"], " alex ")
    hass.states.async_set(satellite["score_sensor"], "0.8")
    await hass.async_block_till_done()

    assert hass.states.get(satellite["helper"]).state == "owner"


async def test_a_tracking_automation_needs_a_score_sensor(hass, router):
    add_satellite(hass, router)
    satellite = only(hass, router)
    no_score = setup_guide.Satellite(**{**satellite, "score_sensor": None})

    with pytest.raises(ValueError, match="score sensor"):
        setup_guide.tracking_automation(no_score, NAMES)


async def test_a_tracking_automation_needs_a_speaker_sensor_and_two_names(hass, router):
    add_satellite(hass, router)
    satellite = only(hass, router)
    no_sensor = setup_guide.Satellite(**{**satellite, "speaker_sensor": None})

    with pytest.raises(ValueError, match="speaker sensor"):
        setup_guide.tracking_automation(no_sensor, NAMES)
    with pytest.raises(ValueError, match="names"):
        setup_guide.tracking_automation(satellite, {"owner": "Alex", "partner": "  "})
    with pytest.raises(ValueError, match="names"):
        setup_guide.tracking_automation(satellite, {"owner": "Alex"})


async def test_regression_final_a_tracking_automation_needs_two_different_names(hass, router):
    # The same name twice would make every turn of that person count as the partner.
    add_satellite(hass, router)

    with pytest.raises(ValueError, match="different names"):
        setup_guide.tracking_automation(only(hass, router), {"owner": "Alex", "partner": "ALEX "})


async def test_the_pipeline_copy_keeps_the_source_settings_and_uses_the_router(
    hass, router, pipelines
):
    ws = pipelines["ws"]
    await ws.send_json_auto_id(
        {
            "type": "assist_pipeline/pipeline/create",
            "name": "Source",
            "conversation_engine": "conversation.home_assistant",
            "conversation_language": "de",
            "language": "de",
            "stt_engine": "stt.example",
            "stt_language": "de-DE",
            "tts_engine": "tts.example",
            "tts_language": "de-DE",
            "tts_voice": "ada",
            "wake_word_entity": "wake_word.example",
            "wake_word_id": "hey_jeeves",
            "prefer_local_intents": True,
        }
    )
    source_id = (await ws.receive_json())["result"]["id"]
    router_id = registered_router_id(hass, router)

    payload = setup_guide.pipeline_copy_payload(hass, source_id, router_id, "Source (router)")
    await ws.send_json_auto_id({"type": "assist_pipeline/pipeline/create", **payload})
    response = await ws.receive_json()

    assert response["success"], response
    created = response["result"]
    assert created["id"] != source_id
    assert created["stt_engine"] == "stt.example"
    assert created["tts_engine"] == "tts.example"
    assert created["tts_voice"] == "ada"
    assert created["language"] == "de"
    assert created["wake_word_entity"] == "wake_word.example"
    assert created["wake_word_id"] == "hey_jeeves"
    assert created["conversation_engine"] == router_id
    assert created["prefer_local_intents"] is False
    assert created["name"] == "Source (router)"


async def test_a_name_taken_by_another_agents_pipeline_gets_a_number(hass, router, pipelines):
    router_id = registered_router_id(hass, router)

    payload = setup_guide.pipeline_copy_payload(hass, pipelines["other"], router_id, "Plain")

    assert payload["name"] == "Plain 2"
    assert setup_guide.reusable_pipeline(hass, "Plain", router_id) is None
    assert setup_guide.free_pipeline_name(hass, "Fresh") == "Fresh"


async def test_a_pipeline_named_after_the_router_that_uses_it_is_reused(hass, router, pipelines):
    router_id = registered_router_id(hass, router)

    assert setup_guide.reusable_pipeline(hass, "Jeeves", router_id) == pipelines["router"]
    assert setup_guide.free_pipeline_name(hass, "Jeeves") == "Jeeves 2"


async def test_regression_final_a_shared_name_resolves_to_the_first_pipeline_as_ha_does(
    hass, router, pipelines
):
    # Home Assistant's select takes the first pipeline with the chosen name.
    ws = pipelines["ws"]
    other_id = await create_pipeline(ws, "Shared", "conversation.home_assistant")
    await create_pipeline(ws, "Shared", registered_router_id(hass, router))
    add_satellite(hass, router, selects=(("pipeline", "Shared"),))

    (word,) = only(hass, router)["wake_words"]

    assert word["pipeline_id"] == other_id
    assert word["uses_router"] is False


@pytest.mark.parametrize("router_first", [True, False])
async def test_regression_final_a_routers_pipeline_with_a_shared_name_is_not_reused(
    hass, router, pipelines, router_first
):
    # Its name is shared, so the guide could never select it: a copy is made instead.
    ws = pipelines["ws"]
    router_id = registered_router_id(hass, router)
    engines = [router_id, "conversation.home_assistant"]
    for engine in engines if router_first else reversed(engines):
        await create_pipeline(ws, router.title, engine)

    assert setup_guide.reusable_pipeline(hass, router.title, router_id) is None
    assert setup_guide.free_pipeline_name(hass, router.title) == f"{router.title} 2"


# Ready-made styles.

_EN = Path(setup_guide.__file__).parent / "translations" / "en.json"


def _guide_strings() -> dict[str, str]:
    options = json.loads(_EN.read_text(encoding="utf-8"))["options"]
    flat: dict[str, str] = {}

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(f"{prefix}.{key}" if prefix else key, value)
        else:
            flat[prefix] = node

    walk("", options["guide"] if "guide" in options else {})
    return {f"guide.{key}": text for key, text in flat.items()}


@pytest.mark.parametrize("style", setup_guide.STYLES)
def test_every_style_fills_every_field(style):
    filled = setup_guide.fill_style(_guide_strings(), style, "Alex", "Sam")

    assert set(filled) == {"owner", "partner", "strangers", "near_miss"}
    assert set(filled["strangers"]) == {"persona", "fixed_replies"}
    texts = [
        filled["owner"]["persona"],
        filled["partner"]["persona"],
        filled["strangers"]["persona"],
        filled["strangers"]["fixed_replies"],
        filled["near_miss"]["persona"],
    ]
    assert all(text.strip() for text in texts)
    assert len(filled["strangers"]["fixed_replies"].split("\n")) == 2
    assert not any("{" in text for text in texts)


def test_a_style_puts_in_both_names():
    filled = setup_guide.fill_style(_guide_strings(), "butler", "Alex", "Sam")

    assert "Alex" in filled["owner"]["persona"]
    assert "Sam" in filled["owner"]["persona"]
    assert filled["partner"]["persona"].index("Sam") < filled["partner"]["persona"].index("Alex")


def test_a_style_uses_person_1_and_person_2_for_blank_names():
    filled = setup_guide.fill_style(_guide_strings(), "plain", " ", "")

    assert "Person 1" in filled["owner"]["persona"]
    assert "Person 2" in filled["owner"]["persona"]


def test_an_unknown_style_is_refused():
    with pytest.raises(ValueError, match="style"):
        setup_guide.fill_style(_guide_strings(), "pirate", "Alex", "Sam")
