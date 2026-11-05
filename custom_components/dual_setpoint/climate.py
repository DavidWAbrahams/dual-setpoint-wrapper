# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""Dual Setpoint Climate wrapper entity."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    ATTR_SWING_HORIZONTAL_MODE,
    ATTR_SWING_HORIZONTAL_MODES,
    ATTR_SWING_MODE,
    ATTR_SWING_MODES,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    ATTR_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_SUPPORTED_FEATURES,
    PRECISION_HALVES,
    PRECISION_TENTHS,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MIN_CYCLE_TIME,
    CONF_MIN_GAP,
    CONF_SOURCE_ENTITY,
    DOMAIN,
    GAP_EPSILON,
    LOGGER,
    SETTLE_WINDOW,
    cycle_seconds,
    default_min_gap,
    units_are_metric,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

# Core's own fallbacks are metric constants, so pick bounds per unit system.
_FALLBACK_LIMITS = {
    UnitOfTemperature.CELSIUS: (7.0, 35.0),
    UnitOfTemperature.FAHRENHEIT: (41.0, 95.0),
}

UNAVAILABLE_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)

# Appended to the source's name so a wrapper is recognizable next to its base.
_NAME_SUFFIX = " dual setpoint"

# Core rounds displayed temperatures to the entity's precision, and its default
# precision for Fahrenheit systems is whole degrees. A Celsius-native source
# (e.g. an ESPHome CN105) still publishes half- or tenth-degree Fahrenheit
# readings though, so those would collapse to integers on our face. Any
# fractional value among the source's own published temperatures tells us the
# source displays finer than whole degrees, and we match it.
_PRECISION_PROBE_ATTRS = (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_TEMPERATURE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
)

# Slack for deciding a fraction lands exactly on a display step.
_PRECISION_EPSILON = 1e-6

# Core's display steps, coarsest fractional to finest.
_PRECISION_STEPS = (PRECISION_HALVES, PRECISION_TENTHS)

_MIRRORED_FEATURES = (
    ClimateEntityFeature.FAN_MODE,
    ClimateEntityFeature.SWING_MODE,
    ClimateEntityFeature.PRESET_MODE,
    ClimateEntityFeature.TURN_ON,
    ClimateEntityFeature.TURN_OFF,
    ClimateEntityFeature.SWING_HORIZONTAL_MODE,
)


def _suffixed_name(base: str | None) -> str | None:
    """Mark a copied source name so the wrapper reads as one."""
    if base is None:
        return None
    if base.casefold().endswith(_NAME_SUFFIX.strip()):
        return base
    return f"{base}{_NAME_SUFFIX}"


