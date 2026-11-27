# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two pages: where they live in Home Assistant, and whether the sidebar shows them.

The settings page is frontend/panel.js, for administrators; what it may ask for is in
panel_api.py. The My assistant page is frontend/mine.js, for everyone; what it may ask for
is in mine_api.py.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from . import announcement_look, data_keys
from .chat_show import ChatLimits
from .const import DOMAIN
from .stage import SCRIPT_URL as STAGE_SCRIPT_URL
from .stage import StageSettings

PANEL_URL = "voice-assistant-manager"
PANEL_ELEMENT = "voice-assistant-manager-panel"
PANEL_TITLE = "Voice Manager"
PANEL_ICON = "mdi:account-voice"
SCRIPT_URL = f"/{DOMAIN}/panel.js"
SCRIPT_FILE = Path(__file__).parent / "frontend" / "panel.js"
MINE_URL = "my-assistant"
MINE_ELEMENT = "voice-assistant-manager-mine"
MINE_TITLE = "My assistant"
MINE_SCRIPT_URL = f"/{DOMAIN}/mine.js"
MINE_SCRIPT_FILE = Path(__file__).parent / "frontend" / "mine.js"
# The card of the chat stage, cast to Google Cast devices (stage.py).
STAGE_SCRIPT_FILE = Path(__file__).parent / "frontend" / "stage.js"
# The element for talking to an avatar, which the page fetches when Talk is pressed (talk.py).
TALK_SCRIPT_URL = f"/{DOMAIN}/talk.js"
TALK_SCRIPT_FILE = Path(__file__).parent / "frontend" / "talk.js"
STORAGE_VERSION = 1
GuideState = Literal["done", "dismissed", "started"]
GUIDE_STATES: tuple[GuideState, ...] = ("done", "dismissed", "started")
# A finished or skipped guide is stored as its state; a started one with the step it is at.
_FINAL_STATES = ("done", "dismissed")
STARTED = "started"
GUIDE_STEPS = 7
_PLAYER_PREFIX = "media_player."
# "done", "dismissed" or {"state": "started", "step": n}, as kept in the Store.
StoredGuide = str | dict[str, Any]


@dataclass
class PanelState:
    """What is kept about the page between restarts, and how it was registered."""

    store: Store[dict[str, Any]]
    sidebar: bool
    module_url: str
    # Where each router's setup guide stands, by config entry id.
    guide: dict[str, StoredGuide]
    # The limits a chat starts with, as saved on the page; None until someone saves them.
    chat_defaults: ChatLimits | None = None
    # The player each satellite plays favorites on: device id -> media_player entity id.
    speaker_overrides: dict[str, str] = field(default_factory=dict)
    # Whether the speaking assistant's character is chosen on devices that offer one.
    announcement_look: bool = True
    # Whether a chat on a cast device shows the avatars there, and with captions.
    chat_stage: StageSettings = field(default_factory=StageSettings)
    # Where the stage card is served, with the version so an update is fetched again.
    stage_url: str = STAGE_SCRIPT_URL


def _register(hass: HomeAssistant, state: PanelState, *, update: bool) -> None:
    """(Re)register the page. Admins only: it shows and changes every setting."""
    frontend.async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=PANEL_URL,
        config={
            "_panel_custom": {
                "name": PANEL_ELEMENT,
                "module_url": state.module_url,
                "embed_iframe": False,
                "trust_external": False,
                "handle_safe_area": False,
            }
        },
        require_admin=True,
        update=update,
        show_in_sidebar=state.sidebar,
    )


def _register_mine(hass: HomeAssistant, module_url: str) -> None:
    """Register My assistant for every user, always in the sidebar.

    It shows a person only their own settings (mine_api.py checks that), so it needs no
    admin, and the sidebar switch of the settings page does not apply to it.
    """
    frontend.async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title=MINE_TITLE,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=MINE_URL,
        config={
            "_panel_custom": {
                "name": MINE_ELEMENT,
                "module_url": module_url,
                "embed_iframe": False,
                "trust_external": False,
                "handle_safe_area": False,
            }
        },
        require_admin=False,
        show_in_sidebar=True,
    )


