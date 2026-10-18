from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from rtt_io_api import RTTAPIError, RTTClient, Service, Stop

SERVICE = {
    "temporalData": {
        "departure": {
            "scheduleAdvertised": "2026-10-18T10:00:00",
            "realtimeForecast": "2026-10-18T10:03:00",
        },
        "displayAs": "CALL",
    },
    "locationMetadata": {"platform": {"planned": "4", "actual": "5"}},
    "scheduleMetadata": {
        "uniqueIdentity": "gb-nr:L01525:2026-10-18",
        "identity": "L01525",
        "trainReportingIdentity": "1L40",
        "departureDate": "2026-10-18",
        "operator": {"code": "SW", "name": "South Western Railway"},
    },
    "origin": [{"location": {"description": "Clapham Junction"}}],
    "destination": [{"location": {"description": "Woking"}}],
}


QUERY = {"location": {"namespace": "gb-nr", "description": "Clapham Junction"}}


def make_client(response: httpx.Response, requests: list[httpx.Request] | None = None) -> RTTClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/get_access_token":
            return httpx.Response(
                200,
                json={"token": "t", "entitlements": [], "validUntil": "2099-01-01T00:00:00Z"},
            )
        if requests is not None:
            requests.append(request)
        return response

    return RTTClient(
        refresh_token="refresh-xyz",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_get_services() -> None:
    requests: list[httpx.Request] = []
    client = make_client(
        httpx.Response(200, json={"query": QUERY, "services": [SERVICE]}), requests
    )

    services = await client.get_services("gb-nr:CLJ", Stop("gb-nr:WOK", "gb-nr", "Woking"))

    assert len(services) == 1
    service = services[0]
    assert isinstance(service, Service)
    assert service.unique_identity == "gb-nr:L01525:2026-10-18"
    assert service.identity == "1L40"
    assert service.operator_name == "South Western Railway"
    assert service.origin == ("Clapham Junction",)
    assert service.destination == ("Woking",)
    london = ZoneInfo("Europe/London")
    assert service.departure.scheduled == datetime(2026, 10, 18, 10, 0, tzinfo=london)
    assert service.departure.expected == datetime(2026, 10, 18, 10, 3, tzinfo=london)
    assert service.departure.scheduled.utcoffset() == timedelta(hours=1)
    assert service.platform == "5"
    assert not service.is_cancelled
    params = requests[0].url.params
    assert requests[0].url.path == "/rtt/location"
    assert params["code"] == "gb-nr:CLJ"
    assert params["filterTo"] == "gb-nr:WOK"


@pytest.mark.parametrize(
    ("kwargs", "start_offset", "length"),
    [
        pytest.param({}, timedelta(0), timedelta(hours=4), id="defaults"),
        pytest.param({"time_offset": 30}, timedelta(minutes=30), timedelta(hours=4), id="offset"),
        pytest.param(
            {"window": timedelta(hours=1), "time_offset": -15},
            timedelta(minutes=-15),
            timedelta(hours=1),
            id="window-and-negative-offset",
        ),
    ],
)
async def test_time_window(
    kwargs: dict[str, Any], start_offset: timedelta, length: timedelta
) -> None:
    requests: list[httpx.Request] = []
    client = make_client(httpx.Response(200, json={"services": []}), requests)
    before = datetime.now(timezone.utc)

    await client.get_services("gb-nr:CLJ", "gb-nr:WOK", **kwargs)

    params = requests[0].url.params
    time_from = datetime.fromisoformat(params["timeFrom"])
    time_to = datetime.fromisoformat(params["timeTo"])
    assert time_to - time_from == length
    assert abs((time_from - before) - start_offset) < timedelta(seconds=5)


async def test_no_services() -> None:
    client = make_client(httpx.Response(204))

    assert await client.get_services("gb-nr:CLJ", "gb-nr:WOK") == []


@pytest.mark.parametrize("window", [timedelta(0), timedelta(hours=24)])
async def test_invalid_window(window: timedelta) -> None:
    client = make_client(httpx.Response(200, json={"services": []}))

    with pytest.raises(ValueError):
        await client.get_services("gb-nr:CLJ", "gb-nr:WOK", window=window)


async def test_malformed_services_response() -> None:
    client = make_client(httpx.Response(200, json={"query": QUERY, "services": [{}]}))

    with pytest.raises(RTTAPIError):
        await client.get_services("gb-nr:CLJ", "gb-nr:WOK")


async def test_winter_times_use_gmt_offset() -> None:
    winter = {
        **SERVICE,
        "temporalData": {"departure": {"scheduleAdvertised": "2026-12-18T10:00:00"}},
    }
    client = make_client(httpx.Response(200, json={"query": QUERY, "services": [winter]}))

    (service,) = await client.get_services("gb-nr:CLJ", "gb-nr:WOK")

    assert service.departure.scheduled is not None
    assert service.departure.scheduled.utcoffset() == timedelta(0)


async def test_explicit_offset_is_respected() -> None:
    explicit = {
        **SERVICE,
        "temporalData": {"departure": {"scheduleAdvertised": "2026-10-18T10:00:00Z"}},
    }
    client = make_client(httpx.Response(200, json={"query": QUERY, "services": [explicit]}))

    (service,) = await client.get_services("gb-nr:CLJ", "gb-nr:WOK")

    assert service.departure.scheduled == datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)


