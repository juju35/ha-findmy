"""Coordinators: one per account, attached to its config entry.

Each account has its own coordinator, so an expired session starts the re-authentication of that account's entry.
The last known state of every device is saved and restored: positions and batteries survive restarts and the
periods when Apple has nothing new, and entities never turn unavailable because a fetch failed.
"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers.storage import STORAGE_DIR, Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BATTERY_ESTIMATE,
    CONF_ACCOUNT,
    CONF_APPLE_ID,
    CONF_DATA,
    CONF_KIND,
    CONF_LOCATE,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_WITH_FAMILY,
    DOMAIN,
    ICLOUD_DEFAULT_INTERVAL,
    NETWORK_DEFAULT_INTERVAL,
    NETWORK_FETCH_TIMEOUT,
    SOURCE_NETWORK,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
    SUBENTRY_ACCESSORY,
)
from .icloud import ICloudAuthError, ICloudClient, ICloudError
from .models import DeviceState, battery_state_from_status
from .network import NetworkAuthError, NetworkClient, NetworkError, Report

_LOGGER = logging.getLogger(__name__)

# Saved with the session: the entry data holds the session of the last sign-in (config flow), the Store the session as
# the library last left it. The Store one is used only while it belongs to the same sign-in.
CONF_SESSION_GENERATION = "session_generation"


class FindMyCoordinator(DataUpdateCoordinator[dict[str, DeviceState]]):
    """Shared logic: saved states, fetch status, never-unavailable data."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, interval_minutes: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(minutes=interval_minutes),
            always_update=False,
        )
        self.store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.saved: dict[str, Any] = {}
        self.last_fetch: datetime | None = None
        self.last_fetch_duration: float | None = None
        self.last_fetch_ok: bool | None = None
        self.last_error: str | None = None
        self._closed = False
        self.data = {}

    async def async_load(self) -> None:
        """Restore the saved states, so entities come back at once with their last values."""
        self.saved = await self.store.async_load() or {}
        devices: dict[str, DeviceState] = {}
        for device_id, raw in (self.saved.get("devices") or {}).items():
            try:
                devices[device_id] = DeviceState.from_dict(raw)
            except TypeError:
                _LOGGER.debug("Saved state of %s dropped", device_id)
        self.data = devices

    @callback
    def _save(self, data: dict[str, DeviceState] | None = None) -> None:
        data = self.data if data is None else data
        self.saved["devices"] = {device_id: state.as_dict() for device_id, state in (data or {}).items()}
        self.store.async_delay_save(lambda: self.saved, STORAGE_SAVE_DELAY)

    async def _async_update_data(self) -> dict[str, DeviceState]:
        started = time.monotonic()
        try:
            data = await self._fetch()
        except ConfigEntryAuthFailed as err:
            self._fetch_done(started, False, str(err))
            raise
        except UpdateFailed as err:
            self._fetch_done(started, False, str(err))
            raise
        self._fetch_done(started, True, None)
        self._save(data)
        return data

    def _fetch_done(self, started: float, ok: bool, error: str | None) -> None:
        # Set once the fetch is over, never before: a fetch that timed out is not reported as a success.
        self.last_fetch = datetime.now(UTC)
        self.last_fetch_duration = round(time.monotonic() - started, 1)
        self.last_fetch_ok = ok
        self.last_error = error

    @abstractmethod
    async def _fetch(self) -> dict[str, DeviceState]:
        """Ask Apple; raise ConfigEntryAuthFailed or UpdateFailed."""

    @callback
    def async_set_updated_data(self, data: dict[str, DeviceState]) -> None:
        super().async_set_updated_data(data)
        self._save()

    # A manual refresh closer than this to the last fetch is refused (the Find My network throttles accounts that
    # ask too often).
    manual_refresh_gap: timedelta = timedelta(seconds=30)

    async def async_manual_refresh(self) -> None:
        """Refresh now, unless the last fetch was too recent."""
        if self.last_fetch is not None and datetime.now(UTC) - self.last_fetch < self.manual_refresh_gap:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="refresh_too_soon",
                translation_placeholders={"seconds": str(int(self.manual_refresh_gap.total_seconds()))},
            )
        await self.async_refresh()

    @abstractmethod
    async def async_shutdown(self) -> None:
        """Close the account."""

    @callback
    def forget_device(self, device_id: str) -> None:
        """Drop a device the user removed from Home Assistant."""
        if self.data and device_id in self.data:
            self.data = {key: value for key, value in self.data.items() if key != device_id}
            self._save()


