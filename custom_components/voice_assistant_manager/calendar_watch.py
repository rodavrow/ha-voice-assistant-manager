# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Watches the calendars of one router and hands due events to the script that speaks.

Once a minute it asks each calendar for its events with calendar.get_events, works out
which are due (calendars.py has the rules), writes a line for each with the agent
without tools, fires an event and calls the script chosen in the settings. It never
speaks itself, and it never sends anything to the main agent.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, tzinfo
from typing import Any, TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .announce import async_compose_calendar_line
from .calendars import (
    DEFAULT_ALL_DAY_TIME,
    DEFAULT_CATCH_UP_HOURS,
    EVENT_CALENDAR_DUE,
    KIND_CALENDAR,
    Occurrence,
    describe_when,
    due_at,
    end_at,
    event_key,
    late_limit,
    matches_filter,
    owner_value,
    parse_occurrence,
    parse_time,
    should_announce,
)
from .const import DOMAIN
from .reminders import assistant_name

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
POLL_INTERVAL = timedelta(minutes=1)
# An all-day event can be due early on the day before it starts.
LOOK_AHEAD = timedelta(days=2)
# The last-check times only matter after a restart, and a few minutes more or less
# change nothing then, so they are not worth a write to disk every minute.
SAVE_EVERY = timedelta(minutes=15)
# Just after a start a calendar may not have read its events yet. An empty answer
# then does not count as a check, or the catch-up would find nothing to catch up.
STARTUP_GRACE = timedelta(minutes=10)
KEEP_ANNOUNCED = timedelta(days=1)
WATCHERS = "calendar_watchers"
# A calendar that does not answer in this many seconds is treated as one that failed.
FETCH_TIMEOUT = 30
# More due events than this in one check of one calendar is abnormal (an import, or a
# catch-up after an outage): the rest are marked as announced and stay silent.
MAX_ANNOUNCEMENTS_PER_CHECK = 5
# The settings page limits a lead time to a day; anything outside this is not a lead time.
MAX_LEAD_MINUTES = 1440


class _Stored(TypedDict):
    last_check: dict[str, str]
    announced: dict[str, str]
    last: dict[str, str] | None


def _storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.calendars.{entry_id}"