def panel_state(hass: HomeAssistant) -> PanelState:
    state: PanelState = hass.data[DOMAIN][data_keys.PANEL]
    return state


def _is_step(step: Any) -> bool:
    # bool is an int in Python, and never a step.
    return isinstance(step, int) and not isinstance(step, bool) and 1 <= step <= GUIDE_STEPS


def _valid_guide(value: Any) -> bool:
    if isinstance(value, str):
        return value in _FINAL_STATES
    return (
        isinstance(value, dict)
        and value.get("state") == STARTED
        and _is_step(value.get("step"))
    )


def _stored_guide(stored: Any) -> dict[str, StoredGuide]:
    """The guide states in the stored data; a store from before the guide has none.

    An entry that is not a known shape is dropped, so that router falls back to its default.
    """
    guide = stored.get("guide") if isinstance(stored, dict) else None
    if not isinstance(guide, dict):
        return {}
    return {key: value for key, value in guide.items() if _valid_guide(value)}


def _stored_chat_defaults(stored: Any) -> ChatLimits | None:
    """The saved chat limits; a store from before the chat show, or a damaged one, has none."""
    saved = stored.get("chat_defaults") if isinstance(stored, dict) else None
    if not isinstance(saved, dict):
        return None
    try:
        return ChatLimits.from_dict(saved)
    except ValueError:
        return None


def _is_override(device_id: Any, entity_id: Any) -> bool:
    return (
        isinstance(device_id, str)
        and bool(device_id)
        and isinstance(entity_id, str)
        and entity_id.startswith(_PLAYER_PREFIX)
        and len(entity_id) > len(_PLAYER_PREFIX)
    )


def _stored_speaker_overrides(stored: Any) -> dict[str, str]:
    """The saved speaker per satellite; a store from before favorites, or a damaged one, has none.

    An override that is not a device id and a media_player entity id is dropped.
    """
    saved = stored.get("speaker_overrides") if isinstance(stored, dict) else None
    if not isinstance(saved, dict):
        return {}
    return {
        device_id: entity_id
        for device_id, entity_id in saved.items()
        if _is_override(device_id, entity_id)
    }


def _data(state: PanelState) -> dict[str, Any]:
    data: dict[str, Any] = {"sidebar": state.sidebar, "guide": state.guide}
    if state.speaker_overrides:
        data["speaker_overrides"] = state.speaker_overrides
    if not state.announcement_look:
        data["announcement_look"] = False
    if state.chat_defaults is not None:
        data["chat_defaults"] = asdict(state.chat_defaults)
    if state.chat_stage != StageSettings():
        data["chat_stage"] = asdict(state.chat_stage)
    return data


def chat_defaults(hass: HomeAssistant) -> ChatLimits:
    """The limits a chat starts with: the saved ones, else the built-in ones."""
    return panel_state(hass).chat_defaults or ChatLimits()


async def async_set_chat_defaults(hass: HomeAssistant, limits: ChatLimits) -> None:
    """Remember the limits a chat starts with."""
    panel = panel_state(hass)
    panel.chat_defaults = limits
    await panel.store.async_save(_data(panel))


def speaker_overrides(hass: HomeAssistant) -> dict[str, str]:
    """The player each satellite plays favorites on, by device id; {} before the page is set up."""
    state: PanelState | None = hass.data.get(DOMAIN, {}).get(data_keys.PANEL)
    return dict(state.speaker_overrides) if state is not None else {}


async def async_set_speaker_overrides(hass: HomeAssistant, overrides: Mapping[str, str]) -> None:
    """Replace every speaker override. Raises ValueError for one that is not device -> player."""
    for device_id, entity_id in overrides.items():
        if not _is_override(device_id, entity_id):
            raise ValueError(
                f"A speaker override needs a device and a media player, not {device_id!r} -> "
                f"{entity_id!r}"
            )
    panel = panel_state(hass)
    panel.speaker_overrides = dict(overrides)
    await panel.store.async_save(_data(panel))


