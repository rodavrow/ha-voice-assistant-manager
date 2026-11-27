# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Favorites: named stations, podcasts and playlists, one store shared by every assistant.

No UI, voice or playing code lives here; the router, the tools and the pages build on it.

Lists are keyed:
  person:owner, person:wife   one person's favorites, used by every assistant with
                              favorites on while that role is the same person in all of them
  household                   everyone's favorites
"""

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, TypedDict, cast, get_args
from urllib.parse import urlsplit

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.ulid import ulid_now

from .const import DOMAIN
from .memory import Person, clean_text, holder_conflicts, holder_user, role_holders

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.favorites"
STORAGE_VERSION = 1
SAVE_DELAY = 1
MAX_FAVORITES = 100
MAX_NAME_LENGTH = 80
MAX_ALIASES = 5
MAX_URL_LENGTH = 2000

Kind = Literal["radio", "podcast", "playlist", "album", "track", "stream"]
ListKey = Literal["person:owner", "person:wife", "household"]
OwnState = Literal["ok", "conflict", "off"]
ErrorKey = Literal[
    "too_many",
    "name_taken",
    "bad_name",
    "bad_url",
    "bad_uri",
    "bad_kind",
    "no_source",
    "too_many_aliases",
    "not_found",
]
KINDS: Final[tuple[Kind, ...]] = get_args(Kind)
LIST_KEYS: Final[tuple[ListKey, ...]] = get_args(ListKey)
HOUSEHOLD: Final[ListKey] = "household"
PERSON_LIST: Final[dict[Person, ListKey]] = {"owner": "person:owner", "wife": "person:wife"}


class Favorite(TypedDict):
    id: str
    name: str
    aliases: list[str]
    kind: Kind
    # Exactly one of uri (a Music Assistant item) and url (an http(s) stream) is set.
    uri: str
    url: str
    image: str


class FavoriteError(ValueError):
    """A favorite that cannot be saved; `key` says why, for the caller's own wording."""

    def __init__(self, key: ErrorKey, message: str) -> None:
        super().__init__(message)
        self.key: ErrorKey = key


# Names.

_APOSTROPHES = re.compile(r"['’]")
_NOT_WORD = re.compile(r"[^\w\s]|_")


def normalise(text: str) -> str:
    """A name as it is compared: casefolded, without punctuation or a leading "the"."""
    words = _NOT_WORD.sub(" ", _APOSTROPHES.sub("", str(text).casefold())).split()
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def _names(favorite: Favorite) -> set[str]:
    return {normalise(name) for name in (favorite["name"], *favorite["aliases"])}


# Validating one favorite.


