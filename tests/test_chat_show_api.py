# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Starting and stopping a chat from the settings page and from actions."""

# Fixtures are imported from the neighbouring test modules and used by name.
# ruff: noqa: F401, F811

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import chat_show
from custom_components.voice_assistant_manager.chat_show import ChatLimits
from custom_components.voice_assistant_manager.const import DOMAIN

from .router_helpers import MAIN_AGENT, add_router, options_with
from .router_helpers import router_entity_id as registered_router_id
from .test_chat_show import (
    ADA_LINE,
    JEEVES_LINE,
    PLAYER,
    SATELLITE,
    add_speaker,
    gate,
    run_chat,
    show,
)
from .test_panel import Page, page
from .test_setup_guide import (
    add_satellite,
    create_pipeline,
    without_native_audio_libraries,
)

FAST = {"max_lines": 4, "pause": 0}
HALL = "assist_satellite.hall_satellite"
PLAY_MEDIA = int(MediaPlayerEntityFeature.PLAY_MEDIA)


def start_args(show: SimpleNamespace, **overrides: Any) -> dict[str, Any]:
    return {
        "a": show.jeeves.entry_id,
        "b": show.ada.entry_id,
        "speaker": SATELLITE,
        "limits": FAST,
        **overrides,
    }


async def finish(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def refusal(page: Page, command: str, **data: Any) -> str:
    answer = await page.ok(command, **data)
    assert answer["ok"] is False, answer
    return str(answer["errors"]["base"])


# Who may use it.


@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("chat/start", {"a": "x", "b": "y", "speaker": SATELLITE}),
        ("chat/stop", {}),
        ("chat/state", {}),
        ("chat/save_defaults", {"limits": {"max_lines": 5}}),
    ],
)
async def test_every_chat_command_is_for_admins_only(
    hass, hass_ws_client, hass_read_only_access_token, show, command, data
):
    reader = Page(await hass_ws_client(hass, hass_read_only_access_token))

    answer = await reader.call(command, **data)

    assert answer["success"] is False
    assert answer["error"]["code"] == "unauthorized"
    assert show.manager.state()["running"] is False


# Starting.


async def test_start_runs_a_chat_that_the_state_then_shows(hass, page, show):
    result = await page.ok("chat/start", **start_args(show, topic="cheese"))
    await finish(hass)

    state = (await page.ok("chat/state"))["chat"]
    assert result == {"ok": True}
    assert state["topic"] == "cheese"
    assert state["assistants"] == ["Jeeves", "Ada"]
    assert [line["text"] for line in state["lines"]] == [JEEVES_LINE, ADA_LINE] * 2
    assert state["ended"] == "chat_done"


async def test_the_topic_is_optional(hass, page, show):
    assert (await page.ok("chat/start", **start_args(show)))["ok"] is True
    await finish(hass)


async def test_the_same_assistant_twice_is_refused_in_plain_words(hass, page, show):
    text = await refusal(page, "chat/start", **start_args(show, b=show.jeeves.entry_id))

    assert "different" in text.lower()
    assert "chat_same_assistant" not in text
    assert show.manager.state()["running"] is False


async def test_an_unknown_assistant_is_refused(hass, page, show):
    text = await refusal(page, "chat/start", **start_args(show, b="nope"))

    assert "chat_no_router" not in text
    assert "assistant" in text.lower()


async def test_an_assistant_without_a_safe_writer_is_refused(hass, page, show):
    bare = await add_router(hass, options_with(), title="Sam")

    text = await refusal(page, "chat/start", **start_args(show, b=bare.entry_id))

    assert "chat_no_writer" not in text
    assert "announcement" in text.lower()


@pytest.mark.parametrize("speaker", ["light.kitchen", "media_player.nowhere", "sensor.x"])
async def test_a_speaker_that_cannot_speak_is_refused(hass, page, show, speaker):
    hass.states.async_set("light.kitchen", "on", {"supported_features": 0})
    hass.states.async_set("sensor.x", "1", {"supported_features": 0})

    text = await refusal(page, "chat/start", **start_args(show, speaker=speaker))

    assert "chat_bad_speaker" not in text
    assert "speaker" in text.lower()


