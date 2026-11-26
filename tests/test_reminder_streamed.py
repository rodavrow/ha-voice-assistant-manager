# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A reminder reply that streams in: a false confirmation must never reach the voice."""

from unittest.mock import patch

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import async_get_chat_log
from homeassistant.helpers.chat_session import async_get_chat_session

from custom_components.voice_assistant_manager.emotion import StreamedReply
from custom_components.voice_assistant_manager.reminders import (
    DEFAULT_CANCEL_FAILURE_LINE,
    DEFAULT_FAILURE_LINE,
)

from .router_helpers import MAIN_AGENT, _result, add_router, options_with
from .test_emotion_router import Pipeline, ask_streamed, sent, speech  # noqa: F401 - fixture
from .test_reminder_cancel import cancel_call
from .test_reminders import tool_call

REMINDERS = {"enabled": True}
ASKED = "Remind me tomorrow at 9am to send Alex the instructions I promised."
CLAIM = [
    "[playful] Of course, darling. [snappy] I've set a reminder for you tomorrow at 9 AM to send",
    " Alex the instructions.",
]


def streaming_agent(pieces: list[str], before: list | None = None, seen: list | None = None):
    """A main agent that leaves `before` in the chat log (tool calls), then streams."""

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
                if seen is not None:
                    seen.append(piece)

        with (
            async_get_chat_session(hass, conversation_id, context=context) as session,
            async_get_chat_log(hass, session, user_input) as log,
        ):
            log.content.extend(before or [])
            async for _content in log.async_add_delta_content_stream(MAIN_AGENT, deltas()):
                pass
        result = _result("".join(pieces))
        result.conversation_id = conversation_id
        return result

    return converse


async def streamed(hass, sent, text, pieces, before=None, **reminders):  # noqa: F811
    entry = await add_router(
        hass, options_with(reminders={**REMINDERS, **reminders}, memory={"enabled": False})
    )
    pipeline = Pipeline(sent)
    with patch.object(conversation, "async_converse", streaming_agent(pieces, before)):
        result, _listener, _content = await ask_streamed(hass, entry, text, pipeline)
    return pipeline, result


async def test_regression_a_streamed_false_confirmation_is_never_spoken(hass, sent):  # noqa: F811
    # Seen in the house: the reply streamed, so the voice said "I've set a reminder" while
    # only the text was corrected.
    pipeline, result = await streamed(hass, sent, ASKED, CLAIM)

    assert speech(result) == DEFAULT_FAILURE_LINE
    assert pipeline.spoken == DEFAULT_FAILURE_LINE
    assert "set a reminder" not in pipeline.spoken


async def test_a_streamed_true_confirmation_is_spoken_whole(hass, sent):  # noqa: F811
    pipeline, result = await streamed(hass, sent, ASKED, CLAIM, before=tool_call("ok"))

    assert pipeline.spoken == "Of course, darling. I've set a reminder for you tomorrow at 9 AM to send Alex the instructions."
    assert speech(result) == pipeline.spoken


async def test_a_streamed_false_cancellation_is_never_spoken(hass, sent):  # noqa: F811
    pieces = ["Done, darling. I've cancelled your pills reminder, ", "so that one is gone for good."]

    pipeline, result = await streamed(hass, sent, "Cancel my pills reminder", pieces)

    assert pipeline.spoken == DEFAULT_CANCEL_FAILURE_LINE
    assert speech(result) == DEFAULT_CANCEL_FAILURE_LINE


async def test_a_streamed_true_cancellation_is_spoken_whole(hass, sent):  # noqa: F811
    pieces = ["Done, darling. I've cancelled your pills reminder, ", "so that one is gone for good."]

    pipeline, _result_ = await streamed(
        hass, sent, "Cancel my pills reminder", pieces, before=cancel_call("ok")
    )

    assert pipeline.spoken == "".join(pieces)


