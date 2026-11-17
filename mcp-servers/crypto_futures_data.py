#!/usr/bin/env python3
"""
Crypto Futures Data MCP Server

Provides perpetual futures market data using CCXT (no API keys required).

10 Tools:
1. get_funding_rate - Current perpetual funding rate
2. get_funding_rate_history - Historical funding rates
3. get_open_interest - Current Open Interest
4. get_long_short_ratio - Long/Short ratio of traders
5. get_taker_buy_sell_ratio - Taker buy/sell volume ratio
6. calculate_liquidation_levels - Estimated liquidation levels
7. get_perpetual_stats - Complete perpetual statistics
8. compare_funding_rates - Compare funding across exchanges
9. analyze_funding_trend - Analyze funding rate trend
10. detect_funding_arbitrage - Detect funding rate arbitrage opportunities

Supported Exchanges (no API keys):
- OKX (default), Bitget, MEXC, Binance, Bybit
  (Binance/Bybit geo-block cloud IPs with 451/403; ccxt instances come from
  desk_common.get_exchange with proxy support, venues ordered by exchange_priority("swap"))
- Long/short + taker ratios: OKX Rubik public stats (default) or Binance futures data.
"""

import ccxt
from fastmcp import FastMCP
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import functools
import statistics
import logging

from collections.abc import Mapping

from desk_common import tool_serializer, get_exchange, exchange_priority, fetch_with_fallback, http_get_json, HttpError
from desk_common.exchanges import mark_blocked, _is_geo_block, _blocked as _BLOCKED_VENUES
from validators import validate_symbol, validate_exchange, validate_positive_int

logger = logging.getLogger(__name__)

# Initialize MCP server (numpy/NaN-safe serializer)
mcp = FastMCP("crypto-futures-data", tool_serializer=tool_serializer)

FUTURES_VENUES = ["okx", "bitget", "mexc", "binance", "bybit"]


class _FuturesRegistry(Mapping):
    """Lazy name -> ccxt swap instance mapping (desk_common.get_exchange, proxy-aware)."""

    def __init__(self, names: List[str]):
        self._names = list(names)

    def __contains__(self, name) -> bool:
        return isinstance(name, str) and name in self._names

    def __getitem__(self, name: str):
        if name not in self._names:
            raise KeyError(name)
        return get_exchange(name, "swap")

    def __iter__(self):
        prio = [n for n in exchange_priority("swap") if n in self._names]
        return iter(prio + [n for n in self._names if n not in prio])

    def __len__(self) -> int:
        return len(self._names)


# Reliable exchanges with public futures data (lazy; tests may patch with a dict)
RELIABLE_FUTURES_EXCHANGES = _FuturesRegistry(FUTURES_VENUES)

# Public positioning-stat sources (long/short + taker ratios)
OKX_RUBIK = "https://www.okx.com/api/v5/rubik/stat"
BINANCE_FAPI_DATA = "https://fapi.binance.com/futures/data"
RATIO_SOURCES = ("okx", "binance")

# Funding rate thresholds (annualized %)
FUNDING_NEUTRAL_THRESHOLD = 5        # |annual rate| < 5% = neutral
FUNDING_EXTREME_THRESHOLD = 50       # |annual rate| > 50% = extreme

# Long/short ratio thresholds
LS_EXTREMELY_BULLISH = 2.0           # Ratio > 2 = extremely bullish
LS_BULLISH = 1.2                     # Ratio > 1.2 = bullish
LS_EXTREMELY_BEARISH = 0.5           # Ratio < 0.5 = extremely bearish
LS_BEARISH = 0.8                     # Ratio < 0.8 = bearish

# Taker buy/sell ratio thresholds
TAKER_STRONG_BUY = 1.5              # Ratio > 1.5 = strong buy pressure
TAKER_MODERATE_BUY = 1.1            # Ratio > 1.1 = moderate buy pressure
TAKER_STRONG_SELL = 0.7             # Ratio < 0.7 = strong sell pressure
TAKER_MODERATE_SELL = 0.9           # Ratio < 0.9 = moderate sell pressure

# Funding trend thresholds (% change)
TREND_RAPID_INCREASE_PCT = 50       # > 50% change = rapidly increasing
TREND_INCREASE_PCT = 10             # > 10% change = increasing
TREND_RAPID_DECREASE_PCT = -50      # < -50% change = rapidly decreasing
TREND_DECREASE_PCT = -10            # < -10% change = decreasing

# Funding volatility thresholds
FUNDING_VOL_HIGH = 0.0005           # Funding rate stdev > 0.0005 = high vol
FUNDING_VOL_MODERATE = 0.0002       # Funding rate stdev > 0.0002 = moderate vol

# Arbitrage thresholds
MIN_ARBITRAGE_SPREAD_ANNUAL = 10    # Minimum annual % for arbitrage opportunity

# Scoring adjustments
SCORE_FUNDING_EXTREME = 15
SCORE_LS_RATIO = 10
SCORE_TAKER_PRESSURE = 10


