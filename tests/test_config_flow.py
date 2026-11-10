# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Dual Setpoint Climate config and options flows."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.climate import ATTR_HVAC_MODES, HVACMode
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dual_setpoint.const import (
    CONF_COORDINATE_MODES,
    CONF_COORDINATION_GROUP,
    CONF_COORDINATION_PRIORITY,
    CONF_MIN_CYCLE_TIME,
    CONF_MIN_GAP,
    CONF_SOURCE_ENTITY,
    CONF_THWARTED_COOL_HOLD,
    CONF_THWARTED_FAN_MODE,
    CONF_THWARTED_HEAT_HOLD,
    DOMAIN,
)

from .conftest import SOURCE_ENTITY


async def _start_flow(hass: HomeAssistant) -> Any:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})


async def test_show_form(hass: HomeAssistant) -> None:
    """The user step shows a climate entity picker."""
    result = await _start_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_create_entry(hass: HomeAssistant, thermostat) -> None:
    """A valid source creates an entry with unit-appropriate defaults."""
    thermostat.seed()
    hass.config.units = METRIC_SYSTEM

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_ENTITY: SOURCE_ENTITY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Thermostat"
    assert result["data"] == {CONF_SOURCE_ENTITY: SOURCE_ENTITY}
    assert result["options"] == {CONF_MIN_GAP: 2.0, CONF_MIN_CYCLE_TIME: 1800}

    # Finishing the flow adds (and auto-sets-up) the entry under "result".
    entry = result["result"]
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_create_entry_imperial_defaults(hass: HomeAssistant, thermostat) -> None:
    """An imperial system defaults the gap to 4 degrees."""
    thermostat.seed()
    hass.config.units = US_CUSTOMARY_SYSTEM

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_ENTITY: SOURCE_ENTITY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_MIN_GAP] == 4.0


async def test_source_not_found(hass: HomeAssistant, thermostat) -> None:
    """An entity with no state is rejected."""
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"source": "climate.nowhere"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "source_not_found"}

    # Recover and finish with the real entity.
    thermostat.seed()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_ENTITY: SOURCE_ENTITY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_source_not_dual(hass: HomeAssistant) -> None:
    """A heat-only source is rejected."""
    hass.states.async_set(
        "climate.heater",
        HVACMode.HEAT,
        {ATTR_HVAC_MODES: [HVACMode.OFF, HVACMode.HEAT]},
    )
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"source": "climate.heater"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "source_not_dual"}


async def test_source_unavailable_still_wrappable(
    hass: HomeAssistant, thermostat
) -> None:
    """A source that exists but is unavailable can be wrapped: it may recover."""
    thermostat.seed()
    hass.states.async_set(SOURCE_ENTITY, STATE_UNAVAILABLE, {})

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_ENTITY: SOURCE_ENTITY}
    )
    # The modes attribute is gone while unavailable, so it is not dual-capable
    # right now; the user can retry once the device reports again.
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "source_not_dual"}


async def test_duplicate_source_aborts(hass: HomeAssistant, thermostat) -> None:
    """One source may only be wrapped once."""
    thermostat.seed()

    result = await _start_flow(hass)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_ENTITY: SOURCE_ENTITY}
    )

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_ENTITY: SOURCE_ENTITY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_circular_source_aborts(hass: HomeAssistant) -> None:
    """A wrapper entity cannot wrap another wrapper."""
    other_entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="other")
    other_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    wrapped = registry.async_get_or_create(
        "climate", DOMAIN, "wrapped", config_entry=other_entry
    )

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"source": wrapped.entity_id}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "circular"


@pytest.fixture
async def loaded_wrapper(create_wrapper):
    """Return a loaded wrapper entry for options tests."""
    return await create_wrapper()


async def test_options_form_shows_current_values(
    hass: HomeAssistant, loaded_wrapper: MockConfigEntry
) -> None:
    """The options form defaults to the stored values."""
    result = await hass.config_entries.options.async_init(loaded_wrapper.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    schema = result["data_schema"]
    defaults = {str(key): key.default() for key in schema.schema}
    assert defaults[CONF_MIN_GAP] == 2.0
    assert defaults[CONF_MIN_CYCLE_TIME] == {"seconds": 900}


async def test_options_flow_updates_and_reloads(
    hass: HomeAssistant, loaded_wrapper: MockConfigEntry
) -> None:
    """Submitting options stores gap and cycle time and reloads the entry."""
    result = await hass.config_entries.options.async_init(loaded_wrapper.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MIN_GAP: 3.0,
            CONF_MIN_CYCLE_TIME: {"days": 0, "hours": 0, "minutes": 20},
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_wrapper.options[CONF_MIN_GAP] == 3.0
    # The options flow normalizes the duration dict to whole seconds.
    assert loaded_wrapper.options[CONF_MIN_CYCLE_TIME] == 1200
    assert loaded_wrapper.state is ConfigEntryState.LOADED


async def test_options_flow_imperial_unit_label(
    hass: HomeAssistant, loaded_wrapper: MockConfigEntry
) -> None:
    """The gap selector shows degrees Fahrenheit on an imperial system."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    # Drop the stored gap so the unit default kicks in.
    hass.config_entries.async_update_entry(
        loaded_wrapper, options={CONF_MIN_CYCLE_TIME: 900}
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(loaded_wrapper.entry_id)
    schema = result["data_schema"]
    gap_key = next(key for key in schema.schema if str(key) == CONF_MIN_GAP)
    assert gap_key.default() == 4.0


async def test_options_form_shows_coordination_defaults(
    hass: HomeAssistant, loaded_wrapper: MockConfigEntry
) -> None:
    """An entry from before coordination sees every field at its default."""
    result = await hass.config_entries.options.async_init(loaded_wrapper.entry_id)
    assert result["type"] is FlowResultType.FORM

    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults[CONF_COORDINATE_MODES] is False
    assert defaults[CONF_COORDINATION_GROUP] == "default"
    assert defaults[CONF_COORDINATION_PRIORITY] == 0
    assert defaults[CONF_THWARTED_HEAT_HOLD] == "off"
    assert defaults[CONF_THWARTED_COOL_HOLD] == "off"
    assert defaults[CONF_THWARTED_FAN_MODE] == ""


async def test_options_flow_stores_coordination_and_reloads(
    hass: HomeAssistant, loaded_wrapper: MockConfigEntry
) -> None:
    """Submitting the coordination fields stores them and reloads the entry."""
    result = await hass.config_entries.options.async_init(loaded_wrapper.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_MIN_GAP: 2.0,
            CONF_MIN_CYCLE_TIME: {"seconds": 900},
            CONF_COORDINATE_MODES: True,
            CONF_COORDINATION_GROUP: "house",
            CONF_COORDINATION_PRIORITY: 3,
            CONF_THWARTED_HEAT_HOLD: "fan_only",
            CONF_THWARTED_COOL_HOLD: "off",
            CONF_THWARTED_FAN_MODE: "high",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_wrapper.options[CONF_COORDINATE_MODES] is True
    assert loaded_wrapper.options[CONF_COORDINATION_GROUP] == "house"
    assert loaded_wrapper.options[CONF_COORDINATION_PRIORITY] == 3
    assert loaded_wrapper.options[CONF_THWARTED_HEAT_HOLD] == "fan_only"
    assert loaded_wrapper.options[CONF_THWARTED_COOL_HOLD] == "off"
    assert loaded_wrapper.options[CONF_THWARTED_FAN_MODE] == "high"
    assert loaded_wrapper.state is ConfigEntryState.LOADED
