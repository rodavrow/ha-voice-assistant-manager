# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the favorites lists in favorites.py."""

import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry, flush_store

from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.favorites import (
    MAX_ALIASES,
    MAX_FAVORITES,
    MAX_NAME_LENGTH,
    MAX_URL_LENGTH,
    STORAGE_KEY,
    STORAGE_VERSION,
    Favorite,
    FavoriteError,
    FavoritesStore,
    async_get_favorites,
    favorites_on,
    find,
    lists_for,
    normalise,
    parse_play,
    person_list_conflicts,
    person_list_user,
    validate,
)

RADIO_2 = {"name": "Radio 2", "url": "https://stream.example.com/radio2.mp3"}
JAZZ = {"name": "Jazz FM", "uri": "library://radio/12", "kind": "radio"}


def error_key(data: dict[str, Any]) -> str:
    with pytest.raises(FavoriteError) as caught:
        validate(data)
    return caught.value.key


def fav(name: str, *aliases: str) -> Favorite:
    return Favorite(
        id=name,
        name=name,
        aliases=list(aliases),
        kind="stream",
        uri="",
        url="https://stream.example.com/s",
        image="",
    )


# Normalising names.


def test_normalise_ignores_case_punctuation_and_a_leading_the():
    assert normalise("The Radio 2!") == normalise("radio 2") == "radio 2"


def test_normalise_drops_apostrophes_and_collapses_spaces():
    assert normalise("  Alex's   Mix ") == "alexs mix"


def test_normalise_keeps_the_inside_a_word():
    assert normalise("Theatre FM") == "theatre fm"


def test_normalise_keeps_a_lone_the():
    assert normalise("The") == "the"


# Validating one favorite.


def test_validate_trims_and_fills_the_defaults_for_a_stream_url():
    assert validate({"name": "  Radio 2 ", "url": " https://stream.example.com/r2 "}) == {
        "id": "",
        "name": "Radio 2",
        "aliases": [],
        "kind": "stream",
        "uri": "",
        "url": "https://stream.example.com/r2",
        "image": "",
    }


def test_validate_keeps_a_music_assistant_uri_kind_and_image():
    favorite = validate({**JAZZ, "image": "https://img.example.com/jazz.png"})

    assert (favorite["uri"], favorite["url"], favorite["kind"], favorite["image"]) == (
        "library://radio/12",
        "",
        "radio",
        "https://img.example.com/jazz.png",
    )


def test_validate_trims_aliases_and_drops_empty_ones():
    favorite = validate({**RADIO_2, "aliases": [" Two ", "", "  ", "Wogan FM"]})

    assert favorite["aliases"] == ["Two", "Wogan FM"]


@pytest.mark.parametrize("name", ["", "   ", "!!!", "x" * (MAX_NAME_LENGTH + 1), 7, None])
def test_validate_rejects_a_bad_name(name: Any):
    assert error_key({**RADIO_2, "name": name}) == "bad_name"


def test_validate_accepts_a_name_of_exactly_the_maximum_length():
    assert validate({**RADIO_2, "name": "x" * MAX_NAME_LENGTH})["name"] == "x" * MAX_NAME_LENGTH


def test_validate_rejects_too_many_aliases():
    aliases = [f"Alias {n}" for n in range(MAX_ALIASES + 1)]

    assert error_key({**RADIO_2, "aliases": aliases}) == "too_many_aliases"


def test_validate_counts_aliases_after_dropping_empty_ones():
    aliases = ["", *[f"Alias {n}" for n in range(MAX_ALIASES)]]

    assert len(validate({**RADIO_2, "aliases": aliases})["aliases"]) == MAX_ALIASES


@pytest.mark.parametrize("aliases", [["x" * (MAX_NAME_LENGTH + 1)], [3], "Two"])
def test_validate_rejects_a_bad_alias(aliases: Any):
    assert error_key({**RADIO_2, "aliases": aliases}) == "bad_name"


def test_validate_rejects_an_alias_equal_to_the_name():
    assert error_key({**RADIO_2, "aliases": ["the radio 2."]}) == "name_taken"


def test_validate_rejects_two_equal_aliases():
    assert error_key({**RADIO_2, "aliases": ["Two", "two!"]}) == "name_taken"


@pytest.mark.parametrize(
    "source",
    [{}, {"url": "", "uri": ""}, {"url": "https://a/b", "uri": "library://radio/1"}],
)
def test_validate_needs_exactly_one_source(source: dict[str, str]):
    assert error_key({"name": "Radio 2", **source}) == "no_source"


