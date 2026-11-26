# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Emotion tags in the router's replies: never spoken or shown, and told to the avatar."""

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import async_get_chat_log
from homeassistant.core import Context
from homeassistant.helpers.chat_session import async_get_chat_session

from custom_components.voice_assistant_manager import emotion

from .router_helpers import (
    MAIN_AGENT,
    FakeAgents,
    _result,
    add_router,
    ask,
    ask_in_pipeline,
    options_with,
    router_entity_id,
)


def speech(result: conversation.ConversationResult) -> str:
    return str((result.response.speech.get("plain") or {}).get("speech") or "")


@pytest.fixture
def sent():
    """The emotions the router told the avatar, as (entry title, emotion)."""
    calls: list[tuple[str, str]] = []
    with patch.object(
        emotion, "send_emotion", lambda _hass, entry, found: calls.append((entry.title, found))
    ):
        yield calls


class Pipeline:
    """A voice pipeline's chat log listener: records what it would speak and show."""

    def __init__(self, sent: list[tuple[str, str]]) -> None:
        self.deltas: list[dict[str, Any]] = []
        # How many emotions had been sent when each delta arrived.
        self.sent_before: list[int] = []
        self._sent = sent

    def __call__(self, _chat_log, delta: dict[str, Any]) -> None:
        self.deltas.append(delta)
        self.sent_before.append(len(self._sent))

    @property
    def spoken(self) -> str:
        return "".join(str(d.get("content") or "") for d in self.deltas)


def streaming_main_agent(pieces: list[str], final: str | None = None):
    """A main agent that streams its reply into the running chat log, as an LLM does."""

    async def converse(hass, text, conversation_id, context, **kwargs):
        if kwargs.get("agent_id") != MAIN_AGENT:
            return _result("{}")
        user_input = conversation.ConversationInput(
            text=text, context=context, conversation_id=conversation_id, device_id=None,
            satellite_id=None, language="en", agent_id=MAIN_AGENT,
        )

        async def deltas():
            yield {"role": "assistant"}
            for piece in pieces:
                yield {"content": piece}

        with (
            async_get_chat_session(hass, conversation_id, context=context) as session,
            async_get_chat_log(hass, session, user_input) as log,
        ):
            async for _content in log.async_add_delta_content_stream(MAIN_AGENT, deltas()):
                pass
        result = _result("".join(pieces) if final is None else final)
        result.conversation_id = conversation_id
        return result

    return converse


async def ask_streamed(hass, entry, text: str, pipeline: Pipeline):
    """Ask the router inside a chat log that has a pipeline's delta listener."""
    entity_id = router_entity_id(hass, entry)
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(entity_id)
    context = Context()
    with async_get_chat_session(hass, None, context=context) as session:
        user_input = conversation.ConversationInput(
            text=text, context=context, conversation_id=session.conversation_id,
            device_id=None, satellite_id=None, language="en", agent_id=entity_id,
        )
        with async_get_chat_log(
            hass, session, user_input, chat_log_delta_listener=pipeline
        ) as log:
            result = await entity.async_process(user_input)
            # Home Assistant takes the listener off when the turn's chat log closes.
            return result, log.delta_listener, list(log.content)


async def test_a_streamed_reply_reaches_the_pipeline_without_its_tag(hass, sent):
    entry = await add_router(hass)
    pipeline = Pipeline(sent)

    with patch.object(
        conversation, "async_converse",
        streaming_main_agent(["[hap", "py] Hello", " there, it is a long and sunny day."]),
    ):
        result, _listener, _content = await ask_streamed(hass, entry, "Hi", pipeline)

    assert pipeline.spoken == "Hello there, it is a long and sunny day."
    assert speech(result) == "Hello there, it is a long and sunny day."


async def test_a_streamed_reply_tells_the_emotion_before_any_text_is_spoken(hass, sent):
    entry = await add_router(hass)
    pipeline = Pipeline(sent)

    with patch.object(
        conversation, "async_converse", streaming_main_agent(["[sad] ", "Oh no."])
    ):
        await ask_streamed(hass, entry, "Hi", pipeline)

    assert sent == [("Kitchen assistant", "sad")]
    first_text = next(i for i, d in enumerate(pipeline.deltas) if d.get("content"))
    assert pipeline.sent_before[first_text] == 1


