# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The memory store: per-person lists of facts, kept in one Home Assistant store.

Lists are keyed by scope:
  entry:<entry_id>:<person>   what one assistant remembers about a person
  global:<person>             what every assistant remembers about a person

Re-exported by memory.py.
"""

import asyncio
import logging
import secrets
from collections.abc import Iterable
from typing import Literal, NotRequired, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import data_keys
from .const import DOMAIN, Person

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.memory"
STORAGE_VERSION = 1
SAVE_DELAY = 5
MAX_TEXT_LENGTH = 200
# Websocket schemas accept a little more than MAX_TEXT_LENGTH so that a slightly long
# line still reaches async_apply_edit and gets the friendly "too long" answer.
MAX_LINE_SCHEMA_LENGTH = MAX_TEXT_LENGTH + 50
# The most facts one list may hold; also the most the prompt settings allow.
MAX_MAX_ITEMS = 200

# What the pages call each person who has no name.
PERSON_LABELS: dict[Person, str] = {"owner": "Person 1", "wife": "Person 2"}
Source = Literal["told", "judged"]


class MemoryItem(TypedDict):
    id: str
    text: str
    source: Source
    saved: str
    # Present and True when the person asked to keep the fact from the other person.
    private: NotRequired[bool]


PRIVATE_PREFIX = "(private)"


def item_line(item: MemoryItem) -> str:
    """One fact as a line of the editor. Private facts start with PRIVATE_PREFIX."""
    return f"{PRIVATE_PREFIX} {item['text']}" if item.get("private") else item["text"]


def _parse_line(line: str) -> tuple[str, bool]:
    """Text and private mark of one edited line."""
    cleaned = clean_text(line)
    if cleaned.casefold().startswith(PRIVATE_PREFIX):
        return clean_text(cleaned[len(PRIVATE_PREFIX) :]), True
    return cleaned, False


class LineTooLongError(ValueError):
    """An edited line is longer than MAX_TEXT_LENGTH."""

    def __init__(self, line: str) -> None:
        super().__init__(f"Line is longer than {MAX_TEXT_LENGTH} characters: {line}")
        self.line = line


class TooManyItemsError(ValueError):
    """An edit would leave a list with more than MAX_MAX_ITEMS facts."""

    def __init__(self, count: int) -> None:
        super().__init__(f"A list may hold {MAX_MAX_ITEMS} facts at most, not {count}")
        self.count = count


def entry_key(entry_id: str, person: Person) -> str:
    return f"entry:{entry_id}:{person}"


def global_key(person: Person) -> str:
    return f"global:{person}"


def clean_text(text: str) -> str:
    """Collapse newlines and runs of spaces into single spaces."""
    return " ".join(str(text).split())


def _dedupe_key(text: str) -> str:
    return clean_text(text).casefold()


def _today() -> str:
    return dt_util.now().date().isoformat()


def _apply_cap(items: list[MemoryItem], max_items: int) -> None:
    """Drop the oldest judged items until the list fits. Told items are never dropped."""
    excess = len(items) - max_items
    if excess <= 0:
        return
    judged = [item for item in items if item["source"] == "judged"]
    for item in judged[:excess]:
        items.remove(item)


def _valid_lists(data: object) -> dict[str, list[MemoryItem]]:
    """The lists of a loaded file, without anything that does not have the right shape.

    The file can be edited by hand, and one bad entry must not stop the routers.
    """
    if data is None:
        return {}
    stored = data.get("lists") if isinstance(data, dict) else None
    if not isinstance(stored, dict):
        _LOGGER.warning("The memory file does not have the expected shape, starting empty")
        return {}
    lists: dict[str, list[MemoryItem]] = {}
    for key, raw_items in stored.items():
        if not isinstance(key, str) or not isinstance(raw_items, list):
            _LOGGER.warning("Skipping a memory list that is not a list")
            continue
        items: list[MemoryItem] = []
        taken: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
                _LOGGER.warning("Skipping a remembered fact that is not valid")
                continue
            text = clean_text(raw["text"])
            if not text or len(text) > MAX_TEXT_LENGTH:
                _LOGGER.warning("Skipping a remembered fact that is not valid")
                continue
            item_id = raw.get("id")
            if not isinstance(item_id, str) or not item_id or item_id in taken:
                item_id = secrets.token_hex(4)
            taken.add(item_id)
            saved = raw.get("saved")
            item = MemoryItem(
                id=item_id,
                text=text,
                source="told" if raw.get("source") == "told" else "judged",
                saved=saved if isinstance(saved, str) and saved else _today(),
            )
            if raw.get("private") is True:
                item["private"] = True
            items.append(item)
        lists[key] = items
    return lists


class MemoryStore:
    """In-memory copy of every list, saved to .storage with a short delay."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.store: Store[dict[str, dict[str, list[MemoryItem]]]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY
        )
        self._lists: dict[str, list[MemoryItem]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def async_load(self) -> None:
        self._lists = _valid_lists(await self.store.async_load())

    def _schedule_save(self) -> None:
        self.store.async_delay_save(lambda: {"lists": self._lists}, SAVE_DELAY)

    def lock(self, key: str) -> asyncio.Lock:
        """The lock that every change to one list must hold."""
        return self._locks.setdefault(key, asyncio.Lock())

    def items(self, key: str) -> list[MemoryItem]:
        """Copies of the items of one list, in saved order."""
        return [MemoryItem(**item) for item in self._lists.get(key, [])]

    def has_room(self, key: str) -> bool:
        """Whether async_add can still save a fact a person asked to remember.

        It cannot when the list is at MAX_MAX_ITEMS and holds only told facts, which are
        never dropped for a new one.
        """
        items = self._lists.get(key, [])
        return len(items) < MAX_MAX_ITEMS or any(item["source"] == "judged" for item in items)

    def _new_item(
        self,
        key: str,
        text: str,
        source: Source,
        private: bool = False,
        taken: set[str] | None = None,
    ) -> MemoryItem:
        """A new item with an unused id. `taken` is the ids in use, grown by the new one.

        Pass it when making many items for one list so the ids are collected once.
        """
        if taken is None:
            taken = {item["id"] for item in self._lists.get(key, [])}
        item_id = secrets.token_hex(4)
        while item_id in taken:
            item_id = secrets.token_hex(4)
        taken.add(item_id)
        item = MemoryItem(id=item_id, text=text, source=source, saved=_today())
        if private:
            item["private"] = True
        return item

    async def async_add(
        self,
        key: str,
        text: str,
        source: Source,
        *,
        max_items: int | None = None,
        private: bool = False,
    ) -> MemoryItem | None:
        """Add one fact. Returns None when the list already has it, or has no room.

        A private fact is never shown when the other person speaks. No list holds more
        than MAX_MAX_ITEMS, whatever `max_items` says: the oldest judged facts make room,
        and when only told ones are left, which are never dropped, the new fact is not
        added and a warning says the list is full.

        Raises ValueError when the text is empty or longer than MAX_TEXT_LENGTH.
        """
        cleaned = clean_text(text)
        if not cleaned or len(cleaned) > MAX_TEXT_LENGTH:
            raise ValueError(f"Memory text must be 1 to {MAX_TEXT_LENGTH} characters")
        async with self.lock(key):
            items = self._lists.setdefault(key, [])
            if any(_dedupe_key(item["text"]) == _dedupe_key(cleaned) for item in items):
                return None
            item = self._new_item(key, cleaned, source, private)
            items.append(item)
            _apply_cap(items, min(max_items or MAX_MAX_ITEMS, MAX_MAX_ITEMS))
            if len(items) > MAX_MAX_ITEMS:
                items.remove(item)
                # The fact itself is personal and stays out of the log.
                _LOGGER.warning(
                    "The memory list %s is full (%d facts), so a new fact was not saved. "
                    "Remove some on the Voice Manager page",
                    key, MAX_MAX_ITEMS,
                )
            if not any(kept is item for kept in items):
                return None
            self._schedule_save()
            return MemoryItem(**item)

    async def async_remove(self, key: str, item_ids: Iterable[str]) -> list[MemoryItem]:
        """Remove items by id. Unknown ids are ignored. Returns what was removed."""
        wanted = set(item_ids)
        async with self.lock(key):
            items = self._lists.get(key, [])
            removed = [item for item in items if item["id"] in wanted]
            if removed:
                self._lists[key] = [item for item in items if item["id"] not in wanted]
                self._schedule_save()
            return [MemoryItem(**item) for item in removed]

    async def async_apply_edit(
        self, key: str, shown: list[MemoryItem], lines: Iterable[str]
    ) -> None:
        """Apply an edited list as a diff against what the form showed.

        Shown items whose text is still a line are kept as they are. Shown items whose
        line is gone are removed. New or changed lines are added as told. Items added
        since the form was shown are left alone. A line that starts with PRIVATE_PREFIX
        is private; adding or deleting the prefix of a kept line only changes that mark.

        Raises LineTooLongError, before changing anything, for a line that is too long,
        and TooManyItemsError when the list would end up with more than MAX_MAX_ITEMS.
        """
        wanted: dict[str, bool] = {}
        seen: set[str] = set()
        for line in lines:
            text, private = _parse_line(line)
            if not text:
                continue
            if len(text) > MAX_TEXT_LENGTH:
                raise LineTooLongError(text)
            if _dedupe_key(text) not in seen:
                seen.add(_dedupe_key(text))
                wanted[text] = private

        shown_ids = {item["id"] for item in shown}
        shown_texts = {item["text"] for item in shown}
        gone = {item["id"] for item in shown if item["text"] not in wanted}
        async with self.lock(key):
            items = [item for item in self._lists.get(key, []) if item["id"] not in gone]
            present = {_dedupe_key(item["text"]) for item in items}
            additions: list[tuple[str, bool]] = []
            for text, private in wanted.items():
                if text in shown_texts or _dedupe_key(text) in present:
                    continue
                additions.append((text, private))
                present.add(_dedupe_key(text))
            if len(items) + len(additions) > MAX_MAX_ITEMS:
                raise TooManyItemsError(len(items) + len(additions))
            for item in items:
                if item["id"] in shown_ids and item["text"] in wanted:
                    if wanted[item["text"]]:
                        item["private"] = True
                    else:
                        item.pop("private", None)
            taken = {item["id"] for item in items}
            for text, private in additions:
                items.append(self._new_item(key, text, "told", private, taken))
            self._lists[key] = items
            self._schedule_save()

    async def async_remove_entry(self, entry_id: str, *, last: bool = False) -> None:
        """Delete every per-assistant list of one router entry.

        The lists shared by every assistant stay, unless this was the `last` assistant:
        then nothing is left that could use them.
        """
        prefixes = (f"entry:{entry_id}:", *(("global:",) if last else ()))
        for key in [key for key in self._lists if key.startswith(prefixes)]:
            async with self.lock(key):
                self._lists.pop(key, None)
        self._schedule_save()


async def async_get_memory(hass: HomeAssistant) -> MemoryStore:
    """The one shared store, loaded on first use."""
    data = data_keys.domain_data(hass)
    async with data.setdefault(data_keys.MEMORY_LOCK, asyncio.Lock()):
        if data_keys.MEMORY not in data:
            memory = MemoryStore(hass)
            await memory.async_load()
            data[data_keys.MEMORY] = memory
    loaded: MemoryStore = data[data_keys.MEMORY]
    return loaded


def get_loaded_memory(hass: HomeAssistant) -> MemoryStore | None:
    """The shared store if it is loaded. Never waits, so it is safe in the reply path."""
    loaded: MemoryStore | None = hass.data.get(DOMAIN, {}).get(data_keys.MEMORY)
    return loaded
