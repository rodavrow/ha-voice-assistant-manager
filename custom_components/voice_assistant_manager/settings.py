# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The settings screen's forms, defaults, validation and save logic.

Shared by the configure dialog (config_flow.py) and the Voice Manager page
(panel_api.py, mine_api.py), so both store the same thing and refuse the same thing.
"""

from collections.abc import Iterable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .announce import safe_toolless_agent
from .calendar_watch import calendar_rows
from .calendars import (
    DAY_BEFORE,
    DAY_SAME,
    DEFAULT_ALL_DAY_TIME,
    DEFAULT_CATCH_UP_HOURS,
    parse_time,
)
from .const import DEFAULT_MATCH_WITHIN, DEFAULT_MAX_ITEMS, DEFAULT_MIN_SCORE, DOMAIN
from .memory import (
    MemoryStore,
    Person,
    entry_key,
    global_key,
    router_agent_ids,
)

# Settings as the forms show and save them: section name to its fields. The values are
# whatever the form's selectors produce, hence Any.
type Settings = Mapping[str, Any]
type Choices = Iterable[tuple[str, str]]

MEMORY_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "auto_save": True,
    "max_items": DEFAULT_MAX_ITEMS,
    "use_global": True,
    "shared": True,
}
REMINDER_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "cancel": True,
    "owner_calendars": [],
    "partner_calendars": [],
    "household_calendars": [],
}
FAVORITES_DEFAULTS = {"enabled": False}
CALENDAR_DEFAULTS: dict[str, Any] = {
    "all_day_time": DEFAULT_ALL_DAY_TIME,
    "catch_up_hours": DEFAULT_CATCH_UP_HOURS,
}
CHAT_CHARACTER_MAX = 2000
ADD_CALENDAR = "add"
LINE_PREVIEW_LENGTH = 60

DEFAULT_NEAR_PERSONA = (
    "The voice sounded a little like someone who lives here, but you could not be sure. "
    "Do not help yet and do not carry out the request. Say in one short sentence, in your "
    "own style, that they sound familiar and ask them to say it again clearly."
)
DEFAULT_NEAR_LINES = "You sound familiar, but I am not sure it is you. Say that again?"


def _text(multiline: bool = False) -> selector.TextSelector:
    return selector.TextSelector(selector.TextSelectorConfig(multiline=multiline))


def _agent() -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="conversation"))


def _calendars() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="calendar", multiple=True)
    )


def _choice(options: Choices) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=v, label=l) for v, l in options],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _number(
    low: float, high: float, step: float, unit: str | None = None
) -> selector.NumberSelector:
    config: selector.NumberSelectorConfig = {
        "min": low,
        "max": high,
        "step": step,
        "mode": selector.NumberSelectorMode.BOX,
    }
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _opt(key: str, current: Settings, allowed: set[str] | None = None) -> vol.Optional:
    """Optional field that shows the saved value and may be cleared.

    With `allowed`, a saved value that is no longer one of the choices (a deleted
    user, say) is not suggested, so the form can still be saved.
    """
    value = current.get(key)
    if value is None or value == "" or (allowed is not None and value not in allowed):
        return vol.Optional(key)
    return vol.Optional(key, description={"suggested_value": value})


async def login_users(hass: HomeAssistant) -> list[tuple[str, str]]:
    """(id, name) of the active people who can log in, without Home Assistant's own accounts."""
    users = await hass.auth.async_get_users()
    return [(u.id, u.name or u.id) for u in users if u.is_active and not u.system_generated]


def same_user_for_both(settings: Settings) -> bool:
    owner_user = (settings.get("owner") or {}).get("user")
    partner_user = (settings.get("partner") or {}).get("user")
    return bool(owner_user) and owner_user == partner_user


def _required(key: str, current: Settings) -> vol.Required:
    """Required field that shows the saved value, if there is one."""
    value = current.get(key)
    if not value:
        return vol.Required(key)
    return vol.Required(key, description={"suggested_value": value})


def _fixed(key: str, current: Settings, fallback: object) -> vol.Optional:
    """Field that always has a value."""
    value = current.get(key)
    return vol.Optional(key, default=fallback if value is None else value)


