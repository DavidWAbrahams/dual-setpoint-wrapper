# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""Tests for owned-state rules: gap validation, restore, availability."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TEMPERATURE,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.dual_setpoint.const import (
    CONF_MIN_CYCLE_TIME,
    CONF_MIN_GAP,
    CONF_SOURCE_ENTITY,
    DOMAIN,
)

from .conftest import ENTRY_ID, SOURCE_ENTITY


async def test_range_below_min_gap_is_rejected(
    hass: HomeAssistant, create_wrapper, wrapper_entity, wrapper_id
) -> None:
    """A range tighter than min_gap raises and changes nothing."""
    await create_wrapper(min_gap=2.0)
    entity = wrapper_entity()

    with pytest.raises(ServiceValidationError) as exc_info:
        await entity.async_set_temperature(
            **{ATTR_TARGET_TEMP_LOW: 20.5, ATTR_TARGET_TEMP_HIGH: 22.0}
        )
    assert exc_info.value.translation_key == "gap_too_small"
    await hass.async_block_till_done()

    # Bounds and source are untouched.
    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 21.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 23.0


async def test_range_exactly_min_gap_is_accepted(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity, wrapper_id
) -> None:
    """A range with a gap of exactly min_gap passes and commands the heat side."""
    thermostat.step = 0.5  # Keep the tolerance from swallowing the 1.0 move.
    thermostat.external_set(current=15.0)
    await create_wrapper(min_gap=2.0)
    entity = wrapper_entity()

    await entity.async_set_temperature(
        **{ATTR_TARGET_TEMP_LOW: 20.0, ATTR_TARGET_TEMP_HIGH: 22.0}
    )
    await hass.async_block_till_done()

    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 20.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 22.0
    assert len(thermostat.set_temperature_calls) == 1


async def test_restore_heat_cool_normalizes_gap_and_keeps_flip_clock(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_id
) -> None:
    """Restored bounds below min_gap are widened; the flip clock survives."""
    flip = dt_util.utcnow()
    last_state = State(
        "climate.thermostat_2",
        HVACMode.HEAT_COOL,
        {
            ATTR_TARGET_TEMP_LOW: 20.0,
            ATTR_TARGET_TEMP_HIGH: 21.0,
            "last_flip": flip.isoformat(),
        },
    )
    thermostat.current_temperature = 15.0
    await create_wrapper(min_gap=2.0, last_state=last_state)

    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.state == HVACMode.HEAT_COOL
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 20.0
    # The 1.0 gap was widened back out to the configured 2.0.
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 22.0
    assert state.attributes["last_flip"] == flip.isoformat()
    # Cold room, heat already wanted; the source heats a full step away from
    # the restored low bound, so we re-assert the restored setpoint.
    assert thermostat.set_temperature_calls == [
        {
            ATTR_ENTITY_ID: SOURCE_ENTITY,
            ATTR_TEMPERATURE: 20.0,
            ATTR_HVAC_MODE: HVACMode.HEAT,
        }
    ]


async def test_restore_heat_cool_recommands_hot_source(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity, wrapper_id
) -> None:
    """Restored heat_cool takes over the source right away (no cycle gate)."""
    last_state = State(
        "climate.thermostat_2",
        HVACMode.HEAT_COOL,
        {ATTR_TARGET_TEMP_LOW: 20.0, ATTR_TARGET_TEMP_HIGH: 21.0},
    )
    thermostat.current_temperature = 25.0
    await create_wrapper(min_gap=2.0, last_state=last_state)
    await hass.async_block_till_done()

    # Above the midpoint: flip straight to cooling at the high bound.
    assert thermostat.set_temperature_calls == [
        {
            "entity_id": SOURCE_ENTITY,
            "temperature": 22.0,
            "hvac_mode": HVACMode.COOL,
        }
    ]
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.COOL
    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.state == HVACMode.HEAT_COOL
    assert wrapper_entity()._target_temp_high == 22.0


async def test_unavailable_source_hides_wrapper_and_blocks_commands(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity, wrapper_id
) -> None:
    """With the source gone the wrapper is unavailable and stays silent."""
    await create_wrapper()
    entity = wrapper_entity()

    thermostat.set_online(False)
    await hass.async_block_till_done()

    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.state == STATE_UNAVAILABLE

    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 24.0})
    await hass.async_block_till_done()
    assert thermostat.set_temperature_calls == []

    # The source coming back is mirrored like any other out-of-band state.
    thermostat.set_online(True)
    await hass.async_block_till_done()
    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.state == HVACMode.HEAT
    assert thermostat.set_temperature_calls == []


async def test_missing_source_retries_setup(hass: HomeAssistant, thermostat) -> None:
    """An entry whose source is not loaded yet retries and then loads."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Thermostat",
        entry_id=ENTRY_ID,
        data={CONF_SOURCE_ENTITY: SOURCE_ENTITY},
        options={CONF_MIN_GAP: 2.0, CONF_MIN_CYCLE_TIME: 900},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY

    # Once the source appears, HA's retry timer re-runs setup and it loads.
    # The retry setup runs as a background task, so it needs the
    # wait_background_tasks flag to be awaited here.
    thermostat.seed()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.LOADED
