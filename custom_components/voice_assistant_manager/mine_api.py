# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a signed-in person may ask for about their own assistant, over the websocket.

These commands are open to every user, so each one works out who is asking from the
connection alone and refuses anything that is not that person's own router, persona,
facts or reminders. Nothing here takes a role or a user from the page.
"""

import logging
import re
from datetime import date
from typing import Any, Literal

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .chat_show import get_manager
from .favorites import (
    HOUSEHOLD,
    MAX_NAME_LENGTH,
    ListKey,
    async_get_favorites,
    own_list,
)
from .memory import (
    LineTooLongError,
    TooManyItemsError,
    async_get_memory,
    entry_key,
    global_key,
)
from .mine import (
    MyRouter,
    ReminderRefused,
    async_add_reminder,
    async_delete_reminder,
    async_list_reminders,
    can_add_reminders,
    my_router,
    my_routers,
    owns_global_list,
)
from .panel import panel_state
from .panel_api import (
    LINE_PREVIEW_LENGTH,
    chat_default_limits,
    chat_speakers,
    chat_start_refusal,
    favorites_change,
    favorites_search,
    routers_for_chat,
    shown_chat,
)
from .reminders import assistant_name
from .settings import (
    current_settings,
    memory_list_choices,
    settings_error,
    settings_to_save,
)
from .ws_helpers import MEMORY_LINES
from .ws_helpers import error_text as _error_text
from .ws_helpers import refused as _refused
from .ws_helpers import strings as _strings

_LOGGER = logging.getLogger(__name__)

PREFIX = "voice_assistant_manager/me"
MAX_PERSONA_LENGTH = 2000
MAX_TITLE_LENGTH = 200
# Hard limits of what a command takes at all. The page never comes near them, and a
# friendlier answer exists for the fields a person can get wrong (the title, the persona).
MAX_MINUTES = 525600  # a year
MAX_SCRIPT_MESSAGE = 500
_ID = vol.All(str, vol.Length(max=64))
_WORD = vol.All(str, vol.Length(max=32))
_NAME = vol.All(str, vol.Length(max=255))
_LONG_TEXT = vol.All(str, vol.Length(max=10 * MAX_PERSONA_LENGTH))
TIME_OF_DAY = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")
# The days the page offers (DAYS in frontend/mine.js), or a date as MM-DD.
DAYS = (
    "today",
    "tomorrow",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
MONTH_DAY = re.compile(r"([0-9]{2})-([0-9]{2})")
LEAP_YEAR = 2000  # so that 02-29 counts as a day that exists


def _is_day(on_day: str) -> bool:
    """Whether `on_day` is one of the page's days or a date that exists, as MM-DD."""
    if on_day in DAYS:
        return True
    found = MONTH_DAY.fullmatch(on_day)
    if found is None:
        return False
    try:
        date(LEAP_YEAR, int(found.group(1)), int(found.group(2)))
    except ValueError:
        return False
    return True


def _persona(settings: dict[str, Any], mine: MyRouter) -> str:
    return str((settings.get(mine["role"]) or {}).get("persona") or "")


def _calendar_rows(hass: HomeAssistant, settings: dict[str, Any], mine: MyRouter) -> list[Any]:
    """The watched calendars announced for this person or for everyone, nothing else."""
    rows = []
    for row in settings.get("calendar_rows") or []:
        if not isinstance(row, dict) or not row.get("calendar"):
            continue
        who = row.get("for") or "household"
        if who not in (mine["role"], "household"):
            continue
        state = hass.states.get(row["calendar"])
        name = state.attributes.get("friendly_name") if state else None
        rows.append(
            {
                "calendar": row["calendar"],
                "name": str(name or row["calendar"]),
                "for": who,
                "when": {
                    "all_day_time": row.get("all_day_time"),
                    "all_day_day": row.get("all_day_day"),
                    "lead_minutes": row.get("lead_minutes") or 0,
                },
            }
        )
    return rows


