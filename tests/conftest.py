# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the Dual Setpoint Climate tests."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_socket
from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    ATTR_TEMPERATURE,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_SUPPORTED_FEATURES,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, ServiceCall, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dual_setpoint.climate import DualSetpointClimate
from custom_components.dual_setpoint.const import (
    CONF_MIN_CYCLE_TIME,
    CONF_MIN_GAP,
    CONF_SOURCE_ENTITY,
    DOMAIN,
)

SOURCE_ENTITY = "climate.thermostat"
ENTRY_ID = "test-entry"


if os.name != "posix":
    # pytest-homeassistant-custom-component guards socket.socket to block the
    # network, but its exemption for asyncio's self-pipe only covers AF_UNIX,
    # which Windows lacks: its proactor loop pairs two AF_INET sockets, so the
    # very first event loop would trip the guard. Leave the guard disabled on
    # Windows; these tests never touch the network anyway.
    def _disable_socket(*_args: object, **_kwargs: object) -> None:
        """Pretend to block sockets, but let the Windows event loop run."""

    pytest_socket.disable_socket = _disable_socket


# Where the live FakeThermostats publish themselves on a given hass so the
# climate service handlers can route a call to the fake owning the target.
_FAKE_REGISTRY = "_dual_setpoint_fake_thermostats"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow our custom integration to load in every test."""


class FakeThermostat:
    """
    A climate entity simulated with plain state.

    By default it is a single-setpoint heat/cool/off source. The two
    heat_cool flavors the wrapper must handle are selectable:

    ``expose_heat_cool``
        Adds heat_cool to the mode list without dual setpoints: the
        CN105-style "auto" that balances around one setpoint.

    ``native_range``
        Adds heat_cool together with the TARGET_TEMPERATURE_RANGE feature
        and live target_temp_low/target_temp_high attributes: a source the
        wrapper can delegate the pair to.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str = SOURCE_ENTITY,
        *,
        friendly_name: str = "Thermostat",
    ) -> None:
        """Register the climate.set_temperature service we answer to."""
        self.hass = hass
        self.entity_id = entity_id
        self.friendly_name = friendly_name
        self.mode: HVACMode = HVACMode.HEAT
        self.temperature: float = 21.0
        self.current_temperature: float = 20.0
        self.min_temp: float = 7.0
        self.max_temp: float = 35.0
        self.step: float = 1.0
        self.online: bool = True
        self.echo: bool = True
        self.expose_heat_cool: bool = False
        self.native_range: bool = False
        self.low_temp: float = 18.0
        self.high_temp: float = 24.0
        # Optional extras some coordination scenarios need; all default to
        # "the source does not report this", matching what every existing
        # test has always seen.
        self.hvac_action: str | None = None
        self.fan_only: bool = False
        self.fan_modes: list[str] | None = None
        self.fan_mode: str | None = None
        self.set_temperature_calls: list[dict[str, Any]] = []
        self.set_hvac_mode_calls: list[dict[str, Any]] = []
        self.set_fan_mode_calls: list[dict[str, Any]] = []
        self.register_service()

    def register_service(self) -> None:
        """
        (Re-)register the climate services we answer to.

        The handlers route by entity id through a per-hass registry so
        several fakes can coexist, each answering only for its own source.
        """
        registry: dict[str, FakeThermostat] = self.hass.data.setdefault(
            _FAKE_REGISTRY, {}
        )
        registry[self.entity_id] = self

        def _route(handler: str) -> Callable[[ServiceCall], Awaitable[None]]:
            async def _handle(call: ServiceCall) -> None:
                target = registry.get(str(call.data.get(ATTR_ENTITY_ID)))
                if target is not None:
                    await getattr(target, handler)(call)

            return _handle

        self.hass.services.async_register(
            "climate", "set_temperature", _route("_async_handle_set_temperature")
        )
        self.hass.services.async_register(
            "climate", "set_hvac_mode", _route("_async_handle_set_hvac_mode")
        )
        self.hass.services.async_register(
            "climate", "set_fan_mode", _route("_async_handle_set_fan_mode")
        )

    @property
    def attributes(self) -> dict[str, Any]:
        """Return the attributes the source reports."""
        modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
        if self.fan_only:
            modes.append(HVACMode.FAN_ONLY)
        if self.expose_heat_cool or self.native_range:
            modes.append(HVACMode.HEAT_COOL)
        features = ClimateEntityFeature(0)
        if self.native_range:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        if self.fan_modes:
            features |= ClimateEntityFeature.FAN_MODE
        attrs: dict[str, Any] = {
            ATTR_FRIENDLY_NAME: self.friendly_name,
            ATTR_HVAC_MODES: modes,
            ATTR_MIN_TEMP: self.min_temp,
            ATTR_MAX_TEMP: self.max_temp,
            ATTR_TARGET_TEMP_STEP: self.step,
            ATTR_CURRENT_TEMPERATURE: self.current_temperature,
            ATTR_TEMPERATURE: self.temperature,
            ATTR_SUPPORTED_FEATURES: features,
        }
        if self.native_range:
            attrs[ATTR_TARGET_TEMP_LOW] = self.low_temp
            attrs[ATTR_TARGET_TEMP_HIGH] = self.high_temp
        if self.hvac_action is not None:
            attrs[ATTR_HVAC_ACTION] = self.hvac_action
        if self.fan_modes:
            attrs[ATTR_FAN_MODES] = self.fan_modes
            attrs[ATTR_FAN_MODE] = self.fan_mode or self.fan_modes[0]
        return attrs

    def seed(self) -> None:
        """Create the source entity for the first time."""
        self._push()

    def _push(self) -> None:
        if not self.online:
            self.hass.states.async_set(self.entity_id, STATE_UNAVAILABLE, {})
            return
        self.hass.states.async_set(self.entity_id, self.mode, self.attributes)

    def set_online(self, online: bool) -> None:
        """Flip the source between available and unavailable."""
        self.online = online
        self._push()

    def external_set(  # noqa: PLR0913
        self,
        *,
        mode: HVACMode | None = None,
        temperature: float | None = None,
        current: float | None = None,
        low: float | None = None,
        high: float | None = None,
        action: str | None = None,
    ) -> None:
        """Change the source the way a remote, app, or UI would."""
        if mode is not None:
            self.mode = mode
        if temperature is not None:
            self.temperature = temperature
        if current is not None:
            self.current_temperature = current
        if low is not None:
            self.low_temp = low
        if high is not None:
            self.high_temp = high
        if action is not None:
            self.hvac_action = action
        self._push()

    async def _async_handle_set_temperature(self, call: ServiceCall) -> None:
        """Answer the service call the wrapper sends and echo new state."""
        self.set_temperature_calls.append(dict(call.data))
        # Core's schema requires a setpoint here, mirroring that keeps the
        # fake honest about services the wrapper must not call bare.
        if not any(
            key in call.data
            for key in (ATTR_TEMPERATURE, "target_temp_high", "target_temp_low")
        ):
            msg = (
                "at least one of ['temperature', 'target_temp_high', "
                "'target_temp_low'] was specified, and none were valid"
            )
            raise ServiceValidationError(msg)
        if (mode := call.data.get(ATTR_HVAC_MODE)) is not None:
            self.mode = HVACMode(mode)
        if (temperature := call.data.get(ATTR_TEMPERATURE)) is not None:
            self.temperature = float(temperature)
        if (low := call.data.get(ATTR_TARGET_TEMP_LOW)) is not None:
            self.low_temp = float(low)
        if (high := call.data.get(ATTR_TARGET_TEMP_HIGH)) is not None:
            self.high_temp = float(high)
        if self.echo:
            self._push()

    async def _async_handle_set_hvac_mode(self, call: ServiceCall) -> None:
        """Answer a mode-only command and echo new state."""
        self.set_hvac_mode_calls.append(dict(call.data))
        if (mode := call.data.get(ATTR_HVAC_MODE)) is not None:
            self.mode = HVACMode(mode)
        if self.echo:
            self._push()

    async def _async_handle_set_fan_mode(self, call: ServiceCall) -> None:
        """Answer a fan speed command and echo new state."""
        self.set_fan_mode_calls.append(dict(call.data))
        if (fan := call.data.get(ATTR_FAN_MODE)) is not None:
            self.fan_mode = str(fan)
        if self.echo:
            self._push()


