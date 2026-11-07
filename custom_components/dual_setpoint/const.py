# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""Constants for the Dual Setpoint Climate integration."""

from __future__ import annotations

from datetime import timedelta
from logging import Logger, getLogger
from typing import TYPE_CHECKING

from homeassistant.const import UnitOfTemperature

if TYPE_CHECKING:
    from homeassistant.util.unit_system import UnitSystem

LOGGER: Logger = getLogger(__package__)

DOMAIN = "dual_setpoint"

# Config / options keys
CONF_SOURCE_ENTITY = "source"
CONF_MIN_GAP = "min_gap"
CONF_MIN_CYCLE_TIME = "min_cycle_time"

# Options defaults.
#
# The minimum gap is expressed in the *system* temperature unit, so its default
# depends on which unit the system uses. Offering 2 °C and 4 °F (rather than
# converting one to the other) keeps the number the user sees a round one
# instead of an awkward 3.6 °F.
DEFAULT_MIN_GAP_CELSIUS = 2.0
DEFAULT_MIN_GAP_FAHRENHEIT = 4.0
DEFAULT_MIN_CYCLE_TIME = timedelta(minutes=30)

# How long to wait for the source entity to report the mode and setpoint we
# just commanded before deciding that whatever it reports is the truth.
# Absorbs stale polls and devices that briefly report a remembered per-mode
# setpoint right after a mode change.
SETTLE_WINDOW = timedelta(seconds=90)

# Slack used when comparing temperatures that the user configured, so that
# 2.0 and 2.0000000000000004 are treated as equal.
GAP_EPSILON = 1e-3


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
