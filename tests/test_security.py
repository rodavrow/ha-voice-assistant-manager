# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Security regressions: what a signed-in user who is not an admin can reach over the API."""

import copy
from typing import Any, Literal
from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import ChatLog
from homeassistant.core import (
    Context,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from pytest_homeassistant.common import MockConfigEntry, MockUser

from custom_components.voice_assistant_manager.announce import can_control_home, writer_agent
from custom_components.voice_assistant_manager.config_flow import settings_error
from custom_components.voice_assistant_manager.conversation import LAST_RESORT_REPLY, PersonaRouter
from custom_components.voice_assistant_manager.memory import (
    REMEMBER_RULE,
    async_get_memory,
    entry_key,
)
from custom_components.voice_assistant_manager.status import router_status
from custom_components.voice_assistant_manager.voice_identity import VoiceIdentity

from .router_helpers import (
    MAIN_AGENT,
    MEMORY_AGENT,
    REFUSAL_AGENT,
    FakeAgents,
    add_router,
    ask,
    options_with,
    router_entity_id,
)
from .test_memory_extraction import reply, told
from .test_mine_api import ALEX, Mine, alex_router

ALEX_USER = "user-alex"
ALEX_FACT = "Alex keeps the spare key under the mat."


async def kitchen_satellite(
    hass: HomeAssistant, helper_state: str, score: str | None = None
) -> str:
    """A satellite whose voice_speaker helper holds the last voice match."""
    satellite_entry = MockConfigEntry(domain="test")
    satellite_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=satellite_entry.entry_id,
        identifiers={("test", "kitchen")},
        name="Kitchen satellite",
    )
    hass.states.async_set(f"input_text.voice_speaker_{device.id}", helper_state)
    if score is not None:
        sensor = er.async_get(hass).async_get_or_create(
            "sensor",
            "test",
            "kitchen-score",
            device_id=device.id,
            suggested_object_id="kitchen_satellite_last_speaker_score",
        )
        hass.states.async_set(sensor.entity_id, score)
    return device.id


async def process_over_websocket(
    hass: HomeAssistant, hass_ws_client: Any, token: str, **message: Any
) -> dict[str, Any]:
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json_auto_id({"type": "conversation/process", **message})
    answer: dict[str, Any] = await client.receive_json()
    assert answer["success"], answer
    return answer["result"]  # type: ignore[no-any-return]


