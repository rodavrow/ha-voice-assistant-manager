# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The setup guide's commands on the settings page's websocket."""

# Fixtures are imported from the neighbouring test modules and used by name.
# ruff: noqa: F401, F811

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, async_mock_service

from custom_components.voice_assistant_manager import setup_guide
from custom_components.voice_assistant_manager.const import DOMAIN

from .router_helpers import MAIN_AGENT, add_router
from .router_helpers import router_entity_id as registered_router_id
from .test_panel import Page, bare_router, page
from .test_setup_guide import (
    NAMES,
    _automation_using,
    add_satellite,
    create_pipeline,
    pipelines,
    router,
    without_native_audio_libraries,
)

SELECT = "select.kitchen_pipeline_0"


@pytest.fixture
async def pipelines_for_title(
    without_native_audio_libraries, hass: HomeAssistant, hass_ws_client
) -> tuple[MockConfigEntry, dict[str, str]]:
    """A router titled like one of its pipelines, so the guide finds that pipeline again."""
    entry = await add_router(hass, title="Jeeves")
    assert await async_setup_component(hass, "assist_pipeline", {})
    await hass.async_block_till_done()
    ws = await hass_ws_client(hass)
    return entry, {
        "router": await create_pipeline(ws, "Jeeves", registered_router_id(hass, entry)),
        "other": await create_pipeline(ws, "Plain", "conversation.home_assistant"),
    }


async def pipeline_uses_local_intents(hass: HomeAssistant, pipeline_id: str) -> bool:
    # Imported here: assist_pipeline needs the stubbed audio libraries (see the fixture).
    from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline

    return async_get_pipeline(hass, pipeline_id).prefer_local_intents


# Who may use it.


@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("guide/satellites", {}),
        ("guide/tracking_automation", {"device_id": "d", "names": NAMES}),
        ("guide/pipeline_copy", {"select": SELECT}),
        ("guide/set_wake_word", {"select": SELECT, "pipeline_id": "p"}),
        ("guide/local_off", {"pipeline_id": "p"}),
        ("guide/state", {"state": "done"}),
        ("guide/style", {"style": "butler", "owner": "Alex", "partner": "Sam"}),
    ],
)
async def test_guide_commands_are_for_admins_only(
    hass, hass_ws_client, hass_read_only_access_token, command, data
):
    entry = await add_router(hass)
    reader = Page(await hass_ws_client(hass, hass_read_only_access_token))

    answer = await reader.call(command, entry_id=entry.entry_id, **data)

    assert answer["success"] is False
    assert answer["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("guide/satellites", {}),
        ("guide/tracking_automation", {"device_id": "d", "names": NAMES}),
        ("guide/pipeline_copy", {"select": SELECT}),
        ("guide/set_wake_word", {"select": SELECT, "pipeline_id": "p"}),
        ("guide/local_off", {"pipeline_id": "p"}),
        ("guide/state", {"state": "done"}),
        ("guide/style", {"style": "butler", "owner": "Alex", "partner": "Sam"}),
    ],
)
async def test_a_guide_command_for_a_router_that_does_not_exist_is_an_error(
    hass, page, command, data
):
    await add_router(hass)

    answer = await page.call(command, entry_id="nope", **data)

    assert answer["success"] is False
    assert answer["error"]["code"] == "not_found"


# Satellites.


async def test_satellites_lists_satellites_and_helper_payloads(hass, router, page, pipelines):
    device_id = add_satellite(hass, router, selects=(("pipeline", "Jeeves"),))

    result = await page.ok("guide/satellites", entry_id=router.entry_id)

    assert result["ok"] is True
    (satellite,) = result["satellites"]
    assert satellite == dict(setup_guide.find_satellites(hass, router)[0])
    assert satellite["wake_words"][0]["pipeline_id"] == pipelines["router"]
    assert result["helper_payloads"] == {device_id: setup_guide.helper_payload(device_id)}


async def test_satellites_is_empty_without_satellites(hass, router, page):
    result = await page.ok("guide/satellites", entry_id=router.entry_id)

    assert result == {"ok": True, "satellites": [], "helper_payloads": {}}


# The tracking automation.


