# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""Config flow for the Dual Setpoint Climate integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.climate import (
    ATTR_HVAC_MODES,
    HVACMode,
)
from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_MIN_CYCLE_TIME,
    CONF_MIN_GAP,
    CONF_SOURCE_ENTITY,
    DEFAULT_MIN_CYCLE_TIME,
    DOMAIN,
    cycle_seconds,
    default_min_gap,
    units_are_metric,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

MIN_GAP_MAX = 15.0
MIN_GAP_STEP = 0.5
DEFAULT_MIN_CYCLE_SECONDS = int(DEFAULT_MIN_CYCLE_TIME.total_seconds())


def _min_gap_default(options: dict[str, Any] | None, *, is_metric: bool) -> float:
    """Return the stored gap, or the unit-appropriate default."""
    value = (options or {}).get(CONF_MIN_GAP)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default_min_gap(is_metric=is_metric)


def _cycle_seconds(options: dict[str, Any] | None) -> int:
    """Return the stored cycle time in seconds, or the default."""
    return cycle_seconds((options or {}).get(CONF_MIN_CYCLE_TIME))


class DualSetpointConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Dual Setpoint Climate."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,  # noqa: ARG004
    ) -> DualSetpointOptionsFlow:
        """Return the options flow for this handler."""
        return DualSetpointOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step: pick the climate entity to wrap."""
        errors: dict[str, str] = {}

        if user_input is not None:
            source = user_input[CONF_SOURCE_ENTITY]

            await self.async_set_unique_id(source)
            if await self._async_source_is_wrapper(source):
                return self.async_abort(reason="circular")
            self._abort_if_unique_id_configured()

            error = self._async_validate_source(source)
            if error is not None:
                errors["base"] = error
            else:
                state = self.hass.states.get(source)
                assert state is not None  # noqa: S101
                return self.async_create_entry(
                    title=state.attributes.get(ATTR_FRIENDLY_NAME) or source,
                    data={CONF_SOURCE_ENTITY: source},
                    options={
                        CONF_MIN_GAP: default_min_gap(
                            is_metric=units_are_metric(self.hass.config.units)
                        ),
                        CONF_MIN_CYCLE_TIME: DEFAULT_MIN_CYCLE_SECONDS,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SOURCE_ENTITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=CLIMATE_DOMAIN),
                    ),
                },
            ),
            errors=errors,
        )

    def _async_validate_source(self, source: str) -> str | None:
        """Return an error key when the entity cannot be wrapped."""
        state = self.hass.states.get(source)
        if state is None:
            return "source_not_found"

        modes = state.attributes.get(ATTR_HVAC_MODES) or []
        if HVACMode.HEAT not in modes or HVACMode.COOL not in modes:
            return "source_not_dual"
        return None

    async def _async_source_is_wrapper(self, source: str) -> bool:
        """Return True when the source is itself one of our entities."""
        registry_entry = er.async_get(self.hass).async_get(source)
        if (
            registry_entry is None
            or (config_entry_id := registry_entry.config_entry_id) is None
        ):
            return False
        entry = self.hass.config_entries.async_get_entry(config_entry_id)
        return entry is not None and entry.domain == DOMAIN


class DualSetpointOptionsFlow(OptionsFlow):
    """Handle options for an existing Dual Setpoint Climate entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            # The DurationSelector hands back a duration dict; store whole
            # seconds so the option stays a simple JSON value.
            merged = {
                **self.config_entry.options,
                **user_input,
                CONF_MIN_CYCLE_TIME: cycle_seconds(user_input[CONF_MIN_CYCLE_TIME]),
            }
            return self.async_create_entry(title=self.config_entry.title, data=merged)

        options = self.config_entry.options
        is_metric = units_are_metric(self.hass.config.units)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MIN_GAP,
                        default=_min_gap_default(options, is_metric=is_metric),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=MIN_GAP_MAX,
                            step=MIN_GAP_STEP,
                            unit_of_measurement="°C" if is_metric else "°F",
                        ),
                    ),
                    vol.Required(
                        CONF_MIN_CYCLE_TIME,
                        default={"seconds": _cycle_seconds(options)},
                    ): selector.DurationSelector(
                        # Hours, minutes and seconds are enabled by default;
                        # this only adds the day field.
                        selector.DurationSelectorConfig(enable_day=True),
                    ),
                },
            ),
        )