async def test_regression_security_typed_request_naming_a_satellite_is_not_its_last_speaker(
    hass: HomeAssistant, hass_ws_client: Any, hass_read_only_access_token: str
) -> None:
    # Alex is mapped, the signed-in user is not: naming the satellite Alex last spoke to
    # must not make them Alex.
    entry = await add_router(hass, options_with(owner={"user": ALEX_USER}))
    memory = await async_get_memory(hass)
    await memory.async_add(entry_key(entry.entry_id, "owner"), ALEX_FACT, "told")
    device_id = await kitchen_satellite(hass, "owner")
    agents = FakeAgents(memory_reply=reply(add=[told("Likes coffee.")]))

    with agents.patched():
        result = await process_over_websocket(
            hass,
            hass_ws_client,
            hass_read_only_access_token,
            text="What do you remember about me?",
            agent_id=router_entity_id(hass, entry),
            device_id=device_id,
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert result["response"]["speech"]["plain"]["speech"] == "The answer is no."
    assert agents.calls_to(MAIN_AGENT) == []
    assert agents.calls_to(MEMORY_AGENT) == []
    assert [item["text"] for item in memory.items(entry_key(entry.entry_id, "owner"))] == [
        ALEX_FACT
    ]


async def test_regression_security_typed_request_naming_a_satellite_goes_by_the_user(
    hass: HomeAssistant,
    hass_ws_client: Any,
    hass_read_only_user: MockUser,
    hass_read_only_access_token: str,
) -> None:
    # The signed-in user is Sam: they get Sam's persona, never Alex's, whatever the helper says.
    entry = await add_router(
        hass,
        options_with(
            owner={"user": ALEX_USER},
            partner={"user": hass_read_only_user.id},
        ),
    )
    memory = await async_get_memory(hass)
    await memory.async_add(entry_key(entry.entry_id, "owner"), ALEX_FACT, "told", private=True)
    device_id = await kitchen_satellite(hass, "owner")
    agents = FakeAgents()

    with agents.patched():
        await process_over_websocket(
            hass,
            hass_ws_client,
            hass_read_only_access_token,
            text="What should I drink tonight?",
            agent_id=router_entity_id(hass, entry),
            device_id=device_id,
        )
        await hass.async_block_till_done(wait_background_tasks=True)

    assert "The person speaking is Sam." in agents.main_prompt
    assert "The person speaking is Alex." not in agents.main_prompt
    assert ALEX_FACT not in agents.main_prompt


async def test_regression_security_typed_request_naming_a_satellite_gets_no_near_miss(
    hass: HomeAssistant,
) -> None:
    # The satellite's last voice was a near miss; a guest's typed request naming it must get
    # the plain refusal for an unknown voice, not the near-miss reply.
    entry = await add_router(
        hass,
        options_with(
            owner={"user": ALEX_USER},
            near_miss={"enabled": True, "min_score": 0.5, "fixed_replies": "Say that again?"},
        ),
    )
    device_id = await kitchen_satellite(hass, "stranger", score="0.9")
    agents = FakeAgents()

    with agents.patched():
        result = await ask(hass, entry, "Who am I?", device_id=device_id, user_id="user-guest")

    assert result.response.speech["plain"]["speech"] == "The answer is no."
    assert agents.calls == []


async def test_regression_security_request_with_a_user_never_waits_for_a_pending_helper(
    hass: HomeAssistant,
) -> None:
    entry = await add_router(
        hass,
        options_with(
            owner={"user": ALEX_USER},
            advanced={"match_within": 6, "no_helper_role": "stranger"},
        ),
    )
    device_id = await kitchen_satellite(hass, "pending")
    agents = FakeAgents()

    with (
        agents.patched(),
        patch.object(VoiceIdentity, "_await_match", autospec=True) as helper_state,
    ):
        await ask(hass, entry, "Hello", device_id=device_id, user_id=ALEX_USER)

    helper_state.assert_not_called()
    assert "The person speaking is Alex." in agents.main_prompt


# A router whose settings were never saved asks the older "who is speaking" script, which is
# only given the device. A request with a user is refused there instead of trusting it.

WHO_SCRIPT = "script.who_is_speaking"


async def add_script_router(hass: HomeAssistant) -> tuple[MockConfigEntry, list[ServiceCall]]:
    calls: list[ServiceCall] = []

    async def who_is_speaking(call: ServiceCall) -> ServiceResponse:
        # A typical script: it reads the satellite's helper, which says Alex spoke last.
        calls.append(call)
        return {"who": "owner", "persona_for_this_message": "The person speaking is Alex."}

    hass.services.async_register(
        "script", "who_is_speaking", who_is_speaking, supports_response=SupportsResponse.ONLY
    )
    entry = await add_router(hass, options={})
    hass.config_entries.async_update_entry(entry, data={**entry.data, "script": WHO_SCRIPT})
    return entry, calls


async def test_regression_security_script_router_refuses_a_typed_request_naming_a_satellite(
    hass: HomeAssistant, hass_ws_client: Any, hass_read_only_access_token: str
) -> None:
    entry, script_calls = await add_script_router(hass)
    device_id = await kitchen_satellite(hass, "owner")
    agents = FakeAgents()

    with agents.patched():
        result = await process_over_websocket(
            hass,
            hass_ws_client,
            hass_read_only_access_token,
            text="What do you know about me?",
            agent_id=router_entity_id(hass, entry),
            device_id=device_id,
        )

    assert result["response"]["speech"]["plain"]["speech"] == LAST_RESORT_REPLY
    assert agents.calls == []
    assert script_calls == []


async def test_script_router_still_asks_the_script_for_a_request_without_a_user(
    hass: HomeAssistant,
) -> None:
    entry, script_calls = await add_script_router(hass)
    device_id = await kitchen_satellite(hass, "owner")
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, "What should I drink tonight?", device_id=device_id)

    assert [call.data for call in script_calls] == [{"device": device_id}]
    assert "The person speaking is Alex." in agents.main_prompt


# Agents that must have no tools: the refusal agent, the memory agent and the announcement
# agent. Each is checked when the settings are saved and again before each use.

