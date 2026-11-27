# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for favorites in the router: who is asking, the switch and the fast path."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import conversation as router_module
from custom_components.voice_assistant_manager import favorites_context
from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.favorites import ListKey, async_get_favorites
from custom_components.voice_assistant_manager.favorites_context import (
    ASKER_LIMIT,
    Asker,
    asker,
    forget,
    remember,
)
from custom_components.voice_assistant_manager.panel import (
    async_set_speaker_overrides,
    speaker_overrides,
)

from custom_components.voice_assistant_manager.conversation import DEFAULT_AGENT_DOWN_LINE

from .router_helpers import (
    MAIN_AGENT,
    FakeAgents,
    add_router,
    ask,
    ask_in_pipeline,
    options_with,
    router_entity_id,
)
from .test_favorites_play import add_device, add_player, record
from .test_options_flow import open_step, shown_values
from .test_panel import Page

ALEX_RADIO_2 = "https://stream.example.com/alex-radio2.mp3"
SAM_RADIO_2 = "https://stream.example.com/sam-radio2.mp3"
HOUSEHOLD_JAZZ = "https://stream.example.com/jazz.mp3"
FAVORITES_ON = {"favorites": {"enabled": True}}


async def favorites_router(hass: HomeAssistant, **sections: dict[str, Any]) -> MockConfigEntry:
    """A router with favorites on and some lists filled."""
    entry = await add_router(hass, options_with(**{**FAVORITES_ON, **sections}))
    store = await async_get_favorites(hass)
    lists: dict[ListKey, list[dict[str, Any]]] = {
        "person:owner": [{"name": "Radio 2", "url": ALEX_RADIO_2}],
        "person:wife": [{"name": "Radio 2", "url": SAM_RADIO_2}],
        "household": [
            {"name": "Jazz", "aliases": ["the jazz station"], "url": HOUSEHOLD_JAZZ},
            {"name": "Jazz on Sunday", "url": HOUSEHOLD_JAZZ + "?sunday"},
            {"name": "Deep Focus", "kind": "playlist", "uri": "library://playlist/7"},
        ],
    }
    for key, favorites in lists.items():
        for favorite in favorites:
            await store.async_add(key, favorite)
    return entry


@pytest.fixture
def satellite(hass: HomeAssistant) -> SimpleNamespace:
    """The Kitchen satellite with its own speaker, and the Denon elsewhere."""
    device_id = add_device(hass, "Kitchen satellite")
    return SimpleNamespace(
        device_id=device_id,
        speaker=add_player(hass, "kitchen_speaker", device_id=device_id),
        denon=add_player(hass, "denon", friendly_name="Denon"),
    )


@pytest.fixture
def played(hass: HomeAssistant) -> list[ServiceCall]:
    return record(hass, "media_player", "play_media")


def speech(result: conversation.ConversationResult) -> str:
    return str(result.response.speech["plain"]["speech"])


def plays(calls: list[ServiceCall]) -> list[tuple[str, str]]:
    return [(call.data["entity_id"], call.data["media_content_id"]) for call in calls]


