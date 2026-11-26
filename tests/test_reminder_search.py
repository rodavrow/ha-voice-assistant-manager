# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Looking reminders up: the day range, the keywords and the repeat in words."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.voice_assistant_manager.reminder_search import (
    Occurrence,
    SearchError,
    day_range,
    describe_repeat,
    matches,
    resolve_day,
    summarise,
)

ZONE = ZoneInfo("Europe/Amsterdam")
THURSDAY = date(2026, 11, 26)
NOW = datetime(2026, 11, 26, 7, 0, tzinfo=ZONE)


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("today", date(2026, 11, 26)),
        ("Tomorrow", date(2026, 11, 27)),
        ("thursday", date(2026, 11, 26)),
        ("wednesday", date(2026, 12, 2)),
        (" Wed ", date(2026, 12, 2)),
        ("12-24", date(2026, 12, 24)),
        ("01-05", date(2027, 1, 5)),
        ("2027-03-01", date(2027, 3, 1)),
    ],
)
def test_a_day_in_words_becomes_a_date(text, day):
    assert resolve_day(text, THURSDAY) == day


@pytest.mark.parametrize("text", ["", "someday", "13-45", "we", "next week"])
def test_a_day_that_cannot_be_read_says_what_is_accepted(text):
    with pytest.raises(SearchError, match="today, tomorrow"):
        resolve_day(text, THURSDAY)


def test_without_a_day_everything_from_now_on_is_looked_at():
    start, end, ranged = day_range(None, None, NOW)

    assert (start, ranged) == (NOW, False)
    assert end - start == timedelta(days=731)


def test_one_day_is_that_whole_day():
    start, end, ranged = day_range("wednesday", "", NOW)

    assert start == datetime(2026, 12, 2, 0, 0, tzinfo=ZONE)
    assert end == datetime(2026, 12, 3, 0, 0, tzinfo=ZONE)
    assert ranged


def test_today_starts_now_so_nothing_already_past_is_listed():
    start, end, _ = day_range("today", None, NOW)

    assert start == NOW
    assert end == datetime(2026, 11, 27, 0, 0, tzinfo=ZONE)


def test_a_last_day_is_counted_from_the_first_day():
    start, end, _ = day_range("monday", "friday", NOW)

    assert start == datetime(2026, 11, 30, 0, 0, tzinfo=ZONE)
    assert end == datetime(2026, 12, 5, 0, 0, tzinfo=ZONE)


def test_a_last_day_without_a_first_day_counts_from_today():
    start, end, ranged = day_range(None, "sunday", NOW)

    assert (start, end, ranged) == (NOW, datetime(2026, 11, 30, 0, 0, tzinfo=ZONE), True)


@pytest.mark.parametrize(
    ("keywords", "found"),
    [
        ("", True),
        ("car", True),
        ("CHARGING the car", True),
        ("reminders for the car", True),
        ("car bins", False),
        ("garage", True),
        ("cat", False),
    ],
)
def test_every_keyword_must_be_in_the_title_or_the_notes(keywords, found):
    assert matches(keywords, "Charge the car", "In the garage") is found


def test_unicode_keywords_match_whatever_the_case():
    assert matches("ÉCOLE", "Aller à l'école", "")


@pytest.mark.parametrize(
    ("rule", "repeats", "ends"),
    [
        (None, "no", ""),
        ("FREQ=DAILY", "every day", "never"),
        ("FREQ=DAILY;INTERVAL=2", "every 2 days", "never"),
        ("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "every weekday", "never"),
        ("FREQ=WEEKLY;BYDAY=SA,SU", "every Saturday and Sunday", "never"),
        ("FREQ=WEEKLY", "every week", "never"),
        ("FREQ=WEEKLY;INTERVAL=2;BYDAY=FR", "every 2 weeks on Friday", "never"),
        ("FREQ=WEEKLY;BYDAY=MO,TH;COUNT=5", "every week on Monday and Thursday",
         "after 5 times in all"),
        ("FREQ=MONTHLY;UNTIL=20270301T235959", "every month", "on 2027-03-01"),
        ("FREQ=YEARLY;INTERVAL=1", "every year", "never"),
        ("FREQ=MONTHLY;BYDAY=1MO", "by the calendar rule FREQ=MONTHLY;BYDAY=1MO", "never"),
    ],
)
def test_a_calendar_rule_is_put_in_words(rule, repeats, ends):
    assert describe_repeat(rule) == (repeats, ends)


def occurrence(title="Charge the car", day=2, hour=8, key="a", **changes) -> Occurrence:
    start = datetime(2026, 12, day, hour, 0, tzinfo=ZONE)
    values = {
        "key": key, "title": title, "description": "assistant: ada", "for_whom": "alex",
        "spoken": True, "start": start, "end": start + timedelta(minutes=5), "rrule": None,
        "all_day": False,
    }
    return Occurrence(**{**values, **changes})


def test_a_reminder_is_reported_with_its_exact_settings():
    found, more = summarise([occurrence()], "", ranged=False)

    assert not more
    assert found == [
        {
            "title": "Charge the car",
            "for_whom": "alex",
            "when": "Wednesday 2026-12-02 08:00",
            "repeats": "no",
            "duration_minutes": 5,
            "spoken": True,
            "assistant": "ada",
        }
    ]


def test_a_repeating_reminder_is_one_entry_with_its_next_time_and_rule():
    weekly = [
        occurrence(day=day, rrule="FREQ=WEEKLY;BYDAY=WE;COUNT=3") for day in (16, 2, 9)
    ]

    found, _ = summarise(weekly, "", ranged=True)

    assert len(found) == 1
    assert found[0]["when"] == "Wednesday 2026-12-02 08:00"
    assert found[0]["repeats"] == "every week on Wednesday"
    assert found[0]["ends"] == "after 3 times in all"
    assert found[0]["rule"] == "FREQ=WEEKLY;BYDAY=WE;COUNT=3"
    assert found[0]["times_in_range"] == 3


def test_reminders_come_soonest_first_and_only_those_that_match():
    found, _ = summarise(
        [occurrence("Bins out", day=4, key="b"), occurrence(key="a"),
         occurrence("Car insurance", day=3, key="c")],
        "car", ranged=False,
    )

    assert [r["title"] for r in found] == ["Charge the car", "Car insurance"]


def test_notes_are_kept_apart_from_the_assistant_line():
    found, _ = summarise(
        [occurrence(description="Use the blue cable\nassistant: Jeeves")], "", ranged=False
    )

    assert found[0]["assistant"] == "Jeeves"
    assert found[0]["notes"] == "Use the blue cable"


def test_an_all_day_reminder_has_no_time():
    found, _ = summarise([occurrence(all_day=True)], "", ranged=False)

    assert found[0]["when"] == "Wednesday 2026-12-02, all day"
    assert "duration_minutes" not in found[0]


def test_a_long_list_is_cut_and_says_so():
    many = [occurrence(f"Reminder {n}", key=str(n)) for n in range(30)]

    found, more = summarise(many, "", ranged=False, limit=25)

    assert len(found) == 25
    assert more


def test_nothing_found_is_an_empty_list():
    assert summarise([], "car", ranged=False) == ([], False)
