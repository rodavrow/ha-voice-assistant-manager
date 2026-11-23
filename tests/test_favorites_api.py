# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the favorites commands of both pages: My assistant and Voice Manager."""

import json
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant.common import MockConfigEntry, MockUser

from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.favorites import (
    MAX_FAVORITES,
    ListKey,
    async_get_favorites,
)
from custom_components.voice_assistant_manager.panel import speaker_overrides

from .router_helpers import add_router, options_with
from .test_favorites_llm import main_agent_of
from .test_favorites_play import MA, add_music_assistant, add_player, item
from .test_mine_api import Mine
from .test_panel import Page
from .test_setup_guide import add_satellite

ALEX_RADIO = "https://stream.example.com/alex.mp3"
SAM_RADIO = "https://stream.example.com/sam.mp3"
NEWS = "https://stream.example.com/news.mp3"
NOT_LINKED = (
    "Favorites are kept for the people of an assistant. Ask an administrator to choose "
    "your Home Assistant user under People on the Voice Manager page."
)
CONFLICT = (
    "Your favorites are not shared until every assistant agrees who you are; ask an "
    "administrator."
)
NOT_FOUND = "That favorite is not in the list any more. Reload the page."
FAVORITES_OFF = "Favorites are off for your assistants. Ask an administrator to switch them on."
BAD_ADDRESS = (
    "An address must start with http:// or https:// (an image may also be a path on Home "
    "Assistant starting with /), have no spaces and be 2000 characters at most."
)
NO_MUSIC_ASSISTANT = (
    "Searching needs Music Assistant, which is not set up. Add the favorite with a stream "
    "address instead."
)


@pytest.fixture
async def mine(hass: HomeAssistant, hass_ws_client: Any, hass_read_only_access_token: str) -> Mine:
    return Mine(await hass_ws_client(hass, access_token=hass_read_only_access_token))


@pytest.fixture
async def admin(hass: HomeAssistant, hass_ws_client: Any) -> Page:
    return Page(await hass_ws_client(hass))


@pytest.fixture(autouse=True)
async def music_assistant_entries_not_unloaded(hass: HomeAssistant) -> Any:
    """Teardown must not unload the faked Music Assistant entries (see test_favorites_play)."""
    yield
    for entry in hass.config_entries.async_entries(MA):
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def assistant(
    hass: HomeAssistant,
    title: str = "Kitchen assistant",
    *,
    owner: str = "u-alex",
    partner: str = "u-sam",
    owner_name: str = "Alex",
    favorites: bool = True,
) -> MockConfigEntry:
    options = options_with(
        owner={"user": owner, "name": owner_name},
        partner={"user": partner, "name": "Sam"},
        favorites={"enabled": favorites},
    )
    return await add_router(hass, options, title)


async def fill(hass: HomeAssistant) -> dict[ListKey, str]:
    """One favorite in each list; returns their ids."""
    store = await async_get_favorites(hass)
    alex = await store.async_add("person:owner", {"name": "Radio 2", "url": ALEX_RADIO})
    sam = await store.async_add("person:wife", {"name": "Sams Secret", "url": SAM_RADIO})
    news = await store.async_add("household", {"name": "News", "url": NEWS})
    return {"person:owner": alex["id"], "person:wife": sam["id"], "household": news["id"]}


async def names_in(hass: HomeAssistant, key: ListKey) -> list[str]:
    return [favorite["name"] for favorite in (await async_get_favorites(hass)).items(key)]


def names(favorites: list[dict[str, Any]] | None) -> list[str] | None:
    return None if favorites is None else [favorite["name"] for favorite in favorites]


# My assistant: who may ask, and which lists they see.


@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("favorites/get", {}),
        ("favorites/add", {"list": "household", "data": {"name": "News", "url": NEWS}}),
        ("favorites/update", {"list": "household", "favorite_id": "x", "data": {"name": "N"}}),
        ("favorites/remove", {"list": "household", "favorite_id": "x"}),
        ("favorites/search", {"name": "Radio 2"}),
    ],
)
async def test_mine_refuses_a_user_linked_nowhere(
    hass: HomeAssistant, mine: Mine, command: str, data: dict[str, Any]
) -> None:
    await assistant(hass)
    await fill(hass)

    assert await mine.refused(command, **data) == NOT_LINKED
    assert await names_in(hass, "household") == ["News"]


