"""Importing accessories: plist, zip export, JSON, static key."""

from __future__ import annotations

import base64
import io
import json
import plistlib
import secrets
import zipfile
from datetime import datetime

import pytest
from findmy import FindMyAccessory, KeyPair

from custom_components.findmy.const import KIND_ACCESSORY, KIND_FIXED, KIND_STATIC
from custom_components.findmy.importer import (
    ImportError_,
    accessories_from_zip,
    accessory_from_json,
    accessory_from_plist,
    load_accessory,
    static_key,
)

from .conftest import beacon_bytes, beacon_plist

ID_A = "AAAAAAAA-0000-0000-0000-000000000001"
ID_B = "BBBBBBBB-0000-0000-0000-000000000002"


def test_plist_with_alignment() -> None:
    alignment = plistlib.dumps({"lastIndexObserved": 1234, "lastIndexObservationDate": datetime(2026, 1, 1)})
    spec = accessory_from_plist(beacon_bytes(identifier=ID_A), alignment, "Keys")
    assert spec.kind == KIND_ACCESSORY
    assert spec.title == "Keys"
    assert spec.unique_id == ID_A
    assert spec.data["alignment_index"] == 1234
    assert spec.data["serial_number"] == "H0123456789"
    assert isinstance(load_accessory(spec.kind, spec.data), FindMyAccessory)


def test_plist_without_name_uses_model() -> None:
    spec = accessory_from_plist(beacon_bytes(identifier=ID_A))
    assert spec.title == "AirTag1,1"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"not a plist", "invalid_plist"),
        (plistlib.dumps({"encryptedData": b"\x01\x02"}), "encrypted_plist"),
        (plistlib.dumps({"foo": "bar"}), "not_an_accessory"),
        (plistlib.dumps([b"nonce", b"tag", b"ciphertext"], fmt=plistlib.FMT_BINARY), "encrypted_plist"),
        (plistlib.dumps(["a", "b"]), "invalid_plist"),
    ],
)
def test_plist_errors(content: bytes, reason: str) -> None:
    with pytest.raises(ImportError_) as err:
        accessory_from_plist(content)
    assert err.value.reason == reason


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_zip_export_with_names_and_alignment() -> None:
    content = _zip(
        {
            f"FindMy/OwnedBeacons/{ID_A}.plist": beacon_bytes(identifier=ID_A),
            f"FindMy/OwnedBeacons/{ID_B}.plist": beacon_bytes(identifier=ID_B, model="AirPods3,1"),
            f"FindMy/BeaconNamingRecord/{ID_A}/X.plist": plistlib.dumps(
                {"associatedBeacon": ID_A, "name": "Keys", "emoji": "🔑"}
            ),
            f"FindMy/KeyAlignmentRecords/{ID_A}.plist": plistlib.dumps(
                {"beaconIdentifier": ID_A, "lastIndexObserved": 99, "lastIndexObservationDate": datetime(2026, 1, 1)}
            ),
            "__MACOSX/FindMy/OwnedBeacons/._x.plist": b"junk",
            "FindMy/readme.txt": b"hello",
        }
    )
    specs = {spec.unique_id: spec for spec in accessories_from_zip(content)}
    assert set(specs) == {ID_A, ID_B}
    assert specs[ID_A].title == "🔑 Keys"
    assert specs[ID_A].data["alignment_index"] == 99
    assert specs[ID_B].title == "AirPods3,1"


def test_zip_errors() -> None:
    with pytest.raises(ImportError_) as err:
        accessories_from_zip(b"not a zip")
    assert err.value.reason == "invalid_zip"
    with pytest.raises(ImportError_) as err:
        accessories_from_zip(_zip({"a.txt": b"x"}))
    assert err.value.reason == "no_accessory_in_zip"
    with pytest.raises(ImportError_) as err:
        accessories_from_zip(_zip({"OwnedBeacons/a.plist": plistlib.dumps({"encryptedData": b"\x01"})}))
    assert err.value.reason == "encrypted_plist"


SEALED = plistlib.dumps([b"nonce", b"tag", b"ciphertext"], fmt=plistlib.FMT_BINARY)


@pytest.mark.parametrize("record", [SEALED, b"\x00not a plist"])
def test_zip_of_mac_records_is_encrypted(record: bytes) -> None:
    """A zip of the Mac's `.record` files, not decrypted first, says so instead of finding nothing."""
    content = _zip(
        {
            f"Storage/OwnedBeacons/{ID_A}.record": record,
            f"Storage/OwnedBeacons/{ID_B}.record": record,
            f"Storage/KeyAlignmentRecords/{ID_A}.record": record,
        }
    )
    with pytest.raises(ImportError_) as err:
        accessories_from_zip(content)
    assert err.value.reason == "encrypted_plist"


def test_zip_mixing_decrypted_and_sealed_records() -> None:
    content = _zip(
        {
            f"OwnedBeacons/{ID_A}.plist": beacon_bytes(identifier=ID_A),
            f"OwnedBeacons/{ID_B}.record": SEALED,
        }
    )
    assert [spec.unique_id for spec in accessories_from_zip(content)] == [ID_A]


def test_json_accessory_roundtrip() -> None:
    accessory = FindMyAccessory.from_plist(beacon_plist(identifier=ID_A))
    spec = accessory_from_json(json.dumps(accessory.to_json()).encode(), "Bag")
    assert spec.kind == KIND_ACCESSORY
    assert spec.title == "Bag"
    assert spec.unique_id == ID_A


def test_json_key_list() -> None:
    keys = [KeyPair.new() for _ in range(3)]
    spec = accessory_from_json(json.dumps([k.private_key_b64 for k in keys]).encode(), "Bike")
    assert spec.kind == KIND_FIXED
    assert spec.title == "Bike"
    assert spec.unique_id == keys[0].hashed_adv_key_b64
    assert len(load_accessory(spec.kind, spec.data).private_keys) == 3


@pytest.mark.parametrize("content", [b"{", b"42", b'{"type": "other"}', b'{"type": "custom_rolling_key_accessory"}'])
def test_json_errors(content: bytes) -> None:
    with pytest.raises(ImportError_):
        accessory_from_json(content)


def test_static_key_base64_and_hex() -> None:
    key = KeyPair.new()
    spec = static_key(key.private_key_b64, "Tag")
    assert spec.kind == KIND_STATIC
    assert spec.unique_id == key.hashed_adv_key_b64
    assert static_key(key.private_key_bytes.hex(), None).unique_id == key.hashed_adv_key_b64
    assert isinstance(load_accessory(spec.kind, spec.data), KeyPair)


@pytest.mark.parametrize("value", ["", "abc", base64.b64encode(secrets.token_bytes(16)).decode()])
def test_static_key_errors(value: str) -> None:
    with pytest.raises(ImportError_) as err:
        static_key(value, None)
    assert err.value.reason == "invalid_key"
