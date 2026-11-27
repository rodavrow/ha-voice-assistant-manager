# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small things several modules of Voice Assistant Manager need in the same way."""

from __future__ import annotations

from homeassistant.helpers import intent


def speech_text(response: intent.IntentResponse) -> str:
    """What a reply says, or "" when it says nothing."""
    return str((response.speech.get("plain") or {}).get("speech") or "")