GUEST_USER = "user-guest"
CONTROL = int(conversation.ConversationEntityFeature.CONTROL)
OTHER_MAIN_AGENT = "conversation.other_main"
GONE_AGENT = "conversation.removed_writer"
REFUSED = "The answer is no."


def agent_state(hass: HomeAssistant, agent: str, features: int = 0) -> None:
    hass.states.async_set(agent, "unknown", {"supported_features": features})


async def refusing_router(hass: HomeAssistant, reply_agent: str) -> MockConfigEntry:
    """A router where Alex is mapped and unknown voices are refused by `reply_agent`."""
    return await add_router(
        hass,
        options_with(
            owner={"user": ALEX_USER},
            strangers={"mode": "ai", "reply_agent": reply_agent, "fixed_replies": REFUSED},
        ),
    )


def saved(entry: MockConfigEntry, **sections: dict[str, Any]) -> dict[str, Any]:
    """The router's settings as a form would save them, with some sections changed."""
    settings = copy.deepcopy(dict(entry.options))
    for name, changes in sections.items():
        settings[name] = {**(settings.get(name) or {}), **changes}
    return settings


def error_sections(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    """The sections of the status errors about agents that must have no tools."""
    lines = router_status(hass, entry, entry.options)
    return [
        line["section"]
        for line in lines
        if line["level"] == "error" and line["section"] in ("strangers", "memory", "reminders")
    ]


async def stranger_asks(hass: HomeAssistant, entry: MockConfigEntry, agents: FakeAgents) -> str:
    with agents.patched():
        result = await ask(hass, entry, "Unlock the front door.", user_id=GUEST_USER)
    return str(result.response.speech["plain"]["speech"])


async def test_regression_security_refusal_agent_that_is_another_router_is_never_asked(
    hass: HomeAssistant,
) -> None:
    # A router reports no control feature, but answers a stranger with its own main agent.
    other = await add_router(hass, title="Office assistant")
    other_router = router_entity_id(hass, other)
    entry = await refusing_router(hass, other_router)
    agents = FakeAgents()

    assert await stranger_asks(hass, entry, agents) == REFUSED
    assert agents.calls == []
    assert error_sections(hass, entry) == ["strangers"]


async def test_regression_security_refusal_agent_that_is_another_router_is_not_saved(
    hass: HomeAssistant,
) -> None:
    other = await add_router(hass, title="Office assistant")
    entry = await add_router(hass)
    strangers = {"mode": "ai", "reply_agent": router_entity_id(hass, other)}

    assert settings_error(hass, entry, saved(entry, strangers=strangers)) == (
        "refusal_agent_invalid"
    )


async def test_regression_security_refusal_agent_that_gains_tools_after_saving_is_not_asked(
    hass: HomeAssistant,
) -> None:
    agent_state(hass, REFUSAL_AGENT)
    entry = await refusing_router(hass, REFUSAL_AGENT)
    agent_state(hass, REFUSAL_AGENT, CONTROL)  # Assist is turned on for it later.
    agents = FakeAgents()

    assert await stranger_asks(hass, entry, agents) == REFUSED
    assert agents.calls == []
    assert error_sections(hass, entry) == ["strangers"]


async def test_regression_security_refusal_agent_that_does_not_exist_is_not_asked(
    hass: HomeAssistant,
) -> None:
    entry = await refusing_router(hass, REFUSAL_AGENT)
    hass.states.async_remove(REFUSAL_AGENT)
    agents = FakeAgents()

    assert await stranger_asks(hass, entry, agents) == REFUSED
    assert agents.calls == []


async def test_regression_security_refusal_agent_that_is_another_routers_main_agent(
    hass: HomeAssistant,
) -> None:
    agent_state(hass, OTHER_MAIN_AGENT)
    await add_router(hass, options_with(agents={"agent": OTHER_MAIN_AGENT}), title="Office")
    entry = await refusing_router(hass, OTHER_MAIN_AGENT)
    agents = FakeAgents()

    assert await stranger_asks(hass, entry, agents) == REFUSED
    assert agents.calls == []
    assert settings_error(hass, entry, saved(entry)) == "refusal_agent_invalid"


@pytest.mark.parametrize("agent", [conversation.HOME_ASSISTANT_AGENT, MAIN_AGENT])
async def test_regression_security_refusal_agent_that_is_home_assistant_or_main_is_not_asked(
    hass: HomeAssistant, agent: str
) -> None:
    agent_state(hass, agent)
    entry = await refusing_router(hass, agent)
    agents = FakeAgents()

    assert await stranger_asks(hass, entry, agents) == REFUSED
    assert agents.calls == []


async def test_a_refusal_agent_without_tools_still_writes_the_refusal(
    hass: HomeAssistant,
) -> None:
    agent_state(hass, REFUSAL_AGENT)
    entry = await refusing_router(hass, REFUSAL_AGENT)
    agents = FakeAgents()

    assert await stranger_asks(hass, entry, agents) == "A word, if I may."
    assert settings_error(hass, entry, saved(entry)) is None
    assert error_sections(hass, entry) == []


async def test_regression_security_memory_agent_with_tools_is_not_saved(
    hass: HomeAssistant,
) -> None:
    agent_state(hass, MEMORY_AGENT, CONTROL)
    entry = await add_router(hass)

    assert settings_error(hass, entry, saved(entry)) == "memory_agent_invalid"


async def remember_tea(hass: HomeAssistant, entry: MockConfigEntry) -> FakeAgents:
    agents = FakeAgents(memory_reply=reply(add=[told("Alex likes green tea.")]))
    with agents.patched():
        await ask(hass, entry, "Remember that I like green tea.", user_id=ALEX_USER)
        await hass.async_block_till_done(wait_background_tasks=True)
    return agents


async def test_regression_security_memory_agent_with_tools_is_never_asked(
    hass: HomeAssistant,
) -> None:
    agent_state(hass, MEMORY_AGENT)
    entry = await add_router(hass, options_with(owner={"user": ALEX_USER}))
    agent_state(hass, MEMORY_AGENT, CONTROL)  # Assist is turned on for it later.

    agents = await remember_tea(hass, entry)

    assert agents.calls_to(MEMORY_AGENT) == []
    assert len(agents.calls_to(MAIN_AGENT)) == 1
    assert error_sections(hass, entry) == ["memory"]


async def test_regression_security_memory_agent_that_is_another_router_is_never_asked(
    hass: HomeAssistant,
) -> None:
    agent_state(hass, MEMORY_AGENT)
    other = await add_router(hass, title="Office assistant")
    other_router = router_entity_id(hass, other)
    entry = await add_router(
        hass, options_with(owner={"user": ALEX_USER}, memory={"agent": other_router})
    )

    agents = await remember_tea(hass, entry)

    assert agents.calls_to(other_router) == []
    assert settings_error(hass, entry, saved(entry)) == "memory_agent_invalid"


async def test_a_memory_agent_without_tools_still_saves(hass: HomeAssistant) -> None:
    agent_state(hass, MEMORY_AGENT)
    entry = await add_router(hass, options_with(owner={"user": ALEX_USER}))

    agents = await remember_tea(hass, entry)

    assert len(agents.calls_to(MEMORY_AGENT)) == 1
    assert settings_error(hass, entry, saved(entry)) is None
    assert error_sections(hass, entry) == []


async def test_regression_security_announcement_agent_that_is_another_routers_main_agent(
    hass: HomeAssistant,
) -> None:
    agent_state(hass, MEMORY_AGENT)
    agent_state(hass, OTHER_MAIN_AGENT)
    await add_router(hass, options_with(agents={"agent": OTHER_MAIN_AGENT}), title="Office")
    entry = await add_router(
        hass, options_with(reminders={"enabled": True, "agent": OTHER_MAIN_AGENT})
    )

    assert settings_error(hass, entry, saved(entry)) == "reminder_agent_invalid"
    assert writer_agent(hass, entry) is None
    assert error_sections(hass, entry) == ["reminders"]


async def test_regression_security_announcement_agent_that_does_not_exist_writes_nothing(
    hass: HomeAssistant,
) -> None:
    entry = await add_router(
        hass, options_with(reminders={"enabled": True, "agent": GONE_AGENT})
    )

    assert settings_error(hass, entry, saved(entry)) == "reminder_agent_invalid"
    assert writer_agent(hass, entry) is None


async def test_regression_security_unused_announcement_agent_does_not_block_a_save(
    hass: HomeAssistant,
) -> None:
    # Reminders off and no calendars: nothing announces, so a stale agent may stay saved.
    # writer_agent still refuses it if something asks.
    entry = await add_router(
        hass, options_with(reminders={"enabled": False, "agent": GONE_AGENT})
    )

    assert settings_error(hass, entry, saved(entry)) is None
    assert writer_agent(hass, entry) is None


async def test_regression_security_announcement_agent_for_calendars_is_checked(
    hass: HomeAssistant,
) -> None:
    options = options_with(reminders={"enabled": False, "agent": GONE_AGENT})
    options["calendar_rows"] = [{"calendar": "calendar.bins", "for": "household"}]
    entry = await add_router(hass, options)

    assert settings_error(hass, entry, saved(entry)) == "reminder_agent_invalid"


@pytest.mark.parametrize("features", ["lots", None, [1]])
async def test_regression_security_odd_supported_features_count_as_control(
    hass: HomeAssistant, features: Any
) -> None:
    hass.states.async_set(REFUSAL_AGENT, "unknown", {"supported_features": features})

    assert can_control_home(hass, REFUSAL_AGENT) is True


async def test_regression_security_unsafe_memory_agent_keeps_the_facts_but_saves_nothing(
    hass: HomeAssistant,
) -> None:
    agent_state(hass, MEMORY_AGENT, CONTROL)
    entry = await add_router(hass, options_with(owner={"user": ALEX_USER}))
    memory = await async_get_memory(hass)
    await memory.async_add(entry_key(entry.entry_id, "owner"), ALEX_FACT, "told")

    agents = await remember_tea(hass, entry)

    assert ALEX_FACT in agents.main_prompt
    assert REMEMBER_RULE not in agents.main_prompt
    assert agents.calls_to(MEMORY_AGENT) == []


async def test_an_announcement_agent_without_tools_still_writes(hass: HomeAssistant) -> None:
    agent_state(hass, MEMORY_AGENT)
    agent_state(hass, REFUSAL_AGENT)
    entry = await add_router(hass, options_with(reminders={"agent": REFUSAL_AGENT}))

    assert settings_error(hass, entry, saved(entry)) is None
    assert writer_agent(hass, entry) == REFUSAL_AGENT


@pytest.fixture
async def mine(hass: HomeAssistant, hass_ws_client: Any, hass_read_only_access_token: str) -> Mine:
    return Mine(await hass_ws_client(hass, access_token=hass_read_only_access_token))


async def test_regression_security_a_persona_save_with_an_unsafe_agent_asks_for_an_admin(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    # Settings saved before this check may hold an unsafe refusal agent. The person cannot
    # fix that, so they are told to ask an administrator, and nothing is saved.
    entry = await alex_router(
        hass, hass_read_only_user, strangers={"mode": "ai", "reply_agent": REFUSAL_AGENT}
    )
    agent_state(hass, REFUSAL_AGENT, CONTROL)
    before = copy.deepcopy(dict(entry.options))

    text = await mine.refused("save_persona", entry_id=entry.entry_id, persona="New persona.")

    assert "administrator" in text
    assert dict(entry.options) == before


async def test_regression_security_a_refused_persona_save_names_no_router_or_agent(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    # The admin reason would name the other router and what is wrong with the agents.
    other = await add_router(
        hass, options_with(reminders={"household_calendars": [ALEX]}), title="Office assistant"
    )
    other_router = router_entity_id(hass, other)
    entry = await alex_router(
        hass, hass_read_only_user, strangers={"mode": "ai", "reply_agent": other_router}
    )
    before = copy.deepcopy(dict(entry.options))

    text = await mine.refused("save_persona", entry_id=entry.entry_id, persona="New persona.")

    assert "administrator" in text
    for secret in ("Office assistant", other_router, ALEX, "control the home", "{"):
        assert secret not in text
    assert dict(entry.options) == before


# When the speaker cannot be worked out, the request is refused: the main agent is never
# asked. Home Assistant keeps the previous turn's extra prompt when a turn gives none, so
# the main agent's prompt always names the person speaking now.

KNOWN_ONLY = "What is on the shopping list?"


async def ask_in_conversation(
    hass: HomeAssistant, entry: MockConfigEntry, text: str, conversation_id: str
) -> conversation.ConversationResult:
    """Like ask, in a conversation that goes on from one request to the next."""
    entity_id = router_entity_id(hass, entry)
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(entity_id)
    assert entity is not None
    return await entity.async_process(
        conversation.ConversationInput(
            text=text,
            context=Context(),
            conversation_id=conversation_id,
            device_id=None,
            satellite_id=None,
            language="en",
            agent_id=entity_id,
        )
    )


def speech(result: conversation.ConversationResult) -> str:
    return str(result.response.speech["plain"]["speech"])


async def test_regression_security_a_failing_lookup_is_refused(hass: HomeAssistant) -> None:
    entry = await add_router(hass)
    agents = FakeAgents()

    with (
        agents.patched(),
        patch.object(
            PersonaRouter, "_lookup_from_settings", side_effect=RuntimeError("lookup broke")
        ),
    ):
        result = await ask(hass, entry, KNOWN_ONLY)

    assert speech(result) == LAST_RESORT_REPLY
    assert agents.calls == []


async def test_regression_security_a_router_without_settings_or_script_is_refused(
    hass: HomeAssistant,
) -> None:
    entry = await add_router(hass, options={})
    agents = FakeAgents()

    with agents.patched():
        result = await ask(hass, entry, KNOWN_ONLY)

    assert speech(result) == LAST_RESORT_REPLY
    assert agents.calls == []


async def script_router_answering(
    hass: HomeAssistant, answer: ServiceResponse | Exception
) -> MockConfigEntry:
    async def who_is_speaking(call: ServiceCall) -> ServiceResponse:
        if isinstance(answer, Exception):
            raise answer
        return answer

    hass.services.async_register(
        "script", "who_is_speaking", who_is_speaking, supports_response=SupportsResponse.ONLY
    )
    entry = await add_router(hass, options={})
    hass.config_entries.async_update_entry(entry, data={**entry.data, "script": WHO_SCRIPT})
    return entry


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"persona_for_this_message": "", "label": "", "fixed_replies": [], "reply_agent": ""},
        RuntimeError("script broke"),
    ],
    ids=["nothing", "only-empty-values", "fails"],
)
async def test_regression_security_a_script_that_fails_or_says_nothing_is_refused(
    hass: HomeAssistant, answer: ServiceResponse | Exception
) -> None:
    entry = await script_router_answering(hass, answer)
    agents = FakeAgents()

    with agents.patched():
        result = await ask(hass, entry, KNOWN_ONLY)

    assert speech(result) == LAST_RESORT_REPLY
    assert agents.calls == []