async def test_a_media_player_that_cannot_play_media_is_refused(hass, page, show):
    add_speaker(hass, PLAYER, "idle", 0)

    text = await refusal(page, "chat/start", **start_args(show, speaker=PLAYER))

    assert "speaker" in text.lower()


async def test_a_media_player_that_can_play_media_is_accepted(hass, page, show):
    add_speaker(hass, PLAYER, "idle", PLAY_MEDIA)

    result = await page.ok("chat/start", **start_args(show, speaker=PLAYER))
    await finish(hass)

    assert result["ok"] is True


async def test_a_second_start_while_one_runs_is_refused(hass, page, show):
    playing, release = gate(show)
    await page.ok("chat/start", **start_args(show))
    await playing.wait()

    text = await refusal(page, "chat/start", **start_args(show))
    release.set()
    await finish(hass)

    assert "already" in text.lower()
    assert len(show.speakers.announced) == 4


async def test_limits_outside_their_ranges_are_clamped(hass, page, show):
    await page.ok("chat/start", **start_args(show, limits={"max_lines": 1000, "pause": 0}))
    await finish(hass)

    assert len(show.manager.state()["lines"]) == 50


async def test_limits_that_are_not_numbers_are_refused(hass, page, show):
    text = await refusal(page, "chat/start", **start_args(show, limits={"max_lines": "lots"}))

    assert "limit" in text.lower()
    assert show.manager.state()["running"] is False


async def test_unknown_limits_are_refused(hass, page, show):
    text = await refusal(page, "chat/start", **start_args(show, limits={"volume": 11}))

    assert "limit" in text.lower()


# Stopping and state.


async def test_stop_ends_a_running_chat_with_the_reason_stopped(hass, page, show):
    playing, release = gate(show)
    await page.ok("chat/start", **start_args(show))
    await playing.wait()

    stopping = hass.async_create_task(page.ok("chat/stop"))
    release.set()
    await stopping
    await finish(hass)

    state = (await page.ok("chat/state"))["chat"]
    assert state["running"] is False
    assert state["ended"] == "chat_stopped"


async def test_stop_without_a_chat_is_not_an_error(hass, page, show):
    assert (await page.ok("chat/stop"))["ok"] is True


async def test_state_while_running_shows_the_chat_so_far(hass, page, show):
    playing, release = gate(show)
    await page.ok("chat/start", **start_args(show))
    await playing.wait()

    state = (await page.ok("chat/state"))["chat"]
    release.set()
    await finish(hass)

    assert state["running"] is True
    assert state["speaker"] == SATELLITE
    assert state["ended"] == ""


async def test_state_before_any_chat_is_empty(hass, page, show):
    state = (await page.ok("chat/state"))["chat"]

    assert state["running"] is False
    assert state["lines"] == []


async def test_state_lists_the_routers_with_their_writer_and_character(hass, page, show):
    bare = await add_router(hass, options_with(), title="Sam")

    routers = {r["title"]: r for r in (await page.ok("chat/state"))["routers"]}

    assert routers["Jeeves"] == {
        "entry_id": show.jeeves.entry_id,
        "title": "Jeeves",
        "has_writer": True,
        "has_character": True,
    }
    assert routers["Ada"]["has_writer"] is True
    assert routers["Ada"]["has_character"] is False
    assert routers["Sam"]["has_writer"] is False
    assert routers["Sam"]["entry_id"] == bare.entry_id


async def test_state_lists_speakers_that_can_speak_and_whether_voice_can_join(
    without_native_audio_libraries, hass, hass_ws_client, page, show
):
    add_speaker(hass, PLAYER, "idle", PLAY_MEDIA)
    add_speaker(hass, "media_player.tv", "idle", 0)
    assert await async_setup_component(hass, "assist_pipeline", {})
    await hass.async_block_till_done()
    ws = await hass_ws_client(hass)
    await create_pipeline(ws, "Jeeves", registered_router_id(hass, show.jeeves))
    add_satellite(hass, show.jeeves, selects=(("pipeline", "Jeeves"),), name="Hall satellite")
    hass.states.async_set(HALL, "idle", {"supported_features": 3})

    speakers = {s["entity_id"]: s for s in (await page.ok("chat/state"))["speakers"]}

    assert set(speakers) == {SATELLITE, HALL, PLAYER}
    assert speakers[HALL]["voice"] is True
    assert speakers[SATELLITE]["voice"] is False
    assert speakers[PLAYER]["voice"] is False
    # Only a satellite that can start a conversation can listen after a round.
    assert speakers[HALL]["listen"] is True
    assert speakers[SATELLITE]["listen"] is False
    assert speakers[PLAYER]["listen"] is False