def _get_exchange(exchange_name: str):
    """Get exchange instance."""
    if exchange_name not in RELIABLE_FUTURES_EXCHANGES:
        raise ValueError(f"Unsupported exchange: {exchange_name}. Supported: {sorted(RELIABLE_FUTURES_EXCHANGES.keys())}")
    return RELIABLE_FUTURES_EXCHANGES[exchange_name]


def _should_fallback(err: Exception) -> bool:
    return _is_geo_block(err) or isinstance(err, (ccxt.NetworkError, ccxt.BadSymbol))


def _venue_call(exchange: str, fn):
    """Run ``fn(ex)`` on the requested venue; if it is geo-blocked / unreachable / lacks the
    market, fall back through the swap priority list. Returns (result, exchange_used)."""
    try:
        return fn(_get_exchange(exchange)), exchange
    except Exception as e:
        if not _should_fallback(e):
            raise
        if _is_geo_block(e):
            mark_blocked(exchange)
        logger.warning("futures: %s failed (%s), falling back", exchange, e.__class__.__name__)
    others = [n for n in RELIABLE_FUTURES_EXCHANGES.keys() if n != exchange and n not in _BLOCKED_VENUES]
    result, used, _errs = fetch_with_fallback(fn, exchanges=others, market="swap")
    return result, used


def _num(x) -> Optional[float]:
    try:
        v = float(x)
        return v if v == v else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _format_symbol(symbol: str) -> str:
    """
    Format symbol for perpetual futures.
    BTC -> BTC/USDT:USDT
    """
    if ':' in symbol:
        return symbol
    if '/' in symbol:
        base = symbol.split('/')[0]
        return f"{base}/USDT:USDT"
    return f"{symbol}/USDT:USDT"


def _get_funding_rate(
    symbol: str = "BTC",
    exchange: str = "okx"
) -> Dict[str, Any]:
    """
    Get current perpetual funding rate.

    Args:
        symbol: Symbol (BTC, ETH, etc.)
        exchange: Exchange (binance, bybit, okx, bitget, mexc)

    Returns:
        Current funding rate, next funding time, and analysis
    """
    try:
        symbol = validate_symbol(symbol)
        exchange = validate_exchange(exchange, supported=set(RELIABLE_FUTURES_EXCHANGES.keys()))

        formatted_symbol = _format_symbol(symbol)

        funding_rate, exchange_used = _venue_call(exchange, lambda ex: ex.fetch_funding_rate(formatted_symbol))

        # Annualized funding rate
        rate = funding_rate['fundingRate']
        annual_rate = rate * 3 * 365 * 100  # 3 times per day

        # Next funding time
        next_funding = datetime.fromtimestamp(funding_rate['fundingTimestamp'] / 1000)

        # Analysis
        if abs(annual_rate) < FUNDING_NEUTRAL_THRESHOLD:
            bias = "NEUTRAL"
            interpretation = "Neutral funding rate, balanced market"
        elif annual_rate > FUNDING_NEUTRAL_THRESHOLD:
            bias = "BULLISH_EXTREME"
            interpretation = "Very positive funding rate - Longs paying Shorts - Possible correction"
        else:
            bias = "BEARISH_EXTREME"
            interpretation = "Very negative funding rate - Shorts paying Longs - Possible bounce"

        return {
            "success": True,
            "exchange": exchange,
            "exchange_used": exchange_used,
            "symbol": formatted_symbol,
            "funding_rate": rate,
            "funding_rate_percentage": rate * 100,
            "annual_rate_percentage": round(annual_rate, 2),
            "next_funding_time": next_funding.isoformat(),
            "time_until_funding": str(next_funding - datetime.now()),
            "market_bias": bias,
            "interpretation": interpretation,
            "raw_data": funding_rate
        }

    except Exception as e:
        logger.exception("get_funding_rate failed")
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "exchange": exchange,
            "symbol": symbol
        }


def _get_funding_rate_history(
    symbol: str = "BTC",
    exchange: str = "okx",
    hours: int = 24
) -> Dict[str, Any]:
    """
    Get historical funding rates.

    Args:
        symbol: Symbol (BTC, ETH, etc.)
        exchange: Exchange
        hours: Hours of history (default 24)

    Returns:
        Historical funding rates with statistics
    """
    try:
        symbol = validate_symbol(symbol)
        exchange = validate_exchange(exchange, supported=set(RELIABLE_FUTURES_EXCHANGES.keys()))
        hours = validate_positive_int(hours, "hours", max_value=720)

        formatted_symbol = _format_symbol(symbol)

        since = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)
        history, exchange_used = _venue_call(
            exchange, lambda ex: ex.fetch_funding_rate_history(formatted_symbol, since=since))

        if not history:
            return {
                "success": False,
                "error": "No funding rate history available",
                "error_type": "NoDataError",
                "exchange": exchange,
                "symbol": formatted_symbol
            }

        rates = [h['fundingRate'] for h in history]

        avg_rate = statistics.mean(rates)
        max_rate = max(rates)
        min_rate = min(rates)

        # Trend (last 8 vs first 8 funding rates)
        if len(rates) >= 16:
            recent_avg = statistics.mean(rates[-8:])
            older_avg = statistics.mean(rates[:8])
            trend = "INCREASING" if recent_avg > older_avg else "DECREASING"
        else:
            trend = "INSUFFICIENT_DATA"

        return {
            "success": True,
            "exchange": exchange,
            "exchange_used": exchange_used,
            "symbol": formatted_symbol,
            "period_hours": hours,
            "total_fundings": len(rates),
            "average_rate": avg_rate,
            "average_rate_percentage": avg_rate * 100,
            "max_rate": max_rate,
            "min_rate": min_rate,
            "current_rate": rates[-1],
            "trend": trend,
            "annual_rate_avg": round(avg_rate * 3 * 365 * 100, 2),
            "history": history[-20:]
        }

    except Exception as e:
        logger.exception("get_funding_rate_history failed")
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "exchange": exchange,
            "symbol": symbol
        }


