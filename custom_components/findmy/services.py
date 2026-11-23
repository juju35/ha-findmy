"""Integration-wide actions (the device actions are entity services of device_tracker)."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_REFRESH

SCHEMA_REFRESH = vol.Schema({vol.Optional("config_entry_id"): cv.string})


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register `findmy.refresh`."""

    async def _refresh(call: ServiceCall) -> None:
        entry_id = call.data.get("config_entry_id")
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED and (entry_id is None or entry.entry_id == entry_id)
        ]
        if entry_id and not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
                translation_placeholders={"entry": entry_id},
            )
        for entry in entries:
            try:
                await entry.runtime_data.async_manual_refresh()
            except ServiceValidationError:
                # Refreshed a moment ago: reported when that account was asked for, skipped among all of them.
                if entry_id:
                    raise

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, _refresh, schema=SCHEMA_REFRESH)
