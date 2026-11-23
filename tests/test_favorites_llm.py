# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the favorites tools of the main agent, and the status lines about them."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import favorites_context
from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.favorites import ListKey, async_get_favorites
from custom_components.voice_assistant_manager.favorites_context import (
    ASKER_LIMIT,
    Asker,
    remember,
)
from custom_components.voice_assistant_manager.memory import Person
from custom_components.voice_assistant_manager.panel import async_set_speaker_overrides
from custom_components.voice_assistant_manager.status import router_status

from .router_helpers import MAIN_AGENT, add_router, options_with
from .test_favorites_play import MA, add_device, add_music_assistant, add_player, item, record
from .test_favorites_router import (
    ALEX_RADIO_2,
    FAVORITES_ON,
    HOUSEHOLD_JAZZ,
    SAM_RADIO_2,
    favorites_router,
)

NEW_STREAM = "https://stream.example.com/new.mp3"


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


@pytest.fixture(autouse=True)
async def music_assistant_entries_not_unloaded(hass: HomeAssistant) -> Any:
    """The faked Music Assistant entries must not be unloaded at teardown."""
    yield
    for entry in hass.config_entries.async_entries(MA):
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


def plays(calls: list[ServiceCall]) -> list[tuple[str, str]]:
    return [(call.data["entity_id"], call.data["media_content_id"]) for call in calls]


def llm_context(context: Context | None, device_id: str | None = None) -> llm.LLMContext:
    return llm.LLMContext(
        platform="test",
        context=context,
        language="en",
        assistant="conversation",
        device_id=device_id,
    )


async def call_tool(
    hass: HomeAssistant,
    tool: str,
    args: dict[str, Any],
    *,
    entry: MockConfigEntry | None = None,
    role: Person = "owner",
    device_id: str | None = None,
    context_device_id: str | None = None,
    known: bool = True,
) -> llm.ToolResult:
    """Call one tool as the main agent would, with the router's record of who is asking."""
    context = Context()
    if known and entry is not None:
        remember(hass, context.id, Asker(entry.entry_id, role, device_id))
    api = await llm.async_get_api(hass, DOMAIN, llm_context(context, context_device_id))
    return await api.async_call_tool(llm.ToolInput(tool_name=tool, tool_args=args))


async def names_in(hass: HomeAssistant, key: ListKey) -> list[str]:
    store = await async_get_favorites(hass)
    return [favorite["name"] for favorite in store.items(key)]


# The API.


async def test_the_api_is_listed_for_the_main_agents(hass: HomeAssistant):
    await add_router(hass)

    apis = {api.id: api.name for api in llm.async_get_apis(hass)}

    assert apis[DOMAIN] == "Voice Assistant Manager"


async def test_the_api_offers_the_four_tools_and_says_to_try_a_favorite_first(
    hass: HomeAssistant,
):
    await add_router(hass)

    api = await llm.async_get_api(hass, DOMAIN, llm_context(Context()))

    assert sorted(tool.name for tool in api.tools) == [
        "list_favorites", "play_favorite", "remove_favorite", "save_favorite",
    ]
    assert "play_favorite first" in api.api_prompt
    assert (
        "To save what is playing, call save_favorite with this=true and the name the person "
        "gave; ask for a name if they gave none." in api.api_prompt
    )
    assert all(tool.integration == DOMAIN for tool in api.tools)


# Who is asking (Review Focus 3).


async def test_a_tool_call_without_a_record_of_who_asks_is_refused(hass, satellite, played):
    entry = await favorites_router(hass)

    result = await call_tool(
        hass, "play_favorite", {"name": "Radio 2"}, entry=entry,
        context_device_id=satellite.device_id, known=False,
    )

    assert result.error is True
    assert result.data == {"status": "error", "message": "I can't tell who is asking."}
    assert played == []


async def test_a_tool_call_without_a_context_is_refused(hass, satellite, played):
    await favorites_router(hass)
    api = await llm.async_get_api(hass, DOMAIN, llm_context(None, satellite.device_id))

    result = await api.async_call_tool(
        llm.ToolInput(tool_name="play_favorite", tool_args={"name": "Radio 2"})
    )

    assert result.data["message"] == "I can't tell who is asking."
    assert played == []


