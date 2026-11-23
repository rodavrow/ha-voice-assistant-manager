# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The favorites tools the main agent can use: an LLM API, switched on next to Assist.

Who is asking is never the model's to say: each tool reads the router's record for its own
context id (favorites_context) and refuses without one. Results are
{"status": "ok" | "error" | "choose", "message": ...} plus data, worded for the model.
"""

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, override

import probatio
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers import llm

from .const import DOMAIN
from .favorites import (
    HOUSEHOLD,
    PERSON_LIST,
    Favorite,
    FavoriteError,
    ListKey,
    async_get_favorites,
    favorites_on,
    find,
    lists_for,
    normalise,
    parse_play,
    person_list_conflicts,
)
from .favorites_context import asker
from .favorites_play import (
    PLAYING_LINE,
    PLAYING_ON_LINE,
    PlayRefused,
    async_play,
    async_saveable_now,
    async_search,
    default_player,
    player_name,
    resolve_player,
)
from .memory import Person
from .panel import speaker_overrides

API_NAME: Final = "Voice Assistant Manager"
API_PROMPT: Final = (
    "Whenever you are asked to play something by name, call play_favorite first, before any "
    "other media tool; only if it is not a favorite, use other tools. When asked to save or "
    "forget a favorite, use save_favorite or remove_favorite. To save what is playing, call "
    "save_favorite with this=true and the name the person gave; ask for a name if they gave "
    "none. Set household only when the person says it is for everyone or for the household."
)

# Tool results, worded for the model. {name} is a favorite's name or the name asked for.
UNKNOWN_ASKER: Final = "I can't tell who is asking."
FAVORITES_OFF: Final = "Favorites are off for this assistant."
OWN_LIST_CONFLICT: Final = (
    "Your own favorites are not available until every assistant agrees who you are."
)
NOT_A_FAVORITE: Final = "{name} is not one of your favorites or the household's."
NOT_IN_LIST: Final = "{name} is not one of {whose}."
NAME_TAKEN: Final = "There is already a favorite called {name}."
LIST_FULL: Final = "That list is full."
COULD_NOT_SAVE: Final = "I couldn't save that."
NOT_FOUND: Final = "I couldn't find {name}."
CHOOSE: Final = "I couldn't find {name} exactly. Ask whether one of the candidates is meant."
NEED_NAME: Final = "What should I call it?"
SAVED: Final = "Saved {name} to {whose}."
REMOVED: Final = "Removed {name} from {whose}."
_WHOSE: Final[dict[bool, str]] = {False: "your favorites", True: "the household's favorites"}
LISTED: Final[dict[bool, str]] = {False: "Your favorites.", True: "The household's favorites."}
# Names that mean what is playing now, normalised: this=true without a name.
_PLAYING_NOW: Final = frozenset({"this", "that", "whats playing"})


@dataclass(frozen=True)
class _Asking:
    """Who asks, from the router's record: the person, the satellite device, the context."""

    role: Person
    device_id: str | None
    context: Context


def _ok(message: str, **data: Any) -> llm.ToolResult:
    return llm.ToolResult(data={"status": "ok", "message": message, **data})


def _error(message: str) -> llm.ToolResult:
    return llm.ToolResult(data={"status": "error", "message": message}, error=True)


def _list_key(hass: HomeAssistant, role: Person, household: bool) -> ListKey | None:
    """The one list a save, removal or listing works on; None while the person's conflicts."""
    if household:
        return HOUSEHOLD
    if person_list_conflicts(hass, role):
        return None
    return PERSON_LIST[role]


def _taken_name(favorites: list[Favorite], data: Mapping[str, Any]) -> str:
    """The name of the favorite in the list that already has one of the names in `data`."""
    for name in (data.get("name"), *(data.get("aliases") or [])):
        if isinstance(name, str) and (taken := find([favorites], name)) is not None:
            return taken["name"]
    return str(data.get("name") or "")


def _household_key() -> probatio.Optional:
    return probatio.Optional(
        "household",
        default=False,
        description="True only when the person says it is for everyone or the household.",
    )


