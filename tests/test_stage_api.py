# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The chat stage card's websocket commands: who may call them and what they answer.

What the stage itself does is in test_stage.py; here the stage is replaced, so each test
sees only what the command passes on and sends back.
"""

from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.voice_assistant_manager import stage, stage_api
from custom_components.voice_assistant_manager.avatar import LiveTalkingError

from .router_helpers import add_router

PREFIX = "voice_assistant_manager/stage"
OFFER = {"token": "chat-1", "key": "ada", "sdp": "offer-sdp"}
CONNECTED = {"token": "chat-1", "key": "ada", "up": True}


async def call(client, command: str, **data: Any) -> dict[str, Any]:
    await client.send_json_auto_id({"type": f"{PREFIX}/{command}", **data})
    return await client.receive_json()


@pytest.fixture
async def card(hass: HomeAssistant, hass_ws_client):
    await add_router(hass)
    return await hass_ws_client(hass)


async def test_an_offer_is_answered_with_livetalkings_sdp(hass, card):
    async def offer(hass_, token, key, sdp):
        assert (token, key, sdp) == ("chat-1", "ada", "offer-sdp")
        return "answer-sdp"

    with patch.object(stage, "async_offer", offer):
        answer = await call(card, "offer", **OFFER)

    assert answer["result"] == {"sdp": "answer-sdp", "type": "answer"}


@pytest.mark.parametrize(
    "error", [aiohttp.ClientError("http://livetalking.local:8010"), TimeoutError(),
              LiveTalkingError("full")]
)
async def test_a_livetalking_failure_names_the_kind_of_error_only(hass, card, error):
    async def offer(*args):
        raise error

    with patch.object(stage, "async_offer", offer):
        answer = await call(card, "offer", **OFFER)

    assert answer["success"] is False
    assert answer["error"] == {"code": "livetalking", "message": type(error).__name__}


async def test_an_offer_larger_than_an_sdp_is_refused_before_the_stage_sees_it(hass, card):
    with patch.object(stage, "async_offer") as offer:
        answer = await call(card, "offer", **{**OFFER, "sdp": "x" * (stage_api.MAX_SDP + 1)})

    assert answer["error"]["code"] == "invalid_format"
    offer.assert_not_called()


async def test_a_connected_report_reaches_the_stage(hass, card):
    with patch.object(stage, "connected") as connected:
        answer = await call(card, "connected", **CONNECTED)

    assert answer["success"] is True
    connected.assert_called_once_with(hass, "chat-1", "ada", True)


async def test_a_connected_report_for_another_chat_is_refused(hass, card):
    answer = await call(card, "connected", **CONNECTED)

    assert answer["success"] is False
    assert answer["error"]["code"] == "not_on_stage"


@pytest.mark.parametrize(
    ("command", "data"), [("subscribe", {}), ("offer", OFFER), ("connected", CONNECTED)]
)
async def test_someone_who_is_not_an_admin_gets_nothing(
    hass, hass_ws_client, hass_read_only_access_token, command, data
):
    await add_router(hass)
    reader = await hass_ws_client(hass, hass_read_only_access_token)

    with patch.object(stage, "async_offer") as offer, patch.object(stage, "connected") as seen:
        answer = await call(reader, command, **data)

    assert answer["error"]["code"] == "unauthorized"
    offer.assert_not_called()
    seen.assert_not_called()
