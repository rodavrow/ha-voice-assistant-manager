# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Configure dialog: the menu, the Settings form and the Memory editor."""

import copy

import pytest
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.memory import (
    async_get_memory,
    entry_key,
    global_key,
)

MAIN_AGENT = "conversation.home_assistant"
MEMORY_AGENT = "conversation.memory_writer"
MEMORY_DEFAULTS = {
    "enabled": False,
    "auto_save": True,
    "max_items": 50,
    "use_global": True,
    "shared": True,
}
REMINDER_DEFAULTS = {
    "enabled": False,
    "cancel": True,
    "owner_calendars": [],
    "partner_calendars": [],
    "household_calendars": [],
}
CALENDAR_DEFAULTS = {"all_day_time": "09:00:00", "catch_up_hours": 3}
NEW_DEFAULTS = {
    "memory": MEMORY_DEFAULTS,
    "reminders": REMINDER_DEFAULTS,
    "favorites": {"enabled": False},
    "calendars": CALENDAR_DEFAULTS,
    "chat": {},
}
EXISTING_OPTIONS = {
    "agents": {"agent": MAIN_AGENT},
    "local": {"enabled": True, "leave_questions": False},
    "owner": {"name": "Alex", "persona": "The person speaking is Alex."},
    "partner": {"name": "Sam", "persona": "The person speaking is Sam."},
    "strangers": {"mode": "fixed", "fixed_replies": "The answer is no."},
    "near_miss": {
        "enabled": True,
        "min_score": 0.4,
        "persona": "Ask them to repeat.",
        "fixed_replies": "Say that again?",
    },
    "advanced": {"rule": "Names are labels.", "wait_seconds": 2, "no_helper_role": "stranger"},
}
# What the form shows and saves: the existing settings, defaults for the new ones.
EXPECTED_SETTINGS = {
    **EXISTING_OPTIONS,
    **NEW_DEFAULTS,
    "advanced": {
        # A stored "wait for the voice match" from before 0.5.2 is not kept: it counted from
        # the end of speech, the new setting from the wake word.
        **{k: v for k, v in EXISTING_OPTIONS["advanced"].items() if k != "wait_seconds"},
        "avatar_connection": "on_wake",
        "avatar_stream": True,
        "match_within": 6,
        "follow_up_window": 60,
        "voice_tags": False,
    },
}


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    assert await async_setup_component(hass, "homeassistant", {})
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Kitchen assistant",
        data={"name": "Kitchen assistant", "agent": MAIN_AGENT},
        options=copy.deepcopy(EXISTING_OPTIONS),
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


def shown_values(schema) -> dict:
    """What a form would submit if the person saved it without touching anything."""
    values = {}
    for marker, field in schema.schema.items():
        if isinstance(field, section):
            values[str(marker)] = shown_values(field.schema)
        elif marker.description and "suggested_value" in marker.description:
            values[str(marker)] = marker.description["suggested_value"]
        elif callable(getattr(marker, "default", None)):
            values[str(marker)] = marker.default()
    return values


async def open_step(hass: HomeAssistant, entry: MockConfigEntry, step: str):
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": step}
    )


async def open_list(hass: HomeAssistant, entry: MockConfigEntry, key: str):
    picker = await open_step(hass, entry, "memory")
    return await hass.config_entries.options.async_configure(picker["flow_id"], {"list": key})


def picker_labels(picker) -> dict[str, str]:
    (selector,) = picker["data_schema"].schema.values()
    return {option["value"]: option["label"] for option in selector.config["options"]}


def facts_shown(form) -> str:
    return shown_values(form["data_schema"]).get("facts", "")


# The menu.


