# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers for tests that run a request through the router with fake agents behind it."""

import asyncio
import copy
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import (
    DATA_CHAT_LOGS,
    AssistantContent,
    UserContent,
    async_get_chat_log,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.helpers.chat_session import async_get_chat_session
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager.const import DOMAIN

MAIN_AGENT = "conversation.main_agent"
MEMORY_AGENT = "conversation.memory_writer"
REFUSAL_AGENT = "conversation.refusal_writer"

BASE_OPTIONS: dict[str, Any] = {
    "agents": {"agent": MAIN_AGENT},
    # Built-in commands off, so every request reaches the fake main agent.
    "local": {"enabled": False, "leave_questions": True},
    "memory": {
        "enabled": True,
        "agent": MEMORY_AGENT,
        "auto_save": True,
        "max_items": 50,
        "use_global": True,
    },
    "owner": {"name": "Alex", "persona": "The person speaking is Alex."},
    "partner": {"name": "Sam", "persona": "The person speaking is Sam."},
    "strangers": {"mode": "fixed", "fixed_replies": "The answer is no."},
    "near_miss": {"enabled": False, "min_score": 0.5},
    "advanced": {"rule": "Names are labels.", "wait_seconds": 0, "no_helper_role": "owner"},
}


def options_with(**sections: dict[str, Any]) -> dict[str, Any]:
    """BASE_OPTIONS with some keys of some sections replaced."""
    options = copy.deepcopy(BASE_OPTIONS)
    for name, changes in sections.items():
        options[name] = {**options.get(name, {}), **changes}
    return options


async def add_router(
    hass: HomeAssistant, options: dict[str, Any] | None = None, title: str = "Kitchen assistant"
) -> MockConfigEntry:
    assert await async_setup_component(hass, "homeassistant", {})
    # The agents without tools exist, as they would in Home Assistant, unless a test set
    # them up otherwise (with tools, say) or removes one afterwards.
    for agent in (MEMORY_AGENT, REFUSAL_AGENT):
        if hass.states.get(agent) is None:
            hass.states.async_set(agent, "unknown", {"supported_features": 0})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={"name": title, "agent": MAIN_AGENT},
        options=copy.deepcopy(BASE_OPTIONS if options is None else options),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def router_entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("conversation", DOMAIN, entry.entry_id)
    assert entity_id is not None
    return entity_id


def _result(text: str, *, error: bool = False) -> conversation.ConversationResult:
    response = intent.IntentResponse(language="en")
    if error:
        response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, text)
    else:
        response.async_set_speech(text)
    return conversation.ConversationResult(response=response, conversation_id="fake")


