# Find My — Agent Context

## Purpose
Home Assistant custom integration (domain `findmy`) for Apple's Find My, with two sources:
- **iCloud** (`source: icloud`): the account's devices (iPhone, iPad, Mac, Watch, AirPods, family devices) from the iCloud Find My service, with play sound / message / Lost Mode;
- **Find My network** (`source: network`): AirTags and accessories, by fetching and decrypting their location reports with their exported keys.

Main rules: **Home Assistant's event loop never does the crypto**, **entities never turn unavailable because a fetch failed**, **an expired session starts the re-authentication of its own entry**.

## Architecture

```
custom_components/findmy/
├── __init__.py      # Entry setup by source: coordinator.async_setup() (restores the Store, no request), platforms, update listener → reload (options, subentries), first refresh STARTUP_DELAY later; async_remove_entry deletes the Store and the iCloud cookie folder; iCloud devices removable (accessories go with their subentry)
├── worker.py        # Worker: a daemon thread with its own asyncio loop; run(factory, timeout) creates the coroutine on that loop and awaits it from the caller's loop (timeout cancels it there)
├── network.py       # NetworkClient: AsyncAppleAccount and accessories live on the worker only; login / second_factors / request_code / submit_code / restore / export_session / set_accessories (saved alignment overrides the imported one, checkpoint restored) / fetch → {subentry_id: Report|None} / accessory_state (alignment + checkpoint); NetworkAuthError for UnauthorizedError/InvalidStateError/not LOGGED_IN
├── checkpoint.py    # export/restore of the furthest derived secret of each key chain (private generator caches, fingerprinted; any mismatch → ignored)
├── icloud.py        # ICloudClient: the iCloud library service in the executor behind a lock, authenticate=False then a default request timeout patched on its session, refresh thread kept idle (refresh_interval of days); device_state() maps an FMIP record to DeviceState
├── importer.py      # Pure parsing of uploads → AccessorySpec(kind, data, title, unique_id): plist (+ alignment), zip export (OwnedBeacons + BeaconNamingRecord + KeyAlignmentRecords), JSON (accessory, fixed key list, keypair), static key; ImportError_.reason = translation key
├── models.py        # DeviceState (frozen, JSON-serializable, saved as is), battery from the status byte (bits 6-7), iCloud battery 0-1 → %
├── coordinator.py   # One DataUpdateCoordinator per entry (config_entry set → ConfigEntryAuthFailed starts its reauth); Store `findmy.<entry_id>`: devices, accessories (alignment, checkpoint), session + session_generation (network); last_fetch/_ok/_duration/_error set after the fetch; async_manual_refresh throttled (5 min network, 30 s iCloud); network: a report only replaces an older position; iCloud: no position / unknown battery keep the last ones
├── config_flow.py   # Menu icloud / network; iCloud: credentials → code (security keys refused); network: credentials (+ anisette URL in a collapsed section) → method (trusted_device / sms_<i>) → code (network_code or network_sms_code); reauth for both (unique_id mismatch → wrong_account), new network session → session_generation + 1; options (interval, family, locate); AccessoryFlow subentries (archive / plist / json / key), a zip adds all its accessories in one go
├── entity.py        # FindMyEntity (unique_id <device_id>_<key>, available while the device is known), FindMyAccountEntity, add_device_entities (new devices as they appear, accessory entities attached to their subentry), described_entities / setup_described_platform
├── device_tracker.py # GPS tracker + entity services play_sound / display_message / lost_mode (iCloud only, capability checked)
├── sensor.py / binary_sensor.py / button.py # battery (iCloud %), battery_state (network enum), last_seen, status; battery_low, charging, lost; play_sound; account last_fetch, fetch_problem, refresh
├── services.py      # findmy.refresh (config_entry_id optional)
├── diagnostics.py   # Redacted entry, accessories kinds/models, fetch status, device states
└── const.py / manifest.json / icons.json / services.yaml / translations/*.json / brand/
```

## Rules
- Never call the Find My network library on Home Assistant's loop: go through NetworkClient (worker). Never touch its objects from another thread.
- Never let a failed fetch blank a value: merge into the previous DeviceState.
- New entity keys go in translations/en.json and icons.json only (entity names are not translated; tests/test_translations.py checks en.json ↔ entities ↔ icons, and that the other languages have the same non-entity keys).
- No reference to other projects in code comments or docs.

## Release
Bump `version` in manifest.json and `VERSION` in const.py, add the CHANGELOG.md section; pushing to main publishes `vX.Y.Z` with `findmy.zip`.

## Tests
`pip install -r requirements_test.txt && pytest`. The clients are faked in tests/conftest.py (`network_client`, `icloud_client`); accessories are built from random keys (`beacon_plist`). The first refresh runs STARTUP_DELAY after setup: tests tick the clock 11 s and wait with `async_block_till_done(wait_background_tasks=True)`. CI also runs the suite on Home Assistant 2026.6.0 (the first with bleak 3.0.2, needed by the Find My library).