async def test_configure_opens_a_menu_with_settings_calendars_and_memory(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert list(result["menu_options"]) == ["settings", "calendars", "memory"]


# The Settings form.


async def test_settings_form_shows_existing_values_and_memory_defaults(hass, entry):
    form = await open_step(hass, entry, "settings")

    assert form["type"] is FlowResultType.FORM
    assert shown_values(form["data_schema"]) == EXPECTED_SETTINGS


async def test_saving_untouched_settings_keeps_options_and_adds_memory_defaults(hass, entry):
    form = await open_step(hass, entry, "settings")

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], shown_values(form["data_schema"])
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == EXPECTED_SETTINGS


async def test_memory_section_comes_right_after_built_in_commands(hass, entry):
    form = await open_step(hass, entry, "settings")

    sections = [str(marker) for marker in form["data_schema"].schema]
    assert sections.index("memory") == sections.index("local") + 1


async def test_settings_for_an_entry_without_options_propose_memory_defaults(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    new_entry = MockConfigEntry(domain=DOMAIN, title="Office assistant", data={"agent": MAIN_AGENT})
    new_entry.add_to_hass(hass)

    form = await open_step(hass, new_entry, "settings")

    assert shown_values(form["data_schema"])["memory"] == MEMORY_DEFAULTS


async def submit_memory_settings(hass, entry, memory_settings):
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["memory"] = {**MEMORY_DEFAULTS, **memory_settings}
    return await hass.config_entries.options.async_configure(form["flow_id"], values)


async def test_memory_can_be_enabled_with_a_separate_agent(hass, entry):
    hass.states.async_set(MEMORY_AGENT, "unknown", {"supported_features": 0})
    result = await submit_memory_settings(hass, entry, {"enabled": True, "agent": MEMORY_AGENT})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["memory"] == {**MEMORY_DEFAULTS, "enabled": True, "agent": MEMORY_AGENT}


async def test_enabling_memory_without_an_agent_is_a_form_error(hass, entry):
    result = await submit_memory_settings(hass, entry, {"enabled": True})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "memory_agent_required"}
    assert entry.options == EXISTING_OPTIONS


async def test_memory_agent_may_not_be_the_main_agent(hass, entry):
    result = await submit_memory_settings(hass, entry, {"enabled": True, "agent": MAIN_AGENT})

    assert result["errors"] == {"base": "memory_agent_invalid"}
    assert entry.options == EXISTING_OPTIONS


async def test_memory_agent_may_not_be_this_router(hass, entry):
    router = er.async_get(hass).async_get_entity_id("conversation", DOMAIN, entry.entry_id)
    assert router is not None

    result = await submit_memory_settings(hass, entry, {"agent": router})

    assert result["errors"] == {"base": "memory_agent_invalid"}


async def test_a_rejected_settings_form_keeps_what_was_typed(hass, entry):
    result = await submit_memory_settings(hass, entry, {"enabled": True, "max_items": 80})

    assert shown_values(result["data_schema"])["memory"]["max_items"] == 80


async def test_same_home_assistant_user_for_both_people_is_a_form_error(
    hass, entry, hass_admin_user
):
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["owner"] = {**values["owner"], "user": hass_admin_user.id}
    values["partner"] = {**values["partner"], "user": hass_admin_user.id}

    result = await hass.config_entries.options.async_configure(form["flow_id"], values)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "same_user"}
    assert entry.options == EXISTING_OPTIONS


async def test_a_home_assistant_user_can_be_chosen_for_one_person(hass, entry, hass_admin_user):
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["owner"] = {**values["owner"], "user": hass_admin_user.id}

    result = await hass.config_entries.options.async_configure(form["flow_id"], values)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["owner"]["user"] == hass_admin_user.id


# The Memory picker.


async def test_picker_lists_four_lists_with_names_and_counts(hass, entry):
    memory = await async_get_memory(hass)
    await memory.async_add(entry_key(entry.entry_id, "owner"), "Likes jazz.", "told")
    await memory.async_add(entry_key(entry.entry_id, "owner"), "Has a cat.", "judged")
    await memory.async_add(global_key("wife"), "Likes tea.", "told")

    picker = await open_step(hass, entry, "memory")

    assert picker["type"] is FlowResultType.FORM
    assert picker_labels(picker) == {
        entry_key(entry.entry_id, "owner"): "Alex: this assistant (2)",
        global_key("owner"): "Alex: all assistants (0)",
        entry_key(entry.entry_id, "wife"): "Sam: this assistant (0)",
        global_key("wife"): "Sam: all assistants (1)",
    }