async def test_the_pipeline_gets_its_own_listener_back_after_the_turn(hass, sent):
    entry = await add_router(hass)
    pipeline = Pipeline(sent)

    with patch.object(conversation, "async_converse", streaming_main_agent(["[sad] Oh."])):
        _result_, listener, _content = await ask_streamed(hass, entry, "Hi", pipeline)

    assert listener is pipeline


async def test_the_pipeline_gets_its_own_listener_back_when_the_main_agent_fails(hass, sent):
    entry = await add_router(hass)
    pipeline = Pipeline(sent)
    agents = FakeAgents(main_raises=RuntimeError("down"))

    with agents.patched():
        result, listener, _content = await ask_streamed(hass, entry, "Hi", pipeline)

    assert listener is pipeline
    assert speech(result)


async def test_a_reply_that_is_only_a_tag_streams_nothing_and_tells_no_emotion(hass, sent):
    entry = await add_router(hass)
    pipeline = Pipeline(sent)

    with patch.object(conversation, "async_converse", streaming_main_agent(["[happy]"])):
        result, _listener, _content = await ask_streamed(hass, entry, "Hi", pipeline)

    assert pipeline.spoken == ""
    assert speech(result) == ""


async def test_a_reply_has_its_tag_taken_off_and_its_emotion_told(hass, sent):
    entry = await add_router(hass)
    agents = FakeAgents(main_reply="[Sad] I am sorry to hear that.")

    with agents.patched():
        result = await ask(hass, entry, "My day was bad")

    assert speech(result) == "I am sorry to hear that."
    assert sent == [("Kitchen assistant", "sad")]


async def test_the_history_keeps_the_tag(hass, sent):
    entry = await add_router(hass)
    agents = FakeAgents(main_reply="[happy] Great news.")

    with agents.patched():
        result, content = await ask_in_pipeline(hass, entry, "I won")

    assert speech(result) == "Great news."
    assert content[-1].content == "[happy] Great news."


async def test_a_reply_without_a_tag_is_spoken_as_it_is_and_neutral(hass, sent):
    entry = await add_router(hass)
    agents = FakeAgents(main_reply="The lights are on.")

    with agents.patched():
        result = await ask(hass, entry, "Lights?")

    assert speech(result) == "The lights are on."
    assert sent == [("Kitchen assistant", "neutral")]


async def test_an_unknown_tag_is_taken_off_and_neutral(hass, sent):
    entry = await add_router(hass)
    agents = FakeAgents(main_reply="[thrilled] Wonderful.")

    with agents.patched():
        result = await ask(hass, entry, "Hi")

    assert speech(result) == "Wonderful."
    assert sent == [("Kitchen assistant", "neutral")]


async def test_a_reply_with_nothing_to_say_tells_no_emotion(hass, sent):
    entry = await add_router(hass)
    agents = FakeAgents(main_reply="[happy]")

    with agents.patched():
        result = await ask(hass, entry, "Hi")

    assert speech(result) == ""
    assert sent == []


async def test_a_fixed_reply_has_its_tag_taken_off(hass, sent):
    entry = await add_router(
        hass,
        options_with(
            strangers={"mode": "fixed", "fixed_replies": "[angry] Go away."},
            advanced={"no_helper_role": "stranger"},
        ),
    )
    agents = FakeAgents()

    with agents.patched():
        result = await ask(hass, entry, "Open the door")

    assert speech(result) == "Go away."
    assert sent == [("Kitchen assistant", "angry")]


async def test_the_memory_pass_reads_the_reply_without_its_tag(hass, sent):
    entry = await add_router(hass)
    agents = FakeAgents(main_reply="[happy] I will remember that.")

    with agents.patched(), patch(
        "custom_components.voice_assistant_manager.conversation.async_run_extraction"
    ) as extraction:
        await ask(hass, entry, "Remember that I like tea")
        await hass.async_block_till_done()

    (call,) = extraction.call_args_list
    assert call.args[3].reply_text == "I will remember that."


async def test_a_failing_emotion_never_breaks_the_reply(hass):
    entry = await add_router(hass)
    agents = FakeAgents(main_reply="[sad] Oh no.")

    with agents.patched(), patch.object(
        emotion, "split_tag", side_effect=RuntimeError("bug")
    ):
        result = await ask(hass, entry, "Hi")

    assert speech(result) == "[sad] Oh no."
