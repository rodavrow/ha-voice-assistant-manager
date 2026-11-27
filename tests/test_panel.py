# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the settings page: its place in Home Assistant and what it can ask for."""

import copy
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components import frontend
from homeassistant.components.conversation import ConversationEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import announcement_look
from custom_components.voice_assistant_manager.calendar_watch import get_watcher
from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.memory import async_get_memory, entry_key, global_key
from custom_components.voice_assistant_manager.panel import PANEL_URL

from .router_helpers import (
    MAIN_AGENT,
    MEMORY_AGENT,
    REFUSAL_AGENT,
    FakeAgents,
    add_router,
    options_with,
)
from .test_calendar_watch import LINE, WASTE, WASTE_ROW, Home, all_day, calendar_options
from .test_setup_guide import (
    _automation_using,
    add_entity,
    add_satellite,
    pipelines,  # noqa: F401 - fixture
    router,  # noqa: F401 - fixture
    without_native_audio_libraries,  # noqa: F401 - fixture
)

PREFIX = "voice_assistant_manager/panel"
MINE_URL = "my-assistant"
COMPONENT = Path(__file__).parent.parent / "custom_components" / DOMAIN


class Page:
    """The settings page's side of the websocket."""

    def __init__(self, client) -> None:
        self._client = client

    async def call(self, command: str, **data: Any) -> dict[str, Any]:
        await self._client.send_json_auto_id({"type": f"{PREFIX}/{command}", **data})
        return await self._client.receive_json()

    async def ok(self, command: str, **data: Any) -> dict[str, Any]:
        answer = await self.call(command, **data)
        assert answer["success"], answer
        return answer["result"]

    async def router(self, entry: MockConfigEntry) -> dict[str, Any]:
        routers = (await self.ok("get"))["routers"]
        return next(router for router in routers if router["entry_id"] == entry.entry_id)


@pytest.fixture
async def page(hass: HomeAssistant, hass_ws_client) -> Page:
    return Page(await hass_ws_client(hass))


async def bare_router(hass: HomeAssistant) -> MockConfigEntry:
    """A router that was added but whose settings were never saved."""
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Office assistant", data={"agent": MAIN_AGENT})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def set_agents(hass: HomeAssistant) -> None:
    for agent in (MAIN_AGENT, MEMORY_AGENT, REFUSAL_AGENT):
        hass.states.async_set(agent, "unknown", {"supported_features": 0})


def texts(router: dict[str, Any], level: str | None = None) -> list[str]:
    return [item["text"] for item in router["status"] if level in (None, item["level"])]


# Its place in Home Assistant.


async def test_the_page_is_registered_for_admins_and_shown_in_the_sidebar(hass: HomeAssistant):
    await add_router(hass)

    panel = hass.data[frontend.DATA_PANELS][PANEL_URL]

    assert panel.require_admin is True
    assert panel.show_in_sidebar is True
    assert panel.sidebar_title == "Voice Manager"
    assert panel.config["_panel_custom"]["name"] == "voice-assistant-manager-panel"
    assert panel.config["_panel_custom"]["embed_iframe"] is False


async def test_the_page_is_registered_once_for_two_routers(hass: HomeAssistant):
    await add_router(hass)
    await add_router(hass, title="Office assistant")

    assert sorted(hass.data[frontend.DATA_PANELS]) == [MINE_URL, PANEL_URL]


async def test_my_assistant_is_registered_for_everyone_and_shown_in_the_sidebar(
    hass: HomeAssistant,
):
    await add_router(hass)

    panel = hass.data[frontend.DATA_PANELS][MINE_URL]

    assert panel.require_admin is False
    assert panel.show_in_sidebar is True
    assert panel.sidebar_title == "My assistant"
    assert panel.sidebar_icon == "mdi:account-voice"
    assert panel.config["_panel_custom"]["name"] == "voice-assistant-manager-mine"
    assert panel.config["_panel_custom"]["embed_iframe"] is False


async def test_my_assistant_stays_in_the_sidebar_when_the_settings_page_is_hidden(hass, page):
    await add_router(hass)

    await page.ok("set_sidebar", show=False)

    assert hass.data[frontend.DATA_PANELS][MINE_URL].show_in_sidebar is True


async def test_my_assistant_is_in_the_sidebar_after_a_start_with_the_settings_page_hidden(
    hass, hass_storage
):
    hass_storage[f"{DOMAIN}.panel"] = {"version": 1, "data": {"sidebar": False}}

    await add_router(hass)

    assert hass.data[frontend.DATA_PANELS][MINE_URL].show_in_sidebar is True


