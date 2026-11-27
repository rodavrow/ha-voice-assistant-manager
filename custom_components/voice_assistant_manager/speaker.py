# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decide who is speaking. Pure logic with no Home Assistant imports, so it can be tested alone."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Role = Literal["owner", "wife", "stranger"]
ROLES: tuple[Role, ...] = ("owner", "wife", "stranger")


def _as_role(value: str | None, default: Role) -> Role:
    for role in ROLES:
        if value == role:
            return role
    return default


def resolve_speaker(
    helper_state: str | None,
    user_id: str | None,
    user_map: Iterable[tuple[Role, str | None]],
    no_helper_role: str | None,
) -> tuple[Role, bool]:
    """Return the role of the person speaking and whether a voice match decided it.

    helper_state    final state of the satellite's voice_speaker helper, or None when
                    the device has no helper (phone app, browser, other devices)
    user_id         Home Assistant user that sent the request, or None (voice satellites)
    user_map        (role, user id) pairs from the settings; empty ids are ignored
    no_helper_role  role for a request that neither a helper nor a user mapping decides

    A request with a user goes by that user alone and the helper is ignored: Home
    Assistant lets any logged-in user name any device in a typed request, and the helper
    keeps the last voice match, so trusting it would let anyone pass as that person.
    A user mapped to a person is that person; once any user is mapped, a user who is not
    mapped counts as a stranger; with nobody mapped, no_helper_role decides.

    A request without a user (a satellite's voice pipeline, an automation) goes by the
    helper when the device has one. A helper that holds anything but a role counts as a
    stranger. Without a helper either, no_helper_role decides.
    """
    fallback = _as_role(no_helper_role, "owner")

    if user_id:
        mapped = [(role, mapped_id) for role, mapped_id in user_map if mapped_id]
        for role, mapped_id in mapped:
            if mapped_id == user_id:
                return role, False
        return ("stranger" if mapped else fallback), False

    if helper_state is not None:
        if helper_state == "partner":  # "partner" is accepted as another word for "wife".
            return "wife", True
        if helper_state in ROLES:
            return _as_role(helper_state, fallback), True
        # Still pending (the voice match never arrived) or a value that is not a role:
        # refuse, so a misconfigured satellite fails closed.
        return "stranger", False

    return fallback, False


# How long after a turn whose reply asked a question the answer may come, on the same
# satellite, to count as that person's. A setting; this is its default.
FOLLOW_UP_WINDOW = timedelta(seconds=60)
# A request may go on this long after its voice match was written: the match lands a few
# seconds after the wake word, and Home Assistant ends a spoken request after 15 s.
UTTERANCE_ALLOWANCE = timedelta(seconds=30)
# How long a conversation's person is remembered at most: Home Assistant's own conversation
# lifetime.
CONVERSATION_LIFETIME = timedelta(minutes=5)
# Helper values that are a voice match's verdict, unlike "pending".
_VERDICTS = ("owner", "wife", "partner", "stranger")


def is_verdict(helper_state: str | None) -> bool:
    """Whether a helper's value says who spoke: a person or an unknown voice."""
    return helper_state in _VERDICTS


@dataclass(frozen=True)
class Remembered:
    """Who spoke in a voice conversation: a person, never an unknown voice."""

    role: Role  # owner or wife
    device_id: str
    decided_by: datetime | None  # when the helper got the voice match that decided it
    at: datetime  # when that person's last turn was resolved
    # Whether the reply to that turn asked a question, so an answer is expected.
    asked: bool = False


def follow_up_role(
    remembered: Remembered | None,
    device_id: str | None,
    helper_state: str | None,
    helper_changed: datetime | None,
    now: datetime,
    woke_since: bool | None,
    window: timedelta = FOLLOW_UP_WINDOW,
) -> Role | None:
    """The person a follow-up keeps, or None when this turn needs its own voice match.

    A follow-up is the answer to a question the assistant asked: the satellite listens
    again without a wake word, and satellites often make no voice match for it. So the
    person who was asked is kept, on the same satellite, unless a voice match arrived
    since then: another person switches, an unknown voice refuses. After a reply that
    asked nothing no answer is expected, so the next turn needs its own match: otherwise
    anyone who spoke next would be taken for that person.

    woke_since  True when the satellite reported a new wake word since the previous turn:
                then this is a new turn. Anything else proves nothing, because Home
                Assistant drops a report with an unchanged value, so a second wake word
                with the same phrase never shows.
    window      how long after the reply the answer may come; zero keeps nobody.
    """
    if remembered is None or device_id != remembered.device_id:
        return None
    if not remembered.asked or woke_since is True:
        return None
    if now - remembered.at > window or window <= timedelta(0):
        return None
    new_match = (
        is_verdict(helper_state)
        and helper_changed is not None
        and (remembered.decided_by is None or helper_changed > remembered.decided_by)
    )
    return None if new_match else remembered.role


def verdict_is_current(
    helper_state: str | None,
    reported: datetime,
    since: datetime | None,
    now: datetime,
    max_age: timedelta,
) -> bool:
    """Whether the helper holds a voice match that was made for the turn running now.

    The helper keeps a match until something writes over it, so its value alone says who
    spoke last, not who speaks now.

    reported  when the helper's value was last written
    since     when this turn started, as far as that is known (the satellite started
              listening, or reported its wake word); a match written before it is an
              earlier turn's
    max_age   a match written longer ago than this is never this turn's
    """
    if not is_verdict(helper_state):
        return False
    if since is not None and reported < since:
        return False
    return now - reported <= max_age
