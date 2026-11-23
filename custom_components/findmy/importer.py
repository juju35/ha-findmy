"""Turn the files a user uploads into Find My network accessories.

Accepted:
- the `.plist` of one accessory (from the `OwnedBeacons` folder of the Find My data exported from a Mac), with the
  optional `.plist` of its key alignment (from `KeyAlignmentRecords`);
- a `.zip` of that exported data: every accessory of `OwnedBeacons`, with its name (from `BeaconNamingRecord`) and its
  alignment (from `KeyAlignmentRecords`) when they are there;
- a `.json` accessory as this integration exports it, or a list of pre-generated private keys;
- one static private key, in base64.

Everything here is pure and synchronous (run it in the executor): no Home Assistant import.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import plistlib
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from findmy import FindMyAccessory, FixedRollingKeyPairAccessory, KeyPair

from .const import KIND_ACCESSORY, KIND_FIXED, KIND_STATIC

# A zip with more is not an export of the Find My data: refused instead of read.
MAX_ZIP_ENTRIES = 5000
MAX_ZIP_SIZE = 50 * 1024 * 1024


class ImportError_(Exception):  # noqa: N801 - "ImportError" is a builtin
    """The file is not something this integration can import; `reason` is a translation key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AccessorySpec:
    """An accessory ready to become a subentry."""

    kind: str
    data: dict[str, Any]
    title: str
    unique_id: str


def _accessory_spec(accessory: FindMyAccessory, name: str | None) -> AccessorySpec:
    if name:
        accessory.name = name
    title = accessory.name or accessory.model or "Accessory"
    unique_id = accessory.identifier or accessory.skn.hex()[:32]
    return AccessorySpec(KIND_ACCESSORY, dict(accessory.to_json()), title, unique_id)


def _read_plist(content: bytes) -> dict[str, Any]:
    try:
        data = plistlib.loads(content)
    except (plistlib.InvalidFileException, ValueError, binascii.Error) as err:
        raise ImportError_("invalid_plist") from err
    if _is_sealed(data):
        raise ImportError_("encrypted_plist")
    if not isinstance(data, dict):
        raise ImportError_("invalid_plist")
    return data


def _is_sealed(data: Any) -> bool:
    """A `.record` file as the Mac stores it: a sealed box (nonce, tag, ciphertext), not the keys."""
    return isinstance(data, bytes) or (
        isinstance(data, list) and bool(data) and all(isinstance(v, bytes) for v in data)
    )


def _is_encrypted(data: dict[str, Any]) -> bool:
    """A record still sealed with the Mac's key, in a box or a dict of raw bytes, not the plain keys."""
    return "privateKey" not in data and any(isinstance(v, bytes) for v in data.values())


def accessory_from_plist(
    device: bytes | dict[str, Any], alignment: bytes | dict[str, Any] | None = None, name: str | None = None
) -> AccessorySpec:
    """An accessory from its OwnedBeacons record, and its KeyAlignmentRecords record if given."""
    device_data = device if isinstance(device, dict) else _read_plist(device)
    if _is_encrypted(device_data):
        raise ImportError_("encrypted_plist")
    alignment_data = alignment if isinstance(alignment, dict) or alignment is None else _read_plist(alignment)
    try:
        accessory = FindMyAccessory.from_plist(device_data, alignment_data or None, name=name)
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        raise ImportError_("not_an_accessory") from err
    return _accessory_spec(accessory, name)


