# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Talking to an assistant's avatar from the Voice Manager page."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.voice_assistant_manager import (
    avatar,
    avatar_stream,
    panel,
    stage,
    talk,
)
from custom_components.voice_assistant_manager.avatar_stream import (
    ReplyFeed,
    feed_target,
    turn_ended,
    turn_started,
)

from .router_helpers import ask
from .test_avatar_stream import live  # noqa: F401 - fixture
from .test_avatar_tts import add_avatar, engines, sent, set_avatar  # noqa: F401 - fixtures
from .test_chat_show import chat_audio, show  # noqa: F401 - fixtures
from .test_stage import FakeLiveTalking

PREFIX = "voice_assistant_manager/panel/talk"
ALEX = "user-alex"
SAM = "user-sam"
URL = "http://livetalking.local:8010"


@pytest.fixture
async def jeeves(hass: HomeAssistant, show: Any, engines: Any, sent: Any) -> Any:
    """Jeeves has an avatar that is on, and LiveTalking takes offers."""
    await add_avatar(hass, show.jeeves, "tts.cloud_voice", title="Jeeves avatar")
    set_avatar(hass, show.jeeves, avatar_id="jeeves")
    await hass.async_block_till_done()
    server = FakeLiveTalking()
    with patch.object(talk, "LiveTalkingClient", server.client):
        yield SimpleNamespace(entry=show.jeeves, server=server, ada=show.ada)


# Opening and closing


async def test_opening_a_talk_opens_the_avatar_for_the_page(hass, jeeves):
    answer = await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")

    assert answer == "answer-jeeves"
    assert jeeves.server.offers == [("offer-sdp", "jeeves")]
    assert talk.sessions(hass) == frozenset({"session-jeeves"})


async def test_a_talk_session_never_gets_a_copy_of_the_speech_too(hass, jeeves):
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")

    assert "session-jeeves" in avatar.own_sessions(hass)


async def test_closing_a_talk_forgets_its_session(hass, jeeves):
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")

    talk.close(hass, jeeves.entry.entry_id, ALEX)

    assert talk.sessions(hass) == frozenset()


async def test_only_the_one_talking_can_close_it(hass, jeeves):
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")

    talk.close(hass, jeeves.entry.entry_id, SAM)

    assert talk.sessions(hass) == frozenset({"session-jeeves"})


async def test_opening_again_replaces_the_session(hass, jeeves):
    """The page lost its stream and connects once more."""
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")
    with patch.object(
        jeeves.server, "async_offer", return_value=("answer-2", "session-2")
    ):
        await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")

    assert talk.sessions(hass) == frozenset({"session-2"})


async def test_refused_without_an_avatar_that_is_on(hass, jeeves):
    with pytest.raises(talk.TalkRefused) as refused:
        await talk.async_open(hass, jeeves.ada, ALEX, "offer-sdp")

    assert refused.value.key == "talk_no_avatar"


async def test_refused_while_someone_else_talks_to_this_assistant(hass, jeeves):
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")

    with pytest.raises(talk.TalkRefused) as refused:
        await talk.async_open(hass, jeeves.entry, SAM, "offer-sdp")

    assert refused.value.key == "talk_busy"
    assert len(jeeves.server.offers) == 1


async def test_refused_when_livetalking_has_no_room(hass, jeeves):
    taken = frozenset(f"s{n}" for n in range(avatar.MAX_SESSIONS))
    with (
        patch.object(talk, "present_device_sessions", return_value=taken),
        pytest.raises(talk.TalkRefused) as refused,
    ):
        await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")

    assert refused.value.key == "talk_no_room"
    assert jeeves.server.offers == []


async def test_a_talk_takes_room_from_a_chat_on_the_stage(hass, jeeves):
    before = stage._room(hass)

    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")

    assert stage._room(hass) == before - 1


