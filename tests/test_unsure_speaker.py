# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A voice match that never arrives, and unrecognised voices while a chat show plays."""

import logging
import time
from datetime import timedelta

from homeassistant.components import conversation
from homeassistant.core import Context

from custom_components.voice_assistant_manager import chat_show
from custom_components.voice_assistant_manager.conversation import UNSURE_LINE

from .router_helpers import MAIN_AGENT, FakeAgents, add_router, options_with, router_entity_id
from .test_chat_show import chat_audio, gate, show, start  # noqa: F401 - fixtures
from .test_speaker_follow_up import add_satellite, match

REFUSAL = "The answer is no."


def helper_since(hass, device_id: str, value: str, seconds_ago: float) -> None:
    hass.states.async_set(
        f"input_text.voice_speaker_{device_id}", value, timestamp=time.time() - seconds_ago
    )


async def ask_on(hass, entry, device_id: str, text: str = "What time is it?") -> str:
    entity_id = router_entity_id(hass, entry)
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(entity_id)
    result = await entity.async_process(
        conversation.ConversationInput(
            text=text, context=Context(), conversation_id=None, device_id=device_id,
            satellite_id=None, language="en", agent_id=entity_id,
        )
    )
    return str((result.response.speech.get("plain") or {}).get("speech") or "")


# The voice match never arrived


async def test_regression_a_match_that_never_arrived_is_refused_as_not_told_apart(hass):
    # Seen in the house: the satellite's sensors were unavailable, the helper stayed on
    # pending, and a known person was told "you are entirely unknown to me".
    entry = await add_router(hass, options_with(advanced={"match_within": 6}))
    kitchen = add_satellite(hass)
    helper_since(hass, kitchen, "pending", 7)
    agents = FakeAgents()

    with agents.patched():
        said = await ask_on(hass, entry, kitchen)

    assert said == UNSURE_LINE
    assert said != REFUSAL
    assert agents.calls_to(MAIN_AGENT) == []


async def test_a_voice_that_was_matched_as_unknown_still_gets_the_refusal(hass):
    entry = await add_router(hass)
    kitchen = add_satellite(hass)
    match(hass, kitchen, "stranger")
    agents = FakeAgents()

    with agents.patched():
        said = await ask_on(hass, entry, kitchen)

    assert said == REFUSAL


async def test_a_helper_with_a_value_that_is_no_verdict_is_not_told_apart_either(hass):
    entry = await add_router(hass)
    kitchen = add_satellite(hass)
    match(hass, kitchen, "somebody")
    agents = FakeAgents()

    with agents.patched():
        said = await ask_on(hass, entry, kitchen)

    assert said == UNSURE_LINE
    assert agents.calls_to(MAIN_AGENT) == []


async def test_a_match_that_never_arrives_is_warned_about_once_per_satellite(hass, caplog):
    entry = await add_router(hass, options_with(advanced={"match_within": 6}))
    kitchen = add_satellite(hass)
    helper_since(hass, kitchen, "pending", 7)
    agents = FakeAgents()

    with agents.patched(), caplog.at_level(logging.WARNING):
        await ask_on(hass, entry, kitchen)
        await ask_on(hass, entry, kitchen)

    warnings = [r for r in caplog.records if "voice match" in r.getMessage()]
    assert len(warnings) == 1


async def test_a_match_that_arrives_again_is_answered_as_that_person(hass):
    entry = await add_router(hass, options_with(advanced={"match_within": 6}))
    kitchen = add_satellite(hass)
    helper_since(hass, kitchen, "pending", 7)
    agents = FakeAgents(main_reply="Half past four.")
    with agents.patched():
        await ask_on(hass, entry, kitchen)
        match(hass, kitchen, "owner")

        said = await ask_on(hass, entry, kitchen)

    assert said == "Half past four."


# While a chat show plays on another speaker


async def chat_playing(hass, show):
    playing, release = gate(show, on_call=1)
    await start(show)
    await playing.wait()
    return release


async def end_chat(hass, show, release) -> None:
    await show.manager.async_stop()
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_an_unknown_voice_is_met_with_silence_while_a_chat_plays_elsewhere(hass, show):
    office = add_satellite(hass, "Office satellite")
    match(hass, office, "stranger")
    release = await chat_playing(hass, show)

    said = await ask_on(hass, show.jeeves, office, "Sylvia, what do you think?")

    assert said == ""
    await end_chat(hass, show, release)


async def test_a_voice_that_cannot_be_told_apart_is_met_with_silence_during_a_chat(hass, show):
    office = add_satellite(hass, "Office satellite")
    helper_since(hass, office, "pending", 60)
    release = await chat_playing(hass, show)

    said = await ask_on(hass, show.jeeves, office)

    assert said == ""
    await end_chat(hass, show, release)


async def test_a_known_person_is_still_answered_while_a_chat_plays_elsewhere(hass, show):
    office = add_satellite(hass, "Office satellite")
    match(hass, office, "owner")
    release = await chat_playing(hass, show)
    show.agents.main_reply = "Half past four."

    said = await ask_on(hass, show.jeeves, office)

    assert said == "Half past four."
    await end_chat(hass, show, release)


async def test_an_unknown_voice_gets_the_refusal_again_once_the_chat_is_over(hass, show, freezer):
    office = add_satellite(hass, "Office satellite")
    match(hass, office, "stranger")
    release = await chat_playing(hass, show)
    await end_chat(hass, show, release)
    freezer.tick(timedelta(seconds=chat_show.REPLY_GRACE + 1))

    said = await ask_on(hass, show.jeeves, office)

    assert said == REFUSAL


async def test_without_a_chat_an_unknown_voice_gets_the_refusal(hass):
    entry = await add_router(hass)
    office = add_satellite(hass, "Office satellite")
    match(hass, office, "stranger")
    agents = FakeAgents()

    with agents.patched():
        said = await ask_on(hass, entry, office)

    assert said == REFUSAL
