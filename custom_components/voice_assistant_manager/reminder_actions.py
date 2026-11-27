# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Actions that set a repeating reminder, cancel one and look reminders up, for the scripts.

Home Assistant's calendar.create_event action cannot repeat an event, and no action
deletes one: only the calendar page can, through the calendar entity. These actions do the
same, for script.create_reminder and script.cancel_reminder (see examples/). Like the
calendar page, they refuse a user who may not control the calendar. A reminder that
cannot be set or found is not an error of the action: the answer says why, with status
error, so the script can pass it on to the agent.

Looking up (script.list_reminders) changes nothing, but is not open to everyone: a person
is shown their own reminders and the household's, never the other person's, and
cancelling goes by the same rule. Who is asking
is not taken from the agent. The assistant notes the person speaking for each turn
(note_asker), and the action finds that note by the turn's context, which Home Assistant
passes from the assistant through the main agent and the script to the action. Without a
note, only the household's reminders are shown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.components.calendar import CalendarEntity, CalendarEntityFeature
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.core import (
    Context,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .recurring import RepeatError, plan_repeat
from .reminder_search import DATA_NOTE, Occurrence, SearchError, day_range, shown, summarise
from .reminders import HOUSEHOLD

_LOGGER = logging.getLogger(__name__)

SERVICE_CREATE = "create_reminder_event"
SERVICE_CANCEL = "cancel_reminder_event"
SERVICE_LIST = "list_reminder_events"
# hass.data[DOMAIN][ASKERS]: context id -> who is asking in that turn.
ASKERS = "reminder_askers"
# A turn's tool calls come well within this; after it the note is dropped.
ASKER_LIFETIME = timedelta(minutes=10)
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
        vol.Optional("for_whom"): vol.All(_blank_as_none, vol.Any(None, cv.string)),
    }
)


LIST_SCHEMA = vol.Schema(
    {
        vol.Required("reminders"): vol.All(
            cv.ensure_list,
            vol.Length(min=1, max=20),
            [
                {
                    vol.Required("for_whom"): vol.All(cv.string, vol.Length(min=1, max=100)),
                    vol.Required("calendar"): vol.All(
                        cv.entity_ids, [cv.entity_domain("calendar")]
                    ),
                    vol.Optional("spoken"): cv.boolean,
                }
            ],
        ),
        vol.Optional("keywords", default=""): vol.All(
            lambda value: "" if value is None else value, cv.string, vol.Length(max=MAX_TITLE)
        ),
        vol.Optional("on_day"): vol.All(_blank_as_none, vol.Any(None, cv.string)),
        vol.Optional("until_day"): vol.All(_blank_as_none, vol.Any(None, cv.string)),
    }
)


@dataclass(frozen=True)
class _Asker:
    name: str  # as create_reminder's for_whom: the person's name, lower case
    at: datetime
    user_id: str | None = None  # the person's Home Assistant user, if the assistant has one


def note_asker(
    hass: HomeAssistant, context: Context | None, name: str | None, user_id: str | None = None
) -> None:
    """Note who is speaking in the turn with this context, or that nobody known is.

    Called for every turn, so a context a satellite uses again never keeps an earlier
    person. `user_id` is the Home Assistant user the assistant's settings give the person.
    """
    if context is None:
        return
    now = dt_util.utcnow()
    askers: dict[str, _Asker] = hass.data.setdefault(DOMAIN, {}).setdefault(ASKERS, {})
    for context_id in [key for key, seen in askers.items() if now - seen.at > ASKER_LIFETIME]:
        del askers[context_id]
    shown = " ".join(str(name or "").split()).casefold()
    if shown and shown != HOUSEHOLD:
        askers[context.id] = _Asker(shown, now, user_id or None)
    else:
        askers.pop(context.id, None)


def forget_asker(hass: HomeAssistant, context: Context | None) -> None:
    """The turn with this context is over: its note no longer says who is asking."""
    if context is not None:
        hass.data.get(DOMAIN, {}).get(ASKERS, {}).pop(context.id, None)


def _noted(hass: HomeAssistant, context: Context | None) -> _Asker | None:
    if context is None:
        return None
    seen: _Asker | None = hass.data.get(DOMAIN, {}).get(ASKERS, {}).get(context.id)
    if seen is None or dt_util.utcnow() - seen.at > ASKER_LIFETIME:
        return None
    return seen


def asker(hass: HomeAssistant, context: Context | None) -> str | None:
    """Who is asking in the turn with this context, or None when that is not known."""
    seen = _noted(hass, context)
    return seen.name if seen is not None else None


