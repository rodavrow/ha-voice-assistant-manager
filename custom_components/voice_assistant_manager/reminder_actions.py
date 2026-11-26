# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Actions that set a repeating reminder and cancel a reminder, for the reminder scripts.

Home Assistant's calendar.create_event action cannot repeat an event, and no action
deletes one: only the calendar page can, through the calendar entity. These actions do the
same, for script.create_reminder and script.cancel_reminder (see examples/). Like the
calendar page, they refuse a user who may not control the calendar. A reminder that
cannot be set or found is not an error of the action: the answer says why, with status
error, so the script can pass it on to the agent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.calendar import (
    DATA_COMPONENT,
    CalendarEntity,
    CalendarEntityFeature,
)
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .recurring import RepeatError, plan_repeat

_LOGGER = logging.getLogger(__name__)

SERVICE_CREATE = "create_reminder_event"
SERVICE_CANCEL = "cancel_reminder_event"
MAX_TITLE = 200
# How far ahead a reminder to cancel is looked for.
CANCEL_WINDOW = timedelta(days=731)


def _blank_as_none(value: Any) -> Any:
    """A script passes an empty string for a field it leaves out."""
    return None if value is None or str(value).strip() == "" else value


_OPTIONAL_INT = vol.All(_blank_as_none, vol.Any(None, vol.Coerce(int)))

CREATE_SCHEMA = vol.Schema(
    {
        vol.Required("calendar"): cv.entity_domain("calendar"),
        vol.Required("summary"): vol.All(cv.string, vol.Length(min=1, max=MAX_TITLE)),
        vol.Optional("description", default=""): vol.All(cv.string, vol.Length(max=1000)),
        vol.Required("start"): cv.datetime,
        vol.Optional("duration_minutes", default=5): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=1440)
        ),
        vol.Required("repeat"): cv.string,
        vol.Optional("every"): _OPTIONAL_INT,
        vol.Optional("on_days"): vol.All(
            _blank_as_none, vol.Any(None, cv.string, [cv.string])
        ),
        vol.Optional("times"): _OPTIONAL_INT,
        vol.Optional("until"): vol.All(_blank_as_none, vol.Any(None, cv.string)),
    }
)

CANCEL_SCHEMA = vol.Schema(
    {
        vol.Required("calendar"): vol.All(cv.entity_ids, [cv.entity_domain("calendar")]),
        vol.Required("title"): vol.All(cv.string, vol.Length(min=1, max=MAX_TITLE)),
    }
)


def _error(message: str) -> ServiceResponse:
    return {"status": "error", "message": message}


async def _check_user(hass: HomeAssistant, call: ServiceCall, entity_ids: list[str]) -> None:
    """Refuse a user who may not control a calendar, as the calendar page does."""
    if not call.context.user_id:
        return  # An automation or script without a user.
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is None:
        raise Unauthorized(context=call.context)
    for entity_id in entity_ids:
        if not user.permissions.check_entity(entity_id, POLICY_CONTROL):
            raise Unauthorized(context=call.context, entity_id=entity_id)


def _calendar(hass: HomeAssistant, entity_id: str, feature: CalendarEntityFeature) -> Any:
    """The calendar entity, or a message saying why it cannot be used."""
    component = hass.data.get(DATA_COMPONENT)
    entity = component.get_entity(entity_id) if component is not None else None
    if not isinstance(entity, CalendarEntity):
        return f"{entity_id} is not a calendar."
    if not (entity.supported_features or 0) & feature:
        action = "add" if feature is CalendarEntityFeature.CREATE_EVENT else "remove"
        return f"{entity_id} cannot {action} events. Use a Local Calendar."
    return entity


def _local(when: datetime) -> datetime:
    return when.replace(tzinfo=dt_util.get_default_time_zone()) if when.tzinfo is None else (
        dt_util.as_local(when)
    )


async def _async_create(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    data = call.data
    await _check_user(hass, call, [data["calendar"]])
    entity = _calendar(hass, data["calendar"], CalendarEntityFeature.CREATE_EVENT)
    if isinstance(entity, str):
        return _error(entity)
    try:
        planned = plan_repeat(
            _local(data["start"]),
            data["repeat"],
            every=data.get("every") or 1,
            on_days=data.get("on_days") or (),
            times=data.get("times"),
            until=data.get("until"),
        )
    except RepeatError as err:
        return _error(f"Reminder not set. {err}")
    try:
        await entity.async_create_event(
            dtstart=planned.first,
            dtend=planned.first + timedelta(minutes=data["duration_minutes"]),
            summary=data["summary"],
            description=data["description"],
            rrule=planned.rrule,
        )
    except (HomeAssistantError, ValueError) as err:
        _LOGGER.warning("Could not add a repeating reminder to %s: %s", data["calendar"], err)
        return _error(f"Reminder not set. The calendar refused it: {err}")
    return {
        "status": "ok",
        "first": planned.first.strftime("%A %Y-%m-%d %H:%M"),
        "rrule": planned.rrule,
    }


async def _async_cancel(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    calendars: list[str] = call.data["calendar"]
    title = call.data["title"].strip()
    await _check_user(hass, call, calendars)
    now = dt_util.now()
    # (calendar entity, uid) -> title, for each reminder still to come.
    found: dict[tuple[Any, str], str] = {}
    for entity_id in calendars:
        entity = _calendar(hass, entity_id, CalendarEntityFeature.DELETE_EVENT)
        if isinstance(entity, str):
            return _error(entity)
        for event in await entity.async_get_events(hass, now, now + CANCEL_WINDOW):
            if event.uid:
                found[(entity, event.uid)] = event.summary
    wanted = title.casefold()
    exact = {key: name for key, name in found.items() if name.strip().casefold() == wanted}
    matches = exact or {key: name for key, name in found.items() if wanted in name.casefold()}
    names = sorted(set(matches.values()))
    if not names:
        return _error(f'No reminder called "{title}" is coming up.')
    if len(names) > 1:
        return _error(
            f'Several reminders match "{title}": {"; ".join(names)}. Ask which one.'
        )
    try:
        for entity, uid in matches:
            # Without a recurrence id, a repeating reminder goes as a whole.
            await entity.async_delete_event(uid)
    except (HomeAssistantError, ValueError) as err:
        _LOGGER.warning("Could not cancel the reminder %r: %s", names[0], err)
        return _error(f"Reminder not cancelled. The calendar refused it: {err}")
    return {"status": "ok", "removed": len(matches), "title": names[0]}


def async_register_reminder_actions(hass: HomeAssistant) -> None:
    """Register the actions. Safe to call once per Home Assistant start."""

    async def create(call: ServiceCall) -> ServiceResponse:
        return await _async_create(hass, call)

    async def cancel(call: ServiceCall) -> ServiceResponse:
        return await _async_cancel(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_CREATE, create, CREATE_SCHEMA, SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL, cancel, CANCEL_SCHEMA, SupportsResponse.ONLY
    )
