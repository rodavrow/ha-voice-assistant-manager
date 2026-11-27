# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Favorites on players: which player, playing a favorite, and what can be saved.

A Music Assistant player is a media_player entity of the `music_assistant` platform that is
a speaker, Music Assistant's own rule for its play actions (its dashboard displays are not
players at all). Music Assistant is optional: a favorite with a stream address plays on any
player.
"""

import logging
from collections.abc import Iterable, Mapping
from typing import Any, Final
from urllib.parse import urlsplit

import probatio
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_ENTITY_PICTURE,
    ATTR_FRIENDLY_NAME,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from .favorites import MAX_NAME_LENGTH, Favorite, Kind, normalise

_LOGGER = logging.getLogger(__name__)

MEDIA_PLAYER: Final = "media_player"
MUSIC_ASSISTANT: Final = "music_assistant"
_SPEAKER_DEVICE_CLASS: Final = "speaker"
_DASHBOARD_DEVICE_CLASS: Final = "tv"
_PLAYING_STATES: Final = frozenset({"playing", "paused", "buffering"})

# Spoken after a favorite was started, by the router's quick path and by the tools alike.
PLAYING_LINE: Final = "Playing {name}."
PLAYING_ON_LINE: Final = "Playing {name} on {player}."

# Spoken replies. {name} is as the person said it or the favorite's name; {player} a name.
NO_SUCH_PLAYER: Final = "I can't find a player called {name}."
SEVERAL_PLAYERS: Final = "More than one player is called {name}."
NOT_AVAILABLE: Final = "{name} is not available right now."
NO_SPEAKER: Final = "There is no speaker for this satellite. Say which player to use."
NEEDS_MUSIC_ASSISTANT: Final = "{name} needs a Music Assistant speaker."
COULD_NOT_START: Final = "I couldn't start {name} on {player}."
NOTHING_TO_SAVE: Final = "Nothing is playing that I can save."
ONLY_STATIONS: Final = (
    "I can only save a station or stream that is playing. Ask me to save it by name."
)
SEARCH_NEEDS_MUSIC_ASSISTANT: Final = "I can only save that by name with Music Assistant."
SEARCH_FAILED: Final = "I couldn't search Music Assistant."

# Builder answer 11: the Music Assistant media type for each kind; a stream has none.
_MEDIA_TYPES: Final[dict[Kind, str]] = {
    "radio": "radio",
    "podcast": "podcast",
    "playlist": "playlist",
    "album": "album",
    "track": "track",
}
# Builder answer 8: searched in this order. Each maps to its list in the search response.
_SEARCHED: Final[tuple[tuple[Kind, str], ...]] = (
    ("radio", "radio"),
    ("podcast", "podcasts"),
    ("playlist", "playlists"),
)
SEARCH_LIMIT: Final = 5
MAX_CANDIDATES: Final = 3
_RADIO_CONTENT_TYPES: Final = frozenset({"radio", "channel"})


class PlayRefused(Exception):
    """Nothing was played or saved; `reply` says why, ready to be spoken."""

    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.reply = reply


class SearchUnavailable(PlayRefused):
    """Nothing can be searched: Music Assistant is not set up or not loaded."""

    def __init__(self) -> None:
        super().__init__(SEARCH_NEEDS_MUSIC_ASSISTANT)


# Which player.


def _device_class(entry: er.RegistryEntry) -> str | None:
    return entry.device_class or entry.original_device_class


def _is_music_assistant(entry: er.RegistryEntry | None) -> bool:
    return (
        entry is not None
        and entry.platform == MUSIC_ASSISTANT
        and _device_class(entry) == _SPEAKER_DEVICE_CLASS
    )


def _is_dashboard(entry: er.RegistryEntry | None) -> bool:
    """A Music Assistant dashboard display: a media_player that cannot play audio."""
    return (
        entry is not None
        and entry.platform == MUSIC_ASSISTANT
        and _device_class(entry) == _DASHBOARD_DEVICE_CLASS
    )


def _is_available(hass: HomeAssistant, entity_id: str) -> bool:
    # Home Assistant skips an unavailable target without an error, so nothing would play.
    state = hass.states.get(entity_id)
    return state is not None and state.state != STATE_UNAVAILABLE


def _area_name(hass: HomeAssistant, entry: er.RegistryEntry) -> str | None:
    area_id = er.async_get_effective_area_id(hass, entry)
    if area_id is None:
        return None
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area is not None else None


def _player_names(hass: HomeAssistant, state: State, entry: er.RegistryEntry | None) -> set[str]:
    """Every name a player can be called by, normalised."""
    names: list[str | None] = [
        state.attributes.get(ATTR_FRIENDLY_NAME),
        state.object_id,  # normalise turns its "_" into spaces
    ]
    if entry is not None:
        names += [entry.name, entry.original_name, _area_name(hass, entry)]
        names += er.async_get_entity_aliases(hass, entry)
    return {normalise(name) for name in names if isinstance(name, str)} - {""}


def _players(hass: HomeAssistant) -> Iterable[tuple[State, er.RegistryEntry | None]]:
    registry = er.async_get(hass)
    for state in hass.states.async_all(MEDIA_PLAYER):
        entry = registry.async_get(state.entity_id)
        if not _is_dashboard(entry):
            yield state, entry


def resolve_player(hass: HomeAssistant, name: str) -> str:
    """The entity id of the one player called `name`. Raises PlayRefused on none or several.

    Matched exactly (normalised) against the friendly name, the registry names, the object
    id, the aliases and the area. Unavailable players are not chosen. When several available
    players match and exactly one of them is a Music Assistant player, it is the Music
    Assistant view of the same speaker and wins.
    """
    spoken = name.strip()
    wanted = normalise(spoken)
    named = [
        (state, entry)
        for state, entry in _players(hass)
        if wanted and wanted in _player_names(hass, state, entry)
    ]
    if not named:
        raise PlayRefused(NO_SUCH_PLAYER.format(name=spoken))
    matches = [
        (state.entity_id, entry) for state, entry in named if state.state != STATE_UNAVAILABLE
    ]
    if not matches:
        raise PlayRefused(NOT_AVAILABLE.format(name=named[0][0].name))
    if len(matches) == 1:
        return matches[0][0]
    music_assistant = [entity_id for entity_id, entry in matches if _is_music_assistant(entry)]
    if len(music_assistant) == 1:
        return music_assistant[0]
    raise PlayRefused(SEVERAL_PLAYERS.format(name=spoken))


def default_player(
    hass: HomeAssistant, device_id: str | None, overrides: Mapping[str, str]
) -> str:
    """The player for a satellite: its override, else a player on the device itself.

    Unavailable players are skipped. A Music Assistant player on the device comes first,
    then the first by entity id. Raises PlayRefused when there is no device (phone app,
    browser) or no player, or when every player there is unavailable.
    """
    if device_id is None:
        raise PlayRefused(NO_SPEAKER)
    override = overrides.get(device_id)
    candidates = [override] if override is not None and hass.states.get(override) else []
    candidates += [
        entry.entity_id
        for entry in sorted(
            er.async_entries_for_device(er.async_get(hass), device_id),
            key=lambda entry: (not _is_music_assistant(entry), entry.entity_id),
        )
        if entry.domain == MEDIA_PLAYER
        and not _is_dashboard(entry)
        and hass.states.get(entry.entity_id) is not None
    ]
    if not candidates:
        raise PlayRefused(NO_SPEAKER)
    for entity_id in candidates:
        if _is_available(hass, entity_id):
            return entity_id
    raise PlayRefused(NOT_AVAILABLE.format(name=player_name(hass, candidates[0])))


def player_name(hass: HomeAssistant, entity_id: str) -> str:
    """The player's name as people see it, or its entity id."""
    state = hass.states.get(entity_id)
    if state is not None:
        return state.name
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and (entry.name or entry.original_name):
        return str(entry.name or entry.original_name)
    return entity_id


