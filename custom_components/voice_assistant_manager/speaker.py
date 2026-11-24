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


# A follow-up in the same voice conversation, on the same satellite, within this long of
# the previous turn keeps that turn's person unless a new voice match says otherwise.
FOLLOW_UP_WINDOW = timedelta(seconds=20)
# Helper values that are a voice match's verdict, unlike "pending".
_VERDICTS = ("owner", "wife", "partner", "stranger")


@dataclass(frozen=True)
class Remembered:
    """Who spoke in a voice conversation: a person, never an unknown voice."""

    role: Role  # owner or wife
    device_id: str
    decided_by: datetime | None  # when the helper got the voice match that decided it
    at: datetime  # when that person's last turn was resolved


def follow_up_role(
    remembered: Remembered | None,
    device_id: str | None,
    helper_state: str | None,
    helper_changed: datetime | None,
    now: datetime,
) -> Role | None:
    """The person a follow-up keeps, or None when this turn needs its own voice match.

    A follow-up carries no wake word, so a satellite often makes no new voice match for it,
    and whether its tracking automation still holds the last speaker depends on the
    satellite's timing. So the person of the conversation's previous turn is kept unless
    a voice match arrived since then: another person switches, an unknown voice refuses.
    Only the same satellite, and only within FOLLOW_UP_WINDOW of the previous turn.
    """
    if remembered is None or device_id != remembered.device_id:
        return None
    if now - remembered.at > FOLLOW_UP_WINDOW:
        return None
    new_match = (
        helper_state in _VERDICTS
        and helper_changed is not None
        and (remembered.decided_by is None or helper_changed > remembered.decided_by)
    )
    return None if new_match else remembered.role