class _FavoritesTool(llm.Tool):
    """A favorites tool: refuses an unknown asker and an assistant with favorites off."""

    integration = DOMAIN

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> llm.ToolResult:
        args = self.parameters(tool_input.tool_args)
        context = llm_context.context
        found = asker(hass, context.id) if context is not None else None
        if context is None or found is None:
            return _error(UNKNOWN_ASKER)
        entry = hass.config_entries.async_get_entry(found.entry_id)
        if entry is None or entry.domain != DOMAIN or not favorites_on(entry):
            return _error(FAVORITES_OFF)
        asking = _Asking(found.role, found.device_id or llm_context.device_id, context)
        return await self._async_run(hass, args, asking)

    @abstractmethod
    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any], asking: _Asking
    ) -> llm.ToolResult:
        """The tool's own work, for a known person on an assistant with favorites on."""


class PlayFavoriteTool(_FavoritesTool):
    name = "play_favorite"
    description = (
        "Play one of the speaker's favorites (a station, podcast or playlist) or the "
        "household's, by its name. Plays on the satellite's own speaker unless a player is "
        "named."
    )
    annotations = llm.ToolAnnotations(destructive=False, open_world=False)
    parameters = probatio.Schema(
        {
            probatio.Required("name", description="The favorite's name, as said."): str,
            probatio.Optional(
                "player", description="The player to play on, only when one was named."
            ): str,
        }
    )

    @override
    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any], asking: _Asking
    ) -> llm.ToolResult:
        name = args["name"].strip()
        player = (args.get("player") or "").strip() or None
        # Builder answer 9: the whole text is a name first, then split on its last on / in.
        readings = [(name, player)] if player else parse_play(f"play {name}") or [(name, None)]
        store = await async_get_favorites(hass)
        keys = lists_for(hass, asking.role)
        lists = [store.items(key) for key in keys]
        for wanted, named in readings:
            favorite = find(lists, wanted)
            if favorite is not None:
                return await _async_play_on(hass, favorite, named, asking)
        if keys == [HOUSEHOLD]:
            # The person's own list was not searched (a conflict): say only what was.
            return _error(NOT_IN_LIST.format(name=name, whose=_WHOSE[True]))
        return _error(NOT_A_FAVORITE.format(name=name))


async def _async_play_on(
    hass: HomeAssistant, favorite: Favorite, player: str | None, asking: _Asking
) -> llm.ToolResult:
    try:
        if player is not None:
            entity_id = resolve_player(hass, player)
        else:
            entity_id = default_player(hass, asking.device_id, speaker_overrides(hass))
        await async_play(hass, favorite, entity_id, asking.context)
    except PlayRefused as refused:
        return _error(refused.reply)
    if player is not None:
        where = player_name(hass, entity_id)
        return _ok(PLAYING_ON_LINE.format(name=favorite["name"], player=where))
    return _ok(PLAYING_LINE.format(name=favorite["name"]))


class SaveFavoriteTool(_FavoritesTool):
    name = "save_favorite"
    description = (
        "Save a favorite. With this=true it saves the station or stream playing on the "
        "satellite's speaker under the name the person gave; otherwise it finds the name in "
        "Music Assistant. It always needs a name from the person and asks for one without it. "
        "Returns what was saved, or candidates to offer."
    )
    annotations = llm.ToolAnnotations(destructive=False)
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "name",
                description="The name the person said: to save what plays under, or to find.",
            ): str,
            probatio.Optional(
                "this",
                default=False,
                description="True to save what is playing now on the speaker.",
            ): bool,
            _household_key(): bool,
        }
    )

    @override
    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any], asking: _Asking
    ) -> llm.ToolResult:
        household: bool = args["household"]
        key = _list_key(hass, asking.role, household)
        if key is None:
            return _error(OWN_LIST_CONFLICT)
        asked = (args.get("name") or "").strip()
        this: bool = args["this"]
        if normalise(asked) in _PLAYING_NOW:
            # An older way of saying this=true; it names nothing.
            this, asked = True, ""
        if not asked:
            # Never named after what the player reports: that is often the song on air.
            return llm.ToolResult(data={"status": "need_name", "message": NEED_NAME})
        try:
            if this:
                entity_id = default_player(hass, asking.device_id, speaker_overrides(hass))
                data = await async_saveable_now(hass, entity_id, asked)
            else:
                found, candidates = await async_search(hass, asked)
                if found is None:
                    return _not_found(asked, candidates)
                data = found
        except PlayRefused as refused:
            return _error(refused.reply)
        return await _async_save(hass, key, data, household)


