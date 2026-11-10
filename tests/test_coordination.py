# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""Tests for multi-zone mode coordination (idle-unit parking)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.dual_setpoint.const import (
    CONF_COORDINATE_MODES,
    CONF_COORDINATION_GROUP,
    CONF_COORDINATION_PRIORITY,
    CONF_MIN_CYCLE_TIME,
    CONF_MIN_GAP,
    CONF_SOURCE_ENTITY,
    CONF_THWARTED_COOL_HOLD,
    CONF_THWARTED_FAN_MODE,
    DOMAIN,
    EVENT_MODE_CONFLICT,
    Demand,
)

from .conftest import FakeThermostat

GROUP = "house"

ATTR_CURRENT = "current_temperature"

BEDROOM_SOURCE = "climate.bedroom"
OFFICE_SOURCE = "climate.office"


async def _make_zone(
    hass: HomeAssistant,
    *,
    name: str,
    source_id: str,
    options: dict[str, Any] | None = None,
    min_cycle: int = 0,
) -> tuple[MockConfigEntry, FakeThermostat]:
    """Set up one coordinated wrapper on its own fake source."""
    fake = FakeThermostat(hass, source_id, friendly_name=name)
    fake.seed()
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=name,
        entry_id=f"entry-{name}",
        data={CONF_SOURCE_ENTITY: source_id},
        options={
            CONF_MIN_GAP: 2.0,
            CONF_MIN_CYCLE_TIME: min_cycle,
            CONF_COORDINATE_MODES: True,
            CONF_COORDINATION_GROUP: GROUP,
            **(options or {}),
        },
    )
    entry.add_to_hass(hass)
    # Same ordering dance as the single-zone fixture: climate's real
    # dispatcher first, then (re-)register the routed fake handlers.
    assert await async_setup_component(hass, "climate", {})
    fake.register_service()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fake.register_service()
    return entry, fake


def _wrapper(hass: HomeAssistant, entry_id: str):
    """Resolve the live wrapper entity for one config entry."""
    entity_id = er.async_get(hass).async_get_entity_id("climate", DOMAIN, entry_id)
    assert entity_id is not None
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        if entity_id in platform.entities:
            return platform.entities[entity_id]
    raise AssertionError(f"wrapper for {entry_id} not found")


def _backdate_demand(entity, member_entity_id: str, minutes: int) -> None:
    """Age one member's queue position so its claim has matured."""
    coordinator = entity._coordinator
    assert coordinator is not None
    demand, _started = coordinator._demands[member_entity_id]
    coordinator._demands[member_entity_id] = (
        demand,
        dt_util.utcnow() - timedelta(minutes=minutes),
    )


def _backdate_group_change(entity, minutes: int) -> None:
    """Open the group's cycle-time gate without waiting in real time."""
    coordinator = entity._coordinator
    assert coordinator is not None
    if coordinator._last_change is not None:
        coordinator._last_change -= timedelta(minutes=minutes)


async def _arbitrate(hass: HomeAssistant, entity) -> None:
    """Wake the group so it re-runs its decision."""
    entity._async_notify_coordinator()
    await hass.async_block_till_done()


async def _make_pair(
    hass: HomeAssistant,
    *,
    options: dict[str, Any] | None = None,
    min_cycle: int = 0,
) -> dict[str, Any]:
    """Bedroom wants heat, office wants cool: the classic split house."""
    bedroom_entry, bedroom = await _make_zone(
        hass,
        name="Bedroom",
        source_id=BEDROOM_SOURCE,
        options=options,
        min_cycle=min_cycle,
    )
    office_entry, office = await _make_zone(
        hass,
        name="Office",
        source_id=OFFICE_SOURCE,
        options=options,
        min_cycle=min_cycle,
    )
    bedroom_entity = _wrapper(hass, bedroom_entry.entry_id)
    office_entity = _wrapper(hass, office_entry.entry_id)

    # Bedroom: a cold room in heat mode, demanding heat.
    bedroom.external_set(mode=HVACMode.HEAT, temperature=21.0, current=15.0)
    await hass.async_block_till_done()

    # Office: a hot room the user set to cool at 26. Cool first, then the
    # setpoint: while still in heat mode a 26 edit would land on the low
    # (heating) bound instead of becoming the cool setpoint.
    await office_entity.async_set_hvac_mode(HVACMode.COOL)
    await office_entity.async_set_temperature(**{ATTR_TEMPERATURE: 26.0})
    office.external_set(current=30.0)
    await hass.async_block_till_done()

    return {
        "bedroom_entry": bedroom_entry,
        "office_entry": office_entry,
        "bedroom": bedroom,
        "office": office,
        "bedroom_entity": bedroom_entity,
        "office_entity": office_entity,
    }


