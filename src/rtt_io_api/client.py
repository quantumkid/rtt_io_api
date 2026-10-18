"""HTTP client for the Realtime Trains API."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import TracebackType
from typing import Any

import httpx

from rtt_io_api.exceptions import (
    RTTAPIError,
    RTTAuthenticationError,
    RTTRateLimitError,
)
from rtt_io_api.models import AccessToken, Service, Stop, timezone_for_namespace

DEFAULT_BASE_URL = "https://data.rtt.io"
DEFAULT_TIMEOUT = 30.0
DEFAULT_SERVICES_WINDOW = timedelta(hours=4)
MAX_SERVICES_WINDOW = timedelta(hours=23, minutes=59)


class RTTClient:
    """Async client for the Realtime Trains API.

    ``refresh_token`` is the long-life refresh token issued by Realtime Trains. Data methods
    exchange it for a short-life access token automatically, caching it until it expires.

    ``api_version`` pins the API version (sent as the ``Version`` header, e.g. ``"2025-10-25"``);
    if omitted, the API serves its latest version.

    ``http_client`` lets you supply your own ``httpx.AsyncClient`` (e.g. Home Assistant's shared
    client). It is not closed by :meth:`aclose`, and ``timeout`` is ignored when it is given.
    """

    def __init__(
        self,
        *,
        refresh_token: str,
        base_url: str = DEFAULT_BASE_URL,
        api_version: str | date | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not refresh_token:
            raise ValueError("refresh_token must be a non-empty string")
        self._refresh_token = refresh_token
        self._access_token: AccessToken | None = None
        self._access_token_lock = asyncio.Lock()
        self._stops: list[Stop] | None = None
        self._stops_lock = asyncio.Lock()
        self._api_version = (
            api_version.isoformat() if isinstance(api_version, date) else api_version
        )
        self._base_url = base_url.rstrip("/")
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> RTTClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client, if this instance created it."""
        if self._owns_http_client:
            await self._http.aclose()

    async def get_access_token(self) -> AccessToken:
        """Exchange the client's refresh token for a short-life access token.

        Calls ``GET /api/get_access_token`` using the refresh token as the bearer. The new
        token is also cached and used by subsequent data calls.
        """
        response, data = await self._get_json("/api/get_access_token", bearer=self._refresh_token)
        try:
            self._access_token = AccessToken.from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise RTTAPIError(
                f"Unexpected response from /api/get_access_token: {exc!r}", response
            ) from exc
        return self._access_token

    async def get_stops(self, *, refresh: bool = False) -> list[Stop]:
        """Return every passenger stop available to you.

        Calls ``GET /data/stops`` (API version 2026-04-09 onwards). A stop is any location
        where a service may validly stop for passengers: railway stations, bus stops, tram
        stops, etc.

        The list rarely changes, so it is fetched once and cached on this client; later calls
        return the cached list without contacting the API. Pass ``refresh=True`` to fetch it
        again. Failed requests are not cached. Concurrent first calls share a single request.
        """
        if self._stops is None or refresh:
            async with self._stops_lock:
                # Another task may have filled the cache while we waited for the lock.
                if self._stops is None or refresh:
                    self._stops = await self._fetch_stops()
        # Return a copy so callers can't modify the cache (Stop itself is immutable).
        return list(self._stops)

    async def get_services(
        self,
        from_station: str | Stop,
        to_station: str | Stop,
        *,
        window: timedelta = DEFAULT_SERVICES_WINDOW,
        time_offset: int = 0,
    ) -> list[Service]:
        """Return services that run from ``from_station`` to ``to_station``.

        Calls ``GET /rtt/location`` for ``from_station``, filtered to services subsequently
        calling at ``to_station``. Stations are namespaced codes such as ``"gb-nr:CLJ"``, or
        :class:`Stop` objects from :meth:`get_stops`.

        The time window starts now and lasts ``window`` (default 4 hours, at most 23 hours
        59 minutes). ``time_offset`` shifts both ends of the window by that many minutes
        (negative values look into the past).
        """
        time_from = datetime.now(timezone.utc) + timedelta(minutes=time_offset)
        return await self._get_services_from(from_station, to_station, time_from, window)

    async def get_next_service(
        self,
        from_station: str | Stop,
        to_station: str | Stop,
        *,
        window: timedelta = DEFAULT_SERVICES_WINDOW,
        time_offset: int = 0,
    ) -> Service | None:
        """Return the next service to depart ``from_station`` for ``to_station``.

        Takes the same arguments as :meth:`get_services`. This is
        :meth:`get_next_service_after` with ``after`` set to now (plus ``time_offset``).
        """
        after = datetime.now(timezone.utc) + timedelta(minutes=time_offset)
        return await self.get_next_service_after(from_station, to_station, after, window=window)

    async def get_next_service_after(
        self,
        from_station: str | Stop,
        to_station: str | Stop,
        after: datetime,
        *,
        window: timedelta = DEFAULT_SERVICES_WINDOW,
    ) -> Service | None:
        """Return the next service to depart ``from_station`` for ``to_station`` after a time.

        ``after`` must be timezone-aware. The search window starts at ``after`` and lasts
        ``window``. Cancelled and already departed services are ignored, and the remainder
        are ordered by estimated departure time (the forecast, falling back to the scheduled
        time). Returns ``None`` if there are none in the window.
        """
        services = await self._get_services_from(from_station, to_station, after, window)
        upcoming = [
            service
            for service in services
            if not service.is_cancelled
            and not service.has_departed
            and service.departure.estimated is not None
        ]
        return min(upcoming, key=lambda service: service.departure.estimated, default=None)

    async def _get_services_from(
        self,
        from_station: str | Stop,
        to_station: str | Stop,
        time_from: datetime,
        window: timedelta,
    ) -> list[Service]:
        if time_from.tzinfo is None:
            raise ValueError("time must be timezone-aware")
        if window <= timedelta(0) or window > MAX_SERVICES_WINDOW:
            raise ValueError("window must be greater than zero and at most 23 hours 59 minutes")
        time_from = time_from.astimezone(timezone.utc)
        time_to = time_from + window
        path = "/rtt/location"
        params = {
            "code": _station_code(from_station),
            "filterTo": _station_code(to_station),
            "timeFrom": time_from.isoformat(timespec="seconds"),
            "timeTo": time_to.isoformat(timespec="seconds"),
        }
        response, data = await self._get_json(
            path, bearer=await self._current_access_token(), params=params, allow_empty=True
        )
        items = data.get("services") or ()
        try:
            tz = timezone_for_namespace(data["query"]["location"]["namespace"]) if items else None
            return [Service.from_dict(item, tz) for item in items]
        except (KeyError, TypeError, ValueError) as exc:
            raise RTTAPIError(f"Unexpected response from {path}: {exc!r}", response) from exc

    async def _fetch_stops(self) -> list[Stop]:
        path = "/data/stops"
        response, data = await self._get_json(path, bearer=await self._current_access_token())
        try:
            return [Stop.from_dict(item) for item in data["stops"]]
        except (KeyError, TypeError) as exc:
            raise RTTAPIError(f"Unexpected response from {path}: {exc!r}", response) from exc

    async def _current_access_token(self) -> str:
        token = self._access_token
        if token is None or token.is_expired():
            async with self._access_token_lock:
                # Another task may have renewed the token while we waited for the lock.
                token = self._access_token
                if token is None or token.is_expired():
                    token = await self.get_access_token()
        return token.token

    async def _get_json(
        self,
        path: str,
        *,
        bearer: str,
        params: dict[str, Any] | None = None,
        allow_empty: bool = False,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
        if self._api_version:
            headers["Version"] = self._api_version
        response = await self._http.get(self._base_url + path, params=params, headers=headers)
        _raise_for_status(response)
        if allow_empty and response.status_code == 204:
            return response, {}
        try:
            data = response.json()
        except ValueError as exc:
            raise RTTAPIError(f"Response from {path} was not valid JSON", response) from exc
        if not isinstance(data, dict):
            raise RTTAPIError(f"Expected a JSON object from {path}", response)
        return response, data


def _station_code(station: str | Stop) -> str:
    return station.unique_identity if isinstance(station, Stop) else station


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    status = response.status_code
    message = f"{status} {response.reason_phrase} for {response.request.method} {response.url}"
    body = response.text.strip()
    if body:
        message += f": {body[:500]}"
    if status in (401, 403):
        raise RTTAuthenticationError(message, response)
    if status == 429:
        raise RTTRateLimitError(message, response)
    raise RTTAPIError(message, response)