async def test_regression_security_a_stranger_after_alex_gets_no_alex_persona(
    hass: HomeAssistant,
) -> None:
    # Alex speaks first. The next request in the same conversation cannot be worked out:
    # handing it to the main agent without a prompt would let Alex's persona carry over.
    entry = await add_router(hass)
    agents = FakeAgents(main_reply="Hello Alex, the spare key is under the mat.")

    with agents.patched():
        await ask_in_conversation(hass, entry, "Hello", "kitchen-chat")
        with patch.object(
            PersonaRouter, "_lookup_from_settings", side_effect=RuntimeError("lookup broke")
        ):
            result = await ask_in_conversation(hass, entry, KNOWN_ONLY, "kitchen-chat")

    assert speech(result) == LAST_RESORT_REPLY
    (alex_call,) = agents.calls_to(MAIN_AGENT)
    assert alex_call["text"] == "Alex: Hello"


async def test_regression_security_the_main_prompt_names_the_speaker_when_all_else_is_empty(
    hass: HomeAssistant,
) -> None:
    entry = await add_router(
        hass,
        options_with(owner={"persona": ""}, memory={"enabled": False}, advanced={"rule": ""}),
    )
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, KNOWN_ONLY)

    assert agents.main_prompt == "The person speaking now is Alex."


