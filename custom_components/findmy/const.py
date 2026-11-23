"""Constants of the Find My integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "findmy"
VERSION: Final = "1.0.1"

# Config entry data
CONF_SOURCE: Final = "source"
SOURCE_ICLOUD: Final = "icloud"
SOURCE_NETWORK: Final = "network"

CONF_APPLE_ID: Final = "apple_id"
CONF_PASSWORD: Final = "password"
CONF_ACCOUNT: Final = "account"  # serialized Find My network account (session, anisette)
CONF_ANISETTE_URL: Final = "anisette_url"

# Options
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_WITH_FAMILY: Final = "with_family"
CONF_LOCATE: Final = "locate"

# Find My network: Apple asks clients not to poll more than every 15 minutes or so; less often lowers the risk
# of the account being throttled, more often brings nothing (accessories upload reports at that pace anyway).
NETWORK_DEFAULT_INTERVAL: Final = 15
NETWORK_MIN_INTERVAL: Final = 15
NETWORK_MAX_INTERVAL: Final = 120

# iCloud: the devices answer a locate request within a minute or two; every 5 minutes is what Apple's own web
# client does while it is open.
ICLOUD_DEFAULT_INTERVAL: Final = 5
ICLOUD_MIN_INTERVAL: Final = 2
ICLOUD_MAX_INTERVAL: Final = 60

# A fetch that takes longer is abandoned (and counted as failed): key derivation of an accessory paired years ago
# can take a while the first time, but never that long once the checkpoint is saved.
NETWORK_FETCH_TIMEOUT: Final = 300

# The first refresh waits this long after the entry is set up, so that a restart does not fire every request at once.
STARTUP_DELAY: Final = timedelta(seconds=10)

# Store: last known state of each device (kept across restarts), alignment and key checkpoints.
STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 30

# Subentries (Find My network accessories)
SUBENTRY_ACCESSORY: Final = "accessory"
CONF_KIND: Final = "kind"
CONF_DATA: Final = "data"
KIND_ACCESSORY: Final = "accessory"  # official accessory (AirTag, AirPods...), keys exported from a Mac
KIND_FIXED: Final = "fixed"  # list of pre-generated keys
KIND_STATIC: Final = "static"  # one static key

# Battery of a Find My network accessory, from the status byte of its reports (two high bits).
BATTERY_STATES: Final = ("full", "medium", "low", "critical")
BATTERY_ESTIMATE: Final = {"full": 100, "medium": 50, "low": 20, "critical": 5}

# iCloud device status codes
ICLOUD_STATUS: Final = {"200": "online", "201": "offline", "203": "pending", "204": "unregistered"}

ATTR_SOURCE: Final = "source"
ATTR_LAST_SEEN: Final = "last_seen"

SERVICE_PLAY_SOUND: Final = "play_sound"
SERVICE_DISPLAY_MESSAGE: Final = "display_message"
SERVICE_LOST_MODE: Final = "lost_mode"
SERVICE_REFRESH: Final = "refresh"
