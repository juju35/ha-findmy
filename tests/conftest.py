"""Fixtures: fake accessories, fake Find My network and iCloud clients, config entries."""

from __future__ import annotations

import plistlib
import secrets
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.findmy.const import (
    CONF_ACCOUNT,
    CONF_APPLE_ID,
    CONF_DATA,
    CONF_KIND,
    CONF_LOCATE,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE,
    CONF_WITH_FAMILY,
    DOMAIN,
    SOURCE_ICLOUD,
    SOURCE_NETWORK,
    SUBENTRY_ACCESSORY,
)
from custom_components.findmy.importer import accessory_from_plist
from custom_components.findmy.models import DeviceState
from custom_components.findmy.network import Report, SecondFactor

EMAIL = "jane@example.com"
SESSION = {"type": "account", "account": {"username": EMAIL}, "anisette": {"type": "aniLocal"}}


def beacon_plist(identifier: str = "11111111-2222-3333-4444-555555555555", model: str = "AirTag1,1") -> dict[str, Any]:
    """An OwnedBeacons record with random keys (a valid P-224 private key: 28 bytes below the curve order)."""
    private = b"\x00" * 8 + secrets.token_bytes(27) + b"\x01"
    return {
        "privateKey": {"key": {"data": private}},
        "sharedSecret": {"key": {"data": secrets.token_bytes(32)}},
        "secondarySharedSecret": {"key": {"data": secrets.token_bytes(32)}},
        "pairingDate": datetime(2024, 1, 2, 3, 4, 5),
        "model": model,
        "identifier": identifier,
        "stableIdentifier": ["2006~#ABCDEF~#H0123456789"],
    }


def beacon_bytes(**kwargs: Any) -> bytes:
    return plistlib.dumps(beacon_plist(**kwargs), fmt=plistlib.FMT_BINARY)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/findmy."""


@pytest.fixture
def network_client() -> Generator[MagicMock]:
    """The Find My network client, faked: signed in, one report per accessory."""
    client = MagicMock()
    client.login = AsyncMock(return_value=False)
    client.second_factors = AsyncMock(
        return_value=[SecondFactor(0, "trusted_device", "trusted_device"), SecondFactor(1, "sms", "+33 6 •• •• •• 12")]
    )
    client.request_code = AsyncMock()
    client.submit_code = AsyncMock(return_value=True)
    client.restore = AsyncMock()
    client.export_session = AsyncMock(return_value=SESSION)
    client.set_accessories = AsyncMock()
    client.fetch = AsyncMock(return_value={})
    client.accessory_state = AsyncMock(return_value={})
    client.close = AsyncMock()
    with (
        patch("custom_components.findmy.coordinator.NetworkClient", return_value=client),
        patch("custom_components.findmy.config_flow.NetworkClient", return_value=client),
    ):
        yield client


def icloud_state(device_id: str = "dev1", **changes: Any) -> DeviceState:
    """An iPhone as iCloud lists it."""
    base = DeviceState(
        device_id=device_id,
        name="Jane's iPhone",
        source=SOURCE_ICLOUD,
        model="iPhone16,1",
        model_name="iPhone 15 Pro",
        latitude=48.11,
        longitude=-1.68,
        accuracy=12.0,
        last_seen=datetime(2026, 11, 20, 10, 0, tzinfo=UTC).isoformat(),
        battery_level=76,
        charging=False,
        status="online",
        lost_mode=False,
        can_play_sound=True,
        can_message=True,
        can_lost_mode=True,
    )
    return base.merged(**changes)


@pytest.fixture
def icloud_client() -> Generator[MagicMock]:
    """The iCloud client, faked: signed in, one iPhone."""
    client = MagicMock()
    client.connect = AsyncMock(return_value=False)
    client.request_code = AsyncMock(return_value="trusted_device")
    client.validate_code = AsyncMock(return_value=True)
    client.fetch = AsyncMock(return_value={"dev1": icloud_state()})
    client.play_sound = AsyncMock()
    client.display_message = AsyncMock()
    client.lost_mode = AsyncMock()
    client.close = AsyncMock()
    with (
        patch("custom_components.findmy.coordinator.ICloudClient", return_value=client),
        patch("custom_components.findmy.config_flow.ICloudClient", return_value=client),
    ):
        yield client


def accessory_subentry(identifier: str = "11111111-2222-3333-4444-555555555555", name: str = "Keys") -> dict[str, Any]:
    spec = accessory_from_plist(beacon_plist(identifier=identifier), name=name)
    return {
        "data": {CONF_KIND: spec.kind, CONF_DATA: spec.data},
        "subentry_type": SUBENTRY_ACCESSORY,
        "title": spec.title,
        "unique_id": spec.unique_id,
    }


@pytest.fixture
def network_entry() -> MockConfigEntry:
    """A Find My network account with one AirTag."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=f"{SOURCE_NETWORK}_{EMAIL}",
        data={CONF_SOURCE: SOURCE_NETWORK, CONF_ACCOUNT: SESSION, "session_generation": 0},
        options={CONF_SCAN_INTERVAL: 15},
        subentries_data=[accessory_subentry()],
    )


@pytest.fixture
def icloud_entry() -> MockConfigEntry:
    """An iCloud account."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=f"{SOURCE_ICLOUD}_{EMAIL}",
        data={CONF_SOURCE: SOURCE_ICLOUD, CONF_APPLE_ID: EMAIL, CONF_PASSWORD: "secret"},
        options={CONF_SCAN_INTERVAL: 5, CONF_WITH_FAMILY: True, CONF_LOCATE: True},
    )


def report(lat: float = 48.1, lon: float = -1.6, status: int = 0, when: datetime | None = None) -> Report:
    """A decrypted report."""
    when = when or datetime(2026, 11, 20, 9, 30, tzinfo=UTC)
    return Report(latitude=lat, longitude=lon, accuracy=30, timestamp=when.isoformat(), status=status, confidence=3)
