# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the settings page may ask Home Assistant for, over the websocket.

Every command is for admins only. Saving goes through the same rules as the Configure
dialog (config_flow.py), so the page cannot store what the dialog would refuse.
"""

import logging
from collections.abc import Mapping
from dataclasses import asdict
from types import MappingProxyType
from typing import Any, Literal

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigSubentry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, ServiceCall, State, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.translation import async_get_translations

from . import stage
from .announce import writer_agent
from .avatar import CONF_AVATAR_ID, CONF_ENABLED, CONF_ENGINE, CONF_URL
from .avatar import SUBENTRY_TYPE as AVATAR
from .avatar_flow import validate_avatar
from .calendar_watch import calendar_rows, find_watcher
from .chat_show import (
    ChatLimits,
    ChatRefused,
    ChatState,
    can_listen,
    can_speak,
    character_of,
    get_manager,
    voice_satellites,
)
from .config_flow import (
    calendar_row_schema,
    changed_calendar_rows,
    current_settings,
    login_users,
    memory_list_choices,
    person_names,
    settings_error,
    settings_error_placeholders,
    settings_schema,
    settings_to_save,
    without_retired,
)
from .const import DOMAIN, PERSON_SECTION
from .favorites import (
    HOUSEHOLD,
    LIST_KEYS,
    MAX_NAME_LENGTH,
    PERSON_LIST,
    FavoriteError,
    ListKey,
    async_get_favorites,
    favorites_on,
    person_list_conflicts,
)
from .favorites_play import (
    PlayRefused,
    SearchUnavailable,
    async_search_candidates,
    available_players,
    default_player,
)
from .memory import (
    PERSON_LABELS,
    LineTooLongError,
    Person,
    TooManyItemsError,
    async_get_memory,
)
from .panel import (
    GUIDE_STATES,
    GUIDE_STEPS,
    STARTED,
    async_set_announcement_look,
    async_set_chat_defaults,
    async_set_chat_stage,
    async_set_guide,
    async_set_sidebar,
    async_set_speaker_overrides,
    chat_defaults,
    guide_state,
    guide_step,
    panel_state,
    speaker_overrides,
)
from .reminders import assistant_name
from .setup_guide import (
    STYLES,
    Satellite,
    WakeWord,
    fill_style,
    find_satellites,
    helper_payload,
    pipeline_copy_payload,
    pipeline_name_is_shared,
    reusable_pipeline,
    router_entity_id,
    router_pipeline,
    tracking_automation,
)
from .status import favorites_tools_on, main_agent, router_status
from .ws_helpers import MEMORY_LINES
from .ws_helpers import error_text as _error_text
from .ws_helpers import refused as _refused
from .ws_helpers import strings as _strings

_LOGGER = logging.getLogger(__name__)

PREFIX = f"{DOMAIN}/panel"
NOT_FOUND = "not_found"
LINE_PREVIEW_LENGTH = 60
# The page only lists and saves these: the calendars have a command of their own.
NOT_SETTINGS = ("calendar_rows",)
# The section holding the field each settings_error key is about. The setup guide saves
# every section at each step, so a refusal may be about a field on a later step.
ERROR_SECTIONS: dict[str, str] = {
    "same_user": "people",
    "memory_agent_invalid": "memory",
    "memory_agent_required": "memory",
    "reminder_agent_invalid": "reminders",
    "reminder_calendar_shared": "reminders",
    "reminder_calendar_twice": "reminders",
    "reminder_calendar_no_user": "reminders",
    "refusal_agent_invalid": "strangers",
    "chat_character_too_long": "assistant",
}


def _settings(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    settings = current_settings(hass, entry)
    return {key: value for key, value in settings.items() if key not in NOT_SETTINGS}


async def _router(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Everything the page shows about one router."""
    settings = _settings(hass, entry)
    memory = await async_get_memory(hass)
    watcher = find_watcher(hass, entry.entry_id)
    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "assistant": assistant_name(
            entry.title, (settings.get("reminders") or {}).get("assistant")
        ),
        # A router whose settings were never saved shows a proposal, and has no calendars yet.
        "saved": bool(entry.options),
        # Where the setup guide stands: a router whose settings were never saved opens in it.
        "guide": guide_state(hass, entry.entry_id) or ("none" if entry.options else "new"),
        # The step to reopen a started guide at; None for every other state.
        "guide_step": guide_step(hass, entry.entry_id),
        "settings": settings,
        "calendars": [dict(row) for row in calendar_rows(entry.options)],
        "people": person_names(settings),
        "status": router_status(hass, entry, settings),
        "memory": [
            {"key": key, "label": label, "items": memory.items(key)}
            for key, label in memory_list_choices(memory, entry.entry_id, settings)
        ],
        "last_announced": watcher.last_announced if watcher else None,
        "avatars": _avatars(hass, entry),
    }


