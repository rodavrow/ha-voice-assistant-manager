# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""When each satellite last started listening.

A voice match belongs to a turn only when it was written after the satellite started
listening for that turn. The satellite's helper cannot say when that was: it holds the last
match until something writes "pending", and what writes "pending" is the person's own
automation, which may be off, may skip a wake word that follows a reply closely, or may
not know the satellite. Home Assistant's assist_satellite entity does say it, by going to
"listening", so that is noted here for every satellite.

Only kept in memory: after a restart nothing is known until a satellite listens again,
which is when its next turn starts.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import TrackStates, async_track_state_change_filtered

from . import data_keys

SATELLITE_DOMAIN = "assist_satellite"
LISTENING = "listening"
_SINCE = data_keys.LISTENING_SINCE


def _since(hass: HomeAssistant) -> dict[str, datetime]:
    since: dict[str, datetime] = data_keys.domain_data(hass).setdefault(_SINCE, {})
    return since


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Note each satellite's change to listening, for as long as Home Assistant runs."""

    @callback
    def changed(event: Event[EventStateChangedData]) -> None:
        new, old = event.data["new_state"], event.data["old_state"]
        if new is None or new.state != LISTENING:
            return
        if old is not None and old.state == LISTENING:
            return
        _since(hass)[new.entity_id] = new.last_changed

    async_track_state_change_filtered(hass, TrackStates(False, set(), {SATELLITE_DOMAIN}), changed)


def listening_since(
    hass: HomeAssistant, device_id: str | None, satellite_id: str | None
) -> datetime | None:
    """When the satellite of this request last started listening, or None if not known.

    `satellite_id` is the request's satellite entity; without it, the device's.
    """
    since = _since(hass)
    if satellite_id:
        return since.get(satellite_id)
    if not device_id:
        return None
    registry = er.async_get(hass)
    found = [
        since[entity.entity_id]
        for entity in er.async_entries_for_device(registry, device_id)
        if entity.domain == SATELLITE_DOMAIN and entity.entity_id in since
    ]
    return max(found) if found else None