def _parked(hass: HomeAssistant, entity) -> str | None:
    """Read the face's advertised park state."""
    state = hass.states.get(entity.entity_id)
    assert state is not None
    return state.attributes.get("parked")


async def test_coordination_disabled_keeps_current_behavior(
    hass: HomeAssistant, create_wrapper, wrapper_entity
) -> None:
    """With the switch off there are no coordination attributes at all."""
    await create_wrapper()
    entity = wrapper_entity()
    state = hass.states.get(entity.entity_id)
    assert state is not None
    for attr in ("demand", "group", "group_mode", "deferred_to", "parked"):
        assert attr not in state.attributes
    assert entity._coordinator is None


async def test_demand_attribute_follows_the_room(
    hass: HomeAssistant, thermostat, create_wrapper, wrapper_entity
) -> None:
    """The face publishes what its room needs, independent of its mode."""
    await create_wrapper(options={CONF_COORDINATE_MODES: True})
    entity = wrapper_entity()

    state = hass.states.get(entity.entity_id)
    assert state.attributes["demand"] == "heat"  # 20.0 room below the 21.0 heat bound
    assert state.attributes["group"] == "default"
    assert state.attributes["group_mode"] is None
    assert state.attributes["parked"] is None

    # A satisfied room stops demanding without changing its mode.
    thermostat.external_set(current=25.0)
    await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).attributes["demand"] == "idle"


async def test_group_forms_and_parks_the_losing_side(
    hass: HomeAssistant,
) -> None:
    """The mature claimant runs; the opposed idle unit's source parks off."""
    pair = await _make_pair(hass)
    bedroom, office = pair["bedroom"], pair["office"]
    bedroom_entity, office_entity = pair["bedroom_entity"], pair["office_entity"]

    # Age both claims, bedroom first: its claim is the elder.
    _backdate_demand(bedroom_entity, bedroom_entity.entity_id, 10)
    _backdate_demand(bedroom_entity, office_entity.entity_id, 5)
    await _arbitrate(hass, bedroom_entity)

    # The group runs heat; the office face says so and parks the valve.
    office_state = hass.states.get(office_entity.entity_id)
    assert office_state.attributes["group_mode"] == "heat"
    assert office_state.attributes["deferred_to"] == bedroom_entity.entity_id
    assert office_state.attributes["parked"] == "off"
    assert office_state.state == HVACMode.COOL
    assert office_state.attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE

    # The park only ever touches the source, as a mode-only command.
    assert office.set_hvac_mode_calls[-1] == {
        ATTR_ENTITY_ID: OFFICE_SOURCE,
        ATTR_HVAC_MODE: HVACMode.OFF,
    }
    assert hass.states.get(OFFICE_SOURCE).state == HVACMode.OFF
    # The winner is never commanded by the coordinator.
    assert bedroom.set_hvac_mode_calls == []
    assert hass.states.get(BEDROOM_SOURCE).state == HVACMode.HEAT


