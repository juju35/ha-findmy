"""Position of each device and accessory."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_LAST_SEEN,
    ATTR_SOURCE,
    DOMAIN,
    SERVICE_DISPLAY_MESSAGE,
    SERVICE_LOST_MODE,
    SERVICE_PLAY_SOUND,
)
from .coordinator import FindMyCoordinator, ICloudCoordinator
from .entity import FindMyEntity, add_device_entities
from .icloud import ICloudError

PARALLEL_UPDATES = 0

DEFAULT_SUBJECT = "Find My"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator: FindMyCoordinator = entry.runtime_data
    add_device_entities(
        entry, coordinator, async_add_entities, lambda state: [FindMyTracker(coordinator, state.device_id)]
    )

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_PLAY_SOUND,
        {vol.Optional("subject", default=DEFAULT_SUBJECT): cv.string},
        "async_play_sound",
    )
    platform.async_register_entity_service(
        SERVICE_DISPLAY_MESSAGE,
        {
            vol.Required("message"): cv.string,
            vol.Optional("subject", default=DEFAULT_SUBJECT): cv.string,
            vol.Optional("sound", default=False): cv.boolean,
        },
        "async_display_message",
    )
    platform.async_register_entity_service(
        SERVICE_LOST_MODE,
        {
            vol.Required("phone_number"): cv.string,
            vol.Optional("message", default=""): cv.string,
        },
        "async_lost_mode",
    )


class FindMyTracker(FindMyEntity, TrackerEntity):
    """Where the device was last located."""

    _attr_name = None
    _attr_translation_key = "location"

    def __init__(self, coordinator: FindMyCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id, None)

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        return self.state_data.latitude

    @property
    def longitude(self) -> float | None:
        return self.state_data.longitude

    @property
    def location_accuracy(self) -> float:
        return self.state_data.accuracy or 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.state_data
        attributes: dict[str, Any] = {ATTR_SOURCE: state.source, ATTR_LAST_SEEN: state.last_seen}
        if state.altitude is not None:
            attributes["altitude"] = state.altitude
        if state.position_type:
            attributes["position_type"] = state.position_type
        return attributes

    # Actions (iCloud devices only) -----------------------------------------------------------------------------

    def _icloud(self, capability: bool, action: str) -> ICloudCoordinator:
        if not isinstance(self.coordinator, ICloudCoordinator):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="icloud_only",
                translation_placeholders={"entity": self.entity_id},
            )
        if not capability:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_supported",
                translation_placeholders={"entity": self.entity_id, "action": action},
            )
        return self.coordinator

    async def _call(self, coroutine: Any) -> None:
        try:
            await coroutine
        except ICloudError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="action_failed", translation_placeholders={"error": str(err)}
            ) from err

    async def async_play_sound(self, subject: str = DEFAULT_SUBJECT) -> None:
        """Make the device ring."""
        coordinator = self._icloud(self.state_data.can_play_sound, SERVICE_PLAY_SOUND)
        await self._call(coordinator.client.play_sound(self._device_id, subject))

    async def async_display_message(self, message: str, subject: str = DEFAULT_SUBJECT, sound: bool = False) -> None:
        """Show a message on the device."""
        coordinator = self._icloud(self.state_data.can_message, SERVICE_DISPLAY_MESSAGE)
        await self._call(coordinator.client.display_message(self._device_id, subject, message, sound))

    async def async_lost_mode(self, phone_number: str, message: str = "") -> None:
        """Turn on Lost Mode."""
        coordinator = self._icloud(self.state_data.can_lost_mode, SERVICE_LOST_MODE)
        await self._call(coordinator.client.lost_mode(self._device_id, phone_number, message))
        await coordinator.async_request_refresh()
