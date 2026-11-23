# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for when the lists shared by all assistants may be used.

Those lists are keyed by role, so a role's list is only used while that role is the same
person in every router that uses the shared lists.
"""

import json
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager.memory import (
    async_get_memory,
    entry_key,
    global_key,
    shared_list_conflicts,
    shared_list_ok,
)
from custom_components.voice_assistant_manager.status import router_status

from .router_helpers import MEMORY_AGENT, FakeAgents, add_router, ask, options_with

CAT = "Has a cat."
TEA = "Runs on Sundays."
CONFLICT_LINE = (
    "Person 1 is not the same person on every assistant (Kitchen assistant, Office), "
    "so the list shared by all assistants is not used for them."
)


def person_1(user: str, name: str, **memory: Any) -> dict[str, Any]:
    owner = {"user": user, "name": name, "persona": f"The person speaking is {name}."}
    return options_with(owner=owner, memory=memory)


async def kitchen_and_office(
    hass: HomeAssistant, office_user: str, office_name: str, **office_memory: Any
) -> tuple[MockConfigEntry, MockConfigEntry]:
    """Kitchen has Alex (u-alex) as Person 1; Office has the Person 1 given."""
    kitchen = await add_router(hass, person_1("u-alex", "Alex"))
    office = await add_router(hass, person_1(office_user, office_name, **office_memory), "Office")
    return kitchen, office


async def remember_globally(hass: HomeAssistant) -> None:
    memory = await async_get_memory(hass)
    await memory.async_add(global_key("owner"), CAT, "told")
    await memory.async_add(global_key("wife"), TEA, "told")


async def prompt_of(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    agents = FakeAgents()
    with agents.patched():
        await ask(hass, entry, "What should I do tonight?")
    return agents.main_prompt


async def texts(hass: HomeAssistant, key: str) -> list[tuple[str, str, bool]]:
    memory = await async_get_memory(hass)
    return [
        (item["text"], item["source"], bool(item.get("private"))) for item in memory.items(key)
    ]


def warnings(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    return [
        line["text"]
        for line in router_status(hass, entry, entry.options)
        if line["level"] == "warn" and line["section"] == "memory"
    ]


# The rule.


async def test_regression_shared_lists_two_people_as_person_1_conflict(hass: HomeAssistant):
    await kitchen_and_office(hass, "u-sam", "Sam")

    assert shared_list_ok(hass, "owner") is False
    assert shared_list_conflicts(hass, "owner") == ["Kitchen assistant", "Office"]


async def test_regression_shared_lists_only_the_role_that_differs_is_affected(
    hass: HomeAssistant,
):
    await kitchen_and_office(hass, "u-sam", "Sam")

    assert shared_list_ok(hass, "wife") is True
    assert shared_list_conflicts(hass, "wife") == []


async def test_regression_shared_lists_same_user_with_another_name_conflicts(
    hass: HomeAssistant,
):
    await kitchen_and_office(hass, "u-alex", "Sam")

    assert shared_list_ok(hass, "owner") is False


async def test_regression_shared_lists_same_name_with_another_user_conflicts(
    hass: HomeAssistant,
):
    await kitchen_and_office(hass, "u-other", "Alex")

    assert shared_list_ok(hass, "owner") is False


async def test_regression_shared_lists_names_match_without_case_or_spaces(hass: HomeAssistant):
    await kitchen_and_office(hass, "u-alex", "  aLEX ")

    assert shared_list_ok(hass, "owner") is True


async def test_regression_shared_lists_a_router_without_a_user_does_not_conflict_on_users(
    hass: HomeAssistant,
):
    await kitchen_and_office(hass, "", "Alex")

    assert shared_list_ok(hass, "owner") is True


async def test_regression_shared_lists_a_router_with_shared_lists_off_does_not_count(
    hass: HomeAssistant,
):
    await kitchen_and_office(hass, "u-sam", "Sam", use_global=False)

    assert shared_list_ok(hass, "owner") is True


# The voice side.


async def test_regression_shared_lists_different_people_get_no_shared_facts_in_the_prompt(
    hass: HomeAssistant,
):
    kitchen, office = await kitchen_and_office(hass, "u-sam", "Sam")
    await remember_globally(hass)

    kitchen_prompt = await prompt_of(hass, kitchen)
    office_prompt = await prompt_of(hass, office)

    assert CAT not in kitchen_prompt
    assert CAT not in office_prompt


async def test_regression_shared_lists_the_same_person_still_gets_shared_facts(
    hass: HomeAssistant,
):
    kitchen, office = await kitchen_and_office(hass, "u-alex", "Alex")
    await remember_globally(hass)

    assert CAT in await prompt_of(hass, kitchen)
    assert CAT in await prompt_of(hass, office)


async def test_regression_shared_lists_a_role_that_agrees_keeps_its_shared_facts(
    hass: HomeAssistant,
):
    kitchen, _ = await kitchen_and_office(hass, "u-sam", "Sam")
    await remember_globally(hass)

    # Sam is Person 2 in both routers, so what Sam told is still shown to Person 1.
    assert TEA in await prompt_of(hass, kitchen)


async def test_regression_shared_lists_remember_for_all_lands_in_this_routers_list(
    hass: HomeAssistant,
):
    kitchen, _ = await kitchen_and_office(hass, "u-sam", "Sam")
    fact = {"text": "Is vegetarian.", "scope": "global", "source": "told", "private": True}
    agents = FakeAgents(memory_reply=json.dumps({"add": [fact], "remove": []}))

    with agents.patched():
        await ask(hass, kitchen, "Remember for all the assistants that I am vegetarian")
        await hass.async_block_till_done(wait_background_tasks=True)

    own = await texts(hass, entry_key(kitchen.entry_id, "owner"))
    assert own == [("Is vegetarian.", "told", True)]
    assert await texts(hass, global_key("owner")) == []


async def test_regression_shared_lists_the_memory_agent_is_not_shown_shared_facts(
    hass: HomeAssistant,
):
    kitchen, _ = await kitchen_and_office(hass, "u-sam", "Sam")
    await remember_globally(hass)
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, kitchen, "Remember that I like jazz")
        await hass.async_block_till_done(wait_background_tasks=True)

    (call,) = agents.calls_to(MEMORY_AGENT)
    assert CAT not in call["extra_system_prompt"]
    assert "There is no memory shared by all assistants." in call["extra_system_prompt"]


async def test_regression_shared_lists_the_same_person_still_saves_for_all(
    hass: HomeAssistant,
):
    kitchen, _ = await kitchen_and_office(hass, "u-alex", "Alex")
    fact = {"text": "Is vegetarian.", "scope": "global", "source": "told"}
    agents = FakeAgents(memory_reply=json.dumps({"add": [fact], "remove": []}))

    with agents.patched():
        await ask(hass, kitchen, "Remember for all the assistants that I am vegetarian")
        await hass.async_block_till_done(wait_background_tasks=True)

    assert await texts(hass, global_key("owner")) == [("Is vegetarian.", "told", False)]


# The admin page.


async def test_regression_shared_lists_the_overview_warns_on_every_affected_router(
    hass: HomeAssistant,
):
    kitchen, office = await kitchen_and_office(hass, "u-sam", "Sam")

    assert warnings(hass, kitchen) == [CONFLICT_LINE]
    assert warnings(hass, office) == [CONFLICT_LINE]


async def test_regression_shared_lists_no_warning_when_it_is_the_same_person(
    hass: HomeAssistant,
):
    kitchen, _ = await kitchen_and_office(hass, "u-alex", "Alex")

    assert warnings(hass, kitchen) == []


async def test_regression_shared_lists_no_warning_on_a_router_with_shared_lists_off(
    hass: HomeAssistant,
):
    await add_router(hass, person_1("u-alex", "Alex"))
    await add_router(hass, person_1("u-sam", "Sam"), "Office")
    hall = await add_router(hass, person_1("u-sam", "Sam", use_global=False), "Hall")

    assert warnings(hass, hall) == []