async def test_park_honors_fan_only_hold_and_fan_speed(
    hass: HomeAssistant,
) -> None:
    """A fan_only hold config parks the source blowing at the chosen speed."""
    pair = await _make_pair(
        hass,
        options={
            CONF_THWARTED_COOL_HOLD: "fan_only",
            CONF_THWARTED_FAN_MODE: "high",
        },
    )
    office = pair["office"]
    office.fan_only = True
    office.fan_modes = ["low", "high"]
    office._push()
    await hass.async_block_till_done()

    bedroom_entity = pair["bedroom_entity"]
    office_entity = pair["office_entity"]
    _backdate_demand(bedroom_entity, bedroom_entity.entity_id, 10)
    await _arbitrate(hass, bedroom_entity)

    assert hass.states.get(OFFICE_SOURCE).state == HVACMode.FAN_ONLY
    assert office.set_fan_mode_calls[-1] == {
        ATTR_ENTITY_ID: OFFICE_SOURCE,
        ATTR_FAN_MODE: "high",
    }
    assert office.fan_mode == "high"
    # The face still shows the user's cool mode and setpoint.
    office_state = hass.states.get(office_entity.entity_id)
    assert office_state.state == HVACMode.COOL
    assert office_state.attributes[ATTR_TEMPERATURE] == 26.0
    assert office_state.attributes["parked"] == "fan_only"


async def test_fan_only_falls_back_to_off_when_source_lacks_it(
    hass: HomeAssistant,
) -> None:
    """Park config without the mode on the device degrades to off."""
    pair = await _make_pair(hass, options={CONF_THWARTED_COOL_HOLD: "fan_only"})
    bedroom_entity = pair["bedroom_entity"]
    office = pair["office"]
    office_entity = pair["office_entity"]

    _backdate_demand(bedroom_entity, bedroom_entity.entity_id, 10)
    await _arbitrate(hass, bedroom_entity)

    assert hass.states.get(OFFICE_SOURCE).state == HVACMode.OFF
    assert _parked(hass, office_entity) == "off"
    assert office.set_fan_mode_calls == []


async def test_group_flips_when_the_incumbent_stops_pressing(
    hass: HomeAssistant,
) -> None:
    """Role reversal: satisfied bedroom, released office, now bedroom parks."""
    pair = await _make_pair(hass)
    bedroom, office = pair["bedroom"], pair["office"]
    bedroom_entity, office_entity = pair["bedroom_entity"], pair["office_entity"]

    _backdate_demand(bedroom_entity, bedroom_entity.entity_id, 10)
    _backdate_demand(bedroom_entity, office_entity.entity_id, 5)
    await _arbitrate(hass, bedroom_entity)
    assert _parked(hass, office_entity) == "off"

    # The bedroom warms up and stops demanding heat.
    bedroom.external_set(current=22.0)
    await hass.async_block_till_done()

    # The office's claim is still queued from before; it gets its turn.
    assert hass.states.get(office_entity.entity_id).attributes["group_mode"] == "cool"

    # Office back online cooling at its remembered setpoint...
    office_state = hass.states.get(office_entity.entity_id)
    assert office_state.attributes["parked"] is None
    assert hass.states.get(OFFICE_SOURCE).state == HVACMode.COOL
    assert office.set_temperature_calls[-1] == {
        ATTR_ENTITY_ID: OFFICE_SOURCE,
        ATTR_TEMPERATURE: 26.0,
        ATTR_HVAC_MODE: HVACMode.COOL,
    }
    # ...and the idle heat-mode bedroom parks to free the valve.
    assert hass.states.get(BEDROOM_SOURCE).state == HVACMode.OFF
    assert hass.states.get(bedroom_entity.entity_id).attributes["parked"] == "off"
    assert hass.states.get(bedroom_entity.entity_id).state == HVACMode.HEAT


async def test_fairness_order_follows_the_older_claim(
    hass: HomeAssistant,
) -> None:
    """With equal priority the longer-waiting room wins the valve."""
    pair = await _make_pair(hass)
    bedroom_entity = pair["bedroom_entity"]
    office_entity = pair["office_entity"]

    # The office claimed cool first this time.
    _backdate_demand(bedroom_entity, office_entity.entity_id, 10)
    _backdate_demand(bedroom_entity, bedroom_entity.entity_id, 5)
    await _arbitrate(hass, bedroom_entity)

    assert hass.states.get(bedroom_entity.entity_id).attributes["group_mode"] == "cool"
    assert hass.states.get(BEDROOM_SOURCE).state == HVACMode.OFF
    assert _parked(hass, bedroom_entity) == "off"
    assert hass.states.get(office_entity.entity_id).attributes["parked"] is None


