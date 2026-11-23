"""Set-up, entities, saved states, re-authentication and actions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.findmy.const import DOMAIN
from custom_components.findmy.icloud import ICloudAuthError, ICloudError
from custom_components.findmy.network import NetworkAuthError, NetworkError

from .conftest import icloud_state, report

TRACKER = "device_tracker.keys"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, freezer: FrozenDateTimeFactory) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _subentry_id(entry: MockConfigEntry) -> str:
    return next(iter(entry.subentries))


async def test_network_accessory(
    hass: HomeAssistant, network_entry: MockConfigEntry, network_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    sub_id = _subentry_id(network_entry)
    network_client.fetch.return_value = {sub_id: report(status=0b1000_0000)}  # battery low
    await _setup(hass, network_entry, freezer)

    assert network_entry.state is ConfigEntryState.LOADED
    network_client.restore.assert_awaited_once()
    specs = network_client.set_accessories.await_args.args[0]
    assert set(specs) == {sub_id}

    state = hass.states.get(TRACKER)
    assert state.attributes["latitude"] == 48.1
    assert state.attributes["longitude"] == -1.6
    assert state.attributes["gps_accuracy"] == 30
    assert state.attributes["source"] == "network"
    assert hass.states.get("sensor.keys_battery_state").state == "low"
    assert hass.states.get("binary_sensor.keys_battery_low").state == "on"
    assert hass.states.get("sensor.keys_last_seen").state == "2026-11-20T09:30:00+00:00"
    # Accessories have no percentage, no ring button.
    assert hass.states.get("sensor.keys_battery") is None
    assert hass.states.get("button.keys_play_sound") is None

    # Entities belong to the accessory's subentry, the device to the account.
    entity = er.async_get(hass).async_get(TRACKER)
    assert entity.config_subentry_id == sub_id
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device.manufacturer == "Apple"
    assert device.model == "AirTag"
    assert device.serial_number == "H0123456789"


async def test_older_report_keeps_position(
    hass: HomeAssistant, network_entry: MockConfigEntry, network_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    sub_id = _subentry_id(network_entry)
    network_client.fetch.return_value = {sub_id: report(lat=1.0)}
    await _setup(hass, network_entry, freezer)
    network_client.fetch.return_value = {sub_id: report(lat=2.0, when=datetime(2026, 11, 19, tzinfo=UTC))}
    await network_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(TRACKER).attributes["latitude"] == 1.0
    network_client.fetch.return_value = {sub_id: None}
    await network_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(TRACKER).attributes["latitude"] == 1.0


async def test_states_survive_a_restart(
    hass: HomeAssistant,
    network_entry: MockConfigEntry,
    network_client: MagicMock,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict,
) -> None:
    sub_id = _subentry_id(network_entry)
    network_client.fetch.return_value = {sub_id: report()}
    network_client.accessory_state.return_value = {
        sub_id: {"alignment": {"date": "2026-11-20T09:30:00+00:00", "index": 5}, "checkpoint": None}
    }
    await _setup(hass, network_entry, freezer)
    await hass.config_entries.async_unload(network_entry.entry_id)
    freezer.tick(timedelta(seconds=40))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    saved = hass_storage[f"{DOMAIN}.{network_entry.entry_id}"]["data"]
    assert saved["devices"][sub_id]["latitude"] == 48.1
    assert saved["accessories"][sub_id]["alignment"]["index"] == 5

    # Next start: the position is there before any request, and the saved alignment is given to the library.
    network_client.fetch.reset_mock()
    network_client.fetch.side_effect = NetworkError("offline")
    assert await hass.config_entries.async_setup(network_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(TRACKER).attributes["latitude"] == 48.1
    specs = network_client.set_accessories.await_args.args[0]
    assert specs[sub_id][2]["index"] == 5
    network_client.fetch.assert_not_awaited()

    # A failed fetch keeps the entities available with their values, and says so.
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(TRACKER).state != "unavailable"
    assert hass.states.get(TRACKER).attributes["latitude"] == 48.1
    problem = next(s for s in hass.states.async_all("binary_sensor") if s.entity_id.endswith("fetch_problem"))
    assert problem.state == "on"


async def test_network_auth_failure_starts_reauth(
    hass: HomeAssistant, network_entry: MockConfigEntry, network_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    network_client.fetch.side_effect = NetworkAuthError("2FA required")
    await _setup(hass, network_entry, freezer)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    assert flows[0]["context"]["entry_id"] == network_entry.entry_id


async def test_unreadable_session_starts_reauth(
    hass: HomeAssistant, network_entry: MockConfigEntry, network_client: MagicMock
) -> None:
    network_client.restore.side_effect = NetworkAuthError("bad")
    network_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(network_entry.entry_id)
    await hass.async_block_till_done()
    assert network_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in hass.config_entries.flow.async_progress())
    network_client.close.assert_awaited()


async def test_icloud_devices_and_actions(
    hass: HomeAssistant, icloud_entry: MockConfigEntry, icloud_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, icloud_entry, freezer)
    icloud_client.connect.assert_awaited_once()
    tracker = "device_tracker.jane_s_iphone"
    assert hass.states.get(tracker).attributes["latitude"] == 48.11
    assert hass.states.get("sensor.jane_s_iphone_battery").state == "76"
    assert hass.states.get("sensor.jane_s_iphone_status").state == "online"
    assert hass.states.get("binary_sensor.jane_s_iphone_lost_mode").state == "off"
    assert hass.states.get("binary_sensor.jane_s_iphone_charging").state == "off"

    await hass.services.async_call("button", "press", {"entity_id": "button.jane_s_iphone_play_sound"}, blocking=True)
    icloud_client.play_sound.assert_awaited_once_with("dev1", "Find My")

    await hass.services.async_call(
        DOMAIN, "display_message", {"entity_id": tracker, "message": "Hi", "sound": True}, blocking=True
    )
    icloud_client.display_message.assert_awaited_once_with("dev1", "Find My", "Hi", True)

    icloud_client.fetch.return_value = {"dev1": icloud_state(lost_mode=True)}
    await hass.services.async_call(
        DOMAIN, "lost_mode", {"entity_id": tracker, "phone_number": "+33600000000"}, blocking=True
    )
    icloud_client.lost_mode.assert_awaited_once_with("dev1", "+33600000000", "")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.jane_s_iphone_lost_mode").state == "on"


async def test_icloud_position_kept_when_none(
    hass: HomeAssistant, icloud_entry: MockConfigEntry, icloud_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, icloud_entry, freezer)
    icloud_client.fetch.return_value = {
        "dev1": icloud_state(latitude=None, longitude=None, accuracy=None, last_seen=None, battery_level=None)
    }
    await icloud_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get("device_tracker.jane_s_iphone")
    assert state.attributes["latitude"] == 48.11
    assert hass.states.get("sensor.jane_s_iphone_battery").state == "76"


async def test_icloud_errors(
    hass: HomeAssistant, icloud_entry: MockConfigEntry, icloud_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    icloud_client.connect.return_value = True  # verification code needed
    await _setup(hass, icloud_entry, freezer)
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in hass.config_entries.flow.async_progress())


async def test_action_errors(
    hass: HomeAssistant,
    icloud_entry: MockConfigEntry,
    icloud_client: MagicMock,
    network_entry: MockConfigEntry,
    network_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    network_client.fetch.return_value = {_subentry_id(network_entry): report()}
    await _setup(hass, icloud_entry, freezer)
    await _setup(hass, network_entry, freezer)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "play_sound", {"entity_id": TRACKER}, blocking=True)

    icloud_client.play_sound.side_effect = ICloudError("nope")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.jane_s_iphone_play_sound"}, blocking=True
        )


async def test_refresh_action_is_throttled(
    hass: HomeAssistant, network_entry: MockConfigEntry, network_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, network_entry, freezer)
    network_client.fetch.reset_mock()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    freezer.tick(timedelta(minutes=6))
    await hass.services.async_call(DOMAIN, "refresh", {"config_entry_id": network_entry.entry_id}, blocking=True)
    network_client.fetch.assert_awaited_once()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "refresh", {"config_entry_id": "nope"}, blocking=True)


async def test_unload_and_remove(
    hass: HomeAssistant,
    icloud_entry: MockConfigEntry,
    icloud_client: MagicMock,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict,
) -> None:
    await _setup(hass, icloud_entry, freezer)
    assert await hass.config_entries.async_unload(icloud_entry.entry_id)
    icloud_client.close.assert_awaited_once()
    await hass.config_entries.async_remove(icloud_entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{icloud_entry.entry_id}" not in hass_storage


async def test_icloud_auth_error_during_refresh(
    hass: HomeAssistant, icloud_entry: MockConfigEntry, icloud_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    await _setup(hass, icloud_entry, freezer)
    icloud_client.fetch.side_effect = ICloudAuthError("expired")
    await icloud_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in hass.config_entries.flow.async_progress())
