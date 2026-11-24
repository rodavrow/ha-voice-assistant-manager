# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The page asks which voices a text-to-speech engine has, to offer them as a list."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.tts import Voice

from .router_helpers import add_router
from .test_panel import Page, page  # noqa: F401 - fixture

GET_ENGINE = "homeassistant.components.tts.get_engine_instance"


@pytest.fixture(autouse=True)
async def an_assistant(hass):
    """The page's commands exist once an assistant is set up."""
    await add_router(hass)


def engine(voices, language: str = "en"):
    asked: list[str] = []

    def supported(asked_language: str):
        asked.append(asked_language)
        return voices

    return SimpleNamespace(
        default_language=language, async_get_supported_voices=supported, asked=asked
    )


async def voices_of(page: Page, fake, entity_id: str = "tts.butler"):
    with patch(GET_ENGINE, lambda hass, engine_id: fake):
        return (await page.ok("voices", engine=entity_id))["voices"]


async def test_the_voices_of_an_engine_are_listed_by_name(hass, page):
    fake = engine([Voice("id-2", "Jeeves"), Voice("id-1", "Ada")])

    voices = await voices_of(page, fake)

    assert voices == [{"id": "id-1", "name": "Ada"}, {"id": "id-2", "name": "Jeeves"}]


async def test_the_voices_are_asked_in_the_engines_own_language(hass, page):
    fake = engine([Voice("id-1", "Ada")], language="nl")

    await voices_of(page, fake)

    assert fake.asked == ["nl"]


async def test_an_engine_without_a_voice_list_gives_none(hass, page):
    assert await voices_of(page, engine(None)) == []


async def test_an_engine_that_does_not_exist_gives_none(hass, page):
    assert await voices_of(page, None) == []


async def test_an_engine_that_fails_to_list_gives_none(hass, page):
    def broken(language: str):
        raise RuntimeError("provider down")

    fake = SimpleNamespace(default_language="en", async_get_supported_voices=broken)

    assert await voices_of(page, fake) == []


async def test_something_that_is_not_a_tts_entity_is_refused(hass, page):
    answer = await page.call("voices", engine="light.kitchen")

    assert answer["success"] is False
