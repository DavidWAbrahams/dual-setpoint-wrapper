# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""Behavior tests for the Dual Setpoint Climate wrapper."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    ATTR_TEMPERATURE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import SOURCE_ENTITY


async def test_setup_mirrors_source_without_commands(
    hass: HomeAssistant, create_wrapper, wrapper_id
) -> None:
    """Setup adopts the source's mode and setpoint without commanding it."""
    await create_wrapper()

    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_TEMPERATURE] == 21.0
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 21.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 23.0
    assert state.attributes["current_temperature"] == 20.0
    # The source's own modes survive, with heat_cool added on the end.
    assert state.attributes["hvac_modes"] == [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.HEAT_COOL,
    ]
    # Both single and range setpoints are advertised.
    features = state.attributes[ATTR_SUPPORTED_FEATURES]
    assert features & ClimateEntityFeature.TARGET_TEMPERATURE
    assert features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    assert state.attributes["source_entity"] == SOURCE_ENTITY
    assert state.attributes["last_flip"] is None

    # The source was not touched.
    source = hass.states.get(SOURCE_ENTITY)
    assert source is not None
    assert source.state == HVACMode.HEAT
    assert source.attributes[ATTR_TEMPERATURE] == 21.0


async def test_mirrored_surface_tracks_source_attributes(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_id
) -> None:
    """Limits, step, action and fan settings come straight from the source."""
    await create_wrapper()

    hass.states.async_set(
        SOURCE_ENTITY,
        HVACMode.HEAT,
        {
            **thermostat.attributes,
            # The fan settings only surface when the source supports them.
            ATTR_SUPPORTED_FEATURES: int(ClimateEntityFeature.FAN_MODE),
            ATTR_HVAC_ACTION: HVACAction.HEATING,
            ATTR_FAN_MODE: "low",
            ATTR_FAN_MODES: ["auto", "low"],
        },
    )
    await hass.async_block_till_done()

    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.attributes["min_temp"] == 7.0
    assert state.attributes["max_temp"] == 35.0
    assert state.attributes[ATTR_TARGET_TEMP_STEP] == 1.0
    assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING
    assert state.attributes[ATTR_FAN_MODE] == "low"
    assert state.attributes["fan_modes"] == ["auto", "low"]

    thermostat.external_set(current=18.5)
    await hass.async_block_till_done()
    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.attributes["current_temperature"] == 18.5


async def test_availability_follows_source(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_id
) -> None:
    """The wrapper goes unavailable with its source."""
    await create_wrapper()

    thermostat.set_online(False)
    await hass.async_block_till_done()
    assert hass.states.get(wrapper_id()).state == "unavailable"

    thermostat.set_online(True)
    await hass.async_block_till_done()
    assert hass.states.get(wrapper_id()).state == HVACMode.HEAT


async def test_enter_heat_cool_cold_room_stays_on_heat(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity
) -> None:
    """Entering heat_cool below the midpoint keeps heating, commanding nothing."""
    thermostat.current_temperature = 15.0
    await create_wrapper()
    entity = wrapper_entity()

    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()

    assert thermostat.set_temperature_calls == []
    state = hass.states.get(entity.entity_id)
    assert state is not None
    assert state.state == HVACMode.HEAT_COOL
    # No single target while in heat_cool; the bounds carry the setpoints.
    assert state.attributes.get(ATTR_TEMPERATURE) is None
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 21.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 23.0
    # Entering stamps the flip clock so the next flip is rate limited.
    assert state.attributes["last_flip"] is not None


async def test_enter_heat_cool_hot_room_flips_heat_source_to_cool(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity
) -> None:
    """Entering heat_cool from heat in a hot room drives the source to cool."""
    thermostat.current_temperature = 25.0
    await create_wrapper()
    entity = wrapper_entity()

    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()

    assert thermostat.set_temperature_calls == [
        {
            ATTR_ENTITY_ID: SOURCE_ENTITY,
            ATTR_TEMPERATURE: 23.0,
            ATTR_HVAC_MODE: HVACMode.COOL,
        }
    ]
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.COOL
    # The wrapper itself stays heat_cool.
    assert hass.states.get(entity.entity_id).state == HVACMode.HEAT_COOL