async def test_the_script_of_my_assistant_is_served_with_the_version(
    hass: HomeAssistant, hass_client
):
    import json  # noqa: PLC0415

    await add_router(hass)
    version = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))["version"]
    url = hass.data[frontend.DATA_PANELS][MINE_URL].config["_panel_custom"]["module_url"]
    client = await hass_client()

    response = await client.get(url)

    assert url == f"/{DOMAIN}/mine.js?v={version}"
    assert response.status == 200
    assert "voice-assistant-manager-mine" in await response.text()


async def test_the_script_of_the_page_is_served(hass: HomeAssistant, hass_client):
    await add_router(hass)
    url = hass.data[frontend.DATA_PANELS][PANEL_URL].config["_panel_custom"]["module_url"]
    client = await hass_client()

    response = await client.get(url)

    assert response.status == 200
    assert "voice-assistant-manager-panel" in await response.text()


async def test_the_address_of_the_script_changes_with_the_version(hass: HomeAssistant):
    import json  # noqa: PLC0415

    await add_router(hass)
    version = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))["version"]

    url = hass.data[frontend.DATA_PANELS][PANEL_URL].config["_panel_custom"]["module_url"]

    assert url.endswith(f"?v={version}")


async def test_hiding_the_page_takes_it_out_of_the_sidebar_but_keeps_it(hass, page):
    await add_router(hass)

    result = await page.ok("set_sidebar", show=False)

    assert result == {"sidebar": False}
    panel = hass.data[frontend.DATA_PANELS][PANEL_URL]
    assert panel.show_in_sidebar is False
    assert (await page.ok("get"))["sidebar"] is False


async def test_a_hidden_page_stays_hidden_after_a_restart(hass, page, hass_storage):
    await add_router(hass)
    await page.ok("set_sidebar", show=False)

    assert hass_storage[f"{DOMAIN}.panel"]["data"] == {"sidebar": False, "guide": {}}


async def test_a_stored_choice_to_hide_the_page_is_used_at_the_start(hass, hass_storage):
    hass_storage[f"{DOMAIN}.panel"] = {"version": 1, "data": {"sidebar": False}}

    await add_router(hass)

    assert hass.data[frontend.DATA_PANELS][PANEL_URL].show_in_sidebar is False



# Showing which assistant speaks on devices that can.


async def test_showing_the_speaking_assistant_is_on_by_default(hass, page):
    await add_router(hass)

    assert (await page.ok("get"))["announcement_look"] is True
    assert announcement_look.is_enabled(hass) is True


async def test_switching_off_showing_the_speaking_assistant(hass, page, hass_storage):
    await add_router(hass)

    result = await page.ok("set_announcement_look", on=False)

    assert result == {"announcement_look": False}
    assert announcement_look.is_enabled(hass) is False
    assert hass_storage[f"{DOMAIN}.panel"]["data"]["announcement_look"] is False


async def test_switched_off_it_stays_off_after_a_restart(hass, hass_storage):
    hass_storage[f"{DOMAIN}.panel"] = {"version": 1, "data": {"announcement_look": False}}

    await add_router(hass)

    assert announcement_look.is_enabled(hass) is False


async def test_switching_it_back_on_leaves_nothing_in_the_store(hass, page, hass_storage):
    await add_router(hass)
    await page.ok("set_announcement_look", on=False)

    await page.ok("set_announcement_look", on=True)

    assert "announcement_look" not in hass_storage[f"{DOMAIN}.panel"]["data"]
    assert announcement_look.is_enabled(hass) is True


# Who may use it.