async def test_tracking_automation_answers_the_id_and_config(hass, router, page):
    device_id = add_satellite(hass, router)

    result = await page.ok(
        "guide/tracking_automation",
        entry_id=router.entry_id,
        device_id=device_id,
        names=NAMES,
    )

    expected_id, expected_config = setup_guide.tracking_automation(
        setup_guide.find_satellites(hass, router)[0], NAMES
    )
    assert result == {"ok": True, "id": expected_id, "config": expected_config}


async def test_tracking_automation_is_refused_for_a_tracked_satellite(hass, router, page):
    device_id = add_satellite(hass, router)
    await _automation_using(hass, "sensor.kitchen_last_speaker_score")

    result = await page.ok(
        "guide/tracking_automation",
        entry_id=router.entry_id,
        device_id=device_id,
        names=NAMES,
    )

    assert result["ok"] is False
    assert "automation" in result["errors"]["base"]


@pytest.mark.parametrize(
    ("satellite_options", "names", "reason"),
    [
        ({"sensors": False}, NAMES, "speaker sensors"),
        ({}, {"owner": "Alex", "partner": " "}, "names"),
    ],
)
async def test_tracking_automation_is_refused_when_it_cannot_be_built(
    hass, router, page, satellite_options, names, reason
):
    device_id = add_satellite(hass, router, **satellite_options)

    result = await page.ok(
        "guide/tracking_automation", entry_id=router.entry_id, device_id=device_id, names=names
    )

    assert result["ok"] is False
    assert reason in result["errors"]["base"]


async def test_regression_final_tracking_is_refused_for_the_same_name_twice(hass, router, page):
    device_id = add_satellite(hass, router)
    names = {"owner": "Alex", "partner": " alex "}

    result = await page.ok(
        "guide/tracking_automation", entry_id=router.entry_id, device_id=device_id, names=names
    )

    assert result["ok"] is False
    assert "different names" in result["errors"]["base"]


async def test_tracking_automation_is_refused_without_a_score_sensor(hass, router, page):
    device_id = add_satellite(hass, router)
    er.async_get(hass).async_remove("sensor.kitchen_last_speaker_score")

    result = await page.ok(
        "guide/tracking_automation", entry_id=router.entry_id, device_id=device_id, names=NAMES
    )

    assert result["ok"] is False
    assert "speaker sensors" in result["errors"]["base"]


async def test_tracking_automation_is_refused_for_an_unknown_satellite(hass, router, page):
    result = await page.ok(
        "guide/tracking_automation", entry_id=router.entry_id, device_id="nope", names=NAMES
    )

    assert result["ok"] is False
    assert "satellite" in result["errors"]["base"]


# Local commands.


async def test_local_off_changes_only_a_pipeline_of_this_router(hass, router, page, pipelines):
    add_satellite(hass, router, selects=(("pipeline", "Jeeves"),))

    result = await page.ok(
        "guide/local_off", entry_id=router.entry_id, pipeline_id=pipelines["router"]
    )

    assert result["ok"] is True
    assert not await pipeline_uses_local_intents(hass, pipelines["router"])
    assert await pipeline_uses_local_intents(hass, pipelines["other"])


async def test_local_off_refuses_a_pipeline_of_another_agent(hass, router, page, pipelines):
    result = await page.ok(
        "guide/local_off", entry_id=router.entry_id, pipeline_id=pipelines["other"]
    )

    assert result["ok"] is False
    assert "does not use this assistant" in result["errors"]["base"]
    assert await pipeline_uses_local_intents(hass, pipelines["other"])


async def test_local_off_refuses_the_preferred_pipeline_of_another_agent(
    hass, router, page, pipelines
):
    await pipelines["ws"].send_json_auto_id(
        {"type": "assist_pipeline/pipeline/set_preferred", "pipeline_id": pipelines["other"]}
    )
    assert (await pipelines["ws"].receive_json())["success"]
    add_satellite(hass, router, selects=(("pipeline", "preferred"),))
    (word,) = setup_guide.find_satellites(hass, router)[0]["wake_words"]

    result = await page.ok(
        "guide/local_off", entry_id=router.entry_id, pipeline_id=word["pipeline_id"]
    )

    assert result["ok"] is False
    assert await pipeline_uses_local_intents(hass, pipelines["other"])


async def test_local_off_refuses_an_unknown_pipeline(hass, router, page, pipelines):
    result = await page.ok("guide/local_off", entry_id=router.entry_id, pipeline_id="nope")

    assert result["ok"] is False


