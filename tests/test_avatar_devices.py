# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Satellites follow their assistants' avatars: avatar mode on where an avatar is on."""

import logging
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager import avatar, avatar_devices
from custom_components.voice_assistant_manager.avatar import device_sessions
from custom_components.voice_assistant_manager.avatar_devices import (
    CONNECTION_OPTIONS,
    SlotWish,
    satellite_slots,
)

from .router_helpers import add_router, options_with
from .test_avatar_tts import BASE, add_avatar, engines, sent  # noqa: F401 - fixtures
from .test_setup_guide import (  # noqa: F401 - fixtures
    pipelines,
    router,
    without_native_audio_libraries,
)

SATELLITE = "assist_satellite.kitchen_satellite"


MAC = "aa:bb:cc:dd:ee:ff"
# What the satellite firmware calls each avatar entity: (ESPHome type, HA domain, name).
AVATAR_ENTITIES = {
    "mode": ("switch", "switch", "Avatar mode"),
    # ESPHome's type for a text entity is text_info, as on real satellites.
    "url": ("text_info", "text", "Avatar URL"),
    "id": ("text_info", "text", "Avatar ID"),
    "connection": ("select", "select", "Avatar connection"),
}


def add_entity(
    hass, owner, device_id, domain, unique_id, state, *, named: str | None = None
) -> str:
    """An ESPHome entity as Home Assistant registers it.

    Seen on real satellites: unique_id <mac>/<sub-device index>/<ESPHome type>/<name>, and an
    entity id built from the (area,) device and entity names.
    """
    entity = er.async_get(hass).async_get_or_create(
        domain,
        "esphome",
        unique_id,
        suggested_object_id=named,
        device_id=device_id,
        config_entry=owner,
    )
    hass.states.async_set(entity.entity_id, state)
    return entity.entity_id


def add_satellite_device(
    hass: HomeAssistant, *, avatar_entities: bool = True, children: bool = False
) -> None:
    """A satellite whose two assistant sub-devices carry the avatar entities.

    ESPHome registers a sub-device either as a device "via" the satellite or, with
    `children`, as a child device of it (parent_device_id), which is kept apart.
    """
    owner = MockConfigEntry(domain="esphome")
    owner.add_to_hass(hass)
    devices = dr.async_get(hass)
    main = devices.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("esphome", "kitchen")}, name="Kitchen"
    )
    add_entity(
        hass, owner, main.id, "assist_satellite", f"{MAC}-assist_satellite",
        "idle", named="kitchen_satellite",
    )
    if not avatar_entities:
        return
    # A same-named switch on the main device is not any slot's avatar mode.
    add_entity(
        hass, owner, main.id, "switch", f"{MAC}/0/switch/Avatar mode", "off",
        named="kitchen_avatar_mode",
    )
    devices.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("esphome", f"{MAC}_3")},
        name="kitchen screen",
        via_device_id=main.id,
    )
    for slot in (1, 2):
        index = slot + 3  # Not the slot: it differs from one firmware to the next.
        if children:
            sub = devices.async_get_or_create_child(
                config_entry_id=owner.entry_id,
                identifiers={("esphome", f"{MAC}_{index}")},
                name=f"kitchen assistant {slot}",
                parent_device_id=main.id,
            )
        else:
            sub = devices.async_get_or_create(
                config_entry_id=owner.entry_id,
                identifiers={("esphome", f"{MAC}_{index}")},
                name=f"kitchen assistant {slot}",
                via_device_id=main.id,
            )
        defaults = {"mode": "off", "url": "", "id": "", "connection": "Connect when woken"}
        for kind, (esphome_type, domain, name) in AVATAR_ENTITIES.items():
            add_entity(
                hass, owner, sub.id, domain, f"{MAC}/{index}/{esphome_type}/{name}",
                defaults[kind], named=f"kitchen_assistant_{slot}_avatar_{kind}",
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
    """Patch the wake-word slots (1, 2, …) of the kitchen satellite to these assistants."""
    ids = {
        slot: entry.entry_id if entry is not None else None
        for slot, entry in enumerate(entries, start=1)
    }
    return patch.object(avatar_devices, "satellite_slots", return_value={SATELLITE: ids})


def assistant_switch(slot: int) -> str:
    return f"switch.kitchen_assistant_{slot}_avatar_mode"


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

    assert device.calls == ["turn_off switch.kitchen_assistant_1_avatar_mode None"]


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


async def test_avatar_mode_stays_off_when_the_device_refused_a_setting(
    hass, engines, sent, device
):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    device.fail = True

    with slots(entry, None):
        await reconcile(hass)

    assert hass.states.get(assistant_switch(1)).state == "off"


async def test_a_device_that_keeps_refusing_is_warned_about_once(
    hass, engines, sent, device, caplog
):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    device.fail = True

    with slots(entry, None):
        await reconcile(hass)
        await reconcile(hass)

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == avatar_devices.__name__
    ]
    assert len(warnings) == 1


