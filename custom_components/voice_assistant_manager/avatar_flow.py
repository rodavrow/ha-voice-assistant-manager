# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Add avatar and Reconfigure dialogs of an assistant."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .avatar import CONF_ENABLED, CONF_ENGINE, CONF_URL, normalize_url
from .const import DOMAIN

CONF_NAME = "name"


def _is_avatar(hass: HomeAssistant, entity_id: str) -> bool:
    registry_entry = er.async_get(hass).async_get(entity_id)
    return registry_entry is not None and registry_entry.platform == DOMAIN


def _schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME): selector.TextSelector(),
            vol.Required(CONF_ENGINE): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="tts")
            ),
            vol.Required(CONF_URL): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
            ),
            vol.Required(CONF_ENABLED, default=True): selector.BooleanSelector(),
        }
    )


class AvatarSubentryFlow(ConfigSubentryFlow):
    """One avatar of an assistant: its name, engine, LiveTalking address and switch."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._async_step("user", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_step("reconfigure", user_input)

    async def _async_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        editing: ConfigSubentry | None = (
            self._get_reconfigure_subentry() if self.source == SOURCE_RECONFIGURE else None
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            url = normalize_url(str(user_input.get(CONF_URL) or ""))
            engine = str(user_input[CONF_ENGINE])
            if url is None:
                errors[CONF_URL] = "invalid_url"
            if _is_avatar(self.hass, engine):
                errors[CONF_ENGINE] = "engine_is_avatar"
            elif self.hass.states.get(engine) is None:
                errors[CONF_ENGINE] = "engine_missing"
            if not errors:
                title = str(user_input[CONF_NAME]).strip() or f"{entry.title} avatar"
                data = {
                    CONF_ENGINE: engine,
                    CONF_URL: url,
                    CONF_ENABLED: bool(user_input.get(CONF_ENABLED, True)),
                }
                if editing is not None:
                    return self.async_update_and_abort(entry, editing, title=title, data=data)
                return self.async_create_entry(title=title, data=data)

        if user_input is not None:
            suggested = user_input
        elif editing is not None:
            suggested = {CONF_NAME: editing.title, **editing.data}
        else:
            suggested = {CONF_NAME: f"{entry.title} avatar", CONF_ENABLED: True}
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(_schema(), suggested),
            errors=errors,
        )