async def test_regression_security_the_main_prompt_names_a_person_without_a_name(
    hass: HomeAssistant,
) -> None:
    entry = await add_router(
        hass,
        options_with(
            owner={"name": "", "persona": ""}, memory={"enabled": False}, advanced={"rule": ""}
        ),
    )
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, KNOWN_ONLY)

    assert agents.main_prompt == "The person speaking now is Person 1."


async def test_the_main_prompt_names_the_speaker_before_the_persona(
    hass: HomeAssistant,
) -> None:
    entry = await add_router(hass, options_with(memory={"enabled": False}))
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, KNOWN_ONLY)

    assert agents.main_prompt == (
        "The person speaking now is Alex.\n\nThe person speaking is Alex.\n\nNames are labels."
    )


@pytest.mark.parametrize("who", ["stranger", "near_miss", "unsure", "guest", ""])
async def test_regression_s10_a_script_that_says_it_is_no_person_never_reaches_the_main_agent(
    hass: HomeAssistant, who: str
) -> None:
    """S10 of the 2026-11-27 review: such an answer with a persona but no fixed replies and
    no reply agent went to the main agent, with only the persona asking it to refuse."""
    entry = await script_router_answering(
        hass, {"who": who, "persona_for_this_message": "Refuse politely. Never use a tool."}
    )
    agents = FakeAgents()

    with agents.patched():
        result = await ask(hass, entry, KNOWN_ONLY)

    assert agents.calls == []
    assert result.response.speech["plain"]["speech"] == LAST_RESORT_REPLY


