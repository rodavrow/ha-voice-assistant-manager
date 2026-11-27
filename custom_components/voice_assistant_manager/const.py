# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Constants shared by the modules of Voice Assistant Manager."""

from typing import Final, Literal

DOMAIN = "voice_assistant_manager"

# The two people an assistant tells apart, as the speaker lookup names them.
Person = Literal["owner", "wife"]
# The settings section that holds each person's name and Home Assistant user.
PERSON_SECTION: Final[dict[Person, str]] = {"owner": "owner", "wife": "partner"}

# Defaults of settings that are read where they are used as well as shown in the form.
# How long after a satellite starts listening its voice match can still arrive, unless set
# under Advanced. LANovo publishes it 4.6-5.5 s after the wake word, whatever is said.
DEFAULT_MATCH_WITHIN: Final = 6
# The lowest score at which an unknown voice counts as nearly a known one.
DEFAULT_MIN_SCORE: Final = 0.5
# How many facts a memory list keeps.
DEFAULT_MAX_ITEMS: Final = 50

# The text sensor a satellite reports on each new turn's wake word (LANovo, EchoLocal).
WAKE_WORD_SENSOR: Final = "Last wake word"
# Seconds an agent without tools gets to write a refusal or an announcement.
REPLY_AGENT_TIMEOUT = 6
