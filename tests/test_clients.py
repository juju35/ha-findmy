"""The iCloud and Find My network clients, with the libraries faked at their edge."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
import requests
from findmy import InvalidCredentialsError, LoginState, UnauthorizedError
from findmy.errors import EmptyResponseError
from homeassistant.core import HomeAssistant
from pyicloud.exceptions import PyiCloud2FARequiredException, PyiCloudFailedLoginException

from custom_components.findmy.const import KIND_ACCESSORY
from custom_components.findmy.icloud import (
    ICloudAuthError,
    ICloudClient,
    ICloudError,
    ICloudInvalidCredentials,
    device_state,
)
from custom_components.findmy.importer import accessory_from_plist
from custom_components.findmy.models import battery_state_from_status
from custom_components.findmy.network import (
    NetworkAuthError,
    NetworkClient,
    NetworkError,
    NetworkInvalidCredentials,
    NetworkSignInFailed,
)

from .conftest import beacon_plist

IPHONE = {
    "id": "abc==",
    "name": "Jane's iPhone",
    "deviceDisplayName": "iPhone 15 Pro",
    "rawDeviceModel": "iPhone16,1",
    "deviceClass": "iPhone",
    "batteryLevel": 0.756,
    "batteryStatus": "Charging",
    "deviceStatus": "200",
    "lostModeCapable": True,
    "lostModeEnabled": False,
    "features": {"SND": True, "MSG": True, "LOC": True},
    "location": {
        "latitude": 48.1173,
        "longitude": -1.6778,
        "horizontalAccuracy": 14.2,
        "timeStamp": 1763632800000,
        "positionType": "GPS",
    },
}


def test_device_state() -> None:
    state = device_state(IPHONE)
    assert state.device_id == "abc=="
    assert state.model == "iPhone16,1"
    assert state.model_name == "iPhone 15 Pro"
    assert state.battery_level == 76
    assert state.charging is True
    assert state.status == "online"
    assert state.latitude == 48.1173
    assert state.accuracy == 14.2
    assert state.last_seen == datetime.fromtimestamp(1763632800, tz=UTC).isoformat()
    assert state.can_play_sound and state.can_message and state.can_lost_mode


def test_device_state_unknowns() -> None:
    state = device_state(
        {
            "id": "x",
            "deviceDisplayName": "AirPods",
            "batteryLevel": 0,
            "batteryStatus": "Unknown",
            "deviceStatus": "999",
        }
    )
    assert state.name == "AirPods"
    assert state.battery_level is None
    assert state.charging is None
    assert state.status == "unknown"
    assert not state.has_position


@pytest.mark.parametrize(("status", "battery"), [(0x00, "full"), (0x40, "medium"), (0x80, "low"), (0xC4, "critical")])
def test_battery_from_status(status: int, battery: str) -> None:
    assert battery_state_from_status(status) == battery


async def test_icloud_client(hass: HomeAssistant, tmp_path: Path) -> None:
    api = MagicMock(requires_2fa=True, requires_2sa=False, is_trusted_session=False, two_factor_delivery_method="sms")
    device = MagicMock(data=IPHONE)
    api.devices.devices = {"abc==": device}
    with patch("custom_components.findmy.icloud.PyiCloudService", return_value=api) as service:
        client = ICloudClient(hass, "jane@example.com", "pw", str(tmp_path), True)
        assert await client.connect() is True
        assert service.call_args.kwargs["authenticate"] is False
        api.authenticate.assert_called_once()
        assert await client.request_code() == "sms"
        api.validate_2fa_code.return_value = True

        def _trusted() -> bool:
            api.requires_2fa = False
            return True

        api.trust_session.side_effect = _trusted
        assert await client.validate_code("123456") is True
        devices = await client.fetch(locate=True)
        api.devices.refresh.assert_called_once_with(locate=True)
        assert devices["abc=="].battery_level == 76

        await client.play_sound("abc==", "Find My")
        device.play_sound.assert_called_once_with(subject="Find My")
        await client.lost_mode("abc==", "+33", "")
        device.lost_device.assert_called_once_with(number="+33")
        await client.close()


async def test_icloud_client_errors(hass: HomeAssistant, tmp_path: Path) -> None:
    client = ICloudClient(hass, "jane@example.com", "pw", str(tmp_path), True)
    with (
        patch("custom_components.findmy.icloud.PyiCloudService", side_effect=PyiCloudFailedLoginException("bad")),
        pytest.raises(ICloudInvalidCredentials),
    ):
        await client.connect()
    with (
        patch(
            "custom_components.findmy.icloud.PyiCloudService",
            side_effect=PyiCloud2FARequiredException("jane", MagicMock()),
        ),
        pytest.raises(ICloudAuthError),
    ):
        await client.connect()
    with pytest.raises(ICloudAuthError):
        await client.fetch(locate=False)
    api = MagicMock(requires_2fa=False, requires_2sa=False)
    api.devices.refresh.side_effect = requests.ConnectionError("down")
    with patch("custom_components.findmy.icloud.PyiCloudService", return_value=api):
        assert await client.connect() is False
    with pytest.raises(ICloudError):
        await client.fetch(locate=False)


def _fake_report(timestamp: datetime) -> MagicMock:
    return MagicMock(
        is_decrypted=True,
        latitude=48.123456789,
        longitude=-1.6,
        horizontal_accuracy=25,
        timestamp=timestamp,
        status=0x40,
        confidence=2,
    )


async def test_network_client() -> None:
    account = MagicMock()
    account.login = AsyncMock(return_value=LoginState.LOGGED_IN)
    account.login_state = LoginState.LOGGED_IN
    account.close = AsyncMock()
    account.to_json.return_value = {"type": "account"}
    reports: dict = {}

    async def fetch_location(devices):
        return {device: reports.get("value") for device in devices}

    account.fetch_location = fetch_location
    spec = accessory_from_plist(beacon_plist(), name="Keys")
    client = NetworkClient("test")
    try:
        with patch("custom_components.findmy.network.AsyncAppleAccount", return_value=account):
            assert await client.login("jane@example.com", "pw", None) is False
        assert await client.fetch(10) == {}  # no accessory
        await client.set_accessories(
            {"sub": (KIND_ACCESSORY, spec.data, {"date": "2026-01-01T00:00:00+00:00", "index": 7}, None)}
        )
        reports["value"] = _fake_report(datetime(2026, 11, 20, tzinfo=UTC))
        result = await client.fetch(10)
        assert result["sub"].latitude == 48.1234568
        assert result["sub"].status == 0x40
        state = await client.accessory_state()
        assert state["sub"]["alignment"]["index"] == 7
        assert await client.export_session() == {"type": "account"}
        reports["value"] = None
        assert await client.fetch(10) == {"sub": None}

        async def unauthorized(devices):
            raise UnauthorizedError("expired")

        account.fetch_location = unauthorized
        with pytest.raises(NetworkAuthError):
            await client.fetch(10)
        account.login_state = LoginState.REQUIRE_2FA
        with pytest.raises(NetworkAuthError):
            await client.fetch(10)
    finally:
        await client.close()
    account.close.assert_awaited()


async def test_network_client_invalid_credentials() -> None:
    account = MagicMock()
    account.login = AsyncMock(side_effect=InvalidCredentialsError("bad"))
    account.close = AsyncMock()
    client = NetworkClient("test")
    try:
        with (
            patch("custom_components.findmy.network.AsyncAppleAccount", return_value=account),
            pytest.raises(NetworkInvalidCredentials),
        ):
            await client.login("jane@example.com", "pw", "http://ani:6969")
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TypeError('can only concatenate str (not "NoneType") to str'), "TypeError: can only concatenate"),
        (KeyError("Status"), "KeyError: 'Status'"),
        (RuntimeError(), "RuntimeError"),
    ],
)
async def test_network_client_unexpected_sign_in_errors(error: Exception, reason: str) -> None:
    """Any other failure of the sign-in (anisette engine, Apple's answer) ends it with its reason, never unhandled."""
    account = MagicMock()
    account.login = AsyncMock(side_effect=error)
    account.close = AsyncMock()
    client = NetworkClient("test")
    try:
        with (
            patch("custom_components.findmy.network.AsyncAppleAccount", return_value=account),
            pytest.raises(NetworkSignInFailed, match=reason.split(":")[0]) as caught,
        ):
            await client.login("jane@example.com", "pw", "http://ani:6969")
        assert str(caught.value).startswith(reason)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ServerDisconnectedError(),
        aiohttp.InvalidURL("ani"),
        EmptyResponseError("empty"),
        aiohttp.ClientResponseError(MagicMock(), (), status=503),
    ],
)
async def test_network_client_transport_errors(error: Exception) -> None:
    """Failures that are not about the account become NetworkError, at sign-in and at fetch."""
    account = MagicMock()
    account.login = AsyncMock(side_effect=error)
    account.close = AsyncMock()
    client = NetworkClient("test")
    try:
        with patch("custom_components.findmy.network.AsyncAppleAccount", return_value=account):
            with pytest.raises(NetworkError) as raised:
                await client.login("jane@example.com", "pw", "http://ani:6969")
            assert not isinstance(raised.value, NetworkAuthError)
            account.login = AsyncMock(return_value=LoginState.LOGGED_IN)
            account.login_state = LoginState.LOGGED_IN
            await client.login("jane@example.com", "pw", None)
        await client.set_accessories({"sub": (KIND_ACCESSORY, accessory_from_plist(beacon_plist()).data, None, None)})

        async def failing(devices):
            raise error

        account.fetch_location = failing
        with pytest.raises(NetworkError) as raised:
            await client.fetch(10)
        assert not isinstance(raised.value, NetworkAuthError)
    finally:
        await client.close()


async def test_network_client_half_signed_in() -> None:
    account = MagicMock()
    account.login = AsyncMock(return_value=LoginState.AUTHENTICATED)
    account.close = AsyncMock()
    client = NetworkClient("test")
    try:
        with (
            patch("custom_components.findmy.network.AsyncAppleAccount", return_value=account),
            pytest.raises(NetworkError, match="Unexpected sign-in state"),
        ):
            await client.login("jane@example.com", "pw", None)
    finally:
        await client.close()


async def test_icloud_actions_need_a_session(hass: HomeAssistant, tmp_path: Path) -> None:
    client = ICloudClient(hass, "jane@example.com", "pw", str(tmp_path), True)
    with pytest.raises(ICloudError, match="Not signed in"):
        await client.play_sound("abc==", "Find My")
    api = MagicMock(requires_2fa=False, requires_2sa=False)
    api.devices.devices = {}
    with patch("custom_components.findmy.icloud.PyiCloudService", return_value=api):
        await client.connect()
    with pytest.raises(ICloudError, match="no longer listed"):
        await client.lost_mode("gone", "+33", "")