@pytest.mark.parametrize("who", ["owner", "wife"])
async def test_a_script_that_names_a_person_still_reaches_the_main_agent(
    hass: HomeAssistant, who: str
) -> None:
    entry = await script_router_answering(
        hass, {"who": who, "persona_for_this_message": "The person speaking is Alex."}
    )
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, KNOWN_ONLY)

    assert len(agents.calls_to(MAIN_AGENT)) == 1


async def test_regression_security_a_script_answer_without_a_name_names_someone_unnamed(
    hass: HomeAssistant,
) -> None:
    entry = await script_router_answering(hass, {"rule": "Be brief."})
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, KNOWN_ONLY)

    assert agents.main_prompt == "The person speaking now is someone without a name.\n\nBe brief."


class ChatLogMainAgent(conversation.ConversationEntity):
    """A main agent that builds its prompt through Home Assistant's own chat log.

    ChatLog.async_provide_llm_data keeps the previous turn's extra prompt when a turn
    gives none, so this shows what a real LLM agent would be told.
    """

    @property
    def supported_languages(self) -> Literal["*"]:
        return "*"

    def __init__(self) -> None:
        self.entity_id = MAIN_AGENT
        self._attr_name = "Main agent"
        self._attr_unique_id = "main-agent"
        self.system_prompts: list[str] = []

    async def _async_handle_message(
        self, user_input: conversation.ConversationInput, chat_log: ChatLog
    ) -> conversation.ConversationResult:
        await chat_log.async_provide_llm_data(
            user_input.as_llm_context("test"),
            None,
            None,
            user_input.extra_system_prompt,
        )
        self.system_prompts.append(str(chat_log.content[0].content))
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=MAIN_AGENT, content="Noted.")
        )
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech("Noted.")
        return conversation.ConversationResult(
            response=response, conversation_id=chat_log.conversation_id
        )