async def run(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    text: str,
    agents: FakeAgents,
    device_id: str | None = None,
) -> conversation.ConversationResult:
    with agents.patched():
        result = await ask(hass, entry, text, device_id=device_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    return result


# The fast path: who is asking (Review Focus 1).


async def test_alex_hears_his_own_radio_2_when_both_have_one(hass, satellite, played):
    entry = await favorites_router(hass)
    agents = FakeAgents()

    result = await run(hass, entry, "Play Radio 2", agents, satellite.device_id)

    assert speech(result) == "Playing Radio 2."
    assert plays(played) == [(satellite.speaker, ALEX_RADIO_2)]


async def test_sam_hears_her_own_radio_2_when_both_have_one(hass, satellite, played):
    entry = await favorites_router(hass)
    hass.states.async_set(f"input_text.voice_speaker_{satellite.device_id}", "wife")

    await run(hass, entry, "Play Radio 2", FakeAgents(), satellite.device_id)

    assert plays(played) == [(satellite.speaker, SAM_RADIO_2)]


async def test_a_stranger_asking_for_a_favorite_plays_nothing_and_reaches_no_agent(
    hass, satellite, played
):
    entry = await favorites_router(hass)
    hass.states.async_set(f"input_text.voice_speaker_{satellite.device_id}", "stranger")
    agents = FakeAgents()

    result = await run(hass, entry, "Play Radio 2", agents, satellite.device_id)

    assert speech(result) == "The answer is no."
    assert played == []
    assert agents.calls == []


async def test_a_household_favorite_plays_by_another_name(hass, satellite, played):
    entry = await favorites_router(hass)

    result = await run(
        hass, entry, "Can you play the jazz station, please?", FakeAgents(), satellite.device_id
    )

    assert speech(result) == "Playing Jazz."
    assert plays(played) == [(satellite.speaker, HOUSEHOLD_JAZZ)]


async def test_a_favorite_whose_name_has_on_in_it_plays_whole(hass, satellite, played):
    entry = await favorites_router(hass)

    result = await run(hass, entry, "play Jazz on Sunday", FakeAgents(), satellite.device_id)

    assert speech(result) == "Playing Jazz on Sunday."
    assert plays(played) == [(satellite.speaker, HOUSEHOLD_JAZZ + "?sunday")]


async def test_a_named_player_is_used_and_named_in_the_reply(hass, satellite, played):
    entry = await favorites_router(hass)

    result = await run(hass, entry, "play Radio 2 on the Denon", FakeAgents(), satellite.device_id)

    assert speech(result) == "Playing Radio 2 on Denon."
    assert plays(played) == [(satellite.denon, ALEX_RADIO_2)]


async def test_the_speaker_override_of_the_satellite_comes_first(hass, satellite, played):
    entry = await favorites_router(hass)
    await async_set_speaker_overrides(hass, {satellite.device_id: satellite.denon})

    result = await run(hass, entry, "play Radio 2", FakeAgents(), satellite.device_id)

    assert speech(result) == "Playing Radio 2."
    assert plays(played) == [(satellite.denon, ALEX_RADIO_2)]


# A hit is answered by the router alone.


async def test_a_hit_never_calls_the_main_agent_or_memory(hass, satellite, played):
    entry = await favorites_router(hass)
    agents = FakeAgents()

    await run(hass, entry, "play Radio 2", agents, satellite.device_id)

    assert agents.calls == []


async def test_a_hit_comes_before_the_built_in_commands(hass, satellite, played):
    entry = await favorites_router(hass, local={"enabled": True})
    agents = FakeAgents()

    with patch.object(conversation, "async_handle_intents") as built_in:
        result = await run(hass, entry, "play Radio 2", agents, satellite.device_id)

    assert speech(result) == "Playing Radio 2."
    built_in.assert_not_called()
    assert agents.calls == []


async def test_a_hit_is_kept_in_the_history_with_the_speakers_label(hass, satellite, played):
    entry = await favorites_router(hass)

    with FakeAgents().patched():
        result, log = await ask_in_pipeline(hass, entry, "play Radio 2 on Denon")

    assert speech(result) == "Playing Radio 2 on Denon."
    assert [(item.role, item.content) for item in log[-2:]] == [
        ("user", "Alex: play Radio 2 on Denon"),
        ("assistant", "Playing Radio 2 on Denon."),
    ]


async def test_a_favorite_the_player_cannot_play_is_answered_with_why(hass, satellite, played):
    entry = await favorites_router(hass)
    agents = FakeAgents()

    result = await run(hass, entry, "play Deep Focus", agents, satellite.device_id)

    assert speech(result) == "Deep Focus needs a Music Assistant speaker."
    assert played == []
    assert agents.calls == []


# Everything else goes on as before (Review Focus 2).


@pytest.mark.parametrize("text", ["Play a game with me", "play some Queen", "what time is it"])
async def test_a_request_that_is_not_a_favorite_reaches_the_built_ins_and_main_agent(
    hass, satellite, played, text
):
    entry = await favorites_router(hass, local={"enabled": True})
    agents = FakeAgents()

    with patch.object(conversation, "async_handle_intents", return_value=None) as built_in:
        result = await run(hass, entry, text, agents, satellite.device_id)

    assert speech(result) == "Noted."
    built_in.assert_called_once()
    assert [call["text"] for call in agents.calls_to(MAIN_AGENT)] == [f"Alex: {text}"]
    assert played == []


async def test_favorites_off_leaves_a_favorite_to_the_main_agent(hass, satellite, played):
    entry = await favorites_router(hass, favorites={"enabled": False})
    agents = FakeAgents()

    await run(hass, entry, "play Radio 2", agents, satellite.device_id)

    assert played == []
    assert len(agents.calls_to(MAIN_AGENT)) == 1


async def test_settings_without_a_favorites_section_leave_it_off(hass, satellite, played):
    entry = await add_router(hass)
    await (await async_get_favorites(hass)).async_add(
        "household", {"name": "Jazz", "url": HOUSEHOLD_JAZZ}
    )
    agents = FakeAgents()

    await run(hass, entry, "play Jazz", agents, satellite.device_id)

    assert played == []
    assert len(agents.calls_to(MAIN_AGENT)) == 1


@pytest.mark.parametrize(
    ("text", "device"),
    [("play Radio 2 on the garage", True), ("play Radio 2", False)],
    ids=["unknown player", "no device and no player named"],
)
async def test_a_favorite_with_no_player_found_goes_to_the_main_agent(
    hass, satellite, played, text, device
):
    entry = await favorites_router(hass)
    agents = FakeAgents()

    result = await run(hass, entry, text, agents, satellite.device_id if device else None)

    assert speech(result) == "Noted."
    assert played == []
    assert len(agents.calls_to(MAIN_AGENT)) == 1


@pytest.mark.parametrize(
    ("text", "device"),
    [("play Radio 2 on the garage", True), ("play Radio 2", False)],
    ids=["unknown player", "no device and no player named"],
)
async def test_regression_a_favorite_with_no_player_found_skips_the_built_in_commands(
    hass, satellite, played, text, device
):
    # A built-in command would search Music Assistant and could play another "Radio 2".
    entry = await favorites_router(hass, local={"enabled": True})
    agents = FakeAgents()

    with patch.object(conversation, "async_handle_intents", return_value=None) as built_in:
        result = await run(hass, entry, text, agents, satellite.device_id if device else None)

    assert speech(result) == "Noted."
    built_in.assert_not_called()
    assert len(agents.calls_to(MAIN_AGENT)) == 1
    assert played == []


async def test_a_hit_is_answered_when_its_history_cannot_be_written(
    hass, satellite, played, caplog
):
    entry = await favorites_router(hass)
    agents = FakeAgents()

    with patch.object(
        router_module.PersonaRouter, "_in_turn_log", side_effect=RuntimeError("log is gone")
    ):
        result = await run(hass, entry, "play Radio 2", agents, satellite.device_id)

    assert speech(result) == "Playing Radio 2."
    assert result.conversation_id
    assert plays(played) == [(satellite.speaker, ALEX_RADIO_2)]
    assert agents.calls == []
    assert "history" in caplog.text.casefold()


async def test_an_unexpected_error_in_the_fast_path_lets_the_request_through(
    hass, satellite, played, caplog
):
    entry = await favorites_router(hass)
    agents = FakeAgents()

    with patch.object(router_module, "async_get_favorites", side_effect=RuntimeError("boom")):
        result = await run(hass, entry, "play Radio 2", agents, satellite.device_id)

    assert speech(result) == "Noted."
    assert len(agents.calls_to(MAIN_AGENT)) == 1
    assert "favorite" in caplog.text.casefold()


async def test_a_chat_show_request_never_plays_a_favorite(hass, satellite, played):
    entry = await favorites_router(hass)
    manager = SimpleNamespace(
        is_running_on=lambda device_id: True,
        is_self_wake=lambda device_id: False,
        is_live_on=lambda device_id: True,
        is_fragment=lambda text: False,
        add_person_line=lambda name, text: None,
    )
    agents = FakeAgents()

    with patch.object(router_module, "get_manager", return_value=manager):
        result = await run(hass, entry, "play Radio 2", agents, satellite.device_id)

    assert speech(result) == ""
    assert played == []
    assert agents.calls == []


# Who is asking, for the tools of the main agent.


def watching(hass: HomeAssistant, agents: FakeAgents, raises: Exception | None = None):
    """async_converse that notes (context id, asker) for each main agent call."""
    seen: list[tuple[str, Asker | None]] = []

    async def converse(hass_, text, conversation_id, context, **kwargs):
        if kwargs.get("agent_id") == MAIN_AGENT:
            seen.append((context.id, asker(hass, context.id)))
            if raises is not None:
                raise raises
        return await agents.converse(hass_, text, conversation_id, context, **kwargs)

    return seen, patch.object(conversation, "async_converse", converse)


async def test_the_asker_is_known_while_the_main_agent_answers_and_forgotten_after(
    hass, satellite
):
    entry = await favorites_router(hass)
    seen, patched = watching(hass, FakeAgents())

    with patched:
        await ask(hass, entry, "what's on tonight", device_id=satellite.device_id)

    ((context_id, during),) = seen
    assert during == Asker(entry.entry_id, "owner", satellite.device_id)
    assert asker(hass, context_id) is None


async def test_the_asker_is_forgotten_when_the_main_agent_fails(hass, satellite):
    entry = await favorites_router(hass)
    seen, patched = watching(hass, FakeAgents(), raises=RuntimeError("model is down"))

    with patched:
        result = await ask(hass, entry, "what's on tonight", device_id=satellite.device_id)

    ((context_id, during),) = seen
    assert during is not None
    assert asker(hass, context_id) is None
    # The failure reaches the person as a short line, not as an error.
    assert result.response.speech["plain"]["speech"] == DEFAULT_AGENT_DOWN_LINE


async def test_regression_a_request_that_remembered_nobody_keeps_another_requests_record(
    hass, satellite
):
    # Another router whose main agent is this one remembered the asker under the same
    # context; this request (no person of its own) must not forget it.
    entry = await favorites_router(hass)
    outer = Asker("outer-entry", "wife", None)
    context = Context()
    remember(hass, context.id, outer)
    # An older script's answer that does not say who is speaking: the one answer without
    # a person that still reaches the main agent.
    guest = {
        "persona_for_this_message": "",
        "rule": "",
        "label": "",
        "fixed_replies": [],
        "reply_agent": "",
    }
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(router_entity_id(hass, entry))
    agents = FakeAgents()

    with (
        agents.patched(),
        patch.object(router_module.PersonaRouter, "_lookup_from_settings", return_value=guest),
    ):
        await entity.async_process(
            conversation.ConversationInput(
                text="hello",
                context=context,
                conversation_id=None,
                device_id=satellite.device_id,
                satellite_id=None,
                language="en",
                agent_id=entity.entity_id,
            )
        )

    assert len(agents.calls_to(MAIN_AGENT)) == 1
    assert asker(hass, context.id) == outer


async def test_the_asker_is_known_with_favorites_off_too(hass, satellite):
    # The tools say favorites are off for this assistant; they still need to know whose.
    entry = await add_router(hass)
    seen, patched = watching(hass, FakeAgents())

    with patched:
        await ask(hass, entry, "hello", device_id=satellite.device_id)

    ((_, during),) = seen
    assert during == Asker(entry.entry_id, "owner", satellite.device_id)


# The record itself.


def test_a_remembered_asker_is_found_by_its_context_id(hass):
    remember(hass, "ctx-1", Asker("entry-1", "wife", "device-1"))

    assert asker(hass, "ctx-1") == Asker("entry-1", "wife", "device-1")


def test_no_context_id_or_an_unknown_one_has_no_asker(hass):
    remember(hass, "ctx-1", Asker("entry-1", "wife", None))

    assert asker(hass, None) is None
    assert asker(hass, "ctx-2") is None


def test_remembering_the_same_context_again_overwrites(hass):
    remember(hass, "ctx-1", Asker("entry-1", "owner", None))
    remember(hass, "ctx-1", Asker("entry-2", "wife", "device-2"))

    assert asker(hass, "ctx-1") == Asker("entry-2", "wife", "device-2")


def test_a_forgotten_asker_is_gone_and_forgetting_twice_is_harmless(hass):
    remember(hass, "ctx-1", Asker("entry-1", "owner", None))

    forget(hass, "ctx-1")
    forget(hass, "ctx-1")

    assert asker(hass, "ctx-1") is None


def test_an_asker_expires_after_the_time_limit(hass):
    clock = [1000.0]
    with patch.object(favorites_context, "_clock", lambda: clock[0]):
        remember(hass, "ctx-1", Asker("entry-1", "owner", None))
        clock[0] += ASKER_LIMIT - 0.1
        still = asker(hass, "ctx-1")
        clock[0] += 0.2
        expired = asker(hass, "ctx-1")

    assert ASKER_LIMIT == 60
    assert still is not None
    assert expired is None


# The switch, in the Configure dialog and on the page.


async def test_the_dialog_offers_favorites_off_for_settings_saved_before_it(hass):
    entry = await add_router(hass)

    form = await open_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], shown_values(form["data_schema"])
    )

    assert shown_values(form["data_schema"])["favorites"] == {"enabled": False}
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["favorites"] == {"enabled": False}


