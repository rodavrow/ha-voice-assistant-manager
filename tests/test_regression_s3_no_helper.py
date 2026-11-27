# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for S3 of the 2026-11-27 review: a device without a helper.

"A device without speaker recognition is" defaulted to Person 1. So a satellite whose
helper was missing answered everyone as Person 1 with full access. That includes a
satellite that was paired again: it gets a new device id, and the helper named after the
old one no longer belongs to it.
"""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voice_assistant_manager.config_flow import current_settings
from custom_components.voice_assistant_manager.const import DOMAIN
from custom_components.voice_assistant_manager.conversation import UNSURE_LINE

from .router_helpers import MAIN_AGENT, FakeAgents, add_router, ask, options_with
from .test_speaker_follow_up import add_satellite

REFUSAL = "The answer is no."


def add_speaker_sensors(hass: HomeAssistant, device_id: str) -> None:
    """The sensors of a satellite that recognises speakers."""
    for name in ("last_speaker", "last_speaker_score"):
        er.async_get(hass).async_get_or_create(
            "sensor", "test", f"{device_id}-{name}",
            suggested_object_id=f"kitchen_satellite_{name}", device_id=device_id,
        )


def spoken(result) -> str:
    return str(result.response.speech["plain"]["speech"])


async def test_regression_s3_a_new_assistant_proposes_an_unknown_voice(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, title="Kitchen assistant", data={"agent": MAIN_AGENT})
    entry.add_to_hass(hass)

    assert current_settings(hass, entry)["advanced"]["no_helper_role"] == "stranger"


async def test_an_assistant_saved_before_keeps_what_it_did(hass: HomeAssistant):
    """Settings saved without the choice meant Person 1, and still do."""
    options = options_with()
    del options["advanced"]["no_helper_role"]
    entry = await add_router(hass, options)
    kitchen = add_satellite(hass)
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, "Hello", device_id=kitchen)

    assert "The person speaking is Alex." in agents.main_prompt


async def test_regression_s3_a_satellite_that_recognises_speakers_but_has_no_helper_is_refused(
    hass: HomeAssistant, caplog
):
    """Paired again, say: Person 1 is not assumed for a satellite that could tell."""
    entry = await add_router(hass, options_with(advanced={"no_helper_role": "owner"}))
    kitchen = add_satellite(hass)
    add_speaker_sensors(hass, kitchen)
    agents = FakeAgents()

    with agents.patched():
        first = await ask(hass, entry, "Hello", device_id=kitchen)
        second = await ask(hass, entry, "Hello", device_id=kitchen)

    assert (spoken(first), spoken(second)) == (UNSURE_LINE, UNSURE_LINE)
    assert agents.calls_to(MAIN_AGENT) == []
    assert caplog.text.count(f"input_text.voice_speaker_{kitchen}") == 1


async def test_a_device_without_speaker_recognition_still_goes_by_the_setting(
    hass: HomeAssistant,
):
    entry = await add_router(hass, options_with(advanced={"no_helper_role": "owner"}))
    kitchen = add_satellite(hass)
    agents = FakeAgents()

    with agents.patched():
        await ask(hass, entry, "Hello", device_id=kitchen)

    assert "The person speaking is Alex." in agents.main_prompt


async def test_set_to_an_unknown_voice_a_device_without_a_helper_gets_the_refusal(
    hass: HomeAssistant,
):
    entry = await add_router(hass, options_with(advanced={"no_helper_role": "stranger"}))
    agents = FakeAgents()

    with agents.patched():
        result = await ask(hass, entry, "Hello", device_id=add_satellite(hass))

    assert spoken(result) == REFUSAL
    assert agents.calls == []
