# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the websocket commands of the settings page and of My assistant both need."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN
from .memory import MAX_LINE_SCHEMA_LENGTH, MAX_MAX_ITEMS

# The lines of a memory list as a page sends them: bounded before anything reads them.
MEMORY_LINES = vol.All(
    [vol.All(str, vol.Length(max=MAX_LINE_SCHEMA_LENGTH))], vol.Length(max=MAX_MAX_ITEMS)
)


async def strings(hass: HomeAssistant) -> dict[str, str]:
    """The texts of the Configure dialog, which the page shows next to the same fields."""
    prefix = f"component.{DOMAIN}.options."
    translations = await async_get_translations(hass, hass.config.language, "options", [DOMAIN])
    return {
        key.removeprefix(prefix): text
        for key, text in translations.items()
        if key.startswith(prefix)
    }


async def error_text(hass: HomeAssistant, key: str, **placeholders: str) -> str:
    text = (await strings(hass)).get(f"error.{key}") or (await strings(hass)).get(
        f"abort.{key}", key
    )
    for name, value in placeholders.items():
        text = text.replace(f"{{{name}}}", value)
    return text


def refused(text: str) -> dict[str, Any]:
    return {"ok": False, "errors": {"base": text}}
