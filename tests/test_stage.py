# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A chat show on a Google Cast device shows the avatars talking on the chat stage."""

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import entity_registry as er

from custom_components.voice_assistant_manager import stage
from custom_components.voice_assistant_manager.avatar import stage_sessions
from custom_components.voice_assistant_manager.chat_show import ChatLimits

from .test_avatar_tts import add_avatar, engines, sent, set_avatar  # noqa: F401 - fixtures
from .test_chat_show import (  # noqa: F401 - fixtures
    ADA_LINE,
    JEEVES_LINE,
    chat_audio,
    fake_tts,
    run_chat,
    show,
)

TV = "media_player.living_room_tv"
TWO_LINES = ChatLimits(max_lines=2, pause=0)
FOUR_LINES = ChatLimits(max_lines=4, pause=0)


def add_cast_player(hass: HomeAssistant, entity_id: str = TV) -> str:
    object_id = entity_id.split(".")[1]
    er.async_get(hass).async_get_or_create(
        "media_player", "cast", object_id, suggested_object_id=object_id
    )
    features = MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.TURN_OFF
    hass.states.async_set(entity_id, "off", {"supported_features": int(features)})
    return entity_id


def set_up_dashboard(hass: HomeAssistant, *, dashboard=True, resource=True) -> None:
    items = [{"url": f"{stage.SCRIPT_URL}?v=1", "type": "module"}] if resource else []
    hass.data[LOVELACE_DATA] = SimpleNamespace(
        resource_mode="storage",
        dashboards={stage.DASHBOARD: object()} if dashboard else {},
        resources=SimpleNamespace(async_items=lambda: items),
        yaml_dashboards={},
    )


class FakeLiveTalking:
    """LiveTalking as the stage reaches it: sessions opened, clips said, and speaking."""

    def __init__(self) -> None:
        self.offers: list[tuple[str, str]] = []
        self.said: list[tuple[str, str]] = []
        self.fail_say_at: int | None = None
        self._speaking: dict[str, int] = {}

    def client(self, session, url: str) -> "FakeLiveTalking":
        return self

    async def async_offer(self, sdp: str, avatar_id: str = "") -> tuple[str, str]:
        self.offers.append((sdp, avatar_id))
        return f"answer-{avatar_id}", f"session-{avatar_id}"

    async def async_say(self, session_id: str, extension: str, audio: bytes) -> None:
        if self.fail_say_at == len(self.said) + 1:
            raise aiohttp.ClientError("LiveTalking went away")
        self.said.append((session_id, extension))
        # Speaking for two polls, then done.
        self._speaking[session_id] = 2

    async def async_speaking(self, session_id: str) -> bool:
        left = self._speaking.get(session_id, 0)
        self._speaking[session_id] = max(left - 1, 0)
        return left > 0


