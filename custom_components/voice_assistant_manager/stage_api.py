# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the chat stage card (frontend/stage.js) may ask for, over Home Assistant's websocket.

The card runs on a cast device, logged in as Home Assistant's own "Home Assistant Cast"
user, which is an administrator, or on a page of this integration for a chat on "This
device". An administrator may use every command. A person of an assistant, who may start
such a chat on My assistant, may watch only as their own page, never the stage cast to a
device; the chat's token, which only the page showing it is sent, guards the rest.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import stage
from .avatar import LiveTalkingError
from .const import DOMAIN
from .mine import my_routers

_LOGGER = logging.getLogger(__name__)

PREFIX = f"{DOMAIN}/stage"
# An SDP offer is a few kilobytes.
MAX_SDP = 64 * 1024


def _allowed(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> bool:
    """Whether the user may use the stage at all; answers the card when not."""
    user = connection.user
    if user.is_admin or my_routers(hass, user.id):
        return True
    connection.send_error(msg["id"], websocket_api.ERR_UNAUTHORIZED, "Unauthorized")
    return False


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/subscribe",
        # The Voice Manager page watches as itself; the card cast to a device sends none.
        vol.Optional("viewer", default=""): vol.Any("", vol.Match(stage.VIEWER)),
    }
)
@callback
def ws_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The chat this viewer shows now, then each change: a line starting, the chat ending."""
    if not _allowed(hass, connection, msg):
        return
    if not msg["viewer"] and not connection.user.is_admin:
        connection.send_error(msg["id"], websocket_api.ERR_UNAUTHORIZED, "Unauthorized")
        return

    @callback
    def send(message: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], message))

    # The result first: the frontend only takes events of a subscription it has a result for.
    connection.send_result(msg["id"])
    connection.subscriptions[msg["id"]] = stage.get_stage(hass).listen(send, msg["viewer"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/offer",
        vol.Required("token"): str,
        vol.Required("key"): str,
        vol.Required("sdp"): vol.All(str, vol.Length(min=1, max=MAX_SDP)),
    }
)
@websocket_api.async_response
async def ws_offer(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Open a LiveTalking session for one assistant of the chat on the stage."""
    if not _allowed(hass, connection, msg):
        return
    try:
        answer = await stage.async_offer(hass, msg["token"], msg["key"], msg["sdp"])
    except stage.StageRefused as err:
        connection.send_error(msg["id"], "not_on_stage", str(err))
        return
    except (aiohttp.ClientError, TimeoutError, LiveTalkingError) as err:
        _LOGGER.warning("Chat stage: LiveTalking refused a session (%s)", type(err).__name__)
        connection.send_error(msg["id"], "livetalking", type(err).__name__)
        return
    connection.send_result(msg["id"], {"sdp": answer, "type": "answer"})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{PREFIX}/connected",
        vol.Required("token"): str,
        vol.Required("key"): str,
        vol.Required("up"): bool,
    }
)
@callback
def ws_connected(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """One assistant's stream is connected on the card, or was lost."""
    if not _allowed(hass, connection, msg):
        return
    try:
        stage.connected(hass, msg["token"], msg["key"], msg["up"])
    except stage.StageRefused as err:
        connection.send_error(msg["id"], "not_on_stage", str(err))
        return
    connection.send_result(msg["id"])


@callback
def async_register_stage_commands(hass: HomeAssistant) -> None:
    for command in (ws_subscribe, ws_offer, ws_connected):
        websocket_api.async_register_command(hass, command)