async def test_regression_security_second_speaker_never_inherits_first_prompt(
    hass: HomeAssistant,
) -> None:
    # Alex, then Sam, at one satellite in one conversation. Sam's turn must not be answered
    # with what Alex's turn told the main agent.
    entry = await add_router(
        hass, options_with(memory={"shared": False, "use_global": False})
    )
    memory = await async_get_memory(hass)
    await memory.async_add(entry_key(entry.entry_id, "owner"), ALEX_FACT, "told")
    main_agent = ChatLogMainAgent()
    await hass.data[conversation.DATA_COMPONENT].async_add_entities([main_agent])
    device_id = await kitchen_satellite(hass, "owner")
    router = router_entity_id(hass, entry)

    first = await conversation.async_converse(
        hass, "Hello", None, Context(), agent_id=router, device_id=device_id
    )
    hass.states.async_set(f"input_text.voice_speaker_{device_id}", "partner")
    await conversation.async_converse(
        hass, "Hi there", first.conversation_id, Context(), agent_id=router, device_id=device_id
    )

    alex_prompt, sam_prompt = main_agent.system_prompts
    assert ALEX_FACT in alex_prompt
    assert "The person speaking now is Sam." in sam_prompt
    for alex_line in ("The person speaking now is Alex.", "The person speaking is Alex."):
        assert alex_line not in sam_prompt
    assert ALEX_FACT not in sam_prompt


