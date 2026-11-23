# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for working out which routers, and which role in each, a user has."""

from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.mine import (
    household_calendars,
    my_router,
    my_routers,
    own_calendars,
    owns_global_list,
)


def add_entry(
    hass: HomeAssistant,
    title: str,
    owner: dict[str, Any],
    partner: dict[str, Any],
    reminders: dict[str, Any] | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={"agent": "conversation.main"},
        options={
            "agents": {"agent": "conversation.main"},
            "owner": owner,
            "partner": partner,
            "reminders": reminders or {},
        },
    )
    entry.add_to_hass(hass)
    return entry


def test_a_linked_user_gets_their_router_and_role(hass: HomeAssistant) -> None:
    entry = add_entry(hass, "Kitchen", {"user": "u-alex", "name": "Alex"}, {"user": "u-sam"})

    routers = my_routers(hass, "u-alex")

    assert routers == [
        {
            "entry_id": entry.entry_id,
            "title": "Kitchen",
            "role": "owner",
            "memory_person": "owner",
            "name": "Alex",
            "other_name": "Person 2",
        }
    ]


def test_an_unlinked_user_gets_nothing(hass: HomeAssistant) -> None:
    add_entry(hass, "Kitchen", {"user": "u-alex"}, {"user": "u-sam"})

    assert my_routers(hass, "u-nobody") == []


def test_no_user_gets_nothing(hass: HomeAssistant) -> None:
    add_entry(hass, "Kitchen", {"user": ""}, {})

    assert my_routers(hass, None) == []
    assert my_routers(hass, "") == []


def test_a_user_who_is_person_1_here_and_person_2_there_gets_both_roles(
    hass: HomeAssistant,
) -> None:
    add_entry(hass, "Study", {"user": "u-sam", "name": "Sam"}, {"user": "u-alex"})
    add_entry(hass, "Kitchen", {"user": "u-alex", "name": "Alex"}, {"user": "u-sam"})

    routers = my_routers(hass, "u-alex")

    assert [(r["title"], r["role"], r["memory_person"]) for r in routers] == [
        ("Kitchen", "owner", "owner"),
        ("Study", "partner", "wife"),
    ]


def test_my_router_refuses_a_router_the_user_is_not_in(hass: HomeAssistant) -> None:
    mine = add_entry(hass, "Kitchen", {"user": "u-alex"}, {})
    other = add_entry(hass, "Study", {"user": "u-sam"}, {})

    assert my_router(hass, "u-alex", mine.entry_id) is not None
    assert my_router(hass, "u-alex", other.entry_id) is None
    assert my_router(hass, "u-alex", "no-such-entry") is None
    assert my_router(hass, None, mine.entry_id) is None


def test_partner_maps_to_the_wife_memory_lists(hass: HomeAssistant) -> None:
    entry = add_entry(hass, "Kitchen", {"name": "Alex"}, {"user": "u-sam", "name": "Sam"})

    router = my_router(hass, "u-sam", entry.entry_id)

    assert router is not None
    assert (router["role"], router["memory_person"]) == ("partner", "wife")
    assert (router["name"], router["other_name"]) == ("Sam", "Alex")


def test_own_and_household_calendars() -> None:
    options = {
        "reminders": {
            "owner_calendars": ["calendar.alex"],
            "partner_calendars": ["calendar.sam"],
            "household_calendars": ["calendar.home"],
        }
    }

    assert own_calendars(options, "owner") == ["calendar.alex"]
    assert own_calendars(options, "partner") == ["calendar.sam"]
    assert household_calendars(options) == ["calendar.home"]
    assert own_calendars({}, "owner") == []
    assert household_calendars({}) == []


# The "all assistants" lists are keyed by role, shared by every router where that role exists.


def test_regression_final_global_list_is_owned_when_the_user_holds_the_role_everywhere(
    hass: HomeAssistant,
) -> None:
    add_entry(hass, "Kitchen", {"user": "u-alex"}, {"user": "u-sam"})
    add_entry(hass, "Study", {"user": "u-alex"}, {"user": "u-sam"})

    assert owns_global_list(hass, "u-alex", "owner") is True
    assert owns_global_list(hass, "u-sam", "wife") is True


def test_regression_final_global_list_is_not_owned_when_another_user_holds_the_role(
    hass: HomeAssistant,
) -> None:
    add_entry(hass, "Kitchen", {"user": "u-alex"}, {})
    add_entry(hass, "Study", {"user": "u-sam"}, {})

    assert owns_global_list(hass, "u-alex", "owner") is False
    assert owns_global_list(hass, "u-sam", "owner") is False


def test_regression_final_global_list_is_not_owned_when_the_roles_are_swapped(
    hass: HomeAssistant,
) -> None:
    add_entry(hass, "Kitchen", {"user": "u-alex"}, {"user": "u-sam"})
    add_entry(hass, "Study", {"user": "u-sam"}, {"user": "u-alex"})

    for user in ("u-alex", "u-sam"):
        assert owns_global_list(hass, user, "owner") is False
        assert owns_global_list(hass, user, "wife") is False


def test_regression_final_a_router_with_no_user_for_the_role_does_not_count(
    hass: HomeAssistant,
) -> None:
    add_entry(hass, "Kitchen", {"user": "u-alex", "name": "Alex"}, {})
    add_entry(hass, "Study", {"user": ""}, {})
    add_entry(hass, "Hall", {"name": "Alex"}, {})

    assert owns_global_list(hass, "u-alex", "owner") is True


def test_regression_final_global_list_is_not_owned_without_the_role_or_a_user(
    hass: HomeAssistant,
) -> None:
    add_entry(hass, "Kitchen", {"user": "u-alex"}, {})

    assert owns_global_list(hass, "u-alex", "wife") is False
    assert owns_global_list(hass, None, "owner") is False
    assert owns_global_list(hass, "", "owner") is False
