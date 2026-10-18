from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from rtt_io_api import (
    AccessToken,
    RTTAPIError,
    RTTAuthenticationError,
    RTTClient,
    RTTRateLimitError,
)

TOKEN_RESPONSE = {
    "token": "access-abc",
    "entitlements": ["allowDetailed", "allowAllocations"],
    "validUntil": "2030-01-01T12:00:00Z",
}


def make_client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: object) -> RTTClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RTTClient(refresh_token="refresh-xyz", http_client=http, **kwargs)  # type: ignore[arg-type]


async def test_get_access_token_success() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=TOKEN_RESPONSE)

    token = await make_client(handler).get_access_token()

    assert token == AccessToken(
        token="access-abc",
        valid_until=datetime(2030, 1, 1, 12, tzinfo=timezone.utc),
        entitlements=("allowDetailed", "allowAllocations"),
    )
    assert token.has_entitlement("allowDetailed")
    assert not token.is_expired()

    (request,) = seen
    assert request.method == "GET"
    assert str(request.url) == "https://data.rtt.io/api/get_access_token"
    assert request.headers["Authorization"] == "Bearer refresh-xyz"
    assert "Version" not in request.headers


async def test_version_header_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Version"] == "2025-10-25"
        return httpx.Response(200, json=TOKEN_RESPONSE)

    await make_client(handler, api_version=date(2025, 10, 25)).get_access_token()


async def test_custom_base_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.test/api/get_access_token"
        return httpx.Response(200, json=TOKEN_RESPONSE)

    await make_client(handler, base_url="https://example.test/").get_access_token()


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error(status: int) -> None:
    client = make_client(lambda r: httpx.Response(status, text="nope"))
    with pytest.raises(RTTAuthenticationError) as info:
        await client.get_access_token()
    assert info.value.status_code == status


async def test_rate_limit_error() -> None:
    client = make_client(lambda r: httpx.Response(429, headers={"Retry-After": "17"}))
    with pytest.raises(RTTRateLimitError) as info:
        await client.get_access_token()
    assert info.value.retry_after == 17


async def test_server_error() -> None:
    client = make_client(lambda r: httpx.Response(500))
    with pytest.raises(RTTAPIError) as info:
        await client.get_access_token()
    assert info.value.status_code == 500


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["a", "list"]),
        httpx.Response(200, json={"token": "x"}),
        httpx.Response(200, json={"token": "x", "validUntil": "garbage"}),
    ],
)
async def test_malformed_response(response: httpx.Response) -> None:
    client = make_client(lambda r: response)
    with pytest.raises(RTTAPIError):
        await client.get_access_token()


def test_is_expired_with_leeway() -> None:
    soon = datetime.now(timezone.utc) + timedelta(seconds=10)
    token = AccessToken(token="t", valid_until=soon)
    assert token.is_expired()
    assert not token.is_expired(leeway=timedelta(0))


def test_token_hidden_from_repr() -> None:
    token = AccessToken.from_dict(TOKEN_RESPONSE)
    assert "access-abc" not in repr(token)


def test_offset_datetime_parsed() -> None:
    token = AccessToken.from_dict({**TOKEN_RESPONSE, "validUntil": "2030-01-01T13:00:00+01:00"})
    assert token.valid_until == datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def test_refresh_token_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        RTTClient("refresh-xyz")  # type: ignore[call-arg]


def test_empty_token_rejected() -> None:
    with pytest.raises(ValueError):
        RTTClient(refresh_token="")
