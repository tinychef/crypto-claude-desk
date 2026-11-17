#!/usr/bin/env python3
"""Crypto Derivatives — aggregated OI/funding/liquidations, options, basis and on-chain perps.

Data sources:
- Coinalyze (free key, header ``api_key``; env DESK_COINALYZE_KEY): OI, funding and
  liquidations aggregated across exchanges. Without a key, OI falls back to OKX (ccxt).
- Deribit public API (no key): DVOL, option book summary (put/call, skew, term structure,
  max pain) and dated futures.
- OKX (ccxt + public Rubik stats, no key): dated-futures basis, L/S account ratio,
  taker buy/sell volume, contracts OI history.
- Hyperliquid info API (no key, POST): funding, OI, premium and volume for on-chain perps.
"""
import json
import logging
import math
import re
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastmcp import FastMCP

from desk_common import (
    HttpError,
    http_failure,
    base_symbol,
    get_exchange,
    get_key,
    http_get_json,
    spot_pair,
    tool_serializer,
    unavailable,
)

logger = logging.getLogger(__name__)
mcp = FastMCP("crypto-derivatives", tool_serializer=tool_serializer)

COINALYZE_BASE = "https://api.coinalyze.net/v1"
DERIBIT_BASE = "https://www.deribit.com/api/v2/public"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
OKX_RUBIK = "https://www.okx.com/api/v5/rubik/stat"

COINALYZE_INTERVALS = {"1min", "5min", "15min", "30min", "1hour", "2hour", "4hour", "6hour", "12hour", "daily"}
_INTERVAL_SECONDS = {"1min": 60, "5min": 300, "15min": 900, "30min": 1800, "1hour": 3600, "2hour": 7200,
                     "4hour": 14400, "6hour": 21600, "12hour": 43200, "daily": 86400}
# Coinalyze exchange codes, best effort (only used for display; unknown codes are shown raw).
COINALYZE_EXCHANGES = {"A": "Binance", "6": "Bybit", "3": "OKX", "0": "BitMEX", "2": "Deribit", "H": "Hyperliquid",
                       "4": "Huobi", "C": "Coinbase", "K": "Kraken", "G": "Gate.io", "D": "Bitfinex", "8": "dYdX",
                       "7": "Phemex", "W": "WOO X", "V": "Vertex", "F": "Bitget"}
_COINALYZE_PRIORITY = ["A", "6", "3", "F", "0", "2", "H", "G", "4", "C", "K", "D", "8", "7", "W", "V"]
OKX_RUBIK_PERIODS = {"5m", "1H", "1D"}

