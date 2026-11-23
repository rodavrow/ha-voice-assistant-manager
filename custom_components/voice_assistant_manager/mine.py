# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who a Home Assistant user is in each router, for the My assistant page.

A pure lookup in the saved settings. The role is never taken from the page: callers pass
the id of the signed-in user.
"""

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal, TypedDict

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.components.calendar.const import DATA_COMPONENT, CalendarEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .config_flow import current_settings, person_names
from .const import DOMAIN
from .memory import Person, shared_list_user
from .reminders import assistant_name, for_whom

_LOGGER = logging.getLogger(__name__)

Role = Literal["owner", "partner"]

# A calendar that does not answer in this many seconds is listed as unreadable.
FETCH_TIMEOUT = 30
ROLES: tuple[Role, ...] = ("owner", "partner")
MEMORY_PERSON: dict[Role, Person] = {"owner": "owner", "partner": "wife"}


class MyRouter(TypedDict):
    entry_id: str
    title: str
    role: Role
    memory_person: Person  # "owner" or "wife"
    name: str  # this person's name in that router, or "Person 1"/"Person 2"
    other_name: str  # never their data, only for labels such as "household"


def my_routers(hass: HomeAssistant, user_id: str | None) -> list[MyRouter]:
    """Every router in which this user is Person 1 or Person 2, sorted by title."""
    if not user_id:
        return []
    found: list[MyRouter] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        # config_flow.current_settings has no annotations yet
        settings = current_settings(hass, entry)  # type: ignore[no-untyped-call]
        names = person_names(settings)
        for role in ROLES:
            if (settings.get(role) or {}).get("user") != user_id:
                continue
            other: Role = "partner" if role == "owner" else "owner"
            found.append(
                {
                    "entry_id": entry.entry_id,
                    "title": entry.title,
                    "role": role,
                    "memory_person": MEMORY_PERSON[role],
                    "name": names[role],
                    "other_name": names[other],
                }
            )
    return sorted(found, key=lambda router: (router["title"], router["entry_id"]))


def my_router(hass: HomeAssistant, user_id: str | None, entry_id: str) -> MyRouter | None:
    """This user's role in one router, or None when they are not in it."""
    for router in my_routers(hass, user_id):
        if router["entry_id"] == entry_id:
            return router
    return None


def owns_global_list(hass: HomeAssistant, user_id: str | None, memory_person: Person) -> bool:
    """Whether the "all assistants" list of `memory_person`'s role is this user's.

    That list is keyed by role and shared by every router, so it is offered only while the
    role is the same person everywhere (memory.shared_list_ok, the rule the voice side
    follows too) and that person is this user.
    """
    return bool(user_id) and shared_list_user(hass, memory_person) == user_id


def _calendars(options: Mapping[str, Any], key: str) -> list[str]:
    return [str(entity_id) for entity_id in (options.get("reminders") or {}).get(key) or []]


def own_calendars(options: Mapping[str, Any], role: Role) -> list[str]:
    """The calendars holding this person's own reminders."""
    return _calendars(options, f"{role}_calendars")


def household_calendars(options: Mapping[str, Any]) -> list[str]:
    """The calendars holding reminders for the whole household."""
    return _calendars(options, "household_calendars")


LOOKAHEAD = timedelta(days=30)


class Reminder(TypedDict):
    calendar: str
    calendar_name: str
    uid: str | None
    recurrence_id: str | None
    title: str
    start: str
    end: str
    all_day: bool
    whose: Literal["mine", "household"]
    can_delete: bool


class ReminderList(TypedDict):
    reminders: list[Reminder]
    unreadable: list[str]  # calendar ids that failed or are missing


