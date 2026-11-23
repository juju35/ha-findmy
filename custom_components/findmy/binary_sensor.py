"""Low battery, charging and Lost Mode of each device; fetch problem of each account."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SOURCE_ICLOUD, SOURCE_NETWORK
from .coordinator import FindMyCoordinator
from .entity import FindMyAccountEntity, FindMyEntity, setup_described_platform
from .models import DeviceState

PARALLEL_UPDATES = 0

# iCloud battery at or below this level counts as low.
LOW_BATTERY = 20


def _battery_low(state: DeviceState) -> bool | None:
    if state.battery_state is not None:
        return state.battery_state in ("low", "critical")
    if state.battery_level is not None:
        return state.battery_level <= LOW_BATTERY
    return None


@dataclass(frozen=True, kw_only=True)
class FindMyBinarySensorDescription(BinarySensorEntityDescription):
    """A binary sensor of a device, for the devices of the sources it lists."""

    value_fn: Callable[[DeviceState], bool | None]
    sources: tuple[str, ...] = (SOURCE_ICLOUD, SOURCE_NETWORK)


BINARY_SENSORS: tuple[FindMyBinarySensorDescription, ...] = (
    FindMyBinarySensorDescription(
        key="battery_low",
        translation_key="battery_low",
        device_class=BinarySensorDeviceClass.BATTERY,
        value_fn=_battery_low,
    ),
    FindMyBinarySensorDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda s: s.charging,
        sources=(SOURCE_ICLOUD,),
    ),
    FindMyBinarySensorDescription(
        key="lost",
        translation_key="lost",
        value_fn=lambda s: s.lost_mode,
        sources=(SOURCE_ICLOUD,),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    setup_described_platform(entry, async_add_entities, FindMyBinarySensor, BINARY_SENSORS, FetchProblemSensor)


class FindMyBinarySensor(FindMyEntity, BinarySensorEntity):
    """A flag of a device."""

    entity_description: FindMyBinarySensorDescription

    def __init__(
        self, coordinator: FindMyCoordinator, device_id: str, description: FindMyBinarySensorDescription
    ) -> None:
        super().__init__(coordinator, device_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.state_data)


class FetchProblemSensor(FindMyAccountEntity, BinarySensorEntity):
    """On while the last request to Apple failed."""

    _attr_translation_key = "fetch_problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: FindMyCoordinator) -> None:
        super().__init__(coordinator, "fetch_problem")

    @property
    def is_on(self) -> bool | None:
        ok = self.coordinator.last_fetch_ok
        return None if ok is None else not ok
