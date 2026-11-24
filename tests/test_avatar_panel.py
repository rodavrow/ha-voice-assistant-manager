# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Avatars on the settings page: listed with their assistant, added, changed and removed."""

from typing import Any

from homeassistant.helpers import entity_registry as er

from custom_components.voice_assistant_manager import avatar
from custom_components.voice_assistant_manager.const import DOMAIN

from .router_helpers import add_router
from .test_avatar_tts import BASE, add_avatar, engines, sent  # noqa: F401 - fixtures
from .test_panel import Page, page  # noqa: F401 - fixture

FORM: dict[str, Any] = {
    "name": "Ada avatar",
    "engine": "tts.cloud_voice",
    "url": "http://livetalking.local:8010/",
    "enabled": True,
}


async def test_the_page_lists_an_assistants_avatars(hass, page, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    (subentry_id,) = entry.subentries

    router = await page.router(entry)

    assert router["avatars"] == [
        {
            "id": subentry_id,
            "name": "Ada avatar",
            "engine": "tts.cloud_voice",
            "url": BASE,
            "enabled": True,
            "avatar_id": "",
            "entity_id": avatar_id,
        }
    ]


async def test_an_assistant_without_avatars_lists_none(hass, page):
    entry = await add_router(hass)

    assert (await page.router(entry))["avatars"] == []


async def test_adding_an_avatar_on_the_page_creates_its_entity(hass, page, engines, sent):
    entry = await add_router(hass)

    result = await page.ok("save_avatar", entry_id=entry.entry_id, avatar=FORM)
    await hass.async_block_till_done()

    assert result["ok"] is True
    (subentry,) = entry.subentries.values()
    assert subentry.title == "Ada avatar"
    assert dict(subentry.data) == {
        avatar.CONF_ENGINE: "tts.cloud_voice",
        avatar.CONF_URL: BASE,
        avatar.CONF_ENABLED: True,
        avatar.CONF_AVATAR_ID: "",
    }
    assert [row["name"] for row in result["router"]["avatars"]] == ["Ada avatar"]
    assert er.async_get(hass).async_get_entity_id("tts", DOMAIN, subentry.subentry_id)


async def test_an_empty_name_becomes_the_assistants_name_and_avatar(hass, page, engines, sent):
    entry = await add_router(hass)

    await page.ok("save_avatar", entry_id=entry.entry_id, avatar={**FORM, "name": "  "})

    (subentry,) = entry.subentries.values()
    assert subentry.title == "Kitchen assistant avatar"


async def test_the_page_refuses_an_address_that_is_not_http(hass, page, engines):
    entry = await add_router(hass)

    result = await page.ok(
        "save_avatar", entry_id=entry.entry_id, avatar={**FORM, "url": "livetalking.local"}
    )

    assert result["ok"] is False
    assert result["errors"]["url"].startswith("Enter an address starting with http://")
    assert entry.subentries == {}


async def test_the_page_refuses_an_avatar_or_a_missing_entity_as_the_engine(
    hass, page, engines, sent
):
    entry = await add_router(hass)
    first = await add_avatar(hass, entry, "tts.cloud_voice")

    wrapped = await page.ok(
        "save_avatar", entry_id=entry.entry_id, avatar={**FORM, "engine": first}
    )
    missing = await page.ok(
        "save_avatar", entry_id=entry.entry_id, avatar={**FORM, "engine": "tts.nope"}
    )

    assert wrapped["errors"]["engine"] == "Choose a real engine, not an avatar."
    assert missing["errors"]["engine"] == "That text-to-speech entity does not exist."
    assert len(entry.subentries) == 1


async def test_changing_an_avatar_on_the_page_keeps_its_entity(hass, page, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    (subentry_id,) = entry.subentries

    result = await page.ok(
        "save_avatar",
        entry_id=entry.entry_id,
        subentry_id=subentry_id,
        avatar={**FORM, "engine": "tts.local_voice", "enabled": False},
    )
    await hass.async_block_till_done()

    assert result["ok"] is True
    data = entry.subentries[subentry_id].data
    assert (data[avatar.CONF_ENGINE], data[avatar.CONF_ENABLED]) == ("tts.local_voice", False)
    assert er.async_get(hass).async_get_entity_id("tts", DOMAIN, subentry_id) == avatar_id


async def test_removing_an_avatar_on_the_page(hass, page, engines, sent):
    entry = await add_router(hass)
    avatar_id = await add_avatar(hass, entry, "tts.cloud_voice")
    (subentry_id,) = entry.subentries

    result = await page.ok(
        "save_avatar", entry_id=entry.entry_id, subentry_id=subentry_id, remove=True
    )
    await hass.async_block_till_done()

    assert result["ok"] is True
    assert result["router"]["avatars"] == []
    assert entry.subentries == {}
    assert er.async_get(hass).async_get(avatar_id) is None


async def test_an_avatar_that_does_not_exist_cannot_be_changed(hass, page, engines):
    entry = await add_router(hass)

    result = await page.ok(
        "save_avatar", entry_id=entry.entry_id, subentry_id="gone", avatar=FORM
    )

    assert result["ok"] is False
    assert entry.subentries == {}


async def test_saving_an_avatar_needs_an_assistant_that_exists(hass, page):
    await add_router(hass)

    answer = await page.call("save_avatar", entry_id="nope", avatar=FORM)

    assert answer["error"]["code"] == "not_found"


async def test_someone_who_is_not_an_admin_cannot_save_an_avatar(
    hass, hass_ws_client, hass_read_only_access_token
):
    entry = await add_router(hass)
    reader = Page(await hass_ws_client(hass, hass_read_only_access_token))

    answer = await reader.call("save_avatar", entry_id=entry.entry_id, avatar=FORM)

    assert answer["error"]["code"] == "unauthorized"
    assert entry.subentries == {}


async def test_an_avatar_id_is_saved_and_listed(hass, page, engines, sent):
    entry = await add_router(hass)

    result = await page.ok(
        "save_avatar", entry_id=entry.entry_id, avatar={**FORM, "avatar_id": "ada_v2"}
    )

    (subentry,) = entry.subentries.values()
    assert subentry.data[avatar.CONF_AVATAR_ID] == "ada_v2"
    assert result["router"]["avatars"][0]["avatar_id"] == "ada_v2"


async def test_an_avatar_id_with_odd_characters_is_refused(hass, page, engines):
    entry = await add_router(hass)

    result = await page.ok(
        "save_avatar", entry_id=entry.entry_id, avatar={**FORM, "avatar_id": "../ada"}
    )

    assert result["ok"] is False
    assert result["errors"]["avatar_id"].startswith("Use letters, digits")
    assert entry.subentries == {}


async def test_the_avatar_session_lifetime_is_saved_under_advanced(hass, page):
    entry = await add_router(hass)
    settings = (await page.router(entry))["settings"]
    settings["advanced"] = {**settings["advanced"], "avatar_connection": "always"}

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert entry.options["advanced"]["avatar_connection"] == "always"


async def test_the_avatar_session_lifetime_defaults_to_connect_when_woken(hass, page):
    entry = await add_router(hass)
    settings = (await page.router(entry))["settings"]
    settings["advanced"].pop("avatar_connection", None)

    await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert entry.options["advanced"]["avatar_connection"] == "on_wake"


async def test_an_unknown_avatar_session_lifetime_is_refused(hass, page):
    entry = await add_router(hass)
    settings = (await page.router(entry))["settings"]
    settings["advanced"] = {**settings["advanced"], "avatar_connection": "sometimes"}

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is False
    assert "avatar_connection" not in entry.options["advanced"]