def _get_open_interest(
    symbol: str = "BTC",
    exchange: str = "okx"
) -> Dict[str, Any]:
    """
    Get current Open Interest.

    Open Interest = Total open contracts (longs + shorts).
    Indicator of liquidity and trend strength.

    Args:
        symbol: Symbol (BTC, ETH, etc.)
        exchange: Exchange

    Returns:
        Current Open Interest and trend analysis
    """
    try:
        symbol = validate_symbol(symbol)
        exchange = validate_exchange(exchange, supported=set(RELIABLE_FUTURES_EXCHANGES.keys()))

        formatted_symbol = _format_symbol(symbol)

        def _fetch(ex):
            return ex.fetch_open_interest(formatted_symbol), ex.fetch_ticker(formatted_symbol), ex

        (oi, ticker, ex), exchange_used = _venue_call(exchange, _fetch)
        current_price = _num(ticker.get('last'))

        # ccxt unified fields: openInterestAmount = contracts (or base units on venues
        # without contract sizes), openInterestValue = USD notional. (V1 had them swapped.)
        oi_contracts = _num(oi.get('openInterestAmount'))
        oi_usd = _num(oi.get('openInterestValue'))
        oi_base = _num(oi.get('baseVolume'))  # OKX reports base-coin OI here (oiCcy)
        if oi_base is None and oi_contracts is not None:
            contract_size = None
            try:
                contract_size = _num((ex.markets or {}).get(formatted_symbol, {}).get('contractSize'))
            except Exception:
                contract_size = None
            if contract_size is not None:
                oi_base = oi_contracts * contract_size
            elif oi_usd is not None and current_price:
                oi_base = oi_usd / current_price
        if oi_usd is None and oi_base is not None and current_price:
            oi_usd = oi_base * current_price

        interpretation = []
        if oi_usd is not None:
            if oi_usd > 5_000_000_000:
                interpretation.append("Very high OI - High liquidity and participation")
            elif oi_usd < 100_000_000:
                interpretation.append("Low OI - Low futures participation")

        return {
            "success": True,
            "exchange": exchange,
            "exchange_used": exchange_used,
            "symbol": formatted_symbol,
            "open_interest_usd": round(oi_usd, 2) if oi_usd is not None else None,
            "open_interest_contracts": oi_contracts,
            "open_interest_base": round(oi_base, 4) if oi_base is not None else None,
            "open_interest_btc_equivalent": round(oi_base, 2) if oi_base is not None else None,
            "current_price": current_price,
            "timestamp": oi['timestamp'],
            "interpretation": " | ".join(interpretation) if interpretation else "Normal OI",
            "raw_data": oi
        }

    except Exception as e:
        logger.exception("get_open_interest failed")
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "exchange": exchange,
            "symbol": symbol
        }


_OKX_PERIODS = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "2h": "2H", "4h": "4H",
                "6h": "6H", "12h": "12H", "1d": "1D"}
_OKX_CCY_PERIODS = ("5m", "1H", "1D")  # ccy-level Rubik endpoints only accept these


def _okx_period(period: str, ccy_level: bool = False) -> str:
    p = _OKX_PERIODS.get((period or "5m").strip().lower(), "5m")
    if ccy_level and p not in _OKX_CCY_PERIODS:
        p = "1D" if p.endswith("D") else ("1H" if p.endswith("H") else "5m")
    return p


def _okx_rubik(path: str, params: Dict[str, Any]) -> List[List[str]]:
    """GET an OKX Rubik stat endpoint and return its ``data`` rows (newest first)."""
    payload = http_get_json(f"{OKX_RUBIK}/{path}", params=params, ttl=60)
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        msg = payload.get("msg") if isinstance(payload, dict) else "bad payload"
        raise HttpError(f"OKX Rubik {path} error: {msg}", None, path)
    return payload.get("data") or []


def _binance_futures_data(path: str, base: str, period: str) -> Dict[str, Any]:
    rows = http_get_json(f"{BINANCE_FAPI_DATA}/{path}",
                         params={"symbol": f"{base}USDT", "period": period, "limit": 1}, ttl=60)
    return rows[0] if rows else {}