def _as_float(value: Any) -> float | None:
    """Return value as a float, or None when it is not usable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _observed_precision(values: Iterable[Any]) -> float | None:
    """
    Return the finest core precision that displays the values unchanged.

    Core only rounds displayed temperatures to whole, half, or tenth degrees.
    A value with no fractional part carries no information; a value like 71.5
    needs halves, and one like 71.3 needs tenths. Returns None when none of
    the values are fractional.
    """
    needed: float | None = None
    for value in values:
        number = _as_float(value)
        if number is None:
            continue
        fraction = abs(number) % 1
        if fraction <= _PRECISION_EPSILON or fraction >= 1 - _PRECISION_EPSILON:
            continue
        # Off-grid fractions keep the most detail at the finest step.
        finest = next(
            (
                step
                for step in _PRECISION_STEPS
                if abs(round(fraction / step) * step - fraction) <= _PRECISION_EPSILON
            ),
            _PRECISION_STEPS[-1],
        )
        if needed is None or finest < needed:
            needed = finest
    return needed


class DualSetpointClimate(ClimateEntity, RestoreEntity):
    """Expose heat_cool on top of a single-setpoint climate entity."""

    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the wrapper."""
        self._entry = entry
        self._source_entity_id: str = entry.data[CONF_SOURCE_ENTITY]

        min_gap = _as_float(entry.options.get(CONF_MIN_GAP))
        if min_gap is None:
            min_gap = default_min_gap(is_metric=units_are_metric(hass.config.units))
        self._min_gap = min_gap

        self._min_cycle_time = timedelta(
            seconds=cycle_seconds(entry.options.get(CONF_MIN_CYCLE_TIME))
        )

        self._attr_unique_id = entry.entry_id
        self._attr_temperature_unit = hass.config.units.temperature_unit

        # Owned state
        self._attr_hvac_mode: HVACMode | None = None
        self._target_temp: float | None = None
        self._target_temp_low: float | None = None
        self._target_temp_high: float | None = None
        self._last_flip: datetime | None = None

        # Command tracking, used to tell our own writes from external changes
        self._expected_mode: HVACMode | None = None
        self._expected_temp: float | None = None
        self._pending = False

        self._settle_unsub: CALLBACK_TYPE | None = None
        self._defer_unsub: CALLBACK_TYPE | None = None
        self._applying = False
        self._warned_no_reading = False

        self._attach_device(hass)

    # ------------------------------------------------------------------
    # Device attachment (switch_as_x pattern)
    # ------------------------------------------------------------------
    def _attach_device(self, hass: HomeAssistant) -> None:
        """Attach to the source's device, or create our own."""
        entity_registry = er.async_get(hass)
        registry_entry = entity_registry.async_get(self._source_entity_id)
        source_name: str | None = None
        if registry_entry is not None:
            source_name = registry_entry.original_name or registry_entry.name
            device_id = registry_entry.device_id
            device_registry = dr.async_get(hass)
            if device_id and (device := device_registry.async_get(device_id)):
                self.device_entry = device
                self._attr_has_entity_name = registry_entry.has_entity_name
                self._attr_name = _suffixed_name(registry_entry.original_name)
                return

        if source_name is None and (state := hass.states.get(self._source_entity_id)):
            source_name = state.attributes.get(ATTR_FRIENDLY_NAME)
        self._attr_name = _suffixed_name(source_name) or "Dual setpoint"

    # ------------------------------------------------------------------
    # Mirrored surface
    # ------------------------------------------------------------------
    @property
    def _source_state(self) -> State | None:
        """Return the source entity state."""
        return self.hass.states.get(self._source_entity_id)

    @property
    def _source_attributes(self) -> dict[str, Any]:
        """Return the source attributes, empty when it is gone."""
        state = self._source_state
        return dict(state.attributes) if state is not None else {}

    @property
    def _source_available(self) -> bool:
        """Return True when the source reports usable state."""
        state = self._source_state
        return state is not None and state.state not in UNAVAILABLE_STATES

    @property
    def available(self) -> bool:
        """Follow the source availability."""
        return self._source_available

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the source modes plus heat_cool."""
        modes: list[HVACMode] = []
        state = self._source_state
        if state is not None:
            modes = list(state.attributes.get(ATTR_HVAC_MODES) or [])
        if HVACMode.HEAT_COOL not in modes:
            modes = [*modes, HVACMode.HEAT_COOL]
        return modes

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return our range features plus whatever the source supports."""
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        )
        raw = self._source_attributes.get(ATTR_SUPPORTED_FEATURES) or 0
        try:
            source_features = ClimateEntityFeature(int(raw))
        except ValueError:
            source_features = ClimateEntityFeature(0)
        for feature in _MIRRORED_FEATURES:
            if feature in source_features:
                features |= feature
        return features

    @property
    def min_temp(self) -> float:
        """Mirror the source minimum."""
        if (value := _as_float(self._source_attributes.get(ATTR_MIN_TEMP))) is not None:
            return value
        return _FALLBACK_LIMITS[self.temperature_unit][0]

    @property
    def max_temp(self) -> float:
        """Mirror the source maximum."""
        if (value := _as_float(self._source_attributes.get(ATTR_MAX_TEMP))) is not None:
            return value
        return _FALLBACK_LIMITS[self.temperature_unit][1]

    @property
    def target_temperature_step(self) -> float | None:
        """Mirror the source step."""
        return _as_float(self._source_attributes.get(ATTR_TARGET_TEMP_STEP))

    @property
    def current_temperature(self) -> float | None:
        """Mirror the source reading."""
        return _as_float(self._source_attributes.get(ATTR_CURRENT_TEMPERATURE))

    @property
    def precision(self) -> float:
        """
        Match the precision the source itself displays.

        Core defaults to whole degrees on Fahrenheit systems, which would
        flatten the half-degree readings a Celsius-native source publishes in
        Fahrenheit. When the source's own published temperatures show finer
        fractions, round no more coarsely than the source does.
        """
        default = super().precision
        observed = _observed_precision(
            self._source_attributes.get(attr) for attr in _PRECISION_PROBE_ATTRS
        )
        if observed is not None and observed < default:
            return observed
        return default

    @property
    def hvac_action(self) -> HVACAction | None:
        """Mirror the source action."""
        action = self._source_attributes.get(ATTR_HVAC_ACTION)
        if action is None:
            return None
        with contextlib.suppress(ValueError):
            return HVACAction(action)
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return the single target, unused while in heat_cool."""
        if self._attr_hvac_mode == HVACMode.HEAT_COOL:
            return None
        return self._target_temp

    @property
    def target_temperature_low(self) -> float | None:
        """Return the heating setpoint."""
        return self._target_temp_low

    @property
    def target_temperature_high(self) -> float | None:
        """Return the cooling setpoint."""
        return self._target_temp_high

    @property
    def fan_mode(self) -> str | None:
        """Mirror the source fan mode."""
        value = self._source_attributes.get(ATTR_FAN_MODE)
        return str(value) if value is not None else None

    @property
    def fan_modes(self) -> list[str] | None:
        """Mirror the source fan modes."""
        value = self._source_attributes.get(ATTR_FAN_MODES)
        return list(value) if value else None

    @property
    def swing_mode(self) -> str | None:
        """Mirror the source swing mode."""
        value = self._source_attributes.get(ATTR_SWING_MODE)
        return str(value) if value is not None else None

    @property
    def swing_modes(self) -> list[str] | None:
        """Mirror the source swing modes."""
        value = self._source_attributes.get(ATTR_SWING_MODES)
        return list(value) if value else None

    @property
    def swing_horizontal_mode(self) -> str | None:
        """Mirror the source horizontal swing mode."""
        value = self._source_attributes.get(ATTR_SWING_HORIZONTAL_MODE)
        return str(value) if value is not None else None

    @property
    def swing_horizontal_modes(self) -> list[str] | None:
        """Mirror the source horizontal swing modes."""
        value = self._source_attributes.get(ATTR_SWING_HORIZONTAL_MODES)
        return list(value) if value else None

    @property
    def preset_mode(self) -> str | None:
        """Mirror the source preset."""
        value = self._source_attributes.get(ATTR_PRESET_MODE)
        return str(value) if value is not None else None

    @property
    def preset_modes(self) -> list[str] | None:
        """Mirror the source presets."""
        value = self._source_attributes.get(ATTR_PRESET_MODES)
        return list(value) if value else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the flip timestamp so it survives a restart."""
        return {
            "last_flip": self._last_flip.isoformat() if self._last_flip else None,
            "source_entity": self._source_entity_id,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_added_to_hass(self) -> None:
        """Restore state and start tracking the source."""
        await super().async_added_to_hass()

        self._restore_owned_state(await self.async_get_last_state())

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._source_entity_id], self._async_source_changed
            )
        )
        self.async_on_remove(self._cancel_timers)

        # Seed from the source when we have nothing restored to work with.
        if self._attr_hvac_mode is None:
            self._mirror_source_state(initial=True)

        if self._attr_hvac_mode == HVACMode.HEAT_COOL:
            await self._async_evaluate(gate_flips=False)
        self.async_write_ha_state()

    def _restore_owned_state(self, last_state: State | None) -> None:
        """Load the owned state, normalizing the gap."""
        if last_state is None or last_state.state in UNAVAILABLE_STATES:
            return

        with contextlib.suppress(ValueError):
            self._attr_hvac_mode = HVACMode(last_state.state)

        attrs = last_state.attributes
        self._target_temp = _as_float(attrs.get(ATTR_TEMPERATURE))
        self._target_temp_low = _as_float(attrs.get(ATTR_TARGET_TEMP_LOW))
        self._target_temp_high = _as_float(attrs.get(ATTR_TARGET_TEMP_HIGH))

        if (raw_flip := attrs.get("last_flip")) and isinstance(raw_flip, str):
            with contextlib.suppress(ValueError):
                self._last_flip = datetime.fromisoformat(raw_flip)

        self._expected_mode = self._attr_hvac_mode
        self._expected_temp = self._target_temp

        if self._target_temp_low is not None and self._target_temp_high is not None:
            self._target_temp_low, self._target_temp_high = self._normalize(
                self._target_temp_low, self._target_temp_high
            )

    def _cancel_timers(self) -> None:
        """Cancel any pending timers."""
        for unsub in (self._settle_unsub, self._defer_unsub):
            if unsub is not None:
                unsub()
        self._settle_unsub = None
        self._defer_unsub = None

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------
    def _normalize(self, low: float, high: float) -> tuple[float, float]:
        """Clamp both bounds and restore the configured gap."""
        min_temp = self.min_temp
        max_temp = self.max_temp
        low = max(min_temp, min(low, max_temp))
        high = max(min_temp, min(high, max_temp))

        if high - low + GAP_EPSILON < self._min_gap:
            high = low + self._min_gap
            if high > max_temp:
                high = max_temp
                low = max(min_temp, max_temp - self._min_gap)
            if low < min_temp:
                low = min_temp
                high = min(max_temp, min_temp + self._min_gap)
        return low, high

    def _step_tolerance(self) -> float:
        """
        Return the drift we absorb before calling a change deliberate.

        Devices that quantize setpoints to their own grid (a Celsius-native
        device handed a Fahrenheit target) echo back within half a step of
        what we sent, so that much drift reads as our own command returning.
        A whole step, though, is exactly what a user picks on a dial: with a
        full step as the tolerance a one-step command was judged already
        matched and never sent, and a one-step external change was judged
        noise and never mirrored. The tolerance therefore stays under one
        step.
        """
        step = _as_float(self._source_attributes.get(ATTR_TARGET_TEMP_STEP)) or 1.0
        return step / 2

    def _source_target(self) -> float | None:
        """Return the source's active single setpoint."""
        attrs = self._source_attributes
        return _as_float(attrs.get(ATTR_TEMPERATURE))

    # ------------------------------------------------------------------
    # Command layer
    # ------------------------------------------------------------------
    async def _async_command(self, mode: HVACMode, temperature: float | None) -> bool:
        """
        Push one mode+setpoint command to the source.

        Returns True when the source already matched and nothing was sent.
        """
        if not self._available_for_command():
            return False

        state = self._source_state
        assert state is not None  # noqa: S101
        tolerance = self._step_tolerance()

        mode_matches = state.state == mode
        source_temp = self._source_target()
        temp_matches = (
            temperature is None
            or source_temp is None
            or abs(source_temp - temperature) <= tolerance + GAP_EPSILON
        )
        if mode_matches and temp_matches:
            self._clear_pending()
            self._expected_mode = mode
            self._expected_temp = (
                temperature if temperature is not None else source_temp
            )
            return True

        data: dict[str, Any] = {ATTR_ENTITY_ID: self._source_entity_id}
        data[ATTR_HVAC_MODE] = mode
        if temperature is not None:
            data[ATTR_TEMPERATURE] = temperature
            service = "set_temperature"
        else:
            # Mode-only changes (off, fan_only, dry) must go through
            # set_hvac_mode: core's set_temperature schema requires at
            # least one setpoint field and rejects the call without one.
            service = "set_hvac_mode"

        try:
            await self.hass.services.async_call(
                "climate",
                service,
                data,
                blocking=True,
                context=self._context,
            )
        except ServiceValidationError:
            raise
        except HomeAssistantError as err:
            LOGGER.warning(
                "Could not command %s to %s at %s: %s",
                self._source_entity_id,
                mode,
                temperature,
                err,
            )
            return False

        self._expected_mode = mode
        self._expected_temp = temperature
        self._pending = True
        self._arm_settle_timer()
        return False

    def _available_for_command(self) -> bool:
        """Return True when commands may be sent to the source."""
        state = self._source_state
        return state is not None and state.state not in UNAVAILABLE_STATES

    def _clear_pending(self) -> None:
        """Stop waiting for our last command to be acknowledged."""
        self._pending = False
        if self._settle_unsub is not None:
            self._settle_unsub()
            self._settle_unsub = None

    def _arm_settle_timer(self) -> None:
        """Start the window in which the source may confirm our command."""
        if self._settle_unsub is not None:
            self._settle_unsub()
        self._settle_unsub = async_call_later(
            self.hass, SETTLE_WINDOW, self._async_settle_expired
        )

    @callback
    def _async_settle_expired(self, _now: datetime) -> None:
        """Treat the source as authoritative when it never confirmed."""
        self._settle_unsub = None
        self._pending = False
        self._mirror_source_state()
        self.async_write_ha_state()
        if self._attr_hvac_mode == HVACMode.HEAT_COOL:
            self.hass.async_create_task(self._async_evaluate(gate_flips=True))

    # ------------------------------------------------------------------
    # heat_cool engine
    # ------------------------------------------------------------------
    async def _async_evaluate(self, *, gate_flips: bool = True) -> None:
        """Bring the source in line with the bounds while in heat_cool."""
        if self._applying or self._attr_hvac_mode != HVACMode.HEAT_COOL:
            return
        if not self._available_for_command():
            return
        if self._target_temp_low is None or self._target_temp_high is None:
            return

        reading = self.current_temperature
        if reading is None:
            if not self._warned_no_reading:
                self._warned_no_reading = True
                LOGGER.debug(
                    "%s does not report a current temperature; waiting",
                    self._source_entity_id,
                )
            return
        self._warned_no_reading = False

        mid = (self._target_temp_low + self._target_temp_high) / 2
        if reading < mid:
            desired_mode, desired_temp = HVACMode.HEAT, self._target_temp_low
        else:
            desired_mode, desired_temp = HVACMode.COOL, self._target_temp_high

        state = self._source_state
        assert state is not None  # noqa: S101
        flip_needed = state.state != desired_mode

        if flip_needed and gate_flips and not self._flip_allowed(dt_util.utcnow()):
            return

        self._applying = True
        try:
            if flip_needed:
                self._last_flip = dt_util.utcnow()
            await self._async_command(desired_mode, desired_temp)
        finally:
            self._applying = False
        self.async_write_ha_state()

    def _flip_allowed(self, now: datetime) -> bool:
        """Return True when a heat<->cool flip may happen now."""
        if self._last_flip is None:
            return True
        elapsed = now - self._last_flip
        if elapsed >= self._min_cycle_time:
            return True
        self._schedule_flip(self._min_cycle_time - elapsed)
        return False

    def _schedule_flip(self, delay: timedelta) -> None:
        """Re-evaluate as soon as the cycle time has elapsed."""
        if self._defer_unsub is not None:
            return  # one pending re-evaluation is enough
        LOGGER.debug("Flip blocked by min_cycle_time, retrying in %s", delay)

        @callback
        def _retry(_now: datetime) -> None:
            self._defer_unsub = None
            self.hass.async_create_task(self._async_evaluate(gate_flips=True))

        self._defer_unsub = async_call_later(self.hass, delay, _retry)

    # ------------------------------------------------------------------
    # Source tracking / out-of-band detection
    # ------------------------------------------------------------------
    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        """React to any change on the source entity."""
        if self._applying:
            return

        new_state = event.data["new_state"]
        if new_state is None or new_state.state in UNAVAILABLE_STATES:
            self.async_write_ha_state()
            return

        if self._attr_hvac_mode is None:
            self._mirror_source_state(initial=True)
        elif self._pending:
            if not self._source_matches_expected():
                # Stale poll or a per-mode setpoint echo: not news yet.
                self.async_write_ha_state()
                return
            self._clear_pending()
        elif self._is_out_of_band():
            LOGGER.debug(
                "Source %s changed outside of our control, mirroring %s",
                self._source_entity_id,
                new_state.state,
            )
            self._cancel_pending_flip()
            self._mirror_source_state()
        self.async_write_ha_state()

        if self._attr_hvac_mode == HVACMode.HEAT_COOL:
            self.hass.async_create_task(self._async_evaluate(gate_flips=True))

    def _cancel_pending_flip(self) -> None:
        """Drop a deferred flip that an external change superseded."""
        if self._defer_unsub is not None:
            self._defer_unsub()
            self._defer_unsub = None

    def _source_matches_expected(self) -> bool:
        """Return True when the source reports what we last commanded."""
        state = self._source_state
        assert state is not None  # noqa: S101
        if self._expected_mode is not None and state.state != self._expected_mode:
            return False
        source_temp = self._source_target()
        if self._expected_temp is None or source_temp is None:
            return True
        return (
            abs(source_temp - self._expected_temp)
            <= self._step_tolerance() + GAP_EPSILON
        )

    def _is_out_of_band(self) -> bool:
        """Return True when the source moved somewhere we did not ask it."""
        state = self._source_state
        assert state is not None  # noqa: S101
        if self._expected_mode is not None and state.state != self._expected_mode:
            return True
        source_temp = self._source_target()
        if self._expected_temp is None or source_temp is None:
            return False
        return (
            abs(source_temp - self._expected_temp)
            > self._step_tolerance() + GAP_EPSILON
        )

    def _mirror_source_state(self, *, initial: bool = False) -> None:
        """Adopt the source's mode and setpoint as our own."""
        state = self._source_state
        if state is None or state.state in UNAVAILABLE_STATES:
            return

        with contextlib.suppress(ValueError):
            self._attr_hvac_mode = HVACMode(state.state)
        source_temp = self._source_target()
        self._target_temp = source_temp
        self._expected_mode = self._attr_hvac_mode
        self._expected_temp = source_temp
        self._pending = False

        if source_temp is not None:
            self._reconcile_bounds(source_temp)
        elif self._target_temp_low is None or self._target_temp_high is None:
            self._seed_bounds(None)

        if initial:
            self._clear_pending()

    def _reconcile_bounds(self, source_temp: float) -> None:
        """Keep low/high coherent with a single-setpoint source change."""
        if self._attr_hvac_mode == HVACMode.HEAT:
            low = source_temp
            high = (
                source_temp + self._min_gap
                if self._target_temp_high is None
                or abs(self._target_temp_high - (source_temp + self._min_gap))
                > GAP_EPSILON
                else self._target_temp_high
            )
            self._target_temp_low, self._target_temp_high = self._normalize(low, high)
        elif self._attr_hvac_mode == HVACMode.COOL:
            high = source_temp
            low = (
                source_temp - self._min_gap
                if self._target_temp_low is None
                or abs(self._target_temp_low - (source_temp - self._min_gap))
                > GAP_EPSILON
                else self._target_temp_low
            )
            self._target_temp_low, self._target_temp_high = self._normalize(low, high)
        elif self._target_temp_low is None or self._target_temp_high is None:
            self._seed_bounds(source_temp)

    def _seed_bounds(self, anchor: float | None) -> None:
        """Derive both bounds from a single anchor temperature."""
        if anchor is None:
            anchor = (self.min_temp + self.max_temp) / 2
        mid_gap = self._min_gap / 2
        self._target_temp_low, self._target_temp_high = self._normalize(
            anchor - mid_gap, anchor + mid_gap
        )

    # ------------------------------------------------------------------
    # Public commands
    # ------------------------------------------------------------------
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Handle a mode change."""
        if hvac_mode == self._attr_hvac_mode:
            return

        if hvac_mode == HVACMode.HEAT_COOL:
            await self._async_enter_heat_cool()
            return

        previous = self._attr_hvac_mode
        self._cancel_pending_flip()
        self._attr_hvac_mode = hvac_mode
        if hvac_mode not in (HVACMode.OFF, HVACMode.FAN_ONLY, HVACMode.DRY):
            # Leaving heat_cool keeps the bound the new mode owns: the low
            # setpoint carries into heat, the high setpoint carries into cool.
            if previous == HVACMode.HEAT_COOL:
                if hvac_mode == HVACMode.HEAT and self._target_temp_low is not None:
                    self._target_temp = self._target_temp_low
                elif hvac_mode == HVACMode.COOL and self._target_temp_high is not None:
                    self._target_temp = self._target_temp_high
            if self._target_temp is None:
                self._target_temp = (self.min_temp + self.max_temp) / 2
            await self._async_command(hvac_mode, self._target_temp)
        else:
            await self._async_command(hvac_mode, None)
        self.async_write_ha_state()

    async def _async_enter_heat_cool(self) -> None:
        """Switch into heat_cool and apply the decision immediately."""
        previous = self._attr_hvac_mode
        self._attr_hvac_mode = HVACMode.HEAT_COOL
        self._cancel_pending_flip()

        if self._target_temp_low is None or self._target_temp_high is None:
            source_temp = self._source_target()
            if source_temp is None:
                source_temp = self._target_temp
            self._seed_bounds(source_temp)
        else:
            self._align_bounds(previous)

        # Entering is not subject to min_cycle_time.
        await self._async_evaluate(gate_flips=False)
        if self._last_flip is None:
            self._last_flip = dt_util.utcnow()
        self.async_write_ha_state()

    def _align_bounds(self, previous: HVACMode | None) -> None:
        """Move the single setpoint into the bound its previous mode owns."""
        temp = self._target_temp
        if (
            temp is None
            or self._target_temp_low is None
            or self._target_temp_high is None
        ):
            return
        if previous == HVACMode.HEAT:
            if abs(self._target_temp_low - temp) <= GAP_EPSILON:
                return
            high = max(self._target_temp_high, temp + self._min_gap)
            self._target_temp_low, self._target_temp_high = self._normalize(temp, high)
        elif previous == HVACMode.COOL:
            if abs(self._target_temp_high - temp) <= GAP_EPSILON:
                return
            low = min(self._target_temp_low, temp - self._min_gap)
            self._target_temp_low, self._target_temp_high = self._normalize(low, temp)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Handle a temperature change, enforcing the minimum gap."""
        if (new_mode := kwargs.get(ATTR_HVAC_MODE)) is not None and (
            new_mode != self._attr_hvac_mode
        ):
            await self.async_set_hvac_mode(HVACMode(new_mode))

        low = _as_float(kwargs.get(ATTR_TARGET_TEMP_LOW))
        high = _as_float(kwargs.get(ATTR_TARGET_TEMP_HIGH))
        temperature = _as_float(kwargs.get(ATTR_TEMPERATURE))

        if low is not None and high is not None:
            await self._async_set_range(low, high)
        elif temperature is not None:
            await self._async_set_single(temperature)

    async def _async_set_range(self, low: float, high: float) -> None:
        """Validate and store a heat_cool range."""
        if high - low + GAP_EPSILON < self._min_gap:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="gap_too_small",
                translation_placeholders={
                    "min_gap": f"{self._min_gap:g}",
                    "unit": self.temperature_unit,
                },
            )

        self._target_temp_low, self._target_temp_high = self._normalize(low, high)

        if self._attr_hvac_mode == HVACMode.HEAT:
            self._target_temp = self._target_temp_low
            await self._async_command(HVACMode.HEAT, self._target_temp_low)
        elif self._attr_hvac_mode == HVACMode.COOL:
            self._target_temp = self._target_temp_high
            await self._async_command(HVACMode.COOL, self._target_temp_high)
        elif self._attr_hvac_mode == HVACMode.HEAT_COOL:
            await self._async_evaluate(gate_flips=True)
        self.async_write_ha_state()

    async def _async_set_single(self, temperature: float) -> None:
        """Handle a single setpoint, adjusting the active side in heat_cool."""
        if self._attr_hvac_mode == HVACMode.HEAT_COOL:
            state = self._source_state
            source_mode = state.state if state is not None else ""
            if source_mode == HVACMode.COOL:
                await self._async_set_range(
                    self._target_temp_low or temperature, temperature
                )
            else:
                await self._async_set_range(
                    temperature, self._target_temp_high or temperature
                )
            return

        self._target_temp = temperature
        self._reconcile_bounds(temperature)
        await self._async_command(self._attr_hvac_mode or HVACMode.HEAT, temperature)
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Forward the fan mode to the source."""
        await self._call_source_mirror("set_fan_mode", {ATTR_FAN_MODE: fan_mode})

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Forward the swing mode to the source."""
        await self._call_source_mirror("set_swing_mode", {ATTR_SWING_MODE: swing_mode})

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Forward the preset mode to the source."""
        await self._call_source_mirror(
            "set_preset_mode", {ATTR_PRESET_MODE: preset_mode}
        )

    async def async_set_swing_horizontal_mode(self, swing_horizontal_mode: str) -> None:
        """Forward the horizontal swing mode to the source."""
        await self._call_source_mirror(
            "set_swing_horizontal_mode",
            {ATTR_SWING_HORIZONTAL_MODE: swing_horizontal_mode},
        )

    async def async_turn_on(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Turn the source on."""
        await self._call_source_mirror("turn_on", {})

    async def async_turn_off(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Turn the source off."""
        await self._call_source_mirror("turn_off", {})

    async def _call_source_mirror(self, service: str, data: dict[str, Any]) -> None:
        """Forward a passthrough service call to the source."""
        await self.hass.services.async_call(
            "climate",
            service,
            {ATTR_ENTITY_ID: self._source_entity_id, **data},
            blocking=True,
            context=self._context,
        )
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the wrapper entity for a config entry."""
    async_add_entities([DualSetpointClimate(hass, entry)])
