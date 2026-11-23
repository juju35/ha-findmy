"""iCloud account: the devices of Find My (iPhone, iPad, Mac, Watch, AirPods), their position and their actions.

The iCloud library is synchronous: every call runs in the executor, one at a time per account (it is not
thread-safe). Its own background refresh thread is kept idle (a refresh interval of days): the coordinator decides
when to ask Apple.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloud2SARequiredException,
    PyiCloudAPIResponseException,
    PyiCloudAuthRequiredException,
    PyiCloudException,
    PyiCloudFailedLoginException,
    PyiCloudNoDevicesException,
    PyiCloudServiceUnavailable,
)
from requests.exceptions import RequestException

from .const import ICLOUD_STATUS, SOURCE_ICLOUD
from .models import DeviceState, icloud_battery

_LOGGER = logging.getLogger(__name__)

# The library's own refresh thread: kept asleep, the coordinator polls.
_IDLE_REFRESH = 7 * 24 * 3600.0
# Timeout of each HTTP request to iCloud (connect, read), in seconds.
REQUEST_TIMEOUT = (15, 60)


class ICloudError(Exception):
    """A request to iCloud failed."""


class ICloudAuthError(ICloudError):
    """iCloud wants the account to sign in again (password changed, session no longer trusted)."""


class ICloudInvalidCredentials(ICloudAuthError):
    """Wrong Apple ID or password."""


class ICloudClient:
    """One Apple ID on iCloud."""

    def __init__(self, hass: HomeAssistant, apple_id: str, password: str, cookie_dir: str, with_family: bool) -> None:
        self._hass = hass
        self._apple_id = apple_id
        self._password = password
        self._cookie_dir = cookie_dir
        self._with_family = with_family
        self._api: PyiCloudService | None = None
        self._lock = asyncio.Lock()

    async def _run[T](self, func: Callable[[], T]) -> T:
        # No timeout here: the thread would keep running after it, without the lock. The library's HTTP session
        # times out its own requests.
        async with self._lock:
            try:
                return await self._hass.async_add_executor_job(func)
            except PyiCloudFailedLoginException as err:
                raise ICloudInvalidCredentials(str(err)) from err
            except (
                PyiCloud2FARequiredException,
                PyiCloud2SARequiredException,
                PyiCloudAuthRequiredException,
            ) as err:
                raise ICloudAuthError(str(err)) from err
            except (PyiCloudServiceUnavailable, PyiCloudAPIResponseException, PyiCloudException) as err:
                raise ICloudError(str(err)) from err
            except RequestException as err:
                raise ICloudError(str(err)) from err

    async def connect(self) -> bool:
        """Sign in (reusing the saved session when it is still trusted); True when a verification code is needed."""

        def _connect() -> bool:
            api = PyiCloudService(
                self._apple_id,
                self._password,
                cookie_directory=self._cookie_dir,
                with_family=self._with_family,
                refresh_interval=_IDLE_REFRESH,
                authenticate=False,
            )
            _set_default_timeout(api)
            api.authenticate()
            self._api = api
            return bool(api.requires_2fa or api.requires_2sa)

        return await self._run(_connect)

    async def request_code(self) -> str:
        """Ask Apple to send the verification code; returns how it is delivered (trusted_device, sms...)."""

        def _request() -> str:
            assert self._api is not None
            self._api.request_2fa_code()
            return self._api.two_factor_delivery_method

        return await self._run(_request)

    async def validate_code(self, code: str) -> bool:
        """Check the verification code and trust the session, so that it is not asked again."""

        def _validate() -> bool:
            assert self._api is not None
            if not self._api.validate_2fa_code(code):
                return False
            if not self._api.is_trusted_session:
                self._api.trust_session()
            return not (self._api.requires_2fa or self._api.requires_2sa)

        return await self._run(_validate)

    async def fetch(self, locate: bool) -> dict[str, DeviceState]:
        """Every device of the account (and of the family when enabled), with its last position."""

        def _fetch() -> dict[str, DeviceState]:
            if self._api is None:
                raise ICloudAuthError("Not signed in")
            if self._api.requires_2fa or self._api.requires_2sa:
                raise ICloudAuthError("Verification code required")
            try:
                manager = self._api.devices
                manager.refresh(locate=locate)
            except PyiCloudNoDevicesException:
                return {}
            return {
                device_id: device_state(device.data)
                for device_id, device in manager.devices.items()
                if isinstance(device.data, dict)
            }

        return await self._run(_fetch)

    def _device(self, device_id: str) -> Any:
        # Before the first sign-in (a few seconds after start-up) or after iCloud ended the session, there is nothing
        # to act on yet; a device can also have left the account since the last fetch.
        if self._api is None or self._api.requires_2fa or self._api.requires_2sa:
            raise ICloudError("Not signed in to iCloud yet")
        try:
            return self._api.devices.devices[device_id]
        except KeyError as err:
            raise ICloudError("This device is no longer listed by iCloud") from err

    async def play_sound(self, device_id: str, subject: str) -> None:
        """Make the device ring."""
        await self._run(lambda: self._device(device_id).play_sound(subject=subject))

    async def display_message(self, device_id: str, subject: str, message: str, sound: bool) -> None:
        """Show a message on the device's screen."""
        await self._run(lambda: self._device(device_id).display_message(subject=subject, message=message, sounds=sound))

    async def lost_mode(self, device_id: str, number: str, message: str) -> None:
        """Turn on Lost Mode: the device locks and shows the message and the number to call."""
        kwargs = {"number": number, **({"text": message} if message else {})}
        await self._run(lambda: self._device(device_id).lost_device(**kwargs))

    async def close(self) -> None:
        """Stop the library's refresh thread."""
        api = self._api
        self._api = None
        if api is not None and api._devices is not None:
            api._devices.stop_event.set()