@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("get", {}),
        ("set_sidebar", {"show": False}),
        ("set_announcement_look", {"on": False}),
        ("save_settings", {"entry_id": "x", "settings": {}}),
        ("save_calendar", {"entry_id": "x", "row": {}}),
        ("save_memory", {"entry_id": "x", "key": "k", "shown": [], "lines": []}),
    ],
)
async def test_someone_who_is_not_an_admin_gets_nothing(
    hass, hass_ws_client, hass_read_only_access_token, command, data
):
    await add_router(hass)
    reader = Page(await hass_ws_client(hass, hass_read_only_access_token))

    answer = await reader.call(command, **data)

    assert answer["success"] is False
    assert answer["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("command", ["save_settings", "save_calendar", "save_memory"])
async def test_a_router_that_does_not_exist_is_an_error(hass, page, command):
    await add_router(hass)
    data = {"settings": {}, "row": {}, "key": "k", "shown": [], "lines": []}
    wanted = {
        "save_settings": ("settings",),
        "save_calendar": ("row",),
        "save_memory": ("key", "shown", "lines"),
    }[command]

    answer = await page.call(command, entry_id="nope", **{key: data[key] for key in wanted})

    assert answer["success"] is False
    assert answer["error"]["code"] == "not_found"


# What it shows.


async def test_get_lists_every_router_with_its_settings(hass, page):
    kitchen = await add_router(hass)
    office = await add_router(hass, title="Office assistant")

    result = await page.ok("get")

    assert [router["title"] for router in result["routers"]] == [
        "Kitchen assistant",
        "Office assistant",
    ]
    router = result["routers"][0]
    assert router["entry_id"] == kitchen.entry_id
    assert router["assistant"] == "kitchen"
    assert router["saved"] is True
    assert router["settings"]["owner"] == kitchen.options["owner"]
    assert result["routers"][1]["entry_id"] == office.entry_id
    assert result["sidebar"] is True


async def test_get_proposes_settings_for_a_router_that_never_saved_any(hass, page):
    entry = await bare_router(hass)

    router = await page.router(entry)

    assert router["saved"] is False
    assert router["settings"]["agents"] == {"agent": MAIN_AGENT}
    assert router["settings"]["memory"]["enabled"] is False


async def test_get_leaves_the_calendars_out_of_the_settings_and_lists_them_apart(hass, page):
    entry = await add_router(hass, calendar_options(WASTE_ROW))

    router = await page.router(entry)

    assert "calendar_rows" not in router["settings"]
    assert router["calendars"] == [WASTE_ROW]


async def test_get_gives_the_people_who_can_log_in(hass, page, hass_admin_user):
    await add_router(hass)

    users = (await page.ok("get"))["users"]

    assert {"id": hass_admin_user.id, "name": hass_admin_user.name} in users


async def test_get_gives_the_texts_of_the_settings_screen(hass, page):
    await add_router(hass)

    strings = (await page.ok("get"))["strings"]

    assert strings["step.settings.sections.memory.name"] == "Memory"
    assert strings["step.calendar_edit.data.contains"] == "Only events containing"
    assert strings["error.same_user"].startswith("Person 1 and Person 2")


async def test_get_lists_what_is_remembered(hass, page):
    entry = await add_router(hass)
    memory = await async_get_memory(hass)
    key = entry_key(entry.entry_id, "owner")
    await memory.async_apply_edit(key, [], ["Likes jazz", "(private) Birthday gift for Sam"])

    router = await page.router(entry)

    lists = {found["key"]: found for found in router["memory"]}
    assert lists[key]["label"] == "Alex: this assistant (2)"
    assert [(item["text"], item.get("private", False)) for item in lists[key]["items"]] == [
        ("Likes jazz", False),
        ("Birthday gift for Sam", True),
    ]
    assert global_key("wife") in lists


async def test_get_tells_the_last_calendar_event_that_was_announced(hass, page):
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    home = Home(hass)
    entry = await add_router(hass, calendar_options(WASTE_ROW))
    home.events[WASTE] = [all_day(date(2026, 11, 23))]
    now = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
    with FakeAgents(writer_reply=LINE).patched():
        await get_watcher(hass, entry.entry_id).async_check(now)
        await hass.async_block_till_done()

    router = await page.router(entry)

    assert router["last_announced"] == {
        "title": "Avalex: GFT",
        "calendar": WASTE,
        "line": LINE,
        "at": now.isoformat(),
    }


async def test_what_was_last_announced_is_still_known_after_a_reload(hass, page):
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    home = Home(hass)
    entry = await add_router(hass, calendar_options(WASTE_ROW))
    home.events[WASTE] = [all_day(date(2026, 11, 23))]
    with FakeAgents(writer_reply=LINE).patched():
        await get_watcher(hass, entry.entry_id).async_check(
            datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
        )
        await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert (await page.router(entry))["last_announced"]["title"] == "Avalex: GFT"


async def test_a_router_that_announced_nothing_has_no_last_event(hass, page):
    entry = await add_router(hass)

    assert (await page.router(entry))["last_announced"] is None


# What needs attention.


async def test_a_router_that_is_set_up_well_has_nothing_to_warn_about(hass, page):
    set_agents(hass)
    entry = await add_router(hass, options_with(owner={"user": "user-alex"}))
    device_id = add_satellite(hass, entry)
    hass.states.async_set(f"input_text.voice_speaker_{device_id}", "")
    await _automation_using(hass, "sensor.kitchen_last_speaker_score")

    router = await page.router(entry)

    assert texts(router, "warn") == []
    assert texts(router, "error") == []
    assert "Memory is on." in texts(router, "ok")


async def test_a_main_agent_that_does_not_exist_is_an_error(hass, page):
    entry = await add_router(hass)

    router = await page.router(entry)

    assert f"The main agent {MAIN_AGENT} does not exist." in texts(router, "error")


async def test_a_person_without_a_name_is_warned_about(hass, page):
    set_agents(hass)
    options = copy.deepcopy(calendar_options())
    options["partner"] = {"persona": "Someone."}
    entry = await add_router(hass, options)

    assert "Person 2 has no name." in texts(await page.router(entry), "warn")


async def test_calendars_without_a_script_are_warned_about(hass, page):
    set_agents(hass)
    entry = await add_router(hass, calendar_options(WASTE_ROW, calendars={"script": ""}))

    warnings = texts(await page.router(entry), "warn")

    assert "Calendars are watched, but no script is chosen to speak their events." in warnings


async def test_a_calendar_that_does_not_exist_is_warned_about(hass, page):
    set_agents(hass)
    entry = await add_router(hass, calendar_options(WASTE_ROW))

    assert f"The calendar {WASTE} does not exist." in texts(await page.router(entry), "warn")


async def test_an_announcement_agent_with_tools_is_an_error(hass, page):
    set_agents(hass)
    hass.states.async_set(REFUSAL_AGENT, "unknown", {"supported_features": 1})
    hass.states.async_set(WASTE, "off")
    entry = await add_router(hass, calendar_options(WASTE_ROW))

    errors = texts(await page.router(entry), "error")

    assert any("announcement agent" in text for text in errors)


async def test_the_announcement_agent_does_not_matter_when_nothing_is_announced(hass, page):
    set_agents(hass)
    hass.states.async_set(REFUSAL_AGENT, "unknown", {"supported_features": 1})
    entry = await add_router(hass)

    warnings = texts(await page.router(entry), "warn")

    assert not any("announcement agent" in text for text in warnings)


async def test_every_status_line_names_the_part_of_the_page_it_is_about(hass, page):
    entry = await add_router(hass, calendar_options(WASTE_ROW))

    router = await page.router(entry)

    sections = {
        "assistant",
        "people",
        "strangers",
        "memory",
        "reminders",
        "calendars",
        "satellites",
    }
    assert {item["section"] for item in router["status"]} <= sections


# Satellites and pipelines.


def lines_with_fix(status: dict[str, Any], fix: str) -> list[dict[str, Any]]:
    return [item for item in status["status"] if item.get("fix") == fix]


async def test_a_missing_helper_is_warned_about_with_a_fix(hass, page, router, pipelines):
    device_id = add_satellite(hass, router, selects=(("pipeline", "Jeeves"),))

    (line,) = lines_with_fix(await page.router(router), "create_helper")

    assert line["level"] == "warn"
    assert line["section"] == "satellites"
    assert line["target"] == device_id
    assert "Kitchen satellite" in line["text"]


async def test_untracked_speaker_sensors_are_warned_about_with_a_fix(hass, page, router):
    device_id = add_satellite(hass, router)
    hass.states.async_set(f"input_text.voice_speaker_{device_id}", "")

    (line,) = lines_with_fix(await page.router(router), "tracking")

    assert (line["level"], line["target"]) == ("warn", device_id)


async def test_sensors_that_are_tracked_get_no_tracking_warning(hass, page, router):
    device_id = add_satellite(hass, router)
    hass.states.async_set(f"input_text.voice_speaker_{device_id}", "")
    await _automation_using(hass, "sensor.kitchen_last_speaker")

    assert lines_with_fix(await page.router(router), "tracking") == []


async def test_a_speaker_sensor_without_a_score_sensor_gets_no_tracking_warning(
    hass, page, router
):
    device_id = add_satellite(hass, router, sensors=False)
    add_entity(hass, router, device_id, "sensor", "kitchen_last_speaker", "Alex")
    hass.states.async_set(f"input_text.voice_speaker_{device_id}", "")

    assert lines_with_fix(await page.router(router), "tracking") == []


async def test_local_commands_on_the_routers_pipeline_are_an_error_with_a_fix(
    hass, page, router, pipelines
):
    add_satellite(hass, router, selects=(("pipeline", "Jeeves"),))

    (line,) = lines_with_fix(await page.router(router), "local_off")

    assert (line["level"], line["section"]) == ("error", "satellites")
    assert line["target"] == pipelines["router"]
    assert "unknown voices" in line["text"]


async def test_one_pipeline_on_two_satellites_is_reported_once(hass, page, router, pipelines):
    for name in ("Kitchen satellite", "Hall satellite"):
        add_satellite(hass, router, selects=(("pipeline", "Jeeves"),), name=name)

    assert len(lines_with_fix(await page.router(router), "local_off")) == 1


async def test_a_satellite_of_another_router_is_left_out(hass, page, router, pipelines):
    add_satellite(hass, router, selects=(("pipeline", "Plain"),))

    status = await page.router(router)

    assert [item for item in status["status"] if item["section"] == "satellites"][0][
        "level"
    ] == "info"
    assert not any(item.get("fix") for item in status["status"])


async def test_a_router_on_no_satellite_gets_an_info_line(hass, page, router):
    (line,) = [
        item for item in (await page.router(router))["status"] if item["section"] == "satellites"
    ]

    assert line["level"] == "info"
    assert "fix" not in line


# Saving the settings.


async def changed(page: Page, entry: MockConfigEntry, **sections: dict) -> dict[str, Any]:
    settings = (await page.router(entry))["settings"]
    for name, values in sections.items():
        settings[name] = {**settings.get(name, {}), **values}
    return settings


async def test_saving_settings_stores_them_and_answers_with_the_router(hass, page):
    entry = await add_router(hass)
    settings = await changed(page, entry, owner={"name": "Alexandra"})

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert result["router"]["settings"]["owner"]["name"] == "Alexandra"
    assert entry.options["owner"]["name"] == "Alexandra"


async def test_saving_settings_keeps_the_calendars(hass, page):
    entry = await add_router(hass, calendar_options(WASTE_ROW))
    settings = await changed(page, entry, owner={"name": "Alexandra"})

    await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert entry.options["calendar_rows"] == [WASTE_ROW]


async def test_saving_settings_fills_in_what_the_page_left_out(hass, page):
    entry = await add_router(hass)
    settings = await changed(page, entry)
    del settings["memory"]["max_items"]

    await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert entry.options["memory"]["max_items"] == 50


async def test_settings_the_dialog_would_refuse_are_refused_with_its_message(hass, page):
    entry = await add_router(hass)
    before = copy.deepcopy(dict(entry.options))
    settings = await changed(page, entry, memory={"enabled": True, "agent": MAIN_AGENT})

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is False
    assert result["errors"]["base"].startswith("The agent that decides what to save may not")
    assert entry.options == before


async def test_chat_settings_are_saved_by_the_page(hass, page):
    entry = await add_router(hass)
    chat = {"character": "A dry, put-upon butler.", "tts_engine": "tts.kitchen", "voice": "jeeves"}
    settings = await changed(page, entry, chat=chat)

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert entry.options["chat"] == chat
    assert result["router"]["settings"]["chat"] == chat


async def test_settings_without_a_chat_part_are_saved_with_an_empty_one(hass, page):
    entry = await add_router(hass)
    settings = await changed(page, entry)
    settings.pop("chat", None)

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert entry.options["chat"] == {}


async def test_a_chat_character_over_2000_characters_is_refused_by_the_page(hass, page):
    entry = await add_router(hass)
    before = copy.deepcopy(dict(entry.options))
    settings = await changed(page, entry, chat={"character": "x" * 2001})

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is False
    assert result["section"] == "assistant"
    assert "2000" in result["errors"]["base"]
    assert entry.options == before


async def refusal_save(hass, page, agent_features: int, mode: str = "ai") -> dict[str, Any]:
    """Save a refusal agent with the given features, and answer what the page answers."""
    entry = await add_router(hass)
    hass.states.async_set("conversation.refuser", "unknown", {"supported_features": agent_features})
    settings = await changed(
        page, entry, strangers={"mode": mode, "reply_agent": "conversation.refuser"}
    )
    return await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)


