# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the smaller hardening items (S14) of the 2026-11-27 review."""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import avatar, favorites, wake_handoff
from custom_components.voice_assistant_manager.avatar import LiveTalkingClient, LiveTalkingError
from custom_components.voice_assistant_manager.avatar_stream import decoder_arguments
from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.favorites import FavoriteError

from .router_helpers import MAIN_AGENT
from .test_setup_guide import without_native_audio_libraries  # noqa: F401 - fixture
from .test_wake_handoff import SECOND, hand, home, request, woke  # noqa: F401 - fixture

BASE = "http://livetalking.local:8010"


# ffmpeg reads only what it is piped


@pytest.mark.parametrize("extension", ["mp3", "wav", "opus", "pcm"])
def test_the_decoder_may_only_read_its_pipe(extension):
    """Audio whose format ffmpeg works out by itself could be a playlist that names a
    file or an address. With only the pipe allowed, ffmpeg opens nothing else."""
    arguments = decoder_arguments(extension, {"preferred_sample_rate": 16000})

    at = arguments.index("-protocol_whitelist")
    assert arguments[at + 1] == "pipe"
    assert at < arguments.index("-i")


# LiveTalking's answers


async def test_a_livetalking_answer_larger_than_any_real_one_is_refused(hass, aioclient_mock):
    aioclient_mock.post(f"{BASE}/is_speaking", text="x" * (avatar.MAX_REPLY_BYTES + 1))
    client = LiveTalkingClient(async_get_clientsession(hass), BASE)

    with pytest.raises(LiveTalkingError):
        await client.async_speaking("sess-1")


async def test_livetalking_is_never_followed_to_another_address(hass):
    seen: list[dict] = []

    class Session:
        def post(self, url, **kwargs):
            seen.append(kwargs)
            raise LiveTalkingError("stop here")

        get = post

    client = LiveTalkingClient(Session(), BASE)  # type: ignore[arg-type]
    for call in (
        client.async_speaking("s"),
        client._async_post("/interrupt_talk", json={"sessionid": "s"}),
        client._async_sessions(),
    ):
        with pytest.raises(LiveTalkingError):
            await call

    assert [kwargs.get("allow_redirects") for kwargs in seen] == [False, False, False]


# Favorites


def test_too_many_other_names_are_refused_before_any_is_looked_at():
    names = ["x" * 10_000] * (favorites.MAX_ALIASES + 1)

    with (
        patch.object(favorites, "_clean_name") as clean,
        pytest.raises(FavoriteError) as refused,
    ):
        favorites._clean_aliases(names, "Radio 2")

    assert refused.value.key == "too_many_aliases"
    clean.assert_not_called()


# A typed request is no wake word


async def test_a_typed_request_naming_a_satellite_is_not_handed_over_and_leaves_no_trace(
    hass: HomeAssistant, home  # noqa: F811
):
    woke(hass, SECOND)
    typed = request(home)
    typed.context = Context(user_id="u-alex")

    result = await hand(hass, home.jeeves, typed)

    assert result is None
    assert home.device not in wake_handoff._state(hass).last_request
    # The satellite's own next turn is still handed to the assistant it woke.
    spoken = await hand(hass, home.jeeves, request(home))
    assert spoken.response.speech["plain"]["speech"] == "I am Ada."


# Setting an assistant up


async def test_when_the_calendar_watcher_cannot_start_the_assistant_is_not_half_loaded(
    hass: HomeAssistant,
):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(
        domain=DOMAIN, title="Kitchen assistant", data={"name": "Kitchen", "agent": MAIN_AGENT}
    )
    entry.add_to_hass(hass)

    with patch(f"custom_components.{DOMAIN}.async_start_watcher", side_effect=OSError("disk")):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    # Only Home Assistant's own agent: the assistant's was not set up.
    assert hass.states.async_entity_ids("conversation") == ["conversation.home_assistant"]
    assert entry.entry_id not in hass.data[DOMAIN]["loaded_entries"]


# Found by the review of these fixes


class _InPieces:
    """An answer whose body arrives in pieces, as one larger than a network packet does."""

    def __init__(self, *pieces: bytes) -> None:
        self._pieces = list(pieces)
        self.content = self

    async def read(self, n: int = -1) -> bytes:
        return self._pieces.pop(0) if self._pieces else b""


async def test_regression_s14_a_livetalking_answer_that_arrives_in_pieces_is_read_whole():
    response = _InPieces(b'{"sessions": [', b'"a", "b"', b"]}")

    payload = await avatar._async_json(response, "sessions")  # type: ignore[arg-type]

    assert payload == {"sessions": ["a", "b"]}


async def test_regression_s14_an_answer_in_pieces_is_refused_once_it_is_too_large():
    piece = b"x" * (avatar.MAX_REPLY_BYTES // 2 + 1)
    response = _InPieces(piece, piece, piece)

    with pytest.raises(LiveTalkingError, match="larger"):
        await avatar._async_json(response, "sessions")  # type: ignore[arg-type]

    assert response._pieces == [piece]  # it stopped reading