@pytest.mark.parametrize(
    "source",
    [
        {"url": "ftp://stream.example.com/r2"},
        {"url": "stream.example.com/r2"},
        {"url": "https://"},
        {"url": 5},
    ],
)
def test_validate_rejects_a_bad_stream_address(source: dict[str, Any]):
    assert error_key({"name": "Radio 2", **source}) == "bad_url"


def long_url(length: int) -> str:
    prefix = "https://stream.example.com/"
    return prefix + "x" * (length - len(prefix))


def test_the_address_limit_is_the_one_the_design_sets():
    assert MAX_URL_LENGTH == 2000


def test_validate_accepts_a_stream_address_of_exactly_the_maximum_length():
    url = long_url(MAX_URL_LENGTH)

    assert validate({"name": "Radio 2", "url": url})["url"] == url


def test_validate_rejects_a_stream_address_over_the_maximum_length():
    assert error_key({"name": "Radio 2", "url": long_url(MAX_URL_LENGTH + 1)}) == "bad_url"


@pytest.mark.parametrize(
    "uri",
    [
        "radio 12",
        "library://",
        5,
        "file:///etc/passwd",
        "http://stream.example.com/r2",
        "HTTPS://stream.example.com/r2",
        "media-source://media_source/local/x.mp3",
    ],
)
def test_validate_rejects_a_bad_music_assistant_item(uri: Any):
    assert error_key({"name": "Radio 2", "uri": uri}) == "bad_uri"


@pytest.mark.parametrize("image", ["javascript:alert(1)", "data:image/png;base64,AA", 4, "/"])
def test_validate_rejects_a_bad_image(image: Any):
    assert error_key({**RADIO_2, "image": image}) == "bad_url"


def test_validate_accepts_an_image_path_on_home_assistant():
    assert validate({**RADIO_2, "image": "/api/image/x.png"})["image"] == "/api/image/x.png"


def test_validate_rejects_an_unknown_kind():
    assert error_key({**RADIO_2, "kind": "video"}) == "bad_kind"


def test_favorite_error_is_a_value_error_with_a_key():
    with pytest.raises(ValueError) as caught:
        validate({})

    assert isinstance(caught.value, FavoriteError)
    assert caught.value.key == "bad_name"


# Finding a favorite.


def test_find_prefers_the_first_list():
    mine, household = fav("Radio 2"), fav("radio 2")

    assert find([[mine], [household]], "the radio 2") is mine


def test_find_falls_back_to_the_next_list():
    household = fav("Jazz FM")

    assert find([[fav("Radio 2")], [household]], "jazz fm") is household


def test_find_matches_an_alias():
    favorite = fav("Radio 2", "Wogan FM")

    assert find([[favorite]], "Wogan FM!") is favorite


def test_find_returns_none_when_no_list_has_the_name():
    assert find([[fav("Radio 2")], []], "a game") is None


def test_find_returns_none_for_an_empty_name():
    assert find([[fav("Radio 2")]], " ?") is None


# Reading a play request.


@pytest.mark.parametrize(
    ("text", "pairs"),
    [
        ("play Radio 2", [("Radio 2", None)]),
        ("please play radio 2.", [("radio 2", None)]),
        ("Can you play Radio 2?", [("Radio 2", None)]),
        ("could you please play Radio 2", [("Radio 2", None)]),
        ("Would you play Radio 2, please!", [("Radio 2", None)]),
        ("will you play Radio 2 please", [("Radio 2", None)]),
        ("play Pretty Please please.", [("Pretty Please", None)]),
        ("play kitchen radio", [("kitchen radio", None)]),
        ("play a game", [("a game", None)]),
        (
            "play Radio 2 on the Denon",
            [("Radio 2 on the Denon", None), ("Radio 2", "the Denon")],
        ),
        (
            "play Radio 2 in the kitchen",
            [("Radio 2 in the kitchen", None), ("Radio 2", "the kitchen")],
        ),
        (
            "play Live on Air on the Denon",
            [("Live on Air on the Denon", None), ("Live on Air", "the Denon")],
        ),
    ],
)
def test_parse_play_returns_the_candidates_in_try_order(
    text: str, pairs: list[tuple[str, str | None]]
):
    assert parse_play(text) == pairs


@pytest.mark.parametrize(
    "text", ["what is playing", "playing Radio 2", "play", "please play.", "stop", ""]
)
def test_parse_play_returns_nothing_when_it_is_not_a_play_request(text: str):
    assert parse_play(text) == []


def test_parse_play_of_a_game_is_not_found_as_a_favorite():
    lists = [[fav("Radio 2")], [fav("Jazz FM")]]

    assert [find(lists, name) for name, _player in parse_play("play a game")] == [None]


# The settings switch.