async def test_priority_breaks_the_tie(hass: HomeAssistant) -> None:
    """Equal claim ages, higher priority takes the valve."""
    bedroom_entry, bedroom = await _make_zone(
        hass, name="Bedroom", source_id=BEDROOM_SOURCE
    )
    office_entry, office = await _make_zone(
        hass,
        name="Office",
        source_id=OFFICE_SOURCE,
        options={CONF_COORDINATION_PRIORITY: 5},
    )
    bedroom.external_set(mode=HVACMode.HEAT, temperature=21.0, current=15.0)
    await hass.async_block_till_done()
    office_entity = _wrapper(hass, office_entry.entry_id)
    await office_entity.async_set_hvac_mode(HVACMode.COOL)
    await office_entity.async_set_temperature(**{ATTR_TEMPERATURE: 26.0})
    office.external_set(current=30.0)
    await hass.async_block_till_done()
    bedroom_entity = _wrapper(hass, bedroom_entry.entry_id)

    # FCFS ranks by claim age and priority only breaks exact ties, so put
    # both claims on the same instant: the higher-priority office wins.
    coordinator = bedroom_entity._coordinator
    assert coordinator is not None
    same_age = dt_util.utcnow() - timedelta(minutes=10)
    coordinator._demands[bedroom_entity.entity_id] = (Demand.HEAT, same_age)
    coordinator._demands[office_entity.entity_id] = (Demand.COOL, same_age)
    await _arbitrate(hass, bedroom_entity)

    assert hass.states.get(bedroom_entity.entity_id).attributes["group_mode"] == "cool"
    assert hass.states.get(BEDROOM_SOURCE).state == HVACMode.OFF


async def test_card_command_releases_the_park(
    hass: HomeAssistant,
) -> None:
    """A user command through the card outranks the group for this member."""
    pair = await _make_pair(hass)
    office = pair["office"]
    office_entity = pair["office_entity"]
    _backdate_demand(pair["bedroom_entity"], pair["bedroom_entity"].entity_id, 10)
    await _arbitrate(hass, pair["bedroom_entity"])
    assert _parked(hass, office_entity) == "off"
    office.set_hvac_mode_calls.clear()

    await office_entity.async_set_hvac_mode(HVACMode.OFF)
    await hass.async_block_till_done()

    # The park lifts and nothing re-parks: an off room demands nothing.
    assert office_entity.coordination_parked is False
    assert _parked(hass, office_entity) is None
    assert hass.states.get(office_entity.entity_id).state == HVACMode.OFF
    # The source already sat at the off hold, so the release sent no call.
    assert office.set_hvac_mode_calls == []


async def test_remote_override_of_a_parked_unit_is_reparked(
    hass: HomeAssistant,
) -> None:
    """A remote user wins the moment; the group re-parks without flapping."""
    pair = await _make_pair(hass)
    office = pair["office"]
    office_entity = pair["office_entity"]
    _backdate_demand(pair["bedroom_entity"], pair["bedroom_entity"].entity_id, 10)
    await _arbitrate(hass, pair["bedroom_entity"])
    assert hass.states.get(OFFICE_SOURCE).state == HVACMode.OFF
    parks_before = len(office.set_hvac_mode_calls)

    # Somebody points the remote at the parked office: cool, now.
    office.external_set(mode=HVACMode.COOL, temperature=26.0)
    await hass.async_block_till_done()

    # In the moment the user wins: the face mirrors the remote...
    assert hass.states.get(office_entity.entity_id).state == HVACMode.COOL
    # ...and since the room still opposes the group, the source is parked
    # again — exactly one more park command, no oscillation.
    assert hass.states.get(OFFICE_SOURCE).state == HVACMode.OFF
    assert len(office.set_hvac_mode_calls) == parks_before + 1
    assert _parked(hass, office_entity) == "off"