class NetworkCoordinator(FindMyCoordinator):
    """A Find My network account and its accessories (subentries)."""

    manual_refresh_gap = timedelta(minutes=5)

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, entry.options.get(CONF_SCAN_INTERVAL, NETWORK_DEFAULT_INTERVAL))
        libs = Path(hass.config.path(STORAGE_DIR, f"{DOMAIN}_anisette.bin"))
        self.client = NetworkClient(entry.entry_id[:8], libs)

    def accessories(self) -> dict[str, Any]:
        """Subentries of the entry that are accessories."""
        return {
            sub_id: sub
            for sub_id, sub in self.config_entry.subentries.items()
            if sub.subentry_type == SUBENTRY_ACCESSORY
        }

    async def async_setup(self) -> None:
        """Restore the session and load the accessories (no request to Apple)."""
        await self.async_load()
        generation = self.config_entry.data.get(CONF_SESSION_GENERATION, 0)
        session = self.config_entry.data[CONF_ACCOUNT]
        if self.saved.get(CONF_SESSION_GENERATION, -1) == generation and self.saved.get("session"):
            session = self.saved["session"]
        try:
            await self.client.restore(session)
        except NetworkAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

        saved_accessories = self.saved.get("accessories") or {}
        specs = {}
        for sub_id, sub in self.accessories().items():
            saved = saved_accessories.get(sub_id) or {}
            specs[sub_id] = (
                sub.data[CONF_KIND],
                dict(sub.data[CONF_DATA]),
                saved.get("alignment"),
                saved.get("checkpoint"),
            )
        await self.client.set_accessories(specs)

        # A state for each accessory, positionless until its first report.
        data = dict(self.data or {})
        for sub_id, sub in self.accessories().items():
            current = data.get(sub_id)
            raw = sub.data[CONF_DATA]
            base = DeviceState(
                device_id=sub_id,
                name=sub.title,
                source=SOURCE_NETWORK,
                model=raw.get("model"),
                model_name=_model_name(raw.get("model")),
                serial_number=raw.get("serial_number"),
            )
            data[sub_id] = (
                current.merged(
                    name=base.name, model=base.model, model_name=base.model_name, serial_number=base.serial_number
                )
                if current
                else base
            )
        # Accessories that were removed
        self.data = {key: value for key, value in data.items() if key in self.accessories()}

    async def _fetch(self) -> dict[str, DeviceState]:
        if not self.accessories():
            return dict(self.data or {})
        try:
            reports = await self.client.fetch(NETWORK_FETCH_TIMEOUT)
        except NetworkAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TimeoutError as err:
            raise UpdateFailed(f"No answer from Apple within {NETWORK_FETCH_TIMEOUT} s") from err
        except NetworkError as err:
            raise UpdateFailed(str(err)) from err

        data = dict(self.data or {})
        for sub_id, report in reports.items():
            current = data.get(sub_id)
            if current is not None and report is not None:
                data[sub_id] = _apply_report(current, report)

        # What the library learnt (alignment, derived keys, refreshed tokens) is saved for the next start.
        try:
            self.saved["accessories"] = await self.client.accessory_state()
            self.saved["session"] = await self.client.export_session()
            self.saved[CONF_SESSION_GENERATION] = self.config_entry.data.get(CONF_SESSION_GENERATION, 0)
        except (NetworkError, TimeoutError) as err:
            _LOGGER.debug("Account state not saved: %s", err)
        return data

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if not self._closed:
            self._closed = True
            await self.client.close()


