from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from rtt_io_api import RTTAPIError, RTTClient, Stop

STOPS_RESPONSE = {
    "stops": [
        {
            "namespace": "gb-nr",
            "description": "Clapham Junction",
            "shortCode": "CLJ",
            "uniqueIdentity": "gb-nr:CLJ",
        },
        {
            "description": "Some Tram Stop",
            "uniqueIdentity": "gb-nr:TRAM1",
        },
    ]
}


def token_response(token: str, valid_until: str = "2099-01-01T00:00:00Z") -> dict[str, Any]:
    return {"token": token, "entitlements": [], "validUntil": valid_until}


class FakeAPI:
    """Serves the token and stops endpoints, recording requests."""

    def __init__(self, token_valid_until: str = "2099-01-01T00:00:00Z") -> None:
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.stops_served = 0
        self.token_valid_until = token_valid_until

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/get_access_token":
            assert request.headers["Authorization"] == "Bearer refresh-xyz"
            self.tokens_issued += 1
            token = f"access-{self.tokens_issued}"
            return httpx.Response(200, json=token_response(token, self.token_valid_until))
        if request.url.path == "/data/stops":
            self.stops_served += 1
            return httpx.Response(200, json=STOPS_RESPONSE)
        return httpx.Response(404)

    def client(self) -> RTTClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return RTTClient(refresh_token="refresh-xyz", http_client=http)


async def test_get_stops() -> None:
    api = FakeAPI()

    stops = await api.client().get_stops()

    assert stops == [
        Stop(
            unique_identity="gb-nr:CLJ",
            namespace="gb-nr",
            description="Clapham Junction",
            short_code="CLJ",
        ),
        Stop(
            unique_identity="gb-nr:TRAM1",
            namespace="gb-nr",
            description="Some Tram Stop",
            short_code=None,
        ),
    ]
    stops_request = api.requests[-1]
    assert stops_request.url.path == "/data/stops"
    assert stops_request.headers["Authorization"] == "Bearer access-1"


async def test_stops_are_cached() -> None:
    api = FakeAPI()
    client = api.client()

    first = await client.get_stops()
    second = await client.get_stops()

    assert api.stops_served == 1
    assert first == second


async def test_refresh_refetches_stops() -> None:
    api = FakeAPI()
    client = api.client()

    await client.get_stops()
    await client.get_stops(refresh=True)
    await client.get_stops()

    assert api.stops_served == 2


async def test_cached_stops_cannot_be_mutated_by_caller() -> None:
    api = FakeAPI()
    client = api.client()

    (await client.get_stops()).clear()

    assert len(await client.get_stops()) == 2
    assert api.stops_served == 1


async def test_failed_fetch_is_not_cached() -> None:
    responses = iter([httpx.Response(500), httpx.Response(200, json=STOPS_RESPONSE)])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/get_access_token":
            return httpx.Response(200, json=token_response("access-1"))
        return next(responses)

    client = RTTClient(
        refresh_token="refresh-xyz",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RTTAPIError):
        await client.get_stops()
    assert len(await client.get_stops()) == 2


async def test_access_token_is_cached() -> None:
    api = FakeAPI()
    client = api.client()

    await client.get_stops()
    await client.get_stops(refresh=True)

    assert api.tokens_issued == 1
    assert api.requests[-1].headers["Authorization"] == "Bearer access-1"


async def test_expired_access_token_is_renewed() -> None:
    api = FakeAPI(token_valid_until="2000-01-01T00:00:00Z")
    client = api.client()

    await client.get_stops()
    await client.get_stops(refresh=True)

    assert api.tokens_issued == 2
    assert api.requests[-1].headers["Authorization"] == "Bearer access-2"


@pytest.mark.parametrize("body", [{}, {"stops": [{"description": "no id"}]}])
async def test_malformed_stops_response(body: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/get_access_token":
            return httpx.Response(200, json=token_response("access-1"))
        return httpx.Response(200, json=body)

    client = RTTClient(
        refresh_token="refresh-xyz",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RTTAPIError):
        await client.get_stops()


async def test_concurrent_first_calls_share_one_fetch() -> None:
    api = FakeAPI()

    async def slow_api(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)  # let the other task run while this request is in flight
        return api(request)

    client = RTTClient(
        refresh_token="refresh-xyz",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow_api)),
    )
    results = await asyncio.gather(*(client.get_stops() for _ in range(5)))

    assert api.tokens_issued == 1
    assert api.stops_served == 1
    assert all(r == results[0] for r in results)


async def test_does_not_close_supplied_http_client() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(FakeAPI()))
    async with RTTClient(refresh_token="refresh-xyz", http_client=http) as client:
        await client.get_stops()

    assert not http.is_closed
    await http.aclose()
