# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What Voice Assistant Manager keeps in memory: every key of hass.data[DOMAIN], in one place.

Each module still owns what it stores under its key. This file is the list of them, so two
modules cannot pick the same name and it is plain what is kept per assistant.
"""

from __future__ import annotations

from typing import Any, Final

from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Per loaded entry: the platforms it set up and its avatars.
LOADED_ENTRIES: Final = "loaded_entries"
# Context id -> who asked for a favourite in that turn.
FAVORITES_ASKERS: Final = "favorites_askers"
# Context id -> who is asking in that turn.
REMINDER_ASKERS: Final = "reminder_askers"
# Entry id -> the last reply with its audio tags, for the voice.
VOICE_TAGS_SPOKEN: Final = "voice_tags_spoken"
# The sender of faces to LiveTalking.
EMOTION_SENDER: Final = "emotion_sender"
# Which wake word started each satellite's turn.
WAKE_HANDOFF: Final = "wake_handoff"
# Satellite -> when it started listening.
LISTENING_SINCE: Final = "listening_since"
# False when the announcement look is switched off on the page.
ANNOUNCEMENT_LOOK: Final = "announcement_look"
# The runner of the satellites' avatar check.
AVATAR_DEVICES: Final = "avatar_devices"
# The avatar switches this integration turned on.
AVATAR_DEVICES_OWNED: Final = "avatar_devices_owned"
# What the avatar check already warned about.
AVATAR_DEVICES_WARNED: Final = "avatar_devices_warned"
# Connection selects a chat holds, and until when.
AVATAR_DEVICES_HELD: Final = "avatar_devices_held"
# Entry id -> the turns running with that assistant.
AVATAR_STREAM_TURNS: Final = "avatar_stream_turns"
# Entry id -> its calendar watcher.
CALENDAR_WATCHERS: Final = "calendar_watchers"
# The chat shown on a cast device (stage.py).
STAGE: Final = "stage"
# Satellite -> the last LiveTalking session it reported.
AVATAR_LAST_SESSIONS: Final = "avatar_last_sessions"
# The settings page's state.
PANEL: Final = "panel"
# The memory store, once loaded.
MEMORY: Final = "memory"
# Held while the memory store loads.
MEMORY_LOCK: Final = "memory_lock"
# The favourites store, once loaded.
FAVORITES: Final = "favorites"
# Held while the favourites store loads.
FAVORITES_LOCK: Final = "favorites_lock"
# The chat show manager.
CHAT_SHOW: Final = "chat_show"


def domain_data(hass: HomeAssistant) -> dict[str, Any]:
    """This integration's part of hass.data, made when it is first asked for."""
    data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    return data