async def test_local_off_names_every_satellite_on_that_pipeline(hass, router, page, pipelines):
    add_satellite(hass, router, name="Kitchen satellite", selects=(("pipeline", "Jeeves"),))
    add_satellite(
        hass,
        router,
        name="Study satellite",
        selects=(("pipeline", "Plain"), ("pipeline_n", "Jeeves")),
    )
    add_satellite(hass, router, name="Attic satellite", selects=(("pipeline", "Plain"),))

    result = await page.ok(
        "guide/local_off", entry_id=router.entry_id, pipeline_id=pipelines["router"]
    )

    assert result == {"ok": True, "satellites": ["Kitchen satellite", "Study satellite"]}


async def test_local_off_reports_a_pipeline_that_cannot_be_updated(
    hass, router, page, pipelines, monkeypatch
):
    async def failing_update(*args: Any, **kwargs: Any) -> None:
        raise HomeAssistantError("storage is read-only")

    monkeypatch.setattr(
        "homeassistant.components.assist_pipeline.pipeline.async_update_pipeline", failing_update
    )

    result = await page.ok(
        "guide/local_off", entry_id=router.entry_id, pipeline_id=pipelines["router"]
    )

    assert result["ok"] is False
    assert "storage is read-only" in result["errors"]["base"]


# Wake words.


async def test_set_wake_word_selects_the_routers_pipeline(hass, router, page, pipelines):
    add_satellite(hass, router, selects=(("pipeline", "Plain"),))
    calls = async_mock_service(hass, "select", "select_option")

    result = await page.ok(
        "guide/set_wake_word",
        entry_id=router.entry_id,
        select=SELECT,
        pipeline_id=pipelines["router"],
    )

    assert result == {"ok": True}
    assert [call.data for call in calls] == [{"entity_id": SELECT, "option": "Jeeves"}]


async def test_set_wake_word_refuses_a_pipeline_of_another_router(hass, router, page, pipelines):
    add_satellite(hass, router, selects=(("pipeline", "Jeeves"),))
    calls = async_mock_service(hass, "select", "select_option")

    result = await page.ok(
        "guide/set_wake_word",
        entry_id=router.entry_id,
        select=SELECT,
        pipeline_id=pipelines["other"],
    )

    assert result["ok"] is False
    assert calls == []


async def test_set_wake_word_refuses_a_select_that_is_no_wake_word(hass, router, page, pipelines):
    add_satellite(hass, router, selects=(("pipeline", "Plain"),))
    hass.states.async_set("select.something_else", "a")
    calls = async_mock_service(hass, "select", "select_option")

    result = await page.ok(
        "guide/set_wake_word",
        entry_id=router.entry_id,
        select="select.something_else",
        pipeline_id=pipelines["router"],
    )

    assert result["ok"] is False
    assert calls == []


async def test_set_wake_word_reports_a_refused_selection(hass, router, page, pipelines):
    add_satellite(hass, router, selects=(("pipeline", "Plain"),))

    result = await page.ok(
        "guide/set_wake_word",
        entry_id=router.entry_id,
        select=SELECT,
        pipeline_id=pipelines["router"],
    )

    # No select service is registered here, so Home Assistant refuses the call.
    assert result["ok"] is False
    assert result["errors"]["base"]


# Copying a pipeline.


async def test_pipeline_copy_reuses_the_routers_own_pipeline(hass, page, pipelines_for_title):
    entry, ids = pipelines_for_title
    add_satellite(hass, entry, selects=(("pipeline", "Plain"),))

    result = await page.ok("guide/pipeline_copy", entry_id=entry.entry_id, select=SELECT)

    assert result == {"ok": True, "reuse": ids["router"]}


async def test_pipeline_copy_answers_a_payload_for_the_router(hass, router, page, pipelines):
    add_satellite(hass, router, selects=(("pipeline", "Plain"),))
    router_id = registered_router_id(hass, router)

    result = await page.ok("guide/pipeline_copy", entry_id=router.entry_id, select=SELECT)

    assert result == {
        "ok": True,
        "payload": setup_guide.pipeline_copy_payload(
            hass, pipelines["other"], router_id, router.title
        ),
    }
    assert result["payload"]["conversation_engine"] == router_id


async def test_pipeline_copy_refuses_a_select_with_no_known_pipeline(hass, router, page, pipelines):
    add_satellite(hass, router, selects=(("pipeline", "Deleted one"),))

    result = await page.ok("guide/pipeline_copy", entry_id=router.entry_id, select=SELECT)

    assert result["ok"] is False