async def hall_satellite(hass: HomeAssistant, hass_ws_client, engine: str) -> None:
    """The Hall satellite, able to start a conversation, on a pipeline to `engine`."""
    assert await async_setup_component(hass, "assist_pipeline", {})
    await hass.async_block_till_done()
    ws = await hass_ws_client(hass)
    owner = MockConfigEntry(domain="test")
    owner.add_to_hass(hass)
    await create_pipeline(ws, "Hall", engine)
    add_satellite(hass, owner, selects=(("pipeline", "Hall"),), name="Hall satellite")
    hass.states.async_set(HALL, "idle", {"supported_features": 3})


async def speakers_now(page: Page) -> dict[str, dict[str, Any]]:
    return {s["entity_id"]: s for s in (await page.ok("chat/state"))["speakers"]}


async def test_regression_chat_no_listening_offered_where_the_pipeline_uses_no_router(
    without_native_audio_libraries, hass, hass_ws_client, page, show
):
    await hall_satellite(hass, hass_ws_client, MAIN_AGENT)

    speakers = await speakers_now(page)

    assert speakers[HALL]["voice"] is False
    assert speakers[HALL]["listen"] is False


async def test_regression_chat_an_unloaded_router_gives_no_voice_and_no_listening(
    without_native_audio_libraries, hass, hass_ws_client, page, show
):
    await hall_satellite(hass, hass_ws_client, registered_router_id(hass, show.jeeves))
    assert (await speakers_now(page))[HALL]["listen"] is True

    assert await hass.config_entries.async_unload(show.jeeves.entry_id)
    await hass.async_block_till_done()
    speakers = await speakers_now(page)

    assert speakers[HALL]["voice"] is False
    assert speakers[HALL]["listen"] is False


@pytest.mark.parametrize(
    ("through_router", "order"),
    [
        (True, ["announce", "start_conversation", "announce"]),
        (False, ["announce", "announce", "announce"]),
    ],
    ids=["pipeline-to-a-router", "pipeline-to-the-main-agent"],
)
async def test_regression_chat_a_chat_listens_only_where_a_router_hears_the_reply(
    without_native_audio_libraries, hass, hass_ws_client, show, through_router, order
):
    engine = registered_router_id(hass, show.ada) if through_router else MAIN_AGENT
    await hall_satellite(hass, hass_ws_client, engine)

    with patch.object(chat_show, "BUSY_POLL", 0.01):
        await run_chat(
            hass, show, speaker=HALL, limits=ChatLimits(max_lines=3, pause=0, listen=True)
        )

    assert show.speakers.order == order


# Defaults.


async def test_defaults_start_as_the_spec_values(hass, page, show):
    defaults = (await page.ok("chat/state"))["defaults"]

    assert defaults == {"max_lines": 12, "words": 30, "pause": 2.0, "minutes": 5, "listen": False}


async def test_saved_defaults_are_clamped_and_shown_again(hass, page, show):
    result = await page.ok("chat/save_defaults", limits={"max_lines": 500, "words": 20})

    assert result["ok"] is True
    assert result["defaults"]["max_lines"] == 50
    assert (await page.ok("chat/state"))["defaults"]["words"] == 20


async def test_defaults_with_junk_are_refused_and_not_saved(hass, page, show):
    text = await refusal(page, "chat/save_defaults", limits={"words": "many"})

    assert "limit" in text.lower()
    assert (await page.ok("chat/state"))["defaults"]["words"] == 30