class ReminderRefused(Exception):
    """A reminder action that was refused; `key` is the error key in options.error."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _calendar_entity(hass: HomeAssistant, entity_id: str) -> CalendarEntity | None:
    component = hass.data.get(DATA_COMPONENT)
    return component.get_entity(entity_id) if component else None


def _calendar_name(hass: HomeAssistant, entity: CalendarEntity) -> str:
    state = hass.states.get(entity.entity_id)
    friendly_name = state.attributes.get("friendly_name") if state else None
    return str(friendly_name or entity.entity_id)


def _can_delete(entity: CalendarEntity) -> bool:
    return bool((entity.supported_features or 0) & CalendarEntityFeature.DELETE_EVENT)


def _my_calendars(
    hass: HomeAssistant, mine: MyRouter
) -> list[tuple[str, Literal["mine", "household"]]]:
    entry = hass.config_entries.async_get_entry(mine["entry_id"])
    if entry is None:
        return []
    # config_flow.current_settings has no annotations yet
    settings = current_settings(hass, entry)  # type: ignore[no-untyped-call]
    own: list[tuple[str, Literal["mine", "household"]]] = [
        (entity_id, "mine") for entity_id in own_calendars(settings, mine["role"])
    ]
    house: list[tuple[str, Literal["mine", "household"]]] = [
        (entity_id, "household") for entity_id in household_calendars(settings)
    ]
    return own + house


async def _events_ahead(
    hass: HomeAssistant, entity: CalendarEntity, now: datetime
) -> list[CalendarEvent]:
    async with asyncio.timeout(FETCH_TIMEOUT):
        return await entity.async_get_events(hass, now, now + LOOKAHEAD)


async def async_list_reminders(hass: HomeAssistant, mine: MyRouter, now: datetime) -> ReminderList:
    """This person's own and the household's reminders in the next 30 days, soonest first."""
    unreadable: list[str] = []
    found: dict[tuple[str, str | None, str], tuple[CalendarEvent, Reminder]] = {}
    for entity_id, whose in _my_calendars(hass, mine):
        entity = _calendar_entity(hass, entity_id)
        if entity is None:
            if entity_id not in unreadable:
                unreadable.append(entity_id)
            continue
        try:
            events = await _events_ahead(hass, entity, now)
        except Exception:  # noqa: BLE001 - any calendar failure must not hide the others
            _LOGGER.warning("Could not read calendar %s for My assistant", entity_id, exc_info=True)
            if entity_id not in unreadable:
                unreadable.append(entity_id)
            continue
        name = _calendar_name(hass, entity)
        for item in events:
            key = (entity_id, item.uid, item.start.isoformat())
            if key in found:
                continue
            found[key] = (
                item,
                {
                    "calendar": entity_id,
                    "calendar_name": name,
                    "uid": item.uid,
                    "recurrence_id": item.recurrence_id,
                    "title": item.summary,
                    "start": item.start.isoformat(),
                    "end": item.end.isoformat(),
                    "all_day": item.all_day,
                    "whose": whose,
                    "can_delete": _can_delete(entity) and item.uid is not None,
                },
            )
    ordered = sorted(found.values(), key=lambda pair: pair[0].start_datetime_local)
    return {"reminders": [reminder for _, reminder in ordered], "unreadable": unreadable}


def can_add_reminders(hass: HomeAssistant) -> bool:
    """Whether script.create_reminder exists, the only way this page adds a reminder."""
    return hass.services.has_service("script", "create_reminder")


async def async_add_reminder(
    hass: HomeAssistant,
    mine: MyRouter,
    scope: Literal["me", "household"],
    title: str,
    announce: bool,
    in_minutes: int | None,
    at_time: str | None,
    on_day: str | None,
) -> dict[str, Any]:
    """Create a reminder through script.create_reminder and return its response unchanged."""
    if not can_add_reminders(hass):
        raise ReminderRefused("mine_no_script")
    entry = hass.config_entries.async_get_entry(mine["entry_id"])
    if entry is None:
        raise ReminderRefused("mine_no_script")
    # config_flow.current_settings has no annotations yet
    settings = current_settings(hass, entry)  # type: ignore[no-untyped-call]
    # Without a name, for_whom would be the "Person 1" fallback, which no calendar is for.
    own_name = str((settings.get(mine["role"]) or {}).get("name") or "").strip()
    if scope == "me" and not own_name:
        raise ReminderRefused("mine_no_name")
    data: dict[str, Any] = {
        "title": title,
        "for_whom": for_whom(mine["name"]) if scope == "me" else "household",
        "assistant": assistant_name(
            mine["title"], (settings.get("reminders") or {}).get("assistant")
        ),
        "announce": announce,
    }
    for field, value in (("in_minutes", in_minutes), ("at_time", at_time), ("on_day", on_day)):
        if value is not None:
            data[field] = value
    response: dict[str, Any] | None = await hass.services.async_call(
        "script", "create_reminder", data, blocking=True, return_response=True
    )
    return response or {}


async def async_delete_reminder(
    hass: HomeAssistant, mine: MyRouter, calendar: str, uid: str, recurrence_id: str | None
) -> None:
    """Delete one reminder, only if it is in this person's own or household calendars."""
    if calendar not in [entity_id for entity_id, _ in _my_calendars(hass, mine)]:
        raise ReminderRefused("mine_cannot_delete")
    entity = _calendar_entity(hass, calendar)
    if entity is None or not _can_delete(entity):
        raise ReminderRefused("mine_cannot_delete")
    try:
        events = await _events_ahead(hass, entity, dt_util.utcnow())
    except Exception as err:  # noqa: BLE001 - refuse rather than delete blind
        _LOGGER.warning("Could not read calendar %s before deleting", calendar, exc_info=True)
        raise ReminderRefused("mine_cannot_delete") from err
    if not any(item.uid == uid and item.recurrence_id == recurrence_id for item in events):
        raise ReminderRefused("mine_cannot_delete")
    await entity.async_delete_event(uid, recurrence_id)