async def test_a_refusal_agent_that_can_control_the_home_is_refused(hass, page):
    control = int(ConversationEntityFeature.CONTROL)

    result = await refusal_save(hass, page, control)

    assert result["ok"] is False
    assert result["errors"]["base"].startswith("The agent that writes refusals may not be")


async def test_a_refusal_agent_without_tools_is_accepted(hass, page):
    result = await refusal_save(hass, page, 0)

    assert result["ok"] is True


async def test_the_refusal_agent_does_not_matter_with_fixed_lines(hass, page):
    control = int(ConversationEntityFeature.CONTROL)

    result = await refusal_save(hass, page, control, mode="fixed")

    assert result["ok"] is True


async def test_regression_final_a_refused_save_names_the_section_to_fix(hass, page):
    # The setup guide saves everything at each step: it shows where the refused field is.
    control = int(ConversationEntityFeature.CONTROL)

    result = await refusal_save(hass, page, control)

    assert result["section"] == "strangers"


async def test_regression_final_a_refused_memory_save_names_the_memory_section(hass, page):
    entry = await add_router(hass)
    settings = await changed(page, entry, memory={"enabled": True})
    settings["memory"].pop("agent", None)

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert (result["ok"], result["section"]) == (False, "memory")


async def test_regression_final_a_save_of_the_wrong_shape_names_no_section(hass, page):
    entry = await add_router(hass)
    settings = await changed(page, entry, made_up={"x": 1})

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is False
    assert "section" not in result


