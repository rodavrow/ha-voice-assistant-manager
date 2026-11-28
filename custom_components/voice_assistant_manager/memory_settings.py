# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Memory section of an entry's settings, when memory is active.

Re-exported by memory.py.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .const import DEFAULT_MAX_ITEMS
from .memory_store import MAX_MAX_ITEMS

MIN_MAX_ITEMS = 10


@dataclass(frozen=True)
class MemorySettings:
    """The Memory section of an entry's settings, when memory is active."""

    agent: str
    auto_save: bool
    max_items: int
    use_global: bool
    # What one person told is also known when the other person speaks.
    shared: bool


def active_memory_settings(
    options: Mapping[str, Any], forbidden_agents: Iterable[str | None]
) -> MemorySettings | None:
    """Memory settings of an entry, or None when memory is not active for it.

    Memory is active only when it is enabled and an agent is chosen that is not one
    of `forbidden_agents` (the main agent and the router itself).
    """
    section = options.get("memory") or {}
    agent = str(section.get("agent") or "").strip()
    if not section.get("enabled") or not agent or agent in set(forbidden_agents):
        return None
    try:
        max_items = int(float(section.get("max_items", DEFAULT_MAX_ITEMS)))
    except (TypeError, ValueError):
        max_items = DEFAULT_MAX_ITEMS
    return MemorySettings(
        agent=agent,
        auto_save=bool(section.get("auto_save", True)),
        max_items=min(max(max_items, MIN_MAX_ITEMS), MAX_MAX_ITEMS),
        use_global=bool(section.get("use_global", True)),
        shared=bool(section.get("shared", True)),
    )