async def test_pipeline_copy_refuses_a_select_that_is_no_wake_word(hass, router, page, pipelines):
    result = await page.ok("guide/pipeline_copy", entry_id=router.entry_id, select="select.nope")

    assert result["ok"] is False


# Where the guide stands.


async def test_a_new_router_opens_in_the_guide(hass, page):
    entry = await bare_router(hass)

    assert (await page.router(entry))["guide"] == "new"


async def test_a_router_saved_before_gets_no_guide(hass, page):
    entry = await add_router(hass)

    assert (await page.router(entry))["guide"] == "none"


@pytest.mark.parametrize("state", ["done", "dismissed"])
async def test_a_stored_state_wins_over_the_default(hass, page, state):
    entry = await bare_router(hass)

    assert await page.ok("guide/state", entry_id=entry.entry_id, state=state) == {"ok": True}

    assert (await page.router(entry))["guide"] == state


async def test_an_unknown_state_is_refused(hass, page):
    entry = await bare_router(hass)

    answer = await page.call("guide/state", entry_id=entry.entry_id, state="later")

    assert answer["success"] is False


async def test_the_guide_state_survives_a_restart(hass, page, hass_storage):
    entry = await bare_router(hass)
    await page.ok("guide/state", entry_id=entry.entry_id, state="dismissed")

    assert hass_storage[f"{DOMAIN}.panel"]["data"] == {
        "sidebar": True,
        "guide": {entry.entry_id: "dismissed"},
    }