@dataclass
class FakeAgents:
    """Stands in for conversation.async_converse: a main agent and a memory agent."""

    main_reply: str = "Noted."
    main_error: bool = False
    memory_reply: str = '{"add": [], "remove": []}'
    # When set, the memory agent waits for it before answering.
    memory_gate: asyncio.Event | None = None
    # When set, the memory agent raises it instead of answering.
    memory_raises: Exception | None = None
    # The agent without tools that writes refusals and announcements.
    writer_reply: str = "A word, if I may."
    writer_error: bool = False
    writer_raises: Exception | None = None
    writer_gate: asyncio.Event | None = None
    # Chat log entries the main agent leaves behind (tool calls and their results),
    # added when the turn has a chat log, as in a voice pipeline.
    main_log: list[Any] = field(default_factory=list)
    # False for an agent that answers without using the chat log at all.
    main_writes_log: bool = True
    # Replies of further agents without tools, by agent id (such as a second router's writer).
    replies: dict[str, str] = field(default_factory=dict)
    # True: the main agent opens its conversation's chat session and chat log, as a
    # conversation entity does, and answers with that conversation's id.
    main_uses_session: bool = False
    # With main_uses_session: the chat log content the main agent saw, for each call.
    main_histories: list[list[Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def converse(self, hass, text, conversation_id, context, **kwargs):
        call = {"text": text, "conversation_id": conversation_id, **kwargs}
        self.calls.append(call)
        if kwargs.get("agent_id") in self.replies:
            return _result(self.replies[kwargs["agent_id"]])
        if kwargs.get("agent_id") == MEMORY_AGENT:
            if self.memory_raises is not None:
                raise self.memory_raises
            if self.memory_gate is not None:
                await self.memory_gate.wait()
            return _result(self.memory_reply)
        if kwargs.get("agent_id") == REFUSAL_AGENT:
            if self.writer_raises is not None:
                raise self.writer_raises
            if self.writer_gate is not None:
                await self.writer_gate.wait()
            return _result(self.writer_reply, error=self.writer_error)
        if self.main_uses_session:
            return self._main_in_session(hass, text, conversation_id, context, kwargs)
        log = hass.data.get(DATA_CHAT_LOGS, {}).get(conversation_id or "")
        if log is not None and self.main_writes_log:
            log.content.extend(self.main_log)
            log.content.append(AssistantContent(agent_id=MAIN_AGENT, content=self.main_reply))
        return _result(self.main_reply, error=self.main_error)

    def _main_in_session(self, hass, text, conversation_id, context, kwargs):
        user_input = conversation.ConversationInput(
            text=text,
            context=context,
            conversation_id=conversation_id,
            device_id=kwargs.get("device_id"),
            satellite_id=None,
            language="en",
            agent_id=MAIN_AGENT,
        )
        with (
            async_get_chat_session(hass, conversation_id, context=context) as session,
            async_get_chat_log(hass, session, user_input) as log,
        ):
            self.main_histories.append(list(log.content))
            log.content.extend(self.main_log)
            log.async_add_assistant_content_without_tools(
                AssistantContent(agent_id=MAIN_AGENT, content=self.main_reply)
            )
            result = _result(self.main_reply, error=self.main_error)
            result.conversation_id = session.conversation_id
            return result

    def calls_to(self, agent_id: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call.get("agent_id") == agent_id]

    @property
    def main_prompt(self) -> str:
        (call,) = self.calls_to(MAIN_AGENT)
        return call.get("extra_system_prompt") or ""

    def patched(self):
        return patch.object(conversation, "async_converse", self.converse)


async def ask(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    text: str,
    *,
    device_id: str | None = None,
    user_id: str | None = None,
) -> conversation.ConversationResult:
    """Send one request straight to the router entity, as a voice assistant would."""
    entity_id = router_entity_id(hass, entry)
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(entity_id)
    assert entity is not None
    return await entity.async_process(
        conversation.ConversationInput(
            text=text,
            context=Context(user_id=user_id),
            conversation_id=None,
            device_id=device_id,
            satellite_id=None,
            language="en",
            agent_id=entity_id,
        )
    )


async def ask_in_pipeline(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    text: str,
    earlier: list[tuple[str, str]] | None = None,
) -> tuple[conversation.ConversationResult, list[Any]]:
    """Ask the router with a chat session and chat log open, as a voice pipeline does.

    `earlier` holds (user text, assistant reply) pairs said before this turn in the same
    conversation. Returns the result and the chat log content as it was left after the turn.
    """
    entity_id = router_entity_id(hass, entry)
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(entity_id)
    assert entity is not None
    context = Context()
    with async_get_chat_session(hass, None, context=context) as session:
        user_input = conversation.ConversationInput(
            text=text,
            context=context,
            conversation_id=session.conversation_id,
            device_id=None,
            satellite_id=None,
            language="en",
            agent_id=entity_id,
        )
        with async_get_chat_log(hass, session, user_input) as log:
            before = [
                content
                for said, answered in earlier or []
                for content in (
                    UserContent(content=said),
                    AssistantContent(agent_id=MAIN_AGENT, content=answered),
                )
            ]
            log.content[-1:-1] = before
            result = await entity.async_process(user_input)
            return result, list(log.content)
