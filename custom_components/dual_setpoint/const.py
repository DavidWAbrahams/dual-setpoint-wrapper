# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""Constants for the Dual Setpoint Climate integration."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from logging import Logger, getLogger
from typing import TYPE_CHECKING

from homeassistant.const import UnitOfTemperature

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.util.unit_system import UnitSystem

    from .coordinator import ModeCoordinator

LOGGER: Logger = getLogger(__package__)

DOMAIN = "dual_setpoint"

# Config / options keys
CONF_SOURCE_ENTITY = "source"
CONF_MIN_GAP = "min_gap"
CONF_MIN_CYCLE_TIME = "min_cycle_time"
CONF_COORDINATE_MODES = "coordinate_modes"
CONF_COORDINATION_GROUP = "coordination_group"
CONF_COORDINATION_PRIORITY = "coordination_priority"
CONF_THWARTED_HEAT_HOLD = "thwarted_heat_hold"
CONF_THWARTED_COOL_HOLD = "thwarted_cool_hold"
CONF_THWARTED_FAN_MODE = "thwarted_fan_mode"

# Options defaults.
#
# The minimum gap is expressed in the *system* temperature unit, so its default
# depends on which unit the system uses. Offering 2 °C and 4 °F (rather than
# converting one to the other) keeps the number the user sees a round one
# instead of an awkward 3.6 °F.
DEFAULT_MIN_GAP_CELSIUS = 2.0
DEFAULT_MIN_GAP_FAHRENHEIT = 4.0
DEFAULT_MIN_CYCLE_TIME = timedelta(minutes=30)

# Coordination defaults. The master switch is off, so upgrading never changes
# anybody's behavior until they opt a wrapper into a group.
DEFAULT_COORDINATION_GROUP = "default"
DEFAULT_COORDINATION_PRIORITY = 0
DEFAULT_THWARTED_HOLD = "off"
DEFAULT_THWARTED_FAN_MODE = ""

# Hold states a thwarted member's source may be parked in.
THWARTED_HOLD_OFF = "off"
THWARTED_HOLD_FAN_ONLY = "fan_only"
THWARTED_HOLD_OPTIONS = (THWARTED_HOLD_OFF, THWARTED_HOLD_FAN_ONLY)

# How long to wait for the source entity to report the mode and setpoint we
# just commanded before deciding that whatever it reports is the truth.
# Absorbs stale polls and devices that briefly report a remembered per-mode
# setpoint right after a mode change.
SETTLE_WINDOW = timedelta(seconds=90)

# Slack used when comparing temperatures that the user configured, so that
# 2.0 and 2.0000000000000004 are treated as equal.
GAP_EPSILON = 1e-3

# How long an opposite-mode demand must persist before it may claim the
# group's reversing valve, so a sensor blip cannot re-plumb the whole house.
CLAIM_DELAY = timedelta(seconds=120)

# Event fired when two members genuinely need opposite modes; automations can
# react to the physically unsatisfiable situation.
EVENT_MODE_CONFLICT = "dual_setpoint_mode_conflict"


class Demand(StrEnum):
    """What a member's room actually needs, independent of its set mode."""

    HEAT = "heat"
    COOL = "cool"
    IDLE = "idle"
    NONE = "none"


def get_coordinator(hass: HomeAssistant, group: str) -> ModeCoordinator:
    """
    Return the shared coordinator for a group name, creating it if needed.

    Groups live in ``hass.data[DOMAIN]`` keyed by their user-chosen name; the
    last member to leave disposes of the group (see coordinator.py).
    """
    groups: dict[str, ModeCoordinator] = hass.data.setdefault(DOMAIN, {}).setdefault(
        "groups", {}
    )
    if group not in groups:
        # Imported here so const.py stays importable without the coordinator.
        from .coordinator import ModeCoordinator  # noqa: PLC0415

        groups[group] = ModeCoordinator(hass, group)
    return groups[group]


def dispose_coordinator(hass: HomeAssistant, group: str) -> None:
    """Drop a group's coordinator once its last member leaves."""
    groups: dict[str, ModeCoordinator] | None = hass.data.get(DOMAIN, {}).get("groups")
    if groups is not None:
        groups.pop(group, None)


def units_are_metric(units: UnitSystem) -> bool:
    """
    Return True when the system measures temperature in Celsius.

    ``UnitSystem`` carries no ``is_metric`` flag, so the unit it converts to
    (``temperature_unit``; beware that ``temperature`` is a converter method)
    is the only thing to go on.
    """
    return units.temperature_unit == UnitOfTemperature.CELSIUS


def default_min_gap(*, is_metric: bool) -> float:
    """Return the unit-appropriate default minimum gap."""
    return DEFAULT_MIN_GAP_CELSIUS if is_metric else DEFAULT_MIN_GAP_FAHRENHEIT


def cycle_seconds(value: object) -> int:
    """
    Return a stored cycle-time option in seconds.

    The DurationSelector hands back a duration dict such as
    ``{"hours": 0, "minutes": 20}``, so accept that shape; also accept a
    timedelta and a plain second count, which older or hand-written options
    use.
    """
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    if isinstance(value, dict):
        parts = {
            key: amount
            for key, amount in value.items()
            if key in {"days", "hours", "minutes", "seconds"}
            and isinstance(amount, (int, float))
            and not isinstance(amount, bool)
        }
        return int(timedelta(**parts).total_seconds())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return int(DEFAULT_MIN_CYCLE_TIME.total_seconds())