@pytest.mark.parametrize(
    ("options", "on"),
    [
        ({}, False),
        ({"favorites": {"enabled": False}}, False),
        ({"favorites": {"enabled": True}}, True),
        ({"favorites": "yes"}, False),
    ],
)
def test_favorites_on_follows_the_switch(hass: HomeAssistant, options: dict[str, Any], on: bool):
    entry = MockConfigEntry(domain=DOMAIN, title="Kitchen assistant", options=options)

    assert favorites_on(entry) is on


# Whose list a person's list is (Review Focus 4).


def assistant(
    hass: HomeAssistant,
    title: str,
    user: str,
    name: str,
    *,
    favorites: bool = True,
    use_global: bool = True,
) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        title=title,
        options={
            "owner": {"user": user, "name": name},
            "favorites": {"enabled": favorites},
            "memory": {"use_global": use_global},
        },
    ).add_to_hass(hass)


async def test_person_list_conflicts_when_two_people_hold_the_role(hass: HomeAssistant):
    assistant(hass, "Kitchen assistant", "u-alex", "Alex")
    assistant(hass, "Office", "u-sam", "Sam")

    assert person_list_conflicts(hass, "owner") == ["Kitchen assistant", "Office"]
    assert person_list_user(hass, "owner") is None
    assert lists_for(hass, "owner") == ["household"]


async def test_person_list_agrees_when_the_role_is_the_same_person(hass: HomeAssistant):
    assistant(hass, "Kitchen assistant", "u-alex", "Alex")
    assistant(hass, "Office", "u-alex", "alex")

    assert person_list_conflicts(hass, "owner") == []
    assert person_list_user(hass, "owner") == "u-alex"
    assert lists_for(hass, "owner") == ["person:owner", "household"]


async def test_an_assistant_with_favorites_off_does_not_count(hass: HomeAssistant):
    assistant(hass, "Kitchen assistant", "u-alex", "Alex")
    assistant(hass, "Office", "u-sam", "Sam", favorites=False)

    assert person_list_conflicts(hass, "owner") == []
    assert person_list_user(hass, "owner") == "u-alex"


async def test_memorys_shared_list_switch_does_not_matter(hass: HomeAssistant):
    assistant(hass, "Kitchen assistant", "u-alex", "Alex", use_global=False)
    assistant(hass, "Office", "u-sam", "Sam", use_global=False)

    assert person_list_conflicts(hass, "owner") == ["Kitchen assistant", "Office"]


async def test_person_list_user_is_none_when_no_assistant_links_a_user(hass: HomeAssistant):
    assistant(hass, "Kitchen assistant", "", "Alex")

    assert person_list_user(hass, "owner") is None
    assert lists_for(hass, "owner") == ["person:owner", "household"]


async def test_lists_for_the_second_person_use_their_own_key(hass: HomeAssistant):
    assert lists_for(hass, "wife") == ["person:wife", "household"]


# The store.


@pytest.fixture
async def store(hass: HomeAssistant) -> FavoritesStore:
    return await async_get_favorites(hass)


async def test_add_gives_an_id_and_keeps_the_favorite(store: FavoritesStore):
    added = await store.async_add("person:owner", RADIO_2)

    assert added["id"]
    assert store.items("person:owner") == [added]


async def test_add_ignores_an_id_in_the_data(store: FavoritesStore):
    added = await store.async_add("household", {**RADIO_2, "id": "mine"})

    assert added["id"] != "mine"


async def test_add_validates(store: FavoritesStore):
    with pytest.raises(FavoriteError) as caught:
        await store.async_add("household", {"name": "Radio 2"})

    assert caught.value.key == "no_source"
    assert store.items("household") == []


async def test_add_refuses_a_name_already_in_the_list(store: FavoritesStore):
    await store.async_add("household", RADIO_2)

    with pytest.raises(FavoriteError) as caught:
        await store.async_add("household", {**JAZZ, "name": "The Radio 2!"})

    assert caught.value.key == "name_taken"


async def test_add_refuses_an_alias_that_is_another_favorites_name(store: FavoritesStore):
    await store.async_add("household", RADIO_2)

    with pytest.raises(FavoriteError) as caught:
        await store.async_add("household", {**JAZZ, "aliases": ["radio 2"]})

    assert caught.value.key == "name_taken"


async def test_a_person_and_the_household_may_share_a_name(store: FavoritesStore):
    await store.async_add("person:owner", RADIO_2)
    await store.async_add("household", RADIO_2)

    assert [len(store.items(key)) for key in ("person:owner", "household")] == [1, 1]


async def test_add_refuses_more_than_the_maximum(store: FavoritesStore):
    for number in range(MAX_FAVORITES):
        await store.async_add("household", {**RADIO_2, "name": f"Station {number}"})

    with pytest.raises(FavoriteError) as caught:
        await store.async_add("household", JAZZ)

    assert caught.value.key == "too_many"
    assert len(store.items("household")) == MAX_FAVORITES


