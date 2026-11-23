"""Diagnostics: entry, options, accessories and device states, with keys, tokens and positions redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ACCOUNT, CONF_APPLE_ID, CONF_DATA, CONF_KIND, CONF_PASSWORD

TO_REDACT = {
    CONF_ACCOUNT,
    CONF_APPLE_ID,
    CONF_PASSWORD,
    "latitude",
    "longitude",
    "altitude",
    "serial_number",
    "email",
    "first_name",
    "last_name",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(
            {"title": entry.title, "data": dict(entry.data), "options": dict(entry.options)}, TO_REDACT | {"title"}
        ),
        "accessories": [
            {"kind": sub.data.get(CONF_KIND), "model": (sub.data.get(CONF_DATA) or {}).get("model")}
            for sub in entry.subentries.values()
        ],
        "fetch": {
            "last_fetch": coordinator.last_fetch.isoformat() if coordinator.last_fetch else None,
            "ok": coordinator.last_fetch_ok,
            "duration": coordinator.last_fetch_duration,
            "error": coordinator.last_error,
            "interval_s": coordinator.update_interval.total_seconds() if coordinator.update_interval else None,
        },
        "devices": [
            async_redact_data({k: v for k, v in state.as_dict().items() if k not in ("device_id", "name")}, TO_REDACT)
            for state in (coordinator.data or {}).values()
        ],
    }
