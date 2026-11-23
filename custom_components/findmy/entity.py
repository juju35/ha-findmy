"""Base entities: one Home Assistant device per Apple device or accessory, plus one per account."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SOURCE_NETWORK, VERSION
from .coordinator import FindMyCoordinator
from .models import DeviceState


def device_info(entry: ConfigEntry, state: DeviceState) -> DeviceInfo:
    """The Home Assistant device of an Apple device or accessory."""
    return DeviceInfo(
        identifiers={(DOMAIN, state.device_id)},
        name=state.name,
        manufacturer="Apple",
        model=state.model_name or state.model,
        model_id=state.model if state.model_name and state.model != state.model_name else None,
        serial_number=state.serial_number,
    )


def account_device_info(entry: ConfigEntry) -> DeviceInfo:
    """The device of the account itself (service), holding its diagnostic entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Apple",
        model="Find My network" if entry.data.get("source") == SOURCE_NETWORK else "iCloud",
        entry_type=DeviceEntryType.SERVICE,
        sw_version=VERSION,
    )


class FindMyEntity(CoordinatorEntity[FindMyCoordinator]):
    """An entity of one device; available as long as the device is known (its last values stay)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FindMyCoordinator, device_id: str, key: str | None) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{key}" if key else device_id
        self._attr_device_info = device_info(coordinator.config_entry, self.state_data)

    @property
    def state_data(self) -> DeviceState:
        """The current state of the device (the last known one when Apple has nothing new)."""
        return self.coordinator.data[self._device_id]

    @property
    def available(self) -> bool:
        return self._device_id in (self.coordinator.data or {})


class FindMyAccountEntity(CoordinatorEntity[FindMyCoordinator]):
    """A diagnostic entity of the account."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FindMyCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._attr_device_info = account_device_info(coordinator.config_entry)

    @property
    def available(self) -> bool:
        return True


def described_entities(
    coordinator: FindMyCoordinator, entity_class: Callable[..., Entity], descriptions: Iterable[Any]
) -> Callable[[DeviceState], list[Entity]]:
    """A factory making, for a device, one entity per description that applies to its source."""
    return lambda state: [
        entity_class(coordinator, state.device_id, description)
        for description in descriptions
        if state.source in description.sources
    ]


def add_device_entities(
    entry: ConfigEntry,
    coordinator: FindMyCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[DeviceState], Iterable[Entity]],
) -> None:
    """Create the entities of every device now, and of each new device as it appears.

    Accessories are subentries: their entities are attached to their subentry, so that removing the accessory
    removes them.
    """
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        for device_id, state in (coordinator.data or {}).items():
            if device_id in known:
                continue
            known.add(device_id)
            entities = list(factory(state))
            if not entities:
                continue
            if device_id in entry.subentries:
                async_add_entities(entities, config_subentry_id=device_id)
            else:
                async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


def setup_described_platform(
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entity_class: Callable[..., Entity],
    descriptions: Iterable[Any],
    account_entity: Callable[[FindMyCoordinator], Entity],
) -> None:
    """Set up a platform made of described device entities and one account entity."""
    coordinator: FindMyCoordinator = entry.runtime_data
    add_device_entities(
        entry, coordinator, async_add_entities, described_entities(coordinator, entity_class, descriptions)
    )
    async_add_entities([account_entity(coordinator)])