def _apply_report(state: DeviceState, report: Report) -> DeviceState:
    """A new report updates the state only when it is newer than the position already known."""
    battery = battery_state_from_status(report.status)
    newer = state.last_seen is None or datetime.fromisoformat(report.timestamp) > datetime.fromisoformat(
        state.last_seen
    )
    if not newer:
        return state.merged(battery_state=battery, battery_level=BATTERY_ESTIMATE[battery])
    return state.merged(
        latitude=report.latitude,
        longitude=report.longitude,
        accuracy=float(report.accuracy),
        last_seen=report.timestamp,
        battery_state=battery,
        battery_level=BATTERY_ESTIMATE[battery],
        confidence=report.confidence,
    )


_MODEL_NAMES = {"AirTag": "AirTag", "AirPods": "AirPods", "Beats": "Beats"}


def _model_name(model: str | None) -> str | None:
    """A readable model from an identifier such as `AirTag1,1`."""
    if not model:
        return None
    for prefix, name in _MODEL_NAMES.items():
        if model.startswith(prefix):
            return name
    return model


class ICloudCoordinator(FindMyCoordinator):
    """An iCloud account and its devices."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, entry.options.get(CONF_SCAN_INTERVAL, ICLOUD_DEFAULT_INTERVAL))
        self.client = ICloudClient(
            hass,
            entry.data[CONF_APPLE_ID],
            entry.data[CONF_PASSWORD],
            cookie_directory(hass, entry.data[CONF_APPLE_ID]),
            entry.options.get(CONF_WITH_FAMILY, True),
        )
        self._connected = False

    async def async_setup(self) -> None:
        """Restore the saved states; the sign-in happens at the first refresh."""
        await self.async_load()

    async def _connect(self) -> None:
        try:
            needs_code = await self.client.connect()
        except ICloudAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ICloudError as err:
            raise UpdateFailed(str(err)) from err
        if needs_code:
            raise ConfigEntryAuthFailed("iCloud asks for a verification code")
        self._connected = True

    async def _fetch(self) -> dict[str, DeviceState]:
        if not self._connected:
            await self._connect()
        try:
            devices = await self.client.fetch(self.config_entry.options.get(CONF_LOCATE, True))
        except ICloudAuthError as err:
            self._connected = False
            raise ConfigEntryAuthFailed(str(err)) from err
        except ICloudError as err:
            raise UpdateFailed(str(err)) from err

        data = dict(self.data or {})
        for device_id, fresh in devices.items():
            current = data.get(device_id)
            data[device_id] = _merge_icloud(current, fresh)
        return data

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if not self._closed:
            self._closed = True
            await self.client.close()


def _merge_icloud(current: DeviceState | None, fresh: DeviceState) -> DeviceState:
    """A device without a position now keeps the last one it had; an unknown battery keeps the last level."""
    if current is None:
        return fresh
    changes: dict[str, Any] = {}
    if not fresh.has_position and current.has_position:
        changes.update(
            latitude=current.latitude,
            longitude=current.longitude,
            accuracy=current.accuracy,
            altitude=current.altitude,
            last_seen=current.last_seen,
            position_type=current.position_type,
        )
    if fresh.battery_level is None and current.battery_level is not None:
        changes["battery_level"] = current.battery_level
    return fresh.merged(**changes) if changes else fresh


def cookie_directory(hass: HomeAssistant, apple_id: str) -> str:
    """Where the iCloud session of an Apple ID is kept (trusted session: no new code at each restart)."""
    safe = "".join(c if c.isalnum() else "_" for c in apple_id.lower())
    return hass.config.path(STORAGE_DIR, DOMAIN, f"icloud_{safe}")