def available_players(hass: HomeAssistant) -> list[tuple[str, str]]:
    """(entity id, name) of every player that can play now, sorted by name."""
    players = [
        (state.entity_id, state.name)
        for state, _ in _players(hass)
        if state.state != STATE_UNAVAILABLE
    ]
    return sorted(players, key=lambda player: (player[1].casefold(), player[0]))


def _music_assistant_player(hass: HomeAssistant, entity_id: str) -> bool:
    return _is_music_assistant(er.async_get(hass).async_get(entity_id))


# Playing.


async def async_play(
    hass: HomeAssistant, favorite: Favorite, entity_id: str, context: Context
) -> None:
    """Start the favorite on the player. Raises PlayRefused when it cannot.

    A Music Assistant player takes the item or the stream, replacing its queue; another
    player takes only a stream address. A missing or unavailable player is refused before
    any action is called.
    """
    if not _is_available(hass, entity_id):
        raise PlayRefused(NOT_AVAILABLE.format(name=player_name(hass, entity_id)))
    if _music_assistant_player(hass, entity_id):
        domain, service = MUSIC_ASSISTANT, "play_media"
        data: dict[str, Any] = {
            ATTR_ENTITY_ID: entity_id,
            "media_id": favorite["uri"] or favorite["url"],
            "enqueue": "replace",
        }
        if (media_type := _MEDIA_TYPES.get(favorite["kind"])) is not None:
            data["media_type"] = media_type
    else:
        if not favorite["url"]:
            raise PlayRefused(NEEDS_MUSIC_ASSISTANT.format(name=favorite["name"]))
        domain, service = MEDIA_PLAYER, "play_media"
        data = {
            ATTR_ENTITY_ID: entity_id,
            "media_content_id": favorite["url"],
            "media_content_type": "music",
        }
    try:
        await hass.services.async_call(domain, service, data, blocking=True, context=context)
    except (HomeAssistantError, probatio.Invalid, vol.Invalid) as err:
        # Home Assistant re-raises probatio.Invalid when an action's schema refuses the data;
        # an integration's own handler may still raise voluptuous' Invalid.
        _LOGGER.error("Could not play favorite %r on %s: %s", favorite["name"], entity_id, err)
        raise PlayRefused(
            COULD_NOT_START.format(name=favorite["name"], player=player_name(hass, entity_id))
        ) from err


