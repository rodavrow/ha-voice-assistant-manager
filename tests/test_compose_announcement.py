# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the voice_assistant_manager.compose_announcement service."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.conversation.chat_log import DATA_CHAT_LOGS
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import announce
from custom_components.voice_assistant_manager.const import DOMAIN, REPLY_AGENT_TIMEOUT
from custom_components.voice_assistant_manager.memory import async_get_memory, entry_key, global_key

from .router_helpers import (
    MAIN_AGENT,
    MEMORY_AGENT,
    REFUSAL_AGENT,
    FakeAgents,
    add_router,
    options_with,
    router_entity_id,
)

SATELLITE = "assist_satellite.kitchen_satellite"
PIPELINE_SELECT = "select.kitchen_satellite_assistant"
REMINDER = "Call the plumber about the boiler at 5 pm"
WITH_WRITER = {"agent": REFUSAL_AGENT}


def add_satellite(hass: HomeAssistant, pipeline_name: str | None = "Kitchen pipeline") -> None:
    """Register a satellite with a primary pipeline select and a second wake word select."""
    owner = MockConfigEntry(domain="test")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("test", "kitchen")}
    )
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "assist_satellite", "test", "kitchen-sat", device_id=device.id,
        suggested_object_id="kitchen_satellite",
    )
    registry.async_get_or_create(
        "select", "test", "kitchen-pipeline-2", device_id=device.id,
        suggested_object_id="kitchen_satellite_assistant_2",
    )
    hass.states.async_set("select.kitchen_satellite_assistant_2", "Office pipeline")
    if pipeline_name is not None:
        registry.async_get_or_create(
            "select", "test", "kitchen-pipeline", device_id=device.id,
            suggested_object_id="kitchen_satellite_assistant",
        )
        hass.states.async_set(PIPELINE_SELECT, pipeline_name)


def pipeline(name: str, engine: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, conversation_engine=engine, conversation_language="en")


class Pipelines:
    """Stands in for Home Assistant's pipeline store."""

    def __init__(self, hass, kitchen_engine: str, preferred_engine: str = "conversation.other"):
        self.all = [
            pipeline("Office pipeline", "conversation.home_assistant"),
            pipeline("Kitchen pipeline", kitchen_engine),
        ]
        self.preferred = pipeline("Preferred pipeline", preferred_engine)

    def patched(self):
        return (
            patch.object(announce, "_pipelines", lambda hass: self.all),
            patch.object(announce, "_preferred_pipeline", lambda hass: self.preferred),
        )


