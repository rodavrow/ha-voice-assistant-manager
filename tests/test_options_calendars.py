# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Calendars part of the Configure dialog."""

import copy

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager.const import DOMAIN

from .test_options_flow import (
    EXISTING_OPTIONS,
    MAIN_AGENT,
    NEW_DEFAULTS,
    open_step,
    picker_labels,
    shown_values,
)

WASTE = "calendar.waste"
FAMILY = "calendar.family"
WASTE_ROW = {
    "calendar": WASTE,
    "for": "household",
    "all_day_time": "19:00",
    "all_day_day": "before",
    "lead_minutes": 0,
    "contains": "gft",
    "hint": "Dutch waste collection. Say in English which bin goes out.",
}
NEW_ROW_DEFAULTS = {"for": "household", "all_day_day": "same", "lead_minutes": 0}


async def router(hass: HomeAssistant, *rows: dict) -> MockConfigEntry:
    assert await async_setup_component(hass, "homeassistant", {})
    options = copy.deepcopy(EXISTING_OPTIONS)
    if rows:
        options["calendar_rows"] = [dict(row) for row in rows]
    entry = MockConfigEntry(
        domain=DOMAIN, title="Kitchen assistant", data={"agent": MAIN_AGENT}, options=options
    )
    entry.add_to_hass(hass)
    return entry


async def configure(hass: HomeAssistant, form, values: dict):
    return await hass.config_entries.options.async_configure(form["flow_id"], values)


async def open_row(hass: HomeAssistant, entry: MockConfigEntry, row: str):
    picker = await open_step(hass, entry, "calendars")
    return await configure(hass, picker, {"row": row})


# The Settings form.


async def test_settings_show_the_calendar_defaults(hass: HomeAssistant):
    entry = await router(hass)

    form = await open_step(hass, entry, "settings")

    assert shown_values(form["data_schema"])["calendars"] == {
        "all_day_time": "09:00:00",
        "catch_up_hours": 3,
    }


async def test_calendars_section_comes_right_after_reminders(hass: HomeAssistant):
    entry = await router(hass)

    form = await open_step(hass, entry, "settings")

    sections = [str(marker) for marker in form["data_schema"].schema]
    assert sections.index("calendars") == sections.index("reminders") + 1


async def test_saving_the_settings_keeps_the_calendars(hass: HomeAssistant):
    entry = await router(hass, WASTE_ROW)
    form = await open_step(hass, entry, "settings")

    result = await configure(hass, form, shown_values(form["data_schema"]))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {**EXISTING_OPTIONS, **NEW_DEFAULTS, "calendar_rows": [WASTE_ROW]}


async def test_saving_the_settings_of_a_router_without_calendars_adds_no_list(hass: HomeAssistant):
    entry = await router(hass)
    form = await open_step(hass, entry, "settings")

    await configure(hass, form, shown_values(form["data_schema"]))

    assert "calendar_rows" not in entry.options


# Adding a calendar.


async def test_without_calendars_the_form_for_a_new_one_opens_at_once(hass: HomeAssistant):
    entry = await router(hass)

    form = await open_step(hass, entry, "calendars")

    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "calendar_edit"
    assert shown_values(form["data_schema"]) == NEW_ROW_DEFAULTS


async def test_a_new_calendar_is_saved_and_the_other_settings_stay(hass: HomeAssistant):
    entry = await router(hass)
    form = await open_step(hass, entry, "calendars")

    result = await configure(hass, form, dict(WASTE_ROW))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {**EXISTING_OPTIONS, "calendar_rows": [WASTE_ROW]}


async def test_a_new_calendar_with_only_the_calendar_filled_in_gets_the_defaults(
    hass: HomeAssistant,
):
    entry = await router(hass)
    form = await open_step(hass, entry, "calendars")

    await configure(hass, form, {**NEW_ROW_DEFAULTS, "calendar": FAMILY})

    assert entry.options["calendar_rows"] == [{**NEW_ROW_DEFAULTS, "calendar": FAMILY}]


async def test_texts_are_saved_without_spaces_around_them(hass: HomeAssistant):
    entry = await router(hass)
    form = await open_step(hass, entry, "calendars")
    typed = {"all_day_time": " 7:30 ", "contains": "  ", "hint": " Family things. "}

    await configure(hass, form, {**NEW_ROW_DEFAULTS, "calendar": FAMILY, **typed})

    assert entry.options["calendar_rows"] == [
        {**NEW_ROW_DEFAULTS, "calendar": FAMILY, "all_day_time": "07:30", "hint": "Family things."}
    ]


