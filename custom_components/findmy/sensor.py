"""Battery, last seen and status of each device; last fetch of each account."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import BATTERY_STATES, ICLOUD_STATUS, SOURCE_ICLOUD, SOURCE_NETWORK
from .coordinator import FindMyCoordinator
from .entity import FindMyAccountEntity, FindMyEntity, setup_described_platform
from .models import DeviceState

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class FindMySensorDescription(SensorEntityDescription):
    """A sensor of a device, for the devices of the sources it lists."""

    value_fn: Callable[[DeviceState], Any]
    sources: tuple[str, ...] = (SOURCE_ICLOUD, SOURCE_NETWORK)


SENSORS: tuple[FindMySensorDescription, ...] = (
    FindMySensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.battery_level,
        # Accessories only report a coarse level: shown by battery_state and the battery low sensor instead of a
        # made-up percentage.
        sources=(SOURCE_ICLOUD,),
    ),
    FindMySensorDescription(
        key="battery_state",
        translation_key="battery_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(BATTERY_STATES),
        value_fn=lambda s: s.battery_state,
        sources=(SOURCE_NETWORK,),
    ),
    FindMySensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.last_seen_dt,
    ),
    FindMySensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=[*dict.fromkeys(ICLOUD_STATUS.values()), "unknown"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.status,
        sources=(SOURCE_ICLOUD,),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    setup_described_platform(entry, async_add_entities, FindMySensor, SENSORS, LastFetchSensor)


class FindMySensor(FindMyEntity, SensorEntity):
    """A value of a device."""

    entity_description: FindMySensorDescription

    def __init__(self, coordinator: FindMyCoordinator, device_id: str, description: FindMySensorDescription) -> None:
        super().__init__(coordinator, device_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.state_data)


class LastFetchSensor(FindMyAccountEntity, SensorEntity):
    """When the account last asked Apple, how it went and how long it took."""

    _attr_translation_key = "last_fetch"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: FindMyCoordinator) -> None:
        super().__init__(coordinator, "last_fetch")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_fetch

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coordinator = self.coordinator
        return {
            "result": None
            if coordinator.last_fetch_ok is None
            else ("success" if coordinator.last_fetch_ok else "error"),
            "duration": coordinator.last_fetch_duration,
            "error": coordinator.last_error,
            "devices": len(coordinator.data or {}),
        }
