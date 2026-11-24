# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Satellites follow their assistants' avatars: avatar mode on where an avatar is on."""

import logging
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import avatar, avatar_devices
from custom_components.voice_assistant_manager.avatar_devices import (
    CONNECTION_OPTIONS,
    SlotWish,
    device_sessions,
)

from .router_helpers import add_router, options_with
from .test_avatar_tts import BASE, add_avatar, engines, sent  # noqa: F401 - fixtures

SATELLITE = "assist_satellite.kitchen_satellite"


MAC = "aa:bb:cc:dd:ee:ff"


def add_entity(
    hass, owner, device_id, domain, object_id, state, *, named: str | None = None
) -> str:
    """An ESPHome-like entity: unique_id from the object id, entity id from the names."""
    entity = er.async_get(hass).async_get_or_create(
        domain,
        "esphome",
        f"{MAC}-{domain}-{object_id}",
        suggested_object_id=named or object_id,
        device_id=device_id,
        config_entry=owner,
    )
    hass.states.async_set(entity.entity_id, state)
    return entity.entity_id


def add_satellite_device(hass: HomeAssistant, *, avatar_entities: bool = True) -> None:
    """A satellite whose two assistant sub-devices carry the avatar entities."""
    owner = MockConfigEntry(domain="esphome")
    owner.add_to_hass(hass)
    devices = dr.async_get(hass)
    main = devices.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("esphome", "kitchen")}, name="Kitchen"
    )
    add_entity(hass, owner, main.id, "assist_satellite", "kitchen_satellite", "idle")
    if not avatar_entities:
        return
    for slot in (1, 2):
        sub = devices.async_get_or_create(
            config_entry_id=owner.entry_id,
            identifiers={("esphome", f"kitchen-assistant-{slot}")},
            name=f"Kitchen Assistant {slot}",
            via_device_id=main.id,
        )
        named = f"kitchen_assistant_{slot}"
        for domain, kind, state in (
            ("switch", "mode", "off"),
            ("text", "url", ""),
            ("text", "id", ""),
            ("select", "connection", "Connect when woken"),
        ):
            add_entity(
                hass, owner, sub.id, domain, f"avatar_{kind}_{slot}", state,
                named=f"{named}_avatar_{kind}",
            )


