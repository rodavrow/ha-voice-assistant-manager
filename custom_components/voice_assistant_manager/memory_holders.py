# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who holds each role across the assistants, for the lists every assistant shares.

Re-exported by memory.py.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PERSON_SECTION, Person
from .memory_store import clean_text

_PERSON_SECTION = PERSON_SECTION


@dataclass(frozen=True)
class RoleHolder:
    """Who holds a person's role in one router that uses the shared lists."""

    title: str
    user: str
    name: str  # casefolded


def _uses_shared_lists(entry: ConfigEntry) -> bool:
    section = entry.options.get("memory")
    return bool((section if isinstance(section, Mapping) else {}).get("use_global", True))


def role_holders(
    hass: HomeAssistant, person: Person, counts: Callable[[ConfigEntry], bool]
) -> list[RoleHolder]:
    """The role in every router that `counts` and has the role filled in."""
    holders: list[RoleHolder] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if not counts(entry):
            continue
        section = entry.options.get(_PERSON_SECTION[person])
        if not isinstance(section, Mapping):
            continue
        user = str(section.get("user") or "").strip()
        name = clean_text(str(section.get("name") or "")).casefold()
        if user or name:
            holders.append(RoleHolder(title=entry.title, user=user, name=name))
    return holders


def holder_conflicts(holders: Iterable[RoleHolder]) -> list[str]:
    """Titles of the routers when the holders are not all one person, or [] when they are.

    One person means the same Home Assistant user where one is set, and the same name.
    """
    holders = list(holders)
    users = {holder.user for holder in holders if holder.user}
    names = {holder.name for holder in holders}
    if len(users) <= 1 and len(names) <= 1:
        return []
    return sorted(holder.title for holder in holders)


def holder_user(holders: Iterable[RoleHolder]) -> str | None:
    """The one Home Assistant user the holders agree on, or None.

    None when the holders are not all one person or none of them is linked to a user.
    """
    holders = list(holders)
    if holder_conflicts(holders):
        return None
    users = {holder.user for holder in holders if holder.user}
    return next(iter(users)) if len(users) == 1 else None


def shared_list_conflicts(hass: HomeAssistant, person: Person) -> list[str]:
    """Titles of the routers that disagree on who holds this role, or [] when none do.

    The list shared by all assistants is keyed by role, so it may only be used while the
    role is the same person in every router that uses the shared lists: the same Home
    Assistant user where one is set, and the same name.
    """
    return holder_conflicts(role_holders(hass, person, _uses_shared_lists))


def shared_list_ok(hass: HomeAssistant, person: Person) -> bool:
    """Whether this role's list shared by all assistants may be read and written."""
    return not shared_list_conflicts(hass, person)


def shared_list_user(hass: HomeAssistant, person: Person) -> str | None:
    """The one Home Assistant user whose list this role's shared list is, or None.

    None when the role disagrees between routers or no router links it to a user.
    """
    return holder_user(role_holders(hass, person, _uses_shared_lists))