async def test_defaults_are_written_to_the_store(hass, page, show, hass_storage):
    await page.ok("chat/save_defaults", limits={"max_lines": 7, "listen": True})

    stored = hass_storage[f"{DOMAIN}.panel"]["data"]["chat_defaults"]
    assert stored["max_lines"] == 7
    assert stored["listen"] is True


async def test_stored_defaults_are_used_at_the_start(hass, hass_storage, hass_ws_client):
    hass_storage[f"{DOMAIN}.panel"] = {
        "version": 1,
        "data": {"sidebar": True, "guide": {}, "chat_defaults": {"max_lines": 7}},
    }
    await add_router(hass)
    page = Page(await hass_ws_client(hass))

    assert (await page.ok("chat/state"))["defaults"]["max_lines"] == 7


async def test_a_store_without_defaults_still_loads(hass, hass_storage, hass_ws_client):
    hass_storage[f"{DOMAIN}.panel"] = {"version": 1, "data": {"sidebar": False}}
    await add_router(hass)
    page = Page(await hass_ws_client(hass))

    assert (await page.ok("chat/state"))["defaults"]["max_lines"] == 12


async def test_a_damaged_stored_default_falls_back_to_the_spec_values(
    hass, hass_storage, hass_ws_client
):
    hass_storage[f"{DOMAIN}.panel"] = {
        "version": 1,
        "data": {"sidebar": True, "chat_defaults": {"max_lines": "lots"}},
    }
    await add_router(hass)
    page = Page(await hass_ws_client(hass))

    assert (await page.ok("chat/state"))["defaults"]["max_lines"] == 12


async def test_a_start_without_limits_uses_the_saved_defaults(hass, page, show):
    await page.ok("chat/save_defaults", limits={"max_lines": 2, "pause": 0})

    args = start_args(show)
    del args["limits"]
    await page.ok("chat/start", **args)
    await finish(hass)

    assert len(show.manager.state()["lines"]) == 2


# The actions.


def action_data(show: SimpleNamespace, **overrides: Any) -> dict[str, Any]:
    return {
        "a": show.jeeves.entry_id,
        "b": show.ada.entry_id,
        "speaker": SATELLITE,
        "max_lines": 2,
        "pause": 0,
        **overrides,
    }


async def test_the_start_chat_action_starts_a_chat_and_stop_chat_ends_it(hass, show):
    playing, release = gate(show)
    await hass.services.async_call(DOMAIN, "start_chat", action_data(show), blocking=True)
    await playing.wait()
    assert show.manager.state()["running"] is True

    stopping = hass.async_create_task(
        hass.services.async_call(DOMAIN, "stop_chat", {}, blocking=True)
    )
    release.set()
    await stopping
    await finish(hass)

    assert show.manager.state()["ended"] == "chat_stopped"


async def test_the_start_chat_action_takes_a_topic_and_the_limits(hass, show):
    await hass.services.async_call(
        DOMAIN, "start_chat", action_data(show, topic="tea", words=7), blocking=True
    )
    await finish(hass)

    state = show.manager.state()
    assert state["topic"] == "tea"
    assert len(state["lines"]) == 2


async def test_the_start_chat_action_refuses_in_plain_words(hass, show):
    with pytest.raises(ServiceValidationError, match="different"):
        await hass.services.async_call(
            DOMAIN, "start_chat", action_data(show, b=show.jeeves.entry_id), blocking=True
        )


async def test_the_start_chat_action_refuses_junk_limits(hass, show):
    with pytest.raises(ServiceValidationError, match="limit"):
        await hass.services.async_call(
            DOMAIN, "start_chat", action_data(show, max_lines="lots"), blocking=True
        )


@pytest.mark.parametrize("service", ["start_chat", "stop_chat"])
async def test_the_actions_are_refused_for_a_user_who_is_not_an_admin(
    hass, show, hass_read_only_user, service
):
    data = action_data(show) if service == "start_chat" else {}

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            service,
            data,
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )

    assert show.manager.state()["running"] is False


async def test_the_actions_work_for_an_admin_user(hass, show, hass_admin_user):
    await hass.services.async_call(
        DOMAIN,
        "start_chat",
        action_data(show),
        blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )
    await finish(hass)

    assert show.manager.state()["ended"] == "chat_done"
