# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config flow and configure dialog for Voice Assistant Manager.

The forms, defaults, validation and save logic live in settings.py; the names below are
re-exported so imports from this module keep working.
"""

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.core import callback

from .avatar import SUBENTRY_TYPE as AVATAR
from .avatar_flow import AvatarSubentryFlow
from .calendar_watch import calendar_rows
from .const import DOMAIN
from .memory import (
    LineTooLongError,
    MemoryItem,
    TooManyItemsError,
    async_get_memory,
    item_line,
)
from .settings import (
    ADD_CALENDAR,
    LINE_PREVIEW_LENGTH,
    _agent,
    _choice,
    _opt,
    _text,
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

__all__ = [
    "PersonaRouterFlow",
    "PersonaRouterOptionsFlow",
    "calendar_row_schema",
    "changed_calendar_rows",
    "current_settings",
    "login_users",
    "memory_list_choices",
    "person_names",
    "settings_error",
    "settings_error_placeholders",
    "settings_schema",
    "settings_to_save",
    "without_retired",
]


class PersonaRouterFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PersonaRouterOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {AVATAR: AvatarSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title=user_input["name"], data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): str,
                    vol.Required("agent"): _agent(),
                }
            ),
        )


class PersonaRouterOptionsFlow(OptionsFlow):
    """Configure dialog: a menu leading to the settings, the calendars or the memory."""

    def __init__(self) -> None:
        self._list_key = ""
        self._list_label = ""
        # What the memory editor showed, so a save is a diff against it.
        self._shown: list[MemoryItem] = []
        # The calendar being changed, or None while a new one is added.
        self._calendar: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "calendars", "memory"]
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            error = settings_error(self.hass, self.config_entry, user_input)
            if error is None:
                return self.async_create_entry(
                    title="", data=settings_to_save(self.config_entry, user_input)
                )
            errors["base"] = error
            placeholders = settings_error_placeholders(self.hass, self.config_entry, user_input)
        current = user_input or current_settings(self.hass, self.config_entry)
        return self.async_show_form(
            step_id="settings",
            data_schema=settings_schema(current, await login_users(self.hass)),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_calendars(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the calendar to change, or add one."""
        if not self.config_entry.options:
            # The calendars are saved next to the settings, which do not exist yet.
            return self.async_abort(reason="settings_first")
        rows = calendar_rows(self.config_entry.options)
        if not rows:
            self._calendar = None
            return await self.async_step_calendar_edit()
        if user_input is not None:
            picked = user_input["row"]
            self._calendar = None if picked == ADD_CALENDAR else picked
            return await self.async_step_calendar_edit()
        names = person_names(self.config_entry.options)
        choices = [
            (
                row["calendar"],
                f"{row['calendar']} (for {names.get(str(row.get('for')), 'everyone')})",
            )
            for row in rows
        ]
        return self.async_show_form(
            step_id="calendars",
            data_schema=vol.Schema(
                {vol.Required("row"): _choice([*choices, (ADD_CALENDAR, "Add a calendar")])}
            ),
        )

    async def async_step_calendar_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add, change or remove one calendar. The other settings are left as they are."""
        options = self.config_entry.options
        rows = [dict(row) for row in calendar_rows(options)]
        errors: dict[str, str] = {}
        if user_input is not None:
            saved, errors = changed_calendar_rows(options, self._calendar, user_input)
            if not errors:
                return self.async_create_entry(
                    title="", data={**options, "calendar_rows": saved}
                )
        shown = user_input or next(
            (row for row in rows if row["calendar"] == self._calendar), {}
        )
        return self.async_show_form(
            step_id="calendar_edit",
            data_schema=calendar_row_schema(
                shown, person_names(options), editing=self._calendar is not None
            ),
            errors=errors,
        )

    async def async_step_memory(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the list to edit."""
        memory = await async_get_memory(self.hass)
        choices = memory_list_choices(
            memory, self.config_entry.entry_id, self.config_entry.options
        )
        if user_input is not None:
            self._list_key = user_input["list"]
            self._list_label = dict(choices)[self._list_key]
            self._shown = memory.items(self._list_key)
            return await self.async_step_memory_edit()
        return self.async_show_form(
            step_id="memory",
            data_schema=vol.Schema({vol.Required("list"): _choice(choices)}),
        )

    async def async_step_memory_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit one list, one fact per line. Saving never touches the entry's options."""
        errors = {}
        placeholders = {"list": self._list_label}
        text = "\n".join(item_line(item) for item in self._shown)
        if user_input is not None:
            text = str(user_input.get("facts") or "")
            memory = await async_get_memory(self.hass)
            try:
                await memory.async_apply_edit(self._list_key, self._shown, text.splitlines())
            except LineTooLongError as err:
                errors["base"] = "line_too_long"
                placeholders["line"] = f"{err.line[:LINE_PREVIEW_LENGTH]}…"
            except TooManyItemsError:
                errors["base"] = "too_many_facts"
            else:
                return await self.async_step_init()
        return self.async_show_form(
            step_id="memory_edit",
            data_schema=vol.Schema({_opt("facts", {"facts": text}): _text(True)}),
            errors=errors,
            description_placeholders=placeholders,
        )