async def test_picker_falls_back_to_person_1_and_person_2_without_names(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    unnamed = MockConfigEntry(domain=DOMAIN, title="Office assistant", data={"agent": MAIN_AGENT})
    unnamed.add_to_hass(hass)

    picker = await open_step(hass, unnamed, "memory")

    assert list(picker_labels(picker).values()) == [
        "Person 1: this assistant (0)",
        "Person 1: all assistants (0)",
        "Person 2: this assistant (0)",
        "Person 2: all assistants (0)",
    ]


async def test_picker_does_not_show_another_assistants_lists(hass, entry):
    picker = await open_step(hass, entry, "memory")

    assert entry_key("office-entry", "owner") not in picker_labels(picker)


# The Memory editor.


async def test_editor_shows_one_fact_per_line_in_saved_order(hass, entry):
    key = entry_key(entry.entry_id, "owner")
    memory = await async_get_memory(hass)
    await memory.async_add(key, "Likes jazz.", "told")
    await memory.async_add(key, "Has a cat.", "judged")

    form = await open_list(hass, entry, key)

    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "memory_edit"
    assert facts_shown(form) == "Likes jazz.\nHas a cat."


async def test_saving_an_edit_removes_keeps_and_adds_then_returns_to_the_menu(hass, entry):
    key = entry_key(entry.entry_id, "owner")
    memory = await async_get_memory(hass)
    await memory.async_add(key, "Likes jazz.", "judged")
    cat = await memory.async_add(key, "Has a cat.", "judged")
    form = await open_list(hass, entry, key)

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {"facts": "Has a cat.\nPrefers tea in the morning."}
    )

    assert result["type"] is FlowResultType.MENU
    items = memory.items(key)
    assert items[0] == cat
    assert [(item["text"], item["source"]) for item in items[1:]] == [
        ("Prefers tea in the morning.", "told")
    ]


async def test_an_item_saved_while_the_editor_was_open_survives(hass, entry):
    key = entry_key(entry.entry_id, "owner")
    memory = await async_get_memory(hass)
    await memory.async_add(key, "Likes jazz.", "judged")
    form = await open_list(hass, entry, key)
    await memory.async_add(key, "Has a cat.", "judged")

    await hass.config_entries.options.async_configure(form["flow_id"], {"facts": "Likes jazz."})

    assert [item["text"] for item in memory.items(key)] == ["Likes jazz.", "Has a cat."]


async def test_clearing_the_box_forgets_every_shown_fact(hass, entry):
    key = entry_key(entry.entry_id, "owner")
    memory = await async_get_memory(hass)
    await memory.async_add(key, "Likes jazz.", "judged")
    form = await open_list(hass, entry, key)

    await hass.config_entries.options.async_configure(form["flow_id"], {})

    assert memory.items(key) == []


async def test_a_line_over_200_characters_is_a_form_error_naming_the_line(hass, entry):
    key = entry_key(entry.entry_id, "owner")
    memory = await async_get_memory(hass)
    jazz = await memory.async_add(key, "Likes jazz.", "judged")
    form = await open_list(hass, entry, key)
    long_line = "Remembers " + "very " * 40 + "much."

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {"facts": f"Likes jazz.\n{long_line}"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "line_too_long"}
    assert long_line.startswith(result["description_placeholders"]["line"].rstrip("…"))
    assert facts_shown(result) == f"Likes jazz.\n{long_line}"
    assert memory.items(key) == [jazz]