def _memory_keys(hass: HomeAssistant, user_id: str, mine: MyRouter) -> tuple[str, ...]:
    """The only lists of facts this person may read or change.

    The "all assistants" list is shared by everyone holding this role in any router, so it
    is offered only while this user is the one holding it everywhere.
    """
    own = entry_key(mine["entry_id"], mine["memory_person"])
    if owns_global_list(hass, user_id, mine["memory_person"]):
        return (own, global_key(mine["memory_person"]))
    return (own,)


async def _router_view(
    hass: HomeAssistant, user_id: str, mine: MyRouter
) -> dict[str, Any] | None:
    entry = hass.config_entries.async_get_entry(mine["entry_id"])
    if entry is None:
        return None
    settings: dict[str, Any] = current_settings(hass, entry)
    memory = await async_get_memory(hass)
    keys = _memory_keys(hass, user_id, mine)
    return {
        "entry_id": mine["entry_id"],
        "title": mine["title"],
        "role": mine["role"],
        "name": mine["name"],
        "assistant": assistant_name(
            mine["title"], (settings.get("reminders") or {}).get("assistant")
        ),
        "persona": _persona(settings, mine),
        "strangers_mode": (settings.get("strangers") or {}).get("mode", "ai"),
        "calendars": _calendar_rows(hass, settings, mine),
        # Without the script the page hides its add form instead of offering one that fails.
        "can_add": can_add_reminders(hass),
        "memory": [
            {"key": key, "label": label, "items": memory.items(key)}
            for key, label in memory_list_choices(memory, mine["entry_id"], settings)
            if key in keys
        ],
    }


