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
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_STEP,
    ATTR_TEMPERATURE,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    ATTR_SUPPORTED_FEATURES,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, ServiceCall, State
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


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow our custom integration to load in every test."""


class FakeThermostat:
    """A single-setpoint climate entity simulated with plain state."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Register the climate.set_temperature service we answer to."""
        self.hass = hass
        self.mode: HVACMode = HVACMode.HEAT
        self.temperature: float = 21.0
        self.current_temperature: float = 20.0
        self.min_temp: float = 7.0
        self.max_temp: float = 35.0
        self.step: float = 1.0
        self.online: bool = True
        self.echo: bool = True
        self.set_temperature_calls: list[dict[str, Any]] = []
        self.register_service()

    def register_service(self) -> None:
        """(Re-)register the climate.set_temperature handler we answer to."""
        self.hass.services.async_register(
            "climate", "set_temperature", self._async_handle_set_temperature
        )

    @property
    def attributes(self) -> dict[str, Any]:
        """Return the attributes the source reports."""
        return {
            ATTR_FRIENDLY_NAME: "Thermostat",
            ATTR_HVAC_MODES: [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL],
            ATTR_MIN_TEMP: self.min_temp,
            ATTR_MAX_TEMP: self.max_temp,
            ATTR_TARGET_TEMP_STEP: self.step,
            ATTR_CURRENT_TEMPERATURE: self.current_temperature,
            ATTR_TEMPERATURE: self.temperature,
            ATTR_SUPPORTED_FEATURES: 0,
        }

    def seed(self) -> None:
        """Create the source entity for the first time."""
        self._push()

    def _push(self) -> None:
        if not self.online:
            self.hass.states.async_set(SOURCE_ENTITY, STATE_UNAVAILABLE, {})
            return
        self.hass.states.async_set(SOURCE_ENTITY, self.mode, self.attributes)

    def set_online(self, online: bool) -> None:
        """Flip the source between available and unavailable."""
        self.online = online
        self._push()

    def external_set(
        self,
        *,
        mode: HVACMode | None = None,
        temperature: float | None = None,
        current: float | None = None,
    ) -> None:
        """Change the source the way a remote, app, or UI would."""
        if mode is not None:
            self.mode = mode
        if temperature is not None:
            self.temperature = temperature
        if current is not None:
            self.current_temperature = current
        self._push()

    async def _async_handle_set_temperature(self, call: ServiceCall) -> None:
        """Answer the service call the wrapper sends and echo new state."""
        self.set_temperature_calls.append(dict(call.data))
        if (mode := call.data.get(ATTR_HVAC_MODE)) is not None:
            self.mode = HVACMode(mode)
        if (temperature := call.data.get(ATTR_TEMPERATURE)) is not None:
            self.temperature = float(temperature)
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
    ) -> MockConfigEntry:
        if seed:
            thermostat.seed()
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Thermostat",
            entry_id=ENTRY_ID,
            data={CONF_SOURCE_ENTITY: SOURCE_ENTITY},
            options={CONF_MIN_GAP: min_gap, CONF_MIN_CYCLE_TIME: min_cycle},
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
