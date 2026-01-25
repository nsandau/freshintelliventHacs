"""The Fresh Intellivent Sky integration."""
from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

from .const import CONF_AUTH_KEY, DOMAIN
from .coordinator import FreshIntelliventCoordinator


class UnableToConnect(HomeAssistantError):
    """Exception to indicate that we can not connect to device."""


AUTHENTICATED_PLATFORMS = [
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

READ_ONLY_PLATFORMS = [
    Platform.SENSOR,
]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:  # pyling: disable=too-many-statements
    """Set up Fresh Intellivent Sky."""
    hass.data.setdefault(DOMAIN, {})
    address = entry.unique_id

    assert address is not None

    ble_device = bluetooth.async_ble_device_from_address(hass, address)

    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find Fresh Intellivent Sky device with address {address}"
        )

    auth_key = entry.data.get(CONF_AUTH_KEY)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    coordinator = FreshIntelliventCoordinator(
        hass=hass,
        config_entry=entry,
        address=address,
        auth_key=auth_key,
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.async_stop_worker()
        raise

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(
        entry,
        READ_ONLY_PLATFORMS if auth_key is None else AUTHENTICATED_PLATFORMS
    )

    return True


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Update when config_entry options update."""
    _LOGGER.debug("Config entry was updated, rerunning setup")
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: FreshIntelliventCoordinator | None = hass.data[DOMAIN].get(
        entry.entry_id
    )
    if coordinator is not None:
        await coordinator.async_stop_worker()
    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, AUTHENTICATED_PLATFORMS
    ):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