async def test_the_dialog_switches_favorites_on(hass):
    entry = await add_router(hass)
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["favorites"] = {"enabled": True}

    await hass.config_entries.options.async_configure(form["flow_id"], values)

    assert entry.options["favorites"] == {"enabled": True}


async def test_the_page_saves_the_favorites_switch(hass, hass_ws_client):
    entry = await add_router(hass)
    page = Page(await hass_ws_client(hass))
    settings = (await page.router(entry))["settings"]
    settings["favorites"] = {"enabled": True}

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert entry.options["favorites"] == {"enabled": True}


async def test_the_page_saves_settings_from_before_favorites_with_it_off(hass, hass_ws_client):
    entry = await add_router(hass)
    page = Page(await hass_ws_client(hass))
    settings = (await page.router(entry))["settings"]
    settings.pop("favorites", None)

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert entry.options["favorites"] == {"enabled": False}


# Speaker overrides in the page's store.


async def test_speaker_overrides_are_saved_and_read(hass, hass_storage):
    await add_router(hass)

    await async_set_speaker_overrides(hass, {"device-1": "media_player.denon"})

    assert speaker_overrides(hass) == {"device-1": "media_player.denon"}
    stored = hass_storage[f"{DOMAIN}.panel"]["data"]["speaker_overrides"]
    assert stored == {"device-1": "media_player.denon"}