# Saving what is playing.


def _is_web_url(text: str) -> bool:
    parts = urlsplit(text)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _is_radio_uri(text: str) -> bool:
    """A Music Assistant radio item, e.g. library://radio/12."""
    parts = urlsplit(text)
    return bool(parts.scheme) and parts.netloc == "radio" and bool(parts.path.strip("/"))


def _web_image(state: State) -> str:
    picture = state.attributes.get(ATTR_ENTITY_PICTURE)
    return picture if isinstance(picture, str) and _is_web_url(picture) else ""


async def async_saveable_now(
    hass: HomeAssistant, entity_id: str, name: str
) -> dict[str, Any]:
    """What is playing on the player, named `name`, as data for favorites.validate().

    Only a station or a stream can be saved as "this" (builder answers 1 and 7): on a Music
    Assistant player a radio item or an http(s) stream, on another player an http(s) stream.
    Raises PlayRefused otherwise. The name always comes from the person: what a player
    reports as the title is often the song on air (Music Assistant radio), never the station.
    Raises ValueError for a blank name.
    """
    name = name.strip()
    if not name:
        raise ValueError("A favorite saved from what is playing needs a name")
    state = hass.states.get(entity_id)
    content_id = state.attributes.get("media_content_id") if state is not None else None
    if state is None or state.state not in _PLAYING_STATES or not content_id:
        raise PlayRefused(NOTHING_TO_SAVE)
    content_id = str(content_id).strip()
    kind: Kind
    if _is_web_url(content_id):
        kind, uri, url = "stream", "", content_id
    elif _music_assistant_player(hass, entity_id) and (
        _is_radio_uri(content_id)
        or (
            state.attributes.get("media_content_type") in _RADIO_CONTENT_TYPES
            and "://" in content_id
        )
    ):
        kind, uri, url = "radio", content_id, ""
    else:
        raise PlayRefused(ONLY_STATIONS)
    return {
        "name": name,
        "kind": kind,
        "uri": uri,
        "url": url,
        "image": _web_image(state),
    }


# Saving by name.


def _music_assistant_entry_id(hass: HomeAssistant) -> str | None:
    if not hass.services.has_service(MUSIC_ASSISTANT, "search"):
        return None
    for entry in hass.config_entries.async_entries(MUSIC_ASSISTANT):
        if entry.state is ConfigEntryState.LOADED:
            return entry.entry_id
    return None


def _ends_with(found: str, wanted: str) -> bool:
    """Builder answer 8: a name ending with the asked one at a word boundary."""
    return found.endswith(f" {wanted}")


