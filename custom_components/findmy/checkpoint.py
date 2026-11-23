"""Key-chain checkpoints of official accessories, so that a restart does not derive every key since pairing again.

An accessory derives its secret for 15-minute slot N by applying a KDF N times to the secret it got at pairing; an
AirTag paired three years ago is past slot 100 000. The library keeps the secrets it has derived in memory only, and
its JSON export holds the pairing secret: without a checkpoint, every restart walks the whole chain again before
the first fetch. Here the furthest secret derived is saved with its index, and put back in the generator's cache
when the accessory is loaded.

This reads private attributes of the library's key generator; any mismatch (another library version) is caught and
the checkpoint simply ignored: keys are then derived from the pairing secret, slower but correct.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from findmy import FindMyAccessory

_LOGGER = logging.getLogger(__name__)

_GENERATORS = ("_primary_gen", "_secondary_gen")


def _fingerprint(accessory: FindMyAccessory) -> str:
    """Identifies the key chain, so that a checkpoint is never applied to another accessory."""
    return hashlib.sha256(accessory.master_key + accessory.skn + accessory.sks).hexdigest()[:16]


def export_checkpoint(accessory: FindMyAccessory) -> dict[str, Any] | None:
    """The furthest derived secret of each chain (primary, secondary), or None when nothing was derived yet."""
    try:
        chains: dict[str, list[Any]] = {}
        for attr in _GENERATORS:
            gen = getattr(accessory, attr)
            best: tuple[int, bytes] | None = None
            for cache in gen._sk_caches:
                for index, secret in cache.items():
                    if best is None or index > best[0]:
                        best = (index, secret)
            if best is not None and best[0] > 0:
                chains[attr] = [best[0], best[1].hex()]
    except (AttributeError, TypeError) as err:
        _LOGGER.debug("Cannot export a key checkpoint: %s", err)
        return None
    if not chains:
        return None
    return {"fingerprint": _fingerprint(accessory), "chains": chains}


def restore_checkpoint(accessory: FindMyAccessory, checkpoint: dict[str, Any] | None) -> bool:
    """Put a saved checkpoint back into the generators' caches; False when it does not apply."""
    if not checkpoint or checkpoint.get("fingerprint") != _fingerprint(accessory):
        return False
    try:
        for attr, (raw_index, secret_hex) in checkpoint.get("chains", {}).items():
            if attr not in _GENERATORS:
                continue
            index, secret = int(raw_index), bytes.fromhex(secret_hex)
            if index <= 0 or len(secret) != 32:
                continue
            gen = getattr(accessory, attr)
            # The last tier is the unbounded one: the checkpoint is never evicted from it.
            cache, indices = gen._sk_caches[-1], gen._cache_indices[-1]
            if index not in cache:
                cache[index] = secret
                indices.append(index)
                indices.sort()
    except (AttributeError, TypeError, ValueError, IndexError) as err:
        _LOGGER.debug("Cannot restore a key checkpoint: %s", err)
        return False
    return True