async def test_running_member_blocks_the_flip(hass: HomeAssistant) -> None:
    """No reversal while any member is physically running."""
    pair = await _make_pair(hass, min_cycle=0)
    bedroom = pair["bedroom"]
    bedroom_entity, office_entity = pair["bedroom_entity"], pair["office_entity"]

    _backdate_demand(bedroom_entity, bedroom_entity.entity_id, 10)
    # Age the office's cool claim too: it must be ready to take over the
    # moment the gates open, or the flip we are gating never even tries.
    _backdate_demand(bedroom_entity, office_entity.entity_id, 5)
    await _arbitrate(hass, bedroom_entity)
    _backdate_group_change(bedroom_entity, 20)  # open the cycle gate

    # The bedroom room warms up: nobody pressing for heat any more.
    bedroom.external_set(current=22.0, action="heating")
    await hass.async_block_till_done()

    # But its unit is still running: the valve may not reverse yet.
    assert hass.states.get(office_entity.entity_id).attributes["group_mode"] == "heat"
    assert _parked(hass, office_entity) == "off"

    # Once the bedroom reports quiet, the office gets its turn.
    bedroom.external_set(action="idle")
    await hass.async_block_till_done()
    assert hass.states.get(office_entity.entity_id).attributes["group_mode"] == "cool"
    assert hass.states.get(OFFICE_SOURCE).state == HVACMode.COOL


async def test_cycle_time_gates_the_group_flip(hass: HomeAssistant) -> None:
    """The group waits out the longest member's cycle time before reversing."""
    pair = await _make_pair(hass, min_cycle=900)
    bedroom = pair["bedroom"]
    bedroom_entity, office_entity = pair["bedroom_entity"], pair["office_entity"]

    _backdate_demand(bedroom_entity, bedroom_entity.entity_id, 10)
    _backdate_demand(bedroom_entity, office_entity.entity_id, 5)
    await _arbitrate(hass, bedroom_entity)
    assert hass.states.get(office_entity.entity_id).attributes["group_mode"] == "heat"

    # Incumbent satisfied while the cycle gate is still shut: no flip yet.
    bedroom.external_set(current=22.0)
    await hass.async_block_till_done()
    assert hass.states.get(office_entity.entity_id).attributes["group_mode"] == "heat"

    _backdate_group_change(bedroom_entity, 20)
    await _arbitrate(hass, office_entity)
    assert hass.states.get(office_entity.entity_id).attributes["group_mode"] == "cool"
    assert hass.states.get(OFFICE_SOURCE).state == HVACMode.COOL


async def test_conflict_event_fires_once_per_episode(
    hass: HomeAssistant,
) -> None:
    """Two rooms wanting opposite modes raise one group event per episode."""
    pair = await _make_pair(hass)
    office = pair["office"]
    bedroom_entity = pair["bedroom_entity"]
    office_entity = pair["office_entity"]

    _backdate_demand(bedroom_entity, bedroom_entity.entity_id, 10)
    _backdate_demand(bedroom_entity, office_entity.entity_id, 5)
    await _arbitrate(hass, bedroom_entity)

    # The episode opened during setup, before we started listening; bring
    # the office off the fence to close it...
    events = async_capture_events(hass, EVENT_MODE_CONFLICT)
    office.external_set(current=25.0)
    await hass.async_block_till_done()
    assert events == []

    # ...then push it back into disagreement: one fresh event for the new
    # episode, and re-arbitrating the same disagreement never spams the bus.
    office.external_set(current=30.0)
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["group"] == GROUP
    assert events[0].data["group_mode"] == "heat"
    assert events[0].data["heat"] == [bedroom_entity.entity_id]
    assert events[0].data["cool"] == [office_entity.entity_id]

    await _arbitrate(hass, bedroom_entity)
    assert len(events) == 1