class FakeCast:
    """The cast device: shows the stage dashboard and runs the card on it."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.shown: list[dict[str, Any]] = []
        self.turned_off: list[str] = []
        self.events: list[str] = []
        self.connects = True
        self._stop = None
        hass.services.async_register("cast", "show_lovelace_view", self._show)
        hass.services.async_register("media_player", "turn_off", self._off)

    async def _show(self, call: ServiceCall) -> None:
        self.shown.append(dict(call.data))
        self.hass.states.async_set(TV, "idle", {"app_name": "Home Assistant Cast"})
        self._stop = stage.get_stage(self.hass).listen(self._card)

    @callback
    def _card(self, message: dict[str, Any]) -> None:
        self.events.append(message["event"])
        if message["event"] == "start" and self.connects:
            for actor in message["actors"]:
                self.hass.async_create_task(self._connect(message["token"], actor["key"]))

    async def _connect(self, token: str, key: str) -> None:
        await stage.async_offer(self.hass, token, key, "offer-sdp")
        stage.connected(self.hass, token, key, True)

    async def _off(self, call: ServiceCall) -> None:
        self.turned_off.append(call.data["entity_id"])


@pytest.fixture
async def cast(hass, show, engines, sent):
    """Both assistants have an avatar, the TV is a cast device, and the stage is set up."""
    add_cast_player(hass)
    set_up_dashboard(hass)
    await add_avatar(hass, show.jeeves, "tts.cloud_voice", title="Jeeves avatar")
    set_avatar(hass, show.jeeves, avatar_id="jeeves")
    await add_avatar(hass, show.ada, "tts.cloud_voice")
    set_avatar(hass, show.ada, avatar_id="ada")
    await hass.async_block_till_done()
    live = FakeLiveTalking()
    device = FakeCast(hass)

    async def played(number: int) -> None:
        # Like a cast device: a line plays, then the player is idle again.
        player = show.speakers.played[-1]["entity_id"]
        hass.states.async_set(player, "playing")
        hass.loop.call_later(0.01, hass.states.async_set, player, "idle")

    show.speakers.hook = played
    with (
        patch.object(stage, "LiveTalkingClient", live.client),
        patch.object(stage, "POLL", 0),
        patch.object(stage, "TAIL", 0),
        patch.object(stage, "LINGER", 0),
        patch("homeassistant.components.tts.generate_media_source_id", fake_tts),
    ):
        yield SimpleNamespace(live=live, device=device)


async def chat_on_tv(hass, show, limits=TWO_LINES) -> None:
    await run_chat(hass, show, speaker=TV, limits=limits)


# On the stage


async def test_a_chat_on_a_cast_device_is_shown_with_the_avatars(hass, show, cast):
    await chat_on_tv(hass, show)

    assert cast.device.shown == [
        {"entity_id": TV, "dashboard_path": stage.DASHBOARD, "view_path": stage.VIEW}
    ]
    assert sorted(cast.live.offers) == [("offer-sdp", "ada"), ("offer-sdp", "jeeves")]
    # Each line goes to its own assistant's avatar, and is never also played as audio.
    assert cast.live.said == [("session-jeeves", "mp3"), ("session-ada", "mp3")]
    assert show.speakers.played == []
    assert show.manager.state()["ended"] == "chat_done"


async def test_the_card_hears_each_line_and_the_end(hass, show, cast):
    await chat_on_tv(hass, show)

    assert cast.device.events == ["start", "line", "line", "end"]
    assert cast.device.turned_off == [TV]
    assert stage.get_stage(hass).show is None


async def test_captions_are_sent_only_when_switched_on(hass, show, cast):
    lines: list[dict[str, Any]] = []
    stage.get_stage(hass).listen(lambda m: lines.append(m) if m["event"] == "line" else None)

    with patch.object(stage, "settings", return_value=stage.StageSettings(captions=True)):
        await chat_on_tv(hass, show)

    assert [m["text"] for m in lines] == [JEEVES_LINE, ADA_LINE]


async def test_without_captions_no_line_text_reaches_the_card(hass, show, cast):
    lines: list[dict[str, Any]] = []
    stage.get_stage(hass).listen(lambda m: lines.append(m) if m["event"] == "line" else None)

    await chat_on_tv(hass, show)

    assert [m["text"] for m in lines] == ["", ""]
    assert [m["speaking"] for m in lines] == [show.jeeves.entry_id, show.ada.entry_id]


async def test_the_stage_sessions_never_get_a_copy_of_the_avatars_speech(hass, show, cast):
    seen: list[frozenset[str]] = []
    original = FakeLiveTalking.async_say

    async def say(self, *args, **kwargs):
        seen.append(stage_sessions(hass))
        return await original(self, *args, **kwargs)

    with patch.object(FakeLiveTalking, "async_say", say):
        await chat_on_tv(hass, show)

    assert seen == [frozenset({"session-jeeves", "session-ada"})] * 2
    assert stage_sessions(hass) == frozenset()


async def test_the_cast_is_left_alone_when_something_else_took_the_device(hass, show, cast):
    original = FakeLiveTalking.async_say

    async def say(self, session_id, extension, audio):
        await original(self, session_id, extension, audio)
        hass.states.async_set(TV, "playing", {"app_name": "YouTube"})

    with patch.object(FakeLiveTalking, "async_say", say):
        await chat_on_tv(hass, show)

    assert cast.device.turned_off == []


# Falling back to audio


async def test_a_device_that_is_not_cast_plays_the_lines(hass, show, cast):
    from .test_chat_show import add_speaker  # noqa: PLC0415

    add_speaker(hass, "media_player.kitchen_speaker", "idle", 1 << 9)
    await run_chat(hass, show, speaker="media_player.kitchen_speaker", limits=TWO_LINES)

    assert cast.device.shown == []
    assert len(show.speakers.played) == 2


@pytest.mark.parametrize(
    ("dashboard", "resource", "why"),
    [(False, True, "no_dashboard"), (True, False, "no_resource")],
)
async def test_a_stage_not_set_up_plays_the_lines(
    hass, show, cast, caplog, dashboard, resource, why
):
    set_up_dashboard(hass, dashboard=dashboard, resource=resource)

    with caplog.at_level(logging.INFO):
        await chat_on_tv(hass, show)

    assert cast.device.shown == []
    assert len(show.speakers.played) == 2
    assert any(why in r.getMessage() for r in caplog.records)


async def test_switched_off_plays_the_lines(hass, show, cast):
    with patch.object(stage, "settings", return_value=stage.StageSettings(enabled=False)):
        await chat_on_tv(hass, show)

    assert cast.device.shown == []
    assert len(show.speakers.played) == 2


async def test_an_assistant_without_an_avatar_plays_the_lines(hass, show, cast):
    set_avatar(hass, show.ada, enabled=False)
    await hass.async_block_till_done()

    await chat_on_tv(hass, show)

    assert cast.device.shown == []
    assert len(show.speakers.played) == 2


async def test_no_room_on_livetalking_plays_the_lines(hass, show, cast, caplog):
    taken = frozenset(f"s{n}" for n in range(stage.MAX_SESSIONS - 1))
    with patch.object(stage, "device_sessions", return_value=taken):
        await chat_on_tv(hass, show)

    assert cast.device.shown == []
    assert any("room" in r.getMessage() for r in caplog.records)


async def test_a_card_that_never_connects_plays_the_lines(hass, show, cast, caplog):
    cast.device.connects = False

    with patch.object(stage, "CONNECT_WAIT", 0.05), caplog.at_level(logging.WARNING):
        await chat_on_tv(hass, show)

    assert len(show.speakers.played) == 2
    assert cast.device.events[-1] == "end"
    assert any("not connected" in r.getMessage() for r in caplog.records)


async def test_a_line_that_cannot_be_said_is_played_and_so_is_the_rest(hass, show, cast):
    cast.live.fail_say_at = 2

    await chat_on_tv(hass, show, FOUR_LINES)

    assert cast.live.said == [("session-jeeves", "mp3")]
    assert len(show.speakers.played) == 3
    assert show.manager.state()["ended"] == "chat_done"


async def test_a_lost_stream_plays_the_rest_as_audio(hass, show, cast):
    original = FakeLiveTalking.async_say

    async def say(self, session_id, extension, audio):
        await original(self, session_id, extension, audio)
        current = stage.get_stage(hass).show
        stage.connected(hass, current.token, show.ada.entry_id, False)

    with patch.object(FakeLiveTalking, "async_say", say):
        await chat_on_tv(hass, show)

    assert len(cast.live.said) == 1
    assert len(show.speakers.played) == 1


async def test_a_chat_stopped_while_the_card_connects_ends_the_stage(hass, show, cast):
    cast.device.connects = False
    from .test_chat_show import start  # noqa: PLC0415

    await start(show, speaker=TV, limits=TWO_LINES)
    for _ in range(20):
        await asyncio.sleep(0)
    task = show.manager._chat.task
    show.manager._cancel(show.manager._chat)
    await asyncio.wait({task}, timeout=5)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert cast.device.events[-1] == "end"
    assert stage.get_stage(hass).show is None


async def test_a_line_that_never_starts_speaking_is_given_up_on(hass, show, cast, caplog):
    async def never(self, session_id):
        return False

    with (
        patch.object(FakeLiveTalking, "async_speaking", never),
        patch.object(stage, "FIRST_SOUND", 0.01),
        caplog.at_level(logging.WARNING),
    ):
        await chat_on_tv(hass, show)

    assert len(cast.live.said) == 2
    assert show.speakers.played == []
    assert any("did not start speaking" in r.getMessage() for r in caplog.records)


# What the card may ask


async def test_an_offer_for_another_chat_is_refused(hass, show, cast):
    with pytest.raises(stage.StageRefused):
        await stage.async_offer(hass, "old-token", show.ada.entry_id, "offer-sdp")


async def test_the_websocket_card_api(hass, hass_ws_client, show, cast):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "voice_assistant_manager/stage/subscribe"})
    first = await client.receive_json()
    assert first["success"] is True
    event = await client.receive_json()
    assert event["event"] == {"event": "idle"}

    await client.send_json_auto_id(
        {
            "type": "voice_assistant_manager/stage/offer",
            "token": "nope",
            "key": "nope",
            "sdp": "offer-sdp",
        }
    )
    refused = await client.receive_json()
    assert refused["success"] is False
    assert refused["error"]["code"] == "not_on_stage"


# Settings and setup


def test_settings_from_storage():
    assert stage.StageSettings.from_dict(None) == stage.StageSettings(True, False)
    assert stage.StageSettings.from_dict({"enabled": False, "captions": True}) == (
        stage.StageSettings(False, True)
    )
    assert stage.StageSettings.from_dict({"enabled": "junk", "captions": "yes"}) == (
        stage.StageSettings(True, False)
    )


def test_setup_problems(hass):
    assert stage.setup_problem(hass) == "no_dashboards"
    set_up_dashboard(hass, dashboard=False)
    assert stage.setup_problem(hass) == "no_dashboard"
    set_up_dashboard(hass, resource=False)
    assert stage.setup_problem(hass) == "no_resource"
    set_up_dashboard(hass)
    assert stage.setup_problem(hass) == ""


def test_only_a_cast_media_player_is_a_cast_player(hass):
    add_cast_player(hass)
    er.async_get(hass).async_get_or_create(
        "media_player", "sonos", "kitchen", suggested_object_id="kitchen"
    )
    hass.states.async_set("media_player.kitchen", "idle")

    assert stage.is_cast_player(hass, TV)
    assert not stage.is_cast_player(hass, "media_player.kitchen")
    assert not stage.is_cast_player(hass, "media_player.gone")


async def test_the_page_reports_and_saves_the_stage(hass, hass_ws_client, show, cast):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "voice_assistant_manager/panel/chat/state"})
    state = (await client.receive_json())["result"]
    assert state["stage"]["enabled"] is True
    assert state["stage"]["problem"] == ""
    assert state["stage"]["dashboard"] == stage.DASHBOARD
    assert state["stage"]["resource"].startswith(stage.SCRIPT_URL)
    assert next(s for s in state["speakers"] if s["entity_id"] == TV)["cast"] is True

    await client.send_json_auto_id(
        {
            "type": "voice_assistant_manager/panel/chat/save_stage",
            "enabled": True,
            "captions": True,
        }
    )
    saved = (await client.receive_json())["result"]
    assert saved["stage"]["captions"] is True
    assert stage.settings(hass).captions is True
