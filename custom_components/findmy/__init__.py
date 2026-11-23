"""Find My: Apple devices and Find My network accessories in Home Assistant."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import CONF_APPLE_ID, CONF_SOURCE, DOMAIN, SOURCE_ICLOUD, SOURCE_NETWORK, STARTUP_DELAY, STORAGE_VERSION
from .coordinator import FindMyCoordinator, ICloudCoordinator, NetworkCoordinator, cookie_directory
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.DEVICE_TRACKER, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type FindMyConfigEntry = ConfigEntry[FindMyCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: FindMyConfigEntry) -> bool:
    """Restore the account and its devices, then ask Apple a few seconds later."""
    source = entry.data.get(CONF_SOURCE)
    if source == SOURCE_NETWORK:
        coordinator: FindMyCoordinator = NetworkCoordinator(hass, entry)
    elif source == SOURCE_ICLOUD:
        coordinator = ICloudCoordinator(hass, entry)
    else:
        _LOGGER.error(
            "Entry %s was not created by this integration (no source); remove it and add it again", entry.title
        )
        return False

    try:
        await coordinator.async_setup()
    except BaseException:
        await coordinator.async_shutdown()
        raise
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    # Entities are already there with their saved values; the first request waits a little, so that a restart
    # does not send every account's requests at once.
    @callback
    def _first_refresh(_now: datetime) -> None:
        entry.async_create_background_task(hass, coordinator.async_refresh(), "findmy first refresh")

    entry.async_on_unload(async_call_later(hass, STARTUP_DELAY, _first_refresh))
    return True


async def _async_reload(hass: HomeAssistant, entry: FindMyConfigEntry) -> None:
    """Options or accessories changed."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: FindMyConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete what the entry saved: states, keys checkpoints, iCloud session."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    if entry.data.get(CONF_SOURCE) == SOURCE_ICLOUD and (apple_id := entry.data.get(CONF_APPLE_ID)):
        path = cookie_directory(hass, apple_id)
        await hass.async_add_executor_job(shutil.rmtree, path, True)


async def async_remove_config_entry_device(hass: HomeAssistant, entry: FindMyConfigEntry, device: DeviceEntry) -> bool:
    """An iCloud device can be removed (it comes back if iCloud still lists it); accessories go with their subentry."""
    if entry.data.get(CONF_SOURCE) != SOURCE_ICLOUD:
        return False
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        if identifier == entry.entry_id:
            return False
        entry.runtime_data.forget_device(identifier)
    return True
