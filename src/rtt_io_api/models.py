"""Data models for RTT API responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

NAMESPACE_TIMEZONES: dict[str, tzinfo] = {"gb-nr": ZoneInfo("Europe/London")}


def timezone_for_namespace(namespace: str) -> tzinfo:
    """Return the local timezone of a namespace, falling back to UTC if unknown."""
    return NAMESPACE_TIMEZONES.get(namespace, timezone.utc)


def parse_datetime(value: str, default_tz: tzinfo = timezone.utc) -> datetime:
    """Parse an ISO 8601 / RFC 3339 datetime as used by the API.

    Python 3.10's ``fromisoformat`` does not accept a trailing ``Z``, so normalise it first.
    Naive datetimes are interpreted in ``default_tz``: the API omits the offset on times that
    are in the local timezone of the queried location.
    """
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed


@dataclass(frozen=True)
class AccessToken:
    """A short-life access token issued by ``/api/get_access_token``."""

    token: str = field(repr=False)
    valid_until: datetime
    entitlements: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccessToken:
        return cls(
            token=data["token"],
            valid_until=parse_datetime(data["validUntil"]),
            entitlements=tuple(data.get("entitlements") or ()),
        )

    def is_expired(self, leeway: timedelta = timedelta(seconds=30)) -> bool:
        """Return True if the token has expired, or will within ``leeway``."""
        return datetime.now(timezone.utc) + leeway >= self.valid_until

    def has_entitlement(self, name: str) -> bool:
        return name in self.entitlements


@dataclass(frozen=True)
class Stop:
    """A passenger stop (railway station, bus stop, tram stop, etc.) from ``/data/stops``.

    In the ``gb-nr`` namespace, ``short_code`` is the CRS code (e.g. ``CLJ``).
    """

    unique_identity: str
    namespace: str
    description: str
    short_code: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Stop:
        return cls(
            unique_identity=data["uniqueIdentity"],
            namespace=data.get("namespace") or "gb-nr",
            description=data.get("description") or "",
            short_code=data.get("shortCode") or None,
        )


def _optional_datetime(data: dict[str, Any], key: str, tz: tzinfo) -> datetime | None:
    value = data.get(key)
    return parse_datetime(value, tz) if value else None


@dataclass(frozen=True)
class ServiceTimes:
    """Scheduled and realtime times for an arrival or departure at a location."""

    scheduled: datetime | None = None
    expected: datetime | None = None
    actual: datetime | None = None

    @property
    def estimated(self) -> datetime | None:
        """Best estimate of the time: actual, else expected, else scheduled."""
        return self.actual or self.expected or self.scheduled

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, tz: tzinfo = timezone.utc) -> ServiceTimes:
        data = data or {}
        return cls(
            scheduled=_optional_datetime(data, "scheduleAdvertised", tz)
            or _optional_datetime(data, "scheduleInternal", tz),
            expected=_optional_datetime(data, "realtimeForecast", tz)
            or _optional_datetime(data, "realtimeEstimate", tz),
            actual=_optional_datetime(data, "realtimeActual", tz),
        )


@dataclass(frozen=True)
class Service:
    """A train service calling at a location, from ``/rtt/location``."""

    unique_identity: str
    identity: str | None
    departure_date: str | None
    operator_code: str | None
    operator_name: str | None
    origin: tuple[str, ...]
    destination: tuple[str, ...]
    departure: ServiceTimes
    arrival: ServiceTimes
    platform: str | None
    display_as: str | None
    is_cancelled: bool

    @property
    def has_departed(self) -> bool:
        return self.departure.actual is not None

    @classmethod
    def from_dict(cls, data: dict[str, Any], tz: tzinfo = timezone.utc) -> Service:
        schedule = data["scheduleMetadata"]
        temporal = data.get("temporalData") or {}
        platform = ((data.get("locationMetadata") or {}).get("platform")) or {}
        operator = schedule.get("operator") or {}
        departure = temporal.get("departure") or {}
        display_as = temporal.get("displayAs")
        return cls(
            unique_identity=schedule["uniqueIdentity"],
            identity=schedule.get("trainReportingIdentity") or schedule.get("identity"),
            departure_date=schedule.get("departureDate"),
            operator_code=operator.get("code"),
            operator_name=operator.get("name"),
            origin=_location_names(data.get("origin")),
            destination=_location_names(data.get("destination")),
            departure=ServiceTimes.from_dict(departure, tz),
            arrival=ServiceTimes.from_dict(temporal.get("arrival"), tz),
            platform=platform.get("actual") or platform.get("planned"),
            display_as=display_as,
            is_cancelled=display_as == "CANCELLED" or bool(departure.get("isCancelled")),
        )


def _location_names(pairs: list[dict[str, Any]] | None) -> tuple[str, ...]:
    return tuple((pair.get("location") or {}).get("description") or "" for pair in pairs or ())
