# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""frontend/vam_common.js: what both pages share, served next to them and kept in step
with the limits the Python side enforces."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from custom_components.voice_assistant_manager import favorites, mine_api
from custom_components.voice_assistant_manager.chat_text import LIMIT_RANGES

FRONTEND = Path(__file__).parent.parent / "custom_components/voice_assistant_manager/frontend"
COMMON = (FRONTEND / "vam_common.js").read_text()
PAGES = ("panel.js", "mine.js")


def exported() -> set[str]:
    return set(re.findall(r"^export (?:const|function) (\w+)", COMMON, re.M))


def imported_by(page: str) -> set[str]:
    """The names a page takes from vam_common.js (its one `const { ... } = await import`)."""
    found = re.search(
        r"const \{([^}]*)\} = await import\([^)]*vam_common\.js", (FRONTEND / page).read_text()
    )
    assert found, f"{page} does not import vam_common.js"
    return {name.strip() for name in found.group(1).split(",") if name.strip()}


def constant(name: str) -> Any:
    """A constant of vam_common.js that is JSON once its keys are quoted and trailing commas
    dropped (numbers, strings, arrays of objects). Any: whatever JSON the constant holds."""
    found = re.search(rf"^export const {name} = (.*?);$", COMMON, re.M | re.S)
    assert found, f"vam_common.js has no constant {name}"
    text = re.sub(r"([{,]\s*)(\w+):", r'\1"\2":', found.group(1))
    text = re.sub(r",(\s*[\]}])", r"\1", text)
    return json.loads(text)


async def test_the_shared_script_is_served(hass, hass_client):
    from .router_helpers import add_router

    await add_router(hass)
    client = await hass_client()

    response = await client.get("/voice_assistant_manager/vam_common.js?v=1")

    assert response.status == 200
    assert "export function h(" in await response.text()


@pytest.mark.parametrize("page", PAGES)
def test_a_page_takes_only_names_the_shared_script_exports(page):
    assert imported_by(page) <= exported()


@pytest.mark.parametrize("page", PAGES)
def test_a_page_imports_the_shared_script_with_its_own_version(page):
    text = (FRONTEND / page).read_text()

    assert "vam_common.js${new URL(import.meta.url).search}" in text


@pytest.mark.parametrize("page", PAGES)
def test_a_page_imports_every_shared_name_it_uses(page):
    text = (FRONTEND / page).read_text()

    used = {name for name in exported() if re.search(rf"\b{name}\b", text)}

    assert used <= imported_by(page)


@pytest.mark.parametrize("page", PAGES)
def test_a_page_does_not_define_a_shared_name_again(page):
    text = (FRONTEND / page).read_text()

    defined = set(re.findall(r"^(?:const|function) (\w+)", text, re.M))

    assert defined & exported() == set()


def test_the_chat_limits_are_the_ranges_the_chat_show_accepts():
    limits = {row["key"]: (row["min"], row["max"]) for row in constant("CHAT_LIMITS")}

    assert limits == LIMIT_RANGES


def test_the_favorite_limits_are_those_favorites_enforce():
    assert (constant("MAX_FAVORITE_NAME"), constant("MAX_FAVORITE_URL")) == (
        favorites.MAX_NAME_LENGTH,
        favorites.MAX_URL_LENGTH,
    )


def test_the_my_assistant_limits_are_those_its_commands_enforce():
    assert (constant("MAX_PERSONA"), constant("MAX_TITLE")) == (
        mine_api.MAX_PERSONA_LENGTH,
        mine_api.MAX_TITLE_LENGTH,
    )
