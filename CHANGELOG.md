# Changelog

## 1.0.0 - 2026-11-23

### Features
- iCloud accounts: iPhone, iPad, Mac, Apple Watch and AirPods (and the family's devices) with their position, battery, charging, status and Lost Mode; **Play sound** button and the `findmy.play_sound`, `findmy.display_message` and `findmy.lost_mode` actions.
- Find My network accounts: AirTags and Find My accessories with their position, battery state and last seen time; accessories are sub-entries of their account, imported from a zip of the exported Find My data (all at once, with names and key alignment), a `.plist`, a `.json` or a static key.
- Verification codes by trusted device or text message; re-authentication of the account as soon as Apple ends its session, accessories kept.
- Positions, batteries, key alignment and key-chain checkpoints saved across restarts; entities never turn unavailable on a failed fetch, a **Fetch problem** sensor says so.
- Find My network fetches run on their own event loop, off Home Assistant's: no freeze while keys are derived and reports decrypted.
- `findmy.refresh` action and **Refresh** button, limited to keep the account clear of Apple's throttling; diagnostics with the account, keys and positions redacted.
- 23 languages.