async def test_unavailable_member_leaves_the_group_decision(
    hass: HomeAssistant,
) -> None:
    """A member whose source is unreachable neither claims nor parks."""
    pair = await _make_pair(hass)
    bedroom = pair["bedroom"]
    bedroom_entity, office_entity = pair["bedroom_entity"], pair["office_entity"]

    _backdate_demand(bedroom_entity, bedroom_entity.entity_id, 10)
    _backdate_demand(bedroom_entity, office_entity.entity_id, 5)
    await _arbitrate(hass, bedroom_entity)
    assert _parked(hass, office_entity) == "off"

    bedroom.set_online(False)
    await hass.async_block_till_done()

    # With the bedroom gone the office runs its own side again.
    assert hass.states.get(office_entity.entity_id).attributes["group_mode"] == "cool"
    assert _parked(hass, office_entity) is None
    assert hass.states.get(OFFICE_SOURCE).state == HVACMode.COOL


async def test_native_range_member_is_exempt_from_parking(
    hass: HomeAssistant,
) -> None:
    """A source holding both setpoints itself is never parked."""
    bedroom_entry, bedroom = await _make_zone(
        hass, name="Bedroom", source_id=BEDROOM_SOURCE
    )
    office_entry, office = await _make_zone(
        hass, name="Office", source_id=OFFICE_SOURCE
    )
    office.native_range = True
    office._push()
    await hass.async_block_till_done()

    bedroom.external_set(mode=HVACMode.HEAT, temperature=21.0, current=15.0)
    await hass.async_block_till_done()

    office_entity = _wrapper(hass, office_entry.entry_id)
    await office_entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
    office.external_set(current=30.0, low=24.0, high=26.0)
    await hass.async_block_till_done()
    bedroom_entity = _wrapper(hass, bedroom_entry.entry_id)

    _backdate_demand(bedroom_entity, bedroom_entity.entity_id, 10)
    await _arbitrate(hass, bedroom_entity)

    # The group runs heat, yet the office delegates its pair and stays put.
    assert hass.states.get(bedroom_entity.entity_id).attributes["group_mode"] == "heat"
    assert office_entity.coordination_parkable is False
    assert _parked(hass, office_entity) is None


async def test_parked_unit_still_reports_its_room(
    hass: HomeAssistant,
) -> None:
    """Demand survives a park: the face keeps reading the room it owns."""
    pair = await _make_pair(hass)
    office = pair["office"]
    office_entity = pair["office_entity"]
    _backdate_demand(pair["bedroom_entity"], pair["bedroom_entity"].entity_id, 10)
    await _arbitrate(hass, pair["bedroom_entity"])
    assert _parked(hass, office_entity) == "off"

    # Still parked, still hot, still demanding: queued for the next round.
    office.external_set(current=31.0)
    await hass.async_block_till_done()
    state = hass.states.get(office_entity.entity_id)
    assert state.attributes["demand"] == "cool"
    assert state.attributes["parked"] == "off"
    assert state.attributes[ATTR_CURRENT] == 31.0


async def test_unloading_a_member_lets_the_group_collapse(
    hass: HomeAssistant,
) -> None:
    """The last member out disposes of the group coordinator."""
    pair = await _make_pair(hass)
    office_entity = pair["office_entity"]
    coordinator = office_entity._coordinator
    assert coordinator is not None
    assert coordinator is pair["bedroom_entity"]._coordinator

    assert await hass.config_entries.async_unload(pair["office_entry"].entry_id)
    await hass.async_block_till_done()

    # The remaining member still works, uncoordinated-but-functional...
    assert hass.states.get(pair["bedroom_entity"].entity_id).state == HVACMode.HEAT
    # ...and the group is gone once the last member leaves too.
    assert await hass.config_entries.async_unload(pair["bedroom_entry"].entry_id)
    await hass.async_block_till_done()
    assert "house" not in hass.data[DOMAIN].get("groups", {})
