# rtt-io-api

A Python client for the [Realtime Trains API](https://realtimetrains.github.io/api-specification/) (`https://data.rtt.io`).

> **Status:** early alpha. Currently supports fetching access tokens and listing stops.

## Installation

```bash
pip install rtt-io-api
```

## Authentication

Sign up for API access at <https://api-portal.rtt.io>. You will be issued **either**:

- a long-life **access token**, which you can use directly, or
- a long-life **refresh token**, which you exchange for a short-life access token via `/api/get_access_token`.

> Realtime Trains requires that tokens are never embedded in distributable end-user applications. Proxy requests through your own server instead, or your token will be revoked.

## Usage

The client is async. Use it as an async context manager (or call `await client.aclose()`) so the underlying HTTP connection pool is released.

```python
import asyncio
from rtt_io_api import RTTClient


async def main() -> None:
    async with RTTClient(refresh_token="YOUR_REFRESH_TOKEN") as client:
        for stop in await client.get_stops():
            print(stop.short_code, stop.description, stop.unique_identity)


asyncio.run(main())
```

To experiment in a REPL, `python -m asyncio` starts one where you can use `await` directly.

`api_version` is optional and pins the API version via the `Version` header, e.g. `RTTClient(refresh_token=..., api_version="2025-10-25")`; if omitted, the API serves its latest version.

### Using your own `httpx.AsyncClient`

Pass `http_client=` to reuse an existing connection pool, for example Home Assistant's shared client:

```python
from homeassistant.helpers.httpx_client import get_async_client

client = RTTClient(refresh_token=token, http_client=get_async_client(hass))
```

A client you supply is never closed by `RTTClient`, and the `timeout` argument is ignored in that case (configure it on your own client).

## Access tokens

Data methods like `get_stops()` fetch an access token automatically using your refresh token, cache it, and renew it when it expires. You can also fetch one explicitly:

```python
access = await client.get_access_token()

print(access.valid_until)  # datetime (timezone-aware, UTC)
print(access.entitlements)  # ('allowDetailed', ...)
print(access.is_expired())  # False
```

## Stops

`get_stops()` wraps `/data/stops`, which lists every location where a service may stop for passengers (railway stations, bus stops, tram stops, etc.). In the `gb-nr` namespace, `short_code` is the CRS code (e.g. `CLJ`).

The stops list rarely changes, so `get_stops()` fetches it once and caches it on the client. Later calls return the cached list without contacting the API. Use `get_stops(refresh=True)` to fetch it again. If several tasks call it at once before the cache is filled, they share a single request.

## Errors

All exceptions derive from `rtt_io_api.RTTError`:

| Exception                | When                                                       |
|--------------------------|------------------------------------------------------------|
| `RTTAuthenticationError` | 401/403 – token missing, invalid, expired or not entitled  |
| `RTTRateLimitError`      | 429 – rate limited; see `.retry_after` (seconds)           |
| `RTTAPIError`            | Any other non-2xx status, or an unexpected response body   |

`RTTAPIError` (and its subclasses) expose `.status_code` and the raw `httpx.Response` as `.response`.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run mypy src
```

### Publishing to PyPI

```bash
uv build
uv publish            # or: twine upload dist/*
```

## License

MIT