async def test_a_slot_without_an_address_entity_never_gets_avatar_mode(
    hass, engines, sent, device
):
    er.async_get(hass).async_remove("text.kitchen_assistant_1_avatar_url")
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    device.calls.clear()

    with slots(entry, None):
        await reconcile(hass)

    assert device.calls == []


# Only what Voice Assistant Manager switched on is switched off for other assistants


async def test_avatar_mode_someone_else_set_is_left_alone(hass, engines, sent, device):
    entry = await add_router(hass)
    hass.states.async_set(assistant_switch(2), "on")
    device.calls.clear()

    with slots(entry, None):
        await reconcile(hass)

    assert device.calls == []


async def test_removing_the_last_assistant_turns_its_avatar_mode_off(
    hass, engines, sent, device
):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    with slots(entry, None):
        await reconcile(hass)
    device.calls.clear()

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    hass.config.components.add("assist_pipeline")  # wake words can be known
    with patch.object(avatar_devices, "satellite_slots", return_value={}):
        await reconcile(hass)

    assert device.calls == [f"turn_off {assistant_switch(1)} None"]


async def test_what_was_switched_on_is_remembered_across_restarts(
    hass, hass_storage, engines, sent, device
):
    switch_id = er.async_get(hass).async_get(assistant_switch(1)).id
    hass_storage["voice_assistant_manager.avatar_devices"] = {
        "version": 1,
        "key": "voice_assistant_manager.avatar_devices",
        "data": {"owned": [switch_id]},
    }
    hass.states.async_set(assistant_switch(1), "on")
    hass.config.components.add("assist_pipeline")  # wake words can be known

    with patch.object(avatar_devices, "satellite_slots", return_value={}):
        await reconcile(hass)

    assert device.calls == [f"turn_off {assistant_switch(1)} None"]


async def test_nothing_changes_while_an_assistant_reloads(hass, engines, sent, device):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    with slots(entry, None):
        await reconcile(hass)
    device.calls.clear()
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    with patch.object(avatar_devices, "satellite_slots", return_value={}):
        await reconcile(hass)

    assert device.calls == []
    entry.mock_state(hass, ConfigEntryState.LOADED)


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


def test_an_off_wish_carries_no_settings():
    assert SlotWish.off() == SlotWish(on=False)


# Which assistant each wake word uses, from the real wake-word selects


def add_wake_word_selects(hass, router_entry, words: dict[str, str]) -> None:
    """Wake-word selects on the kitchen satellite, keyed as Home Assistant keys them."""
    satellite = er.async_get(hass).async_get(SATELLITE)
    owner = MockConfigEntry(domain="esphome")
    owner.add_to_hass(hass)
    for key, pipeline in words.items():
        entity = er.async_get(hass).async_get_or_create(
            "select",
            "esphome",
            f"{MAC}-{key}",
            suggested_object_id=f"kitchen_{key}",
            device_id=satellite.device_id,
            config_entry=owner,
            translation_key="pipeline" if key == "pipeline" else "pipeline_n",
        )
        hass.states.async_set(entity.entity_id, pipeline)