async def test_a_second_calendar_is_added_after_the_first(hass: HomeAssistant):
    entry = await router(hass, WASTE_ROW)

    form = await open_row(hass, entry, "add")
    await configure(hass, form, {**NEW_ROW_DEFAULTS, "calendar": FAMILY, "for": "owner"})

    assert [row["calendar"] for row in entry.options["calendar_rows"]] == [WASTE, FAMILY]


@pytest.mark.parametrize("time", ["25:00", "seven", "19"])
async def test_a_time_that_cannot_be_read_is_refused(hass: HomeAssistant, time):
    entry = await router(hass)
    form = await open_step(hass, entry, "calendars")

    result = await configure(
        hass, form, {**NEW_ROW_DEFAULTS, "calendar": FAMILY, "all_day_time": time}
    )

    assert result["errors"] == {"all_day_time": "calendar_time_invalid"}
    assert shown_values(result["data_schema"])["all_day_time"] == time
    assert "calendar_rows" not in entry.options


async def test_the_same_calendar_cannot_be_listed_twice_in_one_router(hass: HomeAssistant):
    entry = await router(hass, WASTE_ROW)
    form = await open_row(hass, entry, "add")

    result = await configure(hass, form, {**NEW_ROW_DEFAULTS, "calendar": WASTE})

    assert result["errors"] == {"calendar": "calendar_duplicate"}
    assert entry.options["calendar_rows"] == [WASTE_ROW]


async def test_something_that_is_not_a_calendar_is_refused(hass: HomeAssistant):
    entry = await router(hass)
    form = await open_step(hass, entry, "calendars")

    with pytest.raises(Exception):  # noqa: B017, PT011 - schema validation error
        await configure(hass, form, {**NEW_ROW_DEFAULTS, "calendar": "light.kitchen"})


# Picking, changing and removing one.


async def test_the_picker_lists_the_calendars_with_who_they_are_for(hass: HomeAssistant):
    entry = await router(hass, WASTE_ROW, {"calendar": FAMILY, "for": "partner"})

    picker = await open_step(hass, entry, "calendars")

    assert picker["step_id"] == "calendars"
    assert picker_labels(picker) == {
        WASTE: "calendar.waste (for everyone)",
        FAMILY: "calendar.family (for Sam)",
        "add": "Add a calendar",
    }


async def test_picking_a_calendar_shows_its_settings(hass: HomeAssistant):
    entry = await router(hass, WASTE_ROW)

    form = await open_row(hass, entry, WASTE)

    assert shown_values(form["data_schema"]) == {**WASTE_ROW, "remove": False}


async def test_changing_a_calendar_replaces_its_row(hass: HomeAssistant):
    entry = await router(hass, WASTE_ROW, {"calendar": FAMILY, "for": "partner"})
    form = await open_row(hass, entry, WASTE)

    await configure(hass, form, {**WASTE_ROW, "all_day_time": "18:30", "remove": False})

    assert entry.options["calendar_rows"] == [
        {**WASTE_ROW, "all_day_time": "18:30"},
        {"calendar": FAMILY, "for": "partner"},
    ]


async def test_a_calendar_can_keep_its_own_entity_when_it_is_changed(hass: HomeAssistant):
    entry = await router(hass, WASTE_ROW)
    form = await open_row(hass, entry, WASTE)

    result = await configure(hass, form, {**WASTE_ROW, "remove": False})

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_calendar_cannot_be_changed_into_one_already_listed(hass: HomeAssistant):
    entry = await router(hass, WASTE_ROW, {"calendar": FAMILY, "for": "partner"})
    form = await open_row(hass, entry, WASTE)

    result = await configure(hass, form, {**WASTE_ROW, "calendar": FAMILY, "remove": False})

    assert result["errors"] == {"calendar": "calendar_duplicate"}


async def test_removing_a_calendar_takes_its_row_away(hass: HomeAssistant):
    entry = await router(hass, WASTE_ROW, {"calendar": FAMILY, "for": "partner"})
    form = await open_row(hass, entry, WASTE)

    await configure(hass, form, {**WASTE_ROW, "remove": True})

    assert entry.options["calendar_rows"] == [{"calendar": FAMILY, "for": "partner"}]


# A router whose settings were never saved.


async def test_calendars_ask_for_the_settings_to_be_saved_first(hass: HomeAssistant):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Office assistant", data={"agent": MAIN_AGENT})
    entry.add_to_hass(hass)

    result = await open_step(hass, entry, "calendars")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "settings_first"


async def test_regression_review_a_calendar_with_a_bad_time_can_still_be_removed(
    hass: HomeAssistant,
):
    entry = await router(hass, WASTE_ROW)
    form = await open_row(hass, entry, WASTE)

    await configure(hass, form, {**WASTE_ROW, "all_day_time": "soon", "remove": True})

    assert entry.options["calendar_rows"] == []