async def test_a_stored_guide_state_is_used_at_the_start(hass, page, hass_storage):
    hass_storage[f"{DOMAIN}.panel"] = {
        "version": 1,
        "data": {"sidebar": True, "guide": {"abc": "done"}},
    }
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(
        domain=DOMAIN, title="Office assistant", entry_id="abc", data={"agent": MAIN_AGENT}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (await page.router(entry))["guide"] == "done"


async def test_an_old_panel_store_still_loads(hass, page, hass_storage):
    hass_storage[f"{DOMAIN}.panel"] = {"version": 1, "data": {"sidebar": False}}

    entry = await bare_router(hass)

    result = await page.ok("get")
    assert result["sidebar"] is False
    assert (await page.router(entry))["guide"] == "new"


async def test_hiding_the_page_keeps_the_guide_state(hass, page, hass_storage):
    entry = await bare_router(hass)
    await page.ok("guide/state", entry_id=entry.entry_id, state="done")

    await page.ok("set_sidebar", show=False)

    assert hass_storage[f"{DOMAIN}.panel"]["data"] == {
        "sidebar": False,
        "guide": {entry.entry_id: "done"},
    }


async def test_a_started_guide_keeps_its_step(hass, page):
    entry = await bare_router(hass)

    answer = await page.ok("guide/state", entry_id=entry.entry_id, state="started", step=3)

    assert answer == {"ok": True}
    router = await page.router(entry)
    assert (router["guide"], router["guide_step"]) == ("started", 3)


async def test_a_started_guide_wins_over_saved_settings(hass, page):
    # Step 2 saves the settings; the guide must still resume after the page is closed.
    entry = await add_router(hass)

    await page.ok("guide/state", entry_id=entry.entry_id, state="started", step=4)

    router = await page.router(entry)
    assert (router["guide"], router["guide_step"]) == ("started", 4)


@pytest.mark.parametrize("state", ["new", "none", "done", "dismissed"])
async def test_only_a_started_guide_has_a_step(hass, page, state):
    entry = await (bare_router(hass) if state == "new" else add_router(hass))
    if state in ("done", "dismissed"):
        await page.ok("guide/state", entry_id=entry.entry_id, state=state)

    router = await page.router(entry)
    assert (router["guide"], router["guide_step"]) == (state, None)


async def test_a_started_guide_needs_a_step(hass, page):
    entry = await bare_router(hass)

    answer = await page.call("guide/state", entry_id=entry.entry_id, state="started")

    assert answer["success"] is False
    assert (await page.router(entry))["guide"] == "new"


@pytest.mark.parametrize("step", [0, 8, "3"])
async def test_a_step_outside_the_guide_is_refused(hass, page, step):
    entry = await bare_router(hass)

    answer = await page.call("guide/state", entry_id=entry.entry_id, state="started", step=step)

    assert answer["success"] is False


async def test_a_started_guide_is_stored_with_its_step(hass, page, hass_storage):
    entry = await bare_router(hass)

    await page.ok("guide/state", entry_id=entry.entry_id, state="started", step=2)

    assert hass_storage[f"{DOMAIN}.panel"]["data"]["guide"] == {
        entry.entry_id: {"state": "started", "step": 2}
    }


async def test_finishing_a_started_guide_drops_its_step(hass, page, hass_storage):
    entry = await bare_router(hass)
    await page.ok("guide/state", entry_id=entry.entry_id, state="started", step=6)

    await page.ok("guide/state", entry_id=entry.entry_id, state="done")

    assert hass_storage[f"{DOMAIN}.panel"]["data"]["guide"] == {entry.entry_id: "done"}


@pytest.mark.parametrize(
    ("stored", "guide", "step"),
    [
        ({"state": "started", "step": 5}, "started", 5),
        ({"state": "started", "step": 9}, "new", None),
        ({"state": "started"}, "new", None),
        ({"state": "later", "step": 2}, "new", None),
        ("later", "new", None),
    ],
)
async def test_a_stored_started_guide_is_loaded_and_a_broken_one_dropped(
    hass, page, hass_storage, stored, guide, step
):
    hass_storage[f"{DOMAIN}.panel"] = {
        "version": 1,
        "data": {"sidebar": True, "guide": {"abc": stored}},
    }
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(
        domain=DOMAIN, title="Office assistant", entry_id="abc", data={"agent": MAIN_AGENT}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    router = await page.router(entry)
    assert (router["guide"], router["guide_step"]) == (guide, step)


async def test_pipeline_commands_are_refused_while_assist_is_not_set_up(hass, router, page):
    add_satellite(hass, router, selects=(("pipeline", "Jeeves"),))

    local_off = await page.ok("guide/local_off", entry_id=router.entry_id, pipeline_id="p")
    wake_word = await page.ok(
        "guide/set_wake_word", entry_id=router.entry_id, select=SELECT, pipeline_id="p"
    )

    assert local_off["ok"] is False
    assert wake_word["ok"] is False


async def _shared_name_pipelines(hass, router, pipelines, *, router_first: bool = False) -> str:
    """The router's pipeline named like another agent's pipeline; returns the router's id."""
    ws = pipelines["ws"]
    router_id = registered_router_id(hass, router)
    if router_first:
        shared_id = await create_pipeline(ws, "Shared", router_id)
    await create_pipeline(ws, "Shared", "conversation.home_assistant")
    return shared_id if router_first else await create_pipeline(ws, "Shared", router_id)


async def test_regression_duplicate_pipeline_name_is_not_selected(hass, router, page, pipelines):
    shared_id = await _shared_name_pipelines(hass, router, pipelines)
    add_satellite(hass, router, selects=(("pipeline", "Plain"),))
    calls = async_mock_service(hass, "select", "select_option")

    result = await page.ok(
        "guide/set_wake_word", entry_id=router.entry_id, select=SELECT, pipeline_id=shared_id
    )

    assert result["ok"] is False
    assert "Rename one" in result["errors"]["base"]
    assert calls == []


async def test_regression_duplicate_pipeline_name_does_not_hide_the_routers_pipeline(
    hass, router, page, pipelines
):
    shared_id = await _shared_name_pipelines(hass, router, pipelines, router_first=True)

    result = await page.ok("guide/local_off", entry_id=router.entry_id, pipeline_id=shared_id)

    assert result["ok"] is True
    assert not await pipeline_uses_local_intents(hass, shared_id)


# Ready-made styles.


async def test_style_command_returns_filled_texts(hass, router, page):
    result = await page.ok(
        "guide/style", entry_id=router.entry_id, style="butler", owner="Alex", partner="Sam"
    )

    assert result["ok"] is True
    assert "Alex" in result["texts"]["owner"]["persona"]
    assert "Sam" in result["texts"]["partner"]["persona"]
    assert result["texts"]["strangers"]["fixed_replies"]


async def test_style_command_refuses_an_unknown_style(hass, router, page):
    answer = await page.call(
        "guide/style", entry_id=router.entry_id, style="pirate", owner="Alex", partner="Sam"
    )

    assert answer["success"] is False