async def test_enter_heat_cool_flips_cool_source_to_heat(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity
) -> None:
    """Entering heat_cool from cool in a cold room drives the source to heat."""
    thermostat.mode = HVACMode.COOL
    thermostat.current_temperature = 15.0
    await create_wrapper()
    entity = wrapper_entity()

    # Mirrored from COOL 21 with a gap of 2: bounds sit at 19/21.
    state = hass.states.get(entity.entity_id)
    assert state is not None
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 19.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 21.0

    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()

    # 15 is below the midpoint of 20, so heating at the low bound wins.
    assert thermostat.set_temperature_calls == [
        {
            ATTR_ENTITY_ID: SOURCE_ENTITY,
            ATTR_TEMPERATURE: 19.0,
            ATTR_HVAC_MODE: HVACMode.HEAT,
        }
    ]
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.HEAT


async def test_midpoint_flip_crossings(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity
) -> None:
    """With no cycle time, each midpoint crossing flips the source."""
    thermostat.step = 0.5
    thermostat.current_temperature = 15.0
    await create_wrapper(min_cycle=0)
    entity = wrapper_entity()
    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()
    assert thermostat.set_temperature_calls == []

    # Warmed past the midpoint of 21/23: cooling at the high bound.
    thermostat.external_set(current=25.0)
    await hass.async_block_till_done()
    assert thermostat.set_temperature_calls[-1] == {
        ATTR_ENTITY_ID: SOURCE_ENTITY,
        ATTR_TEMPERATURE: 23.0,
        ATTR_HVAC_MODE: HVACMode.COOL,
    }
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.COOL

    # Cooled back under the midpoint: heating at the low bound.
    thermostat.external_set(current=15.0)
    await hass.async_block_till_done()
    assert thermostat.set_temperature_calls[-1] == {
        ATTR_ENTITY_ID: SOURCE_ENTITY,
        ATTR_TEMPERATURE: 21.0,
        ATTR_HVAC_MODE: HVACMode.HEAT,
    }
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.HEAT
    assert hass.states.get(entity.entity_id).state == HVACMode.HEAT_COOL


async def test_flip_blocked_by_cycle_time_then_fires(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity
) -> None:
    """A flip inside the cycle time waits, then happens on its own."""
    # Shift the flip clock 15 minutes into the past so the blocked retry's
    # re-evaluation (which reads the real wall clock) sees the cycle time as
    # elapsed once the deferred timer fires.
    past_flip = dt_util.utcnow() - timedelta(minutes=15)
    thermostat.current_temperature = 15.0
    await create_wrapper(min_cycle=900)
    entity = wrapper_entity()
    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()

    thermostat.external_set(current=25.0)
    await hass.async_block_till_done()

    # Nothing was commanded, and a retry is armed.
    assert thermostat.set_temperature_calls == []
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.HEAT
    assert entity._defer_unsub is not None

    entity._last_flip = past_flip

    # Once the cycle time elapses the deferred retry completes the flip.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=901))
    await hass.async_block_till_done()

    assert thermostat.set_temperature_calls == [
        {
            ATTR_ENTITY_ID: SOURCE_ENTITY,
            ATTR_TEMPERATURE: 23.0,
            ATTR_HVAC_MODE: HVACMode.COOL,
        }
    ]
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.COOL
    assert entity._defer_unsub is None


async def test_single_setpoint_edit_in_heat(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity
) -> None:
    """A single setpoint while heating commands the source and moves low."""
    thermostat.step = 0.5
    await create_wrapper()
    entity = wrapper_entity()

    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})
    await hass.async_block_till_done()

    assert thermostat.set_temperature_calls == [
        {
            ATTR_ENTITY_ID: SOURCE_ENTITY,
            ATTR_TEMPERATURE: 23.0,
            ATTR_HVAC_MODE: HVACMode.HEAT,
        }
    ]
    state = hass.states.get(entity.entity_id)
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == 23.0
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 23.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 25.0