def _clean_name(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise FavoriteError("bad_name", f"The {what} must be text")
    name = clean_text(value)
    if not name or not normalise(name):
        raise FavoriteError("bad_name", f"The {what} must have a letter or digit in it")
    if len(name) > MAX_NAME_LENGTH:
        raise FavoriteError(
            "bad_name", f"The {what} may be {MAX_NAME_LENGTH} characters at most"
        )
    return name


def _clean_aliases(value: object, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise FavoriteError("bad_name", "The other names must be a list")
    given = [alias for alias in value if not (isinstance(alias, str) and not alias.strip())]
    # Counted before any is cleaned, so a list of any length costs nothing.
    if len(given) > MAX_ALIASES:
        raise FavoriteError(
            "too_many_aliases", f"A favorite may have {MAX_ALIASES} other names at most"
        )
    aliases = [_clean_name(alias, "other name") for alias in given]
    seen = {normalise(name)}
    for alias in aliases:
        if normalise(alias) in seen:
            raise FavoriteError("name_taken", f'"{alias}" is already a name of this favorite')
        seen.add(normalise(alias))
    return aliases


def _is_web_url(text: str) -> bool:
    parts = urlsplit(text)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


_URI = re.compile(r"([a-z][a-z0-9+.-]*)://\S+", re.IGNORECASE)
# Not Music Assistant items: a local file or Home Assistant media must never be saved by
# people who are not administrators, and an http(s) stream belongs in `url`.
_REFUSED_URI_SCHEMES = frozenset({"file", "http", "https", "media-source"})


def _clean_link(value: object, what: str, key: ErrorKey) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise FavoriteError(key, f"The {what} must be text")
    link = value.strip()
    if len(link) > MAX_URL_LENGTH or any(char.isspace() for char in link):
        raise FavoriteError(key, f"The {what} is not a valid address")
    return link


def _check_uri(uri: str) -> None:
    item = _URI.fullmatch(uri)
    if item is None or item.group(1).casefold() in _REFUSED_URI_SCHEMES:
        raise FavoriteError("bad_uri", "The Music Assistant item is not a valid item address")


def _clean_source(data: Mapping[str, Any]) -> tuple[str, str]:
    uri = _clean_link(data.get("uri"), "Music Assistant item", "bad_uri")
    url = _clean_link(data.get("url"), "stream address", "bad_url")
    if bool(uri) == bool(url):
        raise FavoriteError(
            "no_source", "A favorite needs either a Music Assistant item or a stream address"
        )
    if uri:
        _check_uri(uri)
    if url and not _is_web_url(url):
        raise FavoriteError("bad_url", "The stream address must start with http:// or https://")
    return uri, url


def _clean_image(value: object) -> str:
    image = _clean_link(value, "image address", "bad_url")
    on_home_assistant = len(image) > 1 and image.startswith("/") and not image.startswith("//")
    if image and not (on_home_assistant or _is_web_url(image)):
        raise FavoriteError("bad_url", "The image address must start with http:// or https://")
    return image


def _clean_kind(value: object) -> Kind:
    if value is None or value == "":
        return "stream"
    for kind in KINDS:
        if value == kind:
            return kind
    raise FavoriteError("bad_kind", f"A favorite's kind is one of {', '.join(KINDS)}")


def validate(data: Mapping[str, Any]) -> Favorite:
    """The favorite `data` describes, cleaned up. A missing kind is "stream".

    The id is kept when `data` has one as text, else it is "": the store gives new ids.
    Raises FavoriteError when the favorite cannot be saved.
    """
    name = _clean_name(data.get("name"), "name")
    aliases = _clean_aliases(data.get("aliases"), name)
    uri, url = _clean_source(data)
    favorite_id = data.get("id")
    return Favorite(
        id=favorite_id.strip() if isinstance(favorite_id, str) else "",
        name=name,
        aliases=aliases,
        kind=_clean_kind(data.get("kind")),
        uri=uri,
        url=url,
        image=_clean_image(data.get("image")),
    )


def find(lists: Sequence[Sequence[Favorite]], name: str) -> Favorite | None:
    """The favorite called `name` (its name or another name), from the first list that has it."""
    wanted = normalise(name)
    if not wanted:
        return None
    for favorites in lists:
        for favorite in favorites:
            if wanted in _names(favorite):
                return favorite
    return None


# Reading a play request.

_TRAILING_PUNCTUATION = ".!?,;:"
_POLITE_START = re.compile(
    r"^(?:please|can you|could you|would you|will you)\b[\s,]*", re.IGNORECASE
)
_POLITE_END = "please"
_PLAY = re.compile(r"play\s+(.+)", re.IGNORECASE)
# Greedy, so the split is on the last " on " or " in ".
_ON_PLAYER = re.compile(r"(.+)\s(?:on|in)\s(.+)", re.IGNORECASE)


def _without_end(text: str, characters: str) -> str:
    """`text` without the spaces and `characters` at its end.

    Written out, not a pattern: "[...]+$" is tried from every place in a long run that
    is not at the end, which takes time that grows with the square of the run's length.
    """
    end = len(text)
    while end and (text[end - 1].isspace() or text[end - 1] in characters):
        end -= 1
    return text[:end]


def _without_polite_end(text: str) -> str:
    """`text` without a last word "please" and the spaces and commas before it."""
    start = len(text) - len(_POLITE_END)
    if start < 0 or text[start:].lower() != _POLITE_END:
        return text
    if start and (text[start - 1].isalnum() or text[start - 1] == "_"):
        return text  # the end of a longer word
    return _without_end(text[:start], ",")


def _without_politeness(text: str) -> str:
    # A trailing "please" goes only once, so a name ending in "please" can still be asked for.
    text = _without_end(text, _TRAILING_PUNCTUATION)
    text = _without_end(_without_polite_end(text), _TRAILING_PUNCTUATION)
    previous = None
    while text != previous:
        previous = text
        text = _POLITE_START.sub("", text)
    return text


def parse_play(text: str) -> list[tuple[str, str | None]]:
    """The (name, player) pairs a play request could mean, in the order to try them.

    First the whole text after "play" as a name with no player, then the text split on the
    last " on " or " in " into a name and a player. [] when the text is not a play request.
    The pairs keep the words as spoken; `find` compares them normalised.
    """
    play = _PLAY.fullmatch(_without_politeness(clean_text(text)))
    if play is None:
        return []
    rest = play.group(1)
    pairs: list[tuple[str, str | None]] = [(rest, None)]
    on_player = _ON_PLAYER.fullmatch(rest)
    if on_player is not None:
        pairs.append((on_player.group(1), on_player.group(2)))
    return pairs


# Which lists an assistant uses.


def favorites_on(entry: ConfigEntry) -> bool:
    """Whether favorites are switched on for this assistant (off unless set)."""
    section = entry.options.get("favorites")
    return isinstance(section, Mapping) and section.get("enabled") is True


def person_list_conflicts(hass: HomeAssistant, role: Person) -> list[str]:
    """Titles of the assistants with favorites on that disagree on who holds this role.

    [] when they agree. A person's list is keyed by role, so it is only used while the role
    is the same person in every assistant with favorites on, by memory's shared-list rule.
    """
    return holder_conflicts(role_holders(hass, role, favorites_on))


def person_list_user(hass: HomeAssistant, role: Person) -> str | None:
    """The one Home Assistant user whose favorites this role's list is, or None."""
    return holder_user(role_holders(hass, role, favorites_on))


def own_list(hass: HomeAssistant, user_id: str) -> tuple[OwnState, ListKey | None]:
    """Whether this user has a list of their own, and which.

    "ok" with the list of the first role (Person 1 first) whose list is this user's;
    "conflict" when the user holds a role in an assistant with favorites on but the
    assistants disagree who holds it; "off" when the user holds no role in any assistant
    with favorites on.
    """
    holds_a_role = False
    for person, key in PERSON_LIST.items():
        holders = role_holders(hass, person, favorites_on)
        if not any(holder.user == user_id for holder in holders):
            continue
        if holder_user(holders) == user_id:
            return "ok", key
        holds_a_role = True
    return ("conflict" if holds_a_role else "off"), None


def lists_for(hass: HomeAssistant, role: Person) -> list[ListKey]:
    """The lists to search for this role, in order: their own, then the household's.

    Only the household's while the role is in conflict.
    """
    if person_list_conflicts(hass, role):
        return [HOUSEHOLD]
    return [PERSON_LIST[role], HOUSEHOLD]


# The store.


def _copy(favorite: Favorite) -> Favorite:
    copied = Favorite(**favorite)
    copied["aliases"] = list(favorite["aliases"])
    return copied


def _check_key(key: str) -> None:
    if key not in LIST_KEYS:
        raise ValueError(f"Unknown favorites list {key!r}")


def _check_names_free(favorites: Sequence[Favorite], favorite: Favorite) -> None:
    """Raises name_taken when another favorite in the list has one of its names."""
    names = _names(favorite)
    for other in favorites:
        if other["id"] != favorite["id"] and names & _names(other):
            raise FavoriteError(
                "name_taken", f'"{other["name"]}" already has that name in this list'
            )


def _valid_list(key: str, raw_favorites: list[object]) -> tuple[list[Favorite], bool]:
    """The favorites of one loaded list that are valid, fit and have unique names and ids.

    The flag is True when a kept favorite was given a new id, so the ids must be saved.
    """
    favorites: list[Favorite] = []
    new_ids = False
    for raw in raw_favorites:
        if not isinstance(raw, Mapping):
            _LOGGER.warning("Skipping a favorite in %s that is not valid", key)
            continue
        try:
            favorite = validate(raw)
            if not favorite["id"] or any(f["id"] == favorite["id"] for f in favorites):
                favorite["id"] = ulid_now()
            _check_names_free(favorites, favorite)
        except FavoriteError as err:
            _LOGGER.warning("Skipping a favorite in %s: %s", key, err)
            continue
        if len(favorites) >= MAX_FAVORITES:
            _LOGGER.warning("Skipping favorites in %s over the %s allowed", key, MAX_FAVORITES)
            break
        favorites.append(favorite)
        new_ids = new_ids or favorite["id"] != raw.get("id")
    return favorites, new_ids


def _valid_lists(data: object) -> tuple[dict[ListKey, list[Favorite]], bool]:
    """The lists of a loaded file, without anything that does not have the right shape.

    The file can be edited by hand, and one bad entry must not stop the assistants.
    The flag is True when a favorite was given a new id, so the ids must be saved.
    """
    lists: dict[ListKey, list[Favorite]] = {key: [] for key in LIST_KEYS}
    if data is None:
        return lists, False
    stored = data.get("lists") if isinstance(data, dict) else None
    if not isinstance(stored, dict):
        _LOGGER.warning("The favorites file does not have the expected shape, starting empty")
        return lists, False
    new_ids = False
    for key, raw_favorites in stored.items():
        if key not in LIST_KEYS or not isinstance(raw_favorites, list):
            _LOGGER.warning("Skipping a favorites list that is not valid: %r", key)
            continue
        favorites, list_new_ids = _valid_list(key, raw_favorites)
        lists[cast(ListKey, key)] = favorites
        new_ids = new_ids or list_new_ids
    return lists, new_ids


class FavoritesStore:
    """In-memory copy of every favorites list, saved to .storage with a short delay."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.store: Store[dict[str, dict[ListKey, list[Favorite]]]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self._lists: dict[ListKey, list[Favorite]] = {key: [] for key in LIST_KEYS}
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        self._lists, new_ids = _valid_lists(await self.store.async_load())
        if new_ids:
            # Keep the ids stable across restarts: a page may hold one.
            self._schedule_save()

    def _schedule_save(self) -> None:
        self.store.async_delay_save(lambda: {"lists": self._lists}, SAVE_DELAY)

    def items(self, key: ListKey) -> list[Favorite]:
        """Copies of the favorites of one list, in saved order."""
        _check_key(key)
        return [_copy(favorite) for favorite in self._lists[key]]

    def _index(self, key: ListKey, fav_id: str) -> int:
        for index, favorite in enumerate(self._lists[key]):
            if favorite["id"] == fav_id:
                return index
        raise FavoriteError("not_found", "That favorite is not in the list")

    async def async_add(self, key: ListKey, data: Mapping[str, Any]) -> Favorite:
        """Add a favorite with a new id. Raises FavoriteError when it cannot be added."""
        _check_key(key)
        favorite = validate({**data, "id": ""})
        async with self._lock:
            favorites = self._lists[key]
            if len(favorites) >= MAX_FAVORITES:
                raise FavoriteError(
                    "too_many", f"A list may hold {MAX_FAVORITES} favorites at most"
                )
            _check_names_free(favorites, favorite)
            favorite["id"] = ulid_now()
            favorites.append(favorite)
            self._schedule_save()
        return _copy(favorite)

    async def async_update(
        self, key: ListKey, fav_id: str, data: Mapping[str, Any]
    ) -> Favorite:
        """Change the fields `data` has. A new uri or url replaces the old source.

        Raises FavoriteError when the favorite is not in the list or cannot be saved.
        """
        _check_key(key)
        async with self._lock:
            favorites = self._lists[key]
            index = self._index(key, fav_id)
            merged: dict[str, Any] = dict(favorites[index])
            if "uri" in data or "url" in data:
                merged.pop("uri")
                merged.pop("url")
            merged.update(data)
            merged["id"] = fav_id
            favorite = validate(merged)
            _check_names_free(favorites, favorite)
            favorites[index] = favorite
            self._schedule_save()
        return _copy(favorite)

    async def async_remove(self, key: ListKey, fav_id: str) -> None:
        """Remove one favorite. Raises FavoriteError when it is not in the list."""
        _check_key(key)
        async with self._lock:
            del self._lists[key][self._index(key, fav_id)]
            self._schedule_save()


    async def async_clear(self) -> None:
        """Remove every favorite of every list: the last assistant was removed."""
        async with self._lock:
            self._lists = {key: [] for key in LIST_KEYS}
            self._schedule_save()


async def async_get_favorites(hass: HomeAssistant) -> FavoritesStore:
    """The one shared store, loaded on first use."""
    data = hass.data.setdefault(DOMAIN, {})
    async with data.setdefault("favorites_lock", asyncio.Lock()):
        if "favorites" not in data:
            favorites = FavoritesStore(hass)
            await favorites.async_load()
            data["favorites"] = favorites
    loaded: FavoritesStore = data["favorites"]
    return loaded
