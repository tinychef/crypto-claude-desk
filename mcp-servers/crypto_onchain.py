#!/usr/bin/env python3
"""Crypto On-chain — valuation and network health from public on-chain data.

Data sources:
- CoinMetrics Community API v4 (free, no key) — MVRV, market cap, NVT, active addresses, tx count
- mempool.space REST API (free) — fees, mempool, hashrate, difficulty adjustment
- blockchain.com charts API (free) — hashrate fallback
- CryptoQuant (paid, Bearer key) / Glassnode (paid, X-Api-Key) — exchange netflows (optional)
"""
import logging
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastmcp import FastMCP

from desk_common import HttpError, base_symbol, get_key, http_get_json, tool_serializer, unavailable

logger = logging.getLogger(__name__)
mcp = FastMCP("crypto-onchain", tool_serializer=tool_serializer)

COINMETRICS_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
MEMPOOL_BASE = "https://mempool.space/api"
BLOCKCHAIN_HASHRATE = "https://api.blockchain.info/charts/hash-rate"
CRYPTOQUANT_NETFLOW = "https://api.cryptoquant.com/v1/{asset}/exchange-flows/netflow"
GLASSNODE_NETFLOW = "https://api.glassnode.com/v1/metrics/transactions/transfers_volume_exchanges_net"

BTC_METRICS_FULL = ["CapMVRVCur", "CapMrktCurUSD", "AdrActCnt", "TxCnt", "NVTAdj"]
BTC_METRICS_CORE = ["CapMVRVCur", "CapMrktCurUSD", "AdrActCnt", "TxCnt"]
ETH_METRICS_FULL = ["AdrActCnt", "TxCnt", "FeeTotNtv", "CapMrktCurUSD", "PriceUSD"]
ETH_METRICS_CORE = ["AdrActCnt", "TxCnt", "CapMrktCurUSD"]

PAID_HINT = ("Exchange netflows need a paid key: CryptoQuant Pro (~$99/mo, DESK_CRYPTOQUANT_KEY) or "
             "Glassnode Professional (~$800+/mo, DESK_GLASSNODE_KEY). Free proxy: get_stablecoin_flows / ETF flows.")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _short(e: Exception, n: int = 160) -> str:
    msg = str(e)
    m = re.search(r"host='([^']+)'", msg)
    if "ProxyError" in msg or "Tunnel connection failed" in msg:
        return f"network blocked ({m.group(1) if m else 'host'} not reachable via proxy)"
    return msg[:n]


def _r(x: Optional[float], n: int = 2) -> Optional[float]:
    return None if x is None else round(float(x), n)


