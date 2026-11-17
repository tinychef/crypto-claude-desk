"""ccxt exchange factory.

V1 problems fixed here:
- ccxt's requests session has ``trust_env=False`` so HTTPS_PROXY was ignored
  (cloud sessions could not reach any exchange). We set ``httpsProxy``.
- Binance/Bybit return 451/403 from US cloud IPs. ``exchange_priority()`` puts
  globally reachable venues first and ``fetch_with_fallback`` skips venues that
  fail, remembering geo-blocked ones for the process lifetime.

Configure with ``DESK_EXCHANGES=okx,kraken,coinbase,...`` (comma list, priority order).
"""
import os
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

import ccxt

DEFAULT_PRIORITY = ["okx", "kraken", "coinbase", "kucoin", "mexc", "bitfinex", "binance", "bybit", "bitget"]
DEFAULT_FUTURES_PRIORITY = ["okx", "bitget", "mexc", "binance", "bybit"]

GEO_BLOCKED_HINT = (
    "Binance/Bybit block US cloud IPs (HTTP 451/403). The desk falls back to OKX/Kraken/Coinbase. "
    "Set DESK_EXCHANGES to change priority."
)

_lock = threading.Lock()
_instances: Dict[Tuple[str, str], Any] = {}
_blocked: set = set()


def _proxy() -> Optional[str]:
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")


def exchange_priority(market: str = "spot") -> List[str]:
    env = os.environ.get("DESK_FUTURES_EXCHANGES" if market != "spot" else "DESK_EXCHANGES")
    names = [n.strip().lower() for n in env.split(",") if n.strip()] if env else (
        DEFAULT_PRIORITY if market == "spot" else DEFAULT_FUTURES_PRIORITY)
    names = [n for n in names if hasattr(ccxt, n)]
    return [n for n in names if n not in _blocked] + [n for n in names if n in _blocked]


def get_exchange(name: str, market: str = "spot", **overrides) -> Any:
    """Return a cached ccxt instance. ``market`` = 'spot' | 'swap'."""
    name = (name or "").lower()
    if not hasattr(ccxt, name):
        raise ValueError(f"Unsupported exchange '{name}'")
    key = (name, market)
    with _lock:
        inst = _instances.get(key)
        if inst is None:
            cfg: Dict[str, Any] = {"enableRateLimit": True, "timeout": 20000}
            if market != "spot":
                cfg["options"] = {"defaultType": "swap"}
            proxy = _proxy()
            if proxy:
                cfg["httpsProxy"] = proxy
            cfg.update(overrides)
            inst = getattr(ccxt, name)(cfg)
            _instances[key] = inst
        return inst


def mark_blocked(name: str) -> None:
    _blocked.add(name.lower())


def _is_geo_block(err: Exception) -> bool:
    msg = str(err)
    return any(code in msg for code in ("451", "403 Forbidden", "restricted location", "CloudFront"))


def fetch_with_fallback(fn: Callable[[Any], Any], exchanges: Optional[List[str]] = None,
                        market: str = "spot") -> Tuple[Any, str, List[str]]:
    """Call ``fn(exchange)`` on each venue in priority order until one succeeds.

    Returns (result, exchange_name, errors). Raises RuntimeError if all fail.
    """
    errors: List[str] = []
    for name in exchanges or exchange_priority(market):
        try:
            ex = get_exchange(name, market)
            result = fn(ex)
            if result is None or (hasattr(result, "__len__") and len(result) == 0):
                errors.append(f"{name}: empty result")
                continue
            return result, name, errors
        except Exception as e:  # ccxt raises many types; we want the next venue
            if _is_geo_block(e):
                mark_blocked(name)
            errors.append(f"{name}: {e.__class__.__name__}: {str(e)[:160]}")
    raise RuntimeError("all exchanges failed: " + " | ".join(errors))