def _ls_sentiment(ratio: float):
    if ratio > LS_EXTREMELY_BULLISH:
        return "EXTREMELY_BULLISH", "Top traders very long - Possible reversal"
    if ratio > LS_BULLISH:
        return "BULLISH", "Top traders moderately long"
    if ratio < LS_EXTREMELY_BEARISH:
        return "EXTREMELY_BEARISH", "Top traders very short - Possible reversal"
    if ratio < LS_BEARISH:
        return "BEARISH", "Top traders moderately short"
    return "NEUTRAL", "Top traders balanced"


def _get_long_short_ratio(
    symbol: str = "BTC",
    exchange: str = "okx",
    period: str = "5m"
) -> Dict[str, Any]:
    """
    Get Long/Short ratio of traders (top traders and all accounts).

    Sources (public, no key): OKX Rubik stats (default; reachable from cloud) or Binance
    futures data (geo-blocked from US cloud IPs). ``top_trader_long_short_ratio`` = top
    traders' long/short POSITION ratio; ``global_long_short_ratio`` = all accounts long/short.
    >2 crowded long (contrarian bearish), <0.5 crowded short (squeeze fuel).

    Args:
        symbol: Symbol (BTC, ETH, etc.)
        exchange: Data source: "okx" (default) or "binance"
        period: Period (5m, 15m, 30m, 1h, 4h, 1d)

    Returns:
        Long/Short ratios for top traders and all accounts
    """
    exchange = (exchange or "okx").strip().lower()
    if exchange not in RATIO_SOURCES:
        return {
            "success": False,
            "error": f"Long/Short ratio available on {', '.join(RATIO_SOURCES)} (got '{exchange}')",
            "error_type": "UnsupportedExchange",
            "exchange": exchange
        }

    try:
        symbol = validate_symbol(symbol)
        base = symbol.replace("/", "").replace(":USDT", "")

        if exchange == "okx":
            ccy_period = _okx_period(period, ccy_level=True)
            global_rows = _okx_rubik("contracts/long-short-account-ratio", {"ccy": base, "period": ccy_period})
            if not global_rows:
                raise HttpError(f"OKX returned no long/short data for {base}", None, "long-short-account-ratio")
            global_ratio = float(global_rows[0][1])
            top_ratio, top_row, top_note = None, None, None
            try:
                top_rows = _okx_rubik("contracts/long-short-position-ratio-contract-top-trader",
                                      {"instId": f"{base}-USDT-SWAP", "period": _okx_period(period)})
                if top_rows:
                    top_row = top_rows[0]
                    top_ratio = float(top_row[1])
            except Exception as e:  # top-trader stats are a bonus; keep the account ratio
                top_note = f"top-trader ratio unavailable: {str(e)[:120]}"
            if top_ratio is None:
                top_ratio = global_ratio
                top_note = top_note or "top-trader ratio unavailable; using all-accounts ratio"
            sentiment_top, interpretation_top = _ls_sentiment(top_ratio)
            result = {
                "success": True,
                "exchange": "okx",
                "source": "okx_rubik",
                "symbol": f"{base}-USDT-SWAP",
                "period": ccy_period,
                "as_of": datetime.fromtimestamp(int(global_rows[0][0]) / 1000).isoformat(),
                "top_trader_long_short_ratio": round(top_ratio, 4),
                "global_long_short_ratio": round(global_ratio, 4),
                "top_trader_sentiment": sentiment_top,
                "interpretation": interpretation_top,
                "divergence": abs(top_ratio - global_ratio) > 0.3,
                "raw_data": {"top_traders": top_row, "global": global_rows[0]},
            }
            if top_note:
                result["note"] = top_note
            return result

        # Binance (may be geo-blocked: HTTP 451 -> success False)
        data_top = _binance_futures_data("topLongShortPositionRatio", base, period)
        data_global = _binance_futures_data("globalLongShortAccountRatio", base, period)
        top_ratio = float(data_top.get('longShortRatio', 0))
        global_ratio = float(data_global.get('longShortRatio', 0))
        sentiment_top, interpretation_top = _ls_sentiment(top_ratio)

        return {
            "success": True,
            "exchange": "binance",
            "source": "binance_futures_data",
            "symbol": f"{base}USDT",
            "period": period,
            "top_trader_long_short_ratio": top_ratio,
            "global_long_short_ratio": global_ratio,
            "top_trader_sentiment": sentiment_top,
            "interpretation": interpretation_top,
            "divergence": abs(top_ratio - global_ratio) > 0.3,
            "raw_data": {
                "top_traders": data_top,
                "global": data_global
            }
        }

    except Exception as e:
        logger.warning("get_long_short_ratio failed: %s", e)
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "exchange": exchange,
            "symbol": symbol
        }