async def test_every_reply_reaches_the_voice_only_once_it_is_complete(hass, sent):  # noqa: F811
    entry = await add_router(
        hass, options_with(reminders=REMINDERS, memory={"enabled": False})
    )
    heard_when: list[str] = []
    seen: list[str] = []
    pieces = ["The kitchen lights are on, ", "and the hall is off."]

    class Watching(Pipeline):
        def __call__(self, chat_log, delta):
            super().__call__(chat_log, delta)
            if delta.get("content"):
                heard_when.append(f"heard after {len(seen)} written")

    pipeline = Watching(sent)
    with patch.object(
        conversation, "async_converse", streaming_agent(pieces, seen=seen)
    ):
        await ask_streamed(hass, entry, "Which lights are on?", pipeline)

    assert pipeline.spoken == "".join(pieces)
    # Checked before it is spoken: nothing reached the voice until all of it was written.
    assert heard_when == ["heard after 2 written"]


async def test_with_reminders_off_the_reply_is_still_spoken(hass, sent):  # noqa: F811
    entry = await add_router(hass, options_with(memory={"enabled": False}))
    pipeline = Pipeline(sent)

    with patch.object(conversation, "async_converse", streaming_agent(CLAIM)):
        await ask_streamed(hass, entry, ASKED, pipeline)

    assert "set a reminder" in pipeline.spoken


# The listener


class Listener:
    def __init__(self) -> None:
        self.deltas: list[dict] = []

    def __call__(self, chat_log, delta: dict) -> None:
        self.deltas.append(delta)

    @property
    def spoken(self) -> str:
        return "".join(d.get("content") or "" for d in self.deltas)


def test_held_text_is_passed_on_at_the_end():
    listener = Listener()
    stream = StreamedReply(listener, lambda _f: None, hold=True)

    stream(None, {"role": "assistant"})
    stream(None, {"content": "[happy] One "})
    stream(None, {"content": "two."})
    assert listener.spoken == ""

    stream.finish()

    assert listener.spoken == "One two."


def test_held_text_is_replaced_at_the_end():
    listener = Listener()
    stream = StreamedReply(listener, lambda _f: None, hold=True)
    stream(None, {"role": "assistant", "content": "I've set it."})

    stream.finish("I could not.")

    assert listener.spoken == "I could not."


def test_the_face_is_still_told_at_once_while_the_text_is_held():
    found: list[str] = []
    stream = StreamedReply(Listener(), found.append, hold=True)

    stream(None, {"role": "assistant", "content": "[happy] One"})

    assert found == ["happy"]


def test_two_messages_of_a_held_reply_are_kept_apart():
    listener = Listener()
    stream = StreamedReply(listener, lambda _f: None, hold=True)

    stream(None, {"role": "assistant", "content": "Let me see."})
    stream(None, {"role": "tool_result", "tool_call_id": "1", "tool_name": "x", "result": {}})
    stream(None, {"role": "assistant", "content": "Done."})
    stream.finish()

    assert listener.spoken == "Let me see. Done."


def test_a_reminder_tool_call_starts_holding_the_text():
    listener = Listener()
    stream = StreamedReply(listener, lambda _f: None, hold_tools=("create_reminder",))

    class Call:
        tool_name = "script__create_reminder"

    stream(None, {"role": "assistant", "tool_calls": [Call()]})
    stream(None, {"role": "tool_result", "tool_call_id": "1", "tool_name": "x", "result": {}})
    stream(None, {"role": "assistant", "content": "Your reminder is set."})
    assert listener.spoken == ""

    stream.finish()

    assert listener.spoken == "Your reminder is set."


def test_a_replacement_is_spoken_even_when_nothing_was_held():
    listener = Listener()
    stream = StreamedReply(listener, lambda _f: None)
    stream(None, {"role": "assistant", "content": "Short."})

    stream.finish("I could not.")

    # What already went out cannot be taken back; the correction is not added to it.
    assert listener.spoken == "Short."
