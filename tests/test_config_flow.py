"""Config flow: iCloud, Find My network, re-authentication, options and accessories."""

from __future__ import annotations

import io
import plistlib
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.findmy.const import (
    CONF_ACCOUNT,
    CONF_APPLE_ID,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE,
    DOMAIN,
    SOURCE_ICLOUD,
    SOURCE_NETWORK,
    SUBENTRY_ACCESSORY,
)
from custom_components.findmy.icloud import ICloudError, ICloudInvalidCredentials
from custom_components.findmy.network import (
    NetworkAnisetteRefused,
    NetworkError,
    NetworkInvalidCredentials,
    NetworkSignInFailed,
    SecondFactor,
)

from .conftest import EMAIL, SESSION, beacon_bytes

ID_A = "AAAAAAAA-0000-0000-0000-000000000001"
ID_B = "BBBBBBBB-0000-0000-0000-000000000002"
# Uploaded file ids are UUIDs.
DEV = "0b9c2f0a-5d6e-4a51-9a59-6d0f3c4f0001"
BAD = "0b9c2f0a-5d6e-4a51-9a59-6d0f3c4f0002"
ZIP = "0b9c2f0a-5d6e-4a51-9a59-6d0f3c4f0003"
JSON_ID = "0b9c2f0a-5d6e-4a51-9a59-6d0f3c4f0004"


@pytest.fixture(autouse=True)
def no_setup() -> Generator[None]:
    """Entries are not set up by these tests."""
    with patch("custom_components.findmy.async_setup_entry", return_value=True):
        yield


@pytest.fixture
def uploads(tmp_path: Path) -> Generator[dict[str, bytes]]:
    """Uploaded files: file id -> content."""
    files: dict[str, bytes] = {}

    @contextmanager
    def _process(hass: HomeAssistant, file_id: str):
        if file_id not in files:
            raise ValueError(f"File {file_id} does not exist")
        path = tmp_path / file_id
        path.write_bytes(files[file_id])
        yield path

    with patch("custom_components.findmy.config_flow.process_uploaded_file", _process):
        yield files


async def _start(hass: HomeAssistant, menu: str) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": menu})


async def test_icloud_with_code(hass: HomeAssistant, icloud_client: MagicMock) -> None:
    icloud_client.connect.return_value = True
    result = await _start(hass, SOURCE_ICLOUD)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_APPLE_ID: EMAIL, CONF_PASSWORD: "pw", "with_family": False}
    )
    assert result["step_id"] == "icloud_code"
    icloud_client.request_code.assert_awaited_once()

    icloud_client.validate_code.return_value = False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
    assert result["errors"] == {"base": "invalid_code"}

    icloud_client.validate_code.return_value = True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": " 123456 "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    icloud_client.validate_code.assert_awaited_with("123456")
    assert result["data"] == {CONF_SOURCE: SOURCE_ICLOUD, CONF_APPLE_ID: EMAIL, CONF_PASSWORD: "pw"}
    assert result["options"]["with_family"] is False
    assert result["result"].unique_id == f"icloud_{EMAIL}"


@pytest.mark.parametrize(
    ("error", "expected"), [(ICloudInvalidCredentials("x"), "invalid_auth"), (ICloudError("x"), "cannot_connect")]
)
async def test_icloud_errors(hass: HomeAssistant, icloud_client: MagicMock, error: Exception, expected: str) -> None:
    icloud_client.connect.side_effect = error
    result = await _start(hass, SOURCE_ICLOUD)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_APPLE_ID: EMAIL, CONF_PASSWORD: "pw"}
    )
    assert result["errors"] == {"base": expected}


async def test_icloud_security_key_refused(hass: HomeAssistant, icloud_client: MagicMock) -> None:
    icloud_client.connect.return_value = True
    icloud_client.request_code.return_value = "security_key"
    result = await _start(hass, SOURCE_ICLOUD)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_APPLE_ID: EMAIL, CONF_PASSWORD: "pw"}
    )
    assert result["errors"] == {"base": "security_key"}