async def test_regression_security_a_list_over_200_facts_is_a_form_error(hass, entry):
    key = entry_key(entry.entry_id, "owner")
    memory = await async_get_memory(hass)
    form = await open_list(hass, entry, key)
    facts = "\n".join(f"Fact {number}" for number in range(201))

    result = await hass.config_entries.options.async_configure(form["flow_id"], {"facts": facts})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "too_many_facts"}
    assert memory.items(key) == []


async def test_global_list_edits_are_seen_by_other_assistants(hass, entry):
    form = await open_list(hass, entry, global_key("owner"))

    await hass.config_entries.options.async_configure(form["flow_id"], {"facts": "Has a cat."})

    memory = await async_get_memory(hass)
    assert [item["text"] for item in memory.items(global_key("owner"))] == ["Has a cat."]


async def test_saving_memory_leaves_the_entrys_options_unchanged(hass, entry):
    form = await open_list(hass, entry, entry_key(entry.entry_id, "owner"))

    await hass.config_entries.options.async_configure(form["flow_id"], {"facts": "Has a cat."})

    assert entry.options == EXISTING_OPTIONS


async def test_memory_can_be_edited_while_memory_is_disabled(hass, entry):
    key = entry_key(entry.entry_id, "wife")
    assert "memory" not in entry.options
    form = await open_list(hass, entry, key)

    await hass.config_entries.options.async_configure(form["flow_id"], {"facts": "Likes tea."})

    memory = await async_get_memory(hass)
    assert [item["text"] for item in memory.items(key)] == ["Likes tea."]


async def test_a_list_key_that_is_not_offered_is_rejected(hass, entry):
    picker = await open_step(hass, entry, "memory")

    with pytest.raises(Exception):  # noqa: B017, PT011 - schema validation error
        await hass.config_entries.options.async_configure(
            picker["flow_id"], {"list": entry_key("office-entry", "owner")}
        )


# The Reminders section.


async def test_reminders_section_comes_right_after_memory(hass, entry):
    form = await open_step(hass, entry, "settings")

    sections = [str(marker) for marker in form["data_schema"].schema]
    assert sections.index("reminders") == sections.index("memory") + 1


async def test_reminders_can_be_enabled_with_their_own_writer(hass, entry):
    hass.states.async_set("conversation.announcer", "unknown", {"supported_features": 0})
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["reminders"] = {
        "enabled": True,
        "agent": "conversation.announcer",
        "persona": "Sound like a ship's captain.",
        "failure_line": "That reminder did not take.",
    }

    result = await hass.config_entries.options.async_configure(form["flow_id"], values)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["reminders"] == {**REMINDER_DEFAULTS, **values["reminders"]}


@pytest.mark.parametrize("writer", ["main", "router"])
async def test_announcement_writer_may_not_be_the_main_agent_or_a_router(hass, entry, writer):
    router = er.async_get(hass).async_get_entity_id("conversation", DOMAIN, entry.entry_id)
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["reminders"] = {"enabled": True, "agent": MAIN_AGENT if writer == "main" else router}

    result = await hass.config_entries.options.async_configure(form["flow_id"], values)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "reminder_agent_invalid"}
    assert entry.options == EXISTING_OPTIONS


async def test_announcement_writer_may_not_be_an_agent_that_controls_the_home(hass, entry):
    hass.states.async_set("conversation.with_tools", "unknown", {"supported_features": 1})
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["reminders"] = {"enabled": True, "agent": "conversation.with_tools"}

    result = await hass.config_entries.options.async_configure(form["flow_id"], values)

    assert result["errors"] == {"base": "reminder_agent_invalid"}
    assert entry.options == EXISTING_OPTIONS


async def test_settings_refuse_a_refusal_agent_that_can_control_the_home(hass, entry):
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["strangers"] = {"mode": "ai", "reply_agent": conversation.HOME_ASSISTANT_AGENT}

    result = await hass.config_entries.options.async_configure(form["flow_id"], values)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "refusal_agent_invalid"}
    assert entry.options == EXISTING_OPTIONS