# Whose a calendar is. The caller's for_whom label is not proof: any Home Assistant user
# can call an action with any label. The assistants' settings list each person's reminder
# calendars and the household's, so those decide wherever they say something.


@dataclass(frozen=True)
class _Owners:
    """Whose reminders a calendar holds, by the settings of every assistant."""

    users: frozenset[str]
    names: frozenset[str]
    household: bool


def _listed(hass: HomeAssistant) -> dict[str, _Owners]:
    """Every calendar an assistant lists as a reminder calendar, with whose it is."""
    users: dict[str, set[str]] = {}
    names: dict[str, set[str]] = {}
    household: set[str] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        reminders = entry.options.get("reminders") or {}
        for role in ("owner", "partner"):
            person = entry.options.get(role) or {}
            name = " ".join(str(person.get("name") or "").split()).casefold()
            for calendar in reminders.get(f"{role}_calendars") or []:
                users.setdefault(calendar, set())
                names.setdefault(calendar, set())
                if person.get("user"):
                    users[calendar].add(str(person["user"]))
                if name:
                    names[calendar].add(name)
        household.update(reminders.get("household_calendars") or [])
    return {
        calendar: _Owners(
            frozenset(users.get(calendar, ())),
            frozenset(names.get(calendar, ())),
            calendar in household,
        )
        for calendar in {*users, *household}
    }


@dataclass(frozen=True)
class _Caller:
    """Who calls an action: a person's turn, a user calling it directly, or an automation."""

    kind: Literal["turn", "user", "automation"]
    name: str | None = None
    user_id: str | None = None


def _caller(hass: HomeAssistant, call: ServiceCall) -> _Caller:
    seen = _noted(hass, call.context)
    if seen is not None:
        return _Caller("turn", seen.name, seen.user_id)
    if call.context.user_id:
        return _Caller("user", None, call.context.user_id)
    return _Caller("automation")


def _may_use(caller: _Caller, owners: _Owners | None) -> bool | None:
    """Whether the settings let this caller at a calendar; None when they do not say.

    They do not say for a calendar no assistant lists, and for an automation, which is
    trusted as before; then the for_whom label decides, as it always did. A user calling
    directly has no turn that vouches for a label, so for them an unlisted calendar is
    refused.
    """
    if caller.kind == "automation":
        return None
    if owners is None:
        return False if caller.kind == "user" else None
    if owners.household:
        return True
    if caller.user_id is not None:
        return caller.user_id in owners.users
    return caller.name in owners.names


def _error(message: str) -> ServiceResponse:
    return {"status": "error", "message": message}


async def _check_user(
    hass: HomeAssistant, call: ServiceCall, entity_ids: list[str], policy: str = POLICY_CONTROL
) -> None:
    """Refuse a user who may not control (or read) a calendar, as the calendar page does."""
    if not call.context.user_id:
        return  # An automation or script without a user.
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is None:
        raise Unauthorized(context=call.context)
    for entity_id in entity_ids:
        if not user.permissions.check_entity(entity_id, policy):
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


def _may_not_cancel(hass: HomeAssistant, call: ServiceCall) -> str | None:
    """Why this call may not cancel in these calendars, or None when it may.

    A person cancels their own reminders and the household's, never the other person's.
    Who is asking is the assistant's note for the turn, or the user who calls the action.
    Whose a calendar is comes from the assistants' settings (_may_use); for a calendar
    they do not list it is `for_whom`, which the script passes. A call that does not say
    whose calendars they are is refused, so an older script cannot get round this, unless
    it has no user and is no turn (an automation): that may cancel the household's, or,
    as before, anything when it names nobody.
    """
    caller = _caller(hass, call)
    for_whom = " ".join(str(call.data.get("for_whom") or "").split()).casefold()
    if not for_whom and caller.kind != "automation":
        return (
            "Reminder not cancelled. The cancel_reminder script must pass for_whom to "
            "cancel_reminder_event: it needs updating. Tell the person you cannot cancel "
            "reminders right now."
        )
    listed = _listed(hass)
    for calendar in call.data["calendar"]:
        allowed = _may_use(caller, listed.get(calendar))
        if allowed is None:
            allowed = not for_whom or for_whom in {HOUSEHOLD, caller.name}
        if not allowed:
            whose = f"this one is for {for_whom}" if caller.kind != "user" else (
                f"{calendar} holds neither"
            )
            return (
                "Reminder not cancelled. A person can only cancel their own reminders and "
                f"the household's, and {whose}. Tell them so."
            )
    return None


