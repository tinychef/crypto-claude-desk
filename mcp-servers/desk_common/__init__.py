"""Shared infrastructure for the crypto-desk MCP servers (V2).

- http:       cached, retrying HTTP GET with optional API-key injection
- exchanges:  ccxt factory with proxy support + geo-block aware fallback
- serialize:  numpy/pandas-safe JSON serializer for FastMCP
- symbols:    symbol normalization helpers
- keys:       optional API-key lookup (paid / free-key providers)
"""
from .serialize import json_native, tool_serializer
from .http import http_get, http_get_json, HttpError, clear_cache, http_failure
from .keys import get_key, unavailable
from .symbols import base_symbol, spot_pair, perp_pair
from .exchanges import get_exchange, exchange_priority, fetch_with_fallback, GEO_BLOCKED_HINT

__all__ = [
    "json_native", "tool_serializer",
    "http_get", "http_get_json", "HttpError", "clear_cache", "http_failure",
    "get_key", "unavailable",
    "base_symbol", "spot_pair", "perp_pair",
    "get_exchange", "exchange_priority", "fetch_with_fallback", "GEO_BLOCKED_HINT",
]
