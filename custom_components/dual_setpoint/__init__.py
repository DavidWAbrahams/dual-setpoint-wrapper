# Copyright (C) 2026 Dual Setpoint Climate contributors
# SPDX-License-Identifier: Apache-2.0
"""The Dual Setpoint Climate integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er

from .const import CONF_SOURCE_ENTITY, LOGGER

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Dual Setpoint Climate wrapper."""
    source = entry.data[CONF_SOURCE_ENTITY]

    # The wrapper mirrors the source, so there is nothing useful to do until
    # the source itself exists. Raise ConfigEntryNotReady and let HA retry.
    registry = er.async_get(hass)
    if registry.async_get(source) is None and hass.states.get(source) is None:
        msg = f"Source entity {source} is not loaded yet"
        raise ConfigEntryNotReady(msg)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the wrapper when its options change."""
    LOGGER.debug("Reloading %s after an options change", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a wrapper."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