async def _async_cancel(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    calendars: list[str] = call.data["calendar"]
    title = call.data["title"].strip()
    # Home Assistant's own permission first, so a user without it is refused as before.
    await _check_user(hass, call, calendars)
    if (refusal := _may_not_cancel(hass, call)) is not None:
        return _error(refusal)
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
    # Which reminders match is decided on the whole titles; the agent gets them cut to size.
    names = sorted(set(matches.values()))
    if not names:
        return _error(f'No reminder called "{title}" is coming up.')
    if len(names) > 1:
        return _error(
            f'Several reminders match "{title}": {"; ".join(map(shown, names))}. Ask which one.'
        )
    try:
        for entity, uid in matches:
            # Without a recurrence id, a repeating reminder goes as a whole.
            await entity.async_delete_event(uid)
    except (HomeAssistantError, ValueError) as err:
        _LOGGER.warning(
            "Could not cancel a reminder in %s: %s",
            ", ".join(sorted({entity.entity_id for entity, _ in matches})),
            err,
        )
        return _error(f"Reminder not cancelled. The calendar refused it: {err}")
    return {"status": "ok", "removed": len(matches), "title": shown(names[0])}


def _as_local(when: datetime | date) -> tuple[datetime, bool]:
    """A calendar time as a local datetime, and whether it was a whole day."""
    if isinstance(when, datetime):
        return _local(when), False
    zone = dt_util.get_default_time_zone()
    return datetime(when.year, when.month, when.day, tzinfo=zone), True


async def _async_list(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    caller = _caller(hass, call)
    by_label = {HOUSEHOLD} | ({caller.name} if caller.name else set())
    listed = _listed(hass)
    mine: list[dict[str, Any]] = []
    for entry in call.data["reminders"]:
        labelled = " ".join(entry["for_whom"].split()).casefold() in by_label
        allowed = [
            (labelled if (may := _may_use(caller, listed.get(calendar))) is None else may)
            for calendar in entry["calendar"]
        ]
        calendars = [
            calendar
            for calendar, usable in zip(entry["calendar"], allowed, strict=True)
            if usable
        ]
        if calendars:
            mine.append({**entry, "calendar": calendars})
    if not mine:
        return _error("No reminder calendar is set up for the person asking.")
    await _check_user(
        hass, call, [entity_id for entry in mine for entity_id in entry["calendar"]], POLICY_READ
    )
    try:
        start, end, ranged = day_range(
            call.data.get("on_day"), call.data.get("until_day"), dt_util.now()
        )
    except SearchError as err:
        return _error(f"Reminders not looked up. {err}")
    occurrences: list[Occurrence] = []
    for entry in mine:
        for_whom = " ".join(entry["for_whom"].split()).casefold()
        for entity_id in entry["calendar"]:
            component = hass.data.get(DATA_COMPONENT)
            entity = component.get_entity(entity_id) if component is not None else None
            if not isinstance(entity, CalendarEntity):
                return _error(f"{entity_id} is not a calendar.")
            try:
                events = await entity.async_get_events(hass, start, end)
            except HomeAssistantError as err:
                _LOGGER.warning("Could not read the reminders in %s: %s", entity_id, err)
                return _error(f"Reminders not looked up. The calendar refused: {err}")
            for event in events:
                begins, all_day = _as_local(event.start)
                if begins < start and not all_day:
                    continue  # Already under way: it is not coming up any more.
                occurrences.append(
                    Occurrence(
                        key=f"{entity_id}/{event.uid or f'{event.summary}/{begins}'}",
                        title=event.summary,
                        description=event.description or "",
                        for_whom=for_whom,
                        spoken=entry.get("spoken"),
                        start=begins,
                        end=_as_local(event.end)[0],
                        rrule=event.rrule,
                        all_day=all_day,
                    )
                )
    found, more = summarise(occurrences, call.data["keywords"], ranged=ranged)
    answer: dict[str, Any] = {
        "status": "ok",
        "looked_at": sorted({entry["for_whom"].strip().casefold() for entry in mine}),
        "from": start.strftime("%A %Y-%m-%d %H:%M"),
        "count": len(found),
        "reminders": found,
        "data_note": DATA_NOTE,
    }
    if ranged:
        answer["until"] = (end - timedelta(minutes=1)).strftime("%A %Y-%m-%d %H:%M")
    notes = []
    if caller.kind == "automation":
        notes.append("Who is asking is not known, so only the household's reminders are shown.")
    if more:
        notes.append("There are more reminders than these. Ask for a day or a keyword.")
    if notes:
        answer["message"] = " ".join(notes)
    return answer


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

    async def look_up(call: ServiceCall) -> ServiceResponse:
        return await _async_list(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_LIST, look_up, LIST_SCHEMA, SupportsResponse.ONLY
    )
