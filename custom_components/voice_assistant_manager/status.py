# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the settings page says about a router: what works, and what needs attention."""

from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any, Literal, NotRequired, TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LLM_HASS_API
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import emotion
from .announce import chosen_writer, safe_toolless_agent, writer_agent
from .calendar_watch import calendar_rows
from .const import DOMAIN
from .favorites import favorites_on, person_list_conflicts
from .memory import PERSON_LABELS, shared_list_conflicts
from .setup_guide import find_satellites
from .speaker import resolve_speaker

Level = Literal["ok", "info", "warn", "error"]
Section = Literal[
    "assistant",
    "people",
    "strangers",
    "memory",
    "reminders",
    "calendars",
    "satellites",
    "favorites",
]
Fix = Literal["create_helper", "tracking", "local_off"]


class StatusLine(TypedDict):
    level: Level
    section: Section
    text: str
    fix: NotRequired[Fix]  # what the page offers to do about the line
    target: NotRequired[str]  # device id (helper, tracking) or pipeline id (local_off)


def _line(
    level: Level, section: Section, text: str, fix: Fix | None = None, target: str | None = None
) -> StatusLine:
    line = StatusLine(level=level, section=section, text=text)
    if fix is not None and target is not None:
        line["fix"] = fix
        line["target"] = target
    return line


def _satellite_lines(hass: HomeAssistant, entry: ConfigEntry) -> list[StatusLine]:
    """Checks of the satellites that talk to this router and of the pipelines it serves."""
    lines: list[StatusLine] = []
    local_pipelines: dict[str, str] = {}  # pipeline id -> name, in the order first seen
    on_router = 0
    for satellite in find_satellites(hass, entry):
        words = [word for word in satellite["wake_words"] if word["uses_router"]]
        if not words and not satellite["helper_exists"]:
            continue
        on_router += 1
        name = satellite["name"]
        device_id = satellite["device_id"]
        if not satellite["helper_exists"]:
            lines.append(
                _line(
                    "warn",
                    "satellites",
                    f"{name} has no helper to hold who is speaking, so the assistant "
                    "cannot tell people apart there.",
                    "create_helper",
                    device_id,
                )
            )
        # The tracking automation needs the score sensor to trigger on.
        if satellite["speaker_sensor"] and satellite["score_sensor"] and not satellite["tracked"]:
            lines.append(
                _line(
                    "warn",
                    "satellites",
                    f"{name} reports who spoke, but no automation copies that into its helper.",
                    "tracking",
                    device_id,
                )
            )
        for word in words:
            if word["prefer_local"] and word["pipeline_id"]:
                local_pipelines.setdefault(word["pipeline_id"], word["pipeline_name"])
    for pipeline_id, pipeline_name in local_pipelines.items():
        lines.append(
            _line(
                "error",
                "satellites",
                f"The pipeline {pipeline_name} tries local commands first, so unknown "
                "voices can still switch things without the assistant.",
                "local_off",
                pipeline_id,
            )
        )
    if not on_router:
        lines.append(
            _line("info", "satellites", "No satellite is set up to use this assistant yet.")
        )
    return lines


def _shared_list_lines(hass: HomeAssistant) -> list[StatusLine]:
    """A warning per person whose list shared by all assistants is not used."""
    lines: list[StatusLine] = []
    for person, label in PERSON_LABELS.items():
        titles = shared_list_conflicts(hass, person)
        if titles:
            lines.append(
                _line(
                    "warn",
                    "memory",
                    f"{label} is not the same person on every assistant "
                    f"({', '.join(titles)}), so the list shared by all assistants is not "
                    "used for them.",
                )
            )
    return lines


def _main_agent_apis(hass: HomeAssistant, agent: str) -> list[str] | None:
    """The LLM APIs switched on for the main agent; None when it has no config entry.

    Agents set up as a sub-entry (OpenAI, Anthropic, Ollama and others) keep the setting in
    the sub-entry, older ones in the config entry's options.
    """
    registry_entry = er.async_get(hass).async_get(agent)
    if registry_entry is None or registry_entry.config_entry_id is None:
        return None
    config_entry = hass.config_entries.async_get_entry(registry_entry.config_entry_id)
    if config_entry is None:
        return None
    subentry_id = registry_entry.config_subentry_id
    if subentry_id is not None and subentry_id in config_entry.subentries:
        settings: Mapping[str, Any] = config_entry.subentries[subentry_id].data
    else:
        settings = config_entry.options
    apis = settings.get(CONF_LLM_HASS_API)
    if isinstance(apis, str):
        return [apis]
    return [str(api) for api in apis] if isinstance(apis, list) else []