async def test_livetalking_refusing_is_a_refusal_with_a_reason(hass, jeeves):
    with (
        patch.object(jeeves.server, "async_offer", side_effect=avatar.LiveTalkingError("full")),
        pytest.raises(talk.TalkRefused) as refused,
    ):
        await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")

    assert refused.value.key == "talk_livetalking"
    assert talk.sessions(hass) == frozenset()


# Whose reply goes to the page


async def test_a_turn_of_the_one_talking_is_fed_to_the_page(hass, jeeves):
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")
    entry_id = jeeves.entry.entry_id

    turn_started(hass, entry_id, talk.asking(hass, entry_id, ALEX, None))

    target = feed_target(hass, jeeves.entry, URL)
    assert target is not None
    assert (target.base_url, target.session_id) == (URL, "session-jeeves")


async def test_the_router_feeds_the_page_while_it_answers_the_one_talking(hass, jeeves):
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")
    seen: list[Any] = []
    answer = conversation.async_converse

    async def converse(*args: Any, **kwargs: Any) -> Any:
        seen.append(feed_target(hass, jeeves.entry, URL))
        return await answer(*args, **kwargs)

    with patch.object(conversation, "async_converse", converse):
        await ask(hass, jeeves.entry, "Hi", user_id=ALEX)

    assert [target.session_id for target in seen] == ["session-jeeves"]


async def test_the_page_is_fed_even_with_streaming_to_satellites_switched_off(hass, jeeves):
    """The page plays only its avatar's stream, so it has no other way to hear the reply."""
    options = {**jeeves.entry.options, "advanced": {avatar_stream.CONF_STREAM: False}}
    hass.config_entries.async_update_entry(jeeves.entry, options=options)
    await hass.async_block_till_done()
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")
    entry_id = jeeves.entry.entry_id

    turn_started(hass, entry_id, talk.asking(hass, entry_id, ALEX, None))

    assert feed_target(hass, jeeves.entry, URL) is not None


async def test_someone_elses_typed_turn_is_not_fed_to_the_page(hass, jeeves):
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")
    entry_id = jeeves.entry.entry_id

    assert talk.asking(hass, entry_id, SAM, None) is None
    assert talk.asking(hass, entry_id, None, None) is None


async def test_a_turn_on_a_satellite_stays_that_satellites(hass, jeeves):
    """The one talking on the page also speaks to a satellite: that reply is the satellite's."""
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")
    entry_id = jeeves.entry.entry_id

    assert talk.asking(hass, entry_id, ALEX, "assist_satellite.kitchen") is None


async def test_a_turn_with_another_assistant_is_not_fed_to_the_page(hass, jeeves):
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")

    assert talk.asking(hass, jeeves.ada.entry_id, ALEX, None) is None


async def test_after_closing_nothing_is_fed(hass, jeeves):
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")
    entry_id = jeeves.entry.entry_id
    turn_ended(turn_started(hass, entry_id, talk.asking(hass, entry_id, ALEX, None)))

    talk.close(hass, entry_id, ALEX)

    assert feed_target(hass, jeeves.entry, URL) is None


async def test_a_reply_stops_when_the_talk_is_closed(hass, jeeves, live):
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")
    entry_id = jeeves.entry.entry_id
    turn_started(hass, entry_id, talk.asking(hass, entry_id, ALEX, None))
    target = feed_target(hass, jeeves.entry, URL)
    assert target is not None
    feed = ReplyFeed(hass, target, "mp3", {})
    assert feed._session_changed() is False

    talk.close(hass, entry_id, ALEX)

    assert feed._session_changed() is True


# The pipeline the page talks through


def pipeline(name: str, agent: str, voice: str, stt: str | None = "stt.cloud") -> Any:
    return SimpleNamespace(
        id=name, name=name, conversation_engine=agent, tts_engine=voice, stt_engine=stt
    )


def entity_of(hass: HomeAssistant, entry: Any, domain: str) -> str:
    registry = er.async_get(hass)
    return next(
        item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.domain == domain
    )