async def test_a_tool_call_after_the_time_limit_is_refused(hass, satellite, played):
    entry = await favorites_router(hass)
    now = [1000.0]
    with patch.object(favorites_context, "_clock", lambda: now[0]):
        context = Context()
        remember(hass, context.id, Asker(entry.entry_id, "owner", satellite.device_id))
        now[0] += ASKER_LIMIT + 1
        api = await llm.async_get_api(hass, DOMAIN, llm_context(context))
        result = await api.async_call_tool(
            llm.ToolInput(tool_name="play_favorite", tool_args={"name": "Radio 2"})
        )

    assert result.data["message"] == "I can't tell who is asking."
    assert played == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("play_favorite", {"name": "Radio 2"}),
        ("save_favorite", {}),
        ("remove_favorite", {"name": "Radio 2"}),
        ("list_favorites", {}),
    ],
)
async def test_every_tool_refuses_when_favorites_are_off_for_the_assistant(
    hass, satellite, played, tool, args
):
    entry = await favorites_router(hass, favorites={"enabled": False})

    result = await call_tool(hass, tool, args, entry=entry, device_id=satellite.device_id)

    assert result.data == {"status": "error", "message": "Favorites are off for this assistant."}
    assert played == []
    assert await names_in(hass, "person:owner") == ["Radio 2"]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("save_favorite", {"name": "Radio 2"}),
        ("remove_favorite", {"name": "Radio 2"}),
        ("list_favorites", {}),
    ],
)
async def test_every_tool_refuses_an_unknown_asker(hass, tool, args):
    entry = await favorites_router(hass)

    result = await call_tool(hass, tool, args, entry=entry, known=False)

    assert result.data["message"] == "I can't tell who is asking."
    assert await names_in(hass, "person:owner") == ["Radio 2"]


# Playing.


@pytest.mark.parametrize(("role", "stream"), [("owner", ALEX_RADIO_2), ("wife", SAM_RADIO_2)])
async def test_play_favorite_plays_the_asking_persons_own_favorite(
    hass, satellite, played, role, stream
):
    entry = await favorites_router(hass)

    result = await call_tool(
        hass, "play_favorite", {"name": "Radio 2"}, entry=entry, role=role,
        device_id=satellite.device_id,
    )

    assert result.error is False
    assert result.data == {"status": "ok", "message": "Playing Radio 2."}
    assert plays(played) == [(satellite.speaker, stream)]


async def test_play_favorite_plays_on_a_named_player(hass, satellite, played):
    entry = await favorites_router(hass)

    result = await call_tool(
        hass, "play_favorite", {"name": "the jazz station", "player": "the Denon"},
        entry=entry, device_id=satellite.device_id,
    )

    assert result.data == {"status": "ok", "message": "Playing Jazz on Denon."}
    assert plays(played) == [(satellite.denon, HOUSEHOLD_JAZZ)]


async def test_play_favorite_tries_the_whole_name_before_a_player_in_it(hass, satellite, played):
    entry = await favorites_router(hass)

    whole = await call_tool(
        hass, "play_favorite", {"name": "Jazz on Sunday"}, entry=entry,
        device_id=satellite.device_id,
    )
    split = await call_tool(
        hass, "play_favorite", {"name": "Jazz on Denon"}, entry=entry,
        device_id=satellite.device_id,
    )

    assert whole.data["message"] == "Playing Jazz on Sunday."
    assert split.data["message"] == "Playing Jazz on Denon."
    assert plays(played) == [
        (satellite.speaker, HOUSEHOLD_JAZZ + "?sunday"), (satellite.denon, HOUSEHOLD_JAZZ),
    ]


async def test_play_favorite_uses_the_speaker_override_of_the_satellite(hass, satellite, played):
    entry = await favorites_router(hass)
    await async_set_speaker_overrides(hass, {satellite.device_id: satellite.denon})

    await call_tool(
        hass, "play_favorite", {"name": "Radio 2"}, entry=entry, device_id=satellite.device_id
    )

    assert plays(played) == [(satellite.denon, ALEX_RADIO_2)]


async def test_play_favorite_falls_back_to_the_device_of_the_llm_context(
    hass, satellite, played
):
    entry = await favorites_router(hass)

    await call_tool(
        hass, "play_favorite", {"name": "Radio 2"}, entry=entry,
        context_device_id=satellite.device_id,
    )

    assert plays(played) == [(satellite.speaker, ALEX_RADIO_2)]


async def test_play_favorite_says_when_it_is_no_favorite(hass, satellite, played):
    entry = await favorites_router(hass)

    result = await call_tool(
        hass, "play_favorite", {"name": "Queen"}, entry=entry, device_id=satellite.device_id
    )

    assert result.error is True
    assert result.data == {
        "status": "error",
        "message": "Queen is not one of your favorites or the household's.",
    }
    assert played == []


