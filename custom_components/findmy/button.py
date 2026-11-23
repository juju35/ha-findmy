"""Play a sound on an iCloud device; refresh an account now."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, SOURCE_ICLOUD
from .coordinator import FindMyCoordinator, ICloudCoordinator
from .entity import FindMyAccountEntity, FindMyEntity, add_device_entities
from .icloud import ICloudError

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator: FindMyCoordinator = entry.runtime_data
    add_device_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda state: (
            [PlaySoundButton(coordinator, state.device_id)]
            if state.source == SOURCE_ICLOUD and state.can_play_sound
            else []
        ),
    )
    async_add_entities([RefreshButton(coordinator)])


class PlaySoundButton(FindMyEntity, ButtonEntity):
    """Make the device ring, even when it is on silent."""

    _attr_translation_key = "play_sound"

    def __init__(self, coordinator: FindMyCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, "play_sound")

    async def async_press(self) -> None:
        assert isinstance(self.coordinator, ICloudCoordinator)
        try:
            await self.coordinator.client.play_sound(self._device_id, "Find My")
        except ICloudError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="action_failed", translation_placeholders={"error": str(err)}
            ) from err


class RefreshButton(FindMyAccountEntity, ButtonEntity):
    """Ask Apple now instead of waiting for the next refresh."""

    _attr_translation_key = "refresh"

    def __init__(self, coordinator: FindMyCoordinator) -> None:
        super().__init__(coordinator, "refresh")

    async def async_press(self) -> None:
        await self.coordinator.async_manual_refresh()