def _set_default_timeout(api: PyiCloudService) -> None:
    """The library sends its requests without a timeout: a stalled connection would hold the account forever."""
    session = api.session
    original = session.request

    def request(method: str, url: str, *args: Any, timeout: Any = None, **kwargs: Any) -> Any:
        return original(method, url, *args, timeout=timeout or REQUEST_TIMEOUT, **kwargs)

    session.request = request  # type: ignore[method-assign]


def _timestamp(value: Any) -> str | None:
    """iCloud timestamps are milliseconds since the epoch."""
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC).isoformat()


def device_state(content: dict[str, Any]) -> DeviceState:
    """The state of an iCloud device from its Find My record."""
    location = content.get("location") or {}
    features = content.get("features") or {}
    battery_status = content.get("batteryStatus")
    battery = icloud_battery(content.get("batteryLevel"))
    if battery == 0 and battery_status in (None, "Unknown"):
        battery = None
    has_fix = isinstance(location, dict) and location.get("latitude") is not None
    return DeviceState(
        device_id=str(content["id"]),
        name=str(content.get("name") or content.get("deviceDisplayName") or "Apple device"),
        source=SOURCE_ICLOUD,
        model=content.get("rawDeviceModel") or content.get("deviceModel"),
        model_name=content.get("deviceDisplayName"),
        latitude=round(float(location["latitude"]), 7) if has_fix else None,
        longitude=round(float(location["longitude"]), 7) if has_fix else None,
        accuracy=round(float(location.get("horizontalAccuracy") or 0), 1) if has_fix else None,
        altitude=round(float(location["altitude"]), 1) if has_fix and location.get("altitude") else None,
        last_seen=_timestamp(location.get("timeStamp")) if has_fix else None,
        battery_level=battery,
        charging=battery_status in ("Charging", "Charged") if battery_status not in (None, "Unknown") else None,
        status=ICLOUD_STATUS.get(str(content.get("deviceStatus")), "unknown"),
        lost_mode=bool(content.get("lostModeEnabled")),
        can_play_sound=bool(features.get("SND")),
        can_message=bool(features.get("MSG")),
        can_lost_mode=bool(content.get("lostModeCapable")),
        position_type=location.get("positionType") if has_fix else None,
    )