@pytest.mark.parametrize(
    "broken",
    [
        {"memory": {"max_items": 5000}},
        {"strangers": {"mode": "open the doors"}},
        {"calendars": {"catch_up_hours": -1}},
        {"made_up": {"x": 1}},
    ],
)
async def test_settings_of_the_wrong_shape_are_refused(hass, page, broken):
    entry = await add_router(hass)
    before = copy.deepcopy(dict(entry.options))
    settings = await changed(page, entry, **broken)

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is False
    assert result["errors"]["base"]
    assert entry.options == before


async def test_calendars_cannot_be_slipped_in_with_the_settings(hass, page):
    entry = await add_router(hass)
    settings = await changed(page, entry)
    settings["calendar_rows"] = [{"calendar": "calendar.sneaky"}]

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is False
    assert "calendar_rows" not in entry.options


# Saving a calendar.


async def test_a_calendar_is_added(hass, page):
    entry = await add_router(hass)

    result = await page.ok("save_calendar", entry_id=entry.entry_id, row=dict(WASTE_ROW))

    assert result["ok"] is True
    assert result["router"]["calendars"] == [{**WASTE_ROW, "lead_minutes": 0}]
    assert entry.options["calendar_rows"] == [{**WASTE_ROW, "lead_minutes": 0}]
    assert entry.options["owner"] == {"name": "Alex", "persona": "The person speaking is Alex."}


