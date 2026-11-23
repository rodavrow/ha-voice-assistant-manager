# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is asking, for the main agent's favorites tools.

The router notes the assistant, the person and the satellite of a request under its context
id just before it calls the main agent, and forgets them when that call ends. A tool reads
its own context id; no record means it cannot tell who is asking and refuses. The time
limit is a safety net for a record the router could not forget.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .memory import Person

ASKER_LIMIT: Final = 60  # seconds
_DATA_KEY: Final = "favorites_askers"
# The clock the limit is measured with; tests replace it.
_clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class Asker:
    """The assistant (config entry), the person and the satellite device of a request."""

    entry_id: str
    role: Person
    device_id: str | None


def _askers(hass: HomeAssistant) -> dict[str, tuple[float, Asker]]:
    askers: dict[str, tuple[float, Asker]] = hass.data.setdefault(DOMAIN, {}).setdefault(
        _DATA_KEY, {}
    )
    return askers


def remember(hass: HomeAssistant, context_id: str, asker: Asker) -> None:
    """Note who is asking under this context id, replacing what was noted for it before."""
    askers = _askers(hass)
    now = _clock()
    for old_id in [key for key, (until, _) in askers.items() if until <= now]:
        del askers[old_id]
    askers[context_id] = (now + ASKER_LIMIT, asker)


def forget(hass: HomeAssistant, context_id: str) -> None:
    """Forget who asked under this context id. Nothing happens when nothing was noted."""
    _askers(hass).pop(context_id, None)


def asker(hass: HomeAssistant, context_id: str | None) -> Asker | None:
    """Who is asking under this context id, or None when unknown or noted too long ago."""
    if context_id is None:
        return None
    askers = _askers(hass)
    noted = askers.get(context_id)
    if noted is None:
        return None
    until, found = noted
    if until <= _clock():
        del askers[context_id]
        return None
    return found