def _script_texts(hass: HomeAssistant, script_entity_id: str | None) -> dict[str, Any]:
    """Collect the texts of an older 'who is speaking' script, if it has the expected shape."""
    found: dict[str, Any] = {}

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            variables = node.get("variables")
            if isinstance(variables, dict):
                if isinstance(variables.get("personas"), dict):
                    found["personas"] = variables["personas"]
                if isinstance(variables.get("refusals"), list):
                    found["refusals"] = variables["refusals"]
                result = variables.get("result")
                if isinstance(result, dict) and isinstance(result.get("rule"), str):
                    found["rule"] = result["rule"]
            for value in node.values():
                walk(value)

    try:
        component = hass.data.get("script")
        entity = component.get_entity(script_entity_id) if component and script_entity_id else None
        walk(getattr(entity, "raw_config", None) or {})
    except Exception:  # noqa: BLE001 - the import is a convenience only
        return {}
    return found


# Settings an older version stored that the forms no longer have. They are left out of what
# the forms see and save, so a stored one never makes a save fail.
RETIRED_SETTINGS = {"advanced": ("wait_seconds",)}  # replaced by match_within in 0.5.2


def without_retired[T](settings: T) -> T:
    """The settings without RETIRED_SETTINGS. Anything that is not a mapping is left as is."""
    if not isinstance(settings, dict):
        return settings
    cleaned: dict[str, Any] = dict(settings)
    for section_name, keys in RETIRED_SETTINGS.items():
        part = cleaned.get(section_name)
        if isinstance(part, dict):
            cleaned[section_name] = {k: v for k, v in part.items() if k not in keys}
    # T is a dict here; mypy cannot follow the isinstance guard back to T.
    return cleaned  # type: ignore[return-value]