async def test_a_calendar_is_changed(hass, page):
    entry = await add_router(hass, calendar_options(WASTE_ROW))

    await page.ok(
        "save_calendar",
        entry_id=entry.entry_id,
        original=WASTE,
        row={**WASTE_ROW, "all_day_time": "18:30"},
    )

    assert [row["all_day_time"] for row in entry.options["calendar_rows"]] == ["18:30"]


async def test_a_calendar_is_removed(hass, page):
    entry = await add_router(hass, calendar_options(WASTE_ROW))

    await page.ok(
        "save_calendar", entry_id=entry.entry_id, original=WASTE, row=dict(WASTE_ROW), remove=True
    )

    assert entry.options["calendar_rows"] == []


async def test_a_calendar_with_a_bad_time_is_refused_at_its_field(hass, page):
    entry = await add_router(hass)

    result = await page.ok(
        "save_calendar", entry_id=entry.entry_id, row={**WASTE_ROW, "all_day_time": "soon"}
    )

    assert result["ok"] is False
    assert result["errors"]["all_day_time"].startswith("Write the time as hours and minutes")
    assert "calendar_rows" not in entry.options


async def test_the_same_calendar_twice_is_refused(hass, page):
    entry = await add_router(hass, calendar_options(WASTE_ROW))

    result = await page.ok("save_calendar", entry_id=entry.entry_id, row=dict(WASTE_ROW))

    assert result["errors"] == {"calendar": "This assistant already watches that calendar."}


