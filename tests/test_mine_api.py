# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the commands a person uses on My assistant, as a user who is not an admin."""

import copy
import json
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.components.calendar import CalendarEvent
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, MockUser

from custom_components.voice_assistant_manager.memory import async_get_memory, entry_key, global_key

from .router_helpers import MAIN_AGENT, MEMORY_AGENT, REFUSAL_AGENT, add_router, options_with
from .test_mine_reminders import CREATE_AND_DELETE, StoredCalendar, setup_calendars

PREFIX = "voice_assistant_manager/me"
ALEX = "calendar.alex_reminders"
SAM = "calendar.sam_reminders"
HOUSE = "calendar.house_reminders"
SAM_PERSONA = "Sam's private persona text"
SAM_FACT = "Sam fears the dentist"
SAM_PRIVATE_FACT = "Sam is planning a surprise"
SAM_SHARED_FACT = "Sam likes tea"
NOT_YOURS = "That is not one of your assistants."


class Mine:
    """The My assistant page's side of the websocket, as one signed-in person."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def call(self, command: str, **data: Any) -> dict[str, Any]:
        await self._client.send_json_auto_id({"type": f"{PREFIX}/{command}", **data})
        return await self._client.receive_json()  # type: ignore[no-any-return]

    async def ok(self, command: str, **data: Any) -> dict[str, Any]:
        answer = await self.call(command, **data)
        assert answer["success"], answer
        return answer["result"]  # type: ignore[no-any-return]

    async def refused(self, command: str, **data: Any) -> str:
        result = await self.ok(command, **data)
        assert result["ok"] is False, result
        return str(result["errors"]["base"])


@pytest.fixture
async def mine(hass: HomeAssistant, hass_ws_client: Any, hass_read_only_access_token: str) -> Mine:
    return Mine(await hass_ws_client(hass, access_token=hass_read_only_access_token))


def calendar_event(uid: str, summary: str, hours: int) -> CalendarEvent:
    start = dt_util.utcnow() + timedelta(hours=hours)
    return CalendarEvent(
        start=start, end=start + timedelta(hours=1), summary=summary, uid=uid, recurrence_id=None
    )


class FailingDelete(StoredCalendar):
    async def async_delete_event(
        self, uid: str, recurrence_id: str | None = None, recurrence_range: str | None = None
    ) -> None:
        raise HomeAssistantError("the disk is full")


def reminder_calendars(*, alex: StoredCalendar, sam: StoredCalendar) -> tuple[StoredCalendar, ...]:
    return (alex, sam, StoredCalendar("House reminders", [], CREATE_AND_DELETE))


async def alex_router(
    hass: HomeAssistant, user: MockUser, title: str = "Kitchen assistant", **changes: Any
) -> MockConfigEntry:
    """A router in which `user` is Alex (Person 1) and "u-sam" is Sam (Person 2)."""
    for agent in (MAIN_AGENT, MEMORY_AGENT, REFUSAL_AGENT):
        hass.states.async_set(agent, "unknown", {"supported_features": 0})
    options = options_with(
        owner={"user": user.id, "name": "Alex", "persona": "Alex persona."},
        partner={"user": "u-sam", "name": "Sam", "persona": SAM_PERSONA},
        reminders={
            "owner_calendars": [ALEX],
            "partner_calendars": [SAM],
            "household_calendars": [HOUSE],
            "assistant": "jeeves",
        },
        **changes,
    )
    options["calendar_rows"] = [
        {"calendar": "calendar.bins", "for": "household", "lead_minutes": 30},
        {"calendar": "calendar.alex_work", "for": "owner", "all_day_time": "08:00"},
        {"calendar": "calendar.sam_work", "for": "partner", "hint": "Sam only"},
    ]
    return await add_router(hass, options, title)


async def fill_memory(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    memory = await async_get_memory(hass)
    await memory.async_add(entry_key(entry.entry_id, "owner"), "Alex likes coffee", "told")
    await memory.async_add(global_key("owner"), "Alex is vegetarian", "told", private=True)
    await memory.async_add(entry_key(entry.entry_id, "wife"), SAM_FACT, "told")
    wife = entry_key(entry.entry_id, "wife")
    await memory.async_add(wife, SAM_PRIVATE_FACT, "told", private=True)
    await memory.async_add(global_key("wife"), SAM_SHARED_FACT, "told")


def register_script(hass: HomeAssistant, failure: Exception | None = None) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def create_reminder(call: ServiceCall) -> dict[str, Any]:
        if failure is not None:
            raise failure
        calls.append(dict(call.data))
        return {"status": "ok", "message": "Reminder set."}

    hass.services.async_register(
        "script", "create_reminder", create_reminder, supports_response=SupportsResponse.ONLY
    )
    return calls


# What a person sees.


async def test_get_answers_with_the_persons_own_router(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    await fill_memory(hass, entry)

    result = await mine.ok("get")

    assert len(result["routers"]) == 1
    router = result["routers"][0]
    assert router["entry_id"] == entry.entry_id
    assert router["title"] == "Kitchen assistant"
    assert router["role"] == "owner"
    assert router["name"] == "Alex"
    assert router["assistant"] == "jeeves"
    assert router["persona"] == "Alex persona."
    assert router["strangers_mode"] == "fixed"


async def test_get_says_reminders_can_be_added_when_the_script_exists(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await alex_router(hass, hass_read_only_user)
    register_script(hass)

    result = await mine.ok("get")

    assert result["routers"][0]["can_add"] is True


async def test_get_says_reminders_cannot_be_added_without_the_script(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await alex_router(hass, hass_read_only_user)

    result = await mine.ok("get")

    assert result["routers"][0]["can_add"] is False


async def test_get_lists_only_the_calendars_for_the_person_or_everyone(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await alex_router(hass, hass_read_only_user)

    router = (await mine.ok("get"))["routers"][0]

    assert [(row["calendar"], row["for"]) for row in router["calendars"]] == [
        ("calendar.bins", "household"),
        ("calendar.alex_work", "owner"),
    ]
    assert router["calendars"][1]["when"]["all_day_time"] == "08:00"
    assert router["calendars"][0]["when"]["lead_minutes"] == 30


async def test_get_lists_only_the_persons_two_memory_lists(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    await fill_memory(hass, entry)

    router = (await mine.ok("get"))["routers"][0]

    assert {lst["key"]: [item["text"] for item in lst["items"]] for lst in router["memory"]} == {
        entry_key(entry.entry_id, "owner"): ["Alex likes coffee"],
        global_key("owner"): ["Alex is vegetarian"],
    }


async def test_get_never_contains_the_other_persons_data(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    await fill_memory(hass, entry)

    everything = json.dumps(await mine.ok("get"))

    for secret in (SAM_PERSONA, SAM_FACT, SAM_PRIVATE_FACT, SAM_SHARED_FACT, "Sam only"):
        assert secret not in everything
    assert MAIN_AGENT not in everything
    assert MEMORY_AGENT not in everything


async def test_get_brings_the_texts_of_the_page(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await alex_router(hass, hass_read_only_user)

    strings = (await mine.ok("get"))["strings"]

    assert strings["error.mine_not_yours"] == NOT_YOURS
    assert all(key.startswith(("mine.", "error.")) for key in strings)


async def test_an_unlinked_user_gets_no_routers(hass: HomeAssistant, mine: Mine) -> None:
    await add_router(hass, options_with(owner={"user": "u-someone"}, partner={"user": "u-sam"}))

    assert (await mine.ok("get"))["routers"] == []


async def test_a_partner_gets_the_partners_own_data(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await add_router(
        hass,
        options_with(
            owner={"user": "u-other", "persona": "Owner persona."},
            partner={"user": hass_read_only_user.id, "persona": "Partner persona."},
        ),
    )

    router = (await mine.ok("get"))["routers"][0]
    await mine.ok("save_persona", entry_id=entry.entry_id, persona="Changed.")

    assert router["role"] == "partner"
    assert router["persona"] == "Partner persona."
    assert "Owner persona." not in json.dumps(router)
    assert entry.options["partner"]["persona"] == "Changed."
    assert entry.options["owner"]["persona"] == "Owner persona."


# Persona.


async def test_save_persona_changes_only_the_persona(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    expected = copy.deepcopy(dict(entry.options))
    expected["owner"]["persona"] = "I am Alex. Keep it short."

    result = await mine.ok(
        "save_persona", entry_id=entry.entry_id, persona="I am Alex. Keep it short."
    )

    assert result == {"ok": True}
    assert dict(entry.options) == expected


async def test_persona_save_keeps_every_other_setting(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    before = copy.deepcopy(dict(entry.options))

    await mine.ok("save_persona", entry_id=entry.entry_id, persona="New persona.")

    for section, value in before.items():
        if section != "owner":
            assert entry.options[section] == value, section
    assert entry.options["owner"] == {**before["owner"], "persona": "New persona."}


async def test_persona_over_2000_characters_is_refused(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)

    text = await mine.refused("save_persona", entry_id=entry.entry_id, persona="x" * 2001)

    assert "2000" in text
    assert entry.options["owner"]["persona"] == "Alex persona."


async def test_persona_of_exactly_2000_characters_is_saved(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)

    await mine.ok("save_persona", entry_id=entry.entry_id, persona="x" * 2000)

    assert entry.options["owner"]["persona"] == "x" * 2000


async def test_a_persona_is_refused_when_the_settings_would_be(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(
        hass, hass_read_only_user, memory={"enabled": True, "agent": MAIN_AGENT}
    )
    before = copy.deepcopy(dict(entry.options))

    text = await mine.refused("save_persona", entry_id=entry.entry_id, persona="New persona.")

    assert text.startswith("Your persona cannot be saved until an administrator")
    assert dict(entry.options) == before


# Memory.


async def test_save_memory_edits_the_persons_own_list(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    await fill_memory(hass, entry)
    key = entry_key(entry.entry_id, "owner")
    shown = [item["id"] for item in (await async_get_memory(hass)).items(key)]

    result = await mine.ok(
        "save_memory", entry_id=entry.entry_id, key=key, shown=shown, lines=["Alex likes tea"]
    )

    assert result["ok"] is True
    assert [item["text"] for item in result["items"]] == ["Alex likes tea"]


async def test_save_memory_edits_the_persons_shared_list(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    await fill_memory(hass, entry)

    result = await mine.ok(
        "save_memory",
        entry_id=entry.entry_id,
        key=global_key("owner"),
        shown=[],
        lines=["(private) Alex is vegetarian", "Alex runs"],
    )

    assert [item["text"] for item in result["items"]] == ["Alex is vegetarian", "Alex runs"]


@pytest.mark.parametrize(
    "key_of",
    [
        lambda entry_id: entry_key(entry_id, "wife"),
        lambda entry_id: global_key("wife"),
        lambda entry_id: entry_key("another-entry", "owner"),
        lambda entry_id: "not:a:list",
    ],
    ids=["partner_entry", "partner_global", "another_router", "invented"],
)
async def test_save_memory_refuses_any_list_that_is_not_the_persons_own(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser, key_of: Any
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    await fill_memory(hass, entry)
    memory = await async_get_memory(hass)
    key = key_of(entry.entry_id)
    before = memory.items(key)

    text = await mine.refused(
        "save_memory", entry_id=entry.entry_id, key=key, shown=[i["id"] for i in before], lines=[]
    )

    assert text == NOT_YOURS
    assert memory.items(key) == before


async def test_save_memory_refuses_a_fact_that_is_too_long(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)

    text = await mine.refused(
        "save_memory",
        entry_id=entry.entry_id,
        key=entry_key(entry.entry_id, "owner"),
        shown=[],
        lines=["y" * 201],
    )

    assert text.startswith("A fact may be 200 characters at most")


# Reminders.


async def test_reminders_are_the_persons_own_and_the_households(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    await setup_calendars(
        hass,
        StoredCalendar("Alex reminders", [calendar_event("a1", "Call the dentist", 2)], 0),
        StoredCalendar("Sam reminders", [calendar_event("s1", "Sam's secret", 3)], 0),
        StoredCalendar("House reminders", [calendar_event("h1", "Put the bins out", 4)], 0),
    )
    entry = await alex_router(hass, hass_read_only_user)

    result = await mine.ok("reminders", entry_id=entry.entry_id)

    assert [(r["title"], r["whose"]) for r in result["reminders"]] == [
        ("Call the dentist", "mine"),
        ("Put the bins out", "household"),
    ]
    assert "Sam's secret" not in json.dumps(result)


async def test_add_reminder_for_me_calls_the_script_with_the_persons_own_name(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    calls = register_script(hass)

    result = await mine.ok(
        "add_reminder",
        entry_id=entry.entry_id,
        scope="me",
        title="Call the dentist",
        announce=True,
        in_minutes=30,
    )

    assert result == {"status": "ok", "message": "Reminder set."}
    assert calls == [
        {
            "title": "Call the dentist",
            "for_whom": "alex",
            "assistant": "jeeves",
            "announce": True,
            "in_minutes": 30,
        }
    ]


async def test_add_reminder_for_the_household_with_a_time_and_day(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    calls = register_script(hass)

    await mine.ok(
        "add_reminder",
        entry_id=entry.entry_id,
        scope="household",
        title="Bins",
        announce=False,
        at_time="19:30",
        on_day="tomorrow",
    )

    assert calls[0]["for_whom"] == "household"
    assert (calls[0]["at_time"], calls[0]["on_day"], calls[0]["announce"]) == (
        "19:30",
        "tomorrow",
        False,
    )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"in_minutes": 5, "at_time": "19:30"},
        {"in_minutes": 0},
        {"in_minutes": -3},
        {"at_time": "25:00"},
        {"at_time": "7pm"},
        {"in_minutes": 5, "on_day": "tomorrow"},
        {"scope": "everyone", "in_minutes": 5},
        {"scope": "Sam", "in_minutes": 5},
        {"title": "", "in_minutes": 5},
        {"title": "   ", "in_minutes": 5},
        {"title": "t" * 201, "in_minutes": 5},
    ],
    ids=[
        "no_time",
        "both_times",
        "zero_minutes",
        "negative_minutes",
        "hour_out_of_range",
        "not_hh_mm",
        "day_without_time",
        "scope_everyone",
        "scope_a_name",
        "empty_title",
        "blank_title",
        "long_title",
    ],
)
async def test_add_reminder_refuses_what_the_script_should_not_get(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser, fields: dict[str, Any]
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    calls = register_script(hass)
    data = {"entry_id": entry.entry_id, "scope": "me", "title": "Call", "announce": True}

    text = await mine.refused("add_reminder", **{**data, **fields})

    assert text
    assert calls == []


@pytest.mark.parametrize(
    "on_day",
    [
        *("someday", "Tomorrow", "next week", "2026-12-24", "13-01", "02-30", "12-1"),
        *(" 12-24", "12-24\n", "١٢-٢٤"),
    ],
    ids=[
        *("word", "capital", "phrase", "with_year", "month_13", "feb_30", "one_digit"),
        *("space", "line_break", "other_digits"),
    ],
)
async def test_regression_final_add_reminder_refuses_a_day_the_page_never_sends(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser, on_day: str
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    calls = register_script(hass)

    text = await mine.refused(
        "add_reminder",
        entry_id=entry.entry_id,
        scope="me",
        title="Call",
        announce=True,
        at_time="19:30",
        on_day=on_day,
    )

    assert text.startswith("Choose either a number of minutes")
    assert calls == []


@pytest.mark.parametrize(
    "on_day",
    ["today", "tomorrow", "monday", "sunday", "12-24", "02-29", "01-01"],
)
async def test_regression_final_add_reminder_takes_a_page_day_or_a_date(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser, on_day: str
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    calls = register_script(hass)

    await mine.ok(
        "add_reminder",
        entry_id=entry.entry_id,
        scope="me",
        title="Call",
        announce=True,
        at_time="19:30",
        on_day=on_day,
    )

    assert [call["on_day"] for call in calls] == [on_day]


async def test_regression_final_add_reminder_refuses_a_time_with_a_line_break(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    calls = register_script(hass)

    await mine.refused(
        "add_reminder",
        entry_id=entry.entry_id,
        scope="me",
        title="Call",
        announce=True,
        at_time="19:30\n",
    )

    assert calls == []


@pytest.mark.parametrize("name", [None, "", "   "], ids=["missing", "empty", "blank"])
async def test_regression_final_add_reminder_for_me_needs_the_persons_name(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser, name: str | None
) -> None:
    # Without a name, for_whom would be "person 1", which no reminder calendar answers to.
    entry = await alex_router(hass, hass_read_only_user)
    options = copy.deepcopy(dict(entry.options))
    options["owner"]["name"] = name
    hass.config_entries.async_update_entry(entry, options=options)
    calls = register_script(hass)
    data = {"entry_id": entry.entry_id, "title": "Call", "announce": True, "in_minutes": 5}

    text = await mine.refused("add_reminder", scope="me", **data)
    await mine.ok("add_reminder", scope="household", **data)

    assert "under People" in text
    assert [call["for_whom"] for call in calls] == ["household"]


async def test_add_reminder_says_so_when_there_is_no_script(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)

    text = await mine.refused(
        "add_reminder",
        entry_id=entry.entry_id,
        scope="me",
        title="Call",
        announce=True,
        in_minutes=5,
    )

    assert "create_reminder" in text


@pytest.mark.parametrize(
    "failure",
    [HomeAssistantError("the calendar is full"), ServiceValidationError("bad time")],
    ids=["home_assistant_error", "service_validation_error"],
)
async def test_regression_s9_a_failing_script_is_refused_without_its_reason(
    hass: HomeAssistant,
    mine: Mine,
    hass_read_only_user: MockUser,
    failure: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """What Home Assistant says can name entities or the other person's calendar: it goes
    to the log, and the person is told where to look."""
    entry = await alex_router(hass, hass_read_only_user)
    register_script(hass, failure)

    text = await mine.refused(
        "add_reminder",
        entry_id=entry.entry_id,
        scope="me",
        title="Call",
        announce=True,
        in_minutes=5,
    )

    assert str(failure) not in text
    assert "log" in text
    assert str(failure) in caplog.text


async def test_regression_s9_only_the_status_and_message_of_the_script_reach_the_page(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)

    async def create_reminder(call: ServiceCall) -> dict[str, Any]:
        return {
            "status": "error",
            "message": "No calendar for sam.",
            "calendar": SAM,
            "trace": {"entity": "script.create_reminder"},
        }

    hass.services.async_register(
        "script", "create_reminder", create_reminder, supports_response=SupportsResponse.ONLY
    )

    result = await mine.ok(
        "add_reminder",
        entry_id=entry.entry_id,
        scope="me",
        title="Call",
        announce=True,
        in_minutes=5,
    )

    assert result == {"status": "error", "message": "No calendar for sam."}


@pytest.mark.parametrize(
    "changes",
    [
        {"in_minutes": 525601},
        {"title": "x" * 2001},
        {"scope": "x" * 100},
        {"at_time": "x" * 100, "in_minutes": None},
        {"entry_id": "x" * 200},
    ],
    ids=["minutes", "title", "scope", "at_time", "entry_id"],
)
async def test_regression_s9_a_field_far_beyond_what_the_page_sends_is_not_accepted(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser, changes: dict[str, Any]
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    calls = register_script(hass)
    fields = {
        "entry_id": entry.entry_id,
        "scope": "me",
        "title": "Call",
        "announce": True,
        "in_minutes": 5,
        **changes,
    }

    answer = await mine.call(
        "add_reminder", **{key: value for key, value in fields.items() if value is not None}
    )

    assert answer["success"] is False
    assert answer["error"]["code"] == "invalid_format"
    assert calls == []


async def test_delete_reminder_removes_the_persons_own_event(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    calendars = await setup_calendars(
        hass,
        *reminder_calendars(
            alex=StoredCalendar(
                "Alex reminders", [calendar_event("a1", "Call", 2)], CREATE_AND_DELETE
            ),
            sam=StoredCalendar("Sam reminders", [], CREATE_AND_DELETE),
        ),
    )
    entry = await alex_router(hass, hass_read_only_user)

    result = await mine.ok("delete_reminder", entry_id=entry.entry_id, calendar=ALEX, uid="a1")

    assert result == {"ok": True}
    assert calendars[ALEX].deleted == [("a1", None)]


async def test_delete_reminder_refuses_the_other_persons_event(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    calendars = await setup_calendars(
        hass,
        *reminder_calendars(
            alex=StoredCalendar("Alex reminders", [], CREATE_AND_DELETE),
            sam=StoredCalendar(
                "Sam reminders", [calendar_event("s1", "Sam's", 2)], CREATE_AND_DELETE
            ),
        ),
    )
    entry = await alex_router(hass, hass_read_only_user)

    text = await mine.refused("delete_reminder", entry_id=entry.entry_id, calendar=SAM, uid="s1")

    assert "cannot" in text
    assert calendars[SAM].deleted == []


async def test_regression_s9_a_failing_delete_is_refused_without_its_reason(
    hass: HomeAssistant,
    mine: Mine,
    hass_read_only_user: MockUser,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_calendars(
        hass,
        *reminder_calendars(
            alex=FailingDelete(
                "Alex reminders", [calendar_event("a1", "Call", 2)], CREATE_AND_DELETE
            ),
            sam=StoredCalendar("Sam reminders", [], CREATE_AND_DELETE),
        ),
    )
    entry = await alex_router(hass, hass_read_only_user)

    text = await mine.refused("delete_reminder", entry_id=entry.entry_id, calendar=ALEX, uid="a1")

    assert "the disk is full" not in text
    assert "log" in text
    assert "the disk is full" in caplog.text


# Who may call.

ALL_COMMANDS: list[tuple[str, dict[str, Any]]] = [
    ("save_persona", {"persona": "Hijacked"}),
    ("save_memory", {"key": "global:owner", "shown": [], "lines": []}),
    ("reminders", {}),
    ("add_reminder", {"scope": "me", "title": "Hijacked", "announce": True, "in_minutes": 5}),
    ("delete_reminder", {"calendar": ALEX, "uid": "a1"}),
]


@pytest.mark.parametrize(("command", "data"), ALL_COMMANDS, ids=[c for c, _ in ALL_COMMANDS])
async def test_a_router_the_person_is_not_in_is_refused(
    hass: HomeAssistant,
    mine: Mine,
    hass_read_only_user: MockUser,
    command: str,
    data: dict[str, Any],
) -> None:
    # The person belongs to one router and names another: being in some router is no pass.
    await alex_router(hass, hass_read_only_user)
    calls = register_script(hass)
    other = await add_router(
        hass, options_with(owner={"user": "u-other"}, partner={"user": "u-sam"}), "Office"
    )

    text = await mine.refused(command, entry_id=other.entry_id, **data)

    assert text == NOT_YOURS
    assert calls == []
    assert other.options["owner"]["persona"] == "The person speaking is Alex."


@pytest.mark.parametrize(("command", "data"), ALL_COMMANDS, ids=[c for c, _ in ALL_COMMANDS])
async def test_a_router_that_does_not_exist_is_refused(
    hass: HomeAssistant, mine: Mine, command: str, data: dict[str, Any]
) -> None:
    await add_router(hass)

    assert await mine.refused(command, entry_id="no-such-router", **data) == NOT_YOURS


async def test_a_user_removed_from_the_settings_is_refused_next_time(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    await mine.ok("reminders", entry_id=entry.entry_id)

    options = copy.deepcopy(dict(entry.options))
    options["owner"]["user"] = "u-someone-else"
    hass.config_entries.async_update_entry(entry, options=options)

    assert await mine.refused("reminders", entry_id=entry.entry_id) == NOT_YOURS
    assert await mine.refused("save_persona", entry_id=entry.entry_id, persona="x") == NOT_YOURS
    assert (await mine.ok("get"))["routers"] == []


async def test_a_non_admin_still_cannot_call_admin_commands(
    hass: HomeAssistant, hass_ws_client: Any, hass_read_only_access_token: str
) -> None:
    await add_router(hass)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)

    await client.send_json_auto_id({"type": "voice_assistant_manager/panel/get"})
    answer = await client.receive_json()

    assert answer["success"] is False
    assert answer["error"]["code"] == "unauthorized"


# One person, two roles: owner in one router, partner in another.

B_OWNER = "calendar.b_owner"
B_PARTNER = "calendar.b_partner"


async def two_routers(
    hass: HomeAssistant, user: MockUser
) -> tuple[MockConfigEntry, MockConfigEntry, dict[str, StoredCalendar]]:
    """`user` is Alex (owner) in router A and Alex (partner) in router B."""
    calendars = await setup_calendars(
        hass,
        StoredCalendar("Alex reminders", [calendar_event("a1", "A mine", 2)], CREATE_AND_DELETE),
        StoredCalendar("Sam reminders", [], CREATE_AND_DELETE),
        StoredCalendar("House reminders", [], CREATE_AND_DELETE),
        StoredCalendar("B owner", [calendar_event("bo1", "B owner's", 2)], CREATE_AND_DELETE),
        StoredCalendar("B partner", [calendar_event("bp1", "B mine", 3)], CREATE_AND_DELETE),
    )
    first = await alex_router(hass, user)
    second = await add_router(
        hass,
        options_with(
            owner={"user": "u-sam", "name": "Sam", "persona": "B owner persona."},
            partner={"user": user.id, "name": "Alex", "persona": "B partner persona."},
            reminders={"owner_calendars": [B_OWNER], "partner_calendars": [B_PARTNER]},
        ),
        "Office",
    )
    return first, second, calendars


async def test_each_router_is_answered_with_the_role_the_person_has_in_it(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    first, second, _ = await two_routers(hass, hass_read_only_user)
    hass.config_entries.async_update_entry(
        second,
        options={
            **second.options,
            "calendar_rows": [
                {"calendar": "calendar.b_o", "for": "owner"},
                {"calendar": "calendar.b_p", "for": "partner"},
            ],
        },
    )

    routers = {r["entry_id"]: r for r in (await mine.ok("get"))["routers"]}

    assert routers[first.entry_id]["role"] == "owner"
    assert routers[first.entry_id]["persona"] == "Alex persona."
    assert routers[second.entry_id]["role"] == "partner"
    assert routers[second.entry_id]["persona"] == "B partner persona."
    assert [row["calendar"] for row in routers[second.entry_id]["calendars"]] == ["calendar.b_p"]
    # Sam is Person 1 in B and Person 2 in A, so neither "all assistants" list is Alex's alone.
    assert {lst["key"] for lst in routers[second.entry_id]["memory"]} == {
        entry_key(second.entry_id, "wife"),
    }
    assert {lst["key"] for lst in routers[first.entry_id]["memory"]} == {
        entry_key(first.entry_id, "owner"),
    }


def memory_keys(result: dict[str, Any]) -> dict[str, set[str]]:
    return {r["entry_id"]: {lst["key"] for lst in r["memory"]} for r in result["routers"]}


async def test_regression_final_swapped_roles_offer_neither_global_list(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    first, second, _ = await two_routers(hass, hass_read_only_user)
    memory = await async_get_memory(hass)
    await memory.async_add(global_key("owner"), SAM_PRIVATE_FACT, "told", private=True)
    await memory.async_add(global_key("wife"), SAM_SHARED_FACT, "told")
    edit = {"shown": [], "lines": ["Hijacked"]}

    everything = await mine.ok("get")
    refused_a = await mine.refused(
        "save_memory", entry_id=first.entry_id, key=global_key("owner"), **edit
    )
    refused_b = await mine.refused(
        "save_memory", entry_id=second.entry_id, key=global_key("wife"), **edit
    )

    assert memory_keys(everything) == {
        first.entry_id: {entry_key(first.entry_id, "owner")},
        second.entry_id: {entry_key(second.entry_id, "wife")},
    }
    assert SAM_PRIVATE_FACT not in json.dumps(everything)
    assert SAM_SHARED_FACT not in json.dumps(everything)
    assert refused_a == refused_b == NOT_YOURS
    assert [item["text"] for item in memory.items(global_key("owner"))] == [SAM_PRIVATE_FACT]
    assert [item["text"] for item in memory.items(global_key("wife"))] == [SAM_SHARED_FACT]


async def test_regression_final_another_person_1_elsewhere_hides_the_global_list(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    await add_router(hass, options_with(owner={"user": "u-other", "name": "Sam"}), "Office")
    memory = await async_get_memory(hass)
    await memory.async_add(global_key("owner"), SAM_PRIVATE_FACT, "told", private=True)

    everything = await mine.ok("get")
    refused = await mine.refused(
        "save_memory", entry_id=entry.entry_id, key=global_key("owner"), shown=[], lines=[]
    )

    assert memory_keys(everything) == {entry.entry_id: {entry_key(entry.entry_id, "owner")}}
    assert SAM_PRIVATE_FACT not in json.dumps(everything)
    assert refused == NOT_YOURS
    assert [item["text"] for item in memory.items(global_key("owner"))] == [SAM_PRIVATE_FACT]


async def test_regression_shared_lists_another_name_for_person_1_hides_the_global_list(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    await add_router(hass, options_with(owner={"name": "Sam"}), "Hall")

    everything = await mine.ok("get")
    refused = await mine.refused(
        "save_memory", entry_id=entry.entry_id, key=global_key("owner"), shown=[], lines=[]
    )

    assert memory_keys(everything) == {entry.entry_id: {entry_key(entry.entry_id, "owner")}}
    assert refused == NOT_YOURS


async def test_regression_shared_lists_a_router_with_shared_lists_off_does_not_hide_it(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    office = options_with(owner={"user": "u-other", "name": "Sam"}, memory={"use_global": False})
    await add_router(hass, office, "Office")

    everything = await mine.ok("get")

    assert memory_keys(everything) == {
        entry.entry_id: {entry_key(entry.entry_id, "owner"), global_key("owner")}
    }


async def test_regression_final_the_same_person_1_everywhere_gets_the_global_list(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    first = await alex_router(hass, hass_read_only_user)
    second = await alex_router(hass, hass_read_only_user, "Office")
    # No user for Person 1 there, so it cannot disagree on who the user is.
    await add_router(hass, options_with(owner={"name": "Alex"}), "Hall")

    everything = await mine.ok("get")
    saved = await mine.ok(
        "save_memory", entry_id=second.entry_id, key=global_key("owner"), shown=[], lines=["Tea"]
    )

    assert memory_keys(everything) == {
        first.entry_id: {entry_key(first.entry_id, "owner"), global_key("owner")},
        second.entry_id: {entry_key(second.entry_id, "owner"), global_key("owner")},
    }
    assert [item["text"] for item in saved["items"]] == ["Tea"]


async def test_save_persona_changes_the_role_of_the_named_router(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    first, second, _ = await two_routers(hass, hass_read_only_user)

    await mine.ok("save_persona", entry_id=second.entry_id, persona="Changed B.")
    await mine.ok("save_persona", entry_id=first.entry_id, persona="Changed A.")

    assert second.options["partner"]["persona"] == "Changed B."
    assert second.options["owner"]["persona"] == "B owner persona."
    assert first.options["owner"]["persona"] == "Changed A."
    assert first.options["partner"]["persona"] == SAM_PERSONA


async def test_save_memory_uses_the_role_of_the_named_router(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    first, second, _ = await two_routers(hass, hass_read_only_user)
    edit = {"shown": [], "lines": ["A fact"]}

    own_b = await mine.ok(
        "save_memory", entry_id=second.entry_id, key=entry_key(second.entry_id, "wife"), **edit
    )
    refused_b = await mine.refused(
        "save_memory", entry_id=second.entry_id, key=entry_key(second.entry_id, "owner"), **edit
    )
    own_a = await mine.ok(
        "save_memory", entry_id=first.entry_id, key=entry_key(first.entry_id, "owner"), **edit
    )
    refused_a = await mine.refused(
        "save_memory", entry_id=first.entry_id, key=entry_key(first.entry_id, "wife"), **edit
    )

    assert own_b["ok"] is True
    assert own_a["ok"] is True
    assert refused_b == refused_a == NOT_YOURS


async def test_reminders_come_from_the_role_of_the_named_router(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    first, second, _ = await two_routers(hass, hass_read_only_user)

    in_b = await mine.ok("reminders", entry_id=second.entry_id)
    in_a = await mine.ok("reminders", entry_id=first.entry_id)

    assert [r["title"] for r in in_b["reminders"]] == ["B mine"]
    assert [r["title"] for r in in_a["reminders"]] == ["A mine"]


async def test_delete_reminder_uses_the_role_of_the_named_router(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    first, second, calendars = await two_routers(hass, hass_read_only_user)

    await mine.refused("delete_reminder", entry_id=second.entry_id, calendar=B_OWNER, uid="bo1")
    await mine.refused("delete_reminder", entry_id=second.entry_id, calendar=ALEX, uid="a1")
    await mine.ok("delete_reminder", entry_id=second.entry_id, calendar=B_PARTNER, uid="bp1")
    await mine.refused("delete_reminder", entry_id=first.entry_id, calendar=B_PARTNER, uid="bp1")
    await mine.ok("delete_reminder", entry_id=first.entry_id, calendar=ALEX, uid="a1")

    assert calendars[B_OWNER].deleted == []
    assert calendars[B_PARTNER].deleted == [("bp1", None)]
    assert calendars[ALEX].deleted == [("a1", None)]


# Nothing the page sends decides whose data is used.

IDENTITY_FIELDS = [{"role": "partner"}, {"user_id": "u-sam"}]


@pytest.mark.parametrize("extra", IDENTITY_FIELDS, ids=["role", "user_id"])
@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("save_persona", {"persona": "Hijacked"}),
        ("save_memory", {"key": "global:wife", "shown": [], "lines": ["Hijacked"]}),
        ("delete_reminder", {"calendar": SAM, "uid": "s1"}),
    ],
    ids=["save_persona", "save_memory", "delete_reminder"],
)
async def test_fields_that_name_a_person_are_rejected_not_used(
    hass: HomeAssistant,
    mine: Mine,
    hass_read_only_user: MockUser,
    command: str,
    data: dict[str, Any],
    extra: dict[str, str],
) -> None:
    sam_event = calendar_event("s1", "Sam's", 2)
    calendars = await setup_calendars(
        hass,
        *reminder_calendars(
            alex=StoredCalendar("Alex reminders", [], CREATE_AND_DELETE),
            sam=StoredCalendar("Sam reminders", [sam_event], CREATE_AND_DELETE),
        ),
    )
    entry = await alex_router(hass, hass_read_only_user)
    memory = await async_get_memory(hass)

    answer = await mine.call(command, entry_id=entry.entry_id, **data, **extra)

    assert answer["success"] is False
    assert answer["error"]["code"] == "invalid_format"
    assert entry.options["partner"]["persona"] == SAM_PERSONA
    assert entry.options["owner"]["persona"] == "Alex persona."
    assert memory.items(global_key("wife")) == []
    assert calendars[SAM].deleted == []


@pytest.mark.parametrize(
    "edit",
    [
        {"lines": ["Fact"] * 201},
        {"lines": ["y" * 10_000]},
        {"lines": [], "shown": ["id"] * 201},
        {"lines": [], "shown": ["i" * 10_000]},
    ],
    ids=["201 lines", "10000 characters", "201 shown", "long shown id"],
)
async def test_regression_security_save_memory_refuses_an_oversized_request(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser, edit: dict[str, Any]
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    key = entry_key(entry.entry_id, "owner")

    answer = await mine.call(
        "save_memory", entry_id=entry.entry_id, key=key, **{"shown": [], **edit}
    )

    assert answer["success"] is False
    assert answer["error"]["code"] == "invalid_format"
    assert (await async_get_memory(hass)).items(key) == []


async def test_save_memory_still_answers_a_slightly_long_line_in_plain_words(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)

    text = await mine.refused(
        "save_memory",
        entry_id=entry.entry_id,
        key=entry_key(entry.entry_id, "owner"),
        shown=[],
        lines=["y" * 240],
    )

    assert text.startswith("A fact may be 200 characters at most")


async def test_regression_security_save_memory_refuses_a_list_grown_over_200_facts(
    hass: HomeAssistant, mine: Mine, hass_read_only_user: MockUser
) -> None:
    entry = await alex_router(hass, hass_read_only_user)
    key = entry_key(entry.entry_id, "owner")
    memory = await async_get_memory(hass)
    await memory.async_apply_edit(key, [], [f"Old {number}" for number in range(10)])

    text = await mine.refused(
        "save_memory",
        entry_id=entry.entry_id,
        key=key,
        shown=[],
        lines=[f"New {number}" for number in range(200)],
    )

    assert text.startswith("A list may hold 200 facts at most")
    assert len(memory.items(key)) == 10
