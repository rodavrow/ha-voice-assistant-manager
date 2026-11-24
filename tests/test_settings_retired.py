# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings an older version stored, which the forms no longer know, never block a save."""

from .router_helpers import BASE_OPTIONS, add_router
from .test_panel import Page, page  # noqa: F401 - fixture


def stored_by_0_5_1() -> dict:
    """Options as 0.5.1 betas saved them, with the retired "Wait for the voice match"."""
    advanced = {k: v for k, v in BASE_OPTIONS["advanced"].items() if k != "match_within"}
    return {**BASE_OPTIONS, "advanced": {**advanced, "wait_seconds": 3}}


async def test_regression_the_page_saves_settings_stored_with_the_old_wait(hass, page):
    entry = await add_router(hass, stored_by_0_5_1())
    settings = (await page.router(entry))["settings"]

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert "wait_seconds" not in entry.options["advanced"]
    assert entry.options["advanced"]["match_within"] == 6


async def test_the_page_never_shows_the_retired_setting(hass, page):
    entry = await add_router(hass, stored_by_0_5_1())

    settings = (await page.router(entry))["settings"]

    assert "wait_seconds" not in settings["advanced"]


async def test_a_page_opened_before_the_update_can_still_save(hass, page):
    """A browser tab from before the update still holds the old key and sends it."""
    entry = await add_router(hass)
    settings = (await page.router(entry))["settings"]
    settings["advanced"] = {**settings["advanced"], "wait_seconds": 3}

    result = await page.ok("save_settings", entry_id=entry.entry_id, settings=settings)

    assert result["ok"] is True
    assert "wait_seconds" not in entry.options["advanced"]
