"""Typed exceptions shared across the engine and providers."""
from __future__ import annotations


class MarketIntelError(Exception):
    """Base class for engine-level failures (bad args, DB/config errors)."""


class ConfigError(MarketIntelError):
    """Settings could not be loaded or are invalid."""


class HostRejected(MarketIntelError):
    """SafeHttp refused a request: non-HTTPS, non-whitelisted host, or a
    cross-host redirect."""
