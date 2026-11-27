# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A chat show on "This device": the page that started it shows the avatars talking."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, callback

from custom_components.voice_assistant_manager import stage
from custom_components.voice_assistant_manager.chat_show import ChatLimits, ChatRefused

from .test_avatar_tts import add_avatar, engines, sent, set_avatar  # noqa: F401 - fixtures
from .test_chat_show import chat_audio, fake_tts, run_chat, show  # noqa: F401 - fixtures
from .test_stage import FakeCast, FakeLiveTalking, add_cast_player, set_up_dashboard

VIEWER = "0123456789abcdef"
HERE = stage.page_speaker(VIEWER)
TWO_LINES = ChatLimits(max_lines=2, pause=0)
FOUR_LINES = ChatLimits(max_lines=4, pause=0)


class FakePage:
    """The panel page: watches the stage as one viewer and runs the card on it."""

    def __init__(self, hass: HomeAssistant, viewer: str = VIEWER) -> None:
        self.hass = hass
        self.events: list[str] = []
        self.connects = True
        self.close_after_lines: int | None = None
        self.lose_after_lines: int | None = None
        self._lines = 0
        self.close = stage.get_stage(hass).listen(self._card, viewer)

    @callback
    def _card(self, message: dict[str, Any]) -> None:
        self.events.append(message["event"])
        if message["event"] == "start" and self.connects:
            for actor in message["actors"]:
                self.hass.async_create_task(self._connect(message["token"], actor["key"]))
        if message["event"] == "line":
            self._lines += 1
            if self._lines == self.close_after_lines:
                self.close()
            if self._lines == self.lose_after_lines:
                stage.connected(self.hass, message["token"], message["speaking"], False)

    async def _connect(self, token: str, key: str) -> None:
        await stage.async_offer(self.hass, token, key, "offer-sdp")
        stage.connected(self.hass, token, key, True)


@pytest.fixture
async def page(hass, show, engines, sent):
    """Both assistants have an avatar and the panel page is open; no stage dashboard."""
    await add_avatar(hass, show.jeeves, "tts.cloud_voice", title="Jeeves avatar")
    set_avatar(hass, show.jeeves, avatar_id="jeeves")
    await add_avatar(hass, show.ada, "tts.cloud_voice")
    set_avatar(hass, show.ada, avatar_id="ada")
    await hass.async_block_till_done()
    live = FakeLiveTalking()
    with (
        patch.object(stage, "LiveTalkingClient", live.client),
        patch.object(stage, "POLL", 0),
        patch.object(stage, "TAIL", 0),
        patch.object(stage, "LINGER", 0),
        patch.object(stage, "PAGE_CONNECT_WAIT", 0.05),
        patch.object(stage, "PAGE_GONE_AFTER", 0.05),
        patch("homeassistant.components.tts.generate_media_source_id", fake_tts),
    ):
        yield SimpleNamespace(live=live, here=FakePage(hass))


async def chat_here(hass, show, limits=TWO_LINES) -> None:
    await run_chat(hass, show, speaker=HERE, limits=limits)


def test_a_page_speaker_names_its_viewer():
    assert stage.viewer_of(HERE) == VIEWER
    assert stage.viewer_of("media_player.living_room_tv") == ""
    assert stage.viewer_of("page:not hex") == ""
    assert stage.viewer_of("page:abc") == ""  # Too short to be one of the panel's.


async def test_a_chat_on_this_device_is_shown_with_the_avatars(hass, show, page):
    await chat_here(hass, show)

    assert sorted(page.live.offers) == [("offer-sdp", "ada"), ("offer-sdp", "jeeves")]
    assert page.live.said == [("session-jeeves", "mp3"), ("session-ada", "mp3")]
    assert page.here.events == ["idle", "start", "line", "line", "end"]
    assert show.speakers.played == []
    assert show.manager.state()["ended"] == "chat_done"
    assert stage.get_stage(hass).show is None


async def test_nothing_is_cast_and_no_device_is_turned_off(hass, show, page):
    add_cast_player(hass)
    device = FakeCast(hass)

    await chat_here(hass, show)

    assert device.shown == []
    assert device.turned_off == []