async def test_saved_speaker_overrides_are_read_after_a_restart(hass, hass_storage):
    hass_storage[f"{DOMAIN}.panel"] = {
        "version": 1,
        "data": {"sidebar": True, "speaker_overrides": {"device-1": "media_player.denon"}},
    }

    await add_router(hass)

    assert speaker_overrides(hass) == {"device-1": "media_player.denon"}


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "media_player.denon",
        {"device-1": 7},
        {"device-1": "light.kitchen"},
        {"": "media_player.denon"},
    ],
    ids=["missing", "not a mapping", "not text", "not a player", "no device"],
)
async def test_damaged_speaker_overrides_read_as_none(hass, hass_storage, stored):
    data: dict[str, Any] = {"sidebar": True}
    if stored is not None:
        data["speaker_overrides"] = stored
    hass_storage[f"{DOMAIN}.panel"] = {"version": 1, "data": data}

    await add_router(hass)

    assert speaker_overrides(hass) == {}


@pytest.mark.parametrize(
    "overrides",
    [{"device-1": "light.kitchen"}, {"": "media_player.denon"}, {"device-1": ""}],
    ids=["not a player", "no device", "no player"],
)
async def test_speaker_overrides_that_are_not_valid_are_refused(hass, overrides):
    await add_router(hass)
    await async_set_speaker_overrides(hass, {"device-1": "media_player.denon"})

    with pytest.raises(ValueError):
        await async_set_speaker_overrides(hass, overrides)

    assert speaker_overrides(hass) == {"device-1": "media_player.denon"}


async def test_the_overrides_read_are_a_copy(hass):
    await add_router(hass)
    await async_set_speaker_overrides(hass, {"device-1": "media_player.denon"})

    speaker_overrides(hass)["device-1"] = "media_player.other"

    assert speaker_overrides(hass) == {"device-1": "media_player.denon"}