_DERIBIT_INSTR = re.compile(r"^([A-Z_]+)-(\d{1,2}[A-Z]{3}\d{2})-(\d+(?:d\d+)?)-([CP])$")
_DERIBIT_FUT = re.compile(r"^([A-Z_]+)-(\d{1,2}[A-Z]{3}\d{2})$")


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: Optional[datetime] = None) -> str:
    return (ts or _now()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ms_iso(ms: Any) -> Optional[str]:
    try:
        return _iso(datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def _pct(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old in (None, 0):
        return None
    return round((new - old) / abs(old) * 100, 2)


def _r(x: Optional[float], nd: int = 2) -> Optional[float]:
    return None if x is None else round(x, nd)


def _usd(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 0)


# Tiny in-process TTL cache for the POST helper (http_get_json already caches GETs on disk).
_post_cache: Dict[str, Tuple[float, Any]] = {}
_post_lock = threading.Lock()
_post_session: Optional[requests.Session] = None


def _http_post_json(url: str, payload: Dict[str, Any], ttl: float = 30, timeout: float = 15) -> Any:
    """POST JSON and return parsed JSON. Honors HTTPS_PROXY (trust_env). Raises HttpError."""
    global _post_session
    key = url + json.dumps(payload, sort_keys=True)
    with _post_lock:
        hit = _post_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        if _post_session is None:
            s = requests.Session()
            s.trust_env = True
            s.headers.update({"User-Agent": "crypto-desk/2.0", "Content-Type": "application/json"})
            _post_session = s
        session = _post_session
    try:
        resp = session.post(url, json=payload, timeout=timeout)
    except requests.RequestException as e:
        blocked = isinstance(e, requests.exceptions.ProxyError) or "Tunnel connection failed" in str(e)
        raise HttpError(f"network error: {e.__class__.__name__}: {e}", None, url, blocked=blocked)
    if resp.status_code != 200:
        raise HttpError(f"HTTP {resp.status_code} from {url}", resp.status_code, url)
    try:
        data = resp.json()
    except ValueError as e:
        raise HttpError(f"invalid JSON from {url}: {e}", 200, url)
    with _post_lock:
        _post_cache[key] = (time.time(), data)
    return data


# ---------------------------------------------------------------------------
# Coinalyze helpers
# ---------------------------------------------------------------------------

def _coinalyze_get(path: str, key: str, params: Optional[Dict[str, Any]] = None, ttl: float = 60) -> Any:
    return http_get_json(f"{COINALYZE_BASE}{path}", params=params, headers={"api_key": key}, ttl=ttl)


def _coinalyze_perps(base: str, key: str, max_n: int = 20) -> List[Dict[str, Any]]:
    """Perpetual markets on Coinalyze for ``base`` (max 20 = one request), major venues first."""
    markets = _coinalyze_get("/future-markets", key, ttl=86400) or []
    perps = [m for m in markets
             if str(m.get("base_asset", "")).upper() == base and m.get("is_perpetual")]

    def rank(m):
        code = str(m.get("symbol", "")).rsplit(".", 1)[-1]
        return _COINALYZE_PRIORITY.index(code) if code in _COINALYZE_PRIORITY else len(_COINALYZE_PRIORITY)

    perps.sort(key=rank)
    return perps[:max_n]


def _exchange_of(coinalyze_symbol: str) -> str:
    code = coinalyze_symbol.rsplit(".", 1)[-1] if "." in coinalyze_symbol else "?"
    return COINALYZE_EXCHANGES.get(code, code)


def _validate_window(interval: str, hours: int) -> Optional[str]:
    if interval not in COINALYZE_INTERVALS:
        return f"interval must be one of {sorted(COINALYZE_INTERVALS)}"
    if not isinstance(hours, int) or hours < 1 or hours > 24 * 90:
        return "hours must be an integer between 1 and 2160"
    if hours * 3600 / _INTERVAL_SECONDS[interval] > 2000:
        return "too many points: use a larger interval for this many hours"
    return None


# ---------------------------------------------------------------------------
# Deribit helpers
# ---------------------------------------------------------------------------

def _deribit(method: str, params: Dict[str, Any], ttl: float = 60) -> Any:
    data = http_get_json(f"{DERIBIT_BASE}/{method}", params=params, ttl=ttl)
    if not isinstance(data, dict) or "result" not in data:
        err = data.get("error") if isinstance(data, dict) else None
        raise HttpError(f"Deribit {method} returned no result: {err}", None, method)
    return data["result"]


def _parse_deribit_expiry(code: str) -> Optional[datetime]:
    try:
        d = datetime.strptime(code, "%d%b%y")
    except ValueError:
        return None
    return d.replace(hour=8, tzinfo=timezone.utc)


def _parse_options(book: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    out = []
    for row in book or []:
        m = _DERIBIT_INSTR.match(str(row.get("instrument_name", "")))
        if not m:
            continue
        expiry = _parse_deribit_expiry(m.group(2))
        if expiry is None or expiry <= now:
            continue
        out.append({
            "expiry": expiry,
            "expiry_code": m.group(2),
            "strike": float(m.group(3).replace("d", ".")),
            "type": m.group(4),
            "oi": _f(row.get("open_interest")) or 0.0,
            "volume": _f(row.get("volume")) or 0.0,
            "mark_iv": _f(row.get("mark_iv")),
            "underlying": _f(row.get("underlying_price")),
            "index": _f(row.get("estimated_delivery_price")),
        })
    return out


def _index_from_options(opts: List[Dict[str, Any]]) -> Optional[float]:
    vals = [o["index"] for o in opts if o["index"]] or [o["underlying"] for o in opts if o["underlying"]]
    return statistics.median(vals) if vals else None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs_delta(spot: float, strike: float, iv_pct: float, t_years: float, is_call: bool) -> Optional[float]:
    if not spot or not strike or not iv_pct or t_years <= 0:
        return None
    sigma = iv_pct / 100.0
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t_years) / (sigma * math.sqrt(t_years))
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1


def _is_monthly(expiry: datetime) -> bool:
    """Deribit monthlies expire on the last Friday of the month."""
    return expiry.weekday() == 4 and (expiry + timedelta(days=7)).month != expiry.month


def _group_by_expiry(opts: List[Dict[str, Any]]) -> Dict[datetime, List[Dict[str, Any]]]:
    groups: Dict[datetime, List[Dict[str, Any]]] = {}
    for o in opts:
        groups.setdefault(o["expiry"], []).append(o)
    return dict(sorted(groups.items()))


def _atm_iv(chain: List[Dict[str, Any]]) -> Optional[float]:
    spot = _index_from_options(chain) or None
    und = [o["underlying"] for o in chain if o["underlying"]]
    ref = statistics.median(und) if und else spot
    if not ref:
        return None
    with_iv = [o for o in chain if o["mark_iv"]]
    if not with_iv:
        return None
    k = min({o["strike"] for o in with_iv}, key=lambda s: abs(s - ref))
    ivs = [o["mark_iv"] for o in with_iv if o["strike"] == k]
    return round(sum(ivs) / len(ivs), 2)


def _skew_25d(chain: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    und = [o["underlying"] for o in chain if o["underlying"]]
    spot = statistics.median(und) if und else _index_from_options(chain)
    if not spot:
        return {"method": "none", "skew_25d": None}
    t = max((chain[0]["expiry"] - now).total_seconds() / (365 * 86400), 1e-6)
    calls, puts = [], []
    for o in chain:
        if not o["mark_iv"]:
            continue
        is_call = o["type"] == "C"
        if (is_call and o["strike"] <= spot) or (not is_call and o["strike"] >= spot):
            continue  # OTM only
        d = _bs_delta(spot, o["strike"], o["mark_iv"], t, is_call)
        if d is not None:
            (calls if is_call else puts).append((abs(abs(d) - 0.25), o, d))
    if calls and puts:
        _, c, cd = min(calls, key=lambda x: x[0])
        _, p, pd_ = min(puts, key=lambda x: x[0])
        return {"method": "black_scholes_delta_from_mark_iv", "call_strike": c["strike"], "call_delta": round(cd, 3),
                "call_iv": c["mark_iv"], "put_strike": p["strike"], "put_delta": round(pd_, 3), "put_iv": p["mark_iv"],
                "skew_25d": round(p["mark_iv"] - c["mark_iv"], 2)}
    # Fallback: ±10% moneyness
    ivc = [o for o in chain if o["type"] == "C" and o["mark_iv"]]
    ivp = [o for o in chain if o["type"] == "P" and o["mark_iv"]]
    if ivc and ivp:
        c = min(ivc, key=lambda o: abs(o["strike"] - spot * 1.10))
        p = min(ivp, key=lambda o: abs(o["strike"] - spot * 0.90))
        return {"method": "moneyness_10pct_proxy", "call_strike": c["strike"], "call_iv": c["mark_iv"],
                "put_strike": p["strike"], "put_iv": p["mark_iv"], "skew_25d": round(p["mark_iv"] - c["mark_iv"], 2)}
    return {"method": "none", "skew_25d": None}


def _max_pain(chain: List[Dict[str, Any]]) -> Optional[float]:
    strikes = sorted({o["strike"] for o in chain})
    if not strikes:
        return None
    best, best_pay = None, None
    for k in strikes:
        pay = 0.0
        for o in chain:
            if o["type"] == "C":
                pay += o["oi"] * max(0.0, k - o["strike"])
            else:
                pay += o["oi"] * max(0.0, o["strike"] - k)
        if best_pay is None or pay < best_pay:
            best, best_pay = k, pay
    return best


def _resolve_expiry(groups: Dict[datetime, List[Dict[str, Any]]], expiry: Optional[str]) -> Optional[datetime]:
    if not groups:
        return None
    if expiry:
        e = expiry.strip().upper()
        for exp, chain in groups.items():
            if chain[0]["expiry_code"] == e or exp.strftime("%Y-%m-%d") == expiry.strip():
                return exp
        return None
    fridays = [e for e in groups if e.weekday() == 4]
    return fridays[0] if fridays else next(iter(groups))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _okx_oi_fallback(base: str, hours: int) -> Dict[str, Any]:
    ex = get_exchange("okx", "swap")
    venues, total, errors = [], 0.0, []
    for sym in (f"{base}/USDT:USDT", f"{base}/USD:{base}"):
        try:
            oi = ex.fetch_open_interest(sym)
            val = _f(oi.get("openInterestValue")) or _f((oi.get("info") or {}).get("oiUsd"))
            if val:
                total += val
                venues.append({"market": f"OKX {sym}", "oi_usd": _usd(val)})
        except Exception as e:  # pragma: no cover - market may not exist
            errors.append(f"{sym}: {e.__class__.__name__}")
    funding = None
    try:
        funding = _f(ex.fetch_funding_rate(f"{base}/USDT:USDT").get("fundingRate"))
    except Exception as e:
        errors.append(f"funding: {e.__class__.__name__}")
    change = None
    try:
        hist = http_get_json(f"{OKX_RUBIK}/contracts/open-interest-volume",
                             params={"ccy": base, "period": "1H"}, ttl=300).get("data") or []
        series = [(int(r[0]), float(r[1])) for r in hist]  # newest first
        if len(series) > 1:
            idx = min(hours, len(series) - 1)
            change = _pct(series[0][1], series[idx][1])
    except Exception as e:
        errors.append(f"rubik history: {e.__class__.__name__}")
    if not venues:
        raise RuntimeError("OKX fallback returned no open interest: " + "; ".join(errors))
    out = {
        "status": "success", "source": "okx (fallback: Coinalyze key missing)", "aggregated": False,
        "as_of": _iso(), "symbol": base, "total_oi_usd": _usd(total), f"oi_change_pct_{hours}h": change,
        "venues": venues,
        "funding_rate_8h_pct": _r(funding * 100, 4) if funding is not None else None,
        "note": "Single venue (OKX) only — set DESK_COINALYZE_KEY for cross-exchange aggregation.",
    }
    if errors:
        out["data_gaps"] = errors
    out["interpretation"] = _oi_read(change, out["funding_rate_8h_pct"])
    return out


def _oi_read(change: Optional[float], funding_pct: Optional[float]) -> str:
    parts = []
    if change is not None:
        if change > 5:
            parts.append(f"OI +{change}%: leverage building fast (fuel for a squeeze either way)")
        elif change < -5:
            parts.append(f"OI {change}%: deleveraging / positions closed")
        else:
            parts.append(f"OI {change:+}%: positioning stable")
    if funding_pct is not None:
        if funding_pct > 0.03:
            parts.append("funding hot — longs crowded")
        elif funding_pct < 0:
            parts.append("negative funding — shorts paying, squeeze risk up")
        else:
            parts.append("funding neutral")
    return "; ".join(parts) or "insufficient data"


@mcp.tool()
def get_aggregated_oi(symbol: str = "BTC", interval: str = "1hour", hours: int = 48) -> Dict[str, Any]:
    """Open interest aggregated across exchanges (Coinalyze, USD) with change % over `hours`, top venues
    and OI-weighted funding. Without a Coinalyze key falls back to OKX only (`aggregated: False`).
    Read: rising OI + rising price = new longs (trend fuel); rising OI + falling price = shorts pressing;
    falling OI = deleveraging. Hot funding (>0.03%/8h) = crowded longs."""
    try:
        base = base_symbol(symbol)
        err = _validate_window(interval, hours)
        if err:
            return {"status": "error", "error": err}
        key = get_key("COINALYZE")
        if not key:
            return _okx_oi_fallback(base, hours)

        perps = _coinalyze_perps(base, key)
        if not perps:
            return {"status": "error", "error": f"no perpetual markets for {base} on Coinalyze", "source": "coinalyze"}
        syms = [p["symbol"] for p in perps]
        to_ts = int(time.time())
        hist = _coinalyze_get("/open-interest-history", key, params={
            "symbols": ",".join(syms), "interval": interval, "from": to_ts - hours * 3600, "to": to_ts,
            "convert_to_usd": "true"}, ttl=60) or []
        totals: Dict[int, float] = {}
        last_by_symbol: Dict[str, float] = {}
        for row in hist:
            points = row.get("history") or []
            for p in points:
                c = _f(p.get("c"))
                if c is not None:
                    totals[int(p["t"])] = totals.get(int(p["t"]), 0.0) + c
            if points and _f(points[-1].get("c")) is not None:
                last_by_symbol[row.get("symbol", "?")] = float(points[-1]["c"])
        if not totals:
            return {"status": "error", "error": "Coinalyze returned no OI history", "source": "coinalyze"}
        ts_sorted = sorted(totals)
        # Ignore a trailing bucket where not all venues reported yet.
        first, last = totals[ts_sorted[0]], totals[ts_sorted[-1]]
        total_last = sum(last_by_symbol.values())

        funding = None
        data_gaps: List[str] = []
        try:
            fr = _coinalyze_get("/funding-rate", key, params={"symbols": ",".join(syms)}, ttl=60) or []
            num = den = 0.0
            for row in fr:
                v, w = _f(row.get("value")), last_by_symbol.get(row.get("symbol"))
                if v is not None and w:
                    num += v * w
                    den += w
            funding = num / den if den else None
        except HttpError as e:
            data_gaps.append(f"funding: {e}")

        venues = sorted(last_by_symbol.items(), key=lambda kv: kv[1], reverse=True)[:10]
        change = _pct(last, first)
        out = {
            "status": "success", "source": "coinalyze", "aggregated": True, "as_of": _iso(), "symbol": base,
            "interval": interval, "markets_counted": len(last_by_symbol),
            "total_oi_usd": _usd(total_last), f"oi_change_pct_{hours}h": change,
            "oi_high_usd": _usd(max(totals.values())), "oi_low_usd": _usd(min(totals.values())),
            "top_venues": [{"market": s, "exchange": _exchange_of(s), "oi_usd": _usd(v),
                            "share_pct": _r(v / total_last * 100, 1) if total_last else None} for s, v in venues],
            "funding_rate_8h_pct": _r(funding, 4),
        }
        if data_gaps:
            out["data_gaps"] = data_gaps
        out["interpretation"] = _oi_read(change, out["funding_rate_8h_pct"])
        return out
    except HttpError as e:
        return http_failure(e, "coinalyze")
    except Exception as e:
        logger.exception("get_aggregated_oi failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def get_liquidations_history(symbol: str = "BTC", interval: str = "1hour", hours: int = 48) -> Dict[str, Any]:
    """Liquidations aggregated across exchanges (Coinalyze, USD): long vs short totals over `hours` and
    cascade buckets (> 3x the average bucket). Requires a free Coinalyze key (else `unavailable`).
    Read: long-liquidation cascades after a drop often mark local capitulation lows; short cascades = squeeze tops."""
    try:
        base = base_symbol(symbol)
        err = _validate_window(interval, hours)
        if err:
            return {"status": "error", "error": err}
        key = get_key("COINALYZE")
        if not key:
            return unavailable("coinalyze", "missing API key (free at coinalyze.net)", symbol=base)
        perps = _coinalyze_perps(base, key)
        if not perps:
            return {"status": "error", "error": f"no perpetual markets for {base} on Coinalyze", "source": "coinalyze"}
        to_ts = int(time.time())
        hist = _coinalyze_get("/liquidation-history", key, params={
            "symbols": ",".join(p["symbol"] for p in perps), "interval": interval,
            "from": to_ts - hours * 3600, "to": to_ts, "convert_to_usd": "true"}, ttl=60) or []
        buckets: Dict[int, List[float]] = {}
        for row in hist:
            for p in row.get("history") or []:
                b = buckets.setdefault(int(p["t"]), [0.0, 0.0])
                b[0] += _f(p.get("l")) or 0.0
                b[1] += _f(p.get("s")) or 0.0
        if not buckets:
            return {"status": "success", "source": "coinalyze", "as_of": _iso(), "symbol": base,
                    "long_liquidations_usd": 0, "short_liquidations_usd": 0, "cascades": [],
                    "interpretation": "No liquidations reported in the window."}
        longs = sum(b[0] for b in buckets.values())
        shorts = sum(b[1] for b in buckets.values())
        avg = (longs + shorts) / len(buckets)
        cascades = []
        for t in sorted(buckets):
            tot = sum(buckets[t])
            if avg > 0 and tot > 3 * avg:
                l, s = buckets[t]
                cascades.append({"time": _iso(datetime.fromtimestamp(t, tz=timezone.utc)), "total_usd": _usd(tot),
                                 "long_usd": _usd(l), "short_usd": _usd(s), "x_avg": _r(tot / avg, 1),
                                 "side": "longs" if l >= s else "shorts"})
        peak_t = max(buckets, key=lambda t: sum(buckets[t]))
        ratio = longs / shorts if shorts else None
        if ratio is None or ratio > 2:
            read = "Longs liquidated far more than shorts — long flush; watch for capitulation low if OI also dropped."
        elif ratio < 0.5:
            read = "Shorts liquidated far more than longs — short squeeze; late shorts trapped, beware blow-off."
        else:
            read = "Balanced liquidations — two-way chop."
        if cascades:
            read += f" {len(cascades)} cascade bucket(s); latest {cascades[-1]['time']} ({cascades[-1]['side']})."
        return {
            "status": "success", "source": "coinalyze", "as_of": _iso(), "symbol": base, "interval": interval,
            "hours": hours, "markets_counted": len(hist),
            "long_liquidations_usd": _usd(longs), "short_liquidations_usd": _usd(shorts),
            "long_short_liq_ratio": _r(ratio, 2), "avg_bucket_usd": _usd(avg),
            "peak_bucket": {"time": _iso(datetime.fromtimestamp(peak_t, tz=timezone.utc)),
                            "total_usd": _usd(sum(buckets[peak_t]))},
            "cascades": cascades[-10:], "interpretation": read,
        }
    except HttpError as e:
        return http_failure(e, "coinalyze")
    except Exception as e:
        logger.exception("get_liquidations_history failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _deribit_currency(currency: str) -> str:
    cur = base_symbol(currency)
    return cur


@mcp.tool()
def get_options_snapshot(currency: str = "BTC") -> Dict[str, Any]:
    """Deribit options snapshot: DVOL (30d implied vol index), put/call OI and volume ratios, 25-delta skew
    for the nearest monthly expiry, and ATM IV term structure. Read: skew > +5 vol pts = puts bid (fear/hedging);
    skew < -2 = calls bid (FOMO); front IV > back IV (inverted) = stress event priced; P/C OI > 0.8 = heavy hedging."""
    try:
        cur = _deribit_currency(currency)
        now = _now()
        book = _deribit("get_book_summary_by_currency", {"currency": cur, "kind": "option"}, ttl=120)
        opts = _parse_options(book, now)
        if not opts:
            return {"status": "error", "error": f"no live {cur} options on Deribit", "source": "deribit"}
        data_gaps: List[str] = []
        index = _index_from_options(opts)

        dvol = None
        try:
            end = int(now.timestamp() * 1000)
            vol = _deribit("get_volatility_index_data", {"currency": cur, "start_timestamp": end - 2 * 86400 * 1000,
                                                         "end_timestamp": end, "resolution": "3600"}, ttl=300)
            rows = sorted(vol.get("data") or [], key=lambda r: r[0])
            if rows:
                cur_v = _f(rows[-1][4])
                day_ago = [r for r in rows if r[0] <= rows[-1][0] - 86400 * 1000]
                dvol = {"value": _r(cur_v), "change_24h": _r(cur_v - _f(day_ago[-1][4]), 2) if day_ago else None}
        except HttpError as e:
            data_gaps.append(f"DVOL: {e}")

        call_oi = sum(o["oi"] for o in opts if o["type"] == "C")
        put_oi = sum(o["oi"] for o in opts if o["type"] == "P")
        call_vol = sum(o["volume"] for o in opts if o["type"] == "C")
        put_vol = sum(o["volume"] for o in opts if o["type"] == "P")
        groups = _group_by_expiry(opts)

        term = []
        for exp, chain in list(groups.items())[:10]:
            term.append({"expiry": chain[0]["expiry_code"], "days": _r((exp - now).total_seconds() / 86400, 1),
                         "atm_iv": _atm_iv(chain), "oi": _r(sum(o["oi"] for o in chain), 1)})
        monthly = [e for e in groups if _is_monthly(e) and (e - now).days >= 7] or \
                  [e for e in groups if (e - now).days >= 2] or list(groups)
        skew_exp = monthly[0]
        skew = _skew_25d(groups[skew_exp], now)
        skew["expiry"] = groups[skew_exp][0]["expiry_code"]

        pcr_oi = put_oi / call_oi if call_oi else None
        pcr_vol = put_vol / call_vol if call_vol else None
        ivs = [t["atm_iv"] for t in term if t["atm_iv"] is not None and t["days"] >= 1]
        shape = None
        if len(ivs) >= 2:
            shape = "inverted (front > back: event/stress priced)" if ivs[0] > ivs[-1] + 1 else \
                "upward (normal, calm)" if ivs[-1] > ivs[0] + 1 else "flat"

        reads = []
        s = skew.get("skew_25d")
        if s is not None:
            reads.append("puts bid — fear/hedging" if s > 5 else "calls bid — FOMO/upside chase" if s < -2
                         else "skew balanced")
        if pcr_oi is not None:
            reads.append("heavy put OI (hedged market)" if pcr_oi > 0.8 else "call-heavy OI (complacent/bullish)"
                         if pcr_oi < 0.5 else "P/C OI normal")
        if dvol and dvol["value"] is not None:
            v = dvol["value"]
            reads.append("DVOL low — complacency, cheap options" if v < 40 else "DVOL high — fear, expensive options"
                         if v > 70 else "DVOL mid-range")
        if shape:
            reads.append(f"term structure {shape}")
        out = {
            "status": "success", "source": "deribit", "as_of": _iso(), "currency": cur, "index_price": _r(index),
            "dvol": dvol,
            "open_interest": {"calls": _r(call_oi, 1), "puts": _r(put_oi, 1), "unit": cur,
                              "notional_usd": _usd((call_oi + put_oi) * index) if index else None,
                              "put_call_oi_ratio": _r(pcr_oi, 3)},
            "volume_24h": {"calls": _r(call_vol, 1), "puts": _r(put_vol, 1), "put_call_volume_ratio": _r(pcr_vol, 3)},
            "skew_25d": skew, "term_structure": term, "term_shape": shape,
            "interpretation": "; ".join(reads),
        }
        if data_gaps:
            out["data_gaps"] = data_gaps
        return out
    except HttpError as e:
        return http_failure(e, "deribit")
    except Exception as e:
        logger.exception("get_options_snapshot failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def get_max_pain(currency: str = "BTC", expiry: Optional[str] = None) -> Dict[str, Any]:
    """Deribit max-pain strike for an expiry (default: nearest Friday expiry; pass e.g. '27DEC26' or
    '2026-12-27'), with notional OI, P/C ratio, biggest OI strikes and distance from the index.
    Read: into large expiries spot often gravitates toward max pain; big OI strikes act as magnets/pins."""
    try:
        cur = _deribit_currency(currency)
        now = _now()
        book = _deribit("get_book_summary_by_currency", {"currency": cur, "kind": "option"}, ttl=120)
        opts = _parse_options(book, now)
        groups = _group_by_expiry(opts)
        exp = _resolve_expiry(groups, expiry)
        if exp is None:
            avail = [c[0]["expiry_code"] for c in groups.values()][:12]
            return {"status": "error", "error": f"expiry '{expiry}' not found" if expiry else "no live expiries",
                    "available_expiries": avail, "source": "deribit"}
        chain = groups[exp]
        index = _index_from_options(chain) or _index_from_options(opts)
        mp = _max_pain(chain)
        call_oi = sum(o["oi"] for o in chain if o["type"] == "C")
        put_oi = sum(o["oi"] for o in chain if o["type"] == "P")

        def top(kind):
            agg: Dict[float, float] = {}
            for o in chain:
                if o["type"] == kind:
                    agg[o["strike"]] = agg.get(o["strike"], 0.0) + o["oi"]
            return [{"strike": k, "oi": _r(v, 1)} for k, v in sorted(agg.items(), key=lambda kv: -kv[1])[:5]]

        dist = _pct(mp, index) if mp and index else None
        notional = (call_oi + put_oi) * index if index else None
        upcoming = [{"expiry": c[0]["expiry_code"], "notional_usd": _usd(sum(o["oi"] for o in c) * index)
                     if index else None} for c in list(groups.values())[:8]]
        if dist is None:
            read = "insufficient data"
        else:
            weight = "large" if notional and notional > 1e9 else "moderate" if notional and notional > 2e8 else "small"
            direction = "above" if dist > 0 else "below"
            read = (f"Max pain {mp:,.0f} is {abs(dist):.1f}% {direction} spot; {weight} expiry "
                    f"(${(notional or 0)/1e6:,.0f}M notional) — pin pull {'meaningful' if weight != 'small' else 'weak'}"
                    " into the 08:00 UTC settlement.")
        return {
            "status": "success", "source": "deribit", "as_of": _iso(), "currency": cur,
            "expiry": chain[0]["expiry_code"], "expiry_utc": _iso(exp),
            "days_to_expiry": _r((exp - now).total_seconds() / 86400, 2),
            "max_pain_strike": mp, "index_price": _r(index), "distance_from_index_pct": dist,
            "call_oi": _r(call_oi, 1), "put_oi": _r(put_oi, 1), "put_call_oi_ratio": _r(put_oi / call_oi, 3)
            if call_oi else None, "notional_oi_usd": _usd(notional),
            "top_call_strikes": top("C"), "top_put_strikes": top("P"),
            "upcoming_expiries": upcoming, "interpretation": read,
        }
    except HttpError as e:
        return http_failure(e, "deribit")
    except Exception as e:
        logger.exception("get_max_pain failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _okx_basis(base: str, now: datetime) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    ex = get_exchange("okx", "swap")
    markets = ex.load_markets()
    dated = [m for m in markets.values() if m.get("future") and m.get("base") == base and m.get("expiry")
             and m["expiry"] > now.timestamp() * 1000]
    inverse = [m for m in dated if m.get("settle") == base]
    dated = sorted(inverse or dated, key=lambda m: m["expiry"])[:8]
    if not dated:
        raise RuntimeError(f"no OKX dated futures for {base}")
    index = None
    try:
        idx = ex.publicGetMarketIndexTickers({"instId": f"{base}-USD"})
        index = _f(idx["data"][0]["idxPx"])
    except Exception:
        index = _f(get_exchange("okx").fetch_ticker(spot_pair(base)).get("last"))
    if not index:
        raise RuntimeError("no OKX index price")
    tickers = ex.fetch_tickers([m["symbol"] for m in dated])
    rows = []
    for m in dated:
        t = tickers.get(m["symbol"]) or {}
        bid, ask = _f(t.get("bid")), _f(t.get("ask"))
        px = (bid + ask) / 2 if bid and ask else _f(t.get("last"))
        if not px:
            continue
        days = (m["expiry"] / 1000 - now.timestamp()) / 86400
        basis = (px - index) / index * 100
        rows.append({"venue": "okx", "contract": m["symbol"], "expiry": _ms_iso(m["expiry"]), "days": _r(days, 1),
                     "price": _r(px), "basis_pct": _r(basis, 3),
                     "annualized_pct": _r(basis * 365 / days, 2) if days >= 1 else None})
    return rows, index


def _deribit_basis(base: str, now: datetime) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    book = _deribit("get_book_summary_by_currency", {"currency": base, "kind": "future"}, ttl=60)
    rows, idxs = [], []
    for r in book or []:
        m = _DERIBIT_FUT.match(str(r.get("instrument_name", "")))
        if not m:
            continue  # perpetual / combos
        exp = _parse_deribit_expiry(m.group(2))
        px, idx = _f(r.get("mark_price")), _f(r.get("estimated_delivery_price"))
        if not exp or exp <= now or not px or not idx:
            continue
        idxs.append(idx)
        days = (exp - now).total_seconds() / 86400
        basis = (px - idx) / idx * 100
        rows.append({"venue": "deribit", "contract": r["instrument_name"], "expiry": _iso(exp), "days": _r(days, 1),
                     "price": _r(px), "basis_pct": _r(basis, 3),
                     "annualized_pct": _r(basis * 365 / days, 2) if days >= 1 else None})
    rows.sort(key=lambda x: x["days"])
    return rows[:8], (statistics.median(idxs) if idxs else None)


@mcp.tool()
def get_basis_term_structure(symbol: str = "BTC") -> Dict[str, Any]:
    """Dated-futures basis vs index (annualized %) across expiries on OKX and Deribit → contango/backwardation.
    Read: annualized basis <0 = backwardation (stress/bearish hedging); 0-5% muted demand; 5-15% healthy
    contango; >15% overheated leveraged longs (cash-and-carry attractive, froth risk)."""
    try:
        base = base_symbol(symbol)
        now = _now()
        curve: List[Dict[str, Any]] = []
        data_gaps: List[str] = []
        index = None
        for name, fn in (("okx", _okx_basis), ("deribit", _deribit_basis)):
            if name == "deribit" and base not in ("BTC", "ETH"):
                continue
            try:
                rows, idx = fn(base, now)
                curve.extend(rows)
                index = index or idx
            except Exception as e:
                data_gaps.append(f"{name}: {e.__class__.__name__}: {str(e)[:120]}")
        if not curve:
            return {"status": "error", "error": "no futures data from any venue", "data_gaps": data_gaps,
                    "source": "okx,deribit"}
        ann = [r["annualized_pct"] for r in curve if r["annualized_pct"] is not None and r["days"] >= 7]
        ann = ann or [r["annualized_pct"] for r in curve if r["annualized_pct"] is not None]
        avg = sum(ann) / len(ann) if ann else None
        if avg is None:
            regime, read = "unknown", "insufficient data"
        elif avg < 0:
            regime, read = "backwardation", "Futures below spot — stress/hedging demand, bearish positioning."
        elif avg < 5:
            regime, read = "flat/muted contango", "Little leveraged long demand — cautious market."
        elif avg < 15:
            regime, read = "healthy contango", "Normal bullish carry; leverage demand present but not extreme."
        else:
            regime, read = "steep contango", "Overheated long leverage — froth; carry trade attractive, flush risk."
        okx_rows = sorted((r for r in curve if r["venue"] == "okx" and r["annualized_pct"] is not None),
                          key=lambda r: r["days"])
        slope = None
        if len(okx_rows) >= 2:
            slope = "rising with tenor" if okx_rows[-1]["annualized_pct"] > okx_rows[0]["annualized_pct"] else \
                "falling with tenor (front richer)"
        out = {"status": "success", "source": ",".join(sorted({r["venue"] for r in curve})), "as_of": _iso(),
               "symbol": base, "index_price": _r(index), "avg_annualized_basis_pct": _r(avg, 2), "regime": regime,
               "slope": slope, "curve": curve, "interpretation": read}
        if data_gaps:
            out["data_gaps"] = data_gaps
        return out
    except Exception as e:
        logger.exception("get_basis_term_structure failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _hl_row(asset: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    funding = _f(ctx.get("funding"))
    mark, oracle = _f(ctx.get("markPx")), _f(ctx.get("oraclePx"))
    oi = _f(ctx.get("openInterest"))
    prev = _f(ctx.get("prevDayPx"))
    premium = _f(ctx.get("premium"))
    if premium is None and mark and oracle:
        premium = (mark - oracle) / oracle
    return {
        "coin": asset.get("name"), "mark_price": mark, "oracle_price": oracle,
        "change_24h_pct": _pct(mark, prev),
        "funding_hourly_pct": _r(funding * 100, 5) if funding is not None else None,
        "funding_annualized_pct": _r(funding * 24 * 365 * 100, 2) if funding is not None else None,
        "open_interest": _r(oi, 2), "open_interest_usd": _usd(oi * (mark or oracle or 0)) if oi is not None else None,
        "volume_24h_usd": _usd(_f(ctx.get("dayNtlVlm"))),
        "premium_pct": _r(premium * 100, 4) if premium is not None else None,
        "max_leverage": asset.get("maxLeverage"),
    }


def _funding_read(ann: Optional[float]) -> str:
    if ann is None:
        return "no funding data"
    if ann > 30:
        return f"funding {ann:.0f}%/yr — longs crowded on-chain"
    if ann < -10:
        return f"funding {ann:.0f}%/yr — shorts crowded, squeeze risk"
    return f"funding {ann:.0f}%/yr — neutral"


@mcp.tool()
def get_hyperliquid_market(symbol: str = "BTC") -> Dict[str, Any]:
    """Hyperliquid perp context: hourly funding (and annualized), OI (coins + USD), mark vs oracle premium,
    24h notional volume. symbol='ALL' returns the top-10 coins by OI. Read: on-chain perps skew retail/degen;
    annualized funding >30% = crowded longs, < -10% = crowded shorts; compare with CEX funding for divergences."""
    try:
        data = _http_post_json(HYPERLIQUID_INFO, {"type": "metaAndAssetCtxs"}, ttl=30)
        if not isinstance(data, list) or len(data) < 2:
            return {"status": "error", "error": "unexpected Hyperliquid response shape", "source": "hyperliquid"}
        universe = (data[0] or {}).get("universe") or []
        rows = [_hl_row(a, c) for a, c in zip(universe, data[1]) if not a.get("isDelisted")]
        if symbol.strip().upper() == "ALL":
            rows.sort(key=lambda r: r["open_interest_usd"] or 0, reverse=True)
            top = rows[:10]
            total_oi = sum(r["open_interest_usd"] or 0 for r in rows)
            hot = [r["coin"] for r in rows[:30] if (r["funding_annualized_pct"] or 0) > 30]
            cold = [r["coin"] for r in rows[:30] if (r["funding_annualized_pct"] or 0) < -10]
            read = f"Total HL OI ${total_oi/1e9:,.2f}B."
            if hot:
                read += f" Crowded longs: {', '.join(hot[:8])}."
            if cold:
                read += f" Crowded shorts: {', '.join(cold[:8])}."
            return {"status": "success", "source": "hyperliquid", "as_of": _iso(), "total_oi_usd": _usd(total_oi),
                    "markets": len(rows), "top_by_oi": top, "interpretation": read}
        base = base_symbol(symbol)
        row = next((r for r in rows if str(r["coin"]).upper() == base), None)
        if row is None:
            return {"status": "error", "error": f"{base} not listed on Hyperliquid", "source": "hyperliquid"}
        read = _funding_read(row["funding_annualized_pct"])
        if row["premium_pct"] is not None and abs(row["premium_pct"]) > 0.05:
            read += f"; mark {'above' if row['premium_pct'] > 0 else 'below'} oracle by {abs(row['premium_pct']):.3f}%"
        return {"status": "success", "source": "hyperliquid", "as_of": _iso(), **row, "interpretation": read}
    except HttpError as e:
        return http_failure(e, "hyperliquid")
    except Exception as e:
        logger.exception("get_hyperliquid_market failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _rubik(path: str, params: Dict[str, Any]) -> List[List[Any]]:
    data = http_get_json(f"{OKX_RUBIK}/{path}", params=params, ttl=60)
    if not isinstance(data, dict) or str(data.get("code")) != "0":
        raise HttpError(f"OKX rubik {path}: {data.get('msg') if isinstance(data, dict) else 'bad response'}",
                        None, path)
    rows = data.get("data") or []
    return sorted(rows, key=lambda r: int(r[0]), reverse=True)  # newest first


@mcp.tool()
def get_okx_positioning(symbol: str = "BTC", period: str = "1H") -> Dict[str, Any]:
    """OKX public positioning stats (Rubik): long/short ACCOUNT ratio, taker buy/sell volume (contracts, USD)
    and contracts OI history. period: 5m | 1H | 1D; changes are over the last 24 periods.
    Read: account L/S > 2 = retail heavily long (contrarian bearish); taker buy/sell > 1.1 = aggressive buyers;
    OI rising with taker buying = conviction longs."""
    try:
        base = base_symbol(symbol)
        if period not in OKX_RUBIK_PERIODS:
            return {"status": "error", "error": f"period must be one of {sorted(OKX_RUBIK_PERIODS)}"}
        n = 24
        out: Dict[str, Any] = {"status": "success", "source": "okx-rubik", "as_of": _iso(), "symbol": base,
                               "period": period}
        gaps: List[str] = []
        reads: List[str] = []
        try:
            ls = _rubik("contracts/long-short-account-ratio", {"ccy": base, "period": period})
            vals = [float(r[1]) for r in ls[:n]]
            if vals:
                out["long_short_account_ratio"] = {"latest": vals[0], "change": _r(vals[0] - vals[-1], 3),
                                                   "min": min(vals), "max": max(vals), "time": _ms_iso(ls[0][0])}
                reads.append("accounts heavily long (contrarian bearish)" if vals[0] > 2 else
                             "accounts net short (contrarian bullish)" if vals[0] < 0.9 else "account L/S balanced")
        except Exception as e:
            gaps.append(f"long_short_ratio: {e}")
        try:
            tv = _rubik("taker-volume", {"ccy": base, "instType": "CONTRACTS", "period": period})
            # OKX docs: [ts, sellVol, buyVol]
            sells = [float(r[1]) for r in tv[:n]]
            buys = [float(r[2]) for r in tv[:n]]
            if sells:
                last_ratio = buys[0] / sells[0] if sells[0] else None
                win_ratio = sum(buys) / sum(sells) if sum(sells) else None
                out["taker_volume"] = {"latest_buy_usd": _usd(buys[0]), "latest_sell_usd": _usd(sells[0]),
                                       "latest_buy_sell_ratio": _r(last_ratio, 3),
                                       f"buy_sell_ratio_{len(sells)}p": _r(win_ratio, 3),
                                       "net_taker_usd": _usd(sum(buys) - sum(sells))}
                if win_ratio is not None:
                    reads.append("aggressive taker buying" if win_ratio > 1.1 else "aggressive taker selling"
                                 if win_ratio < 0.9 else "taker flow balanced")
        except Exception as e:
            gaps.append(f"taker_volume: {e}")
        try:
            oi = _rubik("contracts/open-interest-volume", {"ccy": base, "period": period})
            ois = [float(r[1]) for r in oi[:n]]
            if ois:
                ch = _pct(ois[0], ois[-1])
                out["open_interest"] = {"latest_usd": _usd(ois[0]), f"change_pct_{len(ois)}p": ch,
                                        "volume_latest_usd": _usd(float(oi[0][2])) if len(oi[0]) > 2 else None}
                if ch is not None:
                    reads.append(f"OI {ch:+.1f}% over window")
        except Exception as e:
            gaps.append(f"open_interest: {e}")
        if gaps:
            out["data_gaps"] = gaps
        if len(gaps) == 3:
            return {"status": "error", "error": "all OKX rubik calls failed", "data_gaps": gaps, "source": "okx-rubik"}
        out["interpretation"] = "; ".join(reads) or "insufficient data"
        return out
    except Exception as e:
        logger.exception("get_okx_positioning failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


if __name__ == "__main__":
    mcp.run()