@pytest.mark.parametrize(
    ("args", "device", "message"),
    [
        ({"name": "Radio 2", "player": "Garage"}, True, "I can't find a player called Garage."),
        ({"name": "Radio 2"}, False, "There is no speaker for this satellite. Say which player "
         "to use."),
        ({"name": "Deep Focus"}, True, "Deep Focus needs a Music Assistant speaker."),
    ],
)
async def test_play_favorite_returns_why_nothing_plays(
    hass, satellite, played, args, device, message
):
    entry = await favorites_router(hass)

    result = await call_tool(
        hass, "play_favorite", args, entry=entry,
        device_id=satellite.device_id if device else None,
    )

    assert result.data == {"status": "error", "message": message}
    assert played == []


async def test_regression_play_favorite_in_a_conflict_says_only_the_household_was_searched(
    hass, satellite, played
):
    entry = await favorites_router(hass)
    await add_router(
        hass, options_with(**FAVORITES_ON, owner={"name": "Sam"}), title="Office"
    )

    result = await call_tool(
        hass, "play_favorite", {"name": "Radio 2"}, entry=entry, device_id=satellite.device_id
    )

    assert result.data == {
        "status": "error",
        "message": "Radio 2 is not one of the household's favorites.",
    }
    assert played == []


# Saving.


def playing_stream(hass: HomeAssistant, entity_id: str, title: str = "New Station") -> None:
    hass.states.async_set(
        entity_id,
        "playing",
        {"media_content_id": NEW_STREAM, "media_content_type": "music", "media_title": title},
    )


async def test_save_favorite_saves_what_plays_under_the_name_given(hass, satellite):
    entry = await favorites_router(hass)
    playing_stream(hass, satellite.speaker, title="Some song")

    result = await call_tool(
        hass, "save_favorite", {"this": True, "name": "Morning radio"}, entry=entry,
        device_id=satellite.device_id,
    )

    assert result.data == {
        "status": "ok",
        "message": "Saved Morning radio to your favorites.",
        "saved": {"name": "Morning radio", "list": "yours"},
    }
    store = await async_get_favorites(hass)
    assert store.items("person:owner")[-1]["url"] == NEW_STREAM


async def test_regression_save_favorite_names_a_music_assistant_radio_as_told(hass, satellite):
    entry = await favorites_router(hass)
    radio = add_player(
        hass, "kitchen_radio", MA, device_class="speaker", state="playing",
        media_content_id="library://radio/12", media_content_type="music",
        media_title="Queen - Bohemian Rhapsody",
    )
    await async_set_speaker_overrides(hass, {satellite.device_id: radio})

    result = await call_tool(
        hass, "save_favorite", {"this": True, "name": "Jazz FM"}, entry=entry,
        device_id=satellite.device_id,
    )

    assert result.data["saved"] == {"name": "Jazz FM", "list": "yours"}
    saved = (await async_get_favorites(hass)).items("person:owner")[-1]
    assert (saved["name"], saved["uri"]) == ("Jazz FM", "library://radio/12")


@pytest.mark.parametrize(
    "args",
    [
        {"this": True},
        {"this": True, "name": "  "},
        {},
        {"name": "this"},
        {"name": "What's playing?"},
        {"this": True, "name": "that"},
    ],
    ids=["this", "this, blank name", "nothing", "name this", "name what's playing", "this that"],
)
async def test_regression_save_favorite_asks_for_a_name_and_saves_nothing(
    hass, satellite, args
):
    entry = await favorites_router(hass)
    playing_stream(hass, satellite.speaker)

    result = await call_tool(
        hass, "save_favorite", args, entry=entry, device_id=satellite.device_id
    )

    assert result.error is False
    assert result.data == {"status": "need_name", "message": "What should I call it?"}
    assert await names_in(hass, "person:owner") == ["Radio 2"]
    assert "New Station" not in await names_in(hass, "household")


async def test_save_favorite_for_the_household(hass, satellite):
    entry = await favorites_router(hass)
    playing_stream(hass, satellite.speaker)

    result = await call_tool(
        hass, "save_favorite", {"this": True, "name": "New Station", "household": True},
        entry=entry, role="wife", device_id=satellite.device_id,
    )

    assert result.data["saved"] == {"name": "New Station", "list": "household"}
    assert result.data["message"] == "Saved New Station to the household's favorites."
    assert "New Station" in await names_in(hass, "household")
    assert "New Station" not in await names_in(hass, "person:wife")


async def test_save_favorite_says_when_nothing_plays(hass, satellite):
    entry = await favorites_router(hass)

    result = await call_tool(
        hass, "save_favorite", {"this": True, "name": "Morning radio"}, entry=entry,
        device_id=satellite.device_id,
    )

    assert result.data == {"status": "error", "message": "Nothing is playing that I can save."}