def _taker_pressure(ratio: float):
    if ratio > TAKER_STRONG_BUY:
        return "STRONG_BUY_PRESSURE", "Strong buy pressure - Takers buying aggressively"
    if ratio > TAKER_MODERATE_BUY:
        return "MODERATE_BUY_PRESSURE", "Moderate buy pressure"
    if ratio < TAKER_STRONG_SELL:
        return "STRONG_SELL_PRESSURE", "Strong sell pressure - Takers selling aggressively"
    if ratio < TAKER_MODERATE_SELL:
        return "MODERATE_SELL_PRESSURE", "Moderate sell pressure"
    return "BALANCED", "Balanced pressure between buyers and sellers"


def _get_taker_buy_sell_ratio(
    symbol: str = "BTC",
    exchange: str = "okx",
    period: str = "5m"
) -> Dict[str, Any]:
    """
    Get taker buy/sell volume ratio.

    Taker Buy > Taker Sell = Buy pressure
    Taker Sell > Taker Buy = Sell pressure

    Sources (public, no key): OKX Rubik contracts taker volume (default; all OKX
    contracts for the coin) or Binance futures data (geo-blocked from US cloud IPs).

    Args:
        symbol: Symbol (BTC, ETH, etc.)
        exchange: Data source: "okx" (default) or "binance"
        period: Period (5m, 15m, 30m, 1h, 4h, 1d)

    Returns:
        Taker buy/sell ratio with pressure analysis
    """
    exchange = (exchange or "okx").strip().lower()
    if exchange not in RATIO_SOURCES:
        return {
            "success": False,
            "error": f"Taker buy/sell ratio available on {', '.join(RATIO_SOURCES)} (got '{exchange}')",
            "error_type": "UnsupportedExchange",
            "exchange": exchange
        }

    try:
        symbol = validate_symbol(symbol)
        base = symbol.replace("/", "").replace(":USDT", "")

        if exchange == "okx":
            okx_period = _okx_period(period, ccy_level=True)
            rows = _okx_rubik("taker-volume", {"ccy": base, "instType": "CONTRACTS", "period": okx_period})
            if not rows:
                raise HttpError(f"OKX returned no taker volume for {base}", None, "taker-volume")
            # Row layout per OKX docs: [ts, sellVol, buyVol]
            ts, sell_vol, buy_vol = rows[0][0], float(rows[0][1]), float(rows[0][2])
            data = {"ts": ts, "sellVol": rows[0][1], "buyVol": rows[0][2]}
            source, sym = "okx_rubik", f"{base} (OKX contracts)"
        else:
            data = _binance_futures_data("takerlongshortRatio", base, period)
            buy_vol = float(data.get('buyVol', 0))
            sell_vol = float(data.get('sellVol', 0))
            source, sym, okx_period = "binance_futures_data", f"{base}USDT", period

        if exchange == "okx":
            buy_sell_ratio = buy_vol / sell_vol if sell_vol else 0.0
        else:
            buy_sell_ratio = float(data.get('buySellRatio', 0))

        pressure, interpretation = _taker_pressure(buy_sell_ratio)

        return {
            "success": True,
            "exchange": exchange,
            "source": source,
            "symbol": sym,
            "period": okx_period,
            "buy_sell_ratio": round(buy_sell_ratio, 4),
            "taker_buy_volume": buy_vol,
            "taker_sell_volume": sell_vol,
            "market_pressure": pressure,
            "interpretation": interpretation,
            "raw_data": data
        }

    except Exception as e:
        logger.warning("get_taker_buy_sell_ratio failed: %s", e)
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "exchange": exchange,
            "symbol": symbol
        }


def _calculate_liquidation_levels(
    symbol: str = "BTC",
    exchange: str = "okx",
    current_price: Optional[float] = None
) -> Dict[str, Any]:
    """
    Calculate estimated liquidation levels for different leverages.

    Args:
        symbol: Symbol (BTC, ETH, etc.)
        exchange: Exchange
        current_price: Current price (fetched automatically if not provided)

    Returns:
        Liquidation levels for longs and shorts at different leverages
    """
    try:
        symbol = validate_symbol(symbol)
        exchange = validate_exchange(exchange, supported=set(RELIABLE_FUTURES_EXCHANGES.keys()))

        formatted_symbol = _format_symbol(symbol)

        exchange_used = exchange
        if current_price is None:
            ticker, exchange_used = _venue_call(exchange, lambda ex: ex.fetch_ticker(formatted_symbol))
            current_price = ticker['last']

        # Common leverages
        leverages = [5, 10, 20, 50, 100]

        liquidation_levels = {
            "longs": {},
            "shorts": {}
        }

        for lev in leverages:
            # LONG liquidation: price * (1 - 1/leverage - fees)
            long_liq = current_price * (1 - (1/lev) - 0.005)
            liquidation_levels["longs"][f"{lev}x"] = round(long_liq, 2)

            # SHORT liquidation: price * (1 + 1/leverage + fees)
            short_liq = current_price * (1 + (1/lev) + 0.005)
            liquidation_levels["shorts"][f"{lev}x"] = round(short_liq, 2)

        # Critical zones (where most liquidity accumulates)
        critical_zones = {
            "long_liquidations": {
                "10x_zone": f"${round(liquidation_levels['longs']['10x'], 0)} (10x leverage)",
                "20x_zone": f"${round(liquidation_levels['longs']['20x'], 0)} (20x leverage)"
            },
            "short_liquidations": {
                "10x_zone": f"${round(liquidation_levels['shorts']['10x'], 0)} (10x leverage)",
                "20x_zone": f"${round(liquidation_levels['shorts']['20x'], 0)} (20x leverage)"
            }
        }

        return {
            "success": True,
            "exchange": exchange,
            "exchange_used": exchange_used,
            "symbol": formatted_symbol,
            "current_price": current_price,
            "liquidation_levels": liquidation_levels,
            "critical_zones": critical_zones,
            "interpretation": "Liquidation zones are price magnets - High probability of hunting"
        }

    except Exception as e:
        logger.exception("calculate_liquidation_levels failed")
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "exchange": exchange,
            "symbol": symbol
        }