async def test_slots_come_from_the_select_keys(hass, pipelines, router):
    add_satellite_device(hass)
    add_wake_word_selects(hass, router, {"pipeline": "Jeeves", "pipeline_2": "Plain"})

    assert satellite_slots(hass) == {SATELLITE: {1: router.entry_id, 2: None}}


async def test_the_second_wake_word_stays_slot_2_without_a_first(hass, pipelines, router):
    add_satellite_device(hass)
    add_wake_word_selects(hass, router, {"pipeline_2": "Jeeves"})

    assert satellite_slots(hass) == {SATELLITE: {2: router.entry_id}}


# Device sessions, which the copy for other viewers skips


def test_device_sessions_are_read_from_the_avatar_session_sensors(hass):
    owner = MockConfigEntry(domain="esphome")
    owner.add_to_hass(hass)
    for index, name, state in (
        (4, "Avatar session", "abc-1"),
        (5, "Avatar session", ""),
        (4, "Last wake word", "xyz"),
    ):
        add_entity(
            hass, owner, None, "sensor", f"{MAC}/{index}/text_sensor/{name}", state,
            named=f"renamed_{index}_{name.replace(' ', '_').lower()}",
        )
    # Named like a session sensor, but not one: only the registry's unique_id counts.
    hass.states.async_set("sensor.kitchen_assistant_1_avatar_session", "not-a-device")

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



async def test_a_sub_device_the_user_renamed_is_still_its_slot(hass, engines, sent, device):
    """Only the device's own name ("… assistant N") counts, not the name the user gave it."""
    devices = dr.async_get(hass)
    sub = next(d for d in devices.devices if d.name == "kitchen assistant 1")
    devices.async_update_device(sub.id, name_by_user="Ada's corner")
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    device.calls.clear()

    with slots(entry, None):
        await reconcile(hass)

    assert "turn_on switch.kitchen_assistant_1_avatar_mode None" in device.calls


async def test_the_main_devices_same_named_switch_is_never_used(hass, engines, sent, device):
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")

    with slots(entry, None):
        await reconcile(hass)

    assert not any("switch.kitchen_avatar_mode " in call for call in device.calls)


# What the check did, for the log


def debug_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == avatar_devices.__name__ and record.levelno == logging.DEBUG
    ]


async def test_a_slot_left_alone_is_logged_with_the_reason(hass, engines, sent, device, caplog):
    await add_router(hass)
    caplog.set_level(logging.DEBUG, logger=avatar_devices.__name__)

    with slots(None, None):
        await reconcile(hass)

    lines = debug_lines(caplog)
    assert any(SATELLITE in line and "slot 1" in line and "no assistant" in line for line in lines)


async def test_a_satellite_without_avatar_entities_is_logged(hass, engines, sent, caplog):
    add_satellite_device(hass, avatar_entities=False)
    Device(hass)
    entry = await add_router(hass)
    caplog.set_level(logging.DEBUG, logger=avatar_devices.__name__)

    with slots(entry, None):
        await reconcile(hass)

    lines = debug_lines(caplog)
    assert any(SATELLITE in line and "no avatar entities" in line for line in lines)


async def test_regression_avatar_child_sub_devices_get_avatar_mode(hass, engines, sent):
    """Real satellites registered their assistant sub-devices as child devices, which the
    check did not search: it logged "has no avatar entities" and never set avatar mode."""
    add_satellite_device(hass, children=True)
    device = Device(hass)
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    device.calls.clear()

    with slots(entry, None):
        await reconcile(hass)

    assert f"turn_on {assistant_switch(1)} None" in device.calls


async def test_regression_avatar_text_entities_are_text_info(hass, engines, sent, device):
    """Real satellites register "Avatar URL" as <mac>/5/text_info/Avatar URL: the check
    looked for /text/ and skipped every slot as having no address entity."""
    entry = await add_router(hass)
    await add_avatar(hass, entry, "tts.cloud_voice")
    device.calls.clear()

    with slots(entry, None):
        await reconcile(hass)

    assert device.calls[0].startswith("set_value text.kitchen_assistant_1_avatar_url ")
    assert f"turn_on {assistant_switch(1)} None" in device.calls