async def _own_router(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> MyRouter | None:
    """The asking user's role in the router the page named, or None after answering."""
    mine = my_router(hass, connection.user.id, msg["entry_id"])
    if mine is None:
        connection.send_result(msg["id"], _refused(await _error_text(hass, "mine_not_yours")))
    return mine


async def _failed(hass: HomeAssistant, mine: MyRouter, err: Exception) -> dict[str, Any]:
    # What Home Assistant says can name entities, a script's insides or the other person's
    # calendar, so it stays in the log.
    _LOGGER.error("My assistant action failed for assistant %s: %s", mine["entry_id"], err)
    return _refused(await _error_text(hass, "mine_failed"))


@websocket_api.websocket_command({vol.Required("type"): f"{PREFIX}/get"})
@websocket_api.async_response
async def ws_get(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The routers this person is in, with only their own persona, facts and calendars."""
    strings = {
        key: text
        for key, text in (await _strings(hass)).items()
        if key.startswith(("mine.", "error."))
    }
    user_id = connection.user.id
    views = [await _router_view(hass, user_id, mine) for mine in my_routers(hass, user_id)]
    routers = [view for view in views if view is not None]
    connection.send_result(msg["id"], {"routers": routers, "strings": strings})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/save_persona",
        vol.Required("entry_id"): _ID,
        vol.Required("persona"): _LONG_TEXT,
    }
)
@websocket_api.async_response
async def ws_save_persona(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Save this person's persona and nothing else, by the rules of the other settings forms."""
    mine = await _own_router(hass, connection, msg)
    if mine is None:
        return
    persona = msg["persona"].strip()
    if len(persona) > MAX_PERSONA_LENGTH:
        text = await _error_text(hass, "mine_persona_too_long")
        connection.send_result(msg["id"], _refused(text))
        return
    entry = hass.config_entries.async_get_entry(mine["entry_id"])
    if entry is None:
        connection.send_result(msg["id"], _refused(await _error_text(hass, "mine_not_yours")))
        return
    current: dict[str, Any] = current_settings(hass, entry)
    role = mine["role"]
    settings = {**current, role: {**(current.get(role) or {}), "persona": persona}}
    error = settings_error(hass, entry, settings)
    if error is not None:
        # Only the persona changed, so what is wrong is in settings only an administrator
        # can change, such as an agent that gained tools after it was saved. The reason
        # stays in the log: it can name other routers, agents and calendars. Nothing is saved.
        _LOGGER.warning(
            "A persona for assistant %s was not saved, its settings are refused: %s",
            entry.entry_id,
            error,
        )
        text = await _error_text(hass, "mine_settings_need_admin")
        connection.send_result(msg["id"], _refused(text))
        return
    options = settings_to_save(entry, settings)
    hass.config_entries.async_update_entry(entry, options=options)
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/save_memory",
        vol.Required("entry_id"): _ID,
        vol.Required("key"): _NAME,
        vol.Required("shown"): MEMORY_LINES,
        vol.Required("lines"): MEMORY_LINES,
    }
)
@websocket_api.async_response
async def ws_save_memory(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Save one edited list of this person's own facts: the page may only name theirs."""
    mine = await _own_router(hass, connection, msg)
    if mine is None:
        return
    key = msg["key"]
    if key not in _memory_keys(hass, connection.user.id, mine):
        connection.send_result(msg["id"], _refused(await _error_text(hass, "mine_not_yours")))
        return
    memory = await async_get_memory(hass)
    shown_ids = set(msg["shown"])
    shown = [item for item in memory.items(key) if item["id"] in shown_ids]
    try:
        await memory.async_apply_edit(key, shown, msg["lines"])
    except LineTooLongError as err:
        preview = f"{err.line[:LINE_PREVIEW_LENGTH]}…"
        text = await _error_text(hass, "line_too_long", line=preview)
        connection.send_result(msg["id"], _refused(text))
        return
    except TooManyItemsError:
        text = await _error_text(hass, "too_many_facts")
        connection.send_result(msg["id"], _refused(text))
        return
    connection.send_result(msg["id"], {"ok": True, "items": memory.items(key)})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{PREFIX}/reminders", vol.Required("entry_id"): _ID}
)
@websocket_api.async_response
async def ws_reminders(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """This person's own and the household's reminders in the next 30 days."""
    mine = await _own_router(hass, connection, msg)
    if mine is None:
        return
    connection.send_result(msg["id"], await async_list_reminders(hass, mine, dt_util.utcnow()))


async def _reminder_error(
    hass: HomeAssistant,
    scope: str,
    title: str,
    in_minutes: int | None,
    at_time: str | None,
    on_day: str | None,
) -> str | None:
    """The text for why these fields cannot make a reminder, None when they can."""
    key: str | None = None
    if scope not in ("me", "household"):
        key = "mine_scope_invalid"
    elif not title or len(title) > MAX_TITLE_LENGTH:
        key = "mine_title_invalid"
    elif isinstance(in_minutes, bool) or (in_minutes is None) == (at_time is None):
        key = "mine_time_invalid"
    elif in_minutes is not None and (in_minutes < 1 or on_day is not None):
        key = "mine_time_invalid"
    elif at_time is not None and not TIME_OF_DAY.fullmatch(at_time):
        key = "mine_time_invalid"
    elif on_day is not None and not _is_day(on_day):
        key = "mine_time_invalid"
    return await _error_text(hass, key) if key else None


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/add_reminder",
        vol.Required("entry_id"): _ID,
        vol.Required("scope"): _WORD,
        vol.Required("title"): vol.All(str, vol.Length(max=10 * MAX_TITLE_LENGTH)),
        vol.Required("announce"): bool,
        vol.Optional("in_minutes"): vol.All(int, vol.Range(max=MAX_MINUTES)),
        vol.Optional("at_time"): _WORD,
        vol.Optional("on_day"): _WORD,
    }
)
@websocket_api.async_response
async def ws_add_reminder(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Add a reminder through the script that makes them, for this person or the household."""
    mine = await _own_router(hass, connection, msg)
    if mine is None:
        return
    title = msg["title"].strip()
    in_minutes: int | None = msg.get("in_minutes")
    at_time: str | None = msg.get("at_time")
    on_day: str | None = msg.get("on_day")
    refusal = await _reminder_error(hass, msg["scope"], title, in_minutes, at_time, on_day)
    if refusal is not None:
        connection.send_result(msg["id"], _refused(refusal))
        return
    scope: Literal["me", "household"] = "me" if msg["scope"] == "me" else "household"
    try:
        response = await async_add_reminder(
            hass, mine, scope, title, msg["announce"], in_minutes, at_time, on_day
        )
    except ReminderRefused as err:
        connection.send_result(msg["id"], _refused(await _error_text(hass, err.key)))
        return
    except (HomeAssistantError, vol.Invalid) as err:
        connection.send_result(msg["id"], await _failed(hass, mine, err))
        return
    # Only what the page shows: a script may answer with more than a person should see.
    connection.send_result(
        msg["id"],
        {
            key: str(response[key])[:MAX_SCRIPT_MESSAGE]
            for key in ("status", "message")
            if response.get(key) is not None
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/delete_reminder",
        vol.Required("entry_id"): _ID,
        vol.Required("calendar"): _NAME,
        vol.Required("uid"): _NAME,
        vol.Optional("recurrence_id"): vol.Any(_NAME, None),
    }
)
@websocket_api.async_response
async def ws_delete_reminder(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Delete one reminder, only from this person's own or the household's calendars."""
    mine = await _own_router(hass, connection, msg)
    if mine is None:
        return
    try:
        await async_delete_reminder(
            hass, mine, msg["calendar"], msg["uid"], msg.get("recurrence_id")
        )
    except ReminderRefused as err:
        connection.send_result(msg["id"], _refused(await _error_text(hass, err.key)))
        return
    except (HomeAssistantError, vol.Invalid) as err:
        connection.send_result(msg["id"], await _failed(hass, mine, err))
        return
    connection.send_result(msg["id"], {"ok": True})


# Favorites: the person's own list and the household's, never the other person's.

ListChoice = Literal["own", "household"]
LIST_CHOICES: tuple[ListChoice, ...] = ("own", "household")


async def _is_linked(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> bool:
    """Whether the asking user is a person in any assistant; answers the page when not."""
    if my_routers(hass, connection.user.id):
        return True
    text = await _error_text(hass, "mine_favorites_not_linked")
    connection.send_result(msg["id"], _refused(text))
    return False


async def _chosen_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> ListKey | None:
    """The list the page chose, if this user may change it; else None after answering."""
    if not await _is_linked(hass, connection, msg):
        return None
    if msg["list"] == "household":
        return HOUSEHOLD
    state, own = own_list(hass, connection.user.id)
    if own is None:
        key = "mine_favorites_conflict" if state == "conflict" else "mine_favorites_off"
        connection.send_result(msg["id"], _refused(await _error_text(hass, key)))
    return own


@websocket_api.websocket_command({vol.Required("type"): f"{PREFIX}/favorites/get"})
@websocket_api.async_response
async def ws_favorites_get(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """This person's own favorites and the household's.

    `own_state` says whether there is an own list: "ok"; "conflict" while the assistants
    with favorites on disagree who holds this person's role; "off" when this person holds
    no role in an assistant with favorites on. `conflict` is True only for "conflict".
    """
    if not await _is_linked(hass, connection, msg):
        return
    store = await async_get_favorites(hass)
    state, own = own_list(hass, connection.user.id)
    connection.send_result(
        msg["id"],
        {
            "own": store.items(own) if own is not None else None,
            "own_state": state,
            "conflict": state == "conflict",
            "household": store.items(HOUSEHOLD),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/favorites/add",
        vol.Required("list"): vol.In(LIST_CHOICES),
        vol.Required("data"): dict,
    }
)
@websocket_api.async_response
async def ws_favorites_add(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Add a favorite to this person's own list or the household's."""
    key = await _chosen_list(hass, connection, msg)
    if key is not None:
        connection.send_result(msg["id"], await favorites_change(hass, key, "add", msg))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/favorites/update",
        vol.Required("list"): vol.In(LIST_CHOICES),
        vol.Required("favorite_id"): _ID,
        vol.Required("data"): dict,
    }
)
@websocket_api.async_response
async def ws_favorites_update(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Change one favorite of this person's own list or the household's."""
    key = await _chosen_list(hass, connection, msg)
    if key is not None:
        connection.send_result(msg["id"], await favorites_change(hass, key, "update", msg))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/favorites/remove",
        vol.Required("list"): vol.In(LIST_CHOICES),
        vol.Required("favorite_id"): _ID,
    }
)
@websocket_api.async_response
async def ws_favorites_remove(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Remove one favorite from this person's own list or the household's."""
    key = await _chosen_list(hass, connection, msg)
    if key is not None:
        connection.send_result(msg["id"], await favorites_change(hass, key, "remove", msg))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/favorites/search",
        vol.Required("name"): vol.All(str, vol.Length(max=MAX_NAME_LENGTH)),
    }
)
@websocket_api.async_response
async def ws_favorites_search(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Search Music Assistant for something to save as a favorite."""
    if await _is_linked(hass, connection, msg):
        connection.send_result(msg["id"], await favorites_search(hass, msg["name"]))


# The chat show. A person starts one between assistants they are in, on any speaker a chat
# can play on or on their own page, with limits of their own, held to the same ranges as an
# administrator's; the saved ones stay an administrator's to change. Anyone in an assistant
# may stop a chat and read its lines, which were spoken aloud.


async def _may_chat(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> set[str] | None:
    """The asking user's routers, by entry id, or None after answering one who has none."""
    mine = {router["entry_id"] for router in my_routers(hass, connection.user.id)}
    if mine:
        return mine
    connection.send_result(msg["id"], _refused(await _error_text(hass, "mine_chat_not_linked")))
    return None


@websocket_api.websocket_command({vol.Required("type"): f"{PREFIX}/chat/state"})
@websocket_api.async_response
async def ws_chat_state(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The chat, and what this person may choose from for a new one."""
    mine = await _may_chat(hass, connection, msg)
    if mine is None:
        return
    connection.send_result(
        msg["id"],
        {
            "ok": True,
            "chat": shown_chat(hass),
            "defaults": chat_default_limits(hass),
            "routers": [r for r in routers_for_chat(hass) if r["entry_id"] in mine],
            "speakers": chat_speakers(hass),
            # The stage card, which the page shows for a chat on "This device".
            "stage_script": panel_state(hass).stage_url,
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/chat/start",
        vol.Required("a"): _ID,
        vol.Required("b"): _ID,
        vol.Required("speaker"): _NAME,
        vol.Optional("topic", default=""): _LONG_TEXT,
        vol.Optional("limits", default=dict): vol.All(dict, vol.Length(max=16)),
    }
)
@websocket_api.async_response
async def ws_chat_start(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Start a chat between two of this person's assistants, or say why not."""
    mine = await _may_chat(hass, connection, msg)
    if mine is None:
        return
    if msg["a"] not in mine or msg["b"] not in mine:
        connection.send_result(msg["id"], _refused(await _error_text(hass, "mine_not_yours")))
        return
    text = await chat_start_refusal(hass, msg, msg["limits"])
    connection.send_result(msg["id"], {"ok": True} if text is None else _refused(text))


@websocket_api.websocket_command({vol.Required("type"): f"{PREFIX}/chat/stop"})
@websocket_api.async_response
async def ws_chat_stop(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """End the running chat; the line being spoken finishes. Nothing running is no error."""
    if await _may_chat(hass, connection, msg) is None:
        return
    await get_manager(hass).async_stop()
    connection.send_result(msg["id"], {"ok": True})


@callback
def async_register_mine_commands(hass: HomeAssistant) -> None:
    """Register the commands of the My assistant page. Called once per start."""
    for command in (
        ws_get,
        ws_save_persona,
        ws_save_memory,
        ws_reminders,
        ws_add_reminder,
        ws_delete_reminder,
        ws_favorites_get,
        ws_favorites_add,
        ws_favorites_update,
        ws_favorites_remove,
        ws_favorites_search,
        ws_chat_state,
        ws_chat_start,
        ws_chat_stop,
    ):
        websocket_api.async_register_command(hass, command)
