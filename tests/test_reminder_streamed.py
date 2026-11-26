# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A reply that streams to the voice and turns out wrong: the truth follows it at once."""

from unittest.mock import patch

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import async_get_chat_log
from homeassistant.helpers.chat_session import async_get_chat_session

from custom_components.voice_assistant_manager.emotion import StreamedReply
from custom_components.voice_assistant_manager.reminders import (
    DEFAULT_ACTION_FAILURE_LINE,
    DEFAULT_CANCEL_FAILURE_LINE,
    DEFAULT_FAILURE_LINE,
    SPOKE_TOO_SOON,
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


CLAIM_SPOKEN = "Of course, darling. I've set a reminder for you tomorrow at 9 AM to send Alex the instructions."
CANCEL_CLAIM = ["Done, darling. I've cancelled your pills reminder, ", "so that one is gone for good."]


async def test_regression_a_streamed_false_confirmation_is_followed_by_the_truth(hass, sent):  # noqa: F811
    # Seen in the house: the reply streamed, so the voice said "I've set a reminder" while
    # only the text was corrected. The voice now takes it back in the same breath.
    pipeline, result = await streamed(hass, sent, ASKED, CLAIM)

    assert speech(result) == DEFAULT_FAILURE_LINE
    assert pipeline.spoken == f"{CLAIM_SPOKEN} {SPOKE_TOO_SOON} {DEFAULT_FAILURE_LINE}"


async def test_a_streamed_true_confirmation_is_spoken_once_and_nothing_follows(hass, sent):  # noqa: F811
    pipeline, result = await streamed(hass, sent, ASKED, CLAIM, before=tool_call("ok"))

    assert pipeline.spoken == CLAIM_SPOKEN
    assert speech(result) == CLAIM_SPOKEN


async def test_a_streamed_false_cancellation_is_followed_by_the_truth(hass, sent):  # noqa: F811
    pipeline, result = await streamed(hass, sent, "Cancel my pills reminder", CANCEL_CLAIM)

    assert speech(result) == DEFAULT_CANCEL_FAILURE_LINE
    assert pipeline.spoken == (
        f"{''.join(CANCEL_CLAIM)} {SPOKE_TOO_SOON} {DEFAULT_CANCEL_FAILURE_LINE}"
    )


async def test_a_streamed_true_cancellation_is_spoken_once(hass, sent):  # noqa: F811
    pipeline, _result_ = await streamed(
        hass, sent, "Cancel my pills reminder", CANCEL_CLAIM, before=cancel_call("ok")
    )

    assert pipeline.spoken == "".join(CANCEL_CLAIM)


async def test_a_false_confirmation_the_voice_had_not_started_on_is_just_replaced(hass, sent):  # noqa: F811
    # Too short for the voice to start on it while it streamed in: Home Assistant speaks
    # the final reply, which is the honest line. Nothing needs taking back.
    pipeline, result = await streamed(hass, sent, ASKED, ["Your reminder ", "is set."])

    assert speech(result) == DEFAULT_FAILURE_LINE
    assert pipeline.spoken == "Your reminder is set."
    assert SPOKE_TOO_SOON not in pipeline.spoken


async def test_a_reply_streams_to_the_voice_as_it_comes(hass, sent):  # noqa: F811
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
        await ask_streamed(hass, entry, ASKED, pipeline)

    assert pipeline.spoken == "".join(pieces)
    # Also in a conversation about a reminder: the first piece is heard before the second
    # is written.
    assert heard_when[0] == "heard after 0 written"


async def test_with_reminders_off_a_streamed_false_confirmation_is_followed_by_the_truth(hass, sent):  # noqa: F811
    entry = await add_router(hass, options_with(memory={"enabled": False}))
    pipeline = Pipeline(sent)

    with patch.object(conversation, "async_converse", streaming_agent(CLAIM)):
        result, _listener, _content = await ask_streamed(hass, entry, ASKED, pipeline)

    assert speech(result) == DEFAULT_ACTION_FAILURE_LINE
    assert pipeline.spoken == f"{CLAIM_SPOKEN} {SPOKE_TOO_SOON} {DEFAULT_ACTION_FAILURE_LINE}"


# The listener


class Listener:
    def __init__(self) -> None:
        self.deltas: list[dict] = []

    def __call__(self, chat_log, delta: dict) -> None:
        self.deltas.append(delta)

    @property
    def spoken(self) -> str:
        return "".join(d.get("content") or "" for d in self.deltas)


LONG = "This sentence is comfortably longer than sixty characters, as it must be."


def test_the_voice_has_started_once_it_was_given_more_than_sixty_characters():
    stream = StreamedReply(Listener(), lambda _f: None)
    stream(None, {"role": "assistant", "content": "x" * 60})
    assert stream.speaking is False

    stream(None, {"content": "y"})

    assert stream.speaking is True


def test_the_voice_has_started_on_some_text_followed_by_a_tool_call():
    stream = StreamedReply(Listener(), lambda _f: None)
    stream(None, {"role": "assistant", "content": "One moment."})

    stream(None, {"tool_calls": ["call"]})

    assert stream.speaking is True


def test_a_tool_call_before_any_text_does_not_start_the_voice():
    stream = StreamedReply(Listener(), lambda _f: None)

    stream(None, {"role": "assistant", "tool_calls": ["call"]})

    assert stream.speaking is False


def test_nothing_is_passed_on_while_muted_and_what_came_before_is_closed():
    listener = Listener()
    stream = StreamedReply(listener, lambda _f: None)
    stream(None, {"role": "assistant", "content": "[happy] First "})
    stream(None, {"content": "[lau"})

    stream.mute()
    stream(None, {"role": "assistant", "content": "Second answer."})
    stream(None, {"tool_calls": ["call"]})
    stream.unmute()

    assert listener.spoken == "First [lau"
    assert stream.speaking is False


def test_words_can_be_added_after_what_the_voice_was_given():
    listener = Listener()
    stream = StreamedReply(listener, lambda _f: None)
    stream(None, {"role": "assistant", "content": LONG})

    stream.say("Sorry, scratch that.")

    assert listener.spoken == f"{LONG} Sorry, scratch that."