async def test_items_returns_copies(store: FavoritesStore):
    await store.async_add("household", {**RADIO_2, "aliases": ["Two"]})

    store.items("household")[0]["aliases"].append("Changed")

    assert store.items("household")[0]["aliases"] == ["Two"]


async def test_unknown_list_key_is_refused(store: FavoritesStore):
    with pytest.raises(ValueError):
        store.items("person:stranger")  # type: ignore[arg-type]


async def test_update_changes_the_name_and_keeps_the_id(store: FavoritesStore):
    added = await store.async_add("household", RADIO_2)

    updated = await store.async_update(
        "household", added["id"], {"name": "Radio Two", "aliases": ["Radio 2"]}
    )

    assert (updated["id"], updated["name"], updated["aliases"], updated["url"]) == (
        added["id"],
        "Radio Two",
        ["Radio 2"],
        RADIO_2["url"],
    )
    assert store.items("household") == [updated]


async def test_update_may_replace_the_source(store: FavoritesStore):
    added = await store.async_add("household", RADIO_2)

    updated = await store.async_update("household", added["id"], {"uri": "library://radio/2"})

    assert (updated["uri"], updated["url"]) == ("library://radio/2", "")


async def test_update_refuses_another_favorites_name(store: FavoritesStore):
    await store.async_add("household", RADIO_2)
    jazz = await store.async_add("household", JAZZ)

    with pytest.raises(FavoriteError) as caught:
        await store.async_update("household", jazz["id"], {"aliases": ["Radio 2"]})

    assert caught.value.key == "name_taken"
    assert store.items("household")[1] == jazz


async def test_update_of_an_unknown_id_is_not_found(store: FavoritesStore):
    with pytest.raises(FavoriteError) as caught:
        await store.async_update("household", "missing", {"name": "Radio 2"})

    assert caught.value.key == "not_found"


async def test_remove_deletes_the_favorite(store: FavoritesStore):
    radio = await store.async_add("household", RADIO_2)
    jazz = await store.async_add("household", JAZZ)

    await store.async_remove("household", radio["id"])

    assert store.items("household") == [jazz]


async def test_remove_of_an_unknown_id_is_not_found(store: FavoritesStore):
    with pytest.raises(FavoriteError) as caught:
        await store.async_remove("household", "missing")

    assert caught.value.key == "not_found"


async def test_favorites_survive_a_restart(
    hass: HomeAssistant, store: FavoritesStore, hass_storage
):
    added = await store.async_add("person:wife", JAZZ)
    await flush_store(store.store)

    reloaded = FavoritesStore(hass)
    await reloaded.async_load()

    assert reloaded.items("person:wife") == [added]
    assert hass_storage[STORAGE_KEY]["version"] == STORAGE_VERSION


@pytest.mark.parametrize("data", [{"lists": []}, ["nonsense"], {"other": {}}])
async def test_a_damaged_file_loads_as_empty_lists(
    hass: HomeAssistant, hass_storage, caplog, data: Any
):
    hass_storage[STORAGE_KEY] = {"version": STORAGE_VERSION, "key": STORAGE_KEY, "data": data}
    store = FavoritesStore(hass)

    with caplog.at_level(logging.WARNING):
        await store.async_load()

    assert [store.items(key) for key in ("person:owner", "person:wife", "household")] == [
        [],
        [],
        [],
    ]
    assert "favorites" in caplog.text


async def test_a_damaged_favorite_in_the_file_is_skipped(
    hass: HomeAssistant, hass_storage, caplog
):
    good = {**validate(RADIO_2), "id": "01good"}
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "lists": {
                "household": [good, {"name": "No source"}, "junk", {**good, "id": "01dup"}],
                "person:stranger": [good],
            }
        },
    }
    store = FavoritesStore(hass)

    with caplog.at_level(logging.WARNING):
        await store.async_load()

    assert store.items("household") == [good]
    assert "Skipping a favorite in household" in caplog.text
    assert "'person:stranger'" in caplog.text


async def test_ids_given_on_load_are_saved(hass: HomeAssistant, hass_storage):
    no_id = {k: v for k, v in validate(RADIO_2).items() if k != "id"}
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "key": STORAGE_KEY,
        "data": {"lists": {"household": [no_id]}},
    }
    store = FavoritesStore(hass)
    await store.async_load()

    await flush_store(store.store)

    (loaded,) = store.items("household")
    assert hass_storage[STORAGE_KEY]["data"]["lists"]["household"] == [loaded]


async def test_async_get_favorites_returns_one_store(hass: HomeAssistant):
    assert await async_get_favorites(hass) is await async_get_favorites(hass)
