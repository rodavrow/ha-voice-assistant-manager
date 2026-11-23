# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for finding the player, playing a favorite and saving one, in favorites_play.py."""

from typing import Any

import probatio
import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager.favorites import Favorite, validate
from custom_components.voice_assistant_manager.favorites_play import (
    PlayRefused,
    async_play,
    async_saveable_now,
    async_search,
    default_player,
    player_name,
    resolve_player,
)

MA = "music_assistant"
RADIO_2_URL = "https://stream.example.com/radio2.mp3"


def favorite(name: str = "Radio 2", **source: str) -> Favorite:
    data: dict[str, Any] = {"name": name, **source}
    if "uri" not in source and "url" not in source:
        data["url"] = RADIO_2_URL
    return validate(data)


def add_player(
    hass: HomeAssistant,
    object_id: str,
    platform: str = "test",
    *,
    friendly_name: str | None = None,
    device_id: str | None = None,
    area_id: str | None = None,
    aliases: list[str] | None = None,
    device_class: str | None = None,
    state: str = "idle",
    **attributes: Any,
) -> str:
    """A media_player entity in the registry with a state; returns its entity id."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "media_player",
        platform,
        f"{platform}-{object_id}",
        suggested_object_id=object_id,
        device_id=device_id,
        original_device_class=device_class,
    )
    if area_id is not None or aliases is not None:
        registry.async_update_entity(
            entry.entity_id,
            area_id=area_id,
            aliases=aliases if aliases is not None else [],
        )
    if friendly_name is not None:
        attributes["friendly_name"] = friendly_name
    hass.states.async_set(entry.entity_id, state, attributes)
    return entry.entity_id


def add_device(hass: HomeAssistant, name: str = "Kitchen satellite") -> str:
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", name)},
        name=name,
    )
    return device.id


def record(
    hass: HomeAssistant,
    domain: str,
    service: str,
    response: Any = None,
    supports_response: SupportsResponse = SupportsResponse.NONE,
) -> list[ServiceCall]:
    """A fake action that records its calls and answers `response` (a dict or a function)."""
    calls: list[ServiceCall] = []

    async def handle(call: ServiceCall) -> Any:
        calls.append(call)
        return response(call) if callable(response) else response

    hass.services.async_register(domain, service, handle, supports_response=supports_response)
    return calls


@pytest.fixture(autouse=True)
async def music_assistant_entries_not_unloaded(hass: HomeAssistant) -> Any:
    """Mark Music Assistant entries not loaded at the end, so teardown does not unload them.

    The tests only fake a loaded entry; unloading would import the real integration.
    """
    yield
    for entry in hass.config_entries.async_entries(MA):
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


def refusal(error: pytest.ExceptionInfo[PlayRefused]) -> str:
    return error.value.reply


# Which player: a named one.


async def test_resolve_player_matches_the_friendly_name(hass: HomeAssistant) -> None:
    denon = add_player(hass, "living_room_receiver", friendly_name="Denon")

    assert resolve_player(hass, "the Denon") == denon


async def test_resolve_player_matches_the_object_id_aliases_and_area(
    hass: HomeAssistant,
) -> None:
    office = ar.async_get(hass).async_create("Office").id
    player = add_player(hass, "desk_speaker", area_id=office, aliases=["Work speaker"])

    assert resolve_player(hass, "desk speaker") == player
    assert resolve_player(hass, "work speaker") == player
    assert resolve_player(hass, "the office") == player


async def test_resolve_player_matches_its_device_area(hass: HomeAssistant) -> None:
    garden = ar.async_get(hass).async_create("Garden").id
    device_id = add_device(hass, "Garden box")
    dr.async_get(hass).async_update_device(device_id, area_id=garden)
    player = add_player(hass, "garden_box", device_id=device_id)

    assert resolve_player(hass, "Garden") == player


async def test_resolve_player_refuses_when_no_player_has_the_name(hass: HomeAssistant) -> None:
    add_player(hass, "kitchen", friendly_name="Kitchen speaker")

    with pytest.raises(PlayRefused) as caught:
        resolve_player(hass, "Denon")

    assert refusal(caught) == "I can't find a player called Denon."


async def test_resolve_player_does_not_match_part_of_a_name(hass: HomeAssistant) -> None:
    add_player(hass, "denon_upstairs", friendly_name="Denon upstairs")

    with pytest.raises(PlayRefused):
        resolve_player(hass, "Denon")


async def test_resolve_player_refuses_when_two_players_have_the_name(
    hass: HomeAssistant,
) -> None:
    add_player(hass, "denon_1", friendly_name="Denon")
    add_player(hass, "denon_2", friendly_name="Denon")

    with pytest.raises(PlayRefused) as caught:
        resolve_player(hass, "Denon")

    assert refusal(caught) == "More than one player is called Denon."


async def test_resolve_player_takes_the_music_assistant_view_of_a_player(
    hass: HomeAssistant,
) -> None:
    add_player(hass, "denon", "denonavr", friendly_name="Denon")
    ma_denon = add_player(hass, "denon_2", MA, friendly_name="Denon", device_class="speaker")

    assert resolve_player(hass, "Denon") == ma_denon


async def test_resolve_player_refuses_a_player_that_is_unavailable(
    hass: HomeAssistant,
) -> None:
    add_player(hass, "denon", friendly_name="Denon", state="unavailable")

    with pytest.raises(PlayRefused) as caught:
        resolve_player(hass, "the Denon")

    assert refusal(caught) == "Denon is not available right now."


async def test_resolve_player_takes_the_available_one_of_two(hass: HomeAssistant) -> None:
    add_player(hass, "denon_1", friendly_name="Denon", state="unavailable")
    available = add_player(hass, "denon_2", friendly_name="Denon")

    assert resolve_player(hass, "Denon") == available


async def test_resolve_player_skips_an_unavailable_music_assistant_twin(
    hass: HomeAssistant,
) -> None:
    native = add_player(hass, "denon", "denonavr", friendly_name="Denon")
    add_player(
        hass, "denon_2", MA, friendly_name="Denon", device_class="speaker", state="unavailable"
    )

    assert resolve_player(hass, "Denon") == native


async def test_resolve_player_ignores_entities_without_a_state(hass: HomeAssistant) -> None:
    er.async_get(hass).async_get_or_create(
        "media_player", "test", "gone", suggested_object_id="denon"
    )

    with pytest.raises(PlayRefused):
        resolve_player(hass, "Denon")


async def test_resolve_player_skips_music_assistant_dashboard_displays(
    hass: HomeAssistant,
) -> None:
    speaker = add_player(hass, "kitchen", MA, friendly_name="Kitchen", device_class="speaker")
    add_player(hass, "kitchen_display", MA, friendly_name="Kitchen", device_class="tv")

    assert resolve_player(hass, "Kitchen") == speaker


# Which player: the satellite's.


async def test_default_player_override_beats_the_devices_player(hass: HomeAssistant) -> None:
    device_id = add_device(hass)
    add_player(hass, "kitchen_satellite", device_id=device_id)
    denon = add_player(hass, "denon", friendly_name="Denon")

    assert default_player(hass, device_id, {device_id: denon}) == denon


async def test_default_player_ignores_an_override_whose_player_is_gone(
    hass: HomeAssistant,
) -> None:
    device_id = add_device(hass)
    own = add_player(hass, "kitchen_satellite", device_id=device_id)

    assert default_player(hass, device_id, {device_id: "media_player.gone"}) == own


async def test_default_player_skips_an_unavailable_override(hass: HomeAssistant) -> None:
    device_id = add_device(hass)
    own = add_player(hass, "kitchen_satellite", device_id=device_id)
    denon = add_player(hass, "denon", friendly_name="Denon", state="unavailable")

    assert default_player(hass, device_id, {device_id: denon}) == own


async def test_default_player_skips_an_unavailable_player_on_the_device(
    hass: HomeAssistant,
) -> None:
    device_id = add_device(hass)
    add_player(
        hass, "a_kitchen", MA, device_id=device_id, device_class="speaker", state="unavailable"
    )
    available = add_player(hass, "b_kitchen", device_id=device_id)

    assert default_player(hass, device_id, {}) == available


async def test_default_player_refuses_when_its_players_are_unavailable(
    hass: HomeAssistant,
) -> None:
    device_id = add_device(hass)
    add_player(
        hass, "kitchen", device_id=device_id, friendly_name="Kitchen satellite",
        state="unavailable",
    )

    with pytest.raises(PlayRefused) as caught:
        default_player(hass, device_id, {})

    assert refusal(caught) == "Kitchen satellite is not available right now."


async def test_default_player_prefers_a_music_assistant_player_on_the_device(
    hass: HomeAssistant,
) -> None:
    device_id = add_device(hass)
    add_player(hass, "a_kitchen_satellite", device_id=device_id)
    ma = add_player(hass, "z_kitchen_satellite", MA, device_id=device_id, device_class="speaker")

    assert default_player(hass, device_id, {}) == ma


async def test_default_player_takes_the_first_player_on_the_device(hass: HomeAssistant) -> None:
    device_id = add_device(hass)
    add_player(hass, "b_kitchen", device_id=device_id)
    first = add_player(hass, "a_kitchen", device_id=device_id)

    assert default_player(hass, device_id, {}) == first


async def test_default_player_refuses_a_device_without_a_player(hass: HomeAssistant) -> None:
    device_id = add_device(hass)

    with pytest.raises(PlayRefused) as caught:
        default_player(hass, device_id, {})

    assert refusal(caught) == (
        "There is no speaker for this satellite. Say which player to use."
    )


async def test_default_player_refuses_a_request_without_a_device(hass: HomeAssistant) -> None:
    add_player(hass, "denon", friendly_name="Denon")

    with pytest.raises(PlayRefused) as caught:
        default_player(hass, None, {})

    assert refusal(caught) == (
        "There is no speaker for this satellite. Say which player to use."
    )


async def test_player_name_is_the_friendly_name(hass: HomeAssistant) -> None:
    denon = add_player(hass, "living_room", friendly_name="Denon")

    assert player_name(hass, denon) == "Denon"


async def test_player_name_falls_back_to_the_entity_id(hass: HomeAssistant) -> None:
    assert player_name(hass, "media_player.gone") == "media_player.gone"


# Playing.


@pytest.mark.parametrize(
    ("kind", "media_type"),
    [
        ("radio", "radio"),
        ("podcast", "podcast"),
        ("playlist", "playlist"),
        ("album", "album"),
        ("track", "track"),
    ],
)
async def test_async_play_sends_a_music_assistant_item_with_its_media_type(
    hass: HomeAssistant, kind: str, media_type: str
) -> None:
    calls = record(hass, MA, "play_media")
    player = add_player(hass, "kitchen", MA, device_class="speaker")
    context = Context()

    await async_play(hass, favorite(uri="library://radio/12", kind=kind), player, context)

    assert len(calls) == 1
    assert calls[0].data == {
        "entity_id": player,
        "media_id": "library://radio/12",
        "media_type": media_type,
        "enqueue": "replace",
    }
    assert calls[0].context is context


async def test_async_play_sends_a_stream_url_to_music_assistant_without_a_media_type(
    hass: HomeAssistant,
) -> None:
    calls = record(hass, MA, "play_media")
    player = add_player(hass, "kitchen", MA, device_class="speaker")

    await async_play(hass, favorite(), player, Context())

    assert calls[0].data == {"entity_id": player, "media_id": RADIO_2_URL, "enqueue": "replace"}


async def test_async_play_sends_the_url_to_another_player(hass: HomeAssistant) -> None:
    calls = record(hass, "media_player", "play_media")
    player = add_player(hass, "denon", "denonavr", friendly_name="Denon")
    context = Context()

    await async_play(hass, favorite(kind="radio"), player, context)

    assert calls[0].data == {
        "entity_id": player,
        "media_content_id": RADIO_2_URL,
        "media_content_type": "music",
    }
    assert calls[0].context is context


async def test_async_play_refuses_a_music_assistant_item_on_another_player(
    hass: HomeAssistant,
) -> None:
    calls = record(hass, "media_player", "play_media")
    player = add_player(hass, "denon", "denonavr", friendly_name="Denon")

    with pytest.raises(PlayRefused) as caught:
        await async_play(hass, favorite(uri="library://radio/12"), player, Context())

    assert refusal(caught) == "Radio 2 needs a Music Assistant speaker."
    assert calls == []


async def test_async_play_says_so_when_the_player_fails(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("the receiver is off")

    hass.services.async_register("media_player", "play_media", broken)
    player = add_player(hass, "denon", "denonavr", friendly_name="Denon")

    with pytest.raises(PlayRefused) as caught:
        await async_play(hass, favorite(), player, Context())

    assert refusal(caught) == "I couldn't start Radio 2 on Denon."
    assert "the receiver is off" in caplog.text


async def test_async_play_refuses_an_unavailable_player(hass: HomeAssistant) -> None:
    ma_calls = record(hass, MA, "play_media")
    calls = record(hass, "media_player", "play_media")
    player = add_player(
        hass, "denon", MA, friendly_name="Denon", device_class="speaker", state="unavailable"
    )

    with pytest.raises(PlayRefused) as caught:
        await async_play(hass, favorite(), player, Context())

    assert refusal(caught) == "Denon is not available right now."
    assert (ma_calls, calls) == ([], [])


async def test_async_play_refuses_a_player_that_is_gone(hass: HomeAssistant) -> None:
    calls = record(hass, "media_player", "play_media")

    with pytest.raises(PlayRefused):
        await async_play(hass, favorite(), "media_player.gone", Context())

    assert calls == []


@pytest.mark.parametrize("error", [probatio.Invalid("bad"), vol.Invalid("bad")])
async def test_async_play_says_so_when_the_action_rejects_its_data(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    async def rejects(call: ServiceCall) -> None:
        raise error

    hass.services.async_register("media_player", "play_media", rejects)
    player = add_player(hass, "denon", "denonavr", friendly_name="Denon")

    with pytest.raises(PlayRefused) as caught:
        await async_play(hass, favorite(), player, Context())

    assert refusal(caught) == "I couldn't start Radio 2 on Denon."
    assert "Radio 2" in caplog.text


async def test_async_play_treats_a_music_assistant_entity_without_speaker_class_as_plain(
    hass: HomeAssistant,
) -> None:
    ma_calls = record(hass, MA, "play_media")
    calls = record(hass, "media_player", "play_media")
    player = add_player(hass, "kitchen", MA, friendly_name="Kitchen")

    await async_play(hass, favorite(), player, Context())

    assert ma_calls == []
    assert calls[0].data["media_content_id"] == RADIO_2_URL


async def test_async_play_says_so_when_music_assistant_is_missing(
    hass: HomeAssistant,
) -> None:
    player = add_player(hass, "kitchen", MA, friendly_name="Kitchen", device_class="speaker")

    with pytest.raises(PlayRefused) as caught:
        await async_play(hass, favorite(), player, Context())

    assert refusal(caught) == "I couldn't start Radio 2 on Kitchen."


# Saving what is playing.


async def test_saveable_now_saves_a_music_assistant_radio_item(hass: HomeAssistant) -> None:
    player = add_player(
        hass, "kitchen", MA, device_class="speaker", state="playing",
        media_content_id="library://radio/12", media_content_type="music",
        media_title="Jazz FM", entity_picture="https://img.example.com/jazz.png",
    )

    data = await async_saveable_now(hass, player, "My station")

    assert data == {
        "name": "My station", "kind": "radio", "uri": "library://radio/12", "url": "",
        "image": "https://img.example.com/jazz.png",
    }
    assert validate(data)["uri"] == "library://radio/12"


async def test_saveable_now_saves_a_stream_from_music_assistant(hass: HomeAssistant) -> None:
    player = add_player(
        hass, "kitchen", MA, device_class="speaker", state="playing",
        media_content_id=RADIO_2_URL, media_content_type="music", media_title="Radio 2",
        entity_picture="/api/media_player_proxy/media_player.kitchen?token=x",
    )

    data = await async_saveable_now(hass, player, "My station")

    assert data == {
        "name": "My station", "kind": "stream", "uri": "", "url": RADIO_2_URL, "image": "",
    }


async def test_saveable_now_saves_a_music_assistant_channel(hass: HomeAssistant) -> None:
    player = add_player(
        hass, "kitchen", MA, device_class="speaker", state="playing",
        media_content_id="tunein://station/s123", media_content_type="channel",
        media_channel="Jazz FM", media_title="A song",
    )

    data = await async_saveable_now(hass, player, "My station")

    assert (data["name"], data["kind"], data["uri"]) == (
        "My station", "radio", "tunein://station/s123"
    )


async def test_saveable_now_saves_a_stream_from_another_player(hass: HomeAssistant) -> None:
    player = add_player(
        hass, "denon", "denonavr", friendly_name="Denon", state="playing",
        media_content_id=RADIO_2_URL, media_content_type="music", media_title="Radio 2",
    )

    data = await async_saveable_now(hass, player, "My station")

    assert data == {
        "name": "My station", "kind": "stream", "uri": "", "url": RADIO_2_URL, "image": "",
    }


async def test_regression_saveable_now_never_names_it_after_the_song_on_air(
    hass: HomeAssistant,
) -> None:
    # Music Assistant reports the song on a radio stream as the title.
    player = add_player(
        hass, "kitchen", MA, device_class="speaker", state="playing",
        media_content_id="library://radio/12", media_content_type="music",
        media_title="Queen - Bohemian Rhapsody", media_channel="Jazz FM",
    )

    data = await async_saveable_now(hass, player, "  Radio 2 ")

    assert (data["name"], data["uri"]) == ("Radio 2", "library://radio/12")


async def test_saveable_now_saves_what_has_no_title_under_the_name_given(
    hass: HomeAssistant,
) -> None:
    player = add_player(
        hass, "denon", "denonavr", friendly_name="Denon", state="playing",
        media_content_id=RADIO_2_URL, media_content_type="music",
    )

    data = await async_saveable_now(hass, player, "Radio 2")

    assert (data["name"], data["url"]) == ("Radio 2", RADIO_2_URL)


@pytest.mark.parametrize("name", ["", "   "])
async def test_saveable_now_needs_a_name(hass: HomeAssistant, name: str) -> None:
    player = add_player(
        hass, "denon", "denonavr", friendly_name="Denon", state="playing",
        media_content_id=RADIO_2_URL, media_content_type="music", media_title="Radio 2",
    )

    with pytest.raises(ValueError, match="name"):
        await async_saveable_now(hass, player, name)


@pytest.mark.parametrize(
    "content_id", ["library://podcast_episode/7", "library://track/3", "spotify://track/x"]
)
async def test_saveable_now_refuses_an_episode_or_track(
    hass: HomeAssistant, content_id: str
) -> None:
    player = add_player(
        hass, "kitchen", MA, device_class="speaker", state="playing",
        media_content_id=content_id, media_content_type="music", media_title="Episode 7",
    )

    with pytest.raises(PlayRefused) as caught:
        await async_saveable_now(hass, player, "My station")

    assert refusal(caught) == (
        "I can only save a station or stream that is playing. Ask me to save it by name."
    )


async def test_saveable_now_refuses_a_uri_on_another_player(hass: HomeAssistant) -> None:
    player = add_player(
        hass, "denon", "denonavr", state="playing",
        media_content_id="x-sonos-radio:abc", media_content_type="channel",
    )

    with pytest.raises(PlayRefused) as caught:
        await async_saveable_now(hass, player, "My station")

    assert refusal(caught).startswith("I can only save a station or stream")


@pytest.mark.parametrize(("state", "attributes"), [
    ("idle", {"media_content_id": RADIO_2_URL}),
    ("playing", {}),
])
async def test_saveable_now_refuses_when_nothing_plays(
    hass: HomeAssistant, state: str, attributes: dict[str, Any]
) -> None:
    player = add_player(hass, "kitchen", MA, device_class="speaker", state=state, **attributes)

    with pytest.raises(PlayRefused) as caught:
        await async_saveable_now(hass, player, "My station")

    assert refusal(caught) == "Nothing is playing that I can save."


async def test_saveable_now_refuses_a_player_that_is_gone(hass: HomeAssistant) -> None:
    with pytest.raises(PlayRefused) as caught:
        await async_saveable_now(hass, "media_player.gone", "My station")

    assert refusal(caught) == "Nothing is playing that I can save."


# Saving by name, through a Music Assistant search.


def item(name: str, media_type: str, uri: str, image: str | None = None) -> dict[str, Any]:
    return {"media_type": media_type, "uri": uri, "name": name, "version": "", "image": image}


def search_results(by_type: dict[str, list[dict[str, Any]]]) -> Any:
    keys = {"radio": "radio", "podcast": "podcasts", "playlist": "playlists"}

    def answer(call: ServiceCall) -> dict[str, Any]:
        result: dict[str, Any] = {
            key: [] for key in
            ("artists", "albums", "tracks", "playlists", "radio", "audiobooks", "podcasts")
        }
        media_type = call.data["media_type"]
        result[keys[media_type]] = by_type.get(media_type, [])
        return result

    return answer


async def add_music_assistant(
    hass: HomeAssistant, by_type: dict[str, list[dict[str, Any]]]
) -> tuple[str, list[ServiceCall]]:
    entry = MockConfigEntry(domain=MA, state=ConfigEntryState.LOADED)
    entry.add_to_hass(hass)
    calls = record(
        hass, MA, "search", search_results(by_type), supports_response=SupportsResponse.ONLY
    )
    return entry.entry_id, calls


async def test_search_saves_an_exact_match(hass: HomeAssistant) -> None:
    entry_id, calls = await add_music_assistant(hass, {
        "radio": [
            item("Radio 2 Extra", "radio", "library://radio/3"),
            item("Radio 2", "radio", "library://radio/2", "https://img.example.com/r2.png"),
        ],
    })

    data, candidates = await async_search(hass, "radio 2")

    assert data == {
        "name": "Radio 2", "aliases": [], "kind": "radio", "uri": "library://radio/2",
        "url": "", "image": "https://img.example.com/r2.png",
    }
    assert candidates == []
    assert calls[0].data == {
        "config_entry_id": entry_id, "name": "radio 2", "media_type": "radio", "limit": 5,
    }
    assert len(calls) == 1


async def test_search_prefers_an_exact_match_to_one_ending_with_the_name(
    hass: HomeAssistant,
) -> None:
    await add_music_assistant(hass, {
        "radio": [
            item("BBC Radio 2", "radio", "library://radio/22"),
            item("Radio 2", "radio", "library://radio/2"),
        ],
    })

    data, _ = await async_search(hass, "Radio 2")

    assert data is not None
    assert (data["name"], data["uri"]) == ("Radio 2", "library://radio/2")


async def test_search_prefers_an_exact_match_of_a_later_kind(hass: HomeAssistant) -> None:
    await add_music_assistant(hass, {
        "radio": [item("Morning Show Radio 2", "radio", "library://radio/1")],
        "podcast": [item("Radio 2", "podcast", "library://podcast/2")],
    })

    data, _ = await async_search(hass, "Radio 2")

    assert data is not None
    assert (data["kind"], data["uri"]) == ("podcast", "library://podcast/2")


@pytest.mark.parametrize("name", ["", "   ", "!!"])
async def test_search_does_not_search_for_a_blank_name(hass: HomeAssistant, name: str) -> None:
    _, calls = await add_music_assistant(
        hass, {"radio": [item("Radio 2", "radio", "library://radio/2")]}
    )

    assert await async_search(hass, name) == (None, [])
    assert calls == []


async def test_search_saves_a_name_ending_with_the_asked_one(hass: HomeAssistant) -> None:
    await add_music_assistant(hass, {
        "radio": [
            item("Radio 2 Extra", "radio", "library://radio/3"),
            item("BBC Radio 2", "radio", "library://radio/2"),
        ],
    })

    data, _ = await async_search(hass, "Radio 2")

    assert data is not None
    assert (data["name"], data["aliases"], data["uri"]) == (
        "BBC Radio 2", ["Radio 2"], "library://radio/2"
    )
    assert validate(data)["aliases"] == ["Radio 2"]


async def test_search_ends_with_only_at_a_word_boundary(hass: HomeAssistant) -> None:
    await add_music_assistant(hass, {"radio": [item("Studio 2", "radio", "library://radio/9")]})

    data, candidates = await async_search(hass, "dio 2")

    assert data is None
    assert candidates == ["Studio 2"]


async def test_search_goes_on_to_podcasts_then_playlists(hass: HomeAssistant) -> None:
    _, calls = await add_music_assistant(hass, {
        "radio": [item("Morning Show Radio", "radio", "library://radio/1")],
        "playlist": [item("The Morning Show", "playlist", "library://playlist/4")],
    })

    data, _ = await async_search(hass, "Morning Show")

    assert data is not None
    assert (data["kind"], data["uri"]) == ("playlist", "library://playlist/4")
    assert [call.data["media_type"] for call in calls] == ["radio", "podcast", "playlist"]


async def test_search_offers_up_to_three_candidates_when_nothing_matches(
    hass: HomeAssistant,
) -> None:
    await add_music_assistant(hass, {
        "radio": [item(f"Radio {n}", "radio", f"library://radio/{n}") for n in (1, 3)],
        "podcast": [item("Radio Talk", "podcast", "library://podcast/1"),
                    item("Radio 1", "podcast", "library://podcast/2")],
    })

    data, candidates = await async_search(hass, "Radio 2")

    assert data is None
    assert candidates == ["Radio 1", "Radio 3", "Radio Talk"]


async def test_search_needs_music_assistant(hass: HomeAssistant) -> None:
    with pytest.raises(PlayRefused) as caught:
        await async_search(hass, "Radio 2")

    assert refusal(caught) == "I can only save that by name with Music Assistant."


async def test_search_needs_a_loaded_music_assistant_entry(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=MA, state=ConfigEntryState.NOT_LOADED).add_to_hass(hass)
    calls = record(hass, MA, "search", {}, supports_response=SupportsResponse.ONLY)

    with pytest.raises(PlayRefused) as caught:
        await async_search(hass, "Radio 2")

    assert refusal(caught) == "I can only save that by name with Music Assistant."
    assert calls == []


async def test_search_says_so_when_music_assistant_fails(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    MockConfigEntry(domain=MA, state=ConfigEntryState.LOADED).add_to_hass(hass)

    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("server gone")

    hass.services.async_register(MA, "search", broken, supports_response=SupportsResponse.ONLY)

    with pytest.raises(PlayRefused) as caught:
        await async_search(hass, "Radio 2")

    assert refusal(caught) == "I couldn't search Music Assistant."
    assert "server gone" in caplog.text