async def test_mine_get_gives_the_own_and_household_lists(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await assistant(hass, owner=hass_read_only_user.id)
    await fill(hass)

    result = await mine.ok("favorites/get")

    assert names(result["own"]) == ["Radio 2"]
    assert (result["own_state"], result["conflict"]) == ("ok", False)
    assert names(result["household"]) == ["News"]
    assert "Sams Secret" not in json.dumps(result)


async def test_mine_get_gives_person_2_their_own_list(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await assistant(hass, partner=hass_read_only_user.id)
    await fill(hass)

    result = await mine.ok("favorites/get")

    assert names(result["own"]) == ["Sams Secret"]
    assert "Radio 2" not in json.dumps(result)


async def test_mine_get_hides_the_own_list_in_a_conflict_but_keeps_the_household(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await assistant(hass, owner=hass_read_only_user.id)
    await assistant(hass, "Office", owner="u-other", owner_name="Kim")
    await fill(hass)

    result = await mine.ok("favorites/get")

    assert result["own"] is None
    assert (result["own_state"], result["conflict"]) == ("conflict", True)
    assert names(result["household"]) == ["News"]
    assert "Radio 2" not in json.dumps(result)


async def test_mine_get_has_no_own_list_without_an_assistant_with_favorites_on(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await assistant(hass, owner=hass_read_only_user.id, favorites=False)
    await fill(hass)

    result = await mine.ok("favorites/get")

    assert (result["own"], result["own_state"], result["conflict"]) == (None, "off", False)
    assert names(result["household"]) == ["News"]


async def test_mine_get_is_off_for_a_role_held_only_where_favorites_are_off(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    # Person 1 elsewhere is someone else with favorites on: no conflict for this user.
    await assistant(hass, partner=hass_read_only_user.id, favorites=False)
    await assistant(hass, "Office", owner="u-other", owner_name="Kim")
    await fill(hass)

    result = await mine.ok("favorites/get")

    assert (result["own"], result["own_state"], result["conflict"]) == (None, "off", False)
    assert "Sams Secret" not in json.dumps(result)


async def test_mine_refuses_the_own_list_while_favorites_are_off(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await assistant(hass, owner=hass_read_only_user.id, favorites=False)
    ids = await fill(hass)

    add = await mine.refused("favorites/add", list="own", data={"name": "Jazz", "url": NEWS})
    remove = await mine.refused("favorites/remove", list="own", favorite_id=ids["person:owner"])

    assert add == remove == FAVORITES_OFF
    assert await names_in(hass, "person:owner") == ["Radio 2"]


# My assistant: changing the lists.


async def test_mine_adds_to_the_own_list(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await assistant(hass, owner=hass_read_only_user.id)

    result = await mine.ok("favorites/add", list="own", data={"name": "Jazz", "url": NEWS})

    assert result["ok"] is True
    assert result["favorite"]["name"] == "Jazz"
    assert names(result["items"]) == ["Jazz"]
    assert await names_in(hass, "person:owner") == ["Jazz"]
    assert await names_in(hass, "person:wife") == []


async def test_mine_refuses_the_own_list_in_a_conflict(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await assistant(hass, owner=hass_read_only_user.id)
    await assistant(hass, "Office", owner="u-other", owner_name="Kim")
    ids = await fill(hass)

    add = await mine.refused("favorites/add", list="own", data={"name": "Jazz", "url": NEWS})
    update = await mine.refused(
        "favorites/update",
        list="own",
        favorite_id=ids["person:owner"],
        data={"name": "Mine now"},
    )
    remove = await mine.refused("favorites/remove", list="own", favorite_id=ids["person:owner"])

    assert add == update == remove == CONFLICT
    assert await names_in(hass, "person:owner") == ["Radio 2"]


async def test_mine_adds_changes_and_removes_household_favorites(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await assistant(hass, partner=hass_read_only_user.id)
    ids = await fill(hass)

    await mine.ok("favorites/add", list="household", data={"name": "Jazz", "url": NEWS})
    changed = await mine.ok(
        "favorites/update",
        list="household",
        favorite_id=ids["household"],
        data={"aliases": ["Headlines"]},
    )
    await mine.ok("favorites/remove", list="household", favorite_id=ids["household"])

    assert changed["favorite"]["aliases"] == ["Headlines"]
    assert changed["favorite"]["url"] == NEWS
    assert await names_in(hass, "household") == ["Jazz"]


@pytest.mark.parametrize("which", ["own", "household"])
async def test_mine_never_changes_the_other_persons_favorite(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser, which: str
) -> None:
    await assistant(hass, owner=hass_read_only_user.id)
    ids = await fill(hass)
    sams = ids["person:wife"]

    update = await mine.refused(
        "favorites/update", list=which, favorite_id=sams, data={"name": "Gone"}
    )
    remove = await mine.refused("favorites/remove", list=which, favorite_id=sams)

    assert update == remove == NOT_FOUND
    assert await names_in(hass, "person:wife") == ["Sams Secret"]


@pytest.mark.parametrize("which", ["person:wife", "person:owner", "Household", ""])
async def test_mine_takes_no_list_but_own_or_household(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser, which: str
) -> None:
    await assistant(hass, owner=hass_read_only_user.id)
    ids = await fill(hass)

    answer = await mine.call("favorites/remove", list=which, favorite_id=ids["person:wife"])

    assert answer["success"] is False
    assert await names_in(hass, "person:wife") == ["Sams Secret"]


@pytest.mark.parametrize(
    ("data", "text"),
    [
        ({"name": "Jazz", "url": "ftp://jazz.example.com"}, BAD_ADDRESS),
        ({"name": "Jazz", "url": NEWS, "image": "ftp://img.example.com/x.png"}, BAD_ADDRESS),
        ({"name": "Jazz", "url": "https://example.com/" + "a" * 2000}, BAD_ADDRESS),
        (
            {"name": "J" * 81, "url": NEWS},
            "A name needs a letter or digit and may be 80 characters at most.",
        ),
        (
            {"name": "Jazz", "uri": "file:///music/jazz.mp3"},
            "That Music Assistant item cannot be saved. Search for it again.",
        ),
        (
            {"name": "Jazz", "url": NEWS, "kind": "tv"},
            "Choose what kind of favorite this is: radio, podcast, playlist, album, track "
            "or stream.",
        ),
        (
            {"name": "News", "url": NEWS},
            "That name is already used in this list. Choose another name.",
        ),
        (
            {"name": "Jazz"},
            "A favorite needs either a Music Assistant item or a stream address.",
        ),
        (
            {"name": "Jazz", "url": NEWS, "aliases": ["a", "b", "c", "d", "e", "f"]},
            "A favorite may have 5 other names at most.",
        ),
    ],
)
async def test_mine_says_in_plain_words_why_a_favorite_cannot_be_saved(
    hass: HomeAssistant,
    mine: Mine,
    hass_read_only_user: MockUser,
    data: dict[str, Any],
    text: str,
) -> None:
    await assistant(hass, owner=hass_read_only_user.id)
    await fill(hass)

    assert await mine.refused("favorites/add", list="household", data=data) == text
    assert await names_in(hass, "household") == ["News"]


async def test_mine_says_in_plain_words_when_a_list_is_full(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await assistant(hass, owner=hass_read_only_user.id)
    store = await async_get_favorites(hass)
    for number in range(MAX_FAVORITES):
        await store.async_add("household", {"name": f"Station {number}", "url": NEWS})

    text = await mine.refused("favorites/add", list="household", data={"name": "J", "url": NEWS})

    assert text == "A list may hold 100 favorites at most. Remove one first."


# Searching Music Assistant, from either page.


async def test_mine_search_without_music_assistant_says_so(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await assistant(hass, owner=hass_read_only_user.id)

    result = await mine.ok("favorites/search", name="Radio 2")

    assert result == {"candidates": [], "message": NO_MUSIC_ASSISTANT}


async def test_search_gives_the_best_match_first_as_data_to_save(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await assistant(hass, owner=hass_read_only_user.id)
    await add_music_assistant(
        hass,
        {
            "radio": [
                item("Radio 2 Extra", "radio", "library://radio/3"),
                item("Radio 2", "radio", "library://radio/2", "https://img.example.com/r2.png"),
            ],
            "podcast": [item("Radio 2 Talk", "podcast", "library://podcast/1")],
        },
    )

    result = await mine.ok("favorites/search", name="radio 2")

    assert result["candidates"] == [
        {"name": "Radio 2", "kind": "radio", "uri": "library://radio/2",
         "image": "https://img.example.com/r2.png"},
        {"name": "Radio 2 Extra", "kind": "radio", "uri": "library://radio/3", "image": ""},
        {"name": "Radio 2 Talk", "kind": "podcast", "uri": "library://podcast/1", "image": ""},
    ]
    assert "message" not in result


async def test_search_says_when_nothing_is_found(hass: HomeAssistant, admin: Page) -> None:
    await assistant(hass)
    await add_music_assistant(hass, {})

    result = await admin.ok("favorites/search", name="Radio 2")

    assert result == {
        "candidates": [],
        "message": "Music Assistant found nothing by that name. Try another name.",
    }


async def test_admin_search_without_music_assistant_says_so(
    hass: HomeAssistant, admin: Page
) -> None:
    await assistant(hass)

    result = await admin.ok("favorites/search", name="Radio 2")

    assert result == {"candidates": [], "message": NO_MUSIC_ASSISTANT}


async def test_search_says_when_music_assistant_fails(
    hass: HomeAssistant, admin: Page
) -> None:
    await assistant(hass)
    MockConfigEntry(domain=MA, state=ConfigEntryState.LOADED).add_to_hass(hass)

    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("server gone")

    hass.services.async_register(MA, "search", broken, supports_response=SupportsResponse.ONLY)

    result = await admin.ok("favorites/search", name="Radio 2")

    assert result == {
        "candidates": [],
        "message": "Music Assistant could not search just now. Try again later, or add the "
        "favorite with a stream address.",
    }


async def test_search_shortens_a_long_name_so_the_candidate_saves(
    hass: HomeAssistant, admin: Page
) -> None:
    await assistant(hass)
    long_name = "Radio " + "very " * 20 + "long"
    await add_music_assistant(hass, {"radio": [item(long_name, "radio", "library://radio/8")]})

    (candidate,) = (await admin.ok("favorites/search", name="Radio"))["candidates"]
    saved = await admin.ok("favorites/add", list="household", data=candidate)

    assert candidate["name"] == ("Radio " + "very " * 15).strip()
    assert saved["ok"] is True


@pytest.mark.parametrize("page", ["mine", "admin"])
async def test_search_takes_a_name_of_80_characters_at_most(
    hass: HomeAssistant,
    hass_ws_client: Any,
    hass_read_only_access_token: str,
    hass_read_only_user: MockUser,
    page: str,
) -> None:
    await assistant(hass, owner=hass_read_only_user.id)
    _, calls = await add_music_assistant(hass, {})
    client: Mine | Page = (
        Mine(await hass_ws_client(hass, access_token=hass_read_only_access_token))
        if page == "mine"
        else Page(await hass_ws_client(hass))
    )

    answer = await client.call("favorites/search", name="R" * 81)

    assert answer["success"] is False
    assert calls == []


# Voice Manager: administrators only.


@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("favorites/get", {}),
        ("favorites/add", {"list": "household", "data": {"name": "News", "url": NEWS}}),
        ("favorites/update", {"list": "household", "favorite_id": "x", "data": {}}),
        ("favorites/remove", {"list": "household", "favorite_id": "x"}),
        ("favorites/search", {"name": "Radio 2"}),
        ("favorites/overrides/save", {"overrides": {}}),
    ],
)
async def test_someone_who_is_not_an_admin_cannot_use_the_panel_favorites(
    hass: HomeAssistant,
    hass_ws_client: Any,
    hass_read_only_access_token: str,
    hass_read_only_user: MockUser,
    command: str,
    data: dict[str, Any],
) -> None:
    # Even Person 1 of an assistant is no administrator.
    await assistant(hass, owner=hass_read_only_user.id)
    await fill(hass)
    reader = Page(await hass_ws_client(hass, hass_read_only_access_token))

    answer = await reader.call(command, **data)

    assert answer["success"] is False
    assert answer["error"]["code"] == "unauthorized"
    assert await names_in(hass, "household") == ["News"]


async def test_admin_get_gives_every_list_with_names_and_conflicts(
    hass: HomeAssistant, admin: Page
) -> None:
    await assistant(hass)
    await assistant(hass, "Office", owner="u-other", owner_name="Kim")
    await fill(hass)

    result = await admin.ok("favorites/get")

    assert {key: names(items) for key, items in result["lists"].items()} == {
        "person:owner": ["Radio 2"],
        "person:wife": ["Sams Secret"],
        "household": ["News"],
    }
    assert result["names"] == {
        "person:owner": "Person 1",
        "person:wife": "Sam",
        "household": "Household",
    }
    assert result["conflicts"] == {"owner": ["Kitchen assistant", "Office"], "wife": []}


async def test_admin_get_names_person_1_when_every_assistant_agrees(
    hass: HomeAssistant, admin: Page
) -> None:
    await assistant(hass)
    await assistant(hass, "Office", favorites=False, owner="u-other", owner_name="Kim")

    result = await admin.ok("favorites/get")

    assert result["names"]["person:owner"] == "Alex"
    assert result["conflicts"] == {"owner": [], "wife": []}


async def test_admin_get_gives_the_assistants_and_their_main_agent_api(
    hass: HomeAssistant, admin: Page
) -> None:
    main_agent_of(hass, "openai_conversation", ["assist", DOMAIN], subentry=True)
    kitchen = await assistant(hass)
    office = await assistant(hass, "Office", favorites=False)

    result = await admin.ok("favorites/get")

    assert sorted(result["assistants"], key=lambda row: row["title"]) == [
        {"entry_id": kitchen.entry_id, "title": "Kitchen assistant", "enabled": True,
         "tools_on": True},
        {"entry_id": office.entry_id, "title": "Office", "enabled": False, "tools_on": True},
    ]


async def test_admin_get_says_when_the_main_agent_api_is_off_or_unknown(
    hass: HomeAssistant, admin: Page
) -> None:
    await assistant(hass)

    unknown = (await admin.ok("favorites/get"))["assistants"][0]["tools_on"]
    main_agent_of(hass, "openai_conversation", ["assist"], subentry=False)
    off = (await admin.ok("favorites/get"))["assistants"][0]["tools_on"]

    assert (unknown, off) == (None, False)


async def test_admin_get_gives_satellites_players_and_overrides(
    hass: HomeAssistant, admin: Page
) -> None:
    entry = await assistant(hass)
    device_id = add_satellite(hass, entry)
    on_device = add_player(hass, "kitchen_speaker", device_id=device_id, friendly_name="Kitchen")
    denon = add_player(hass, "denon", friendly_name="Denon")
    add_player(hass, "broken", friendly_name="Broken", state="unavailable")
    bare = add_satellite(hass, entry, sensors=False, name="Hall satellite")
    await admin.ok("favorites/overrides/save", overrides={bare: denon})

    result = await admin.ok("favorites/get")

    assert result["overrides"] == {bare: denon}
    assert sorted(result["satellites"], key=lambda row: row["name"]) == [
        {"device_id": bare, "name": "Hall satellite", "player": None},
        {"device_id": device_id, "name": "Kitchen satellite", "player": on_device},
    ]
    assert sorted(result["players"], key=lambda row: row["name"]) == [
        {"entity_id": denon, "name": "Denon"},
        {"entity_id": on_device, "name": "Kitchen"},
    ]


async def test_admin_adds_changes_and_removes_in_any_list(
    hass: HomeAssistant, admin: Page
) -> None:
    await assistant(hass)
    ids = await fill(hass)

    added = await admin.ok(
        "favorites/add", list="person:wife", data={"name": "Jazz", "url": NEWS}
    )
    changed = await admin.ok(
        "favorites/update",
        list="person:owner",
        favorite_id=ids["person:owner"],
        data={"name": "BBC 2"},
    )
    removed = await admin.ok("favorites/remove", list="household", favorite_id=ids["household"])

    assert names(added["items"]) == ["Sams Secret", "Jazz"]
    assert (changed["favorite"]["name"], changed["favorite"]["url"]) == ("BBC 2", ALEX_RADIO)
    assert removed == {"ok": True, "items": []}
    assert await names_in(hass, "person:owner") == ["BBC 2"]


async def test_admin_gets_plain_words_for_a_favorite_that_is_gone(
    hass: HomeAssistant, admin: Page
) -> None:
    await assistant(hass)
    ids = await fill(hass)

    answer = await admin.ok("favorites/remove", list="household", favorite_id=ids["person:wife"])

    assert answer == {"ok": False, "errors": {"base": NOT_FOUND}}


async def test_admin_cannot_name_a_list_that_does_not_exist(
    hass: HomeAssistant, admin: Page
) -> None:
    await assistant(hass)

    answer = await admin.call("favorites/add", list="person:other", data={"name": "N"})

    assert answer["success"] is False


async def test_admin_saves_the_speaker_overrides(hass: HomeAssistant, admin: Page) -> None:
    await assistant(hass)

    result = await admin.ok(
        "favorites/overrides/save", overrides={"device-1": "media_player.denon"}
    )

    assert result == {"ok": True, "overrides": {"device-1": "media_player.denon"}}
    assert speaker_overrides(hass) == {"device-1": "media_player.denon"}


async def test_admin_is_refused_a_speaker_override_that_is_no_player(
    hass: HomeAssistant, admin: Page
) -> None:
    await assistant(hass)
    await admin.ok("favorites/overrides/save", overrides={"device-1": "media_player.denon"})

    answer = await admin.ok("favorites/overrides/save", overrides={"device-1": "light.kitchen"})

    assert answer == {
        "ok": False,
        "errors": {"base": "A satellite's speaker must be a media player. Choose one again."},
    }
    assert speaker_overrides(hass) == {"device-1": "media_player.denon"}
