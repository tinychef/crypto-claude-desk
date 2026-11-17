#!/usr/bin/env python3
"""Crypto Quant — local quant engine on exchange OHLCV: regime, breadth, relative strength,
sector rotation, behavioral base rates, seasonality, correlation/beta and volatility regime.

Data sources: OKX spot OHLCV via ccxt (free, fallback Kraken/others through
desk_common.fetch_with_fallback); CoinGecko /coins/categories (free, optional demo key)
for sector_rotation. All statistics are computed locally and deterministically.

Tools:
1. classify_regime        - trend/vol regime label with the rule that produced it + 4h/1d/1w agreement
2. market_breadth         - % above MA20/50/200, RSI extremes, new highs/lows, dispersion, thrust, score
3. relative_strength_scan - alt screener vs BTC: leaders / oversold-quality / laggards + compact table
4. sector_rotation        - CoinGecko categories 24h + static sector map 7d/30d momentum from OKX
5. behavior_base_rates    - forward returns after a condition (RSI, drawdown, red streak, MA200, vol spike...)
6. seasonality            - day-of-week, hour-of-day and month-of-year return stats
7. correlation_matrix     - daily return correlations, avg pairwise corr, beta/corr to BTC, clusters
8. volatility_regime      - realized vol 7/30/90d, vol-of-vol, percentile, ATR% and stop distances
9. beta_to_btc            - single-symbol beta/correlation to BTC (full, upside, downside)
"""
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from fastmcp import FastMCP

from desk_common import (
    tool_serializer, http_get_json, HttpError, get_key, base_symbol, spot_pair,
    fetch_with_fallback,
)

logger = logging.getLogger(__name__)
mcp = FastMCP("crypto-quant", tool_serializer=tool_serializer)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OHLCV_TTL = 120.0          # seconds, in-process cache
UNIVERSE_TTL = 300.0
SCAN_BARS = 300            # daily bars used by breadth / RS / correlation (one OKX page)
REGIME_BARS = 420          # daily bars for regime / vol (1y percentile of 30d vol)
HISTORY_BARS = 2000        # ~5.5y daily for base rates / seasonality
PAGE_SIZE = {"okx": 300, "kraken": 720, "kucoin": 1500, "mexc": 1000, "coinbase": 300, "bitfinex": 10000}
BARS_PER_DAY = {"1h": 24.0, "4h": 6.0, "1d": 1.0, "1w": 1.0 / 7.0}
COINGECKO_CATEGORIES = "https://api.coingecko.com/api/v3/coins/categories"
ANOMALY_24H_PCT = 25.0

STABLECOINS = {
    "USDT", "USDC", "FDUSD", "DAI", "TUSD", "USDE", "PYUSD", "USDG", "RLUSD", "USDD", "USDP",
    "BUSD", "EURC", "EURT", "EUR", "USD1", "USDS", "FRAX", "GUSD", "LUSD", "SUSD", "USDQ",
    "USDR", "AEUR", "BFUSD", "XUSD", "USTC", "UST", "CUSD", "USDX", "USDB", "USDJ", "USDK",
    "SUSDE", "DOLA", "GHO", "CRVUSD", "MIM", "EURI", "EUROC", "BRZ", "TRYB", "IDRT", "BIDR",
}
WRAPPED = {
    "WBTC", "WETH", "WBETH", "STETH", "WSTETH", "CBBTC", "BTCB", "WEETH", "EETH", "RETH", "METH",
    "CMETH", "WBNB", "WSOL", "JITOSOL", "MSOL", "BNSOL", "BETH", "TBTC", "LBTC", "SOLVBTC",
    "WTRX", "WAVAX", "WMATIC", "WPOL", "EZETH", "RSETH", "OSETH", "SAVAX", "STSOL", "JUPSOL",
}
COMMODITY_TOKENS = {"XAUT", "PAXG", "KAU", "KAG", "DGX"}
_LEVERAGED_RE = re.compile(r"^[A-Z0-9]+(\d+[LS]|BULL|BEAR|HEDGE)$")
_UPDOWN_RE = re.compile(r"^(BTC|ETH|BNB|XRP|ADA|DOT|LINK|SOL|LTC|TRX|XLM|FIL|EOS|AAVE|SUSHI|UNI|YFI|BCH)(UP|DOWN)$")

# Static sector map — used for OKX momentum (always) and as fallback when CoinGecko fails.
SECTOR_MAP: Dict[str, List[str]] = {
    "L1": ["ETH", "SOL", "ADA", "AVAX", "SUI", "NEAR", "APT", "TON", "DOT", "TRX", "ICP", "SEI"],
    "L2": ["ARB", "OP", "POL", "STRK", "MNT", "IMX", "ZK"],
    "DeFi": ["UNI", "AAVE", "HYPE", "LDO", "CRV", "PENDLE", "ENA", "COMP", "JUP", "DYDX"],
    "AI": ["TAO", "FET", "RENDER", "WLD", "GRT", "VIRTUAL", "IO"],
    "Meme": ["DOGE", "SHIB", "PEPE", "BONK", "WIF", "FLOKI", "TRUMP", "PENGU", "PUMP"],
    "Exchange": ["BNB", "OKB", "CRO", "ASTER"],
    "Privacy": ["ZEC", "DASH", "XMR", "ZEN"],
    "Gaming": ["AXS", "SAND", "MANA", "GALA", "APE", "BEAM"],
    "RWA": ["ONDO", "OM", "PLUME", "POLYX", "LINK"],
    "Payments": ["XRP", "XLM", "LTC", "BCH"],
}
_CATEGORY_EXCLUDE = ("portfolio", "stablecoin", "tokenized", "gold", "bridged", "wrapped",
                     "fiat", "holdings", "alleged", "made in", "yield-bearing", "us election")

# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _num(x) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _r(x, nd: int = 2) -> Optional[float]:
    f = _num(x)
    return None if f is None else round(f, nd)


def _px(x) -> Optional[float]:
    """Round a price to ~5 significant figures (works for sub-cent tokens)."""
    f = _num(x)
    if f is None or f == 0:
        return f
    return round(f, max(0, 4 - int(math.floor(math.log10(abs(f))))))


def _pct(a, b) -> Optional[float]:
    a, b = _num(a), _num(b)
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1.0) * 100.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(ts) -> str:
    try:
        return pd.Timestamp(ts).isoformat()
    except Exception:
        return str(ts)


def _parse_symbols(symbols) -> Optional[List[str]]:
    if symbols is None:
        return None
    if isinstance(symbols, str):
        symbols = [s for s in re.split(r"[,\s]+", symbols) if s]
    out: List[str] = []
    for s in symbols:
        b = base_symbol(str(s))
        if b not in out:
            out.append(b)
    return out or None


def _bars(days: float, timeframe: str) -> int:
    return max(1, int(round(days * BARS_PER_DAY.get(timeframe, 1.0))))


def _ann_factor(timeframe: str) -> float:
    return math.sqrt(365.0 * BARS_PER_DAY.get(timeframe, 1.0))