def _avatars(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    """The assistant's avatars; entity_id is None until the reload that adds it is done."""
    registry = er.async_get(hass)
    return [
        {
            "id": sub_id,
            "name": sub.title,
            "engine": sub.data.get(CONF_ENGINE),
            "url": sub.data.get(CONF_URL),
            "enabled": bool(sub.data.get(CONF_ENABLED)),
            "avatar_id": str(sub.data.get(CONF_AVATAR_ID) or ""),
            "entity_id": registry.async_get_entity_id("tts", DOMAIN, sub_id),
        }
        for sub_id, sub in entry.subentries.items()
        if sub.subentry_type == AVATAR
    ]


async def _avatar_error_text(hass: HomeAssistant, key: str) -> str:
    """An error of the Add avatar dialog, so the page says the same."""
    translations = await async_get_translations(
        hass, hass.config.language, "config_subentries", [DOMAIN]
    )
    return translations.get(
        f"component.{DOMAIN}.config_subentries.{AVATAR}.error.{key}", key
    )


def _entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    return entry if entry is not None and entry.domain == DOMAIN else None


def _without_empty(values: Any) -> Any:
    """Settings as the dialog would hand them over: a cleared field is simply not there."""
    if not isinstance(values, Mapping):
        return values
    return {
        key: _without_empty(value)
        for key, value in values.items()
        if value is not None and value != ""
    }


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{PREFIX}/get"})
@websocket_api.async_response
async def ws_get(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Everything the page needs to draw itself."""
    entries = hass.config_entries.async_entries(DOMAIN)
    connection.send_result(
        msg["id"],
        {
            "sidebar": panel_state(hass).sidebar,
            "announcement_look": panel_state(hass).announcement_look,
            "strings": await _strings(hass),
            "users": [
                {"id": user_id, "name": name} for user_id, name in await login_users(hass)
            ],
            "routers": [await _router(hass, entry) for entry in entries],
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{PREFIX}/set_sidebar", vol.Required("show"): bool}
)
@websocket_api.async_response
async def ws_set_sidebar(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    connection.send_result(msg["id"], {"sidebar": await async_set_sidebar(hass, msg["show"])})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{PREFIX}/set_announcement_look", vol.Required("on"): bool}
)
@websocket_api.async_response
async def ws_set_announcement_look(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    on = await async_set_announcement_look(hass, msg["on"])
    connection.send_result(msg["id"], {"announcement_look": on})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/voices",
        vol.Required("engine"): vol.All(str, vol.Match(r"^tts\.[a-z0-9_]{1,200}$")),
    }
)
@callback
def ws_voices(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The voices of a text-to-speech engine, by name; none if it does not list them."""
    connection.send_result(msg["id"], {"voices": _voices(hass, msg["engine"])})


def _voices(hass: HomeAssistant, engine_id: str) -> list[dict[str, str]]:
    from homeassistant.components import tts  # noqa: PLC0415 - only needed for this list

    engine = tts.get_engine_instance(hass, engine_id)
    if engine is None:
        return []
    try:
        # An engine without a default language is asked all the same, as before types.
        language: Any = engine.default_language
        voices = engine.async_get_supported_voices(language) or []
    except Exception:  # noqa: BLE001 - only a nicer field: the page falls back to free text
        _LOGGER.warning("Could not list the voices of %s", engine_id, exc_info=True)
        return []
    listed = [{"id": str(voice.voice_id), "name": str(voice.name)} for voice in voices]
    return sorted(listed, key=lambda voice: voice["name"].casefold())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/save_settings",
        vol.Required("entry_id"): str,
        vol.Required("settings"): dict,
    }
)
@websocket_api.async_response
async def ws_save_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Save the settings of one router, as the Settings form of the dialog does."""
    entry = _entry(hass, msg["entry_id"])
    if entry is None:
        connection.send_error(msg["id"], NOT_FOUND, "That assistant does not exist.")
        return
    # A page opened before an update may still send a setting that no longer exists.
    typed = without_retired(_without_empty(msg["settings"]))
    try:
        schema = settings_schema(typed, await login_users(hass))
        # A part the page sent nothing for gets the form's own defaults.
        whole = {**{str(part): {} for part in schema.schema}, **typed}
        settings = schema(whole)
    except (vol.Invalid, TypeError, AttributeError) as err:
        connection.send_result(msg["id"], _refused(f"These settings cannot be saved: {err}"))
        return
    error = settings_error(hass, entry, settings)
    if error is not None:
        placeholders = settings_error_placeholders(hass, entry, settings)
        refused = _refused(await _error_text(hass, error, **placeholders))
        if error in ERROR_SECTIONS:
            refused["section"] = ERROR_SECTIONS[error]
        connection.send_result(msg["id"], refused)
        return
    hass.config_entries.async_update_entry(entry, options=settings_to_save(entry, settings))
    connection.send_result(msg["id"], {"ok": True, "router": await _router(hass, entry)})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/save_calendar",
        vol.Required("entry_id"): str,
        vol.Optional("original"): vol.Any(str, None),
        vol.Required("row"): dict,
        vol.Optional("remove", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_save_calendar(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Add, change or remove one calendar, as the Calendars screen of the dialog does."""
    entry = _entry(hass, msg["entry_id"])
    if entry is None:
        connection.send_error(msg["id"], NOT_FOUND, "That assistant does not exist.")
        return
    if not entry.options:
        # The calendars are saved next to the settings, which do not exist yet.
        connection.send_result(msg["id"], _refused(await _error_text(hass, "settings_first")))
        return
    original = msg.get("original")
    typed = {**_without_empty(msg["row"]), "remove": msg["remove"]}
    try:
        schema = calendar_row_schema(typed, person_names(entry.options), editing=True)
        values = schema(typed)
    except (vol.Invalid, TypeError, AttributeError) as err:
        connection.send_result(msg["id"], _refused(f"This calendar cannot be saved: {err}"))
        return
    rows, errors = changed_calendar_rows(entry.options, original, values)
    if errors:
        texts = {field: await _error_text(hass, key) for field, key in errors.items()}
        connection.send_result(msg["id"], {"ok": False, "errors": texts})
        return
    hass.config_entries.async_update_entry(entry, options={**entry.options, "calendar_rows": rows})
    connection.send_result(msg["id"], {"ok": True, "router": await _router(hass, entry)})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/save_memory",
        vol.Required("entry_id"): str,
        vol.Required("key"): str,
        vol.Required("shown"): MEMORY_LINES,
        vol.Required("lines"): MEMORY_LINES,
    }
)
@websocket_api.async_response
async def ws_save_memory(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Save one edited list of facts, as the Memory editor of the dialog does.

    `shown` are the ids of the facts the page had on screen: a fact saved since then is
    left alone.
    """
    entry = _entry(hass, msg["entry_id"])
    memory = await async_get_memory(hass)
    key = msg["key"]
    allowed = (
        {found for found, _ in memory_list_choices(memory, entry.entry_id, entry.options)}
        if entry is not None
        else set()
    )
    if entry is None or key not in allowed:
        connection.send_error(msg["id"], NOT_FOUND, "That list does not belong to this assistant.")
        return
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
    connection.send_result(msg["id"], {"ok": True, "router": await _router(hass, entry)})


# The setup guide. Each command does one thing the guide's page asks for, and refuses
# whatever is not clearly this router's own: nothing else in Home Assistant is touched.


def _guide_entry(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> ConfigEntry | None:
    entry = _entry(hass, msg["entry_id"])
    if entry is None:
        connection.send_error(msg["id"], NOT_FOUND, "That assistant does not exist.")
    return entry


def _wake_word(satellites: list[Satellite], select: str) -> WakeWord | None:
    return next(
        (word for item in satellites for word in item["wake_words"] if word["select"] == select),
        None,
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{PREFIX}/guide/satellites", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def ws_guide_satellites(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Every satellite with its helper, speaker sensors and wake words."""
    entry = _guide_entry(hass, connection, msg)
    if entry is None:
        return
    satellites = find_satellites(hass, entry)
    connection.send_result(
        msg["id"],
        {
            "ok": True,
            "satellites": satellites,
            "helper_payloads": {
                item["device_id"]: helper_payload(item["device_id"]) for item in satellites
            },
        },
    )


def _tracking_refusal(satellite: Satellite | None, names: Mapping[str, str]) -> str | None:
    """The error key for why no tracking automation may be built, None when one may."""
    if satellite is None:
        return "guide_no_satellite"
    if satellite["tracked"]:
        return "guide_tracked"
    if satellite["speaker_sensor"] is None or satellite["score_sensor"] is None:
        return "guide_no_sensors"
    owner, partner = (names.get(role, "").strip().lower() for role in ("owner", "partner"))
    if not owner or not partner:
        return "guide_names_missing"
    if owner == partner:
        return "guide_names_same"
    return None


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/guide/tracking_automation",
        vol.Required("entry_id"): str,
        vol.Required("device_id"): str,
        vol.Required("names"): {str: str},
    }
)
@websocket_api.async_response
async def ws_guide_tracking_automation(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The automation that keeps one satellite's speaker helper up to date.

    Only built here, never created: the page posts it, and only for a satellite that no
    automation tracks yet.
    """
    entry = _guide_entry(hass, connection, msg)
    if entry is None:
        return
    satellite = next(
        (item for item in find_satellites(hass, entry) if item["device_id"] == msg["device_id"]),
        None,
    )
    refusal = _tracking_refusal(satellite, msg["names"])
    if refusal is not None or satellite is None:
        text = await _error_text(hass, refusal or "guide_no_satellite")
        connection.send_result(msg["id"], _refused(text))
        return
    automation_id, config = tracking_automation(satellite, msg["names"])
    connection.send_result(msg["id"], {"ok": True, "id": automation_id, "config": config})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/guide/pipeline_copy",
        vol.Required("entry_id"): str,
        vol.Required("select"): str,
    }
)
@websocket_api.async_response
async def ws_guide_pipeline_copy(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The pipeline to create for this router from the one a wake word uses now.

    Answers the id of a pipeline of this router that carries the router's title when there
    is one, else the fields for ``assist_pipeline/pipeline/create``, which the page sends.
    """
    entry = _guide_entry(hass, connection, msg)
    if entry is None:
        return
    router = router_entity_id(hass, entry)
    word = _wake_word(find_satellites(hass, entry), msg["select"])
    if router is None:
        refusal = "guide_router_missing"
    elif word is None:
        refusal = "guide_not_wake_word"
    elif word["pipeline_id"] is None:
        refusal = "guide_no_pipeline"
    else:
        reuse = reusable_pipeline(hass, entry.title, router)
        if reuse is not None:
            connection.send_result(msg["id"], {"ok": True, "reuse": reuse})
            return
        payload = pipeline_copy_payload(hass, word["pipeline_id"], router, entry.title)
        connection.send_result(msg["id"], {"ok": True, "payload": payload})
        return
    connection.send_result(msg["id"], _refused(await _error_text(hass, refusal)))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/guide/set_wake_word",
        vol.Required("entry_id"): str,
        vol.Required("select"): str,
        vol.Required("pipeline_id"): str,
    }
)
@websocket_api.async_response
async def ws_guide_set_wake_word(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Point one wake word of a satellite at a pipeline of this router."""
    entry = _guide_entry(hass, connection, msg)
    if entry is None:
        return
    if _wake_word(find_satellites(hass, entry), msg["select"]) is None:
        text = await _error_text(hass, "guide_not_wake_word")
        connection.send_result(msg["id"], _refused(text))
        return
    router = router_entity_id(hass, entry)
    pipeline = router_pipeline(hass, msg["pipeline_id"], router) if router else None
    if pipeline is None:
        text = await _error_text(hass, "guide_pipeline_not_router")
        connection.send_result(msg["id"], _refused(text))
        return
    if pipeline_name_is_shared(hass, pipeline.name):
        # The select finds its pipeline by name and takes the first with that name.
        text = await _error_text(hass, "guide_pipeline_name_shared", name=pipeline.name)
        connection.send_result(msg["id"], _refused(text))
        return
    try:
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": msg["select"], "option": pipeline.name},
            blocking=True,
        )
    except (HomeAssistantError, vol.Invalid) as err:
        _LOGGER.error("Could not set %s to pipeline %s: %s", msg["select"], pipeline.id, err)
        text = await _error_text(hass, "guide_failed", reason=str(err))
        connection.send_result(msg["id"], _refused(text))
        return
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/guide/local_off",
        vol.Required("entry_id"): str,
        vol.Required("pipeline_id"): str,
    }
)
@websocket_api.async_response
async def ws_guide_local_off(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Switch off Home Assistant's own commands on a pipeline of this router.

    Answers the names of the satellites with a wake word on that pipeline, because the
    change applies to all of them.
    """
    entry = _guide_entry(hass, connection, msg)
    if entry is None:
        return
    router = router_entity_id(hass, entry)
    pipeline = router_pipeline(hass, msg["pipeline_id"], router) if router else None
    if pipeline is None:
        text = await _error_text(hass, "guide_pipeline_not_router")
        connection.send_result(msg["id"], _refused(text))
        return
    # Imported here, not at the top: Home Assistant loads Assist only when it is set up, and
    # a pipeline was found, so it is.
    from homeassistant.components.assist_pipeline import pipeline as assist_pipeline  # noqa: PLC0415

    try:
        await assist_pipeline.async_update_pipeline(hass, pipeline, prefer_local_intents=False)
    except (HomeAssistantError, ValueError) as err:
        _LOGGER.error("Could not switch off local commands on pipeline %s: %s", pipeline.id, err)
        text = await _error_text(hass, "guide_failed", reason=str(err))
        connection.send_result(msg["id"], _refused(text))
        return
    names = [
        item["name"]
        for item in find_satellites(hass, entry)
        if any(word["pipeline_id"] == pipeline.id for word in item["wake_words"])
    ]
    connection.send_result(msg["id"], {"ok": True, "satellites": names})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/guide/state",
        vol.Required("entry_id"): str,
        vol.Required("state"): vol.In(GUIDE_STATES),
        vol.Optional("step"): vol.All(int, vol.Range(min=1, max=GUIDE_STEPS)),
    }
)
@websocket_api.async_response
async def ws_guide_state(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Remember where the guide of one router stands: started at a step, finished or skipped.

    A started guide is kept with its step, so closing the page does not lose the place.
    """
    entry = _guide_entry(hass, connection, msg)
    if entry is None:
        return
    if msg["state"] == STARTED and "step" not in msg:
        connection.send_error(
            msg["id"], websocket_api.ERR_INVALID_FORMAT, "A started guide needs its step."
        )
        return
    await async_set_guide(hass, entry.entry_id, msg["state"], msg.get("step"))
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/guide/style",
        vol.Required("entry_id"): str,
        vol.Required("style"): vol.In(STYLES),
        vol.Required("owner"): str,
        vol.Required("partner"): str,
    }
)
@websocket_api.async_response
async def ws_guide_style(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The personas and refusal texts of a ready-made style. Nothing is saved here."""
    if _guide_entry(hass, connection, msg) is None:
        return
    texts = fill_style(await _strings(hass), msg["style"], msg["owner"], msg["partner"])
    connection.send_result(msg["id"], {"ok": True, "texts": texts})


# The chat show.

CHAT_LIMITS_INVALID = "chat_limits_invalid"
CHAT_LIMIT_FIELDS = ("max_lines", "words", "pause", "minutes", "listen")
SATELLITE_DOMAIN = "assist_satellite"
MEDIA_PLAYER_DOMAIN = "media_player"


def _chat_limits(hass: HomeAssistant, sent: Mapping[str, Any]) -> ChatLimits:
    """The limits sent over the saved defaults, clamped. Raises ValueError for junk."""
    return ChatLimits.from_dict({**asdict(chat_defaults(hass)), **sent})


async def chat_start_refusal(
    hass: HomeAssistant, msg: Mapping[str, Any], sent: Mapping[str, Any]
) -> str | None:
    """Start the chat. Returns the refusal in plain words, or None when it started."""
    try:
        limits = _chat_limits(hass, sent)
    except ValueError as err:
        return await _error_text(hass, CHAT_LIMITS_INVALID, reason=str(err))
    try:
        await get_manager(hass).async_start(
            msg["a"], msg["b"], msg["speaker"], msg.get("topic", ""), limits
        )
    except ChatRefused as refused:
        return await _error_text(hass, refused.key)
    return None


def shown_chat(hass: HomeAssistant) -> ChatState:
    """The chat as a page is told of it.

    A chat on "This device" is named without the page showing it: that page's id is what
    lets a card watch the chat (stage.py), so no other page is given it.
    """
    chat = get_manager(hass).state()
    if stage.viewer_of(chat["speaker"]):
        chat["speaker"] = stage.page_speaker("")
    return chat


def routers_for_chat(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Each loaded router, and whether it can write chat lines and has a character."""
    return [
        {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "has_writer": writer_agent(hass, entry, quiet=True) is not None,
            "has_character": bool(character_of(entry)),
        }
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


# Integrations whose media players take a play_media call but cannot play a sound file:
# Sony's own TV integration starts apps and channels only.
_NO_SOUND_FILES = frozenset({"braviatv"})


def _plays_chat_lines(hass: HomeAssistant, state: State, satellite_devices: set[str]) -> bool:
    """Whether a chat can be offered on this speaker.

    A satellite always. A media player unless it is away, cannot play a sound file (a TV's
    own integration, a Music Assistant dashboard display), or belongs to a satellite that
    is offered itself: the chat speaks through the satellite, which shows the avatars.
    """
    if state.domain != MEDIA_PLAYER_DOMAIN:
        return True
    if state.state == STATE_UNAVAILABLE:
        return False
    entry = er.async_get(hass).async_get(state.entity_id)
    if entry is None:
        return True
    device_class = (
        state.attributes.get("device_class") or entry.device_class or entry.original_device_class
    )
    if entry.platform in _NO_SOUND_FILES:
        return False
    if entry.platform == "music_assistant" and device_class == "tv":
        return False
    return entry.device_id is None or entry.device_id not in satellite_devices


def chat_speakers(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every satellite that can announce and media player a chat can play on, by name.

    Two speakers with one name get their entity id behind it, so they can be told apart.
    """
    voice = voice_satellites(hass)
    registry = er.async_get(hass)
    able = [
        state
        for domain in (SATELLITE_DOMAIN, MEDIA_PLAYER_DOMAIN)
        for state in hass.states.async_all(domain)
        if can_speak(hass, state.entity_id)
    ]
    satellite_devices = {
        entry.device_id
        for state in able
        if state.domain == SATELLITE_DOMAIN
        and (entry := registry.async_get(state.entity_id)) is not None
        and entry.device_id
    }
    offered = [state for state in able if _plays_chat_lines(hass, state, satellite_devices)]
    names = [str(state.attributes.get("friendly_name") or state.entity_id) for state in offered]
    folded = [name.casefold() for name in names]
    shared = {name for name in folded if folded.count(name) > 1}
    speakers = [
        {
            "entity_id": state.entity_id,
            "name": f"{name} ({state.entity_id})" if name.casefold() in shared else name,
            "domain": state.domain,
            # A media player cannot hear anyone, so nobody can join in by voice there.
            "voice": state.entity_id in voice,
            # Only a satellite that can start a conversation a router hears can listen
            # after a round; the chat refuses to listen anywhere else as well.
            "listen": can_listen(hass, state.entity_id),
            # A Google Cast device can show the avatars (stage.py).
            "cast": stage.is_cast_player(hass, state.entity_id),
        }
        for state, name in zip(offered, names, strict=True)
    ]
    return sorted(speakers, key=lambda speaker: (speaker["name"].casefold(), speaker["entity_id"]))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/chat/start",
        vol.Required("a"): str,
        vol.Required("b"): str,
        vol.Required("speaker"): str,
        vol.Optional("topic", default=""): str,
        vol.Optional("limits", default=dict): dict,
    }
)
@websocket_api.async_response
async def ws_chat_start(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Start a chat between two routers on a speaker, or say in plain words why not."""
    text = await chat_start_refusal(hass, msg, msg["limits"])
    connection.send_result(msg["id"], {"ok": True} if text is None else _refused(text))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{PREFIX}/chat/stop"})
@websocket_api.async_response
async def ws_chat_stop(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """End the running chat; the line being spoken finishes. Nothing running is no error."""
    await get_manager(hass).async_stop()
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{PREFIX}/chat/state"})
@websocket_api.async_response
async def ws_chat_state(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The chat, the saved defaults, and what the card may choose from."""
    connection.send_result(
        msg["id"],
        {
            "chat": shown_chat(hass),
            "defaults": asdict(chat_defaults(hass)),
            "routers": routers_for_chat(hass),
            "speakers": chat_speakers(hass),
            "stage": _stage_state(hass),
        },
    )


def _stage_state(hass: HomeAssistant) -> dict[str, Any]:
    """The chat stage's settings, whether it is set up, and what setting it up creates."""
    settings = stage.settings(hass)
    return {
        "enabled": settings.enabled,
        "captions": settings.captions,
        "problem": stage.setup_problem(hass),
        "dashboard": stage.DASHBOARD,
        "view": stage.VIEW,
        "card": stage.CARD,
        "resource": panel_state(hass).stage_url,
    }


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/chat/save_stage",
        vol.Required("enabled"): bool,
        vol.Required("captions"): bool,
    }
)
@websocket_api.async_response
async def ws_chat_save_stage(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Save whether chats on cast devices show the avatars, and with captions."""
    await async_set_chat_stage(
        hass, stage.StageSettings(enabled=msg["enabled"], captions=msg["captions"])
    )
    connection.send_result(msg["id"], {"ok": True, "stage": _stage_state(hass)})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{PREFIX}/chat/save_defaults", vol.Required("limits"): dict}
)
@websocket_api.async_response
async def ws_chat_save_defaults(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Save the limits a chat starts with, clamped to their ranges."""
    try:
        limits = _chat_limits(hass, msg["limits"])
    except ValueError as err:
        text = await _error_text(hass, CHAT_LIMITS_INVALID, reason=str(err))
        connection.send_result(msg["id"], _refused(text))
        return
    await async_set_chat_defaults(hass, limits)
    connection.send_result(msg["id"], {"ok": True, "defaults": asdict(limits)})


_START_CHAT_FIELDS: dict[Any, Any] = {
    vol.Required("a"): cv.string,
    vol.Required("b"): cv.string,
    vol.Required("speaker"): cv.entity_id,
    vol.Optional("topic", default=""): cv.string,
}
# Any number or text: ChatLimits refuses what is no number, in plain words.
_START_CHAT_FIELDS.update(
    {vol.Optional(field): vol.Any(int, float, str, bool) for field in CHAT_LIMIT_FIELDS}
)
START_CHAT_SCHEMA = vol.Schema(_START_CHAT_FIELDS)


_AVATAR_FORM = vol.Schema(
    {
        vol.Optional("name", default=""): str,
        vol.Required("engine"): str,
        vol.Required("url"): str,
        vol.Optional("enabled", default=True): bool,
        vol.Optional("avatar_id", default=""): str,
    }
)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/save_avatar",
        vol.Required("entry_id"): str,
        vol.Optional("subentry_id"): vol.Any(str, None),
        vol.Optional("avatar"): dict,
        vol.Optional("remove", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_save_avatar(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Add, change or remove one avatar of an assistant, as the Add avatar dialog does.

    The entry's update listener reloads the assistant when an avatar is added, removed or
    renamed, which adds or removes its entity.
    """
    entry = _entry(hass, msg["entry_id"])
    if entry is None:
        connection.send_error(msg["id"], NOT_FOUND, "That assistant does not exist.")
        return
    subentry_id = msg.get("subentry_id")
    subentry = entry.subentries.get(subentry_id) if subentry_id else None
    if subentry_id and (subentry is None or subentry.subentry_type != AVATAR):
        connection.send_result(msg["id"], _refused("That avatar does not exist any more."))
        return
    if msg["remove"]:
        if subentry is not None:
            hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
        connection.send_result(msg["id"], {"ok": True, "router": await _router(hass, entry)})
        return
    try:
        values = _AVATAR_FORM(msg.get("avatar") or {})
    except vol.Invalid as err:
        connection.send_result(msg["id"], _refused(f"This avatar cannot be saved: {err}"))
        return
    title, data, errors = validate_avatar(hass, values, entry.title)
    if errors:
        texts = {field: await _avatar_error_text(hass, key) for field, key in errors.items()}
        connection.send_result(msg["id"], {"ok": False, "errors": texts})
        return
    if subentry is None:
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(data), subentry_type=AVATAR, title=title, unique_id=None
            ),
        )
    else:
        hass.config_entries.async_update_subentry(entry, subentry, title=title, data=data)
    connection.send_result(msg["id"], {"ok": True, "router": await _router(hass, entry)})


@callback
def async_register_chat_services(hass: HomeAssistant) -> None:
    """The start_chat and stop_chat actions. Admin users only; automations may call them."""

    async def start_chat(call: ServiceCall) -> None:
        sent = {key: call.data[key] for key in CHAT_LIMIT_FIELDS if key in call.data}
        text = await chat_start_refusal(hass, call.data, sent)
        if text is not None:
            raise ServiceValidationError(text)

    async def stop_chat(call: ServiceCall) -> None:
        await get_manager(hass).async_stop()

    async_register_admin_service(hass, DOMAIN, "start_chat", start_chat, START_CHAT_SCHEMA)
    async_register_admin_service(hass, DOMAIN, "stop_chat", stop_chat)


# Favorites. The shared helpers serve My assistant too, which decides the list itself.

FavoritesAction = Literal["add", "update", "remove"]
_PERSON_SECTIONS = PERSON_SECTION


async def favorites_change(
    hass: HomeAssistant, key: ListKey, action: FavoritesAction, msg: Mapping[str, Any]
) -> dict[str, Any]:
    """Add, change or remove one favorite of a list; a refusal comes back in plain words.

    `msg` holds the `data` to add or update with and the `favorite_id` to update or remove.
    The answer has the whole list, and the favorite as saved unless it was removed.
    """
    store = await async_get_favorites(hass)
    favorite = None
    try:
        if action == "add":
            favorite = await store.async_add(key, msg["data"])
        elif action == "update":
            favorite = await store.async_update(key, msg["favorite_id"], msg["data"])
        else:
            await store.async_remove(key, msg["favorite_id"])
    except FavoriteError as err:
        return _refused(await _error_text(hass, f"favorites_{err.key}"))
    result: dict[str, Any] = {"ok": True, "items": store.items(key)}
    if favorite is not None:
        result["favorite"] = favorite
    return result


async def favorites_search(hass: HomeAssistant, name: str) -> dict[str, Any]:
    """What Music Assistant finds for `name`, best match first, as data to save.

    Without Music Assistant, after a failed search or when nothing is found, `message` says
    so in plain words.
    """
    try:
        candidates = await async_search_candidates(hass, name)
    except SearchUnavailable:
        text = await _error_text(hass, "favorites_no_music_assistant")
        return {"candidates": [], "message": text}
    except PlayRefused:
        return {"candidates": [], "message": await _error_text(hass, "favorites_search_failed")}
    if not candidates and name.strip():
        return {"candidates": [], "message": await _error_text(hass, "favorites_nothing_found")}
    return {"candidates": candidates}


def _person_list_name(hass: HomeAssistant, person: Person) -> str:
    """The person's name when every assistant with favorites on agrees, else the label."""
    section_key, label = _PERSON_SECTIONS[person], PERSON_LABELS[person]
    names: dict[str, str] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        section = entry.options.get(section_key)
        if not favorites_on(entry) or not isinstance(section, Mapping):
            continue
        name = str(section.get("name") or "").strip()
        if name:
            names.setdefault(name.casefold(), name)
    return next(iter(names.values())) if len(names) == 1 else label


def _favorites_satellites(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every satellite on a device, with the player it uses when it has no override."""
    devices = dr.async_get(hass)
    satellites: dict[str, dict[str, Any]] = {}
    for entity in er.async_get(hass).entities.values():
        device_id = entity.device_id
        if entity.domain != SATELLITE_DOMAIN or device_id is None or device_id in satellites:
            continue
        device = devices.async_get(device_id)
        name = (device.name_by_user or device.name) if device is not None else None
        try:
            player: str | None = default_player(hass, device_id, {})
        except PlayRefused:
            player = None
        satellites[device_id] = {
            "device_id": device_id,
            "name": name or entity.entity_id,
            "player": player,
        }
    return sorted(satellites.values(), key=lambda row: (row["name"].casefold(), row["device_id"]))


def _favorites_assistants(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Each assistant's favorites switch, and whether its main agent can use the tools."""
    return [
        {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "enabled": favorites_on(entry),
            # None when it cannot be told, as for an agent without a config entry.
            "tools_on": favorites_tools_on(hass, main_agent(entry, _settings(hass, entry))),
        }
        for entry in hass.config_entries.async_entries(DOMAIN)
    ]


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{PREFIX}/favorites/get"})
@websocket_api.async_response
async def ws_favorites_get(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Every favorites list, whose they are, and what playing them depends on."""
    store = await async_get_favorites(hass)
    names = {PERSON_LIST[person]: _person_list_name(hass, person) for person in _PERSON_SECTIONS}
    conflicts = {person: person_list_conflicts(hass, person) for person in _PERSON_SECTIONS}
    players = [
        {"entity_id": entity_id, "name": name} for entity_id, name in available_players(hass)
    ]
    connection.send_result(
        msg["id"],
        {
            "lists": {key: store.items(key) for key in LIST_KEYS},
            "names": {**names, HOUSEHOLD: "Household"},
            "conflicts": conflicts,
            "overrides": speaker_overrides(hass),
            "satellites": _favorites_satellites(hass),
            "players": players,
            "assistants": _favorites_assistants(hass),
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/favorites/add",
        vol.Required("list"): vol.In(LIST_KEYS),
        vol.Required("data"): dict,
    }
)
@websocket_api.async_response
async def ws_favorites_add(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Add a favorite to any list."""
    connection.send_result(msg["id"], await favorites_change(hass, msg["list"], "add", msg))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/favorites/update",
        vol.Required("list"): vol.In(LIST_KEYS),
        vol.Required("favorite_id"): str,
        vol.Required("data"): dict,
    }
)
@websocket_api.async_response
async def ws_favorites_update(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Change the fields `data` has of one favorite in any list."""
    connection.send_result(msg["id"], await favorites_change(hass, msg["list"], "update", msg))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/favorites/remove",
        vol.Required("list"): vol.In(LIST_KEYS),
        vol.Required("favorite_id"): str,
    }
)
@websocket_api.async_response
async def ws_favorites_remove(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Remove one favorite from any list."""
    connection.send_result(msg["id"], await favorites_change(hass, msg["list"], "remove", msg))


@websocket_api.require_admin
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
    connection.send_result(msg["id"], await favorites_search(hass, msg["name"]))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/favorites/overrides/save",
        vol.Required("overrides"): {str: str},
    }
)
@websocket_api.async_response
async def ws_favorites_overrides_save(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Replace every satellite's chosen speaker; refuse them all for one that is no player."""
    try:
        await async_set_speaker_overrides(hass, msg["overrides"])
    except ValueError as err:
        _LOGGER.warning("Speaker overrides were not saved: %s", err)
        text = await _error_text(hass, "favorites_bad_override")
        connection.send_result(msg["id"], _refused(text))
        return
    connection.send_result(msg["id"], {"ok": True, "overrides": speaker_overrides(hass)})


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the page's commands. Called once per start."""
    for command in (
        ws_get,
        ws_set_sidebar,
        ws_set_announcement_look,
        ws_save_settings,
        ws_voices,
        ws_save_calendar,
        ws_save_memory,
        ws_save_avatar,
        ws_guide_satellites,
        ws_guide_tracking_automation,
        ws_guide_pipeline_copy,
        ws_guide_set_wake_word,
        ws_guide_local_off,
        ws_guide_state,
        ws_guide_style,
        ws_chat_start,
        ws_chat_stop,
        ws_chat_state,
        ws_chat_save_defaults,
        ws_chat_save_stage,
        ws_favorites_get,
        ws_favorites_add,
        ws_favorites_update,
        ws_favorites_remove,
        ws_favorites_search,
        ws_favorites_overrides_save,
    ):
        websocket_api.async_register_command(hass, command)