@mcp.tool()
def get_perpetual_stats(
    symbol: str = "BTC",
    exchange: str = "okx"
) -> Dict[str, Any]:
    """
    Get complete perpetual statistics (funding rate, OI, ratios, liquidations).

    Consolidates all previous tools into a single comprehensive analysis.

    Args:
        symbol: Symbol (BTC, ETH, etc.)
        exchange: Exchange

    Returns:
        Complete perpetual analysis with scoring
    """
    try:
        symbol = validate_symbol(symbol)
        exchange = validate_exchange(exchange, supported=set(RELIABLE_FUTURES_EXCHANGES.keys()))

        funding = _get_funding_rate(symbol, exchange)
        oi = _get_open_interest(symbol, exchange)

        ratio_source = exchange if exchange in RATIO_SOURCES else "okx"
        ls_ratio = _get_long_short_ratio(symbol, ratio_source)
        taker_ratio = _get_taker_buy_sell_ratio(symbol, ratio_source)

        current_price = oi.get('current_price') if oi.get('success') else None
        liq_levels = _calculate_liquidation_levels(symbol, exchange, current_price)

        # Signal scoring (0-100)
        score = 50  # Neutral
        signals = []

        # Funding rate analysis
        if funding.get('success'):
            annual_rate = funding['annual_rate_percentage']
            if abs(annual_rate) > FUNDING_EXTREME_THRESHOLD:
                if annual_rate > FUNDING_EXTREME_THRESHOLD:
                    score -= SCORE_FUNDING_EXTREME
                    signals.append("Extremely high funding - Correction risk")
                else:
                    score += SCORE_FUNDING_EXTREME
                    signals.append("Extremely low funding - Possible bounce")

        # Long/Short ratio analysis
        if ls_ratio and ls_ratio.get('success'):
            ratio = ls_ratio['top_trader_long_short_ratio']
            if ratio > LS_EXTREMELY_BULLISH:
                score -= SCORE_LS_RATIO
                signals.append("Top traders very long - Crowded trade")
            elif ratio < LS_EXTREMELY_BEARISH:
                score += SCORE_LS_RATIO
                signals.append("Top traders very short - Contrarian setup")

        # Taker pressure analysis
        if taker_ratio and taker_ratio.get('success'):
            pressure = taker_ratio['market_pressure']
            if pressure == "STRONG_BUY_PRESSURE":
                score += SCORE_TAKER_PRESSURE
                signals.append("Strong buy pressure")
            elif pressure == "STRONG_SELL_PRESSURE":
                score -= SCORE_TAKER_PRESSURE
                signals.append("Strong sell pressure")

        # Overall signal
        if score >= 70:
            overall_signal = "STRONG_BUY"
        elif score >= 55:
            overall_signal = "BUY"
        elif score <= 30:
            overall_signal = "STRONG_SELL"
        elif score <= 45:
            overall_signal = "SELL"
        else:
            overall_signal = "NEUTRAL"

        return {
            "success": True,
            "exchange": exchange,
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "score": score,
            "overall_signal": overall_signal,
            "signals": signals,
            "funding_rate_data": funding if funding.get('success') else None,
            "open_interest_data": oi if oi.get('success') else None,
            "long_short_ratio_data": ls_ratio if ls_ratio and ls_ratio.get('success') else None,
            "taker_ratio_data": taker_ratio if taker_ratio and taker_ratio.get('success') else None,
            "liquidation_levels": liq_levels if liq_levels.get('success') else None
        }

    except Exception as e:
        logger.exception("get_perpetual_stats failed")
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "exchange": exchange,
            "symbol": symbol
        }