@pytest.mark.parametrize(
    "row",
    [
        {"calendar": "light.kitchen"},
        {"calendar": WASTE, "for": "the cat"},
        {"calendar": WASTE, "all_day_day": "sometime"},
        {"calendar": WASTE, "lead_minutes": 99999},
        {},
    ],
)
async def test_a_calendar_of_the_wrong_shape_is_refused(hass, page, row):
    entry = await add_router(hass)

    result = await page.ok("save_calendar", entry_id=entry.entry_id, row=row)

    assert result["ok"] is False
    assert "calendar_rows" not in entry.options


async def test_a_router_that_never_saved_settings_cannot_get_calendars_yet(hass, page):
    entry = await bare_router(hass)

    result = await page.ok("save_calendar", entry_id=entry.entry_id, row=dict(WASTE_ROW))

    assert result["ok"] is False
    assert result["errors"]["base"].startswith("Open Settings and save them once first")


# Editing what is remembered.


async def test_a_fact_is_added_and_another_removed(hass, page):
    entry = await add_router(hass)
    memory = await async_get_memory(hass)
    key = entry_key(entry.entry_id, "owner")
    await memory.async_apply_edit(key, [], ["Likes jazz", "Hates mornings"])
    shown = [item["id"] for item in memory.items(key)]

    result = await page.ok(
        "save_memory",
        entry_id=entry.entry_id,
        key=key,
        shown=shown,
        lines=["Likes jazz", "(private) Is planning a surprise"],
    )

    assert result["ok"] is True
    assert [(item["text"], item.get("private", False)) for item in memory.items(key)] == [
        ("Likes jazz", False),
        ("Is planning a surprise", True),
    ]
    lists = {found["key"]: found for found in result["router"]["memory"]}
    assert len(lists[key]["items"]) == 2


async def test_a_fact_saved_while_the_page_was_open_is_left_alone(hass, page):
    entry = await add_router(hass)
    memory = await async_get_memory(hass)
    key = entry_key(entry.entry_id, "owner")
    await memory.async_apply_edit(key, [], ["Likes jazz"])
    shown = [item["id"] for item in memory.items(key)]
    await memory.async_apply_edit(key, memory.items(key), ["Likes jazz", "Came in meanwhile"])

    await page.ok("save_memory", entry_id=entry.entry_id, key=key, shown=shown, lines=[])

    assert [item["text"] for item in memory.items(key)] == ["Came in meanwhile"]


async def test_a_fact_that_is_too_long_is_refused(hass, page):
    entry = await add_router(hass)
    key = entry_key(entry.entry_id, "owner")

    result = await page.ok(
        "save_memory", entry_id=entry.entry_id, key=key, shown=[], lines=["x" * 201]
    )

    assert result["ok"] is False
    assert result["errors"]["base"].startswith("A fact may be 200 characters at most")


async def test_the_list_of_another_router_cannot_be_edited(hass, page):
    kitchen = await add_router(hass)
    office = await add_router(hass, title="Office assistant")
    memory = await async_get_memory(hass)
    key = entry_key(office.entry_id, "owner")

    answer = await page.call(
        "save_memory", entry_id=kitchen.entry_id, key=key, shown=[], lines=["Planted"]
    )

    assert answer["success"] is False
    assert answer["error"]["code"] == "not_found"
    assert memory.items(key) == []


# The reminder calendars.

ALEX_CALENDAR = "calendar.alex_reminders"
SAM_CALENDAR = "calendar.sam_reminders"
HOME_CALENDAR = "calendar.household_reminders"


async def second_user(hass):
    return await hass.auth.async_create_user("Second", group_ids=["system-users"])


async def save_reminder_calendars(page, entry, users=(None, None), **reminders):
    settings = await changed(page, entry, reminders=reminders)
    for key, user in (("owner", users[0]), ("partner", users[1])):
        settings[key] = {k: v for k, v in settings[key].items() if k != "user"}
        if user:
            settings[key]["user"] = user
    return await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)


def another_router(hass, title, users, **reminders):
    other = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={"name": title, "agent": MAIN_AGENT},
        options=options_with(
            owner={"user": users[0]},
            partner={"user": users[1]},
            reminders=reminders,
        ),
    )
    other.add_to_hass(hass)
    return other


async def test_reminder_calendars_are_saved(hass, page, hass_admin_user):
    other = await second_user(hass)
    entry = await add_router(hass)
    lists = {
        "owner_calendars": [ALEX_CALENDAR],
        "partner_calendars": [SAM_CALENDAR],
        "household_calendars": [HOME_CALENDAR],
    }

    result = await save_reminder_calendars(page, entry, (hass_admin_user.id, other.id), **lists)

    assert result["ok"] is True
    assert {key: entry.options["reminders"][key] for key in lists} == lists