async def test_the_pipeline_is_the_one_with_this_assistant_and_its_avatar_voice(hass, jeeves):
    agent = entity_of(hass, jeeves.entry, "conversation")
    voice = entity_of(hass, jeeves.entry, "tts")
    offered = [
        pipeline("other agent", "conversation.other", voice),
        pipeline("plain voice", agent, "tts.cloud_voice"),
        pipeline("no ears", agent, voice, stt=None),
        pipeline("jeeves", agent, voice),
    ]
    with patch.object(talk, "async_get_pipelines", return_value=offered):
        assert talk.pipeline_for(hass, jeeves.entry) == "jeeves"


async def test_no_pipeline_that_speaks_through_the_avatar_is_none(hass, jeeves):
    agent = entity_of(hass, jeeves.entry, "conversation")
    with patch.object(
        talk, "async_get_pipelines", return_value=[pipeline("plain", agent, "tts.cloud_voice")]
    ):
        assert talk.pipeline_for(hass, jeeves.entry) is None


# The page's commands


@pytest.fixture
async def page(hass: HomeAssistant, hass_ws_client: Any, jeeves: Any) -> Any:
    with patch.object(talk, "pipeline_for", return_value="jeeves-pipeline"):
        yield await hass_ws_client(hass)


async def call(client: Any, command: str, **data: Any) -> dict[str, Any]:
    await client.send_json_auto_id({"type": f"{PREFIX}/{command}", **data})
    return await client.receive_json()  # type: ignore[no-any-return]


async def test_the_page_opens_a_talk_and_is_told_the_pipeline(hass, page, jeeves):
    answer = await call(page, "open", entry_id=jeeves.entry.entry_id, sdp="offer-sdp")

    assert answer["result"] == {"ok": True, "sdp": "answer-jeeves", "pipeline": "jeeves-pipeline"}
    assert talk.sessions(hass) == frozenset({"session-jeeves"})


async def test_the_page_is_told_in_plain_words_why_not(hass, page, jeeves):
    answer = await call(page, "open", entry_id=jeeves.ada.entry_id, sdp="offer-sdp")

    assert answer["result"]["ok"] is False
    assert "avatar" in answer["result"]["errors"]["base"]


async def test_without_a_pipeline_nothing_is_opened(hass, page, jeeves):
    with patch.object(talk, "pipeline_for", return_value=None):
        answer = await call(page, "open", entry_id=jeeves.entry.entry_id, sdp="offer-sdp")

    assert answer["result"]["ok"] is False
    assert "pipeline" in answer["result"]["errors"]["base"]
    assert jeeves.server.offers == []


async def test_the_page_closes_its_talk(hass, page, jeeves):
    await call(page, "open", entry_id=jeeves.entry.entry_id, sdp="offer-sdp")

    answer = await call(page, "close", entry_id=jeeves.entry.entry_id)

    assert answer["result"] == {"ok": True}
    assert talk.sessions(hass) == frozenset()


async def test_a_page_that_goes_away_closes_its_talk(hass, page, jeeves):
    await call(page, "open", entry_id=jeeves.entry.entry_id, sdp="offer-sdp")

    await page.close()
    await hass.async_block_till_done()

    assert talk.sessions(hass) == frozenset()


async def test_talking_is_for_administrators(
    hass, hass_ws_client, hass_read_only_access_token, jeeves
):
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)

    answer = await call(client, "open", entry_id=jeeves.entry.entry_id, sdp="offer-sdp")

    assert answer["success"] is False
    assert answer["error"]["code"] == "unauthorized"
    assert jeeves.server.offers == []


async def test_unloading_the_assistant_closes_its_talk(hass, jeeves):
    await talk.async_open(hass, jeeves.entry, ALEX, "offer-sdp")

    assert await hass.config_entries.async_unload(jeeves.entry.entry_id)
    await hass.async_block_till_done()

    assert talk.sessions(hass) == frozenset()
    assert avatar_stream.asking_satellite(hass, jeeves.entry.entry_id) is None