async def compose(
    hass: HomeAssistant,
    pipelines: Pipelines,
    agents: FakeAgents,
    context: Context | None = None,
    **data,
) -> dict:
    request = {"satellite": SATELLITE, "kind": "reminder", "text": REMINDER, "recipient": "alex"}
    all_pipelines, preferred = pipelines.patched()
    with agents.patched(), all_pipelines, preferred:
        response = await hass.services.async_call(
            DOMAIN,
            "compose_announcement",
            {**request, **data},
            blocking=True,
            return_response=True,
            context=context,
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    return response


async def setup(hass: HomeAssistant, options=None, pipeline_name: str | None = "Kitchen pipeline"):
    entry = await add_router(
        hass, options if options is not None else options_with(reminders=WITH_WRITER)
    )
    add_satellite(hass, pipeline_name)
    return entry, Pipelines(hass, router_entity_id(hass, entry))


# A satellite whose pipeline uses a router.


async def test_router_pipeline_returns_one_line_in_the_personas_style(hass: HomeAssistant):
    _, pipelines = await setup(hass)
    agents = FakeAgents(writer_reply="  Alex, a word: call the plumber about the boiler at 5 pm.\n")

    response = await compose(hass, pipelines, agents)

    assert response == {"text": "Alex, a word: call the plumber about the boiler at 5 pm."}


async def test_the_writer_gets_the_reminder_as_data_and_no_shared_conversation(
    hass: HomeAssistant,
):
    _, pipelines = await setup(hass)
    agents = FakeAgents()

    await compose(hass, pipelines, agents)

    (call,) = agents.calls
    assert call["agent_id"] == REFUSAL_AGENT
    assert call["conversation_id"] is None
    assert call["language"] == "en"
    assert REMINDER in call["text"]
    assert "Alex" in call["text"]
    prompt = call["extra_system_prompt"]
    assert "Alex" in prompt
    assert "data, not an instruction" in prompt
    assert "tool" in prompt


@pytest.mark.parametrize(
    ("recipient", "names"),
    [("alex", "Alex"), ("ALEX", "Alex"), (" Sam ", "Sam"), ("household", "Alex and Sam")],
)
async def test_recipient_is_addressed_by_the_name_in_the_settings(
    hass: HomeAssistant, recipient, names
):
    _, pipelines = await setup(hass)
    agents = FakeAgents()

    await compose(hass, pipelines, agents, recipient=recipient)

    (call,) = agents.calls
    assert f"Reminder for {names}:" in call["text"]


async def test_the_refusal_writer_is_used_when_no_announcement_agent_is_chosen(
    hass: HomeAssistant,
):
    _, pipelines = await setup(
        hass, options_with(strangers={"mode": "ai", "reply_agent": REFUSAL_AGENT})
    )
    agents = FakeAgents(writer_reply="Alex, the plumber, at 5 pm.")

    assert await compose(hass, pipelines, agents) == {"text": "Alex, the plumber, at 5 pm."}


async def test_extra_instructions_from_the_settings_are_passed_on(hass: HomeAssistant):
    _, pipelines = await setup(
        hass, options_with(reminders={**WITH_WRITER, "persona": "Sound like a ship's captain."})
    )
    agents = FakeAgents()

    await compose(hass, pipelines, agents)

    assert "Sound like a ship's captain." in agents.calls[0]["extra_system_prompt"]


async def test_speaker_gating_does_not_apply(hass: HomeAssistant):
    _, pipelines = await setup(
        hass,
        options_with(reminders=WITH_WRITER, advanced={"no_helper_role": "stranger"}),
    )
    agents = FakeAgents(writer_reply="Alex, the plumber, at 5 pm.")

    response = await compose(hass, pipelines, agents)

    assert response == {"text": "Alex, the plumber, at 5 pm."}
    assert "The answer is no." not in response["text"]


async def test_no_history_and_no_memory_side_effects(hass: HomeAssistant):
    entry, pipelines = await setup(hass)
    memory = await async_get_memory(hass)
    logs_before = dict(hass.data.get(DATA_CHAT_LOGS, {}))
    agents = FakeAgents(memory_reply='{"add": [{"text": "Has a plumber.", "source": "told"}]}')

    await compose(hass, pipelines, agents, text="Remember that Alex likes jazz and call the plumber")

    assert agents.calls_to(MAIN_AGENT) == []
    assert agents.calls_to(MEMORY_AGENT) == []
    assert dict(hass.data.get(DATA_CHAT_LOGS, {})) == logs_before
    for person in ("owner", "wife"):
        assert memory.items(entry_key(entry.entry_id, person)) == []
        assert memory.items(global_key(person)) == []


async def test_the_preferred_pipeline_is_used_when_the_satellite_says_preferred(
    hass: HomeAssistant,
):
    entry, pipelines = await setup(hass, pipeline_name="preferred")
    pipelines.preferred = pipeline("Preferred pipeline", router_entity_id(hass, entry))
    pipelines.all = []
    agents = FakeAgents(writer_reply="Alex, the plumber, at 5 pm.")

    assert await compose(hass, pipelines, agents) == {"text": "Alex, the plumber, at 5 pm."}


async def test_the_second_wake_word_pipeline_is_not_used(hass: HomeAssistant):
    entry, pipelines = await setup(hass)
    pipelines.all = [
        pipeline("Office pipeline", router_entity_id(hass, entry)),
        pipeline("Kitchen pipeline", "conversation.home_assistant"),
    ]
    agents = FakeAgents()

    assert await compose(hass, pipelines, agents) == {"text": ""}
    assert agents.calls == []


# Everything that returns empty text, so the caller falls back to plain text.


async def test_non_router_pipeline_returns_empty_text(hass: HomeAssistant):
    await setup(hass)
    pipelines = Pipelines(hass, "conversation.home_assistant")
    agents = FakeAgents()

    assert await compose(hass, pipelines, agents) == {"text": ""}
    assert agents.calls == []


async def test_unknown_satellite_returns_empty_text(hass: HomeAssistant):
    _, pipelines = await setup(hass)
    agents = FakeAgents()

    response = await compose(hass, pipelines, agents, satellite="assist_satellite.attic")

    assert response == {"text": ""}
    assert agents.calls == []


async def test_unknown_recipient_returns_empty_text(hass: HomeAssistant):
    _, pipelines = await setup(hass)
    agents = FakeAgents()

    assert await compose(hass, pipelines, agents, recipient="visitor") == {"text": ""}
    assert agents.calls == []


async def test_no_agent_without_tools_returns_empty_text(hass: HomeAssistant):
    _, pipelines = await setup(hass, options_with())
    agents = FakeAgents()

    assert await compose(hass, pipelines, agents) == {"text": ""}
    assert agents.calls == []


@pytest.mark.parametrize("agent", [MAIN_AGENT, "router"])
async def test_main_agent_or_a_router_as_writer_returns_empty_text(hass: HomeAssistant, agent):
    entry, pipelines = await setup(hass)
    writer = router_entity_id(hass, entry) if agent == "router" else agent
    hass.config_entries.async_update_entry(entry, options=options_with(reminders={"agent": writer}))
    agents = FakeAgents()

    assert await compose(hass, pipelines, agents) == {"text": ""}
    assert agents.calls == []


async def test_timeout_returns_empty_text(hass: HomeAssistant):
    _, pipelines = await setup(hass)
    agents = FakeAgents(writer_gate=asyncio.Event())

    with patch.object(announce, "COMPOSE_TIMEOUT", 0.01):
        response = await compose(hass, pipelines, agents)

    assert response == {"text": ""}


async def test_writer_error_returns_empty_text(hass: HomeAssistant):
    _, pipelines = await setup(hass)

    assert await compose(hass, pipelines, FakeAgents(writer_error=True)) == {"text": ""}


async def test_writer_exception_returns_empty_text(hass: HomeAssistant):
    _, pipelines = await setup(hass)
    agents = FakeAgents(writer_raises=RuntimeError("model is down"))

    assert await compose(hass, pipelines, agents) == {"text": ""}


@pytest.mark.parametrize("reply", ["", "   ", "x" * 401])
async def test_empty_or_rambling_reply_returns_empty_text(hass: HomeAssistant, reply):
    _, pipelines = await setup(hass)

    assert await compose(hass, pipelines, FakeAgents(writer_reply=reply)) == {"text": ""}


async def test_pipeline_lookup_failure_returns_empty_text(hass: HomeAssistant):
    await setup(hass)
    add_agents = FakeAgents()

    with (
        add_agents.patched(),
        patch.object(announce, "_pipelines", side_effect=RuntimeError("no pipelines")),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            "compose_announcement",
            {"satellite": SATELLITE, "kind": "reminder", "text": REMINDER, "recipient": "alex"},
            blocking=True,
            return_response=True,
        )

    assert response == {"text": ""}


async def test_a_kind_other_than_reminder_is_rejected(hass: HomeAssistant):
    _, pipelines = await setup(hass)

    with pytest.raises(Exception):  # noqa: B017, PT011 - schema validation error
        await compose(hass, pipelines, FakeAgents(), kind="weather")


# Correction A: the assistant that took the reminder decides the persona.


async def two_routers(hass: HomeAssistant):
    """Two routers with different persona texts, and a satellite whose pipeline uses the first."""
    jeeves = await add_router(
        hass,
        options_with(
            reminders=WITH_WRITER,
            owner={"name": "Alex", "persona": "Alex likes dry wit."},
            partner={"name": "Sam", "persona": "Sam likes it brief."},
        ),
        title="Jeeves (by speaker)",
    )
    ada = await add_router(
        hass,
        options_with(reminders=WITH_WRITER, owner={"name": "Alex", "persona": "Alex likes puns."}),
        title="Ada (by speaker)",
    )
    add_satellite(hass)
    return jeeves, ada, Pipelines(hass, router_entity_id(hass, jeeves))


@pytest.mark.parametrize("assistant", ["ada", "Ada", " ADA ", "ada (by speaker)"])
async def test_router_is_chosen_by_assistant_name_before_the_satellite(
    hass: HomeAssistant, assistant
):
    _, _, pipelines = await two_routers(hass)
    agents = FakeAgents(writer_reply="Alex, the plumber, at 5 pm.")

    response = await compose(hass, pipelines, agents, assistant=assistant)

    assert response == {"text": "Alex, the plumber, at 5 pm."}
    prompt = agents.calls[0]["extra_system_prompt"]
    assert "Alex likes puns." in prompt
    assert "Alex likes dry wit." not in prompt


async def test_router_is_chosen_by_the_satellite_when_assistant_is_empty(hass: HomeAssistant):
    _, _, pipelines = await two_routers(hass)
    agents = FakeAgents()

    await compose(hass, pipelines, agents, assistant="")

    assert "Alex likes dry wit." in agents.calls[0]["extra_system_prompt"]


async def test_router_can_be_given_its_own_assistant_name(hass: HomeAssistant):
    jeeves, _, pipelines = await two_routers(hass)
    hass.config_entries.async_update_entry(
        jeeves, options=options_with(reminders={**WITH_WRITER, "assistant": "Butler"})
    )
    agents = FakeAgents(writer_reply="Alex, the plumber, at 5 pm.")

    assert await compose(hass, pipelines, agents, assistant="butler") == {
        "text": "Alex, the plumber, at 5 pm."
    }
    assert await compose(hass, pipelines, FakeAgents(), assistant="jeeves") == {"text": ""}


async def test_an_assistant_without_a_router_returns_empty_text(hass: HomeAssistant):
    _, _, pipelines = await two_routers(hass)
    agents = FakeAgents()

    assert await compose(hass, pipelines, agents, assistant="hal") == {"text": ""}
    assert agents.calls == []


async def test_two_routers_with_the_same_assistant_name_return_empty_text(hass: HomeAssistant):
    jeeves, ada, pipelines = await two_routers(hass)
    for entry in (jeeves, ada):
        hass.config_entries.async_update_entry(
            entry, options=options_with(reminders={**WITH_WRITER, "assistant": "twin"})
        )
    agents = FakeAgents()

    assert await compose(hass, pipelines, agents, assistant="twin") == {"text": ""}
    assert agents.calls == []


@pytest.mark.parametrize(
    ("recipient", "included", "left_out"),
    [
        ("alex", "Alex likes dry wit.", "Sam likes it brief."),
        ("sam", "Sam likes it brief.", "Alex likes dry wit."),
    ],
)
async def test_the_recipients_persona_text_is_used(
    hass: HomeAssistant, recipient, included, left_out
):
    _, _, pipelines = await two_routers(hass)
    agents = FakeAgents()

    await compose(hass, pipelines, agents, assistant="jeeves", recipient=recipient)

    prompt = agents.calls[0]["extra_system_prompt"]
    assert included in prompt
    assert left_out not in prompt


async def test_household_addresses_both_names_without_one_persons_persona(hass: HomeAssistant):
    _, _, pipelines = await two_routers(hass)
    agents = FakeAgents()

    await compose(hass, pipelines, agents, assistant="jeeves", recipient="household")

    prompt = agents.calls[0]["extra_system_prompt"]
    assert "Alex and Sam" in prompt
    assert "Alex likes dry wit." not in prompt
    assert "Sam likes it brief." not in prompt


async def test_the_writer_is_told_to_stay_in_character_and_keep_it_short(hass: HomeAssistant):
    _, _, pipelines = await two_routers(hass)
    agents = FakeAgents()

    await compose(hass, pipelines, agents, assistant="jeeves")

    prompt = agents.calls[0]["extra_system_prompt"]
    assert "in character" in prompt
    assert "at most two sentences" in prompt
    assert "do not ask a question" in prompt
    assert "Nobody has spoken to you" in prompt


async def test_the_main_agent_is_never_called_even_when_named_as_the_writer(hass: HomeAssistant):
    jeeves, _, pipelines = await two_routers(hass)
    hass.config_entries.async_update_entry(
        jeeves, options=options_with(reminders={"agent": MAIN_AGENT})
    )
    agents = FakeAgents()

    assert await compose(hass, pipelines, agents, assistant="jeeves") == {"text": ""}
    assert agents.calls == []


def test_gives_up_after_the_reply_agent_timeout():
    assert announce.COMPOSE_TIMEOUT == REPLY_AGENT_TIMEOUT == 6


# Who may call it: administrators, and automations or scripts that run without a user.


async def test_regression_security_a_non_admin_user_cannot_compose_a_line(
    hass: HomeAssistant, hass_read_only_user
):
    _, pipelines = await setup(hass)
    agents = FakeAgents(writer_reply="Alex, the boiler.")

    with pytest.raises(Unauthorized):
        await compose(hass, pipelines, agents, Context(user_id=hass_read_only_user.id))

    assert agents.calls == []


async def test_a_call_without_a_user_composes_a_line(hass: HomeAssistant):
    _, pipelines = await setup(hass)

    response = await compose(hass, pipelines, FakeAgents(writer_reply="Alex, the boiler."))

    assert response == {"text": "Alex, the boiler."}


async def test_an_admin_user_composes_a_line(hass: HomeAssistant, hass_admin_user):
    _, pipelines = await setup(hass)

    response = await compose(
        hass,
        pipelines,
        FakeAgents(writer_reply="Alex, the boiler."),
        Context(user_id=hass_admin_user.id),
    )

    assert response == {"text": "Alex, the boiler."}


# Showing which assistant speaks, on devices with a character select.


@pytest.fixture
def shown(hass: HomeAssistant):
    """Records whom announcement_look was asked to show, by entry id (None: the default)."""
    calls: list[tuple[str, str | None]] = []

    async def show(hass, satellite, entry):
        calls.append((satellite, entry.entry_id if entry is not None else None))

    with patch.object(announce.announcement_look, "async_show_assistant", show):
        yield calls


async def test_a_written_line_shows_the_assistant_that_wrote_it(hass: HomeAssistant, shown):
    _, ada, pipelines = await two_routers(hass)

    await compose(hass, pipelines, FakeAgents(writer_reply="Alex, the boiler."), assistant="ada")

    assert shown == [(SATELLITE, ada.entry_id)]


async def test_no_line_shows_nothing(hass: HomeAssistant, shown):
    _, _, pipelines = await two_routers(hass)

    await compose(hass, pipelines, FakeAgents(writer_reply=""), assistant="ada")

    assert shown == []


async def prepare(hass: HomeAssistant, context: Context | None = None, **data) -> None:
    await hass.services.async_call(
        DOMAIN,
        "prepare_announcement",
        {"satellite": SATELLITE, **data},
        blocking=True,
        context=context,
    )


async def test_prepare_shows_the_named_assistant(hass: HomeAssistant, shown):
    _, ada, _ = await two_routers(hass)

    await prepare(hass, assistant="Ada")

    assert shown == [(SATELLITE, ada.entry_id)]


@pytest.mark.parametrize("data", [{}, {"assistant": ""}, {"assistant": "nobody"}])
async def test_prepare_without_a_known_assistant_shows_the_default(
    hass: HomeAssistant, shown, data
):
    await two_routers(hass)

    await prepare(hass, **data)

    assert shown == [(SATELLITE, None)]


async def test_regression_security_a_non_admin_user_cannot_prepare_an_announcement(
    hass: HomeAssistant, hass_read_only_user, shown
):
    await two_routers(hass)

    with pytest.raises(Unauthorized):
        await prepare(hass, Context(user_id=hass_read_only_user.id), assistant="Ada")

    assert shown == []