# ---------------------------------------------------------------------------
# Indicators (computed locally)
# ---------------------------------------------------------------------------


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.where(~((loss == 0) & (gain > 0)), 100.0)
    return rsi


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _true_range(df).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _adx(df: pd.DataFrame, n: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    atr = _atr(df, n)
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean() / atr
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denom
    adx = dx.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    return adx, plus_di, minus_di


def _realized_vol(close: pd.Series, window: int, ann: float) -> pd.Series:
    lr = np.log(close / close.shift(1))
    return lr.rolling(window, min_periods=max(2, int(window * 0.8))).std() * ann * 100.0


def _percentile_of_last(series: pd.Series, lookback: int) -> Optional[float]:
    s = series.dropna().iloc[-lookback:]
    if len(s) < 20:
        return None
    last = s.iloc[-1]
    return float((s <= last).mean() * 100.0)


def _ret(close: pd.Series, bars: int) -> Optional[float]:
    if len(close) <= bars:
        return None
    return _pct(close.iloc[-1], close.iloc[-1 - bars])


def _structure(df: pd.DataFrame, window: int = 20) -> str:
    """Compare the last `window` bars with the prior `window`: HH+HL / LH+LL / mixed."""
    if len(df) < 2 * window:
        return "unknown"
    prev, last = df.iloc[-2 * window:-window], df.iloc[-window:]
    hh = last["high"].max() > prev["high"].max()
    hl = last["low"].min() > prev["low"].min()
    if hh and hl:
        return "HH+HL"
    if not hh and not hl:
        return "LH+LL"
    return "mixed"


# ---------------------------------------------------------------------------
# OHLCV access with in-process cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_OHLCV_CACHE: Dict[Tuple[str, str, int], Tuple[float, pd.DataFrame, str]] = {}
_UNIVERSE_CACHE: Dict[int, Tuple[float, List[str], str]] = {}
_MARKETS_CACHE: Dict[str, Tuple[float, set]] = {}


def _clear_cache() -> None:
    with _cache_lock:
        _OHLCV_CACHE.clear()
        _UNIVERSE_CACHE.clear()
        _MARKETS_CACHE.clear()


def _paged_ohlcv(ex, pair: str, timeframe: str, limit: int) -> list:
    """Fetch up to `limit` most recent candles, paging backwards with ``until`` when needed.

    Paging backwards (not forwards from ``since``) works for recently listed coins:
    OKX returns nothing when ``since`` predates the listing.
    """
    per = PAGE_SIZE.get(getattr(ex, "id", ""), 300)
    rows = ex.fetch_ohlcv(pair, timeframe, limit=min(limit, per)) or []
    guard = int(math.ceil(limit / per)) + 2
    while rows and len(rows) < limit and guard > 0:
        guard -= 1
        earliest = rows[0][0]
        params: Dict[str, Any] = {"until": earliest - 1}
        if getattr(ex, "id", "") == "okx":
            params["type"] = "HistoryCandles"  # /market/candles only serves the latest 1440 bars
        try:
            older = ex.fetch_ohlcv(pair, timeframe, limit=per, params=params) or []
        except Exception:
            break
        older = [r for r in older if r[0] < earliest]
        if not older:
            break
        rows = older + rows
    dedup = {r[0]: r for r in rows}
    return [dedup[k] for k in sorted(dedup)][-limit:]


def _to_df(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop(columns=["ts"]).astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _fetch_ohlcv(symbol: str, timeframe: str = "1d", limit: int = SCAN_BARS) -> Tuple[pd.DataFrame, str]:
    """Return (DataFrame[open,high,low,close,volume] indexed by UTC time, exchange).

    Cached in-process for OHLCV_TTL seconds keyed by (symbol, timeframe, limit).
    Raises RuntimeError if no exchange returns data.
    """
    sym = base_symbol(symbol)
    key = (sym, timeframe, int(limit))
    now = time.time()
    with _cache_lock:
        hit = _OHLCV_CACHE.get(key)
        if hit and now - hit[0] < OHLCV_TTL:
            return hit[1], hit[2]
    pair = spot_pair(sym)
    rows, exchange, _errors = fetch_with_fallback(lambda ex: _paged_ohlcv(ex, pair, timeframe, int(limit)))
    df = _to_df(rows)
    with _cache_lock:
        _OHLCV_CACHE[key] = (time.time(), df, exchange)
    return df, exchange


def _fetch_many(symbols: List[str], timeframe: str = "1d", limit: int = SCAN_BARS,
                workers: int = 4) -> Tuple[Dict[str, pd.DataFrame], Dict[str, str], List[str]]:
    """Fetch several symbols concurrently. Returns (data, exchange_by_symbol, skipped)."""
    data: Dict[str, pd.DataFrame] = {}
    venues: Dict[str, str] = {}
    skipped: List[str] = []

    def one(sym):
        try:
            df, ex = _fetch_ohlcv(sym, timeframe, limit)
            return sym, df, ex
        except Exception as e:  # one bad symbol must not kill a scan
            logger.debug("fetch %s failed: %s", sym, e)
            return sym, None, None

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(symbols) or 1))) as pool:
        for sym, df, ex in pool.map(one, symbols):
            if df is None or len(df) < 2:
                skipped.append(sym)
            else:
                data[sym], venues[sym] = df, ex
    return data, venues, skipped


