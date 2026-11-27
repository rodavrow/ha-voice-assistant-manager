# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for S7 of the 2026-11-27 review: long text must not stall the router.

Two patterns took time that grew with the square of the text's length (0.45 s for 10,000
characters, 1.8 s for 20,000), on the event loop: every reply of every assistant waited.
The limits here are far above what the fixed code needs and far below what the old code
took, so they do not depend on how fast the machine is.
"""

import time

import pytest
from homeassistant.core import HomeAssistant

from custom_components.voice_assistant_manager import conversation as router
from custom_components.voice_assistant_manager.favorites import parse_play
from custom_components.voice_assistant_manager.reminder_search import (
    MAX_NOTES,
    _split_description,
)

from .router_helpers import MAIN_AGENT, FakeAgents, add_router, ask, options_with

# The old patterns needed about 18 s for this much; the fixed code a few milliseconds.
LONG = 60_000
ENOUGH = 2.0


def seconds(call) -> float:
    started = time.perf_counter()
    call()
    return time.perf_counter() - started


def test_a_long_run_of_punctuation_in_a_play_request_is_read_quickly():
    assert seconds(lambda: parse_play("play " + "." * LONG + "x")) < ENOUGH


def test_a_long_run_of_spaces_and_commas_before_the_end_is_read_quickly():
    assert seconds(lambda: parse_play("play " + ", " * (LONG // 2) + "x")) < ENOUGH


@pytest.mark.parametrize(
    ("said", "pairs"),
    [
        ("Play Radio One.", [("Radio One", None)]),
        ("play radio one , please !", [("radio one", None)]),
        ("Could you please play jazz in the kitchen?", [("jazz in the kitchen", None),
                                                         ("jazz", "the kitchen")]),
        ("play yes please please", [("yes please", None)]),
        ("play jazz . ", [("jazz", None)]),
        ("what is playing?", []),
    ],
)
def test_play_requests_are_read_as_before(said, pairs):
    assert parse_play(said) == pairs


def test_a_long_run_of_spaces_in_a_reminders_notes_is_read_quickly():
    note = "assistant: a" + " " * LONG + "b"

    assert seconds(lambda: _split_description(note)) < ENOUGH


@pytest.mark.parametrize(
    ("description", "assistant", "notes"),
    [
        ("assistant: jeeves\nBring the blue folder", "jeeves", "Bring the blue folder"),
        ("  Assistant:   Ada  \n\n  two\n three ", "Ada", "two\nthree"),
        ("assistant: jeeves\nassistant: ada", "jeeves", "assistant: ada"),
        ("assistant :jeeves", "", "assistant :jeeves"),
        ("the assistant: jeeves", "", "the assistant: jeeves"),
        ("assistant:", "", ""),
        ("", "", ""),
    ],
)
def test_reminder_notes_are_split_as_before(description, assistant, notes):
    assert _split_description(description) == (assistant, notes)


def test_only_the_start_of_a_very_long_note_is_read():
    assistant, notes = _split_description("assistant: jeeves\n" + "x" * (MAX_NOTES * 2))

    assert assistant == "jeeves"
    assert len(notes) <= MAX_NOTES


async def test_a_request_longer_than_the_limit_is_refused_before_anyone_reads_it(
    hass: HomeAssistant,
):
    entry = await add_router(hass, options_with(owner={"user": "u-alex"}))
    agents = FakeAgents()

    with agents.patched():
        result = await ask(hass, entry, "x" * (router.MAX_REQUEST_CHARS + 1), user_id="u-alex")

    assert result.response.speech["plain"]["speech"] == router.TOO_LONG_LINE
    assert agents.calls == []


async def test_a_request_of_exactly_the_limit_is_answered(hass: HomeAssistant):
    entry = await add_router(hass, options_with(owner={"user": "u-alex"}))
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, "x" * router.MAX_REQUEST_CHARS, user_id="u-alex")

    assert len(agents.calls_to(MAIN_AGENT)) == 1
