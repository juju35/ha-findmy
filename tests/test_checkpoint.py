"""Key-chain checkpoints."""

from __future__ import annotations

import time

from findmy import FindMyAccessory

from custom_components.findmy.checkpoint import export_checkpoint, restore_checkpoint

from .conftest import beacon_plist


def test_checkpoint_roundtrip() -> None:
    data = beacon_plist()
    first = FindMyAccessory.from_plist(data)
    assert export_checkpoint(first) is None  # nothing derived yet
    keys = first.keys_at(3000)
    checkpoint = export_checkpoint(first)
    assert checkpoint is not None
    assert checkpoint["chains"]["_primary_gen"][0] == 3000

    second = FindMyAccessory.from_plist(data)
    assert restore_checkpoint(second, checkpoint)
    started = time.perf_counter()
    assert second.keys_at(3000) == keys
    # From the checkpoint: no walk from index 0.
    assert time.perf_counter() - started < 0.5
    assert second.keys_at(3001) == first.keys_at(3001)


def test_checkpoint_of_another_accessory_is_ignored() -> None:
    first = FindMyAccessory.from_plist(beacon_plist())
    first.keys_at(500)
    other = FindMyAccessory.from_plist(beacon_plist())
    assert not restore_checkpoint(other, export_checkpoint(first))
    assert not restore_checkpoint(other, None)
    assert not restore_checkpoint(other, {"fingerprint": "x"})


def test_corrupt_checkpoint_is_ignored() -> None:
    accessory = FindMyAccessory.from_plist(beacon_plist())
    accessory.keys_at(200)
    checkpoint = export_checkpoint(accessory)
    assert checkpoint is not None
    checkpoint["chains"]["_primary_gen"] = [200, "zz"]
    fresh = FindMyAccessory.from_plist(beacon_plist())
    assert not restore_checkpoint(fresh, checkpoint)