def main_agent(entry: ConfigEntry, settings: Mapping[str, Any]) -> Any:
    """The main agent the settings choose, else the one chosen when the router was made."""
    agents = settings.get("agents")
    chosen = agents.get("agent") if isinstance(agents, Mapping) else None
    return chosen or entry.data.get("agent")


def favorites_tools_on(hass: HomeAssistant, main: Any) -> bool | None:
    """Whether the main agent has this integration's LLM API switched on.

    None when that cannot be told: no main agent, or one without a config entry.
    """
    apis = _main_agent_apis(hass, main) if isinstance(main, str) and main else None
    return None if apis is None else DOMAIN in apis


def _favorites_lines(hass: HomeAssistant, entry: ConfigEntry, main: Any) -> list[StatusLine]:
    """Whether the main agent can use the favorites tools, and whose lists are not used."""
    if not favorites_on(entry):
        return []
    lines: list[StatusLine] = []
    if favorites_tools_on(hass, main) is False:
        lines.append(
            _line(
                "warn",
                "favorites",
                "Favorites are on, but the main agent cannot use them: switch on Voice "
                "Assistant Manager next to Assist in its settings.",
            )
        )
    for person, label in PERSON_LABELS.items():
        titles = person_list_conflicts(hass, person)
        if titles:
            lines.append(
                _line(
                    "error",
                    "favorites",
                    f"{label} is not the same person on every assistant with favorites on "
                    f"({', '.join(titles)}), so their own favorites are not used. The "
                    "household's still are.",
                )
            )
    return lines


def _typed_request_lines(settings: Mapping[str, Any]) -> list[StatusLine]:
    """A warning when a typed request from any logged-in user would count as a person."""

    def section(name: str) -> Mapping[str, Any]:
        found = settings.get(name)
        return found if isinstance(found, Mapping) else {}

    if not settings or any(section(key).get("user") for key in ("owner", "partner")):
        return []
    advanced = section("advanced")
    role, _ = resolve_speaker(None, None, (), str(advanced.get("no_helper_role") or ""))
    if role == "stranger":
        return []
    key, label = ("owner", "Person 1") if role == "owner" else ("partner", "Person 2")
    who = str(section(key).get("name") or "").strip() or label
    return [
        _line(
            "warn",
            "people",
            "No Home Assistant user is chosen for anyone: anyone logged in who types to this "
            f"assistant counts as {who}.",
        )
    ]


def _exists(hass: HomeAssistant, entity_id: str) -> bool:
    return hass.states.get(entity_id) is not None


