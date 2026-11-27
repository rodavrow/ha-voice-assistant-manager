# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for S12 of the 2026-11-27 review: what people say stays out of the log.

Home Assistant's log is read by every administrator and often pasted into bug reports.
A log line says what kind of thing happened and to which entity, not what was said.
"""

import logging
from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.favorites_play import (
    PlayRefused,
    async_play,
    async_search,
)
from custom_components.voice_assistant_manager.reminder_actions import note_asker

from .router_helpers import FakeAgents, add_router, ask, options_with
from .test_favorites_play import (  # noqa: F401 - fixture
    MA,
    add_player,
    favorite,
    music_assistant_entries_not_unloaded,
)
from .test_reminder_list import calendars  # noqa: F401 - fixture

SAID = "tell me about the appointment at the clinic"


@pytest.fixture(autouse=True)
def everything_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=f"custom_components.{DOMAIN}")


def logged(caplog: pytest.LogCaptureFixture) -> str:
    """What this integration wrote to the log. (Home Assistant's own debug log of a
    conversation holds the text; that is its own, and off unless someone turns it on.)"""
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(f"custom_components.{DOMAIN}")
    )


@pytest.mark.parametrize("handled", [False, True], ids=["no_command", "a_command"])
async def test_a_request_is_not_written_to_the_log_by_the_built_in_commands(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, handled: bool
):
    entry = await add_router(
        hass, options_with(local={"enabled": True}, owner={"user": "u-alex"})
    )
    answer = None
    if handled:
        answer = conversation.intent.IntentResponse(language="en")
        answer.async_set_speech("Done.")

    with (
        FakeAgents().patched(),
        patch.object(conversation, "async_handle_intents", return_value=answer) as built_in,
    ):
        await ask(hass, entry, SAID, user_id="u-alex")

    built_in.assert_called_once()
    assert "clinic" not in logged(caplog)
    assert "uilt-in command" in logged(caplog)


async def test_a_favourite_that_does_not_start_is_logged_without_its_name(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
):
    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("the receiver is off")

    hass.services.async_register("media_player", "play_media", broken)
    player = add_player(hass, "denon", "denonavr", friendly_name="Denon")

    with pytest.raises(PlayRefused):
        await async_play(hass, favorite("Clinic waiting room radio"), player, Context())

    assert "the receiver is off" in logged(caplog)
    assert "Clinic" not in logged(caplog)


async def test_a_failed_search_is_logged_without_what_was_searched_for(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
):
    MockConfigEntry(domain=MA, state=ConfigEntryState.LOADED).add_to_hass(hass)

    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("server gone")

    hass.services.async_register(MA, "search", broken, supports_response=SupportsResponse.ONLY)

    with pytest.raises(PlayRefused):
        await async_search(hass, "clinic podcast")

    assert "server gone" in logged(caplog)
    assert "clinic" not in logged(caplog)


async def test_a_reminder_that_cannot_be_cancelled_is_logged_without_its_title(
    hass: HomeAssistant, calendars, caplog: pytest.LogCaptureFixture  # noqa: F811
):
    context = Context()
    note_asker(hass, context, "alex")

    with patch(
        "homeassistant.components.local_calendar.calendar.LocalCalendarEntity.async_delete_event",
        side_effect=HomeAssistantError("the disk is full"),
    ):
        answer = await hass.services.async_call(
            DOMAIN,
            "cancel_reminder_event",
            {"calendar": ["calendar.alex_reminders"], "title": "car", "for_whom": "alex"},
            blocking=True,
            return_response=True,
            context=context,
        )

    assert answer["status"] == "error"
    assert "the disk is full" in logged(caplog)
    assert "Charge the car" not in logged(caplog)