async def test_save_favorite_by_name_saves_the_music_assistant_match(hass, satellite):
    entry = await favorites_router(hass)
    await add_music_assistant(hass, {"radio": [item("BBC Radio 4", "radio", "library://radio/4")]})

    result = await call_tool(
        hass, "save_favorite", {"name": "Radio 4"}, entry=entry, device_id=satellite.device_id
    )

    assert result.data["saved"] == {"name": "BBC Radio 4", "list": "yours"}
    store = await async_get_favorites(hass)
    saved = store.items("person:owner")[-1]
    assert (saved["uri"], saved["aliases"]) == ("library://radio/4", ["Radio 4"])


async def test_save_favorite_by_name_offers_candidates_without_saving(hass, satellite):
    entry = await favorites_router(hass)
    await add_music_assistant(hass, {"radio": [item("Radio 4 Extra", "radio", "library://r/5")]})

    result = await call_tool(
        hass, "save_favorite", {"name": "Radio 4"}, entry=entry, device_id=satellite.device_id
    )

    assert result.error is False
    assert result.data["status"] == "choose"
    assert result.data["candidates"] == ["Radio 4 Extra"]
    assert await names_in(hass, "person:owner") == ["Radio 2"]


async def test_save_favorite_by_name_says_when_nothing_is_found(hass, satellite):
    entry = await favorites_router(hass)
    await add_music_assistant(hass, {})

    result = await call_tool(
        hass, "save_favorite", {"name": "Radio 4"}, entry=entry, device_id=satellite.device_id
    )

    assert result.data == {"status": "error", "message": "I couldn't find Radio 4."}


async def test_save_favorite_by_name_without_music_assistant_says_so(hass, satellite):
    entry = await favorites_router(hass)

    result = await call_tool(
        hass, "save_favorite", {"name": "Radio 4"}, entry=entry, device_id=satellite.device_id
    )

    assert result.data == {
        "status": "error",
        "message": "I can only save that by name with Music Assistant.",
    }


async def test_save_favorite_says_when_the_name_is_taken(hass, satellite):
    entry = await favorites_router(hass)
    playing_stream(hass, satellite.speaker)

    result = await call_tool(
        hass, "save_favorite", {"this": True, "name": "Radio 2"}, entry=entry,
        device_id=satellite.device_id,
    )

    assert result.data == {
        "status": "error",
        "message": "There is already a favorite called Radio 2.",
    }


async def test_save_favorite_says_when_the_list_is_full(hass, satellite):
    entry = await favorites_router(hass)
    store = await async_get_favorites(hass)
    for number in range(99):
        await store.async_add("person:owner", {"name": f"Station {number}", "url": NEW_STREAM})
    playing_stream(hass, satellite.speaker)

    result = await call_tool(
        hass, "save_favorite", {"this": True, "name": "New Station"}, entry=entry,
        device_id=satellite.device_id,
    )

    assert result.data == {"status": "error", "message": "That list is full."}


async def test_save_favorite_refuses_the_persons_list_in_a_conflict(hass, satellite):
    entry = await favorites_router(hass)
    await add_router(
        hass, options_with(**FAVORITES_ON, owner={"name": "Sam"}), title="Office"
    )
    playing_stream(hass, satellite.speaker)

    this = {"this": True, "name": "New Station"}
    own = await call_tool(hass, "save_favorite", this, entry=entry, device_id=satellite.device_id)
    household = await call_tool(
        hass, "save_favorite", {**this, "household": True}, entry=entry,
        device_id=satellite.device_id,
    )

    assert own.data == {
        "status": "error",
        "message": "Your own favorites are not available until every assistant agrees who "
        "you are.",
    }
    assert household.data["status"] == "ok"
    assert await names_in(hass, "person:owner") == ["Radio 2"]


# Removing and listing.


async def test_remove_favorite_removes_from_the_persons_own_list(hass):
    entry = await favorites_router(hass)

    result = await call_tool(hass, "remove_favorite", {"name": "radio 2"}, entry=entry)

    assert result.data == {"status": "ok", "message": "Removed Radio 2 from your favorites."}
    assert await names_in(hass, "person:owner") == []
    assert await names_in(hass, "person:wife") == ["Radio 2"]


async def test_remove_favorite_by_another_name_from_the_household(hass):
    entry = await favorites_router(hass)

    result = await call_tool(
        hass, "remove_favorite", {"name": "the jazz station", "household": True}, entry=entry
    )

    assert result.data["message"] == "Removed Jazz from the household's favorites."
    assert "Jazz" not in await names_in(hass, "household")


