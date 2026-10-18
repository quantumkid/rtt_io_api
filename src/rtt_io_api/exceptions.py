"""Exceptions raised by the RTT API client."""

from __future__ import annotations

import httpx


class RTTError(Exception):
    """Base class for all errors raised by this library."""


class RTTAPIError(RTTError):
    """The API returned an unsuccessful or unparseable response."""

    def __init__(self, message: str, response: httpx.Response) -> None:
        super().__init__(message)
        self.response = response
        self.status_code = response.status_code


class RTTAuthenticationError(RTTAPIError):
    """The token was missing, invalid, expired, or lacks the required entitlement (401/403)."""


class RTTRateLimitError(RTTAPIError):
    """The rate limit was exceeded (429).

    ``retry_after`` holds the number of seconds from the ``Retry-After`` header, if present.
    """

    def __init__(self, message: str, response: httpx.Response) -> None:
        super().__init__(message, response)
        retry_after = response.headers.get("Retry-After")
        try:
            self.retry_after: float | None = float(retry_after) if retry_after else None
        except ValueError:
            self.retry_after = None