def _section(settings: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    found = settings.get(name)
    return found if isinstance(found, Mapping) else {}


# Whether an agent may not be given text from someone unknown (announce.safe_toolless_agent).
# Agent ids come straight from the settings, hence Any.
type Unsafe = Callable[[Any], bool]


def _main_agent_lines(hass: HomeAssistant, main: Any) -> list[StatusLine]:
    if not main:
        return [_line("error", "assistant", "No main agent is chosen.")]
    if not _exists(hass, main):
        return [_line("error", "assistant", f"The main agent {main} does not exist.")]
    return [_line("ok", "assistant", f"Answers come from {main}.")]


def _people_lines(settings: Mapping[str, Any]) -> list[StatusLine]:
    return [
        _line("warn", "people", f"{label} has no name.")
        for key, label in (("owner", "Person 1"), ("partner", "Person 2"))
        if not str(_section(settings, key).get("name") or "").strip()
    ]


def _stranger_lines(strangers: Mapping[str, Any], unsafe: Unsafe) -> list[StatusLine]:
    if strangers.get("mode") == "fixed":
        return [_line("info", "strangers", "Unknown voices get a fixed line.")]
    if not strangers.get("reply_agent"):
        return [
            _line(
                "warn",
                "strangers",
                "No agent is chosen to write refusals, so unknown voices get a fixed line.",
            )
        ]
    if unsafe(strangers.get("reply_agent")):
        return [
            _line(
                "error",
                "strangers",
                "Unknown voices get fixed lines: the agent that writes refusals is missing, "
                "is another assistant or can control the home.",
            )
        ]
    return [_line("ok", "strangers", "Unknown voices are refused in character.")]


def _memory_lines(
    hass: HomeAssistant, memory: Mapping[str, Any], unsafe: Unsafe
) -> list[StatusLine]:
    lines: list[StatusLine] = []
    if not memory.get("enabled"):
        lines.append(_line("info", "memory", "Memory is off."))
    elif not memory.get("agent"):
        lines.append(_line("warn", "memory", "Memory is on, but no agent decides what to save."))
    elif unsafe(memory.get("agent")):
        lines.append(
            _line(
                "error",
                "memory",
                "Nothing new is saved: the agent that decides what to save is missing, is "
                "another assistant or can control the home. Remembered facts are still used.",
            )
        )
    else:
        lines.append(_line("ok", "memory", "Memory is on."))
    if memory.get("use_global", True):
        lines.extend(_shared_list_lines(hass))
    return lines


def _face_lines(settings: Mapping[str, Any]) -> list[StatusLine]:
    # Checked against the settings as given, which may not be saved yet.
    unknown = emotion.unknown_faces(SimpleNamespace(options=settings))
    if not unknown:
        return []
    return [
        _line(
            "warn",
            "assistant",
            f"Face for audio tags names {', '.join(unknown)}, which "
            f"{'is' if len(unknown) == 1 else 'are'} not on the Emotion tags list, so "
            f"{'that line is' if len(unknown) == 1 else 'those lines are'} ignored.",
        )
    ]


def _calendar_lines(
    hass: HomeAssistant, settings: Mapping[str, Any], rows: list[Mapping[str, Any]]
) -> list[StatusLine]:
    if not rows:
        return [_line("info", "calendars", "No calendars are watched.")]
    count = f"{len(rows)} calendar{'s' if len(rows) != 1 else ''}"
    lines = [_line("ok", "calendars", f"{count} watched.")]
    if not _section(settings, "calendars").get("script"):
        lines.append(
            _line(
                "warn",
                "calendars",
                "Calendars are watched, but no script is chosen to speak their events.",
            )
        )
    lines.extend(
        _line("warn", "calendars", f"The calendar {row['calendar']} does not exist.")
        for row in rows
        if not _exists(hass, str(row["calendar"]))
    )
    return lines


def _announcement_lines(
    hass: HomeAssistant, entry: ConfigEntry, settings: Mapping[str, Any], unsafe: Unsafe
) -> list[StatusLine]:
    """Lines about the agent that writes spoken reminders and calendar events."""
    writer = chosen_writer(settings)
    if writer and unsafe(writer):
        return [
            _line(
                "error",
                "reminders",
                "Spoken lines fall back to plain text: the announcement agent is missing, is "
                "another assistant or can control the home.",
            )
        ]
    if writer_agent(hass, entry, quiet=True) is None:
        return [
            _line(
                "warn",
                "reminders",
                "No announcement agent is chosen. Spoken lines fall back to plain text.",
            )
        ]
    return []


def router_status(
    hass: HomeAssistant, entry: ConfigEntry, settings: Mapping[str, Any]
) -> list[StatusLine]:
    """One line per thing worth knowing about a router, most serious first."""
    main = main_agent(entry, settings)

    def unsafe(agent: Any) -> bool:
        return safe_toolless_agent(hass, entry, agent, main_agent=main) is None

    reminders = bool(_section(settings, "reminders").get("enabled"))
    rows = calendar_rows(entry.options)
    lines = [
        *_main_agent_lines(hass, main),
        *_people_lines(settings),
        *_typed_request_lines(settings),
        *_stranger_lines(_section(settings, "strangers"), unsafe),
        *_memory_lines(hass, _section(settings, "memory"), unsafe),
        *_face_lines(settings),
        _line("ok", "reminders", "The main agent can set reminders.")
        if reminders
        else _line("info", "reminders", "Reminders are off."),
        *_calendar_lines(hass, settings, rows),
        *(_announcement_lines(hass, entry, settings, unsafe) if reminders or rows else []),
        *_favorites_lines(hass, entry, main),
        *_satellite_lines(hass, entry),
    ]
    order = {"error": 0, "warn": 1, "ok": 2, "info": 3}
    return sorted(lines, key=lambda line: order[line["level"]])
