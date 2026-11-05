# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""Tests for source changes made outside of the wrapper."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import SOURCE_ENTITY


async def test_remote_turns_source_off_during_heat_cool(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity, wrapper_id
) -> None:
    """A remote power-off is adopted; the wrapper does not fight back."""
    thermostat.current_temperature = 15.0
    await create_wrapper()
    entity = wrapper_entity()
    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()
    assert thermostat.set_temperature_calls == []

    thermostat.external_set(mode=HVACMode.OFF)
    await hass.async_block_till_done()

    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.state == HVACMode.OFF
    # Still nothing commanded; we follow the source, never contradict it.
    assert thermostat.set_temperature_calls == []
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.OFF


async def test_remote_switches_to_cool_during_heat_cool(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity, wrapper_id
) -> None:
    """A remote mode change exits heat_cool and re-anchors the bounds."""
    thermostat.current_temperature = 15.0
    await create_wrapper()
    entity = wrapper_entity()
    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()

    thermostat.external_set(mode=HVACMode.COOL)
    await hass.async_block_till_done()

    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.state == HVACMode.COOL
    # Cooling at 21.0 re-anchors the range below the setpoint.
    assert state.attributes[ATTR_TEMPERATURE] == 21.0
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 19.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 21.0
    assert thermostat.set_temperature_calls == []


async def test_small_source_drift_is_not_out_of_band(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_id
) -> None:
    """Drift within half a source step is ignored; a bigger jump is mirrored."""
    await create_wrapper()

    # Within the half-step echo tolerance: treated as an echo, not news.
    thermostat.external_set(temperature=21.5)
    await hass.async_block_till_done()

    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == 21.0
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 21.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 23.0
    assert thermostat.set_temperature_calls == []

    # Well past the tolerance: the source moved out of band, so we adopt it.
    thermostat.external_set(temperature=24.0)
    await hass.async_block_till_done()

    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == 24.0
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 24.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 26.0
    assert thermostat.set_temperature_calls == []


async def test_one_step_source_change_is_out_of_band(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_id
) -> None:
    """A deliberate one-step move on the source is news, not echo noise."""
    await create_wrapper()  # source heats at 21.0 with a 1.0 step

    thermostat.external_set(temperature=22.0)
    await hass.async_block_till_done()

    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == 22.0
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 22.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 24.0
    assert thermostat.set_temperature_calls == []


async def test_grid_rounded_echo_confirms_command(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity, wrapper_id
) -> None:
    """An echo landed within half a step of what we sent confirms it."""
    await create_wrapper()
    entity = wrapper_entity()

    thermostat.echo = False
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})
    await hass.async_block_till_done()
    assert entity._pending is True

    # A device quantizing setpoints to its own (e.g. Celsius) grid echoes a
    # hair below what we sent; that still reads as our command returning,
    # and the wrapper keeps showing the value the user picked.
    thermostat.external_set(temperature=22.55)
    await hass.async_block_till_done()

    assert entity._pending is False
    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == 23.0
    assert len(thermostat.set_temperature_calls) == 1


async def test_unconfirmed_command_mirrors_source_after_settle_window(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity, wrapper_id
) -> None:
    """A command the source never confirms gives up once the window closes."""
    await create_wrapper()
    entity = wrapper_entity()

    # The silent thermostat accepts the call but never echoes new state.
    thermostat.echo = False
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})
    await hass.async_block_till_done()

    assert thermostat.set_temperature_calls == [
        {
            ATTR_ENTITY_ID: SOURCE_ENTITY,
            ATTR_TEMPERATURE: 23.0,
            ATTR_HVAC_MODE: HVACMode.HEAT,
        }
    ]
    # Until the source confirms, we show what we commanded.
    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == 23.0

    # The settle window passes with no confirmation: the source stays real.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=91))
    await hass.async_block_till_done()

    assert entity._pending is False
    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.attributes[ATTR_TEMPERATURE] == 21.0
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 21.0
    # The failed edit still moved the remembered cooling bound up to keep the
    # gap; giving up on the command only re-reads the heating side.
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 25.0
    assert len(thermostat.set_temperature_calls) == 1