def warnings_about_people(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    lines = router_status(hass, entry, entry.options)
    return [
        line["text"] for line in lines if line["level"] == "warn" and line["section"] == "people"
    ]


NO_USER_WARNING = (
    "No Home Assistant user is chosen for anyone: anyone logged in who types to this "
    "assistant counts as {who}."
)


@pytest.mark.parametrize(
    ("no_helper_role", "who"),
    [("owner", "Alex"), ("wife", "Sam")],
)
async def test_regression_security_status_warns_when_no_user_is_chosen_for_anyone(
    hass: HomeAssistant, no_helper_role: str, who: str
) -> None:
    entry = await add_router(hass, options_with(advanced={"no_helper_role": no_helper_role}))

    assert warnings_about_people(hass, entry) == [NO_USER_WARNING.format(who=who)]


async def test_regression_security_status_names_a_person_without_a_name_by_number(
    hass: HomeAssistant,
) -> None:
    entry = await add_router(hass, options_with(owner={"name": ""}))

    assert warnings_about_people(hass, entry) == [
        "Person 1 has no name.",
        NO_USER_WARNING.format(who="Person 1"),
    ]


async def test_regression_security_status_does_not_warn_when_the_default_is_a_stranger(
    hass: HomeAssistant,
) -> None:
    entry = await add_router(hass, options_with(advanced={"no_helper_role": "stranger"}))

    assert warnings_about_people(hass, entry) == []


async def test_regression_security_status_does_not_warn_when_a_user_is_chosen(
    hass: HomeAssistant,
) -> None:
    entry = await add_router(hass, options_with(partner={"user": ALEX_USER}))

    assert warnings_about_people(hass, entry) == []


async def test_regression_security_odd_stored_agents_get_fixed_lines(
    hass: HomeAssistant,
) -> None:
    entry = await refusing_router(hass, REFUSAL_AGENT)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "agents": "junk"})
    agents = FakeAgents()

    assert await stranger_asks(hass, entry, agents) == REFUSED
    assert agents.calls == []


async def test_regression_security_another_router_with_odd_agents_cannot_break_a_refusal(
    hass: HomeAssistant,
) -> None:
    other = await add_router(hass, title="Office assistant")
    hass.config_entries.async_update_entry(other, options={**other.options, "agents": ["junk"]})
    entry = await refusing_router(hass, REFUSAL_AGENT)
    agents = FakeAgents()

    assert await stranger_asks(hass, entry, agents) == REFUSED
    assert agents.calls == []


# Home Assistant's own hidden users, such as the one the Voice over IP integration puts on every
# phone call, cannot be chosen for a person. Their requests count as a device without speaker
# recognition, and a satellite's helper is never trusted for them.


async def test_regression_security_voip_call_counts_as_a_device_without_recognition(
    hass: HomeAssistant,
) -> None:
    voip_user = await hass.auth.async_create_system_user("Voice over IP")
    entry = await add_router(
        hass,
        options_with(owner={"user": ALEX_USER}, advanced={"no_helper_role": "owner"}),
    )
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, "What's the weather?", user_id=voip_user.id)

    assert "The person speaking is Alex." in agents.main_prompt


async def test_system_user_is_refused_when_unrecognised_devices_are(
    hass: HomeAssistant,
) -> None:
    voip_user = await hass.auth.async_create_system_user("Voice over IP")
    entry = await add_router(
        hass,
        options_with(owner={"user": ALEX_USER}, advanced={"no_helper_role": "stranger"}),
    )
    agents = FakeAgents()

    with agents.patched():
        result = await ask(hass, entry, "What's the weather?", user_id=voip_user.id)

    assert result.response.speech["plain"]["speech"] == "The answer is no."
    assert agents.calls_to(MAIN_AGENT) == []


async def test_system_user_naming_a_satellite_is_not_its_last_speaker(
    hass: HomeAssistant,
) -> None:
    # An add-on calls through a system user too: naming the satellite Alex last spoke to must
    # not make it Alex.
    addon_user = await hass.auth.async_create_system_user("Some add-on")
    entry = await add_router(
        hass,
        options_with(owner={"user": ALEX_USER}, advanced={"no_helper_role": "stranger"}),
    )
    device_id = await kitchen_satellite(hass, "owner")
    agents = FakeAgents()

    with agents.patched():
        result = await ask(hass, entry, "Who am I?", device_id=device_id, user_id=addon_user.id)

    assert result.response.speech["plain"]["speech"] == "The answer is no."
    assert agents.calls_to(MAIN_AGENT) == []