def _not_found(asked: str, candidates: list[str]) -> llm.ToolResult:
    if not candidates:
        return _error(NOT_FOUND.format(name=asked))
    return llm.ToolResult(
        data={
            "status": "choose",
            "message": CHOOSE.format(name=asked),
            "candidates": list(candidates),
        }
    )


async def _async_save(
    hass: HomeAssistant, key: ListKey, data: Mapping[str, Any], household: bool
) -> llm.ToolResult:
    store = await async_get_favorites(hass)
    try:
        saved = await store.async_add(key, data)
    except FavoriteError as err:
        if err.key == "name_taken":
            return _error(NAME_TAKEN.format(name=_taken_name(store.items(key), data)))
        if err.key == "too_many":
            return _error(LIST_FULL)
        return _error(COULD_NOT_SAVE)
    return _ok(
        SAVED.format(name=saved["name"], whose=_WHOSE[household]),
        saved={"name": saved["name"], "list": "household" if household else "yours"},
    )


class RemoveFavoriteTool(_FavoritesTool):
    name = "remove_favorite"
    description = "Remove one favorite, found by its name or another name, from one list."
    annotations = llm.ToolAnnotations(open_world=False)
    parameters = probatio.Schema(
        {
            probatio.Required("name", description="The favorite's name, as said."): str,
            _household_key(): bool,
        }
    )

    @override
    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any], asking: _Asking
    ) -> llm.ToolResult:
        household: bool = args["household"]
        key = _list_key(hass, asking.role, household)
        if key is None:
            return _error(OWN_LIST_CONFLICT)
        store = await async_get_favorites(hass)
        name = args["name"].strip()
        favorite = find([store.items(key)], name)
        if favorite is not None:
            try:
                await store.async_remove(key, favorite["id"])
            except FavoriteError:  # removed on a page in the meantime
                favorite = None
        if favorite is None:
            return _error(NOT_IN_LIST.format(name=name, whose=_WHOSE[household]))
        return _ok(REMOVED.format(name=favorite["name"], whose=_WHOSE[household]))


class ListFavoritesTool(_FavoritesTool):
    name = "list_favorites"
    description = "The names of the person's own favorites, or of the household's."
    annotations = llm.ToolAnnotations(
        read_only=True, destructive=False, idempotent=True, open_world=False
    )
    parameters = probatio.Schema({_household_key(): bool})

    @override
    async def _async_run(
        self, hass: HomeAssistant, args: dict[str, Any], asking: _Asking
    ) -> llm.ToolResult:
        household: bool = args["household"]
        key = _list_key(hass, asking.role, household)
        if key is None:
            return _error(OWN_LIST_CONFLICT)
        store = await async_get_favorites(hass)
        return _ok(
            LISTED[household],
            favorites=[
                {"name": favorite["name"], "aliases": favorite["aliases"]}
                for favorite in store.items(key)
            ],
        )


class FavoritesAPI(llm.API):
    """The favorites tools, for any main agent that has this API switched on."""

    @override
    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        return llm.APIInstance(
            api=self,
            api_prompt=API_PROMPT,
            llm_context=llm_context,
            tools=[
                PlayFavoriteTool(),
                SaveFavoriteTool(),
                RemoveFavoriteTool(),
                ListFavoritesTool(),
            ],
        )


@callback
def async_register_favorites_api(hass: HomeAssistant) -> None:
    """Offer the favorites tools to the main agents. Once, when the integration is set up."""
    llm.async_register_api(hass, FavoritesAPI(hass=hass, id=DOMAIN, name=API_NAME))