def _f(v: Any) -> Optional[float]:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _mean(xs: List[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or not b:
        return None
    return (a / b - 1) * 100


# ---------------------------------------------------------------------------
# CoinMetrics helpers
# ---------------------------------------------------------------------------

def _coinmetrics_rows(asset: str, metrics_options: List[List[str]], days: int) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Fetch daily rows; if a metric isn't in the community tier (HTTP 400/403), retry with fewer."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    last_err: Optional[HttpError] = None
    for metrics in metrics_options:
        params = {"assets": asset, "metrics": ",".join(metrics), "frequency": "1d",
                  "start_time": start, "page_size": 10000}
        try:
            data = http_get_json(COINMETRICS_URL, params=params, ttl=3600, retries=1, timeout=25)
            rows = data.get("data", []) if isinstance(data, dict) else []
            rows.sort(key=lambda r: r.get("time", ""))
            return rows, metrics
        except HttpError as e:
            last_err = e
            if e.status not in (400, 403):
                break
    raise last_err or HttpError("CoinMetrics: no data")


def _series(rows: List[Dict[str, Any]], metric: str) -> List[Optional[float]]:
    return [_f(r.get(metric)) for r in rows]


def _trend(series: List[Optional[float]]) -> Dict[str, Any]:
    """7d avg vs previous 30d avg, and 30d avg vs previous 30d avg."""
    last7 = _mean(series[-7:])
    prev30 = _mean(series[-37:-7])
    last30 = _mean(series[-30:])
    prior30 = _mean(series[-60:-30])
    return {"avg_7d": _r(last7, 0), "avg_30d": _r(last30, 0),
            "chg_7d_vs_prev30d_pct": _r(_pct(last7, prev30), 1),
            "chg_30d_vs_prior30d_pct": _r(_pct(last30, prior30), 1)}


def _mvrv_zone(mvrv: float) -> str:
    if mvrv < 1:
        return "deep value (price below realized cap: capitulation zone)"
    if mvrv < 1.5:
        return "undervalued / accumulation"
    if mvrv < 2.5:
        return "neutral / fair value"
    if mvrv < 3.5:
        return "elevated (late-bull territory)"
    return "overheated (historical cycle-top zone)"


@mcp.tool()
def get_btc_valuation() -> Dict[str, Any]:
    """BTC on-chain valuation from CoinMetrics Community: MVRV (+ ~4y percentile), realized cap,
    NUPL proxy (1 - 1/MVRV), NVT, active addresses and tx count trends (7d/30d).

    Zones: MVRV < 1 deep value, 1-1.5 accumulation, 1.5-2.5 fair, 2.5-3.5 elevated, > 3.5 overheated.
    Rising active addresses alongside price = healthy organic demand.
    """
    try:
        rows, used = _coinmetrics_rows("btc", [BTC_METRICS_FULL, BTC_METRICS_CORE], days=1500)
        mvrv_s = _series(rows, "CapMVRVCur")
        mvrv_vals = [v for v in mvrv_s if v is not None]
        if not mvrv_vals:
            return {"status": "error", "error": "CoinMetrics returned no MVRV data", "source": "coinmetrics"}
        mvrv = mvrv_vals[-1]
        mcap = _f(rows[-1].get("CapMrktCurUSD")) if rows else None
        last_time = next((r.get("time") for r in reversed(rows) if _f(r.get("CapMVRVCur")) is not None), None)
        pctile = sum(1 for v in mvrv_vals if v <= mvrv) / len(mvrv_vals) * 100
        mvrv_30d_ago = mvrv_vals[-31] if len(mvrv_vals) > 30 else None
        realized = mcap / mvrv if mcap and mvrv else None
        nupl = 1 - 1 / mvrv if mvrv else None
        out: Dict[str, Any] = {
            "status": "success", "source": "coinmetrics-community", "as_of": _now_iso(),
            "data_date": (last_time or "")[:10],
            "mvrv": _r(mvrv, 3),
            "mvrv_30d_ago": _r(mvrv_30d_ago, 3),
            "mvrv_percentile": _r(pctile, 1),
            "percentile_window_days": len(mvrv_vals),
            "market_cap_b": _r(mcap / 1e9, 1) if mcap else None,
            "realized_cap_b": _r(realized / 1e9, 1) if realized else None,
            "nupl_proxy": _r(nupl, 3),
            "zone": _mvrv_zone(mvrv),
        }
        if "NVTAdj" in used:
            nvt = [v for v in _series(rows, "NVTAdj") if v is not None]
            if nvt:
                med90 = statistics.median(nvt[-90:])
                out["nvt_adj"] = _r(nvt[-1], 1)
                out["nvt_adj_90d_median"] = _r(med90, 1)
        else:
            out["nvt_adj"] = None
            out["note"] = "NVTAdj not available in the community tier; omitted."
        out["active_addresses"] = _trend(_series(rows, "AdrActCnt"))
        out["tx_count"] = _trend(_series(rows, "TxCnt"))
        aa = out["active_addresses"]["chg_7d_vs_prev30d_pct"]
        aa_txt = f"active addresses 7d {aa:+.1f}% vs prior 30d" if aa is not None else "active-address trend n/a"
        out["interpretation"] = (f"MVRV {mvrv:.2f} ({pctile:.0f}th pct of ~{len(mvrv_vals) // 365}y): {out['zone']}; "
                                 f"{aa_txt}.")
        return out
    except HttpError as e:
        if e.status is None:
            return unavailable("coinmetrics", _short(e),
                               hint="Allow community-api.coinmetrics.io in the network settings (free, no key).")
        return {"status": "error", "error": _short(e), "source": "coinmetrics"}
    except Exception as e:
        logger.exception("get_btc_valuation failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


# ---------------------------------------------------------------------------
# mempool.space
# ---------------------------------------------------------------------------

def _hashrate_from_mempool() -> Dict[str, Any]:
    data = http_get_json(f"{MEMPOOL_BASE}/v1/mining/hashrate/3m", ttl=3600, retries=1)
    hr = [_f(h.get("avgHashrate")) for h in data.get("hashrates", [])]
    hr = [h for h in hr if h]
    cur = _f(data.get("currentHashrate")) or (hr[-1] if hr else None)
    last7 = _mean(hr[-7:])
    prev = _mean(hr[-37:-30]) if len(hr) >= 37 else _mean(hr[:7])
    return {"source": "mempool.space", "current_eh_s": _r(cur / 1e18, 1) if cur else None,
            "avg_7d_eh_s": _r(last7 / 1e18, 1) if last7 else None,
            "chg_7d_vs_30d_ago_pct": _r(_pct(last7, prev), 2),
            "current_difficulty_t": _r(_f(data.get("currentDifficulty")) / 1e12, 2) if data.get("currentDifficulty") else None}


def _hashrate_from_blockchain() -> Dict[str, Any]:
    data = http_get_json(BLOCKCHAIN_HASHRATE, params={"timespan": "3months", "format": "json"}, ttl=3600, retries=1)
    vals = [_f(v.get("y")) for v in data.get("values", [])]
    vals = [v for v in vals if v]
    # blockchain.com reports TH/s
    last7 = _mean(vals[-7:])
    prev = _mean(vals[-37:-30]) if len(vals) >= 37 else _mean(vals[:7])
    return {"source": "blockchain.com", "current_eh_s": _r(vals[-1] / 1e6, 1) if vals else None,
            "avg_7d_eh_s": _r(last7 / 1e6, 1) if last7 else None,
            "chg_7d_vs_30d_ago_pct": _r(_pct(last7, prev), 2)}


@mcp.tool()
def get_network_health() -> Dict[str, Any]:
    """Bitcoin network health: recommended fees (sat/vB), mempool backlog, hashrate trend and the next
    difficulty adjustment (mempool.space; blockchain.com fallback for hashrate).

    Read: rising hashrate = miner confidence; falling hashrate + negative adjustment = miner stress
    (possible miner selling). High fees/backlog = on-chain demand spike (often during volatility).
    """
    out: Dict[str, Any] = {"status": "success", "source": "mempool.space", "as_of": _now_iso()}
    errors: List[str] = []
    try:
        try:
            fees = http_get_json(f"{MEMPOOL_BASE}/v1/fees/recommended", ttl=60, retries=1)
            out["fees_sat_vb"] = {k: fees.get(k) for k in ("fastestFee", "halfHourFee", "hourFee", "economyFee", "minimumFee")}
        except HttpError as e:
            errors.append(f"fees: {_short(e)}")
        try:
            mp = http_get_json(f"{MEMPOOL_BASE}/mempool", ttl=60, retries=1)
            out["mempool"] = {"tx_count": mp.get("count"), "vsize_mvb": _r((mp.get("vsize") or 0) / 1e6, 1),
                              "total_fee_btc": _r((mp.get("total_fee") or 0) / 1e8, 3)}
        except HttpError as e:
            errors.append(f"mempool: {_short(e)}")
        try:
            da = http_get_json(f"{MEMPOOL_BASE}/v1/difficulty-adjustment", ttl=600, retries=1)
            eta = da.get("estimatedRetargetDate")
            out["difficulty_adjustment"] = {
                "progress_pct": _r(da.get("progressPercent"), 1),
                "estimated_change_pct": _r(da.get("difficultyChange"), 2),
                "previous_change_pct": _r(da.get("previousRetarget"), 2),
                "remaining_blocks": da.get("remainingBlocks"),
                "eta": datetime.fromtimestamp(eta / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if eta else None,
            }
        except HttpError as e:
            errors.append(f"difficulty: {_short(e)}")
        try:
            out["hashrate"] = _hashrate_from_mempool()
        except HttpError as e:
            errors.append(f"hashrate(mempool): {_short(e)}")
            try:
                out["hashrate"] = _hashrate_from_blockchain()
            except HttpError as e2:
                errors.append(f"hashrate(blockchain.com): {_short(e2)}")
        sections = [k for k in ("fees_sat_vb", "mempool", "difficulty_adjustment", "hashrate") if k in out]
        if not sections:
            return unavailable("mempool.space", "; ".join(errors),
                               hint="Allow mempool.space (and api.blockchain.info) in the network settings (free, no key).")
        bits = []
        if "fees_sat_vb" in out:
            fast = out["fees_sat_vb"].get("fastestFee")
            bits.append(f"fast fee {fast} sat/vB ({'congested' if (fast or 0) > 50 else 'calm'})")
        if "hashrate" in out and out["hashrate"].get("chg_7d_vs_30d_ago_pct") is not None:
            ch = out["hashrate"]["chg_7d_vs_30d_ago_pct"]
            bits.append(f"hashrate {ch:+.1f}% vs 30d ago ({'miner confidence' if ch > 0 else 'miner stress'})")
        if "difficulty_adjustment" in out and out["difficulty_adjustment"].get("estimated_change_pct") is not None:
            bits.append(f"next difficulty adj {out['difficulty_adjustment']['estimated_change_pct']:+.1f}%")
        out["errors"] = errors or None
        out["interpretation"] = "; ".join(bits) + "." if bits else "Partial data."
        return out
    except Exception as e:
        logger.exception("get_network_health failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


# ---------------------------------------------------------------------------
# Paid: exchange netflows
# ---------------------------------------------------------------------------

def _cryptoquant_netflows(asset: str, days: int) -> List[Dict[str, Any]]:
    headers = {}
    key = get_key("CRYPTOQUANT")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = http_get_json(CRYPTOQUANT_NETFLOW.format(asset=asset.lower()),
                         params={"exchange": "all_exchange", "window": "day", "limit": days},
                         headers=headers, ttl=3600, retries=1)
    rows = ((data or {}).get("result") or {}).get("data") or []
    out = []
    for r in rows:
        v = _f(r.get("netflow_total", r.get("netflow")))
        if v is not None:
            out.append({"date": str(r.get("date", r.get("datetime", "")))[:10], "netflow": v})
    out.sort(key=lambda r: r["date"])
    return out[-days:]


def _glassnode_netflows(asset: str, days: int) -> List[Dict[str, Any]]:
    headers = {}
    key = get_key("GLASSNODE")
    if key:
        headers["X-Api-Key"] = key
    since = int((datetime.now(timezone.utc) - timedelta(days=days + 1)).timestamp())
    data = http_get_json(GLASSNODE_NETFLOW, params={"a": asset.upper(), "i": "24h", "s": since},
                         headers=headers, ttl=3600, retries=1)
    out = []
    for r in data or []:
        v = _f(r.get("v"))
        if v is not None and r.get("t") is not None:
            out.append({"date": datetime.fromtimestamp(int(r["t"]), timezone.utc).strftime("%Y-%m-%d"), "netflow": v})
    return out[-days:]


@mcp.tool()
def get_exchange_netflows(asset: str = "BTC", days: int = 7) -> Dict[str, Any]:
    """Exchange netflows (coins in minus out of all exchanges), PAID providers only: CryptoQuant
    (Bearer key) then Glassnode (X-Api-Key). Returns `unavailable` with cost hint when no key works.

    Read: sustained net OUTFLOWS = coins moving to cold storage (accumulation, bullish);
    net INFLOWS = coins moved to exchanges to sell (distribution, bearish).
    """
    try:
        sym = base_symbol(asset)
        days = max(1, min(int(days), 90))
        errors = []
        for name, fn in (("cryptoquant", _cryptoquant_netflows), ("glassnode", _glassnode_netflows)):
            try:
                rows = fn(sym, days)
            except HttpError as e:
                errors.append(f"{name}: {_short(e)}")
                continue
            if not rows:
                errors.append(f"{name}: empty")
                continue
            total = sum(r["netflow"] for r in rows)
            outflow_days = sum(1 for r in rows if r["netflow"] < 0)
            read = ("net outflows: accumulation / supply leaving exchanges (bullish)" if total < 0
                    else "net inflows: coins moving to exchanges (sell pressure, bearish)")
            return {
                "status": "success", "source": name, "as_of": _now_iso(), "asset": sym, "unit": sym,
                "daily": [{"date": r["date"], "netflow": _r(r["netflow"], 1)} for r in rows],
                "sum": _r(total, 1), "outflow_days": outflow_days, "days": len(rows),
                "interpretation": f"{sym} exchange netflow {total:+,.0f} {sym} over {len(rows)}d ({outflow_days} outflow days): {read}.",
            }
        return unavailable("cryptoquant/glassnode", "; ".join(errors) or "no paid key", hint=PAID_HINT)
    except Exception as e:
        logger.exception("get_exchange_netflows failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def get_eth_activity() -> Dict[str, Any]:
    """ETH network activity from CoinMetrics Community: active addresses, tx count and total fees
    (ETH) with 7d-vs-30d trends. Rising activity + fees = real usage demand (supports ETH);
    falling fees = weaker burn (more net issuance).
    """
    try:
        rows, used = _coinmetrics_rows("eth", [ETH_METRICS_FULL, ETH_METRICS_CORE], days=120)
        if not rows:
            return {"status": "error", "error": "CoinMetrics returned no ETH data", "source": "coinmetrics"}
        out: Dict[str, Any] = {
            "status": "success", "source": "coinmetrics-community", "as_of": _now_iso(),
            "data_date": str(rows[-1].get("time", ""))[:10],
            "active_addresses": _trend(_series(rows, "AdrActCnt")),
            "tx_count": _trend(_series(rows, "TxCnt")),
        }
        if "FeeTotNtv" in used:
            fees = _series(rows, "FeeTotNtv")
            t = _trend(fees)
            out["fees_eth"] = {"avg_7d": _r(_mean(fees[-7:]), 1), "avg_30d": _r(_mean(fees[-30:]), 1),
                               "chg_7d_vs_prev30d_pct": t["chg_7d_vs_prev30d_pct"]}
        aa = out["active_addresses"]["chg_7d_vs_prev30d_pct"]
        tx = out["tx_count"]["chg_7d_vs_prev30d_pct"]
        fee = (out.get("fees_eth") or {}).get("chg_7d_vs_prev30d_pct")
        parts = [f"active addresses {aa:+.1f}%" if aa is not None else None,
                 f"tx {tx:+.1f}%" if tx is not None else None,
                 f"fees {fee:+.1f}%" if fee is not None else None]
        trend = "rising usage" if (aa or 0) > 3 else "falling usage" if (aa or 0) < -3 else "stable usage"
        out["interpretation"] = f"ETH 7d vs prior 30d: {', '.join(p for p in parts if p)} -> {trend}."
        return out
    except HttpError as e:
        if e.status is None:
            return unavailable("coinmetrics", _short(e),
                               hint="Allow community-api.coinmetrics.io in the network settings (free, no key).")
        return {"status": "error", "error": _short(e), "source": "coinmetrics"}
    except Exception as e:
        logger.exception("get_eth_activity failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


if __name__ == "__main__":
    mcp.run()
