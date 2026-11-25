# Changelog

## 1.0.5 - 2026-11-25

### Fixes
- Find My network accounts: when Apple refuses to register the device of the anisette generated here (the sign-in failed with `KeyError: 'ptm'`), the form says so with Apple's own status and suggests an anisette server; Apple's libraries are no longer downloaded again for nothing.

## 1.0.4 - 2026-11-25

### Fixes
- Find My network accounts: when the anisette generated here fails with the Apple libraries it saved (a damaged file fails every sign-in before Apple is asked), the file is removed and the sign-in tried once more with a fresh download.

## 1.0.3 - 2026-11-25

### Fixes
- Find My network accounts: a sign-in that fails in an unexpected way (the anisette engine that runs here, an answer from Apple that cannot be read) no longer ends the form without a word in the log. The form shows the reason and the error is logged with its traceback.

## 1.0.2 - 2026-11-25

### Fixes
- Find My network accounts: when Apple refuses the sign-in, its own message is shown (and logged) instead of always "Wrong Apple Account or password" — Apple gives that refusal for a device it does not trust, too many attempts or a locked account too, with the right password

## 1.0.1 - 2026-11-23

### Fixes
- Accessories: the Mac's encrypted `.record` files, given alone or in a zip, are recognised and reported as still encrypted instead of being ignored or called invalid
- README: the real location of the Find My data on recent macOS releases, and the decryption step needed before importing

## 1.0.0 - 2026-11-23

### Features
- iCloud accounts: iPhone, iPad, Mac, Apple Watch and AirPods (and the family's devices) with their position, battery, charging, status and Lost Mode; **Play sound** button and the `findmy.play_sound`, `findmy.display_message` and `findmy.lost_mode` actions.
- Find My network accounts: AirTags and Find My accessories with their position, battery state and last seen time; accessories are sub-entries of their account, imported from a zip of the exported Find My data (all at once, with names and key alignment), a `.plist`, a `.json` or a static key.
- Verification codes by trusted device or text message; re-authentication of the account as soon as Apple ends its session, accessories kept.
- Positions, batteries, key alignment and key-chain checkpoints saved across restarts; entities never turn unavailable on a failed fetch, a **Fetch problem** sensor says so.
- Find My network fetches run on their own event loop, off Home Assistant's: no freeze while keys are derived and reports decrypted.
- `findmy.refresh` action and **Refresh** button, limited to keep the account clear of Apple's throttling; diagnostics with the account, keys and positions redacted.
- 23 languages.