def _service(
    identity: str,
    scheduled: str,
    *,
    expected: str | None = None,
    actual: str | None = None,
    display_as: str = "CALL",
) -> dict[str, Any]:
    departure = {"scheduleAdvertised": f"2026-10-18T{scheduled}:00"}
    if expected:
        departure["realtimeForecast"] = f"2026-10-18T{expected}:00"
    if actual:
        departure["realtimeActual"] = f"2026-10-18T{actual}:00"
    return {
        **SERVICE,
        "temporalData": {"departure": departure, "displayAs": display_as},
        "scheduleMetadata": {**SERVICE["scheduleMetadata"], "uniqueIdentity": identity},
    }


@pytest.mark.parametrize(
    ("services", "expected_identity"),
    [
        pytest.param(
            [_service("late", "10:00", expected="10:30"), _service("on-time", "10:10")],
            "on-time",
            id="ordered-by-expected-time-not-scheduled",
        ),
        pytest.param(
            [_service("departed", "10:00", actual="10:02"), _service("next", "10:10")],
            "next",
            id="departed-ignored",
        ),
        pytest.param(
            [_service("cancelled", "10:00", display_as="CANCELLED"), _service("next", "10:10")],
            "next",
            id="cancelled-ignored",
        ),
        pytest.param(
            [_service("later", "10:20"), _service("sooner", "10:10")],
            "sooner",
            id="unordered-response",
        ),
        pytest.param(
            [
                _service("departed", "10:00", actual="10:02"),
                _service("cancelled", "10:05", display_as="CANCELLED"),
            ],
            None,
            id="nothing-upcoming",
        ),
        pytest.param([], None, id="no-services"),
    ],
)
async def test_get_next_service(
    services: list[dict[str, Any]], expected_identity: str | None
) -> None:
    client = make_client(httpx.Response(200, json={"query": QUERY, "services": services}))

    service = await client.get_next_service("gb-nr:CLJ", "gb-nr:WOK")

    assert (service.unique_identity if service else None) == expected_identity


async def test_get_next_service_after_uses_given_time() -> None:
    requests: list[httpx.Request] = []
    client = make_client(httpx.Response(200, json={"query": QUERY, "services": []}), requests)
    after = datetime(2026, 10, 19, 8, 30, tzinfo=ZoneInfo("Europe/London"))

    assert await client.get_next_service_after("gb-nr:CLJ", "gb-nr:WOK", after) is None

    params = requests[0].url.params
    assert datetime.fromisoformat(params["timeFrom"]) == after
    assert datetime.fromisoformat(params["timeTo"]) == after + timedelta(hours=4)


async def test_get_next_service_after_picks_next_service() -> None:
    services = [_service("departed", "10:00", actual="10:02"), _service("next", "10:10")]
    client = make_client(httpx.Response(200, json={"query": QUERY, "services": services}))
    after = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)

    service = await client.get_next_service_after("gb-nr:CLJ", "gb-nr:WOK", after)

    assert service is not None
    assert service.unique_identity == "next"


async def test_get_next_service_is_get_next_service_after_now() -> None:
    requests: list[httpx.Request] = []
    client = make_client(httpx.Response(200, json={"query": QUERY, "services": []}), requests)
    before = datetime.now(timezone.utc)

    await client.get_next_service("gb-nr:CLJ", "gb-nr:WOK", time_offset=10)

    time_from = datetime.fromisoformat(requests[0].url.params["timeFrom"])
    assert abs((time_from - before) - timedelta(minutes=10)) < timedelta(seconds=5)


async def test_naive_after_is_rejected() -> None:
    client = make_client(httpx.Response(200, json={"query": QUERY, "services": []}))

    with pytest.raises(ValueError, match="timezone-aware"):
        await client.get_next_service_after("gb-nr:CLJ", "gb-nr:WOK", datetime(2026, 10, 18, 9, 0))