async def test_remove_favorite_looks_in_one_list_only(hass):
    entry = await favorites_router(hass)

    result = await call_tool(hass, "remove_favorite", {"name": "Jazz"}, entry=entry)

    assert result.data == {"status": "error", "message": "Jazz is not one of your favorites."}
    assert "Jazz" in await names_in(hass, "household")


async def test_list_favorites_gives_the_persons_names_only(hass):
    entry = await favorites_router(hass)
    store = await async_get_favorites(hass)
    await store.async_add("person:owner", {"name": "News", "aliases": ["headlines"],
                                            "url": NEW_STREAM})

    result = await call_tool(hass, "list_favorites", {}, entry=entry)

    assert result.data == {
        "status": "ok",
        "message": "Your favorites.",
        "favorites": [
            {"name": "Radio 2", "aliases": []},
            {"name": "News", "aliases": ["headlines"]},
        ],
    }


async def test_list_favorites_of_the_household(hass):
    entry = await favorites_router(hass)

    result = await call_tool(hass, "list_favorites", {"household": True}, entry=entry, role="wife")

    assert [favorite["name"] for favorite in result.data["favorites"]] == [
        "Jazz", "Jazz on Sunday", "Deep Focus",
    ]
    assert result.data["message"] == "The household's favorites."


async def test_list_favorites_never_gives_the_other_persons_list(hass):
    entry = await favorites_router(hass)
    store = await async_get_favorites(hass)
    await store.async_add("person:wife", {"name": "Sams Secret", "url": NEW_STREAM})

    result = await call_tool(hass, "list_favorites", {}, entry=entry, role="owner")

    assert "Sams Secret" not in str(result.data)


# Status lines.


def favorites_lines(hass: HomeAssistant, entry: MockConfigEntry) -> list[tuple[str, str]]:
    return [
        (line["level"], line["text"])
        for line in router_status(hass, entry, entry.options)
        if line["section"] == "favorites"
    ]


def main_agent_of(hass: HomeAssistant, domain: str, apis: Any, *, subentry: bool) -> None:
    """Make MAIN_AGENT an entity of a config entry whose llm_hass_api is `apis`."""
    settings = {} if apis is None else {"llm_hass_api": apis}
    if subentry:
        config_entry = MockConfigEntry(
            domain=domain,
            subentries_data=[{
                "data": settings, "subentry_id": "sub1", "subentry_type": "conversation",
                "title": "Main", "unique_id": None,
            }],
        )
    else:
        config_entry = MockConfigEntry(domain=domain, options=settings)
    config_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "conversation",
        domain,
        "main",
        suggested_object_id="main_agent",
        config_entry=config_entry,
        config_subentry_id="sub1" if subentry else None,
    )


WARNING = (
    "warn",
    "Favorites are on, but the main agent cannot use them: switch on Voice Assistant "
    "Manager next to Assist in its settings.",
)


@pytest.mark.parametrize(
    ("apis", "subentry", "warned"),
    [
        (["assist"], True, True),
        ("assist", False, True),
        (None, True, True),
        (["assist", DOMAIN], True, False),
        (DOMAIN, False, False),
    ],
)
async def test_status_warns_when_the_main_agent_lacks_the_api(
    hass, apis, subentry, warned
):
    main_agent_of(hass, "openai_conversation", apis, subentry=subentry)
    entry = await add_router(hass, options_with(**FAVORITES_ON))

    assert (WARNING in favorites_lines(hass, entry)) is warned


async def test_status_does_not_check_the_api_with_favorites_off(hass):
    main_agent_of(hass, "openai_conversation", ["assist"], subentry=True)
    entry = await add_router(hass)

    assert favorites_lines(hass, entry) == []


async def test_status_skips_the_api_check_for_an_agent_without_a_config_entry(hass):
    entry = await add_router(hass, options_with(**FAVORITES_ON))

    assert favorites_lines(hass, entry) == []
    assert er.async_get(hass).async_get(MAIN_AGENT) is None


async def test_status_names_the_assistants_in_a_person_list_conflict(hass):
    entry = await add_router(hass, options_with(**FAVORITES_ON))
    await add_router(hass, options_with(**FAVORITES_ON, owner={"name": "Sam"}), title="Office")

    assert favorites_lines(hass, entry) == [(
        "error",
        "Person 1 is not the same person on every assistant with favorites on (Kitchen "
        "assistant, Office), so their own favorites are not used. The household's still are.",
    )]