def _closed(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Drop the last bar if it is still forming (for statistics on completed candles)."""
    if df.empty:
        return df
    bar = pd.Timedelta(hours=24.0 / BARS_PER_DAY.get(timeframe, 1.0))
    if df.index[-1] + bar > pd.Timestamp.now(tz="UTC"):
        return df.iloc[:-1]
    return df


def _okx_bases() -> Optional[set]:
    """Base symbols with a USDT spot market on OKX (cached). None if unavailable."""
    now = time.time()
    with _cache_lock:
        hit = _MARKETS_CACHE.get("okx")
        if hit and now - hit[0] < 3600:
            return hit[1]
    try:
        markets, _, _ = fetch_with_fallback(lambda ex: ex.load_markets(), exchanges=["okx"])
        bases = {m.get("base") for s, m in markets.items() if s.endswith("/USDT") and m.get("spot", True)}
    except Exception:
        return None
    with _cache_lock:
        _MARKETS_CACHE["okx"] = (time.time(), bases)
    return bases


def _is_excluded_base(base: str) -> bool:
    b = base.upper()
    if b in STABLECOINS or b in WRAPPED or b in COMMODITY_TOKENS:
        return True
    if b.startswith("USD") or b.endswith("USD"):
        return True
    if b != "BTC" and b.endswith("BTC"):
        return True
    if _LEVERAGED_RE.match(b) or _UPDOWN_RE.match(b):
        return True
    return False


def _default_universe(n: int = 30) -> Tuple[List[str], str]:
    """Top-n liquid USDT spot pairs by 24h quote volume (stables/wrapped/leveraged/stocks excluded)."""
    now = time.time()
    with _cache_lock:
        hit = _UNIVERSE_CACHE.get(n)
        if hit and now - hit[0] < UNIVERSE_TTL:
            return hit[1], hit[2]

    def build(ex):
        tickers = ex.fetch_tickers()
        markets = getattr(ex, "markets", None) or {}
        ranked = []
        for sym, t in tickers.items():
            if not isinstance(sym, str) or not sym.endswith("/USDT"):
                continue
            base = sym.split("/")[0]
            if _is_excluded_base(base):
                continue
            info = (markets.get(sym) or {}).get("info") or {}
            # OKX instCategory: 1 = crypto, 3 = tokenized stocks, 4 = commodities
            if str(info.get("instCategory", "1") or "1") != "1":
                continue
            qv = _num(t.get("quoteVolume"))
            if qv is None:
                bv, last = _num(t.get("baseVolume")), _num(t.get("last"))
                qv = bv * last if bv is not None and last is not None else None
            if qv:
                ranked.append((base, qv))
        ranked.sort(key=lambda x: -x[1])
        return [b for b, _ in ranked]

    ranked, venue, _ = fetch_with_fallback(build)
    universe = ranked[:n]
    if "BTC" not in universe:
        universe = ["BTC"] + universe[: max(0, n - 1)]
    with _cache_lock:
        _UNIVERSE_CACHE[n] = (time.time(), universe, venue)
    return universe, venue


def _resolve_universe(symbols, n: int = 30, include: Optional[str] = None) -> Tuple[List[str], str]:
    parsed = _parse_symbols(symbols)
    if parsed:
        uni, how = parsed, "explicit"
    else:
        uni, venue = _default_universe(n)
        how = f"top{n}_by_24h_volume_{venue}"
    if include and include not in uni:
        uni = [include] + uni
    return uni, how


# ---------------------------------------------------------------------------
# Regime core
# ---------------------------------------------------------------------------


def _regime_metrics(df: pd.DataFrame, timeframe: str = "1d") -> Dict[str, Any]:
    close = df["close"]
    last = float(close.iloc[-1])
    out: Dict[str, Any] = {"price": last}
    ma = {}
    for n in (20, 50, 200):
        s = close.rolling(n).mean()
        val = s.iloc[-1] if len(s) else np.nan
        slope = _pct(s.iloc[-1], s.iloc[-6]) if len(s) >= n + 5 else None
        ma[n] = {"value": _num(val), "dist_pct": _pct(last, val), "slope5_pct": slope}
    out["ma"] = ma
    adx, pdi, mdi = _adx(df)
    out["adx"], out["plus_di"], out["minus_di"] = _num(adx.iloc[-1]), _num(pdi.iloc[-1]), _num(mdi.iloc[-1])
    out["rsi"] = _num(_rsi(close).iloc[-1])
    out["structure"] = _structure(df)
    ann = _ann_factor(timeframe)
    w30 = _bars(30, timeframe)
    rv30 = _realized_vol(close, w30, ann)
    out["rv30"] = _num(rv30.iloc[-1])
    out["rv30_pctile"] = _percentile_of_last(rv30, _bars(365, timeframe))
    atr = _atr(df)
    out["atr_pct"] = _num(atr.iloc[-1] / last * 100.0) if last else None
    for days in (90, 365):
        nb = min(len(df), _bars(days, timeframe))
        out[f"dd{days}"] = _pct(last, df["high"].iloc[-nb:].max())
    out["ret7"] = _ret(close, _bars(7, timeframe))
    out["ret30"] = _ret(close, _bars(30, timeframe))
    label, rule, score, max_score = _label_regime(out)
    out.update({"label": label, "rule": rule, "trend_score": score, "trend_max": max_score})
    return out


def _label_regime(m: Dict[str, Any]) -> Tuple[str, str, int, int]:
    rsi, dd90, ret7, ret30 = m.get("rsi"), m.get("dd90"), m.get("ret7"), m.get("ret30")
    vp = m.get("rv30_pctile")
    d50 = m["ma"][50]["dist_pct"]
    # trend score
    def sgn(v, flat):  # small deviations count as flat (0) so ranges don't score as trends
        return 0 if abs(v) < flat else (1 if v > 0 else -1)

    comps = []
    if d50 is not None:
        comps.append(sgn(d50, 2.0))
    if m["ma"][50]["value"] is not None and m["ma"][200]["value"] is not None:
        comps.append(sgn(_pct(m["ma"][50]["value"], m["ma"][200]["value"]) or 0.0, 1.5))
    s50 = m["ma"][50]["slope5_pct"]
    if s50 is not None:
        comps.append(sgn(s50, 0.3))
    if m.get("plus_di") is not None and m.get("minus_di") is not None:
        comps.append(1 if m["plus_di"] > m["minus_di"] else -1)
    st = m.get("structure")
    if st in ("HH+HL", "LH+LL", "mixed"):
        comps.append({"HH+HL": 1, "LH+LL": -1, "mixed": 0}[st])
    score, mx = int(sum(comps)), len(comps)
    adx = m.get("adx") or 0.0

    def f(x, nd=1):
        return "n/a" if x is None else f"{x:.{nd}f}"

    if (rsi is not None and rsi <= 30 and dd90 is not None and dd90 <= -20
            and ((ret7 is not None and ret7 <= -10) or (vp is not None and vp >= 80))):
        return ("capitulation",
                f"RSI {f(rsi)}<=30 & DD90 {f(dd90)}%<=-20% & (7d {f(ret7)}%<=-10% or vol pctile {f(vp, 0)}>=80)",
                score, mx)
    if rsi is not None and rsi >= 75 and d50 is not None and d50 >= 15 and ret30 is not None and ret30 >= 25:
        return ("euphoria", f"RSI {f(rsi)}>=75 & {f(d50)}% above MA50 (>=15) & 30d {f(ret30)}%>=25%", score, mx)
    base = (f"trend score {score:+d}/{mx} (price vs MA50 ±2%, MA50 vs MA200 ±1.5%, MA50 5-bar slope ±0.3%, "
            f"DI, HH/HL structure), ADX {f(adx)}")
    if adx < 15:
        return "range", base + " — ADX<15: no trend", score, mx
    if adx >= 25 and score >= 4:
        return "strong_uptrend", base + " — ADX>=25 & score>=4", score, mx
    if adx >= 25 and score <= -4:
        return "strong_downtrend", base + " — ADX>=25 & score<=-4", score, mx
    if score >= 2:
        return "uptrend", base + " — score>=2", score, mx
    if score <= -2:
        return "downtrend", base + " — score<=-2", score, mx
    return "range", base + " — |score|<2", score, mx


_DIRECTION = {"strong_uptrend": 1, "uptrend": 1, "euphoria": 1, "range": 0,
              "downtrend": -1, "strong_downtrend": -1, "capitulation": -1}


def _vol_label(pctile: Optional[float]) -> str:
    if pctile is None:
        return "unknown"
    if pctile >= 90:
        return "extreme"
    if pctile >= 75:
        return "high"
    if pctile <= 25:
        return "low"
    return "normal"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def classify_regime(symbol: str = "BTC", timeframe: str = "1d") -> Dict[str, Any]:
    """Classify the market regime of a symbol from local OHLCV math.

    Returns regime label (strong_uptrend | uptrend | range | downtrend | strong_downtrend |
    capitulation | euphoria) plus the exact rule that fired, trend detail (ADX/+DI/-DI,
    MA20/50/200 distance & 5-bar slope, HH/HL structure), volatility (30d realized vol and its
    1y percentile, ATR%), drawdown from 90d/365d high, and 4h/1d/1w agreement.
    Use it to decide whether trend-following or mean-reversion setups are appropriate.
    """
    try:
        sym = base_symbol(symbol)
        if timeframe not in ("4h", "1d", "1w"):
            return {"status": "error", "error": "timeframe must be one of 4h, 1d, 1w"}
        limits = {"4h": SCAN_BARS, "1d": REGIME_BARS, "1w": SCAN_BARS}
        df, venue = _fetch_ohlcv(sym, timeframe, limits[timeframe])
        if len(df) < 60:
            return {"status": "error", "error": f"not enough history for {sym} {timeframe} ({len(df)} bars, need 60)"}
        m = _regime_metrics(df, timeframe)
        mtf: Dict[str, Any] = {}
        for tf in ("4h", "1d", "1w"):
            if tf == timeframe:
                mtf[tf] = m["label"]
                continue
            try:
                d2, _ = _fetch_ohlcv(sym, tf, limits[tf])
                mtf[tf] = _regime_metrics(d2, tf)["label"] if len(d2) >= 40 else "insufficient_data"
            except Exception as e:
                mtf[tf] = f"unavailable ({e.__class__.__name__})"
        dirs = [_DIRECTION[v] for v in mtf.values() if v in _DIRECTION]
        if dirs and all(d == 1 for d in dirs):
            agreement = "aligned_bullish"
        elif dirs and all(d == -1 for d in dirs):
            agreement = "aligned_bearish"
        elif dirs and all(d == 0 for d in dirs):
            agreement = "aligned_range"
        else:
            agreement = "mixed"
        mtf["agreement"] = agreement
        vol_reg = _vol_label(m["rv30_pctile"])
        label = m["label"]
        hints = {
            "capitulation": "forced selling: mean-reversion bounces likely but trend is down — size small, wait for reclaim/confirmation",
            "euphoria": "stretched: avoid chasing, tighten stops, look for exhaustion",
            "strong_uptrend": "trend-following longs favored; buy pullbacks to MA20/MA50",
            "uptrend": "bias long on pullbacks; trend not yet strong",
            "range": "no trend: fade range extremes, avoid breakout chasing without volume",
            "downtrend": "bias short/flat; longs only as tactical bounces with tight risk",
            "strong_downtrend": "trend-following shorts favored; rallies to MA20/MA50 are sell zones",
        }
        interp = (f"{sym} {timeframe}: {label} ({m['rule']}). Vol {vol_reg} "
                  f"(30d RV {_r(m['rv30'], 1)}%, pctile {_r(m['rv30_pctile'], 0)}). MTF {agreement} "
                  f"[4h {mtf['4h']}, 1d {mtf['1d']}, 1w {mtf['1w']}]. {hints.get(label, '')}.")
        ma_out = {f"ma{n}": {"dist_pct": _r(v["dist_pct"]), "slope5_pct": _r(v["slope5_pct"])} for n, v in m["ma"].items()}
        return {
            "status": "success", "source": venue, "symbol": sym, "timeframe": timeframe,
            "as_of": _iso(df.index[-1]), "price": _px(m["price"]),
            "regime": label, "rule": m["rule"],
            "trend": {"adx": _r(m["adx"], 1), "plus_di": _r(m["plus_di"], 1), "minus_di": _r(m["minus_di"], 1),
                      "score": m["trend_score"], "max_score": m["trend_max"], "structure": m["structure"], **ma_out},
            "momentum": {"rsi14": _r(m["rsi"], 1), "ret7_pct": _r(m["ret7"]), "ret30_pct": _r(m["ret30"])},
            "volatility": {"rv30_pct": _r(m["rv30"], 1), "rv30_percentile_1y": _r(m["rv30_pctile"], 0),
                           "atr_pct": _r(m["atr_pct"]), "vol_regime": vol_reg},
            "drawdown": {"from_90d_high_pct": _r(m["dd90"]), "from_365d_high_pct": _r(m["dd365"])},
            "multi_timeframe": mtf,
            "bars": len(df),
            "interpretation": interp,
        }
    except Exception as e:
        logger.exception("classify_regime failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _symbol_snapshot(df: pd.DataFrame) -> Dict[str, Any]:
    c = df["close"]
    last = float(c.iloc[-1])
    snap: Dict[str, Any] = {"px": last}
    for n in (20, 50, 200):
        snap[f"d{n}"] = _pct(last, c.rolling(n).mean().iloc[-1]) if len(c) >= n else None
    snap["rsi"] = _num(_rsi(c).iloc[-1])
    for d in (7, 30, 90):
        snap[f"r{d}"] = _ret(c, d)
    snap["dd90"] = _pct(last, df["high"].iloc[-90:].max())
    v = df["volume"] * c
    snap["vol_tr"] = _pct(v.iloc[-7:].mean(), v.iloc[-30:].mean()) if len(v) >= 30 else None
    snap["adv30"] = _num(v.iloc[-30:].mean() / 1e6)  # avg daily $ volume, millions (this venue only)
    snap["atr"] = _num(_atr(df).iloc[-1] / last * 100.0) if last else None
    snap["new_hi30"] = bool(len(df) >= 30 and df["high"].iloc[-1] >= df["high"].iloc[-30:].max())
    snap["new_lo30"] = bool(len(df) >= 30 and df["low"].iloc[-1] <= df["low"].iloc[-30:].min())
    return snap


def _quality_flags(snap: Dict[str, Any], bars: int) -> List[str]:
    flags = []
    if snap.get("vol_tr") is not None and snap["vol_tr"] >= 150:
        flags.append("volume_spike")
    if snap.get("atr") is not None and snap["atr"] >= 12:
        flags.append("high_atr")
    if snap.get("adv30") is not None and snap["adv30"] < 2:
        flags.append("thin")
    if bars < 200:
        flags.append("new_listing")
    return flags


@mcp.tool()
def market_breadth(symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    """Market breadth across a universe (default: top ~30 liquid OKX USDT pairs, ex-stables).

    Returns % above MA20/50/200, % with daily RSI<30 / >70, new 30d highs vs lows, median 7d/30d
    return, dispersion (std of 30d returns), breadth-thrust / washout flags and a 0-100 breadth
    score (>60 healthy participation, <30 broad weakness/washout).
    """
    try:
        uni, how = _resolve_universe(symbols)
        data, venues, skipped = _fetch_many(uni, "1d", SCAN_BARS)
        if not data:
            return {"status": "error", "error": "no OHLCV data for any symbol", "skipped": skipped}
        snaps = {s: _symbol_snapshot(df) for s, df in data.items()}

        def pct_true(key, pred):
            vals = [pred(v[key]) for v in snaps.values() if v.get(key) is not None]
            return (_r(100.0 * sum(vals) / len(vals), 1), len(vals)) if vals else (None, 0)

        above = {f"ma{n}": pct_true(f"d{n}", lambda x: x > 0)[0] for n in (20, 50, 200)}
        n_ma200 = pct_true("d200", lambda x: x > 0)[1]
        rsi_lo = pct_true("rsi", lambda x: x < 30)[0]
        rsi_hi = pct_true("rsi", lambda x: x > 70)[0]
        highs = sum(1 for v in snaps.values() if v["new_hi30"])
        lows = sum(1 for v in snaps.values() if v["new_lo30"])
        r7 = [v["r7"] for v in snaps.values() if v["r7"] is not None]
        r30 = [v["r30"] for v in snaps.values() if v["r30"] is not None]
        med7 = float(np.median(r7)) if r7 else None
        med30 = float(np.median(r30)) if r30 else None
        disp = float(np.std(r30, ddof=1)) if len(r30) > 1 else None

        # % above MA20 over the last 15 days (thrust detection)
        above20 = pd.concat({s: (df["close"] > df["close"].rolling(20).mean()).where(df["close"].rolling(20).mean().notna())
                             for s, df in data.items()}, axis=1)
        above20.index = above20.index.normalize()
        above20 = above20.groupby(level=0).last()
        series = (above20.astype(float).mean(axis=1, skipna=True) * 100.0).dropna().iloc[-15:]
        thrust = bool(len(series) >= 11 and series.iloc[-11:-1].min() < 40 and series.iloc[-1] >= 61.5)
        washout = bool(len(series) and series.iloc[-1] <= 10)

        hl_ratio = 100.0 * highs / (highs + lows) if (highs + lows) else 50.0
        mom = float(np.clip(50.0 + 2.0 * (med30 or 0.0), 0, 100))
        parts = [(above["ma20"], 0.25), (above["ma50"], 0.25), (above["ma200"], 0.2), (hl_ratio, 0.15), (mom, 0.15)]
        wsum = sum(w for v, w in parts if v is not None)
        score = sum(v * w for v, w in parts if v is not None) / wsum if wsum else None
        if score is None:
            tone = "unknown"
        elif score >= 70:
            tone = "strong/broad participation"
        elif score >= 50:
            tone = "healthy"
        elif score >= 30:
            tone = "weak/narrow"
        else:
            tone = "very weak (broad selling)"
        interp = (f"Breadth score {_r(score, 0)}/100 ({tone}). {above['ma50']}% above MA50, {above['ma200']}% above MA200; "
                  f"{rsi_lo}% oversold (RSI<30); 30d highs/lows {highs}/{lows}; median 30d {_r(med30, 1)}%, "
                  f"dispersion {_r(disp, 1)}pp.")
        if thrust:
            interp += " BREADTH THRUST: %>MA20 jumped from <40% to >61.5% in 10 days — historically a durable-bottom signal."
        elif washout:
            interp += " Washout: <=10% of names above MA20 — capitulation-type breadth, bounces likely but not a thrust yet."
        if disp is not None and disp < 8:
            interp += " Low dispersion = beta-driven tape (alts trade as one with BTC)."
        return {
            "status": "success", "source": ",".join(sorted(set(venues.values()))), "as_of": _now_iso(),
            "universe": how, "n_symbols": len(data), "skipped": skipped,
            "pct_above_ma": above, "n_with_ma200": n_ma200,
            "pct_rsi_below_30": rsi_lo, "pct_rsi_above_70": rsi_hi,
            "new_30d_highs": highs, "new_30d_lows": lows,
            "median_return_pct": {"7d": _r(med7), "30d": _r(med30)},
            "dispersion_30d_pp": _r(disp),
            "pct_above_ma20_last10d": [_r(x, 0) for x in series.iloc[-10:].tolist()],
            "breadth_thrust": thrust, "washout": washout,
            "breadth_score": _r(score, 0),
            "interpretation": interp,
        }
    except Exception as e:
        logger.exception("market_breadth failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _ratio_stats(alt: pd.DataFrame, ref: pd.DataFrame) -> Dict[str, Optional[float]]:
    a = alt["close"].copy()
    b = ref["close"].copy()
    a.index, b.index = a.index.normalize(), b.index.normalize()
    a, b = a.groupby(level=0).last(), b.groupby(level=0).last()
    ratio = (a / b).dropna()
    out: Dict[str, Optional[float]] = {}
    for d in (7, 30, 90):
        out[f"rs{d}"] = _ret(ratio, d)
    tail = np.log(ratio.iloc[-31:]) if len(ratio) >= 31 else None
    if tail is not None and len(tail) >= 10:
        slope = np.polyfit(np.arange(len(tail)), tail.values, 1)[0]
        out["rs_slope"] = float((math.exp(slope) - 1.0) * 100.0)  # % per day
    else:
        out["rs_slope"] = None
    return out


@mcp.tool()
def relative_strength_scan(symbols: Optional[List[str]] = None, vs: str = "BTC", top_n: int = 10) -> Dict[str, Any]:
    """Relative-strength screener (the alt-scan, deterministic). Default universe: top ~30 OKX USDT pairs.

    Per symbol: price, 7/30/90d return, ALT/vs ratio change 7/30/90d (rs*) and 30d log-slope (%/day),
    daily RSI14, % distance to MA50/MA200, 90d drawdown, volume trend (7d vs 30d avg $vol, %), ATR%.
    Buckets (top_n each): leaders (RS score = 0.5*rs30+0.3*rs7+0.2*rs90), oversold_quality
    (RSI<=35 ranked by rs30 + 0.5*clip(d200,±30)), laggards (lowest RS score). Plus the full table.
    Rows carry `flags` (volume_spike, high_atr, thin, new_listing) — flagged leaders are often pumps.
    """
    try:
        ref = base_symbol(vs)
        top_n = max(1, min(int(top_n), 30))
        uni, how = _resolve_universe(symbols, include=ref)
        data, venues, skipped = _fetch_many(uni, "1d", SCAN_BARS)
        if ref not in data:
            return {"status": "error", "error": f"could not fetch reference {ref}", "skipped": skipped}
        rows = []
        for s, df in data.items():
            if len(df) < 31:
                skipped.append(f"{s}(short history)")
                continue
            snap = _symbol_snapshot(df)
            rs = _ratio_stats(df, data[ref]) if s != ref else {"rs7": 0.0, "rs30": 0.0, "rs90": 0.0, "rs_slope": 0.0}
            parts = [(rs.get("rs30"), 0.5), (rs.get("rs7"), 0.3), (rs.get("rs90"), 0.2)]
            w = sum(wt for v, wt in parts if v is not None)
            rs_score = sum(v * wt for v, wt in parts if v is not None) / w if w else None
            rows.append({
                "sym": s, "px": _px(snap["px"]), "r7": _r(snap["r7"], 1), "r30": _r(snap["r30"], 1),
                "r90": _r(snap["r90"], 1), "rs7": _r(rs["rs7"], 1), "rs30": _r(rs["rs30"], 1),
                "rs90": _r(rs["rs90"], 1), "rs_slope": _r(rs["rs_slope"], 2), "rsi": _r(snap["rsi"], 1),
                "d50": _r(snap["d50"], 1), "d200": _r(snap["d200"], 1), "dd90": _r(snap["dd90"], 1),
                "vol_tr": _r(snap["vol_tr"], 0), "atr": _r(snap["atr"], 1), "adv30_m": _r(snap["adv30"], 1),
                "rs_score": _r(rs_score, 1),
            })
            flags = _quality_flags(snap, len(df))
            if flags:
                rows[-1]["flags"] = flags
        alts = [r for r in rows if r["sym"] != ref and r["rs_score"] is not None]
        leaders = sorted(alts, key=lambda r: -r["rs_score"])[:top_n]
        lead_syms = {r["sym"] for r in leaders}
        laggards = sorted([r for r in alts if r["sym"] not in lead_syms], key=lambda r: r["rs_score"])[:top_n]
        oversold = [r for r in rows if r["sym"] != ref and r["rsi"] is not None and r["rsi"] <= 35]
        for r in oversold:
            d200 = r["d200"] if r["d200"] is not None else -30.0
            r["_q"] = (r["rs30"] or 0.0) + 0.5 * float(np.clip(d200, -30, 30))
        oversold = sorted(oversold, key=lambda r: -r["_q"])[:top_n]
        oversold_out = [{"sym": r["sym"], "rsi": r["rsi"], "rs30": r["rs30"], "d200": r["d200"],
                         "dd90": r["dd90"], "quality": _r(r["_q"], 1)} for r in oversold]
        for r in rows:
            r.pop("_q", None)
        table = sorted(rows, key=lambda r: -(r["rs30"] if r["rs30"] is not None else -1e9))

        def brief(lst):
            out = []
            for r in lst:
                b = {"sym": r["sym"], "rs_score": r["rs_score"], "rs30": r["rs30"], "r30": r["r30"],
                     "rsi": r["rsi"], "d200": r["d200"], "vol_tr": r["vol_tr"]}
                if r.get("flags"):
                    b["flags"] = r["flags"]
                out.append(b)
            return out

        ref_row = next((r for r in rows if r["sym"] == ref), {})
        above50 = sum(1 for r in alts if r["d50"] is not None and r["d50"] > 0)
        n_over = sum(1 for r in alts if r["rsi"] is not None and r["rsi"] < 30)
        outperf = sum(1 for r in alts if r["rs30"] is not None and r["rs30"] > 0)
        interp = (f"{len(alts)} alts vs {ref} ({ref} 30d {ref_row.get('r30')}%, RSI {ref_row.get('rsi')}). "
                  f"{outperf}/{len(alts)} outperform {ref} over 30d, {above50} above MA50, {n_over} with RSI<30. "
                  f"Leaders: {', '.join(r['sym'] for r in leaders[:5]) or 'none'}. "
                  f"Laggards: {', '.join(r['sym'] for r in laggards[:5]) or 'none'}.")
        flagged = [r["sym"] for r in leaders if r.get("flags")]
        if flagged:
            interp += f" Flagged leaders (pump/thin/new — verify before trusting RS): {', '.join(flagged)}."
        if alts and above50 <= len(alts) * 0.1:
            interp += f" Almost nothing above MA50: alt longs are leveraged {ref}-bounce bets — size by beta."
        return {
            "status": "success", "source": ",".join(sorted(set(venues.values()))), "as_of": _now_iso(),
            "universe": how, "vs": ref, "n_symbols": len(rows), "skipped": skipped,
            "leaders": brief(leaders), "oversold_quality": oversold_out, "laggards": brief(laggards),
            "table": table,
            "legend": ("r*=return %, rs*=ALT/vs ratio change % (>0 outperforming), rs_slope=%/day of ALT/vs over 30d, "
                       "d50/d200=% vs MA, dd90=% from 90d high, vol_tr=7d vs 30d avg $volume %, atr=ATR14 % of price, "
                       "adv30_m=30d avg daily $volume (M, this venue); flags: volume_spike (vol_tr>=150), "
                       "high_atr (ATR>=12%), thin (<$2M/day), new_listing (<200 daily bars)"),
            "interpretation": interp,
        }
    except Exception as e:
        logger.exception("relative_strength_scan failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _sector_momentum() -> Tuple[List[Dict[str, Any]], List[str]]:
    avail = _okx_bases()
    members = sorted({s for lst in SECTOR_MAP.values() for s in lst if avail is None or s in avail})
    data, _, skipped = _fetch_many(members, "1d", SCAN_BARS)
    rows = []
    for sector, syms in SECTOR_MAP.items():
        r7, r30, best = [], [], None
        for s in syms:
            df = data.get(s)
            if df is None:
                continue
            a, b = _ret(df["close"], 7), _ret(df["close"], 30)
            if a is not None:
                r7.append(a)
                if best is None or a > best[1]:
                    best = (s, a)
            if b is not None:
                r30.append(b)
        if r7:
            rows.append({"sector": sector, "r7_median": _r(np.median(r7), 1),
                         "r30_median": _r(np.median(r30), 1) if r30 else None,
                         "n": len(r7), "best_7d": f"{best[0]} {best[1]:+.1f}%" if best else None})
    rows.sort(key=lambda r: -(r["r7_median"] if r["r7_median"] is not None else -1e9))
    return rows, skipped


@mcp.tool()
def sector_rotation(min_market_cap_usd: float = 1e9) -> Dict[str, Any]:
    """Sector rotation: which crypto sectors lead/lag.

    Primary: CoinGecko /coins/categories → top/bottom 8 categories by 24h market-cap change
    (cap >= min_market_cap_usd; portfolio/stablecoin/tokenized categories excluded; |24h|>25% moves are
    listed under excluded_anomalies as constituent artifacts) with mcap & volume.
    Always adds `okx_momentum`: a built-in sector map (L1, L2, DeFi, AI, Meme, Exchange, Privacy,
    Gaming, RWA, Payments) with median 7d/30d member returns from OKX daily OHLCV (the fallback if
    CoinGecko fails). `path` says which source(s) were used. Lead = money rotating in.
    """
    cg_error = None
    cg_top = cg_bottom = None
    anomalies: List[Dict[str, Any]] = []
    n_cats = 0
    try:
        headers = {"accept": "application/json"}
        key = get_key("COINGECKO")
        if key:
            headers["x-cg-demo-api-key"] = key
        cats = http_get_json(COINGECKO_CATEGORIES, headers=headers, ttl=900)
        if not isinstance(cats, list):
            raise HttpError("unexpected CoinGecko categories shape", 200, COINGECKO_CATEGORIES)
        rows = []
        for c in cats:
            name = str(c.get("name") or "")
            mcap, chg = _num(c.get("market_cap")), _num(c.get("market_cap_change_24h"))
            if mcap is None or chg is None or mcap < min_market_cap_usd:
                continue
            if any(x in name.lower() for x in _CATEGORY_EXCLUDE):
                continue
            rows.append({"name": name, "chg_24h": _r(chg, 2), "mcap_b": _r(mcap / 1e9, 1),
                         "vol_b": _r((_num(c.get("volume_24h")) or 0) / 1e9, 2),
                         "top": (c.get("top_3_coins_id") or [])[:3]})
        # A >25% 24h move of a >=$1B category is almost always a constituent/listing artifact
        # (a new large token added, a bad price) — report separately, do not rank it.
        anomalies = [r for r in rows if abs(r["chg_24h"]) > ANOMALY_24H_PCT]
        rows = [r for r in rows if abs(r["chg_24h"]) <= ANOMALY_24H_PCT]
        if not rows:
            raise HttpError("no CoinGecko categories passed filters", 200, COINGECKO_CATEGORIES)
        rows.sort(key=lambda r: -r["chg_24h"])
        n_cats = len(rows)
        cg_top, cg_bottom = rows[:8], list(reversed(rows[-8:]))
    except HttpError as e:
        cg_error = str(e)
    except Exception as e:
        cg_error = f"{e.__class__.__name__}: {e}"

    okx_rows, okx_err = None, None
    try:
        okx_rows, _ = _sector_momentum()
        if not okx_rows:
            okx_err = "no sector members returned OHLCV"
            okx_rows = None
    except Exception as e:
        okx_err = f"{e.__class__.__name__}: {e}"

    if cg_top is None and okx_rows is None:
        return {"status": "error", "error": f"coingecko: {cg_error}; okx: {okx_err}", "source": "coingecko+okx"}
    path = ("coingecko+okx_static_map" if cg_top is not None and okx_rows is not None
            else "coingecko_only" if cg_top is not None else "okx_static_map_fallback")
    parts = []
    if cg_top is not None:
        def fmt(lst):
            return ", ".join("%s %+.1f%%" % (r["name"], r["chg_24h"]) for r in lst[:3])
        parts.append(f"CoinGecko 24h leaders: {fmt(cg_top)}; laggards: {fmt(cg_bottom)}.")
    if okx_rows:
        lead, lag = okx_rows[0], okx_rows[-1]
        spread = (lead["r7_median"] or 0) - (lag["r7_median"] or 0)
        parts.append(f"7d sector momentum (OKX): {lead['sector']} leads ({lead['r7_median']:+.1f}%), "
                     f"{lag['sector']} lags ({lag['r7_median']:+.1f}%), spread {spread:.1f}pp"
                     + (" — low spread: no real rotation, beta tape." if spread < 5 else "."))
    out: Dict[str, Any] = {"status": "success", "source": "coingecko+okx", "path": path, "as_of": _now_iso()}
    if cg_top is not None:
        out.update({"coingecko_categories_considered": n_cats, "top_24h": cg_top, "bottom_24h": cg_bottom})
        if anomalies:
            out["excluded_anomalies"] = [{"name": a["name"], "chg_24h": a["chg_24h"], "top": a["top"]} for a in anomalies[:5]]
    else:
        out["coingecko_error"] = cg_error
    if okx_rows is not None:
        out["okx_momentum"] = okx_rows
    else:
        out["okx_error"] = okx_err
    out["interpretation"] = " ".join(parts) + " 24h category moves are noisy; weigh the 7d/30d momentum more."
    return out


# ---------------------------------------------------------------------------
# Behavioral base rates
# ---------------------------------------------------------------------------

_COND_RE = [
    (re.compile(r"^rsi_below_(\d+(?:\.\d+)?)$"), "rsi_below"),
    (re.compile(r"^rsi_above_(\d+(?:\.\d+)?)$"), "rsi_above"),
    (re.compile(r"^drawdown_from_high_(\d+(?:\.\d+)?)$"), "drawdown"),
    (re.compile(r"^consecutive_red_days_(\d+)$"), "red_days"),
    (re.compile(r"^below_ma200$"), "below_ma200"),
    (re.compile(r"^above_ma200$"), "above_ma200"),
    (re.compile(r"^weekend$"), "weekend"),
    (re.compile(r"^vol_spike_(\d+(?:\.\d+)?)$"), "vol_spike"),
]
SUPPORTED_CONDITIONS = ("rsi_below_X, rsi_above_X, drawdown_from_high_X, consecutive_red_days_N, "
                        "below_ma200, above_ma200, weekend, vol_spike_X")


def _parse_condition(condition: str) -> Tuple[str, Optional[float]]:
    c = (condition or "").strip().lower()
    for rx, kind in _COND_RE:
        m = rx.match(c)
        if m:
            return kind, (float(m.group(1)) if m.groups() else None)
    raise ValueError(f"unsupported condition '{condition}'. Supported: {SUPPORTED_CONDITIONS}")


def _condition_series(df: pd.DataFrame, kind: str, x: Optional[float]) -> Tuple[pd.Series, str]:
    c = df["close"]
    if kind == "rsi_below":
        return _rsi(c) < x, f"daily RSI14 < {x:g}"
    if kind == "rsi_above":
        return _rsi(c) > x, f"daily RSI14 > {x:g}"
    if kind == "drawdown":
        dd = c / c.rolling(365, min_periods=60).max() - 1.0
        return dd <= -x / 100.0, f"close >= {x:g}% below rolling 365d closing high"
    if kind == "red_days":
        red = (c < df["open"]).astype(int)
        streak = red.groupby((red == 0).cumsum()).cumsum()
        return streak >= int(x), f"{int(x)}+ consecutive red daily candles (close<open)"
    if kind in ("below_ma200", "above_ma200"):
        ma = c.rolling(200).mean()
        cond = (c < ma) if kind == "below_ma200" else (c > ma)
        return cond & ma.notna(), f"close {'below' if kind == 'below_ma200' else 'above'} MA200 (event = cross)"
    if kind == "vol_spike":
        r = c.pct_change()
        sigma = r.rolling(30, min_periods=20).std().shift(1)
        return (r.abs() > x * sigma) & sigma.notna(), f"|daily return| > {x:g} x trailing 30d sigma"
    raise ValueError(kind)


def _events(cond: pd.Series, min_gap: int = 7) -> List[int]:
    c = cond.fillna(False).astype(bool).values
    out: List[int] = []
    for i in range(len(c)):
        if c[i] and (i == 0 or not c[i - 1]):
            if not out or i - out[-1] >= min_gap:
                out.append(i)
    return out


def _forward_stats(df: pd.DataFrame, events: List[int], h: int) -> Dict[str, Any]:
    c, lo = df["close"].values, df["low"].values
    n_all = len(c)
    fwd, mae = [], []
    for i in events:
        if i + h < n_all:
            fwd.append((c[i + h] / c[i] - 1.0) * 100.0)
            mae.append((lo[i + 1:i + h + 1].min() / c[i] - 1.0) * 100.0)
    base = (c[h:] / c[:-h] - 1.0) * 100.0 if n_all > h else np.array([])
    res: Dict[str, Any] = {"n": len(fwd)}
    if fwd:
        a = np.array(fwd)
        res.update({"mean": _r(a.mean()), "median": _r(np.median(a)), "hit_rate": _r((a > 0).mean() * 100, 0),
                    "best": _r(a.max()), "worst": _r(a.min()), "mae_median": _r(np.median(mae))})
    if len(base):
        res.update({"baseline_mean": _r(base.mean()), "baseline_hit_rate": _r((base > 0).mean() * 100, 0)})
        if fwd:
            res["edge_vs_baseline"] = _r(np.mean(fwd) - base.mean())
    return res


@mcp.tool()
def behavior_base_rates(symbol: str = "BTC", condition: str = "rsi_below_25",
                        horizons: Optional[List[int]] = None, lookback_days: int = HISTORY_BARS) -> Dict[str, Any]:
    """Historical forward returns after a condition (base rates) on daily candles (~5y OKX, Kraken fallback).

    Conditions: rsi_below_X, rsi_above_X, drawdown_from_high_X (vs 365d closing high),
    consecutive_red_days_N, below_ma200 / above_ma200 (crosses), vol_spike_X (|ret| > X sigma),
    weekend (weekend vs weekday daily returns). Event = first day the condition turns true; events
    <7 days after the previous one are dropped. Per horizon: n, mean/median %, hit rate, best/worst,
    median max adverse excursion, unconditional baseline and edge. n<10 → anecdotal.
    """
    try:
        sym = base_symbol(symbol)
        kind, x = _parse_condition(condition)
        hs = sorted({int(h) for h in (horizons or [7, 30]) if 1 <= int(h) <= 365}) or [7, 30]
        lookback = max(250, min(int(lookback_days), 3000))
        raw, venue = _fetch_ohlcv(sym, "1d", lookback)
        df = _closed(raw, "1d")
        if len(df) < 120:
            return {"status": "error", "error": f"not enough daily history for {sym} ({len(df)} bars)"}
        hist = {"bars": len(df), "from": df.index[0].date().isoformat(), "to": df.index[-1].date().isoformat(),
                "years": _r(len(df) / 365.0, 1)}
        if kind == "weekend":
            r = df["close"].pct_change().dropna() * 100.0
            wk = r.index.dayofweek >= 5

            def grp(a):
                return {"n": int(len(a)), "mean": _r(a.mean(), 3), "median": _r(a.median(), 3),
                        "std": _r(a.std(), 2), "hit_rate": _r((a > 0).mean() * 100, 0)}
            we, wd = grp(r[wk]), grp(r[~wk])
            ratio = _r((r[wk].std() / r[~wk].std()) if r[~wk].std() else None, 2)
            interp = (f"{sym}: weekend days mean {we['mean']}%/day (vol {we['std']}%) vs weekdays {wd['mean']}% "
                      f"(vol {wd['std']}%); weekend vol is {ratio}x weekday. Day-of-week effects are small vs noise.")
            return {"status": "success", "source": venue, "symbol": sym, "condition": "weekend",
                    "definition": "daily close-to-close returns, Sat+Sun (UTC) vs Mon-Fri", "history": hist,
                    "weekend": we, "weekday": wd, "weekend_vol_ratio": ratio,
                    "caveat": "Daily UTC candles; weekend liquidity is thinner — gaps/wicks distort stops more than means.",
                    "interpretation": interp}
        cond, definition = _condition_series(df, kind, x)
        ev = _events(cond)
        stats = {f"{h}d": _forward_stats(df, ev, h) for h in hs}
        c = df["close"].values
        recent = []
        for i in ev[-5:]:
            row = {"date": df.index[i].date().isoformat(), "close": _px(c[i])}
            for h in hs:
                row[f"r{h}"] = _r((c[i + h] / c[i] - 1) * 100, 1) if i + h < len(c) else None
            recent.append(row)
        cond_now = bool(cond.fillna(False).iloc[-1]) if len(cond) else False
        if kind == "vol_spike" and ev:
            r = df["close"].pct_change().values
            ups = [i for i in ev if r[i] > 0]
            downs = [i for i in ev if r[i] < 0]
            by_dir = {}
            for name, lst in (("up_spikes", ups), ("down_spikes", downs)):
                by_dir[name] = {f"{h}d": {k: v for k, v in _forward_stats(df, lst, h).items()
                                          if k in ("n", "mean", "median", "hit_rate")} for h in hs}
        else:
            by_dir = None
        n_min = min((s["n"] for s in stats.values()), default=0)
        caveat = ("SMALL SAMPLE (n<10): anecdotal, do not size on it." if n_min < 10 else
                  "Overlapping crypto cycles; regime-dependent; past base rates are priors, not forecasts.")
        h0, hl = f"{hs[0]}d", f"{hs[-1]}d"
        s0, sl = stats[h0], stats[hl]
        if s0["n"]:
            interp = (f"{sym} after '{condition}' ({len(ev)} events, {hist['years']}y): {h0} mean {s0.get('mean')}% "
                      f"(median {s0.get('median')}%, hit {s0.get('hit_rate')}%, edge {s0.get('edge_vs_baseline')}pp vs baseline); "
                      f"{hl} mean {sl.get('mean')}% (hit {sl.get('hit_rate')}%, n={sl['n']}). "
                      f"Typical drawdown before payoff (MAE median {hl}): {sl.get('mae_median')}%. "
                      f"Condition currently {'TRUE' if cond_now else 'false'}.")
        else:
            interp = f"{sym}: no '{condition}' events with complete forward windows in {hist['years']}y of data."
        out = {"status": "success", "source": venue, "symbol": sym, "condition": condition,
               "definition": definition + "; event = first day true, min 7 days apart",
               "history": hist, "n_events": len(ev), "condition_true_now": cond_now,
               "horizons": stats, "recent_events": recent, "caveat": caveat, "interpretation": interp}
        if by_dir:
            out["by_direction"] = by_dir
        if hist["bars"] < lookback * 0.9:
            out["history_note"] = f"only {hist['bars']} daily bars available (requested {lookback})"
        return out
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        logger.exception("behavior_base_rates failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _bucket_stats(r: pd.Series, keys) -> List[Dict[str, Any]]:
    out = []
    for k, g in r.groupby(keys):
        n = len(g)
        sd = g.std()
        t = g.mean() / (sd / math.sqrt(n)) if n > 2 and sd and sd > 0 else None
        out.append({"k": k, "mean": _r(g.mean(), 3), "vol": _r(sd, 2), "hit": _r((g > 0).mean() * 100, 0),
                    "n": int(n), "t": _r(t, 1)})
    return out


@mcp.tool()
def seasonality(symbol: str = "BTC") -> Dict[str, Any]:
    """Seasonality: day-of-week and month-of-year returns (daily history, ~5y) and hour-of-day (UTC)
    returns/volatility from 1h candles over the last ~180 days.

    Each bucket: mean % return, vol (std %), hit rate %, n and t-stat (|t|>=2 flagged as notable).
    Seasonal effects in crypto are weak and unstable — use as a tie-breaker for timing, never as a thesis.
    """
    try:
        sym = base_symbol(symbol)
        raw, venue = _fetch_ohlcv(sym, "1d", HISTORY_BARS)
        daily = _closed(raw, "1d")
        if len(daily) < 60:
            return {"status": "error", "error": f"not enough daily history for {sym}"}
        r = daily["close"].pct_change().dropna() * 100.0
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        dow = _bucket_stats(r, r.index.dayofweek)
        for d in dow:
            d["k"] = names[int(d["k"])]
        monthly = daily["close"].resample("ME").last().pct_change().dropna() * 100.0
        # drop the current (incomplete) month
        if len(monthly) and monthly.index[-1].month == pd.Timestamp.now(tz="UTC").month and \
                monthly.index[-1].year == pd.Timestamp.now(tz="UTC").year:
            monthly = monthly.iloc[:-1]
        mnames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        months = _bucket_stats(monthly, monthly.index.month) if len(monthly) else []
        for m in months:
            m["k"] = mnames[int(m["k"]) - 1]
            m["median"] = None
        for m in months:
            g = monthly[monthly.index.month == mnames.index(m["k"]) + 1]
            m["median"] = _r(g.median(), 2)
        hours, hour_note = [], None
        try:
            hraw, _ = _fetch_ohlcv(sym, "1h", 180 * 24)
            h = _closed(hraw, "1h")
            hr = h["close"].pct_change().dropna() * 100.0
            hours = _bucket_stats(hr, hr.index.hour)
            for x in hours:
                x["k"] = int(x["k"])
            hour_note = f"{len(h)} hourly bars ({_r(len(h) / 24, 0)} days)"
        except Exception as e:
            hour_note = f"hourly data unavailable: {e.__class__.__name__}"
        def flag(b):  # month buckets have ~5 samples: never "notable"
            return b["t"] is not None and abs(b["t"]) >= 2 and b["n"] >= 30
        notable = [f"{d['k']} (t={d['t']})" for d in dow if flag(d)]
        notable += [f"{x['k']:02d}h UTC (t={x['t']})" for x in hours if flag(x)]
        best_dow = max(dow, key=lambda d: d["mean"] or -1e9)["k"] if dow else None
        worst_dow = min(dow, key=lambda d: d["mean"] or 1e9)["k"] if dow else None
        vol_hours = sorted(hours, key=lambda x: -(x["vol"] or 0))[:3]
        cur_m = mnames[pd.Timestamp.now(tz="UTC").month - 1]
        cur = next((m for m in months if m["k"] == cur_m), None)
        interp = (f"{sym}: best weekday {best_dow}, worst {worst_dow}; most volatile hours (UTC): "
                  f"{', '.join(str(x['k']) for x in vol_hours) or 'n/a'}. "
                  + (f"{cur_m} historically mean {cur['mean']}% (median {cur['median']}%, n={cur['n']}). " if cur else "")
                  + (f"Statistically notable: {', '.join(notable)}." if notable else "No bucket is statistically notable (|t|<2)."))
        return {
            "status": "success", "source": venue, "symbol": sym, "as_of": _iso(daily.index[-1]),
            "history": {"daily_bars": len(daily), "from": daily.index[0].date().isoformat(), "hourly": hour_note},
            "day_of_week": dow, "month_of_year": months, "hour_of_day_utc": hours,
            "notable": notable,
            "caveat": ("Means are noisy (daily vol ~3x the mean edge); with ~5y each month has only ~5 samples. "
                       "|t|>=2 can occur by chance across 31 day/hour buckets; months (n~5) are never flagged."),
            "interpretation": interp,
        }
    except Exception as e:
        logger.exception("seasonality failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _returns_frame(data: Dict[str, pd.DataFrame], days: int) -> pd.DataFrame:
    closes = {}
    for s, df in data.items():
        c = df["close"].copy()
        c.index = c.index.normalize()
        closes[s] = c.groupby(level=0).last()
    frame = pd.concat(closes, axis=1).sort_index()
    rets = frame.pct_change(fill_method=None).iloc[-days:]
    return rets


def _clusters(corr: pd.DataFrame, thr: float = 0.8) -> List[List[str]]:
    syms = list(corr.columns)
    parent = {s: s for s in syms}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            v = corr.loc[a, b]
            if pd.notna(v) and v > thr:
                parent[find(a)] = find(b)
    groups: Dict[str, List[str]] = {}
    for s in syms:
        groups.setdefault(find(s), []).append(s)
    return sorted([g for g in groups.values() if len(g) > 1], key=lambda g: -len(g))


def _beta(r: pd.Series, m: pd.Series) -> Optional[float]:
    d = pd.concat([r, m], axis=1).dropna()
    if len(d) < 10 or d.iloc[:, 1].var() == 0:
        return None
    return float(d.iloc[:, 0].cov(d.iloc[:, 1]) / d.iloc[:, 1].var())


@mcp.tool()
def correlation_matrix(symbols: Optional[List[str]] = None, days: int = 60) -> Dict[str, Any]:
    """Daily-return correlation over `days` (default universe: top ~30 OKX USDT pairs; BTC always included).

    Returns average pairwise correlation, each symbol's corr & beta to BTC, clusters (corr>0.8) and,
    for <=12 symbols, the full matrix (2dp). High avg corr (>0.7) = one-factor market: diversification
    across alts is illusory; size the book by total BTC beta.
    """
    try:
        days = max(20, min(int(days), 365))
        uni, how = _resolve_universe(symbols, include="BTC")
        data, venues, skipped = _fetch_many(uni, "1d", max(SCAN_BARS, days + 10))
        if "BTC" not in data:
            return {"status": "error", "error": "could not fetch BTC", "skipped": skipped}
        rets = _returns_frame(data, days)
        enough = [s for s in rets.columns if rets[s].notna().sum() >= max(10, int(days * 0.6))]
        skipped += [f"{s}(short history)" for s in rets.columns if s not in enough]
        rets = rets[enough]
        corr = rets.corr(min_periods=10)
        n = len(enough)
        iu = np.triu_indices(n, 1)
        pair_vals = corr.values[iu]
        pair_vals = pair_vals[~np.isnan(pair_vals)]
        avg = float(pair_vals.mean()) if len(pair_vals) else None
        btc_table = []
        for s in enough:
            if s == "BTC":
                continue
            btc_table.append({"sym": s, "corr": _r(corr.loc[s, "BTC"]), "beta": _r(_beta(rets[s], rets["BTC"])),
                              "vol_ann": _r(rets[s].std() * math.sqrt(365) * 100, 0)})
        btc_table.sort(key=lambda r: -(r["beta"] if r["beta"] is not None else -1e9))
        clusters = _clusters(corr)
        out: Dict[str, Any] = {
            "status": "success", "source": ",".join(sorted(set(venues.values()))), "as_of": _now_iso(),
            "universe": how, "days": days, "n_symbols": n, "skipped": skipped,
            "avg_pairwise_corr": _r(avg), "pairs_above_0.8": int((pair_vals > 0.8).sum()),
            "btc_corr_beta": btc_table, "clusters_corr_gt_0.8": clusters,
        }
        if n <= 12:
            out["matrix"] = {a: {b: _r(corr.loc[a, b]) for b in enough} for a in enough}
        hi_beta = [r["sym"] for r in btc_table[:3]]
        lo_corr = [r["sym"] for r in sorted(btc_table, key=lambda r: r["corr"] if r["corr"] is not None else 9)[:3]]
        tone = ("one-factor market (diversification across alts is illusory)" if avg is not None and avg > 0.7 else
                "moderately correlated" if avg is not None and avg > 0.45 else "dispersed — idiosyncratic moves matter")
        out["interpretation"] = (f"{days}d avg pairwise corr {_r(avg)} → {tone}. Highest BTC beta: {', '.join(hi_beta)}; "
                                 f"least BTC-correlated: {', '.join(lo_corr)}. "
                                 f"{len(clusters)} cluster(s) with corr>0.8"
                                 + (f" (largest: {len(clusters[0])} names)." if clusters else "."))
        return out
    except Exception as e:
        logger.exception("correlation_matrix failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def volatility_regime(symbol: str = "BTC") -> Dict[str, Any]:
    """Volatility regime and noise-aware stop distances (daily candles).

    Returns realized vol 7/30/90d (annualized %), vol-of-vol, 30d-vol percentile over 1y, ATR14 %,
    median daily range, and stop distances at 1x/1.5x/2x ATR (% and long/short prices).
    Rule: a stop closer than ~1x ATR sits inside normal daily noise; futures-book stops (2-4%)
    need ATR% below them or lower leverage/size.
    """
    try:
        sym = base_symbol(symbol)
        df, venue = _fetch_ohlcv(sym, "1d", REGIME_BARS)
        if len(df) < 40:
            return {"status": "error", "error": f"not enough daily history for {sym} ({len(df)} bars)"}
        c = df["close"]
        last = float(c.iloc[-1])
        ann = _ann_factor("1d")
        rv = {f"rv{w}": _realized_vol(c, w, ann) for w in (7, 30, 90)}
        rv7_series = rv["rv7"].dropna().iloc[-90:]
        vov = float(rv7_series.std()) if len(rv7_series) > 5 else None
        vov_cv = float(vov / rv7_series.mean()) if vov is not None and rv7_series.mean() else None
        pct = _percentile_of_last(rv["rv30"], 365)
        atr = float(_atr(df).iloc[-1])
        atr_pct = atr / last * 100.0
        rng = ((df["high"] - df["low"]) / df["close"]).iloc[-30:].median() * 100.0
        stops = {}
        for k in (1.0, 1.5, 2.0):
            stops[f"{k:g}x_atr"] = {"pct": _r(k * atr_pct), "long_stop": _px(last - k * atr), "short_stop": _px(last + k * atr)}
        r7, r30, r90 = (_num(rv["rv7"].iloc[-1]), _num(rv["rv30"].iloc[-1]), _num(rv["rv90"].iloc[-1]))
        term = None
        if r7 is not None and r30 is not None:
            term = "expanding (7d > 30d)" if r7 > r30 * 1.1 else "compressing (7d < 30d)" if r7 < r30 * 0.9 else "stable"
        regime = _vol_label(pct)
        fut_note = ("futures-book 2-4% SL is inside 1x ATR noise → cut leverage/size or widen to >=1.5x ATR"
                    if atr_pct > 4 else "a 2-4% futures SL is outside 1x ATR noise" if atr_pct < 2 else
                    "a 2-4% futures SL is roughly 1x ATR — place it beyond structure, not at a round %")
        interp = (f"{sym}: 30d RV {_r(r30, 1)}% ({regime}, pctile {_r(pct, 0)}), vol {term}. ATR {atr_pct:.2f}%/day, "
                  f"median daily range {rng:.2f}%. Stops: 1x ATR {atr_pct:.1f}%, 1.5x {1.5 * atr_pct:.1f}%, "
                  f"2x {2 * atr_pct:.1f}%; {fut_note}.")
        return {
            "status": "success", "source": venue, "symbol": sym, "as_of": _iso(df.index[-1]), "price": _px(last),
            "realized_vol_ann_pct": {"7d": _r(r7, 1), "30d": _r(r30, 1), "90d": _r(r90, 1)},
            "vol_term_structure": term,
            "vol_of_vol": {"std_of_rv7_90d": _r(vov, 1), "cv": _r(vov_cv, 2)},
            "rv30_percentile_1y": _r(pct, 0), "vol_regime": regime,
            "atr14": _px(atr), "atr_pct": _r(atr_pct), "median_daily_range_pct_30d": _r(rng),
            "daily_1sigma_pct": _r((r30 or 0) / ann, 2) if r30 else None,
            "stop_distances": stops,
            "interpretation": interp,
        }
    except Exception as e:
        logger.exception("volatility_regime failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


@mcp.tool()
def beta_to_btc(symbol: str, days: int = 60) -> Dict[str, Any]:
    """Beta and correlation of a symbol's daily returns to BTC over `days` (plus 30d and up/down-day betas).

    beta 1.5 = a 1% BTC move ≈ 1.5% move in the symbol. Downside beta > upside beta = it falls harder
    than it rises with BTC (bad convexity for longs). Use to size alt positions in BTC-equivalent risk.
    """
    try:
        sym = base_symbol(symbol)
        if sym == "BTC":
            return {"status": "success", "source": "definition", "symbol": "BTC", "beta": 1.0, "corr": 1.0,
                    "interpretation": "BTC beta to itself is 1 by definition."}
        days = max(20, min(int(days), 365))
        data, venues, skipped = _fetch_many([sym, "BTC"], "1d", max(SCAN_BARS, days + 10))
        if sym not in data or "BTC" not in data:
            return {"status": "error", "error": f"could not fetch {', '.join(skipped)}"}
        rets = _returns_frame(data, days).dropna()
        if len(rets) < 15:
            return {"status": "error", "error": f"only {len(rets)} overlapping days"}
        r, m = rets[sym], rets["BTC"]
        beta = _beta(r, m)
        corr = float(r.corr(m))
        beta30 = _beta(r.iloc[-30:], m.iloc[-30:])
        up, dn = m > 0, m < 0
        beta_up = _beta(r[up], m[up]) if up.sum() >= 10 else None
        beta_dn = _beta(r[dn], m[dn]) if dn.sum() >= 10 else None
        vol_ratio = float(r.std() / m.std()) if m.std() else None
        conv = ""
        if beta_up is not None and beta_dn is not None:
            conv = (" Falls harder than it rises with BTC (downside beta > upside)." if beta_dn > beta_up * 1.15 else
                    " Rises harder than it falls with BTC (favourable convexity)." if beta_up > beta_dn * 1.15 else "")
        interp = (f"{sym} beta {_r(beta)} (30d {_r(beta30)}), corr {_r(corr)} to BTC over {len(rets)}d; "
                  f"vol {_r(vol_ratio, 1)}x BTC. A 5% BTC drop ≈ {_r((beta or 0) * 5, 1)}% in {sym}.{conv}")
        return {"status": "success", "source": ",".join(sorted(set(venues.values()))), "symbol": sym,
                "as_of": _now_iso(), "days": len(rets), "beta": _r(beta), "corr": _r(corr), "beta_30d": _r(beta30),
                "upside_beta": _r(beta_up), "downside_beta": _r(beta_dn), "vol_ratio_vs_btc": _r(vol_ratio),
                "interpretation": interp}
    except Exception as e:
        logger.exception("beta_to_btc failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


if __name__ == "__main__":
    mcp.run()