async def test_a_calendar_in_both_peoples_lists_is_refused(hass, page, hass_admin_user):
    other = await second_user(hass)
    entry = await add_router(hass)

    result = await save_reminder_calendars(
        page,
        entry,
        (hass_admin_user.id, other.id),
        owner_calendars=[ALEX_CALENDAR],
        partner_calendars=[ALEX_CALENDAR],
    )

    assert result["ok"] is False
    assert ALEX_CALENDAR in result["errors"]["base"]
    assert result["section"] == "reminders"


async def test_a_personal_calendar_also_listed_as_household_is_refused(
    hass, page, hass_admin_user
):
    entry = await add_router(hass)

    result = await save_reminder_calendars(
        page,
        entry,
        (hass_admin_user.id, None),
        owner_calendars=[ALEX_CALENDAR],
        household_calendars=[ALEX_CALENDAR],
    )

    assert result["ok"] is False
    assert ALEX_CALENDAR in result["errors"]["base"]


async def test_regression_final_a_clash_inside_one_router_says_so(hass, page, hass_admin_user):
    other = await second_user(hass)
    entry = await add_router(hass)

    result = await save_reminder_calendars(
        page,
        entry,
        (hass_admin_user.id, other.id),
        owner_calendars=[ALEX_CALENDAR],
        partner_calendars=[ALEX_CALENDAR],
    )

    assert result["errors"]["base"].startswith(
        f"{ALEX_CALENDAR} is chosen twice in this assistant's reminder calendars"
    )
    assert entry.title not in result["errors"]["base"]
    assert result["section"] == "reminders"


async def test_a_calendar_personal_for_another_user_in_another_router_is_refused(
    hass, page, hass_admin_user
):
    other_user = await second_user(hass)
    entry = await add_router(hass)
    another_router(hass, "Study assistant", (other_user.id, None), owner_calendars=[ALEX_CALENDAR])

    result = await save_reminder_calendars(
        page, entry, (hass_admin_user.id, None), owner_calendars=[ALEX_CALENDAR]
    )

    assert result["ok"] is False
    assert "Study assistant" in result["errors"]["base"]
    assert ALEX_CALENDAR in result["errors"]["base"]


async def test_the_same_household_calendar_in_two_routers_is_allowed(hass, page):
    entry = await add_router(hass)
    another_router(hass, "Study assistant", (None, None), household_calendars=[HOME_CALENDAR])

    result = await save_reminder_calendars(page, entry, household_calendars=[HOME_CALENDAR])

    assert result["ok"] is True


@pytest.mark.parametrize("person", ["owner", "partner"])
async def test_personal_reminder_calendars_need_that_persons_user(hass, page, person):
    entry = await add_router(hass)

    result = await save_reminder_calendars(page, entry, **{f"{person}_calendars": [ALEX_CALENDAR]})

    assert result["ok"] is False
    assert result["section"] == "reminders"


@pytest.mark.parametrize(
    "edit",
    [{"lines": ["Fact"] * 201}, {"lines": ["x" * 10_000]}, {"lines": [], "shown": ["id"] * 201}],
    ids=["201 lines", "10000 characters", "201 shown"],
)
async def test_regression_security_an_oversized_memory_save_is_refused(hass, page, edit):
    entry = await add_router(hass)
    key = entry_key(entry.entry_id, "owner")

    answer = await page.call(
        "save_memory", entry_id=entry.entry_id, key=key, **{"shown": [], **edit}
    )

    assert answer["success"] is False
    assert answer["error"]["code"] == "invalid_format"
    assert (await async_get_memory(hass)).items(key) == []


async def test_regression_security_a_list_grown_over_200_facts_is_refused(hass, page):
    entry = await add_router(hass)
    key = entry_key(entry.entry_id, "owner")
    memory = await async_get_memory(hass)
    await memory.async_apply_edit(key, [], [f"Old {number}" for number in range(10)])

    result = await page.ok(
        "save_memory",
        entry_id=entry.entry_id,
        key=key,
        shown=[],
        lines=[f"New {number}" for number in range(200)],
    )

    assert result["ok"] is False
    assert result["errors"]["base"].startswith("A list may hold 200 facts at most")
    assert len(memory.items(key)) == 10
