# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures for the Voice Assistant Manager tests."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components/."""
    yield


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    """Each test gets a configuration directory of its own.

    The harness's default is tests/testing_config, shared by every test and every run, so
    a file one test wrote there (the setup guide writes automations) reached the next.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return str(config_dir)