def guide_state(hass: HomeAssistant, entry_id: str) -> GuideState | None:
    """The stored state of a router's guide, None if it has none."""
    value = panel_state(hass).guide.get(entry_id)
    if isinstance(value, dict):
        return "started"
    if value == "done":
        return "done"
    if value == "dismissed":
        return "dismissed"
    return None


def guide_step(hass: HomeAssistant, entry_id: str) -> int | None:
    """The step a started guide is at, None for any other state."""
    value = panel_state(hass).guide.get(entry_id)
    return int(value["step"]) if isinstance(value, dict) else None


async def async_set_guide(
    hass: HomeAssistant, entry_id: str, state: GuideState, step: int | None = None
) -> None:
    """Remember where the guide of one router stands: started at a step, finished or skipped.

    Raises ValueError for a started guide without a step from 1 to GUIDE_STEPS.
    """
    value: StoredGuide
    if state == STARTED:
        if not _is_step(step):
            raise ValueError(f"A started guide needs a step from 1 to {GUIDE_STEPS}, not {step!r}")
        value = {"state": STARTED, "step": step}
    else:
        value = state
    panel = panel_state(hass)
    panel.guide[entry_id] = value
    await panel.store.async_save(_data(panel))


async def async_setup_panel(hass: HomeAssistant) -> None:
    """Serve the scripts of both pages and register them. Called once per start."""
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.panel")
    stored = await store.async_load()
    integration = await async_get_integration(hass, DOMAIN)
    state = PanelState(
        store=store,
        # Shown unless someone chose to hide it.
        sidebar=not (isinstance(stored, dict) and stored.get("sidebar") is False),
        # The version in the address makes a browser fetch the script again after an update.
        module_url=f"{SCRIPT_URL}?v={integration.version}",
        guide=_stored_guide(stored),
        chat_defaults=_stored_chat_defaults(stored),
        speaker_overrides=_stored_speaker_overrides(stored),
        # On unless someone switched it off.
        announcement_look=not (
            isinstance(stored, dict) and stored.get("announcement_look") is False
        ),
        chat_stage=StageSettings.from_dict(
            stored.get("chat_stage") if isinstance(stored, dict) else None
        ),
        stage_url=f"{STAGE_SCRIPT_URL}?v={integration.version}",
    )
    announcement_look.set_enabled(hass, state.announcement_look)
    data_keys.domain_data(hass)[data_keys.PANEL] = state
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(SCRIPT_URL, str(SCRIPT_FILE), cache_headers=False),
            StaticPathConfig(MINE_SCRIPT_URL, str(MINE_SCRIPT_FILE), cache_headers=False),
            StaticPathConfig(STAGE_SCRIPT_URL, str(STAGE_SCRIPT_FILE), cache_headers=False),
            StaticPathConfig(TALK_SCRIPT_URL, str(TALK_SCRIPT_FILE), cache_headers=False),
        ]
    )
    _register(hass, state, update=False)
    _register_mine(hass, f"{MINE_SCRIPT_URL}?v={integration.version}")


async def async_set_chat_stage(hass: HomeAssistant, settings: StageSettings) -> None:
    """Remember whether chats on cast devices show the avatars, and with captions."""
    state = panel_state(hass)
    state.chat_stage = settings
    await state.store.async_save(_data(state))


async def async_set_announcement_look(hass: HomeAssistant, on: bool) -> bool:
    """Switch choosing the speaking assistant's character on or off, for every assistant."""
    state = panel_state(hass)
    state.announcement_look = on
    announcement_look.set_enabled(hass, on)
    await state.store.async_save(_data(state))
    return on


async def async_set_sidebar(hass: HomeAssistant, show: bool) -> bool:
    """Show the page in the sidebar or take it out. It stays reachable by its address."""
    state = panel_state(hass)
    state.sidebar = show
    _register(hass, state, update=True)
    await state.store.async_save(_data(state))
    return show