def current_settings(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Saved settings, or a first proposal taken from the older script."""
    if entry.options:
        return without_retired(dict(entry.options))
    texts = _script_texts(hass, entry.data.get("script"))
    personas = texts.get("personas") or {}
    lines = [str(line).strip() for line in texts.get("refusals") or [] if str(line).strip()]
    return {
        "agents": {"agent": entry.data.get("agent")},
        "local": {"enabled": True, "leave_questions": True},
        "memory": dict(MEMORY_DEFAULTS),
        "reminders": dict(REMINDER_DEFAULTS),
        "favorites": dict(FAVORITES_DEFAULTS),
        "calendars": dict(CALENDAR_DEFAULTS),
        "owner": {"persona": str(personas.get("owner") or "")},
        "partner": {"persona": str(personas.get("wife") or "")},
        "strangers": {
            "mode": "ai",
            "persona": str(personas.get("stranger") or ""),
            "fixed_replies": "\n".join(lines),
        },
        "near_miss": {
            "enabled": False,
            "min_score": DEFAULT_MIN_SCORE,
            "persona": DEFAULT_NEAR_PERSONA,
            "fixed_replies": DEFAULT_NEAR_LINES,
        },
        "advanced": {
            "rule": str(texts.get("rule") or ""),
            "match_within": DEFAULT_MATCH_WITHIN,
            "follow_up_window": 60,
            # Proposed for a new assistant: nobody is assumed for a device that cannot
            # tell people apart. Settings saved without this choice still mean Person 1.
            "no_helper_role": "stranger",
            "avatar_connection": "on_wake",
            "avatar_stream": True,
            "voice_tags": False,
        },
    }


def settings_schema(current: Settings, users: Choices) -> vol.Schema:
    """Build the settings form. `users` is the (id, name) list from login_users."""
    user_ids = {user_id for user_id, _ in users}
    agents = current.get("agents") or {}
    local = current.get("local") or {}
    memory = current.get("memory") or {}
    reminders = current.get("reminders") or {}
    favorites = current.get("favorites") or {}
    chat = current.get("chat") or {}
    calendars = current.get("calendars") or {}
    owner = current.get("owner") or {}
    partner = current.get("partner") or {}
    strangers = current.get("strangers") or {}
    near = current.get("near_miss") or {}
    advanced = current.get("advanced") or {}
    return vol.Schema(
        {
            vol.Required("agents"): section(
                vol.Schema({_opt("agent", agents): _agent()}),
                {"collapsed": False},
            ),
            vol.Required("local"): section(
                vol.Schema(
                    {
                        _fixed("enabled", local, True): selector.BooleanSelector(),
                        _fixed("leave_questions", local, True): selector.BooleanSelector(),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required("memory"): section(
                vol.Schema(
                    {
                        _fixed("enabled", memory, False): selector.BooleanSelector(),
                        _opt("agent", memory): _agent(),
                        _fixed("auto_save", memory, True): selector.BooleanSelector(),
                        _fixed("max_items", memory, DEFAULT_MAX_ITEMS): _number(10, 200, 1),
                        _fixed("use_global", memory, True): selector.BooleanSelector(),
                        _fixed("shared", memory, True): selector.BooleanSelector(),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required("reminders"): section(
                vol.Schema(
                    {
                        _fixed("enabled", reminders, False): selector.BooleanSelector(),
                        _fixed("cancel", reminders, True): selector.BooleanSelector(),
                        _opt("assistant", reminders): _text(),
                        _opt("agent", reminders): _agent(),
                        _opt("persona", reminders): _text(True),
                        _opt("failure_line", reminders): _text(),
                        _fixed("owner_calendars", reminders, []): _calendars(),
                        _fixed("partner_calendars", reminders, []): _calendars(),
                        _fixed("household_calendars", reminders, []): _calendars(),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required("calendars"): section(
                vol.Schema(
                    {
                        _opt("script", calendars): selector.EntitySelector(
                            selector.EntitySelectorConfig(domain="script")
                        ),
                        _fixed(
                            "all_day_time", calendars, DEFAULT_ALL_DAY_TIME
                        ): selector.TimeSelector(),
                        _fixed(
                            "catch_up_hours", calendars, DEFAULT_CATCH_UP_HOURS
                        ): _number(0, 48, 1, "h"),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required("chat"): section(
                vol.Schema(
                    {
                        _opt("character", chat): _text(True),
                        _opt("tts_engine", chat): selector.EntitySelector(
                            selector.EntitySelectorConfig(domain="tts")
                        ),
                        _opt("voice", chat): _text(),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required("favorites"): section(
                vol.Schema({_fixed("enabled", favorites, False): selector.BooleanSelector()}),
                {"collapsed": True},
            ),
            vol.Required("owner"): section(
                vol.Schema(
                    {
                        _opt("name", owner): _text(),
                        _opt("user", owner, user_ids): _choice(users),
                        _opt("persona", owner): _text(True),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required("partner"): section(
                vol.Schema(
                    {
                        _opt("name", partner): _text(),
                        _opt("user", partner, user_ids): _choice(users),
                        _opt("persona", partner): _text(True),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required("strangers"): section(
                vol.Schema(
                    {
                        _fixed("mode", strangers, "ai"): _choice(
                            [
                                ("ai", "Refusal written by an agent without tools"),
                                ("fixed", "Fixed lines only"),
                            ]
                        ),
                        _opt("reply_agent", strangers): _agent(),
                        _opt("persona", strangers): _text(True),
                        _opt("fixed_replies", strangers): _text(True),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required("near_miss"): section(
                vol.Schema(
                    {
                        _fixed("enabled", near, False): selector.BooleanSelector(),
                        _fixed("min_score", near, DEFAULT_MIN_SCORE): _number(0, 1, 0.01),
                        _opt("persona", near): _text(True),
                        _opt("fixed_replies", near): _text(True),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required("advanced"): section(
                vol.Schema(
                    {
                        _opt("rule", advanced): _text(True),
                        _opt("agent_down_line", advanced): _text(),
                        _opt("emotion_tags", advanced): _text(),
                        _fixed("voice_tags", advanced, False): selector.BooleanSelector(),
                        _opt("face_for_tags", advanced): _text(True),
                        _fixed("match_within", advanced, DEFAULT_MATCH_WITHIN): _number(
                            0, 15, 0.5, "s"
                        ),
                        _fixed("follow_up_window", advanced, 60): _number(0, 300, 5, "s"),
                        _fixed("no_helper_role", advanced, "owner"): _choice(
                            [
                                ("owner", "Person 1"),
                                ("wife", "Person 2"),
                                ("stranger", "An unknown voice"),
                            ]
                        ),
                        _fixed("avatar_stream", advanced, True): selector.BooleanSelector(),
                        _fixed("avatar_connection", advanced, "on_wake"): _choice(
                            [
                                ("on_wake", "Connect when woken"),
                                ("always", "Always connected"),
                            ]
                        ),
                    }
                ),
                {"collapsed": True},
            ),
        }
    )


def memory_settings_error(settings: Settings, other_agents: set[str]) -> str | None:
    """Form error key for the Memory section, or None when it is fine.

    `other_agents` are the entity ids the memory agent may not be: the main agent
    and this router itself.
    """
    memory = settings.get("memory") or {}
    agent = memory.get("agent")
    if agent and agent in other_agents:
        return "memory_agent_invalid"
    if memory.get("enabled") and not agent:
        return "memory_agent_required"
    writer = (settings.get("reminders") or {}).get("agent")
    if writer and writer in other_agents:
        return "reminder_agent_invalid"
    return None


def person_names(settings: Settings) -> dict[str, str]:
    """What to call who a calendar is for: the two names, and everyone."""
    names = {"household": "everyone"}
    for key, fallback in (("owner", "Person 1"), ("partner", "Person 2")):
        names[key] = str((settings.get(key) or {}).get("name") or "").strip() or fallback
    return names


def calendar_row_schema(row: Settings, names: Mapping[str, str], editing: bool) -> vol.Schema:
    """The form for one calendar. `row` holds the values to show."""
    fields: dict[vol.Marker, object] = {
        _required("calendar", row): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="calendar")
        ),
        _fixed("for", row, "household"): _choice(
            [
                ("household", "Everyone"),
                ("owner", names["owner"]),
                ("partner", names["partner"]),
            ]
        ),
        _opt("all_day_time", row): _text(),
        _fixed("all_day_day", row, DAY_SAME): _choice(
            [(DAY_SAME, "On the day itself"), (DAY_BEFORE, "The day before")]
        ),
        _fixed("lead_minutes", row, 0): _number(0, 1440, 1, "min"),
        _opt("contains", row): _text(),
        _opt("hint", row): _text(True),
    }
    if editing:
        fields[vol.Optional("remove", default=False)] = selector.BooleanSelector()
    return vol.Schema(fields)


def clean_calendar_row(values: Settings) -> dict[str, Any]:
    """One calendar as it is saved: texts trimmed, empty ones left out."""
    row: dict[str, Any] = {
        "calendar": values["calendar"],
        "for": values.get("for") or "household",
        "all_day_day": values.get("all_day_day") or DAY_SAME,
        "lead_minutes": int(values.get("lead_minutes") or 0),
    }
    at = parse_time(values.get("all_day_time"))
    if at is not None:
        row["all_day_time"] = f"{at:%H:%M}"
    for key in ("contains", "hint"):
        text = str(values.get(key) or "").strip()
        if text:
            row[key] = text
    return row


def calendar_row_errors(values: Settings, other_calendars: set[str]) -> dict[str, str]:
    """Form errors for one calendar. `other_calendars` are the router's other rows."""
    errors = {}
    if values.get("calendar") in other_calendars:
        errors["calendar"] = "calendar_duplicate"
    typed = str(values.get("all_day_time") or "").strip()
    if typed and parse_time(typed) is None:
        errors["all_day_time"] = "calendar_time_invalid"
    return errors


def other_agents(hass: HomeAssistant, entry: ConfigEntry, settings: Settings) -> set[str]:
    """Agents the memory agent may not be: the main agent and any router."""
    agents: set[str | None] = {
        (settings.get("agents") or {}).get("agent"),
        entry.data.get("agent"),
        *router_agent_ids(hass),
    }
    return {agent for agent in agents if agent is not None}


def _personal_calendars(settings: Settings) -> dict[str, tuple[str | None, list[str]]]:
    """Per person ("owner", "partner"): the Home Assistant user and personal calendars."""
    reminders = settings.get("reminders") or {}
    return {
        key: (
            (settings.get(key) or {}).get("user"),
            list(reminders.get(f"{key}_calendars") or []),
        )
        for key in ("owner", "partner")
    }


def reminder_calendar_error(
    hass: HomeAssistant, entry: ConfigEntry, settings: Settings
) -> tuple[str, dict[str, str]] | None:
    """Error key and placeholders for reminder calendars that may not be saved, or None.

    A personal calendar belongs to one Home Assistant user, across every router, and is
    never also a household calendar. Household calendars may repeat between routers.
    """
    mine = _personal_calendars(settings)
    household = list((settings.get("reminders") or {}).get("household_calendars") or [])
    for user, calendars in mine.values():
        if calendars and not user:
            return "reminder_calendar_no_user", {}
    owner_calendars, partner_calendars = mine["owner"][1], mine["partner"][1]
    for calendar in [*owner_calendars, *partner_calendars]:
        both = calendar in owner_calendars and calendar in partner_calendars
        if calendar in household or both:
            return "reminder_calendar_twice", {"calendar": calendar}
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id:
            continue
        theirs = _personal_calendars(other.options)
        their_household = (other.options.get("reminders") or {}).get("household_calendars") or []
        clash = {"router": other.title}
        for user, calendars in mine.values():
            for calendar in calendars:
                owners = {u for u, cals in theirs.values() if calendar in cals}
                if calendar in their_household or owners - {user}:
                    return "reminder_calendar_shared", {"calendar": calendar, **clash}
        for calendar in household:
            if any(calendar in cals for _, cals in theirs.values()):
                return "reminder_calendar_shared", {"calendar": calendar, **clash}
    return None


def settings_error_placeholders(
    hass: HomeAssistant, entry: ConfigEntry, settings: Settings
) -> dict[str, str]:
    """What fills the {name} places of the message for the calendar error, if it is that."""
    found = reminder_calendar_error(hass, entry, settings)
    return found[1] if found and found[0] == settings_error(hass, entry, settings) else {}


def settings_error(hass: HomeAssistant, entry: ConfigEntry, settings: Settings) -> str | None:
    """Error key for settings that may not be saved, or None when they are fine."""
    error = memory_settings_error(settings, other_agents(hass, entry, settings))
    main_agent = (settings.get("agents") or {}).get("agent")

    def unsafe(agent: str | None) -> bool:
        return safe_toolless_agent(hass, entry, agent, main_agent=main_agent) is None

    memory = settings.get("memory") or {}
    if error is None and memory.get("enabled") and unsafe(memory.get("agent")):
        error = "memory_agent_invalid"
    reminders = settings.get("reminders") or {}
    writer = reminders.get("agent")
    # Only checked while something announces, so a stale agent left behind with reminders
    # off does not block every save. writer_agent checks it again before each use.
    announces = (
        reminders.get("enabled") or calendar_rows(settings) or calendar_rows(entry.options)
    )
    if error is None and announces and writer and unsafe(writer):
        error = "reminder_agent_invalid"
    calendar_error = reminder_calendar_error(hass, entry, settings)
    if error is None and calendar_error is not None:
        error = calendar_error[0]
    strangers = settings.get("strangers") or {}
    refuser = str(strangers.get("reply_agent") or "").strip()
    # Fixed lines never ask the agent, so what it can do does not matter then. The
    # near-miss reply uses this agent too, so one rule keeps a stranger away from tools.
    if error is None and strangers.get("mode", "ai") != "fixed" and refuser and unsafe(refuser):
        error = "refusal_agent_invalid"
    character = str((settings.get("chat") or {}).get("character") or "")
    if error is None and len(character) > CHAT_CHARACTER_MAX:
        error = "chat_character_too_long"
    if same_user_for_both(settings):
        error = "same_user"
    return error


def settings_to_save(entry: ConfigEntry, settings: Settings) -> dict[str, Any]:
    """The options to store for saved settings. The calendars are edited apart, and kept."""
    rows = entry.options.get("calendar_rows")
    return {**settings, **({"calendar_rows": rows} if rows else {})}


def changed_calendar_rows(
    options: Settings, original: str | None, values: Settings
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The router's calendars after adding, changing or removing one, and form errors.

    `original` is the calendar being changed, or None when one is added. With errors,
    the rows come back unchanged.
    """
    rows = [dict(row) for row in calendar_rows(options)]
    others = {row["calendar"] for row in rows if row["calendar"] != original}
    removing = bool(values.get("remove"))
    # What is removed need not be right.
    errors = {} if removing else calendar_row_errors(values, others)
    if errors:
        return rows, errors
    saved = []
    for row in rows:
        if row["calendar"] != original:
            saved.append(row)
        elif not removing:
            saved.append(clean_calendar_row(values))
    if original is None:
        saved.append(clean_calendar_row(values))
    return saved, {}


def memory_list_choices(
    memory: MemoryStore, entry_id: str, settings: Settings
) -> list[tuple[str, str]]:
    """(list key, label) of the lists one router entry can see, with their item counts."""
    choices = []
    people: tuple[tuple[Person, str, str], ...] = (
        ("owner", "owner", "Person 1"),
        ("wife", "partner", "Person 2"),
    )
    for person, settings_key, fallback in people:
        name = str((settings.get(settings_key) or {}).get("name") or "").strip() or fallback
        for key, scope in (
            (entry_key(entry_id, person), "this assistant"),
            (global_key(person), "all assistants"),
        ):
            choices.append((key, f"{name}: {scope} ({len(memory.items(key))})"))
    return choices