def _compare_funding_rates(
    symbol: str = "BTC",
    exchanges: List[str] = None
) -> Dict[str, Any]:
    """
    Compare funding rates across multiple exchanges.

    Args:
        symbol: Symbol (BTC, ETH, etc.)
        exchanges: List of exchanges (default: all supported)

    Returns:
        Funding rate comparison with arbitrage detection
    """
    if exchanges is None:
        exchanges = [n for n in RELIABLE_FUTURES_EXCHANGES.keys() if n not in _BLOCKED_VENUES] \
            or list(RELIABLE_FUTURES_EXCHANGES.keys())

    try:
        symbol = validate_symbol(symbol)

        results = {}
        rates = []

        for exchange in exchanges:
            try:
                funding = _get_funding_rate(symbol, exchange)
                if funding.get('success') and funding.get('exchange_used', exchange) == exchange:
                    results[exchange] = funding
                    rates.append({
                        'exchange': exchange,
                        'rate': funding['funding_rate'],
                        'annual_rate': funding['annual_rate_percentage']
                    })
            except Exception:
                continue

        if not rates:
            return {
                "success": False,
                "error": "Could not fetch funding rates from any exchange",
                "error_type": "NoDataError"
            }

        rates_sorted = sorted(rates, key=lambda x: x['rate'])

        lowest = rates_sorted[0]
        highest = rates_sorted[-1]
        spread = highest['rate'] - lowest['rate']
        spread_annual = spread * 3 * 365 * 100

        arbitrage_opportunity = spread_annual > MIN_ARBITRAGE_SPREAD_ANNUAL

        return {
            "success": True,
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "exchanges_compared": len(rates),
            "lowest_funding": lowest,
            "highest_funding": highest,
            "spread": spread,
            "spread_annual_percentage": round(spread_annual, 2),
            "arbitrage_opportunity": arbitrage_opportunity,
            "arbitrage_strategy": "Long on exchange with lowest funding, Short on exchange with highest funding" if arbitrage_opportunity else None,
            "all_rates": rates_sorted,
            "detailed_data": results
        }

    except Exception as e:
        logger.exception("compare_funding_rates failed")
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "symbol": symbol
        }


@mcp.tool()
def analyze_funding_trend(
    symbol: str = "BTC",
    exchange: str = "okx",
    hours: int = 48
) -> Dict[str, Any]:
    """
    Analyze funding rate trend over time.

    Args:
        symbol: Symbol (BTC, ETH, etc.)
        exchange: Exchange
        hours: Hours of history (default 48)

    Returns:
        Trend analysis with next funding prediction
    """
    try:
        symbol = validate_symbol(symbol)
        exchange = validate_exchange(exchange, supported=set(RELIABLE_FUTURES_EXCHANGES.keys()))
        hours = validate_positive_int(hours, "hours", max_value=720)

        history = _get_funding_rate_history(symbol, exchange, hours)

        if not history.get('success'):
            return history

        rates = [h['fundingRate'] for h in history['history']]

        if len(rates) >= 8:
            recent_4 = statistics.mean(rates[-4:])
            older_4 = statistics.mean(rates[-8:-4])

            change_pct = ((recent_4 - older_4) / abs(older_4)) * 100 if older_4 != 0 else 0

            if change_pct > TREND_RAPID_INCREASE_PCT:
                trend = "RAPIDLY_INCREASING"
                interpretation = "Funding rising rapidly - Market overheating"
            elif change_pct > TREND_INCREASE_PCT:
                trend = "INCREASING"
                interpretation = "Funding increasing - Longs dominating"
            elif change_pct < TREND_RAPID_DECREASE_PCT:
                trend = "RAPIDLY_DECREASING"
                interpretation = "Funding dropping rapidly - Capitulation"
            elif change_pct < TREND_DECREASE_PCT:
                trend = "DECREASING"
                interpretation = "Funding decreasing - Shorts dominating"
            else:
                trend = "STABLE"
                interpretation = "Stable funding - Balanced market"
        else:
            trend = "INSUFFICIENT_DATA"
            interpretation = "Not enough data for trend analysis"
            change_pct = 0

        # Funding volatility
        if len(rates) >= 8:
            volatility = statistics.stdev(rates[-8:])
            vol_label = "HIGH" if volatility > FUNDING_VOL_HIGH else "MODERATE" if volatility > FUNDING_VOL_MODERATE else "LOW"
        else:
            volatility = 0
            vol_label = "UNKNOWN"

        return {
            "success": True,
            "exchange": exchange,
            "symbol": symbol,
            "period_hours": hours,
            "trend": trend,
            "change_percentage": round(change_pct, 2),
            "interpretation": interpretation,
            "volatility": volatility,
            "volatility_label": vol_label,
            "current_rate": rates[-1],
            "average_rate": history['average_rate'],
            "max_rate": history['max_rate'],
            "min_rate": history['min_rate']
        }

    except Exception as e:
        logger.exception("analyze_funding_trend failed")
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "exchange": exchange,
            "symbol": symbol
        }