# The reminder calendars.

ALEX_CALENDAR = "calendar.alex_reminders"
SAM_CALENDAR = "calendar.sam_reminders"
HOME_CALENDAR = "calendar.household_reminders"


async def add_other_router(hass, title, users, **reminders):
    """A second router, with its saved people and reminder calendars."""
    other = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={"name": title, "agent": MAIN_AGENT},
        options={
            **copy.deepcopy(EXISTING_OPTIONS),
            "owner": {"name": "Alex", "user": users[0]},
            "partner": {"name": "Sam", "user": users[1]},
            "reminders": {**REMINDER_DEFAULTS, **reminders},
        },
    )
    other.add_to_hass(hass)
    return other


async def submit_reminder_calendars(hass, entry, users=(None, None), **reminders):
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["owner"] = {**values["owner"], "user": users[0]}
    values["partner"] = {**values["partner"], "user": users[1]}
    values["reminders"] = {**REMINDER_DEFAULTS, **reminders}
    for person in ("owner", "partner"):
        if values[person]["user"] is None:
            del values[person]["user"]
    return await hass.config_entries.options.async_configure(form["flow_id"], values)


async def second_user(hass):
    return await hass.auth.async_create_user("Second", group_ids=["system-users"])


async def test_reminder_calendars_are_saved(hass, entry, hass_admin_user):
    other = await second_user(hass)
    lists = {
        "owner_calendars": [ALEX_CALENDAR],
        "partner_calendars": [SAM_CALENDAR],
        "household_calendars": [HOME_CALENDAR],
    }

    result = await submit_reminder_calendars(
        hass, entry, (hass_admin_user.id, other.id), **lists
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {key: entry.options["reminders"][key] for key in lists} == lists


async def test_a_calendar_in_both_peoples_lists_is_refused(hass, entry, hass_admin_user):
    other = await second_user(hass)

    result = await submit_reminder_calendars(
        hass,
        entry,
        (hass_admin_user.id, other.id),
        owner_calendars=[ALEX_CALENDAR],
        partner_calendars=[ALEX_CALENDAR],
    )

    assert result["errors"] == {"base": "reminder_calendar_twice"}
    assert result["description_placeholders"] == {"calendar": ALEX_CALENDAR}
    assert entry.options == EXISTING_OPTIONS


async def test_a_personal_calendar_also_listed_as_household_is_refused(
    hass, entry, hass_admin_user
):
    result = await submit_reminder_calendars(
        hass,
        entry,
        (hass_admin_user.id, None),
        owner_calendars=[ALEX_CALENDAR],
        household_calendars=[ALEX_CALENDAR],
    )

    assert result["errors"] == {"base": "reminder_calendar_twice"}
    assert entry.options == EXISTING_OPTIONS


@pytest.mark.parametrize(
    "lists",
    [
        {"owner_calendars": [ALEX_CALENDAR], "partner_calendars": [ALEX_CALENDAR]},
        {"partner_calendars": [ALEX_CALENDAR], "household_calendars": [ALEX_CALENDAR]},
    ],
    ids=["both_people", "person_and_household"],
)
async def test_regression_final_a_clash_inside_one_router_names_no_other_router(
    hass, entry, hass_admin_user, lists
):
    # The {router} of the cross-router text would name the router being saved, as if
    # another one already used the calendar.
    other = await second_user(hass)

    result = await submit_reminder_calendars(
        hass, entry, (hass_admin_user.id, other.id), **lists
    )

    assert result["errors"] == {"base": "reminder_calendar_twice"}
    assert result["description_placeholders"] == {"calendar": ALEX_CALENDAR}


async def test_a_calendar_personal_for_another_user_in_another_router_is_refused(
    hass, entry, hass_admin_user
):
    other_user = await second_user(hass)
    await add_other_router(
        hass, "Study assistant", (other_user.id, None), owner_calendars=[ALEX_CALENDAR]
    )

    result = await submit_reminder_calendars(
        hass, entry, (hass_admin_user.id, None), owner_calendars=[ALEX_CALENDAR]
    )

    assert result["errors"] == {"base": "reminder_calendar_shared"}
    assert result["description_placeholders"] == {
        "calendar": ALEX_CALENDAR,
        "router": "Study assistant",
    }
    assert entry.options == EXISTING_OPTIONS


async def test_the_same_household_calendar_in_two_routers_is_allowed(hass, entry):
    await add_other_router(
        hass, "Study assistant", (None, None), household_calendars=[HOME_CALENDAR]
    )

    result = await submit_reminder_calendars(hass, entry, household_calendars=[HOME_CALENDAR])

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_the_same_person_may_keep_a_calendar_in_two_routers(hass, entry, hass_admin_user):
    await add_other_router(
        hass, "Study assistant", (hass_admin_user.id, None), owner_calendars=[ALEX_CALENDAR]
    )

    result = await submit_reminder_calendars(
        hass, entry, (hass_admin_user.id, None), owner_calendars=[ALEX_CALENDAR]
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_household_calendar_that_is_personal_in_another_router_is_refused(
    hass, entry, hass_admin_user
):
    await add_other_router(
        hass, "Study assistant", (hass_admin_user.id, None), owner_calendars=[ALEX_CALENDAR]
    )

    result = await submit_reminder_calendars(hass, entry, household_calendars=[ALEX_CALENDAR])

    assert result["errors"] == {"base": "reminder_calendar_shared"}
    assert result["description_placeholders"]["router"] == "Study assistant"


@pytest.mark.parametrize("person", ["owner", "partner"])
async def test_personal_reminder_calendars_need_that_persons_user(hass, entry, person):
    result = await submit_reminder_calendars(
        hass, entry, **{f"{person}_calendars": [ALEX_CALENDAR]}
    )

    assert result["errors"] == {"base": "reminder_calendar_no_user"}
    assert entry.options == EXISTING_OPTIONS


# Chat settings: a character and a voice per assistant.

CHAT_SETTINGS = {
    "character": "A dry, put-upon butler.",
    "tts_engine": "tts.kitchen_voice",
    "voice": "jeeves",
}


async def test_chat_section_is_offered_with_nothing_set(hass, entry):
    form = await open_step(hass, entry, "settings")

    assert shown_values(form["data_schema"])["chat"] == {}


async def test_chat_settings_are_saved_by_the_dialog(hass, entry):
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["chat"] = dict(CHAT_SETTINGS)

    result = await hass.config_entries.options.async_configure(form["flow_id"], values)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["chat"] == CHAT_SETTINGS


async def test_a_saved_chat_character_is_shown_again(hass, entry):
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["chat"] = dict(CHAT_SETTINGS)
    await hass.config_entries.options.async_configure(form["flow_id"], values)

    again = await open_step(hass, entry, "settings")

    assert shown_values(again["data_schema"])["chat"] == CHAT_SETTINGS


async def test_a_chat_character_of_2000_characters_is_saved(hass, entry):
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["chat"] = {"character": "x" * 2000}

    result = await hass.config_entries.options.async_configure(form["flow_id"], values)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["chat"] == {"character": "x" * 2000}


async def test_a_chat_character_over_2000_characters_is_a_form_error(hass, entry):
    form = await open_step(hass, entry, "settings")
    values = shown_values(form["data_schema"])
    values["chat"] = {"character": "x" * 2001}

    result = await hass.config_entries.options.async_configure(form["flow_id"], values)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "chat_character_too_long"}
    assert entry.options == EXISTING_OPTIONS


async def test_settings_saved_before_the_chat_section_existed_still_save(hass, entry):
    assert "chat" not in entry.options
    form = await open_step(hass, entry, "settings")

    result = await hass.config_entries.options.async_configure(
        form["flow_id"], shown_values(form["data_schema"])
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["chat"] == {}
