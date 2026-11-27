# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The chat show on My assistant: a person who is not an administrator starts and stops one."""

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry, MockUser

from custom_components.voice_assistant_manager import panel_api, stage
from custom_components.voice_assistant_manager.chat_show import ChatLimits

from .router_helpers import add_router
from .test_avatar_tts import engines, sent  # noqa: F401 - fixtures
from .test_chat_show import (  # noqa: F401 - fixtures
    ADA_WRITER,
    SATELLITE,
    chat_audio,
    gate,
    router_options,
    show,
)
from .test_mine_api import Mine, mine  # noqa: F401 - fixtures
from .test_stage_page import HERE, VIEWER, FakePage, page  # noqa: F401 - fixtures

STAGE = "voice_assistant_manager/stage"
NOT_YOURS = "That is not one of your assistants."
NOT_LINKED = "The chat show is for the people of an assistant."


async def link(hass: HomeAssistant, entry: MockConfigEntry, user: MockUser) -> None:
    """Make the user Person 1 of the router."""
    options = {**entry.options, "owner": {**entry.options.get("owner", {}), "user": user.id}}
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()


@pytest.fixture
async def linked(hass: HomeAssistant, show: Any, hass_read_only_user: MockUser) -> None:
    """The person is in both assistants of the show."""
    await link(hass, show.jeeves, hass_read_only_user)
    await link(hass, show.ada, hass_read_only_user)


@pytest.fixture(autouse=True)
def short_chats() -> Iterator[None]:
    """The saved limits, which a chat starts with unless the person changes them."""
    with patch.object(panel_api, "chat_defaults", return_value=ChatLimits(max_lines=2, pause=0)):
        yield


def start(show: Any, **changes: Any) -> dict[str, Any]:
    return {"a": show.jeeves.entry_id, "b": show.ada.entry_id, "speaker": SATELLITE, **changes}


async def test_a_person_is_offered_their_own_assistants_and_the_speakers(
    hass: HomeAssistant, mine: Mine, show: Any, linked: None
) -> None:
    await add_router(hass, router_options(ADA_WRITER), title="Someone else's")

    state = await mine.ok("chat/state")

    assert sorted(router["title"] for router in state["routers"]) == ["Ada", "Jeeves"]
    assert [speaker["entity_id"] for speaker in state["speakers"]] == [SATELLITE]
    assert state["chat"]["running"] is False
    assert state["stage_script"].startswith(stage.SCRIPT_URL)


async def test_a_person_starts_a_chat_which_runs_with_the_saved_limits(
    hass: HomeAssistant, mine: Mine, show: Any, linked: None
) -> None:
    assert await mine.ok("chat/start", **start(show)) == {"ok": True}
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(show.speakers.announced) == 2
    assert (await mine.ok("chat/state"))["chat"]["ended"] == "chat_done"


async def test_a_person_sets_the_limits_of_their_chat(
    hass: HomeAssistant, mine: Mine, show: Any, linked: None
) -> None:
    state = await mine.ok("chat/state")
    assert state["defaults"]["max_lines"] == 2

    await mine.ok("chat/start", **start(show), limits={"max_lines": 3})
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(show.speakers.announced) == 3


async def test_limits_out_of_range_are_brought_into_it(
    hass: HomeAssistant, mine: Mine, show: Any, linked: None
) -> None:
    playing, release = gate(show)
    await mine.ok("chat/start", **start(show), limits={"max_lines": 5000, "minutes": 5000})
    await playing.wait()

    limits = show.manager._chat.limits
    await mine.ok("chat/stop")
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert (limits.max_lines, limits.minutes) == (50, 30)


async def test_limits_that_are_no_limits_are_refused(
    hass: HomeAssistant, mine: Mine, show: Any, linked: None
) -> None:
    text = await mine.refused("chat/start", **start(show), limits={"volume": 11})

    assert text.startswith("The chat limits are not valid")
    assert show.manager.state()["assistants"] == []