@mcp.tool()
def detect_funding_arbitrage(
    symbol: str = "BTC",
    min_spread_annual: float = 10.0
) -> Dict[str, Any]:
    """
    Detect funding rate arbitrage opportunities between exchanges.

    Strategy: Long on exchange with low funding, Short on exchange with high funding.

    Args:
        symbol: Symbol (BTC, ETH, etc.)
        min_spread_annual: Minimum annualized spread to consider arbitrage (%)

    Returns:
        Arbitrage opportunities with spread and recommended strategy
    """
    try:
        symbol = validate_symbol(symbol)

        comparison = _compare_funding_rates(symbol)

        if not comparison.get('success'):
            return comparison

        spread_annual = comparison['spread_annual_percentage']

        if spread_annual < min_spread_annual:
            return {
                "success": True,
                "symbol": symbol,
                "arbitrage_opportunity": False,
                "spread_annual_percentage": spread_annual,
                "min_required_spread": min_spread_annual,
                "message": f"Current spread ({spread_annual:.2f}%) below minimum required ({min_spread_annual}%)"
            }

        lowest = comparison['lowest_funding']
        highest = comparison['highest_funding']

        # Estimated profit (assuming 8 hours per funding, 3 times per day)
        profit_per_funding = comparison['spread']
        daily_profit_pct = profit_per_funding * 3 * 100

        return {
            "success": True,
            "symbol": symbol,
            "arbitrage_opportunity": True,
            "spread_annual_percentage": spread_annual,
            "estimated_daily_profit_percentage": round(daily_profit_pct, 4),
            "strategy": {
                "action": "FUNDING_RATE_ARBITRAGE",
                "long_exchange": lowest['exchange'],
                "long_funding_rate": lowest['rate'],
                "short_exchange": highest['exchange'],
                "short_funding_rate": highest['rate'],
                "hedge_ratio": "1:1 (delta neutral)",
                "risk": "Exchange risk, liquidation risk, funding convergence"
            },
            "execution_steps": [
                f"1. Deposit collateral on {lowest['exchange']} and {highest['exchange']}",
                f"2. Open LONG on {lowest['exchange']} (funding rate: {lowest['rate']:.6f})",
                f"3. Open SHORT on {highest['exchange']} (funding rate: {highest['rate']:.6f})",
                "4. Keep positions balanced (delta neutral)",
                "5. Close when spread decreases or is no longer profitable"
            ],
            "warnings": [
                "Requires collateral on both exchanges",
                "Liquidation risk if price moves significantly",
                "Funding rates can converge quickly",
                "Trading fees may reduce profit"
            ]
        }

    except Exception as e:
        logger.exception("detect_funding_arbitrage failed")
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "symbol": symbol
        }


# ---------------------------------------------------------------------------
# Public MCP tool wrappers.
#
# FastMCP's @mcp.tool() replaces the decorated name with a FunctionTool object
# that is NOT callable, so a tool calling another tool as a plain function blew
# up with `TypeError: 'FunctionTool' object is not callable` (hit by
# get_perpetual_stats, compare_funding_rates, analyze_funding_trend and
# detect_funding_arbitrage). Fix: the real logic lives in the private `_name`
# functions above; internal callers use those, and these thin wrappers expose
# each as an MCP tool. functools.wraps copies the docstring/signature so the
# tool schemas the agents see are byte-identical to before. NOTE: functools.wraps
# copies __name__ from the private `_name` fn, so we pin each tool's public name
# explicitly via name=... — otherwise the tools would register as `_get_funding_rate`.
# ---------------------------------------------------------------------------


@mcp.tool(name="get_funding_rate")
@functools.wraps(_get_funding_rate)
def get_funding_rate(symbol: str = "BTC", exchange: str = "okx") -> Dict[str, Any]:
    return _get_funding_rate(symbol, exchange)


@mcp.tool(name="get_funding_rate_history")
@functools.wraps(_get_funding_rate_history)
def get_funding_rate_history(
    symbol: str = "BTC", exchange: str = "okx", hours: int = 24
) -> Dict[str, Any]:
    return _get_funding_rate_history(symbol, exchange, hours)


@mcp.tool(name="get_open_interest")
@functools.wraps(_get_open_interest)
def get_open_interest(symbol: str = "BTC", exchange: str = "okx") -> Dict[str, Any]:
    return _get_open_interest(symbol, exchange)


@mcp.tool(name="get_long_short_ratio")
@functools.wraps(_get_long_short_ratio)
def get_long_short_ratio(
    symbol: str = "BTC", exchange: str = "okx", period: str = "5m"
) -> Dict[str, Any]:
    return _get_long_short_ratio(symbol, exchange, period)


@mcp.tool(name="get_taker_buy_sell_ratio")
@functools.wraps(_get_taker_buy_sell_ratio)
def get_taker_buy_sell_ratio(
    symbol: str = "BTC", exchange: str = "okx", period: str = "5m"
) -> Dict[str, Any]:
    return _get_taker_buy_sell_ratio(symbol, exchange, period)


@mcp.tool(name="calculate_liquidation_levels")
@functools.wraps(_calculate_liquidation_levels)
def calculate_liquidation_levels(
    symbol: str = "BTC", exchange: str = "okx", current_price: Optional[float] = None
) -> Dict[str, Any]:
    return _calculate_liquidation_levels(symbol, exchange, current_price)


@mcp.tool(name="compare_funding_rates")
@functools.wraps(_compare_funding_rates)
def compare_funding_rates(
    symbol: str = "BTC", exchanges: List[str] = None
) -> Dict[str, Any]:
    return _compare_funding_rates(symbol, exchanges)


if __name__ == "__main__":
    mcp.run()
