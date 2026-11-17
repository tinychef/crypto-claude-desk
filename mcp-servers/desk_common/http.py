"""Cached, retrying HTTP GET.

- Honors HTTPS_PROXY / REQUESTS_CA_BUNDLE (requests trust_env).
- Small on-disk TTL cache (SQLite) shared across servers/processes, so repeated
  agent calls don't burn rate limits (CoinGecko 429s were a V1 pain point).
- Exponential backoff on 429 / 5xx, honoring Retry-After.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

DEFAULT_TIMEOUT = 15
USER_AGENT = "crypto-desk/2.0 (+https://github.com/tinychef/crypto-claude-desk)"

_session_lock = threading.Lock()
_session: Optional[requests.Session] = None
_cache_lock = threading.Lock()


class HttpError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, url: str = "", blocked: bool = False):
        super().__init__(message)
        self.status = status
        self.url = url
        # True when the host is not reachable from this environment (proxy/allowlist
        # refusal or geo-block). Callers should report it as "unavailable", not "error".
        self.blocked = blocked


def _host(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(url).hostname or url
    except Exception:
        return url


def http_failure(err: Exception, provider: str) -> dict:
    """Uniform tool result for an HttpError: blocked hosts -> unavailable, else error."""
    if isinstance(err, HttpError) and err.blocked:
        host = _host(err.url)
        return {"status": "unavailable", "provider": provider,
                "reason": f"host {host} not reachable from this environment",
                "hint": f"Allow {host} in the environment network settings (see docs/data-sources.md)."}
    return {"status": "error", "error": str(err), "source": provider}


def _get_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            s.trust_env = True
            s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/csv, */*"})
            _session = s
        return _session


def _cache_path() -> Path:
    base = os.environ.get("DESK_CACHE_DIR")
    if base:
        p = Path(base)
    else:
        p = Path(__file__).resolve().parents[2] / "data" / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p / "http_cache.sqlite"


def _cache_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_cache_path()), timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, ts REAL, body TEXT)")
    return conn


def _cache_key(url: str, params: Optional[Dict], headers: Optional[Dict]) -> str:
    # Never hash secret header values into a readable key; hash everything.
    raw = json.dumps([url, sorted((params or {}).items()), sorted((headers or {}).keys())], default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_enabled() -> bool:
    return os.environ.get("DESK_HTTP_CACHE", "1") not in ("0", "false", "no")


def _cache_get(key: str, ttl: float) -> Optional[str]:
    if ttl <= 0 or not _cache_enabled():
        return None
    try:
        with _cache_lock, _cache_conn() as conn:
            row = conn.execute("SELECT ts, body FROM cache WHERE k=?", (key,)).fetchone()
        if row and (time.time() - row[0]) < ttl:
            return row[1]
    except sqlite3.Error:
        return None
    return None


def _cache_put(key: str, body: str) -> None:
    if not _cache_enabled():
        return
    try:
        with _cache_lock, _cache_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO cache (k, ts, body) VALUES (?,?,?)", (key, time.time(), body))
    except sqlite3.Error:
        pass


def clear_cache() -> None:
    try:
        with _cache_lock, _cache_conn() as conn:
            conn.execute("DELETE FROM cache")
    except sqlite3.Error:
        pass


def http_get(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    ttl: float = 60,
    retries: int = 3,
    timeout: float = DEFAULT_TIMEOUT,
    backoff: float = 1.5,
) -> str:
    """GET ``url`` and return the body text. Raises HttpError on final failure."""
    key = _cache_key(url, params, headers)
    cached = _cache_get(key, ttl)
    if cached is not None:
        return cached

    session = _get_session()
    last_err: Optional[HttpError] = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            msg = str(e)
            blocked = isinstance(e, requests.exceptions.ProxyError) or "Tunnel connection failed" in msg
            last_err = HttpError(f"network error: {e.__class__.__name__}: {e}", None, url, blocked=blocked)
            if blocked:
                break  # proxy refusal is deterministic; retrying only wastes time
        else:
            if resp.status_code == 200:
                _cache_put(key, resp.text)
                return resp.text
            last_err = HttpError(f"HTTP {resp.status_code} from {url}", resp.status_code, url,
                                 blocked=resp.status_code == 451)
            if resp.status_code not in (429, 500, 502, 503, 504):
                break  # 4xx (incl. 451 geo-block, 403) won't improve with retries
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit() and attempt < retries:
                time.sleep(min(float(retry_after), 30))
                continue
        if attempt < retries:
            time.sleep(backoff ** attempt)
    raise last_err or HttpError("unknown error", None, url)


def http_get_json(url: str, params=None, headers=None, ttl: float = 60, retries: int = 3,
                  timeout: float = DEFAULT_TIMEOUT) -> Any:
    body = http_get(url, params=params, headers=headers, ttl=ttl, retries=retries, timeout=timeout)
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise HttpError(f"invalid JSON from {url}: {e}", 200, url)