@pytest.fixture
def thermostat(hass: HomeAssistant) -> FakeThermostat:
    """Return a fake single-setpoint source, not yet seeded."""
    return FakeThermostat(hass)


@pytest.fixture
def wrapper_id(hass: HomeAssistant) -> Callable[[], str]:
    """Return a callable that resolves the wrapper's entity id."""

    def _get() -> str:
        entity_id = er.async_get(hass).async_get_entity_id("climate", DOMAIN, ENTRY_ID)
        assert entity_id is not None
        return entity_id

    return _get


@pytest.fixture
def wrapper_entity(
    hass: HomeAssistant, wrapper_id: Callable[[], str]
) -> Callable[[], DualSetpointClimate]:
    """Return a callable that resolves the live wrapper entity."""

    def _get() -> DualSetpointClimate:
        entity_id = wrapper_id()
        platforms = entity_platform.async_get_platforms(hass, DOMAIN)
        for platform in platforms:
            if entity_id in platform.entities:
                entity = platform.entities[entity_id]
                assert isinstance(entity, DualSetpointClimate)
                return entity
        raise AssertionError("wrapper entity not found")

    return _get


@pytest.fixture
def create_wrapper(
    hass: HomeAssistant,
    thermostat: FakeThermostat,
    request: pytest.FixtureRequest,
) -> Callable[..., Awaitable[MockConfigEntry]]:
    """Return a factory that seeds the source and sets up the wrapper."""

    async def _create(
        *,
        min_gap: float = 2.0,
        min_cycle: int = 900,
        seed: bool = True,
        last_state: State | None = None,
        options: dict[str, Any] | None = None,
    ) -> MockConfigEntry:
        if seed:
            thermostat.seed()
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Thermostat",
            entry_id=ENTRY_ID,
            data={CONF_SOURCE_ENTITY: SOURCE_ENTITY},
            options={
                CONF_MIN_GAP: min_gap,
                CONF_MIN_CYCLE_TIME: min_cycle,
                **(options or {}),
            },
        )
        entry.add_to_hass(hass)
        if last_state is not None:
            patcher = patch.object(
                DualSetpointClimate,
                "async_get_last_state",
                new=AsyncMock(return_value=last_state),
            )
            patcher.start()
            request.addfinalizer(patcher.stop)
        # Setting up the climate component registers its real entity
        # dispatcher over our fake's climate.set_temperature handler, and that
        # dispatcher skips our state-only source as "missing". Set up climate
        # first, then (re-)register the fake so every wrapper command,
        # including any issued during entry setup, lands on it.
        assert await async_setup_component(hass, "climate", {})
        thermostat.register_service()
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        thermostat.register_service()
        return entry

    return _create