def calendar_rows(options: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The calendars a router watches, without rows that name no calendar."""
    rows = options.get("calendar_rows")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping) and row.get("calendar")]


def _moments(raw: Any) -> dict[str, datetime]:
    """Stored times that can still be read, as aware datetimes."""
    moments: dict[str, datetime] = {}
    if not isinstance(raw, Mapping):
        return moments
    for key, value in raw.items():
        parsed = dt_util.parse_datetime(value) if isinstance(value, str) else None
        if isinstance(key, str) and parsed is not None and parsed.tzinfo is not None:
            moments[key] = parsed
    return moments


def _lead_time(row: Mapping[str, Any]) -> timedelta | None:
    """The row's lead time, or None when it is not 0 to MAX_LEAD_MINUTES minutes."""
    try:
        minutes = float(row.get("lead_minutes") or 0)
    except (TypeError, ValueError):
        return None
    if not 0 <= minutes <= MAX_LEAD_MINUTES:  # also False for nan
        return None
    return timedelta(minutes=minutes)


class CalendarWatcher:
    """The calendars of one router: what was checked when, and what was announced."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._store: Store[_Stored] = Store(hass, STORAGE_VERSION, _storage_key(entry.entry_id))
        self._last_check: dict[str, datetime] = {}
        self._announced: dict[str, datetime] = {}
        self._failing: set[str] = set()
        self._row_errors: set[str] = set()  # calendars whose row error was logged already
        self._bad_lead: set[str] = set()
        self._lock = asyncio.Lock()
        self._stopped = False
        self._cancel_timer: Callable[[], None] | None = None
        self._first_check: datetime | None = None
        self._saved_at: datetime | None = None
        self._last: dict[str, str] | None = None

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        data: Mapping[str, Any] = stored if isinstance(stored, Mapping) else {}
        self._last_check = _moments(data.get("last_check"))
        self._announced = _moments(data.get("announced"))
        last = data.get("last")
        if isinstance(last, Mapping):
            self._last = {
                key: str(last.get(key) or "") for key in ("title", "calendar", "line", "at")
            }

    def async_start(self) -> None:
        """Check every minute until the router is unloaded."""

        async def tick(now: datetime) -> None:
            if self._lock.locked():  # A slow agent: the next minute will do.
                return
            await self.async_check(now)

        self._cancel_timer = async_track_time_interval(self._hass, tick, POLL_INTERVAL)

    async def async_stop(self) -> None:
        """Stop for good and write down where it was, so a reload picks up from here.

        A check that is running finishes the event it is on and announces no more.
        """
        self._stopped = True
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        async with self._lock:
            await self._store.async_save(self._data())

    def _data(self) -> _Stored:
        return {
            "last_check": {key: value.isoformat() for key, value in self._last_check.items()},
            "announced": {key: value.isoformat() for key, value in self._announced.items()},
            "last": self._last,
        }

    @property
    def last_announced(self) -> dict[str, str] | None:
        """The event announced most recently: its title, calendar, line and time."""
        return dict(self._last) if self._last else None

    async def async_check(self, now: datetime) -> None:
        """Announce every event that is due at `now` and was not announced before."""
        async with self._lock:
            rows = calendar_rows(self._entry.options)
            if self._stopped or not rows:
                return
            if self._first_check is None:
                self._first_check = now
            settings = self._entry.options.get("calendars") or {}
            try:
                hours = float(settings.get("catch_up_hours", DEFAULT_CATCH_UP_HOURS))
            except (TypeError, ValueError):
                hours = DEFAULT_CATCH_UP_HOURS
            catch_up = timedelta(hours=max(hours, 0))
            for row in rows:
                # async_stop sets it during an await below, which mypy cannot know.
                if self._stopped:
                    break  # type: ignore[unreachable]
                calendar = str(row["calendar"])
                try:
                    await self._check_calendar(row, settings, now, catch_up)
                except Exception:  # noqa: BLE001 - one bad row must not stop the others
                    if calendar not in self._row_errors:  # once, until the row works again
                        _LOGGER.exception("Could not check calendar %s", calendar)
                    self._row_errors.add(calendar)
                else:
                    self._row_errors.discard(calendar)
            if self._stopped:  # async_stop writes what there is to write.
                return  # type: ignore[unreachable]
            self._forget_old(now, catch_up, {str(row["calendar"]) for row in rows})
            if self._saved_at is None or abs(now - self._saved_at) >= SAVE_EVERY:
                await self._save(now)

    async def _check_calendar(
        self,
        row: Mapping[str, Any],
        settings: Mapping[str, Any],
        now: datetime,
        catch_up: timedelta,
    ) -> None:
        calendar = str(row["calendar"])
        zone = dt_util.get_default_time_zone()
        lead = _lead_time(row)
        if lead is None:
            if calendar not in self._bad_lead:
                _LOGGER.warning(
                    "Skipping %s: its lead time %r is not between 0 and %d minutes",
                    calendar,
                    row.get("lead_minutes"),
                    MAX_LEAD_MINUTES,
                )
            self._bad_lead.add(calendar)
            return
        self._bad_lead.discard(calendar)
        events = await self._fetch(
            calendar, now - late_limit(catch_up), now + LOOK_AHEAD + lead, zone
        )
        if events is None:
            # The last check stays where it was, so what was missed is caught up later.
            return
        assert self._first_check is not None  # noqa: S101 - set by async_check
        starting = abs(now - self._first_check) < STARTUP_GRACE
        default_time = parse_time(settings.get("all_day_time")) or parse_time(DEFAULT_ALL_DAY_TIME)
        assert default_time is not None  # noqa: S101 - the constant is a valid time
        last_check = self._last_check.get(calendar)
        due_now: list[tuple[datetime, Occurrence]] = []
        for event in events:
            if not matches_filter(event.summary, row.get("contains")):
                continue
            due = due_at(event, row, default_time, zone)
            if event_key(event) in self._announced:
                continue
            if should_announce(due, end_at(event, zone), now, last_check, catch_up):
                due_now.append((due, event))
        due_now.sort(key=lambda pair: pair[0])  # the soonest are the ones worth saying
        announced = 0
        skipped = 0
        for due, event in due_now:
            if self._stopped:
                return
            key = event_key(event)
            if key in self._announced:  # the same event listed twice
                continue
            # Written down before anything is said: once at most, also after a crash.
            # Kept until the event is over, whatever the calendar's times are changed to.
            self._announced[key] = max(due, end_at(event, zone))
            if announced >= MAX_ANNOUNCEMENTS_PER_CHECK:
                skipped += 1
                continue
            await self._save(now)
            announced += 1
            await self._announce(row, event, now, zone)
        if skipped:
            await self._save(now)  # one write for all that were skipped
            _LOGGER.warning(
                "%s had %d events due at once: announced %d, skipped the other %d",
                calendar,
                announced + skipped,
                announced,
                skipped,
            )
        if events or not starting:
            self._last_check[calendar] = now

    async def _fetch(
        self, calendar: str, start: datetime, end: datetime, zone: tzinfo
    ) -> list[Occurrence] | None:
        """The calendar's events in a window, or None when it could not be asked."""
        try:
            async with asyncio.timeout(FETCH_TIMEOUT):
                response = await self._hass.services.async_call(
                    "calendar",
                    "get_events",
                    {
                        "entity_id": calendar,
                        "start_date_time": start.isoformat(),
                        "end_date_time": end.isoformat(),
                    },
                    blocking=True,
                    return_response=True,
                )
        except TimeoutError:
            if calendar not in self._failing:
                _LOGGER.warning("%s did not answer within %s seconds", calendar, FETCH_TIMEOUT)
            self._failing.add(calendar)
            return None
        except Exception as err:  # noqa: BLE001 - one calendar must not stop the others
            if calendar not in self._failing:
                _LOGGER.warning("Could not get the events of %s: %s", calendar, err)
            self._failing.add(calendar)
            return None
        answer = response.get(calendar) if isinstance(response, Mapping) else None
        raw = answer.get("events") if isinstance(answer, Mapping) else None
        if not isinstance(raw, list):
            if calendar not in self._failing:
                _LOGGER.warning("%s did not answer with a list of events", calendar)
            self._failing.add(calendar)
            return None
        self._failing.discard(calendar)
        events = [parse_occurrence(calendar, item, zone) for item in raw]
        return [event for event in events if event is not None]

    async def _announce(
        self, row: Mapping[str, Any], event: Occurrence, now: datetime, zone: tzinfo
    ) -> None:
        options = self._entry.options
        owner = owner_value(options, row.get("for"))
        line = await async_compose_calendar_line(
            self._hass,
            self._entry,
            owner,
            event.summary,
            describe_when(event, now, zone),
            str(row.get("hint") or ""),
        )
        data = {
            "owner": owner,
            "title": event.summary,
            "line": line,
            "assistant": assistant_name(
                self._entry.title, (options.get("reminders") or {}).get("assistant")
            ),
            "calendar": event.calendar,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "all_day": event.all_day,
            "kind": KIND_CALENDAR,
        }
        self._last = {
            "title": event.summary,
            "calendar": event.calendar,
            "line": line,
            "at": now.isoformat(),
        }
        context = Context()
        self._hass.bus.async_fire(EVENT_CALENDAR_DUE, data, context=context)
        script = str((options.get("calendars") or {}).get("script") or "").strip()
        if not script:
            return
        try:
            await self._hass.services.async_call(
                "script",
                "turn_on",
                {"entity_id": script, "variables": dict(data)},
                blocking=False,
                context=context,
            )
        except Exception as err:  # noqa: BLE001 - one event must not stop the others
            _LOGGER.warning("Could not call %s for a calendar event: %s", script, err)

    async def _save(self, now: datetime) -> None:
        self._saved_at = now
        await self._store.async_save(self._data())

    def _forget_old(self, now: datetime, catch_up: timedelta, calendars: set[str]) -> None:
        """Drop what can no longer be announced again, and calendars no longer watched."""
        limit = late_limit(catch_up) + KEEP_ANNOUNCED
        self._announced = {
            key: over for key, over in self._announced.items() if now - over <= limit
        }
        self._last_check = {
            calendar: when for calendar, when in self._last_check.items() if calendar in calendars
        }


async def async_start_watcher(hass: HomeAssistant, entry: ConfigEntry) -> CalendarWatcher:
    """Load and start the watcher of a router entry."""
    watcher = CalendarWatcher(hass, entry)
    await watcher.async_load()
    hass.data.setdefault(DOMAIN, {}).setdefault(WATCHERS, {})[entry.entry_id] = watcher
    watcher.async_start()
    return watcher


async def async_stop_watcher(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Stop the watcher of a router entry that is being unloaded."""
    watcher = hass.data.get(DOMAIN, {}).get(WATCHERS, {}).pop(entry.entry_id, None)
    if watcher is not None:
        await watcher.async_stop()


async def async_remove_watcher_data(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete what the watcher of a removed router entry kept."""
    await Store[_Stored](hass, STORAGE_VERSION, _storage_key(entry.entry_id)).async_remove()


def find_watcher(hass: HomeAssistant, entry_id: str) -> CalendarWatcher | None:
    """The running watcher of a router entry, or None when it is not loaded."""
    watcher: CalendarWatcher | None = (
        hass.data.get(DOMAIN, {}).get(WATCHERS, {}).get(entry_id)
    )
    return watcher


def get_watcher(hass: HomeAssistant, entry_id: str) -> CalendarWatcher:
    """The running watcher of a router entry."""
    watcher: CalendarWatcher = hass.data[DOMAIN][WATCHERS][entry_id]
    return watcher
