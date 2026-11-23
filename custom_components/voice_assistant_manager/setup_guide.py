# SPDX-FileCopyrightText: 2026 rodavrow
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the setup guide needs to know about the satellites in this Home Assistant."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.assist_pipeline.pipeline import Pipeline

PREFERRED = "preferred"
# Pipeline selects of a satellite: the first wake word, then each further one.
_FIRST_SELECT = "pipeline"
_FURTHER_SELECT = "pipeline_n"


class WakeWord(TypedDict):
    select: str  # select entity id
    pipeline_id: str | None  # None when the option names no known pipeline
    pipeline_name: str
    preferred: bool  # the select is set to "preferred"
    uses_router: bool  # pipeline.conversation_engine == router entity id
    prefer_local: bool  # pipeline.prefer_local_intents


class Satellite(TypedDict):
    entity_id: str  # assist_satellite.*
    device_id: str
    name: str  # device name_by_user or name, else entity id
    helper: str  # input_text.voice_speaker_<device_id>
    helper_exists: bool
    speaker_sensor: str | None  # sensor.*_last_speaker on the device
    score_sensor: str | None  # sensor.*_last_speaker_score on the device
    tracked: bool  # any automation references either sensor
    wake_words: list[WakeWord]  # translation_key "pipeline" first, then "pipeline_n"