async def test_a_chat_with_someone_elses_assistant_is_refused(
    hass: HomeAssistant, mine: Mine, show: Any, hass_read_only_user: MockUser
) -> None:
    await link(hass, show.jeeves, hass_read_only_user)

    assert await mine.refused("chat/start", **start(show)) == NOT_YOURS
    assert show.manager.state()["assistants"] == []


async def test_a_person_stops_a_chat(
    hass: HomeAssistant, mine: Mine, show: Any, linked: None
) -> None:
    playing, release = gate(show)
    await mine.ok("chat/start", **start(show))
    await playing.wait()

    assert await mine.ok("chat/stop") == {"ok": True}
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert show.manager.state()["ended"] == "chat_stopped"


@pytest.mark.parametrize("command", ["chat/state", "chat/start", "chat/stop"])
async def test_someone_in_no_assistant_gets_nothing(
    hass: HomeAssistant, mine: Mine, show: Any, command: str
) -> None:
    data = start(show) if command == "chat/start" else {}

    assert (await mine.refused(command, **data)).startswith(NOT_LINKED)
    assert show.manager.state()["assistants"] == []


async def test_a_person_watches_their_own_chat_on_this_device(
    hass: HomeAssistant, mine: Mine, show: Any, page: Any, linked: None
) -> None:
    assert await mine.ok("chat/start", **start(show, speaker=HERE)) == {"ok": True}
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(page.live.said) == 2
    assert show.manager.state()["ended"] == "chat_done"


# The stage card's own commands, which the page's card uses.


async def stage_call(mine: Mine, command: str, **data: Any) -> dict[str, Any]:
    await mine._client.send_json_auto_id({"type": f"{STAGE}/{command}", **data})
    return await mine._client.receive_json()  # type: ignore[no-any-return]


async def test_a_persons_page_may_watch_the_stage_as_itself(
    hass: HomeAssistant, mine: Mine, show: Any, linked: None
) -> None:
    assert (await stage_call(mine, "subscribe", viewer=VIEWER))["success"] is True
    assert stage.get_stage(hass).watched(VIEWER)


async def test_a_person_may_not_watch_the_stage_cast_to_a_device(
    hass: HomeAssistant, mine: Mine, show: Any, linked: None
) -> None:
    answer = await stage_call(mine, "subscribe")

    assert answer["success"] is False
    assert answer["error"]["code"] == "unauthorized"
    assert not stage.get_stage(hass).watched("")


async def test_a_persons_card_may_offer_and_report(
    hass: HomeAssistant, mine: Mine, show: Any, linked: None
) -> None:
    """Refused for the chat it names, which is not on the stage, not for who asks."""
    offer = await stage_call(mine, "offer", token="nope", key="nope", sdp="offer-sdp")
    report = await stage_call(mine, "connected", token="nope", key="nope", up=True)

    assert offer["error"]["code"] == "not_on_stage"
    assert report["error"]["code"] == "not_on_stage"


@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("subscribe", {"viewer": VIEWER}),
        ("offer", {"token": "nope", "key": "nope", "sdp": "offer-sdp"}),
        ("connected", {"token": "nope", "key": "nope", "up": True}),
    ],
)
async def test_someone_in_no_assistant_may_not_use_the_stage(
    hass: HomeAssistant, mine: Mine, show: Any, command: str, data: dict[str, Any]
) -> None:
    answer = await stage_call(mine, command, **data)

    assert answer["success"] is False
    assert answer["error"]["code"] == "unauthorized"


async def test_the_page_showing_a_chat_is_not_named_to_anyone(
    hass: HomeAssistant, mine: Mine, hass_ws_client: Any, show: Any, page: Any, linked: None
) -> None:
    """Its id is what lets a card watch that chat, so no page is told another's."""
    await mine.ok("chat/start", **start(show, speaker=HERE))
    await hass.async_block_till_done(wait_background_tasks=True)

    admin = await hass_ws_client(hass)
    await admin.send_json_auto_id({"type": "voice_assistant_manager/panel/chat/state"})

    assert (await admin.receive_json())["result"]["chat"]["speaker"] == "page:"
    assert (await mine.ok("chat/state"))["chat"]["speaker"] == "page:"