class Device:
    """The service calls a reconcile made, as 'service entity value'."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.calls: list[str] = []
        self.fail = False
        for domain, service in (
            ("switch", "turn_on"),
            ("switch", "turn_off"),
            ("text", "set_value"),
            ("select", "select_option"),
        ):
            hass.services.async_register(domain, service, self._handler(hass, domain, service))

    def _handler(self, hass, domain, service):
        async def handle(call: ServiceCall) -> None:
            if self.fail:
                raise HomeAssistantError("device offline")
            entity_id = call.data["entity_id"]
            entity_id = entity_id[0] if isinstance(entity_id, list) else entity_id
            value = call.data.get("value", call.data.get("option"))
            self.calls.append(" ".join(str(part) for part in (service, entity_id, value)))
            state = {"turn_on": "on", "turn_off": "off"}.get(service, value)
            hass.states.async_set(entity_id, state)

        return handle


@pytest.fixture
def device(hass) -> Device:
    add_satellite_device(hass)
    return Device(hass)


def slots(*entries) -> Any:
    """Patch the wake-word slots of the kitchen satellite to these assistants."""
    ids = [entry.entry_id if entry is not None else None for entry in entries]
    return patch.object(avatar_devices, "satellite_slots", return_value={SATELLITE: ids})


async def reconcile(hass) -> None:
    await avatar_devices.async_reconcile(hass)
    await hass.async_block_till_done()


# Avatar mode follows the avatar


async def test_a_slot_whose_assistant_has_an_avatar_gets_avatar_mode(
    hass, engines, sent, device
):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    set_avatar_id(hass, entry, "ada")
    device.calls.clear()

    with slots(entry, None):
        await reconcile(hass)

    assert device.calls == [
        f"set_value text.kitchen_assistant_1_avatar_url {BASE}",
        "set_value text.kitchen_assistant_1_avatar_id ada",
        "turn_on switch.kitchen_assistant_1_avatar_mode None",
    ]


async def test_nothing_is_sent_when_the_device_already_matches(hass, engines, sent, device):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    with slots(entry, None):
        await reconcile(hass)
    device.calls.clear()

    with slots(entry, None):
        await reconcile(hass)

    assert device.calls == []


async def test_switching_the_avatar_off_turns_avatar_mode_off(hass, engines, sent, device):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    with slots(entry, None):
        await reconcile(hass)
        device.calls.clear()
        change_avatar(hass, entry, enabled=False)
        await reconcile(hass)

    assert device.calls == ["turn_off switch.kitchen_assistant_1_avatar_mode None"]


async def test_removing_the_avatar_turns_avatar_mode_off(hass, engines, sent, device):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    with slots(entry, None):
        await reconcile(hass)
        device.calls.clear()
        hass.config_entries.async_remove_subentry(entry, next(iter(entry.subentries)))
        await hass.async_block_till_done()
        await reconcile(hass)

    assert "turn_off switch.kitchen_assistant_1_avatar_mode None" in device.calls


async def test_a_slot_that_no_longer_uses_the_assistant_is_turned_off(
    hass, engines, sent, device
):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    with slots(entry, None):
        await reconcile(hass)
    device.calls.clear()

    with slots(None, entry):
        await reconcile(hass)

    assert "turn_off switch.kitchen_assistant_1_avatar_mode None" in device.calls
    assert "turn_on switch.kitchen_assistant_2_avatar_mode None" in device.calls


async def test_each_slot_follows_its_own_assistant(hass, engines, sent, device):
    with_avatar = await add_router(hass)
    without = await add_router(hass, title="Office assistant")
    await add_avatar(hass, with_avatar, "tts.cloud_voice")
    hass.states.async_set("switch.kitchen_assistant_2_avatar_mode", "on")
    device.calls.clear()

    with slots(with_avatar, without):
        await reconcile(hass)

    assert "turn_on switch.kitchen_assistant_1_avatar_mode None" in device.calls
    assert "turn_off switch.kitchen_assistant_2_avatar_mode None" in device.calls


async def test_the_session_lifetime_comes_from_the_advanced_settings(
    hass, engines, sent, device
):
    entry = await add_router(hass, options_with(advanced={"avatar_connection": "always"}))
    await add_avatar(hass, entry, "tts.cloud_voice")
    device.calls.clear()

    with slots(entry, None):
        await reconcile(hass)

    assert (
        f"select_option select.kitchen_assistant_1_avatar_connection {CONNECTION_OPTIONS['always']}"
        in device.calls
    )


# Devices that cannot, and failures


async def test_a_satellite_without_avatar_entities_is_left_alone(hass, engines, sent):
    add_satellite_device(hass, avatar_entities=False)
    device = Device(hass)
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")

    with slots(entry, None):
        await reconcile(hass)

    assert device.calls == []


async def test_a_device_that_refuses_is_logged_and_never_raises(
    hass, engines, sent, device, caplog
):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    device.fail = True

    with slots(entry, None):
        await reconcile(hass)

    assert any(
        record.levelno == logging.WARNING and "kitchen_assistant_1_avatar" in record.getMessage()
        for record in caplog.records
    )


# When it runs


async def test_changing_an_avatar_updates_the_devices(hass, engines, sent, device):
    entry = await add_router(hass)
    with slots(entry, None):
        await add_avatar(hass, entry, "tts.cloud_voice")
        await hass.async_block_till_done()
        assert "turn_on switch.kitchen_assistant_1_avatar_mode None" in device.calls
        device.calls.clear()

        change_avatar(hass, entry, url="http://other.local:8010")
        await hass.async_block_till_done()

    assert device.calls == ["set_value text.kitchen_assistant_1_avatar_url http://other.local:8010"]


# The wishes themselves


def test_a_slot_wish_turns_off_without_touching_the_other_settings():
    assert SlotWish.off().on is False


# Device sessions, which the copy for other viewers skips


def test_device_sessions_are_read_from_the_avatar_session_sensors(hass):
    owner = MockConfigEntry(domain="esphome")
    owner.add_to_hass(hass)
    for object_id, state in (
        ("avatar_session_1", "abc-1"),
        ("avatar_session_2", ""),
        ("last_speaker", "xyz"),
    ):
        add_entity(hass, owner, None, "sensor", object_id, state, named=f"renamed_{object_id}")
    # Named like a session sensor, but not one: only the registry's unique_id counts.
    hass.states.async_set("sensor.fake_avatar_session_1", "not-a-device")

    assert device_sessions(hass) == frozenset({"abc-1"})


async def test_avatar_entities_are_found_after_the_user_renamed_them(
    hass, engines, sent, device
):
    registry = er.async_get(hass)
    registry.async_update_entity(
        "switch.kitchen_assistant_1_avatar_mode", new_entity_id="switch.my_face_toggle"
    )
    hass.states.async_set("switch.my_face_toggle", "off")
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    device.calls.clear()

    with slots(entry, None):
        await reconcile(hass)

    assert "turn_on switch.my_face_toggle None" in device.calls


# Helpers


def change_avatar(hass, entry, **changes: Any) -> None:
    subentry = next(iter(entry.subentries.values()))
    keys = {"enabled": avatar.CONF_ENABLED, "url": avatar.CONF_URL}
    data = {**subentry.data, **{keys[key]: value for key, value in changes.items()}}
    hass.config_entries.async_update_subentry(entry, subentry, data=data)


def set_avatar_id(hass, entry, avatar_id: str) -> None:
    subentry = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        entry, subentry, data={**subentry.data, avatar.CONF_AVATAR_ID: avatar_id}
    )