async def test_network_with_method_choice(hass: HomeAssistant, network_client: MagicMock) -> None:
    network_client.login.return_value = True
    result = await _start(hass, SOURCE_NETWORK)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_APPLE_ID: EMAIL, CONF_PASSWORD: "pw", "advanced": {"anisette_url": "http://ani:6969"}},
    )
    network_client.login.assert_awaited_once_with(EMAIL, "pw", "http://ani:6969")
    assert result["step_id"] == "network_method"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"method": "sms_1"})
    network_client.request_code.assert_awaited_once_with(1)
    assert result["step_id"] == "network_sms_code"
    assert result["description_placeholders"] == {"phone": "+33 6 •• •• •• 12"}

    network_client.submit_code.return_value = False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "1"})
    assert result["errors"] == {"base": "invalid_code"}
    network_client.submit_code.return_value = True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCOUNT] == SESSION
    assert result["options"] == {CONF_SCAN_INTERVAL: 15}
    await hass.async_block_till_done()
    network_client.close.assert_awaited()


async def test_network_single_method(hass: HomeAssistant, network_client: MagicMock) -> None:
    network_client.login.return_value = True
    network_client.second_factors.return_value = [SecondFactor(0, "trusted_device", "trusted_device")]
    result = await _start(hass, SOURCE_NETWORK)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_APPLE_ID: EMAIL, CONF_PASSWORD: "pw", "advanced": {}}
    )
    assert result["step_id"] == "network_code"
    network_client.request_code.assert_awaited_once_with(0)


@pytest.mark.parametrize(
    ("error", "anisette", "expected"),
    [
        (NetworkInvalidCredentials(""), None, "invalid_auth"),
        (NetworkInvalidCredentials("Password authentication failed: Your Apple ID was locked."), None, "apple_refused"),
        (NetworkError("x"), None, "cannot_connect"),
        (NetworkError("x"), "http://ani", "anisette_error"),
        (NetworkSignInFailed("MaxRetryError: download failed"), None, "sign_in_failed"),
        (NetworkAnisetteRefused("Device not allowed (-45061)"), None, "anisette_refused"),
    ],
)
async def test_network_errors(
    hass: HomeAssistant, network_client: MagicMock, error: Exception, anisette: str | None, expected: str
) -> None:
    network_client.login.side_effect = error
    result = await _start(hass, SOURCE_NETWORK)
    advanced = {"anisette_url": anisette} if anisette else {}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_APPLE_ID: EMAIL, CONF_PASSWORD: "pw", "advanced": advanced}
    )
    assert result["errors"] == {"base": expected}
    if expected == "apple_refused":
        assert result["description_placeholders"] == {"reason": "Your Apple ID was locked."}
    if expected == "sign_in_failed":
        assert result["description_placeholders"] == {"reason": "MaxRetryError: download failed"}
    if expected == "anisette_refused":
        assert result["description_placeholders"] == {"reason": "Device not allowed (-45061)"}


async def test_duplicate_account(
    hass: HomeAssistant, network_entry: MockConfigEntry, network_client: MagicMock
) -> None:
    network_entry.add_to_hass(hass)
    result = await _start(hass, SOURCE_NETWORK)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_APPLE_ID: EMAIL, CONF_PASSWORD: "pw", "advanced": {}}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_network_reauth(hass: HomeAssistant, network_entry: MockConfigEntry, network_client: MagicMock) -> None:
    network_entry.add_to_hass(hass)
    subentries = dict(network_entry.subentries)
    result = await network_entry.start_reauth_flow(hass)
    assert result["step_id"] == "network"
    new_session = {**SESSION, "login": {"state": 3}}
    network_client.export_session.return_value = new_session
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_APPLE_ID: EMAIL, CONF_PASSWORD: "new", "advanced": {}}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert network_entry.data[CONF_ACCOUNT] == new_session
    assert network_entry.data["session_generation"] == 1
    assert dict(network_entry.subentries) == subentries


async def test_network_reauth_wrong_account(
    hass: HomeAssistant, network_entry: MockConfigEntry, network_client: MagicMock
) -> None:
    network_entry.add_to_hass(hass)
    result = await network_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_APPLE_ID: "other@example.com", CONF_PASSWORD: "x", "advanced": {}}
    )
    assert result["reason"] == "wrong_account"


async def test_icloud_reauth(hass: HomeAssistant, icloud_entry: MockConfigEntry, icloud_client: MagicMock) -> None:
    icloud_entry.add_to_hass(hass)
    result = await icloud_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new"})
    assert result["reason"] == "reauth_successful"
    assert icloud_entry.data[CONF_PASSWORD] == "new"