def _zip_records(content: bytes) -> tuple[dict[str, list[tuple[PurePosixPath, dict[str, Any]]]], int]:
    """The plist records of an export, by folder (OwnedBeacons, KeyAlignmentRecords, BeaconNamingRecord).

    Also counts the accessories left sealed (the Mac's `.record` files, not decrypted first).
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as err:
        raise ImportError_("invalid_zip") from err
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_SIZE:
        raise ImportError_("invalid_zip")
    records: dict[str, list[tuple[PurePosixPath, dict[str, Any]]]] = {}
    sealed = 0
    for info in infos:
        path = PurePosixPath(info.filename)
        if info.is_dir() or path.suffix.lower() not in (".plist", ".record") or "__MACOSX" in path.parts:
            continue
        folder = next(
            (part for part in path.parts if part in ("OwnedBeacons", "KeyAlignmentRecords", "BeaconNamingRecord")),
            None,
        )
        if folder is None:
            continue
        try:
            data = plistlib.loads(archive.read(info))
        except (plistlib.InvalidFileException, ValueError):
            sealed += folder == "OwnedBeacons" and path.suffix.lower() == ".record"
            continue
        if isinstance(data, dict):
            records.setdefault(folder, []).append((path, data))
        elif folder == "OwnedBeacons" and _is_sealed(data):
            sealed += 1
    return records, sealed


def accessories_from_zip(content: bytes) -> list[AccessorySpec]:
    """Every accessory of a zipped export of the Find My data, with its name and alignment."""
    records, sealed = _zip_records(content)
    beacons = records.get("OwnedBeacons", [])
    if not beacons:
        raise ImportError_("encrypted_plist" if sealed else "no_accessory_in_zip")

    names: dict[str, str] = {}
    for path, data in records.get("BeaconNamingRecord", []):
        beacon = data.get("associatedBeacon") or (path.parent.name if len(path.parts) > 1 else None)
        if beacon and isinstance(data.get("name"), str) and data["name"].strip():
            emoji = data.get("emoji")
            names[str(beacon)] = f"{emoji} {data['name']}".strip() if isinstance(emoji, str) else data["name"]

    alignments: dict[str, dict[str, Any]] = {}
    for path, data in records.get("KeyAlignmentRecords", []):
        beacon = data.get("beaconIdentifier") or path.stem
        alignments[str(beacon)] = data

    specs: list[AccessorySpec] = []
    encrypted = sealed
    for path, data in beacons:
        if _is_encrypted(data):
            encrypted += 1
            continue
        identifier = str(data.get("identifier") or path.stem)
        try:
            specs.append(accessory_from_plist(data, alignments.get(identifier), names.get(identifier)))
        except ImportError_:
            continue
    if not specs:
        raise ImportError_("encrypted_plist" if encrypted else "no_accessory_in_zip")
    return specs


def accessory_from_json(content: bytes, name: str | None = None) -> AccessorySpec:
    """An accessory exported as JSON: an official accessory, a list of pre-generated keys, or one static key."""
    try:
        data = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ImportError_("invalid_json") from err
    if isinstance(data, list):
        # A bare list of private keys (hex or base64)
        data = {"type": "custom_rolling_key_accessory", "private_keys": data, "name": name, "identifier": None}
    if not isinstance(data, dict):
        raise ImportError_("invalid_json")
    kind = data.get("type")
    try:
        if kind == "accessory":
            return _accessory_spec(FindMyAccessory.from_json(data), name)
        if kind == "custom_rolling_key_accessory":
            keys = [_key_bytes(key) for key in data.get("private_keys") or []]
            if not keys:
                raise ImportError_("invalid_json")
            accessory = FixedRollingKeyPairAccessory(
                private_keys=keys, name=name or data.get("name"), identifier=data.get("identifier")
            )
            first = KeyPair(keys[0])
            unique_id = accessory.identifier or first.hashed_adv_key_b64
            title = accessory.name or f"Accessory {first.hashed_adv_key_b64[:6]}"
            return AccessorySpec(KIND_FIXED, dict(accessory.to_json()), title, unique_id)
        if kind == "keypair":
            key = KeyPair.from_json(data)
            return static_key(key.private_key_b64, name or key.name)
    except ImportError_:
        raise
    except (KeyError, TypeError, ValueError, AssertionError) as err:
        raise ImportError_("invalid_json") from err
    raise ImportError_("invalid_json")


def _key_bytes(value: Any) -> bytes:
    """A private key given as hex or base64."""
    if not isinstance(value, str):
        raise ImportError_("invalid_key")
    value = value.strip()
    try:
        raw = bytes.fromhex(value) if len(value) == 56 else base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as err:
        raise ImportError_("invalid_key") from err
    if len(raw) != 28:
        raise ImportError_("invalid_key")
    return raw


def static_key(private_key: str, name: str | None) -> AccessorySpec:
    """An accessory broadcasting one static key (a DIY tag)."""
    try:
        key = KeyPair(_key_bytes(private_key), name=name)
    except ValueError as err:
        raise ImportError_("invalid_key") from err
    title = name or f"Tag {key.hashed_adv_key_b64[:6]}"
    data = dict(key.to_json())
    data["name"] = title
    return AccessorySpec(KIND_STATIC, data, title, key.hashed_adv_key_b64)


def load_accessory(kind: str, data: dict[str, Any]) -> FindMyAccessory | FixedRollingKeyPairAccessory | KeyPair:
    """The library object of a stored accessory."""
    if kind == KIND_ACCESSORY:
        return FindMyAccessory.from_json(data)
    if kind == KIND_FIXED:
        return FixedRollingKeyPairAccessory.from_json(data)
    if kind == KIND_STATIC:
        return KeyPair.from_json(data)
    raise ValueError(f"Unknown accessory kind {kind}")
