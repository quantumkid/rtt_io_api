"""Python client for the Realtime Trains API (https://data.rtt.io)."""

from rtt_io_api.client import RTTClient
from rtt_io_api.exceptions import (
    RTTAPIError,
    RTTAuthenticationError,
    RTTError,
    RTTRateLimitError,
)
from rtt_io_api.models import AccessToken, Service, ServiceTimes, Stop

__version__ = "0.1.0"

__all__ = [
    "AccessToken",
    "RTTAPIError",
    "RTTAuthenticationError",
    "RTTClient",
    "RTTError",
    "RTTRateLimitError",
    "Service",
    "ServiceTimes",
    "Stop",
]