async def test_options(hass: HomeAssistant, icloud_entry: MockConfigEntry, network_entry: MockConfigEntry) -> None:
    icloud_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(icloud_entry.entry_id)
    assert result["step_id"] == "icloud"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 10, "with_family": False, "locate": False}
    )
    assert icloud_entry.options == {CONF_SCAN_INTERVAL: 10, "with_family": False, "locate": False}

    network_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(network_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SCAN_INTERVAL: 30})
    assert network_entry.options[CONF_SCAN_INTERVAL] == 30


async def _subentry(hass: HomeAssistant, entry: MockConfigEntry, menu: str) -> dict:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_ACCESSORY), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.subentries.async_configure(result["flow_id"], {"next_step_id": menu})


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


async def test_add_plist(hass: HomeAssistant, network_entry: MockConfigEntry, uploads: dict[str, bytes]) -> None:
    network_entry.add_to_hass(hass)
    uploads[DEV] = beacon_bytes(identifier=ID_A)
    result = await _subentry(hass, network_entry, "plist")
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"file": DEV, "name": "Bag"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    sub = next(s for s in network_entry.subentries.values() if s.unique_id == ID_A)
    assert sub.title == "Bag"
    assert sub.data["kind"] == "accessory"

    # The same accessory again
    result = await _subentry(hass, network_entry, "plist")
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"file": DEV})
    assert result["reason"] == "already_configured"


async def test_add_plist_error(hass: HomeAssistant, network_entry: MockConfigEntry, uploads: dict[str, bytes]) -> None:
    network_entry.add_to_hass(hass)
    uploads[BAD] = b"nope"
    result = await _subentry(hass, network_entry, "plist")
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"file": BAD})
    assert result["errors"] == {"base": "invalid_plist"}


async def test_add_archive(hass: HomeAssistant, network_entry: MockConfigEntry, uploads: dict[str, bytes]) -> None:
    network_entry.add_to_hass(hass)
    uploads[ZIP] = _zip(
        {
            f"OwnedBeacons/{ID_A}.plist": beacon_bytes(identifier=ID_A),
            f"OwnedBeacons/{ID_B}.plist": beacon_bytes(identifier=ID_B),
            f"BeaconNamingRecord/{ID_B}/n.plist": plistlib.dumps({"associatedBeacon": ID_B, "name": "Bike"}),
        }
    )
    result = await _subentry(hass, network_entry, "archive")
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"file": ZIP})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    titles = {s.unique_id: s.title for s in network_entry.subentries.values()}
    assert titles[ID_B] == "Bike"
    assert ID_A in titles
    assert len(titles) == 3  # with the accessory of the fixture

    result = await _subentry(hass, network_entry, "archive")
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"file": ZIP})
    assert result["reason"] == "already_configured"


async def test_add_key_and_json(hass: HomeAssistant, network_entry: MockConfigEntry, uploads: dict[str, bytes]) -> None:
    from findmy import KeyPair

    network_entry.add_to_hass(hass)
    key = KeyPair.new()
    result = await _subentry(hass, network_entry, "key")
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"private_key": "bad"})
    assert result["errors"] == {"base": "invalid_key"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"private_key": key.private_key_b64, "name": "DIY"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    uploads[JSON_ID] = f'["{KeyPair.new().private_key_b64}"]'.encode()
    result = await _subentry(hass, network_entry, "json")
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"file": JSON_ID, "name": "Fixed"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {s.data["kind"] for s in network_entry.subentries.values()} == {"accessory", "static", "fixed"}


async def test_no_accessories_for_icloud(hass: HomeAssistant, icloud_entry: MockConfigEntry) -> None:
    icloud_entry.add_to_hass(hass)
    assert icloud_entry.supported_subentry_types == {}


async def test_upload_already_read(
    hass: HomeAssistant, network_entry: MockConfigEntry, uploads: dict[str, bytes]
) -> None:
    """A form sent again after an error points to files that were deleted once read."""
    network_entry.add_to_hass(hass)
    result = await _subentry(hass, network_entry, "plist")
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"file": DEV})
    assert result["errors"] == {"base": "upload_missing"}