def router_entity_id(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """The conversation entity this config entry registered, if it has one."""
    return er.async_get(hass).async_get_entity_id("conversation", DOMAIN, entry.entry_id)


def _pipelines_by_name(hass: HomeAssistant) -> dict[str, Pipeline]:
    """Every pipeline name with the pipeline a select set to it uses.

    Names are not unique; a satellite's select takes the first pipeline with the name, in
    store order, so the first one is kept here as well. Only call once assist_pipeline is
    set up.
    """
    from homeassistant.components.assist_pipeline.pipeline import async_get_pipelines

    by_name: dict[str, Pipeline] = {}
    for pipeline in async_get_pipelines(hass):
        by_name.setdefault(pipeline.name, pipeline)
    return by_name


def _preferred_pipeline(hass: HomeAssistant) -> Pipeline | None:
    from homeassistant.components.assist_pipeline.error import PipelineNotFound
    from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline

    try:
        return async_get_pipeline(hass)
    except PipelineNotFound:
        return None


def _wake_words(
    hass: HomeAssistant, device_entities: list[er.RegistryEntry], router_id: str | None
) -> list[WakeWord]:
    if "assist_pipeline" not in hass.config.components:
        return []
    selects = [
        entity
        for entity in device_entities
        if entity.domain == "select" and entity.translation_key in (_FIRST_SELECT, _FURTHER_SELECT)
    ]
    # The first wake word's select leads; the rest keep a stable, readable order.
    selects.sort(key=lambda entity: (entity.translation_key != _FIRST_SELECT, entity.entity_id))
    if not selects:
        return []

    by_name = _pipelines_by_name(hass)
    preferred = _preferred_pipeline(hass)
    words: list[WakeWord] = []
    for select in selects:
        state = hass.states.get(select.entity_id)
        option = state.state if state is not None else ""
        is_preferred = option == PREFERRED
        pipeline = preferred if is_preferred else by_name.get(option)
        words.append(
            WakeWord(
                select=select.entity_id,
                pipeline_id=pipeline.id if pipeline is not None else None,
                pipeline_name=pipeline.name if pipeline is not None else option,
                preferred=is_preferred,
                uses_router=(
                    pipeline is not None
                    and router_id is not None
                    and pipeline.conversation_engine == router_id
                ),
                prefer_local=pipeline.prefer_local_intents if pipeline is not None else False,
            )
        )
    return words


def _is_tracked(hass: HomeAssistant, sensors: list[str | None]) -> bool:
    """Whether any automation references one of the sensors."""
    if "automation" not in hass.config.components:
        return False
    from homeassistant.components.automation import automations_with_entity

    return any(sensor and automations_with_entity(hass, sensor) for sensor in sensors)


def _satellite_name(devices: dr.DeviceRegistry, device_id: str, entity_id: str) -> str:
    device = devices.async_get(device_id)
    if device is None:
        return entity_id
    return device.name_by_user or device.name or entity_id


def find_satellites(hass: HomeAssistant, entry: ConfigEntry) -> list[Satellite]:
    """Every assist satellite on a device, with its speaker sensors and wake words.

    Sorted by name. Wake words are empty while assist_pipeline is not set up.
    """
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    router_id = router_entity_id(hass, entry)
    satellites: list[Satellite] = []
    for entity in entities.entities.values():
        if entity.domain != "assist_satellite" or entity.device_id is None:
            continue
        device_id = entity.device_id
        on_device = er.async_entries_for_device(entities, device_id)
        sensors = [item.entity_id for item in on_device if item.domain == "sensor"]
        score = next((s for s in sensors if s.endswith("_last_speaker_score")), None)
        speaker = next((s for s in sensors if s.endswith("_last_speaker")), None)
        helper = f"input_text.voice_speaker_{device_id}"
        satellites.append(
            Satellite(
                entity_id=entity.entity_id,
                device_id=device_id,
                name=_satellite_name(devices, device_id, entity.entity_id),
                helper=helper,
                helper_exists=hass.states.get(helper) is not None,
                speaker_sensor=speaker,
                score_sensor=score,
                tracked=_is_tracked(hass, [speaker, score]),
                wake_words=_wake_words(hass, on_device, router_id),
            )
        )
    return sorted(satellites, key=lambda satellite: satellite["name"])


def helper_payload(device_id: str) -> dict[str, Any]:
    """Fields for ``input_text/create``; Home Assistant derives the entity id from the name."""
    return {"name": f"voice speaker {device_id}", "min": 0, "max": 20, "mode": "text"}


def _template_literal(text: str) -> str:
    """A Jinja string literal; JSON escaping is a valid subset of it."""
    return json.dumps(text)


def tracking_automation(
    satellite: Satellite, names: Mapping[str, str]
) -> tuple[str, dict[str, Any]]:
    """The automation id and config that keep this satellite's speaker helper up to date.

    Mirrors examples/speaker_tracking_automation.yaml for one satellite. Raises ValueError
    when the satellite has no speaker sensor or no score sensor (the score changes on every
    turn, the speaker name does not when the same person speaks twice), a name is empty, or
    both names are the same.
    """
    speaker_sensor = satellite["speaker_sensor"]
    score_sensor = satellite["score_sensor"]
    if speaker_sensor is None:
        raise ValueError(f"{satellite['name']} has no speaker sensor to track")
    if score_sensor is None:
        raise ValueError(f"{satellite['name']} has no score sensor to trigger on")
    owner = names.get("owner", "").strip().lower()
    partner = names.get("partner", "").strip().lower()
    if not owner or not partner:
        raise ValueError("both person names are needed to tell the speakers apart")
    if owner == partner:
        # The template maps names to roles: one name twice keeps only the partner role.
        raise ValueError("the two people need different names in the voice match")

    roles = f"{{{_template_literal(owner)}: 'owner', {_template_literal(partner)}: 'partner'}}"
    helper = {"entity_id": satellite["helper"]}
    config: dict[str, Any] = {
        "alias": f"Voice: track who is speaking on {satellite['name']}",
        "description": f"Sets {satellite['helper']} to owner, partner, stranger or pending.",
        "mode": "queued",
        "max": 10,
        "triggers": [
            {
                "trigger": "state",
                "id": "wake",
                "entity_id": [satellite["entity_id"]],
                "to": "listening",
            },
            {
                "trigger": "state",
                "id": "score",
                "entity_id": [score_sensor],
                "not_to": ["unavailable", "unknown"],
            },
        ],
        "actions": [
            {
                "choose": [
                    {
                        "conditions": [
                            {"condition": "trigger", "id": "wake"},
                            # A listen starting within 2 seconds of the reply ending is a
                            # follow-up with no new voice match: the last speaker carries on.
                            {
                                "condition": "template",
                                "value_template": (
                                    "{{ not (trigger.from_state.state == 'idle' and "
                                    "(trigger.to_state.last_changed - "
                                    "trigger.from_state.last_changed).total_seconds() < 2) }}"
                                ),
                            },
                        ],
                        "sequence": [_set_helper(helper, "pending")],
                    },
                    {
                        "conditions": [{"condition": "trigger", "id": "score"}],
                        "sequence": [
                            {
                                "variables": {
                                    "role": (
                                        f"{{{{ {roles}.get("
                                        f"states({_template_literal(speaker_sensor)}) "
                                        "| trim | lower, 'stranger') }}"
                                    )
                                }
                            },
                            _set_helper(helper, "{{ role }}"),
                        ],
                    },
                ]
            }
        ],
    }
    return f"voice_assistant_manager_speaker_{satellite['device_id']}", config


def _set_helper(target: dict[str, str], value: str) -> dict[str, Any]:
    return {"action": "input_text.set_value", "target": target, "data": {"value": value}}


def reusable_pipeline(hass: HomeAssistant, title: str, router: str) -> str | None:
    """The id of a pipeline named ``title`` that already uses this router, if any.

    None while another pipeline shares the name: a wake word could not be pointed at it
    (see ``pipeline_name_is_shared``), so the guide makes a copy with a free name instead.
    """
    from homeassistant.components.assist_pipeline.pipeline import async_get_pipelines

    if pipeline_name_is_shared(hass, title):
        return None
    return next(
        (
            pipeline.id
            for pipeline in async_get_pipelines(hass)
            if pipeline.name == title and pipeline.conversation_engine == router
        ),
        None,
    )


def router_pipeline(hass: HomeAssistant, pipeline_id: str, router: str) -> Pipeline | None:
    """The pipeline with this id, only when it uses this router; None for any other.

    Every change the guide makes to a pipeline goes through this check, so a pipeline of
    another agent is never touched. None as well while Assist is not set up. Looked up by
    id: names are not unique, so a name can stand for a pipeline of another agent.
    """
    if "assist_pipeline" not in hass.config.components:
        return None
    from homeassistant.components.assist_pipeline.pipeline import async_get_pipelines

    return next(
        (
            pipeline
            for pipeline in async_get_pipelines(hass)
            if pipeline.id == pipeline_id and pipeline.conversation_engine == router
        ),
        None,
    )


def pipeline_name_is_shared(hass: HomeAssistant, name: str) -> bool:
    """Whether more than one pipeline has this name (a select picks its pipeline by name)."""
    from homeassistant.components.assist_pipeline.pipeline import async_get_pipelines

    return sum(pipeline.name == name for pipeline in async_get_pipelines(hass)) > 1


def free_pipeline_name(hass: HomeAssistant, title: str) -> str:
    """``title``, or ``title 2``, ``title 3``, ... for the first name no pipeline uses."""
    taken = set(_pipelines_by_name(hass))
    candidate = title
    number = 2
    while candidate in taken:
        candidate = f"{title} {number}"
        number += 1
    return candidate


def pipeline_copy_payload(
    hass: HomeAssistant, source_id: str, router: str, title: str
) -> dict[str, Any]:
    """Fields for ``assist_pipeline/pipeline/create``: the source pipeline, on this router.

    Every field of the source except its id is kept, so STT, TTS, language and wake word
    stay as they were. Local intents are off: the router has its own built-in commands.
    """
    from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline

    source = async_get_pipeline(hass, source_id)
    payload = {
        key: value for key, value in dataclasses.asdict(source).items() if key != "id"
    }
    payload["conversation_engine"] = router
    payload["prefer_local_intents"] = False
    payload["name"] = free_pipeline_name(hass, title)
    return payload


STYLES = ("butler", "friendly", "plain")
# Shown for a name left blank, as the settings do.
_BLANK_NAMES = {"owner": "Person 1", "partner": "Person 2"}


def fill_style(
    strings: Mapping[str, str], style: str, owner: str, partner: str
) -> dict[str, Any]:
    """The personas and refusal texts of one ready-made style, with the names put in.

    Only the settings the guide fills are returned. The texts are the translations
    ``guide.styles.<style>.*``; ``{name}`` is the person the persona is for and
    ``{partner}`` the other one, so the partner's persona swaps the two names.
    """
    if style not in STYLES:
        raise ValueError(f"Unknown style: {style!r}")
    first = owner.strip() or _BLANK_NAMES["owner"]
    second = partner.strip() or _BLANK_NAMES["partner"]

    def text(key: str, name: str = first, other: str = second) -> str:
        template = strings[f"guide.styles.{style}.{key}"]
        return template.replace("{name}", name).replace("{partner}", other)

    return {
        "owner": {"persona": text("owner_persona")},
        "partner": {"persona": text("owner_persona", second, first)},
        "strangers": {
            "persona": text("strangers_persona"),
            "fixed_replies": text("fixed_replies"),
        },
        "near_miss": {"persona": text("near_miss_persona")},
    }