async def test_it_needs_no_stage_dashboard_and_ignores_the_tv_stage_switch(hass, show, page):
    hass.data[stage.DOMAIN][stage.data_keys.PANEL] = SimpleNamespace(
        chat_stage=stage.StageSettings(enabled=False)
    )

    await chat_here(hass, show)

    assert len(page.live.said) == 2


async def test_only_the_page_that_started_it_is_told(hass, show, page):
    other = FakePage(hass, "fedcba9876543210")
    tv_card = FakePage(hass, "")

    await chat_here(hass, show)

    assert other.events == ["idle"]
    assert tv_card.events == ["idle"]
    assert len(page.live.offers) == 2


async def test_a_page_is_not_told_of_a_chat_cast_to_a_tv(hass, show, page):
    tv = add_cast_player(hass)
    set_up_dashboard(hass)
    FakeCast(hass)

    await run_chat(hass, show, speaker=tv, limits=TWO_LINES)

    assert page.here.events == ["idle"]
    assert len(page.live.said) == 2


async def test_refused_when_an_assistant_has_no_avatar(hass, show, page):
    set_avatar(hass, show.ada, enabled=False)
    await hass.async_block_till_done()

    with pytest.raises(ChatRefused) as refused:
        await chat_here(hass, show)

    assert refused.value.key == "chat_page_no_avatar"
    assert page.here.events == ["idle"]


async def test_refused_when_livetalking_has_no_room(hass, show, page):
    taken = frozenset(f"s{n}" for n in range(stage.MAX_SESSIONS - 1))
    with (
        patch.object(stage, "present_device_sessions", return_value=taken),
        pytest.raises(ChatRefused) as refused,
    ):
        await chat_here(hass, show)

    assert refused.value.key == "chat_page_no_room"


async def test_refused_when_the_page_is_not_watching(hass, show, page):
    page.here.close()

    with pytest.raises(ChatRefused) as refused:
        await chat_here(hass, show)

    assert refused.value.key == "chat_page_gone"


async def test_a_speaker_that_is_no_page_of_the_panel_is_refused(hass, show, page):
    with pytest.raises(ChatRefused) as refused:
        await run_chat(hass, show, speaker="page:../../x", limits=TWO_LINES)

    assert refused.value.key == "chat_bad_speaker"


async def test_a_page_that_never_connects_ends_the_chat_unspoken(hass, show, page):
    page.here.connects = False

    await chat_here(hass, show)

    assert show.manager.state()["ended"] == "chat_page_not_connected"
    assert page.live.said == []
    assert show.speakers.played == []
    assert stage.get_stage(hass).show is None


async def test_a_page_that_was_closed_ends_the_chat(hass, show, page):
    page.here.close_after_lines = 1

    await chat_here(hass, show, FOUR_LINES)

    assert show.manager.state()["ended"] == "chat_page_gone"
    assert len(page.live.said) == 1
    assert show.speakers.played == []


async def test_a_page_that_comes_back_in_time_keeps_the_chat(hass, show, page):
    """A websocket that drops for a moment subscribes again; the streams are still up."""
    page.here.close_after_lines = 1
    watching = stage.get_stage(hass).watched

    async def back() -> None:
        while watching(VIEWER):
            await asyncio.sleep(0.005)
        stage.get_stage(hass).listen(lambda message: None, VIEWER)

    returning = hass.async_create_task(back())
    with patch.object(stage, "PAGE_GONE_AFTER", 5.0):
        await chat_here(hass, show)
    await returning

    assert show.manager.state()["ended"] == "chat_done"


async def test_a_lost_stream_ends_the_chat_instead_of_playing_audio(hass, show, page):
    page.here.lose_after_lines = 1

    await chat_here(hass, show, FOUR_LINES)

    assert show.manager.state()["ended"] == "chat_page_gone"
    assert show.speakers.played == []


async def test_the_card_subscribes_as_a_viewer(hass, hass_ws_client, show, page):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "voice_assistant_manager/stage/subscribe", "viewer": VIEWER}
    )
    assert (await client.receive_json())["success"] is True
    assert (await client.receive_json())["event"] == {"event": "idle"}
    assert stage.get_stage(hass).watched(VIEWER)

    await client.send_json_auto_id(
        {"type": "voice_assistant_manager/stage/subscribe", "viewer": "not hex"}
    )
    assert (await client.receive_json())["success"] is False
