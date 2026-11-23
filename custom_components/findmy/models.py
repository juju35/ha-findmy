"""What the integration knows of one device: plain values, saved as they are and compared to skip unchanged writes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from typing import Any

from .const import BATTERY_STATES


@dataclass(frozen=True, slots=True)
class DeviceState:
    """Last known state of a device or accessory.

    Every field is JSON-serializable: the whole state is saved in the Store and restored at start-up, so a device keeps
    its last position and battery across restarts and while Apple has nothing new.
    """

    device_id: str
    name: str
    source: str
    model: str | None = None
    model_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    accuracy: float | None = None
    altitude: float | None = None
    last_seen: str | None = None  # ISO 8601, when the position was recorded
    battery_level: int | None = None  # percent (iCloud), or an estimate from battery_state
    battery_state: str | None = None  # full / medium / low / critical (Find My network)
    charging: bool | None = None
    status: str | None = None  # iCloud: online / offline / pending / unregistered
    lost_mode: bool | None = None
    can_play_sound: bool = False
    can_message: bool = False
    can_lost_mode: bool = False
    confidence: int | None = None
    position_type: str | None = None
    serial_number: str | None = None

    @property
    def has_position(self) -> bool:
        """A position was received at least once."""
        return self.latitude is not None and self.longitude is not None

    @property
    def last_seen_dt(self) -> datetime | None:
        """`last_seen` as a datetime."""
        return datetime.fromisoformat(self.last_seen) if self.last_seen else None

    def as_dict(self) -> dict[str, Any]:
        """The state as saved in the Store."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceState:
        """A state read from the Store; unknown keys (from a newer version) are dropped."""
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    def merged(self, **changes: Any) -> DeviceState:
        """A copy with these values."""
        return replace(self, **changes)


def battery_state_from_status(status: int) -> str:
    """Battery level of a Find My network accessory from the status byte of a report (bits 6-7)."""
    return BATTERY_STATES[(status >> 6) & 0b11]


def icloud_battery(level: Any) -> int | None:
    """iCloud reports the battery as a 0-1 float; 0 with an unknown status means unknown."""
    try:
        value = float(level)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return round(min(value, 1.0) * 100)
