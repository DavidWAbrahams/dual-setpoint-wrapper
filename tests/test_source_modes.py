# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""
Tests for sources whose own heat_cool mode changes the contract.

A source exposing heat_cool is one of two very different devices:

* an "auto" mode that balances around a single setpoint (an ESPHome
  Mitsubishi CN105, for example). It cannot hold the pair the wrapper
  owns, so it must be kicked back to heat or cool.
* a native dual-setpoint heat_cool (TARGET_TEMPERATURE_RANGE). There the
  pair is mirrored between wrapper and source instead of emulated.
"""

from __future__ import annotations

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from .conftest import SOURCE_ENTITY

# ---------------------------------------------------------------------------
# Real case: heat_cool mode with a single setpoint (incompatible "auto")
# ---------------------------------------------------------------------------


async def test_setup_kicks_incompatible_auto_source_to_heat(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_id
) -> None:
    """A CN105-style source found sitting in auto is forced back on setup."""
    thermostat.expose_heat_cool = True
    thermostat.mode = HVACMode.HEAT_COOL
    await create_wrapper()

    # The kick: single mode for the room's side, commanded with our bound.
    assert thermostat.set_temperature_calls == [
        {
            ATTR_ENTITY_ID: SOURCE_ENTITY,
            ATTR_HVAC_MODE: HVACMode.HEAT,
            ATTR_TEMPERATURE: 20.0,
        }
    ]
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.HEAT

    # The wrapper itself stays heat_cool, holding the pair the source cannot.
    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.state == HVACMode.HEAT_COOL
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 20.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 22.0


async def test_auto_mode_during_heat_cool_is_kicked_back(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity, wrapper_id
) -> None:
    """
    A remote pressing AUTO on an incompatible source is undone at once.

    Entering heat_cool armed the flip clock, so the pre-fix code judged the
    source's heat_cool a gated flip, found no side to hold it on, and gave
    up. Leaving a single-setpoint auto is not a compressor flip and must
    never wait out the cycle time.
    """
    thermostat.expose_heat_cool = True
    await create_wrapper()
    entity = wrapper_entity()

    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()
    # Cold room on heat: entry commanded nothing and armed the flip clock.
    assert thermostat.set_temperature_calls == []
    assert entity._last_flip is not None

    thermostat.external_set(mode=HVACMode.HEAT_COOL)
    await hass.async_block_till_done()

    # Kicked straight back to heat at the low bound, with no timer fired.
    assert thermostat.set_temperature_calls == [
        {
            ATTR_ENTITY_ID: SOURCE_ENTITY,
            ATTR_HVAC_MODE: HVACMode.HEAT,
            ATTR_TEMPERATURE: 21.0,
        }
    ]
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.HEAT

    # The wrapper keeps the heat_cool role and its bounds.
    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.state == HVACMode.HEAT_COOL
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 21.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 23.0


async def test_auto_source_kicked_to_cool_in_hot_room(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity
) -> None:
    """The kick picks the side the room needs: warm means cool at the high."""
    thermostat.expose_heat_cool = True
    thermostat.current_temperature = 30.0
    await create_wrapper()
    entity = wrapper_entity()
    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.COOL
    thermostat.set_temperature_calls.clear()

    thermostat.external_set(mode=HVACMode.HEAT_COOL)
    await hass.async_block_till_done()

    assert thermostat.set_temperature_calls == [
        {
            ATTR_ENTITY_ID: SOURCE_ENTITY,
            ATTR_HVAC_MODE: HVACMode.COOL,
            ATTR_TEMPERATURE: 23.0,
        }
    ]
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.COOL


# ---------------------------------------------------------------------------
# Edge case: native dual-setpoint heat_cool source (passthrough)
# ---------------------------------------------------------------------------


async def test_enter_heat_cool_delegates_to_range_source(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity, wrapper_id
) -> None:
    """Entering heat_cool on a range-capable source pushes both setpoints."""
    thermostat.native_range = True
    await create_wrapper()
    entity = wrapper_entity()
    # Source heats at 21, so the wrapper's bounds sit at 21/23.
    assert thermostat.set_temperature_calls == []

    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()

    # One range command, no single-side engine games.
    assert thermostat.set_temperature_calls == [
        {
            ATTR_ENTITY_ID: SOURCE_ENTITY,
            ATTR_HVAC_MODE: HVACMode.HEAT_COOL,
            ATTR_TARGET_TEMP_LOW: 21.0,
            ATTR_TARGET_TEMP_HIGH: 23.0,
        }
    ]
    assert hass.states.get(SOURCE_ENTITY).state == HVACMode.HEAT_COOL

    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.state == HVACMode.HEAT_COOL
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 21.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 23.0


async def test_wrapper_range_edit_mirrors_to_range_source(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity
) -> None:
    """A range edit on the wrapper rides straight through to the source."""
    thermostat.native_range = True
    await create_wrapper()
    entity = wrapper_entity()
    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()
    assert len(thermostat.set_temperature_calls) == 1

    await entity.async_set_temperature(
        **{ATTR_TARGET_TEMP_LOW: 18.0, ATTR_TARGET_TEMP_HIGH: 24.0}
    )
    await hass.async_block_till_done()

    source = hass.states.get(SOURCE_ENTITY)
    assert source is not None
    assert source.state == HVACMode.HEAT_COOL
    assert source.attributes[ATTR_TARGET_TEMP_LOW] == 18.0
    assert source.attributes[ATTR_TARGET_TEMP_HIGH] == 24.0
    assert thermostat.set_temperature_calls[-1][ATTR_TARGET_TEMP_LOW] == 18.0
    assert thermostat.set_temperature_calls[-1][ATTR_TARGET_TEMP_HIGH] == 24.0


async def test_range_source_external_edit_mirrors_to_wrapper(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity, wrapper_id
) -> None:
    """A remote changing one bound on the source updates the wrapper too."""
    thermostat.native_range = True
    await create_wrapper()
    entity = wrapper_entity()
    await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    await hass.async_block_till_done()

    thermostat.external_set(low=19.0)
    await hass.async_block_till_done()

    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.state == HVACMode.HEAT_COOL
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 19.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 23.0
    # The source already holds 19/23, so nothing is pushed back at it.
    assert len(thermostat.set_temperature_calls) == 1


async def test_setup_mirrors_matching_range_without_commands(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_id
) -> None:
    """A range source already at heat_cool is adopted, not fought."""
    thermostat.native_range = True
    thermostat.mode = HVACMode.HEAT_COOL
    thermostat.low_temp = 21.0
    thermostat.high_temp = 23.0
    await create_wrapper()

    assert thermostat.set_temperature_calls == []
    state = hass.states.get(wrapper_id())
    assert state is not None
    assert state.state == HVACMode.HEAT_COOL
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 21.0
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 23.0