async def test_the_talk_script_is_served(hass, hass_client, jeeves):
    client = await hass_client()

    response = await client.get("/voice_assistant_manager/talk.js?v=1")

    assert response.status == 200
    assert 'customElements.define("vam-talk"' in await response.text()


# How far the upright picture is zoomed in, per assistant

PANEL = "voice_assistant_manager/panel"
STORE = "voice_assistant_manager.panel"


async def zoom_shown(client: Any, entry_id: str) -> Any:
    await client.send_json_auto_id({"type": f"{PANEL}/get"})
    routers = (await client.receive_json())["result"]["routers"]
    return next(r for r in routers if r["entry_id"] == entry_id)["talk_zoom"]


async def test_an_assistant_starts_halfway_zoomed_in(hass, page, jeeves):
    assert await zoom_shown(page, jeeves.entry.entry_id) == panel.DEFAULT_TALK_ZOOM == 50


async def test_the_zoom_is_saved_for_that_assistant_only(hass, page, jeeves, hass_storage):
    answer = await call(page, "set_zoom", entry_id=jeeves.entry.entry_id, zoom=20)

    assert answer["result"] == {"ok": True, "zoom": 20}
    assert await zoom_shown(page, jeeves.entry.entry_id) == 20
    assert await zoom_shown(page, jeeves.ada.entry_id) == 50
    assert hass_storage[STORE]["data"]["talk_zoom"] == {jeeves.entry.entry_id: 20}


async def test_saving_the_zoom_leaves_an_open_talk_alone(hass, page, jeeves):
    await call(page, "open", entry_id=jeeves.entry.entry_id, sdp="offer-sdp")

    await call(page, "set_zoom", entry_id=jeeves.entry.entry_id, zoom=0)
    await hass.async_block_till_done()

    assert talk.sessions(hass) == frozenset({"session-jeeves"})


async def test_back_at_the_usual_zoom_nothing_is_kept(hass, page, jeeves, hass_storage):
    await call(page, "set_zoom", entry_id=jeeves.entry.entry_id, zoom=80)

    await call(page, "set_zoom", entry_id=jeeves.entry.entry_id, zoom=50)

    assert "talk_zoom" not in hass_storage[STORE]["data"]


@pytest.mark.parametrize("zoom", [-1, 101, 12.5, "wide", True])
async def test_a_zoom_that_is_no_percentage_is_refused(hass, page, jeeves, zoom):
    answer = await call(page, "set_zoom", entry_id=jeeves.entry.entry_id, zoom=zoom)

    assert answer["success"] is False
    assert await zoom_shown(page, jeeves.entry.entry_id) == 50


async def test_the_zoom_of_an_assistant_that_is_not_there_is_refused(hass, page, jeeves):
    answer = await call(page, "set_zoom", entry_id="nobody", zoom=10)

    assert answer["success"] is False
    assert answer["error"]["code"] == "not_found"


async def test_setting_the_zoom_is_for_administrators(
    hass, hass_ws_client, hass_read_only_access_token, jeeves
):
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)

    answer = await call(client, "set_zoom", entry_id=jeeves.entry.entry_id, zoom=10)

    assert answer["success"] is False
    assert answer["error"]["code"] == "unauthorized"


def test_only_whole_percentages_are_read_back_from_the_store():
    stored = {"talk_zoom": {"a": 0, "b": 100, "c": 101, "d": "30", "e": True, "f": 12.5}}

    assert panel.stored_talk_zoom(stored) == {"a": 0, "b": 100}
    assert panel.stored_talk_zoom({"talk_zoom": [1, 2]}) == {}
    assert panel.stored_talk_zoom(None) == {}


def test_the_talk_page_sizes_the_upright_picture_by_the_zoom():
    script = (panel.TALK_SCRIPT_FILE).read_text(encoding="utf-8")

    assert "--zoom" in script
    assert "object-fit: cover" in script
