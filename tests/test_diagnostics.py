"""Diagnostics redact the account, keys and positions."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.findmy.diagnostics import async_get_config_entry_diagnostics

from .conftest import EMAIL, report


async def test_diagnostics(
    hass: HomeAssistant, network_entry: MockConfigEntry, network_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    network_client.fetch.return_value = {next(iter(network_entry.subentries)): report()}
    network_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(network_entry.entry_id)
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    result = await async_get_config_entry_diagnostics(hass, network_entry)
    text = str(result)
    assert EMAIL not in text
    assert "48.1" not in text
    assert "master_key" not in text
    assert result["accessories"] == [{"kind": "accessory", "model": "AirTag1,1"}]
    assert result["fetch"]["ok"] is True
    assert result["devices"][0]["battery_state"] == "full"