async def test_range_edit_in_heat_cool_moves_active_side(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity
) -> None:
    """Editing the range while heat_cooling pushes the active side only."""
    thermostat.step = 0.5
    thermostat.current_temperature = 15.0
    await create_wrapper(min_cycle=0)
    entity = wrapper_entity()
    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()
    assert thermostat.set_temperature_calls == []

    # The room heats toward the low bound, so only the low side is sent.
    await entity.async_set_temperature(
        **{ATTR_TARGET_TEMP_LOW: 20.0, ATTR_TARGET_TEMP_HIGH: 22.0}
    )
    await hass.async_block_till_done()
    assert thermostat.set_temperature_calls == [
        {
            ATTR_ENTITY_ID: SOURCE_ENTITY,
            ATTR_TEMPERATURE: 20.0,
            ATTR_HVAC_MODE: HVACMode.HEAT,
        }
    ]

    # A single setpoint while heat_cooling edits the active (low) side too.
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 19.0})
    await hass.async_block_till_done()
    assert thermostat.set_temperature_calls[-1] == {
        ATTR_ENTITY_ID: SOURCE_ENTITY,
        ATTR_TEMPERATURE: 19.0,
        ATTR_HVAC_MODE: HVACMode.HEAT,
    }
    state = hass.states.get(entity.entity_id)
    assert state is not None
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 19.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 22.0
    assert hass.states.get(entity.entity_id).state == HVACMode.HEAT_COOL


async def test_single_setpoint_in_heat_cool_follows_cooling_side(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity
) -> None:
    """While the source is cooling, a single setpoint edits the high bound."""
    thermostat.step = 0.5
    thermostat.current_temperature = 25.0
    await create_wrapper(min_cycle=0)
    entity = wrapper_entity()
    # Source is heating at 21, bounds 21/23; entering with 25 flips it to cool.
    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.COOL

    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 24.0})
    await hass.async_block_till_done()

    assert thermostat.set_temperature_calls[-1] == {
        ATTR_ENTITY_ID: SOURCE_ENTITY,
        ATTR_TEMPERATURE: 24.0,
        ATTR_HVAC_MODE: HVACMode.COOL,
    }
    state = hass.states.get(entity.entity_id)
    assert state is not None
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 21.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 24.0


async def test_leave_heat_cool_to_off(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity
) -> None:
    """Leaving heat_cool for off uses set_hvac_mode, never bare set_temperature."""
    await create_wrapper()
    entity = wrapper_entity()
    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()

    await entity.async_set_hvac_mode(HVACMode.OFF)
    await hass.async_block_till_done()

    assert thermostat.set_hvac_mode_calls == [
        {ATTR_ENTITY_ID: SOURCE_ENTITY, ATTR_HVAC_MODE: HVACMode.OFF}
    ]
    assert all(ATTR_TEMPERATURE in call for call in thermostat.set_temperature_calls)
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.OFF
    assert hass.states.get(entity.entity_id).state == HVACMode.OFF


async def test_fan_mode_passes_through_to_source(
    hass: HomeAssistant,
    create_wrapper,
    wrapper_entity,
) -> None:
    """Passthrough services are forwarded to the source entity id."""
    await create_wrapper()
    entity = wrapper_entity()

    seen: list[dict[str, Any]] = []

    async def spy(call: ServiceCall) -> None:
        seen.append(dict(call.data))

    hass.services.async_register("climate", "set_fan_mode", spy)

    await entity.async_set_fan_mode("low")
    await hass.async_block_till_done()

    assert seen == [{ATTR_ENTITY_ID: SOURCE_ENTITY, ATTR_FAN_MODE: "low"}]