def _fitting_name(found: object) -> str:
    """A found name short enough to save: cut at the last word boundary that fits."""
    name = " ".join(str(found).split())
    if len(name) <= MAX_NAME_LENGTH:
        return name
    head, space, _ = name[: MAX_NAME_LENGTH + 1].rpartition(" ")
    return head if space and head else name[:MAX_NAME_LENGTH]


def _saveable(item: Mapping[str, Any], kind: Kind, asked: str) -> dict[str, Any]:
    name = _fitting_name(item["name"])
    image = item.get("image")
    return {
        "name": name,
        # The asked name stays a way to call it when the match only ends with it.
        "aliases": [] if normalise(name) == normalise(asked) else [asked],
        "kind": kind,
        "uri": str(item["uri"]),
        "url": "",
        "image": image if isinstance(image, str) and _is_web_url(image) else "",
    }


async def _search_kind(
    hass: HomeAssistant, entry_id: str, asked: str, kind: Kind, results_key: str
) -> list[Mapping[str, Any]]:
    """The items of one kind Music Assistant finds for `asked` that have a name and a uri.

    Raises PlayRefused when the search fails.
    """
    try:
        response = await hass.services.async_call(
            MUSIC_ASSISTANT,
            "search",
            {
                "config_entry_id": entry_id,
                "name": asked,
                "media_type": _MEDIA_TYPES[kind],
                "limit": SEARCH_LIMIT,
            },
            blocking=True,
            return_response=True,
        )
    except HomeAssistantError as err:
        _LOGGER.error("Music Assistant search for %r failed: %s", asked, err)
        raise PlayRefused(SEARCH_FAILED) from err
    items = (response or {}).get(results_key) or []
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, Mapping) and item.get("name") and item.get("uri")
    ]


def _searched_entry(hass: HomeAssistant) -> str:
    entry_id = _music_assistant_entry_id(hass)
    if entry_id is None:
        raise SearchUnavailable
    return entry_id


async def async_search(hass: HomeAssistant, name: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Find `name` in Music Assistant: radio, then podcasts, then playlists.

    Returns data for favorites.validate() and no candidates for the first item called
    `name`, else for the first item whose name ends with it; else None and up to 3 names
    found, to offer instead. A blank name is not searched: (None, []).
    Raises PlayRefused without a loaded Music Assistant, or when the search fails.
    """
    asked = name.strip()
    wanted = normalise(asked)
    if not wanted:
        return None, []
    entry_id = _searched_entry(hass)
    candidates: list[str] = []
    ending_with: dict[str, Any] | None = None
    for kind, results_key in _SEARCHED:
        for item in await _search_kind(hass, entry_id, asked, kind, results_key):
            found = normalise(str(item["name"]))
            if found == wanted:
                return _saveable(item, kind, asked), []
            if ending_with is None and _ends_with(found, wanted):
                ending_with = _saveable(item, kind, asked)
            if item["name"] not in candidates:
                candidates.append(str(item["name"]))
    if ending_with is not None:
        return ending_with, []
    return None, candidates[:MAX_CANDIDATES]


def _candidate(item: Mapping[str, Any], kind: Kind) -> dict[str, Any]:
    image = item.get("image")
    return {
        "name": _fitting_name(item["name"]),
        "kind": kind,
        "uri": str(item["uri"]),
        "image": image if isinstance(image, str) and _is_web_url(image) else "",
    }


async def async_search_candidates(hass: HomeAssistant, name: str) -> list[dict[str, Any]]:
    """Everything Music Assistant finds for `name`, as data a page can save directly.

    Radio, then podcasts, then playlists, each item once. The item async_search would save
    comes first. A blank name is not searched: [].
    Raises PlayRefused without a loaded Music Assistant, or when the search fails.
    """
    asked = name.strip()
    wanted = normalise(asked)
    if not wanted:
        return []
    entry_id = _searched_entry(hass)
    found: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for kind, results_key in _SEARCHED:
        for item in await _search_kind(hass, entry_id, asked, kind, results_key):
            if str(item["uri"]) not in seen:
                seen.add(str(item["uri"]))
                found.append((normalise(str(item["name"])), _candidate(item, kind)))
    best = next((index for index, (got, _) in enumerate(found) if got == wanted), None)
    if best is None:
        best = next(
            (index for index, (got, _) in enumerate(found) if _ends_with(got, wanted)), None
        )
    candidates = [candidate for _, candidate in found]
    if best is not None:
        candidates.insert(0, candidates.pop(best))
    return candidates
