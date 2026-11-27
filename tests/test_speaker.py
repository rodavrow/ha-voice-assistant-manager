# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for resolve_speaker: who is speaking, from the helper, the logged-in user or the fallback."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.voice_assistant_manager.speaker import (
    FOLLOW_UP_WINDOW,
    Remembered,
    follow_up_role,
    resolve_speaker,
)

PERSON_1_USER = "user-person-1"
PERSON_2_USER = "user-person-2"
GUEST_USER = "user-guest"
BOTH_MAPPED = [("owner", PERSON_1_USER), ("wife", PERSON_2_USER)]
NONE_MAPPED = [("owner", None), ("wife", None)]


# A request without a user (a satellite's voice pipeline): the helper decides.


@pytest.mark.parametrize(
    ("helper_state", "expected_role"),
    [("owner", "owner"), ("wife", "wife"), ("partner", "wife"), ("stranger", "stranger")],
)
def test_helper_role_is_used_and_counts_as_voice_match(helper_state, expected_role):
    assert resolve_speaker(helper_state, None, NONE_MAPPED, "owner") == (expected_role, True)


def test_helper_stranger_wins_over_the_fallback_without_a_user():
    assert resolve_speaker("stranger", None, BOTH_MAPPED, "owner") == ("stranger", True)


def test_helper_still_pending_after_wait_is_stranger_without_voice_match():
    assert resolve_speaker("pending", None, BOTH_MAPPED, "owner") == ("stranger", False)


@pytest.mark.parametrize(
    "helper_state", ["", "unknown", "unavailable", "Owner", "Stranger", "guest", " owner"]
)
def test_regression_review_5_unexpected_helper_value_is_refused_as_a_stranger(helper_state):
    # A helper that exists but holds something else must fail closed, not fall back
    # to the no-helper role.
    assert resolve_speaker(helper_state, None, BOTH_MAPPED, "owner") == (
        "stranger",
        False,
    )


# No helper: the logged-in user decides once any user is mapped.


@pytest.mark.parametrize(
    ("user_id", "expected_role"),
    [(PERSON_1_USER, "owner"), (PERSON_2_USER, "wife")],
)
def test_mapped_user_without_helper_is_that_person(user_id, expected_role):
    assert resolve_speaker(None, user_id, BOTH_MAPPED, "owner") == (expected_role, False)


def test_unmapped_user_without_helper_is_stranger_once_any_user_is_mapped():
    only_person_1 = [("owner", PERSON_1_USER), ("wife", None)]
    assert resolve_speaker(None, GUEST_USER, only_person_1, "owner") == ("stranger", False)


def test_unmapped_user_without_helper_uses_fallback_when_nobody_is_mapped():
    assert resolve_speaker(None, GUEST_USER, NONE_MAPPED, "wife") == ("wife", False)


def test_empty_string_mapping_counts_as_not_mapped():
    assert resolve_speaker(None, GUEST_USER, [("owner", ""), ("wife", "")], "owner") == ("owner", False)


@pytest.mark.parametrize("user_id", [None, ""])
def test_no_user_and_no_helper_uses_fallback_even_when_users_are_mapped(user_id):
    assert resolve_speaker(None, user_id, BOTH_MAPPED, "stranger") == ("stranger", False)


def test_same_user_mapped_twice_resolves_to_person_1():
    both_same = [("owner", PERSON_1_USER), ("wife", PERSON_1_USER)]
    assert resolve_speaker(None, PERSON_1_USER, both_same, "stranger") == ("owner", False)


# The no-helper fallback setting itself.


@pytest.mark.parametrize("no_helper_role", [None, "", "partner", "nobody"])
def test_missing_or_invalid_fallback_setting_defaults_to_person_1(no_helper_role):
    assert resolve_speaker(None, None, NONE_MAPPED, no_helper_role) == ("owner", False)


# A logged-in user decides alone: the helper only speaks for requests without a user.
# Home Assistant lets any logged-in user name any device_id in a typed request, and the
# helper keeps the last voice match, so trusting it would let anyone pass as that person.


def test_regression_security_helper_is_ignored_for_a_mapped_logged_in_user():
    assert resolve_speaker("owner", PERSON_2_USER, BOTH_MAPPED, "owner") == ("wife", False)


def test_regression_security_helper_is_ignored_for_an_unmapped_logged_in_user():
    assert resolve_speaker("owner", GUEST_USER, BOTH_MAPPED, "owner") == ("stranger", False)


def test_regression_security_helper_is_ignored_for_a_user_when_nobody_is_mapped():
    assert resolve_speaker("owner", GUEST_USER, NONE_MAPPED, "stranger") == ("stranger", False)


def test_regression_security_helper_still_decides_a_request_without_a_user():
    assert resolve_speaker("owner", None, BOTH_MAPPED, "stranger") == ("owner", True)



# A follow-up in the same voice conversation keeps its speaker unless a new match says else.

T0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
# Alex's turn, whose reply asked a question: only then is the next turn without a voice
# match taken for the answer (decided 2026-11-27, S1 of the code review).
ALEX_TURN = Remembered(role="owner", device_id="kitchen", decided_by=T0, at=T0, asked=True)


def later(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.mark.parametrize("helper_state", ["pending", "unknown", ""])
def test_a_follow_up_without_a_new_match_keeps_the_speaker(helper_state):
    assert follow_up_role(ALEX_TURN, "kitchen", helper_state, later(5), later(8), None) == "owner"


def test_a_follow_up_with_the_old_match_unchanged_keeps_the_speaker():
    assert follow_up_role(ALEX_TURN, "kitchen", "owner", T0, later(8), None) == "owner"


@pytest.mark.parametrize("helper_state", ["wife", "partner", "stranger", "owner"])
def test_a_new_voice_match_decides_the_follow_up(helper_state):
    assert follow_up_role(ALEX_TURN, "kitchen", helper_state, later(5), later(8), None) is None


def test_a_new_turn_on_the_satellite_needs_its_own_match():
    """A wake word (or the Wake button) was reported since the last turn: not a follow-up."""
    assert follow_up_role(ALEX_TURN, "kitchen", "pending", later(5), later(8), True) is None


def test_no_wake_report_is_no_proof_of_a_follow_up():
    """An unchanged wake word never reaches Home Assistant: the window still applies."""
    seconds = FOLLOW_UP_WINDOW.total_seconds() + 1
    assert follow_up_role(ALEX_TURN, "kitchen", "pending", later(5), later(seconds), None) is None


def test_without_wake_reports_a_turn_inside_the_window_is_a_follow_up():
    seconds = FOLLOW_UP_WINDOW.total_seconds() - 1
    assert follow_up_role(ALEX_TURN, "kitchen", "pending", later(5), later(seconds), None) == (
        "owner"
    )


def test_without_wake_reports_a_turn_after_the_window_needs_its_own_match():
    seconds = FOLLOW_UP_WINDOW.total_seconds() + 1
    assert follow_up_role(ALEX_TURN, "kitchen", "pending", later(5), later(seconds), None) is None


def test_another_device_does_not_carry_the_speaker():
    assert follow_up_role(ALEX_TURN, "office", "pending", later(5), later(8), None) is None


def test_nothing_remembered_means_no_follow_up():
    assert follow_up_role(None, "kitchen", "pending", later(5), later(8), None) is None
