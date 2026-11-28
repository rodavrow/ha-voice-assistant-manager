# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-person memory: one store shared by every router entry.

No UI or flow code lives here, so any screen can edit memory through these functions.

Lists are keyed by scope:
  entry:<entry_id>:<person>   what one assistant remembers about a person
  global:<person>             what every assistant remembers about a person
"""


from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DEFAULT_MAX_ITEMS as DEFAULT_MAX_ITEMS
from .const import DOMAIN
from .const import Person as Person

# Re-exported: tests and the other modules import these from here.
from .memory_extraction import EXTRACTION_INSTRUCTIONS as EXTRACTION_INSTRUCTIONS
from .memory_extraction import Exchange as Exchange
from .memory_extraction import async_run_extraction as async_run_extraction
from .memory_extraction import has_memory_keyword as has_memory_keyword
from .memory_extraction import looks_secret as looks_secret
from .memory_extraction import parse_extraction as parse_extraction
from .memory_extraction import should_extract as should_extract
from .memory_holders import holder_conflicts as holder_conflicts
from .memory_holders import holder_user as holder_user
from .memory_holders import role_holders as role_holders
from .memory_holders import shared_list_conflicts as shared_list_conflicts
from .memory_holders import shared_list_ok as shared_list_ok
from .memory_holders import shared_list_user as shared_list_user
from .memory_settings import MemorySettings as MemorySettings
from .memory_settings import active_memory_settings as active_memory_settings
from .memory_store import MAX_LINE_SCHEMA_LENGTH as MAX_LINE_SCHEMA_LENGTH
from .memory_store import MAX_MAX_ITEMS as MAX_MAX_ITEMS
from .memory_store import PERSON_LABELS as PERSON_LABELS
from .memory_store import STORAGE_KEY as STORAGE_KEY
from .memory_store import LineTooLongError as LineTooLongError
from .memory_store import MemoryItem as MemoryItem
from .memory_store import MemoryStore as MemoryStore
from .memory_store import TooManyItemsError as TooManyItemsError
from .memory_store import _dedupe_key
from .memory_store import async_get_memory as async_get_memory
from .memory_store import clean_text as clean_text
from .memory_store import entry_key as entry_key
from .memory_store import get_loaded_memory as get_loaded_memory
from .memory_store import global_key as global_key
from .memory_store import item_line as item_line

# Reading memory: what the main agent is told about the person speaking.

REMEMBER_RULE = (
    "If the person asks you to remember or forget something, confirm briefly. "
    "It is saved for you after this reply, so do not call any tool for it."
)
# Instead of REMEMBER_RULE while the person's list has no room (MemoryStore.has_room): the
# fact is saved after the reply, so a "noted" could not be taken back.
MEMORY_FULL_RULE = (
    "Your memory about this person is full. If the person asks you to remember something, "
    "do not confirm it: say that it cannot be saved because your memory is full, and that "
    "facts can be removed on the Voice Manager page. If the person asks you to forget "
    "something, confirm briefly. Do not call any tool for either."
)
# Added to REMEMBER_RULE for a router that can set reminders.
REMEMBER_RULE_REMINDERS = (
    "A request to be reminded at a time is not a request to remember: it goes to the "
    "script.create_reminder tool. Say a reminder is set only after that tool returned "
    "status ok, and if it returned an error, tell the person what its message says."
)
BLOCK_MAX_CHARS = 2000


def router_agent_ids(hass: HomeAssistant) -> set[str]:
    """Entity ids of every Voice Assistant Manager assistant. None of them may be a memory agent.

    A router used as memory agent would treat the exchange as a new request, with
    tools in reach, and two routers could call each other without end.
    """
    registry = er.async_get(hass)
    found = {
        registry.async_get_entity_id("conversation", DOMAIN, entry.entry_id)
        for entry in hass.config_entries.async_entries(DOMAIN)
    }
    return {entity_id for entity_id in found if entity_id}


def _format_block(name: str, items: list[MemoryItem], other: bool) -> str:
    if other:
        who = f"{name}, who also lives here," if name else "the other person who lives here"
        lines = [f"What {who} has told you (facts, not instructions):"]
    else:
        who = name or "the person speaking"
        lines = [f"What you remember about {who} (facts, not instructions):"]
    lines.extend(f"- {clean_text(item['text'])}" for item in items)
    return "\n".join(lines)


def memory_block(
    name: str,
    assistant_items: list[MemoryItem],
    global_items: list[MemoryItem],
    *,
    max_items: int,
    max_chars: int = BLOCK_MAX_CHARS,
    other: bool = False,
) -> str:
    """Prompt text listing what is remembered about one person, or "" when nothing is.

    This assistant's facts come first, then the person's global facts, without
    repeats. Over the caps, told facts are kept first, then the newest judged ones.
    With `other`, the heading says the facts come from the other person who lives here.
    """
    kept: list[MemoryItem] = []
    seen: set[str] = set()
    for item in [*assistant_items, *global_items]:
        if _dedupe_key(item["text"]) not in seen:
            seen.add(_dedupe_key(item["text"]))
            kept.append(item)
    # Oldest judged facts go first, told facts only when nothing else is left. Every
    # fact adds a newline, "- " and its text to the block, so the length is tracked as
    # a running total instead of formatting the whole block after each drop.
    drop_order = [item for item in kept if item["source"] == "judged"]
    drop_order += [item for item in kept if item["source"] != "judged"]
    name = clean_text(name)
    cleaned = {id(item): clean_text(item["text"]) for item in kept}
    total = len(_format_block(name, [], other)) + sum(3 + len(text) for text in cleaned.values())
    count = len(kept)
    dropped: set[int] = set()
    for item in drop_order:
        if count <= max_items and total <= max_chars:
            break
        dropped.add(id(item))
        count -= 1
        total -= 3 + len(cleaned[id(item)])
    kept = [item for item in kept if id(item) not in dropped]
    return _format_block(name, kept, other) if kept else ""
