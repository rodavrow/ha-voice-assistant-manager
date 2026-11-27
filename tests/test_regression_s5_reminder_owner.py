# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for S5 of the 2026-11-27 review: whose a reminder calendar is.

The look-up and cancel actions took the caller's word for whose calendar it was (the
for_whom label). Any Home Assistant user can call an action, so a user could label the
other person's calendar "household" and read or cancel their reminders. Whose a calendar
is now comes from the assistants' settings, and who is asking from the turn or the user.
"""

import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant

from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.reminder_actions import asker, note_asker

from .router_helpers import MAIN_AGENT, FakeAgents, options_with, router_entity_id
from .test_reminder_list import calendars, look_up, titles  # noqa: F401 - fixture

ALEX_CALENDAR = "calendar.alex_reminders"
SAM_CALENDAR = "calendar.sam_reminders"
HOUSE_CALENDAR = "calendar.household_reminders"


@pytest.fixture
async def people(hass: HomeAssistant, calendars):  # noqa: F811
    """Alex and Sam are Home Assistant users, and the assistant lists their calendars."""
    alex = await hass.auth.async_create_user("Alex", group_ids=["system-users"])
    sam = await hass.auth.async_create_user("Sam", group_ids=["system-users"])
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.async_update_entry(
        entry,
        options=options_with(
            owner={"user": alex.id},
            partner={"user": sam.id},
            reminders={
                "enabled": True,
                "owner_calendars": [ALEX_CALENDAR],
                "partner_calendars": [SAM_CALENDAR],
                "household_calendars": [HOUSE_CALENDAR],
            },
        ),
    )
    await hass.async_block_till_done()
    return {"alex": alex, "sam": sam, "entry": entry}


async def call(hass: HomeAssistant, action: str, data: dict, context: Context) -> dict:
    return await hass.services.async_call(
        DOMAIN, action, data, blocking=True, return_response=True, context=context
    )


def listing(*pairs: tuple[str, str]) -> dict:
    return {"reminders": [{"for_whom": who, "calendar": [calendar]} for who, calendar in pairs]}


async def all_titles(hass: HomeAssistant, who: str) -> list[str]:
    return titles(await look_up(hass, who))


# Called directly by a user, outside a turn


async def test_a_user_cannot_read_the_other_persons_calendar_by_calling_it_the_households(
    hass, people
):
    sam = Context(user_id=people["sam"].id)

    answer = await call(
        hass, "list_reminder_events", listing(("household", ALEX_CALENDAR)), sam
    )

    assert answer["status"] == "error"
    assert "reminders" not in answer


async def test_a_user_reads_their_own_and_the_households_whatever_the_labels_say(hass, people):
    sam = Context(user_id=people["sam"].id)

    answer = await call(
        hass,
        "list_reminder_events",
        listing(("x", SAM_CALENDAR), ("y", HOUSE_CALENDAR), ("household", ALEX_CALENDAR)),
        sam,
    )

    assert sorted(titles(answer)) == ["Bins out", "Charge the car for the trip"]


@pytest.mark.parametrize("for_whom", [None, "household", "sam"])
async def test_a_user_cannot_cancel_in_the_other_persons_calendar(hass, people, for_whom):
    sam = Context(user_id=people["sam"].id)
    data = {"calendar": [ALEX_CALENDAR], "title": "car"}
    if for_whom:
        data["for_whom"] = for_whom

    answer = await call(hass, "cancel_reminder_event", data, sam)

    assert answer["status"] == "error"
    assert "Charge the car" in await all_titles(hass, "alex")


async def test_a_user_can_cancel_their_own_reminder(hass, people):
    sam = Context(user_id=people["sam"].id)

    answer = await call(
        hass,
        "cancel_reminder_event",
        {"calendar": [SAM_CALENDAR], "title": "car", "for_whom": "sam"},
        sam,
    )

    assert answer["status"] == "ok"


async def test_a_user_who_names_nobody_is_refused_also_for_their_own_calendar(hass, people):
    """Only an automation, which has no user, may leave for_whom out."""
    sam = Context(user_id=people["sam"].id)

    answer = await call(
        hass, "cancel_reminder_event", {"calendar": [SAM_CALENDAR], "title": "car"}, sam
    )

    assert answer["status"] == "error"


async def test_a_user_is_refused_a_calendar_no_assistant_lists(hass, people):
    hass.config_entries.async_update_entry(
        people["entry"], options=options_with(partner={"user": people["sam"].id})
    )
    sam = Context(user_id=people["sam"].id)

    listed = await call(hass, "list_reminder_events", listing(("household", SAM_CALENDAR)), sam)
    cancelled = await call(
        hass,
        "cancel_reminder_event",
        {"calendar": [SAM_CALENDAR], "title": "car", "for_whom": "household"},
        sam,
    )

    assert listed["status"] == "error"
    assert cancelled["status"] == "error"


async def test_a_user_who_is_nobody_in_any_assistant_sees_the_household_only(hass, people):
    other = await hass.auth.async_create_user("Guest", group_ids=["system-users"])

    answer = await call(
        hass,
        "list_reminder_events",
        listing(("household", HOUSE_CALENDAR), ("household", SAM_CALENDAR)),
        Context(user_id=other.id),
    )

    assert titles(answer) == ["Bins out"]


# In a turn: the settings decide whose a listed calendar is, not the script's label


async def test_in_a_turn_a_calendar_listed_for_the_other_person_is_never_shown(hass, people):
    context = Context()
    note_asker(hass, context, "alex", people["alex"].id)

    answer = await call(
        hass,
        "list_reminder_events",
        listing(("alex", SAM_CALENDAR), ("household", SAM_CALENDAR), ("alex", ALEX_CALENDAR)),
        context,
    )

    assert sorted(titles(answer)) == ["Charge the car", "Take the pills"]


async def test_in_a_turn_a_calendar_listed_for_the_other_person_cannot_be_cancelled_in(
    hass, people
):
    context = Context()
    note_asker(hass, context, "alex", people["alex"].id)

    answer = await call(
        hass,
        "cancel_reminder_event",
        {"calendar": [SAM_CALENDAR], "title": "car", "for_whom": "alex"},
        context,
    )

    assert answer["status"] == "error"
    assert "Charge the car for the trip" in await all_titles(hass, "sam")


async def test_in_a_turn_a_person_without_a_user_here_is_known_by_their_name(hass, people):
    """The same person may have a user in one assistant and none in the one asked."""
    context = Context()
    note_asker(hass, context, "alex")

    answer = await call(
        hass,
        "cancel_reminder_event",
        {"calendar": [ALEX_CALENDAR], "title": "car", "for_whom": "alex"},
        context,
    )

    assert answer["status"] == "ok"


# The note of who is asking lasts as long as the turn


async def test_the_note_of_who_is_asking_is_gone_when_the_turn_ends(hass, people):
    entity_id = router_entity_id(hass, people["entry"])
    entity = hass.data[conversation.DATA_COMPONENT].get_entity(entity_id)
    context = Context(user_id=people["alex"].id)
    agents = FakeAgents()
    during: list[str | None] = []
    answer = agents.converse

    async def converse(*args, **kwargs):
        if kwargs.get("agent_id") == MAIN_AGENT:
            during.append(asker(hass, context))
        return await answer(*args, **kwargs)

    agents.converse = converse  # type: ignore[method-assign]
    with agents.patched():
        await entity.async_process(
            conversation.ConversationInput(
                text="What is on today?",
                context=context,
                conversation_id=None,
                device_id=None,
                satellite_id=None,
                language="en",
                agent_id=entity_id,
            )
        )

    assert during == ["alex"]
    assert asker(hass, context) is None
