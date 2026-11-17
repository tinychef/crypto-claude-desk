"""
Tests for crypto_futures_data.py (crypto-futures) MCP server.
All CCXT and HTTP calls are mocked.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers"))
from helpers import call_tool


def _patch_internal_tools():
    """Patch internal FunctionTool references so they call .fn directly.
    
    Some tools internally call other @mcp.tool() decorated functions, which are
    FunctionTool objects. We patch these references to use the .fn attribute.
    """
    import crypto_futures_data as mod
    patches = []
    tool_names = [
        'get_funding_rate', 'get_funding_rate_history', 'get_open_interest',
        'get_long_short_ratio', 'get_taker_buy_sell_ratio',
        'compare_funding_rates',
        'calculate_liquidation_levels',
    ]
    for name in tool_names:
        tool = getattr(mod, name, None)
        if tool and hasattr(tool, 'fn'):
            patches.append(patch(f"crypto_futures_data.{name}", tool.fn))
    return patches


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _make_mock_exchange():
    ex = MagicMock()
    ex.fetch_funding_rate.return_value = {
        "fundingRate": 0.0001,
        "fundingTimestamp": 1740000000000,
        "timestamp": 1740000000000,
    }
    ex.fetch_funding_rate_history.return_value = [
        {"fundingRate": 0.0001 + i * 0.00001, "timestamp": 1740000000000 + i * 28800000}
        for i in range(20)
    ]
    # ccxt unified: openInterestAmount = contracts/base units, openInterestValue = USD notional
    ex.fetch_open_interest.return_value = {
        "openInterestAmount": 50000,
        "openInterestValue": 5_000_000_000,
        "timestamp": 1740000000000,
    }
    ex.fetch_ticker.return_value = {
        "last": 97500.0, "bid": 97490.0, "ask": 97510.0,
        "baseVolume": 1234.5, "timestamp": 1740000000000,
    }
    return ex


MOCK_FUTURES_EXCHANGES = {
    "binance": _make_mock_exchange(),
    "bybit": _make_mock_exchange(),
    "okx": _make_mock_exchange(),
    "bitget": _make_mock_exchange(),
    "mexc": _make_mock_exchange(),
}


def _patch_exchanges():
    return patch("crypto_futures_data.RELIABLE_FUTURES_EXCHANGES", MOCK_FUTURES_EXCHANGES)


def _patch_get_exchange():
    def getter(name):
        if name in MOCK_FUTURES_EXCHANGES:
            return MOCK_FUTURES_EXCHANGES[name]
        raise ValueError(f"Unsupported: {name}")
    return patch("crypto_futures_data._get_exchange", side_effect=getter)


def _mock_http_response(json_data, status_code=200):
    """Return a side_effect for ``crypto_futures_data.http_get_json``.

    V2 uses desk_common.http_get_json (raises HttpError on non-200) instead of requests.get.
    """
    from desk_common import HttpError

    def _fake(url, params=None, **kwargs):
        if status_code != 200:
            raise HttpError(f"HTTP {status_code} from {url}", status_code, url)
        return json_data
    return _fake


def _patch_http(fake):
    return patch("crypto_futures_data.http_get_json", side_effect=fake)


# ---------------------------------------------------------------------------
# Tests — get_funding_rate
# ---------------------------------------------------------------------------

class TestGetFundingRate:
    def test_success(self):
        with _patch_exchanges(), _patch_get_exchange():
            from crypto_futures_data import get_funding_rate
            result = call_tool(get_funding_rate, "BTC", "binance")
        assert result["success"] is True
        assert "funding_rate" in result
        assert "annual_rate_percentage" in result

    def test_neutral_bias(self):
        ex = _make_mock_exchange()
        ex.fetch_funding_rate.return_value = {
            "fundingRate": 0.00001,
            "fundingTimestamp": 1740000000000,
        }
        with patch("crypto_futures_data.RELIABLE_FUTURES_EXCHANGES", {"binance": ex}), \
             patch("crypto_futures_data._get_exchange", return_value=ex):
            from crypto_futures_data import get_funding_rate
            result = call_tool(get_funding_rate, "BTC", "binance")
        assert result["success"] is True
        assert result["market_bias"] == "NEUTRAL"

    def test_bullish_extreme_bias(self):
        ex = _make_mock_exchange()
        ex.fetch_funding_rate.return_value = {
            "fundingRate": 0.001,
            "fundingTimestamp": 1740000000000,
        }
        with patch("crypto_futures_data.RELIABLE_FUTURES_EXCHANGES", {"binance": ex}), \
             patch("crypto_futures_data._get_exchange", return_value=ex):
            from crypto_futures_data import get_funding_rate
            result = call_tool(get_funding_rate, "BTC", "binance")
        assert result["market_bias"] == "BULLISH_EXTREME"

    def test_bearish_extreme_bias(self):
        ex = _make_mock_exchange()
        ex.fetch_funding_rate.return_value = {
            "fundingRate": -0.001,
            "fundingTimestamp": 1740000000000,
        }
        with patch("crypto_futures_data.RELIABLE_FUTURES_EXCHANGES", {"binance": ex}), \
             patch("crypto_futures_data._get_exchange", return_value=ex):
            from crypto_futures_data import get_funding_rate
            result = call_tool(get_funding_rate, "BTC", "binance")
        assert result["market_bias"] == "BEARISH_EXTREME"

    def test_error_handling(self):
        ex = _make_mock_exchange()
        ex.fetch_funding_rate.side_effect = Exception("Network error")
        with patch("crypto_futures_data.RELIABLE_FUTURES_EXCHANGES", {"binance": ex}), \
             patch("crypto_futures_data._get_exchange", return_value=ex):
            from crypto_futures_data import get_funding_rate
            result = call_tool(get_funding_rate, "BTC", "binance")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Tests — get_funding_rate_history
# ---------------------------------------------------------------------------

class TestGetFundingRateHistory:
    def test_success(self):
        with _patch_exchanges(), _patch_get_exchange():
            from crypto_futures_data import get_funding_rate_history
            result = call_tool(get_funding_rate_history, "BTC", "binance", 24)
        assert result["success"] is True
        assert "average_rate" in result
        assert "total_fundings" in result

    def test_trend_detection(self):
        with _patch_exchanges(), _patch_get_exchange():
            from crypto_futures_data import get_funding_rate_history
            result = call_tool(get_funding_rate_history, "BTC", "binance", 48)
        assert result["trend"] in ("INCREASING", "DECREASING", "INSUFFICIENT_DATA")

    def test_empty_history(self):
        ex = _make_mock_exchange()
        ex.fetch_funding_rate_history.return_value = []
        with patch("crypto_futures_data.RELIABLE_FUTURES_EXCHANGES", {"binance": ex}), \
             patch("crypto_futures_data._get_exchange", return_value=ex):
            from crypto_futures_data import get_funding_rate_history
            result = call_tool(get_funding_rate_history, "BTC", "binance", 24)
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Tests — get_open_interest
# ---------------------------------------------------------------------------

class TestGetOpenInterest:
    def test_success(self):
        with _patch_exchanges(), _patch_get_exchange():
            from crypto_futures_data import get_open_interest
            result = call_tool(get_open_interest, "BTC", "binance")
        assert result["success"] is True
        assert "open_interest_usd" in result

    def test_error_handling(self):
        ex = _make_mock_exchange()
        ex.fetch_open_interest.side_effect = Exception("Not supported")
        with patch("crypto_futures_data.RELIABLE_FUTURES_EXCHANGES", {"binance": ex}), \
             patch("crypto_futures_data._get_exchange", return_value=ex):
            from crypto_futures_data import get_open_interest
            result = call_tool(get_open_interest, "BTC", "binance")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Tests — get_long_short_ratio
# ---------------------------------------------------------------------------

class TestGetLongShortRatio:
    def test_unsupported_source_returns_error(self):
        from crypto_futures_data import get_long_short_ratio
        result = call_tool(get_long_short_ratio, "BTC", "bybit")
        assert result["success"] is False
        assert "okx" in result["error"] and "binance" in result["error"]

    def test_success_neutral(self):
        resp = _mock_http_response([{"longShortRatio": "1.0", "longAccount": "0.5", "shortAccount": "0.5"}])
        with _patch_http(resp):
            from crypto_futures_data import get_long_short_ratio
            result = call_tool(get_long_short_ratio, "BTC", "binance")
        assert result["success"] is True
        assert result["top_trader_sentiment"] == "NEUTRAL"

    def test_extremely_bullish(self):
        resp = _mock_http_response([{"longShortRatio": "2.5"}])
        with _patch_http(resp):
            from crypto_futures_data import get_long_short_ratio
            result = call_tool(get_long_short_ratio, "BTC", "binance")
        assert result["top_trader_sentiment"] == "EXTREMELY_BULLISH"

    def test_extremely_bearish(self):
        resp = _mock_http_response([{"longShortRatio": "0.4"}])
        with _patch_http(resp):
            from crypto_futures_data import get_long_short_ratio
            result = call_tool(get_long_short_ratio, "BTC", "binance")
        assert result["top_trader_sentiment"] == "EXTREMELY_BEARISH"

    def test_api_error(self):
        resp = _mock_http_response([], 500)
        with _patch_http(resp):
            from crypto_futures_data import get_long_short_ratio
            result = call_tool(get_long_short_ratio, "BTC", "binance")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Tests — get_taker_buy_sell_ratio
# ---------------------------------------------------------------------------

class TestGetTakerBuySellRatio:
    def test_non_binance_error(self):
        from crypto_futures_data import get_taker_buy_sell_ratio
        result = call_tool(get_taker_buy_sell_ratio, "BTC", "bybit")
        assert result["success"] is False

    def test_balanced(self):
        resp = _mock_http_response([{"buySellRatio": "1.0", "buyVol": "1000", "sellVol": "1000"}])
        with _patch_http(resp):
            from crypto_futures_data import get_taker_buy_sell_ratio
            result = call_tool(get_taker_buy_sell_ratio, "BTC", "binance")
        assert result["market_pressure"] == "BALANCED"

    def test_strong_buy(self):
        resp = _mock_http_response([{"buySellRatio": "1.6", "buyVol": "1600", "sellVol": "1000"}])
        with _patch_http(resp):
            from crypto_futures_data import get_taker_buy_sell_ratio
            result = call_tool(get_taker_buy_sell_ratio, "BTC", "binance")
        assert result["market_pressure"] == "STRONG_BUY_PRESSURE"

    def test_strong_sell(self):
        resp = _mock_http_response([{"buySellRatio": "0.6", "buyVol": "600", "sellVol": "1000"}])
        with _patch_http(resp):
            from crypto_futures_data import get_taker_buy_sell_ratio
            result = call_tool(get_taker_buy_sell_ratio, "BTC", "binance")
        assert result["market_pressure"] == "STRONG_SELL_PRESSURE"


# ---------------------------------------------------------------------------
# Tests — calculate_liquidation_levels
# ---------------------------------------------------------------------------

class TestCalculateLiquidationLevels:
    def test_success(self):
        with _patch_exchanges(), _patch_get_exchange():
            from crypto_futures_data import calculate_liquidation_levels
            result = call_tool(calculate_liquidation_levels, "BTC", "binance")
        assert result["success"] is True
        assert "liquidation_levels" in result
        assert "longs" in result["liquidation_levels"]
        assert "shorts" in result["liquidation_levels"]

    def test_leverages_present(self):
        with _patch_exchanges(), _patch_get_exchange():
            from crypto_futures_data import calculate_liquidation_levels
            result = call_tool(calculate_liquidation_levels, "BTC", "binance")
        longs = result["liquidation_levels"]["longs"]
        assert "5x" in longs
        assert "100x" in longs

    def test_long_liq_below_price(self):
        with _patch_exchanges(), _patch_get_exchange():
            from crypto_futures_data import calculate_liquidation_levels
            result = call_tool(calculate_liquidation_levels, "BTC", "binance", 97500.0)
        for lev, price in result["liquidation_levels"]["longs"].items():
            assert price < 97500.0

    def test_short_liq_above_price(self):
        with _patch_exchanges(), _patch_get_exchange():
            from crypto_futures_data import calculate_liquidation_levels
            result = call_tool(calculate_liquidation_levels, "BTC", "binance", 97500.0)
        for lev, price in result["liquidation_levels"]["shorts"].items():
            assert price > 97500.0

    def test_custom_price(self):
        with _patch_exchanges(), _patch_get_exchange():
            from crypto_futures_data import calculate_liquidation_levels
            result = call_tool(calculate_liquidation_levels, "BTC", "binance", 50000.0)
        assert result["current_price"] == 50000.0


# ---------------------------------------------------------------------------
# Tests — get_perpetual_stats
# ---------------------------------------------------------------------------

class TestGetPerpetualStats:
    def test_success(self):
        resp = _mock_http_response([{"longShortRatio": "1.0", "buySellRatio": "1.0", "buyVol": "1000", "sellVol": "1000"}])
        internal = _patch_internal_tools()
        with _patch_exchanges(), _patch_get_exchange(), \
             _patch_http(resp):
            for p in internal:
                p.start()
            try:
                from crypto_futures_data import get_perpetual_stats
                result = call_tool(get_perpetual_stats, "BTC", "binance")
            finally:
                for p in internal:
                    p.stop()
        assert result["success"] is True
        assert "score" in result
        assert "overall_signal" in result

    def test_signal_values(self):
        resp = _mock_http_response([{"longShortRatio": "1.0", "buySellRatio": "1.0", "buyVol": "1000", "sellVol": "1000"}])
        internal = _patch_internal_tools()
        with _patch_exchanges(), _patch_get_exchange(), \
             _patch_http(resp):
            for p in internal:
                p.start()
            try:
                from crypto_futures_data import get_perpetual_stats
                result = call_tool(get_perpetual_stats, "BTC", "binance")
            finally:
                for p in internal:
                    p.stop()
        assert result["overall_signal"] in (
            "STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL"
        )


# ---------------------------------------------------------------------------
# Tests — compare_funding_rates
# ---------------------------------------------------------------------------

class TestCompareFundingRates:
    def test_success(self):
        internal = _patch_internal_tools()
        with _patch_exchanges(), _patch_get_exchange():
            for p in internal:
                p.start()
            try:
                from crypto_futures_data import compare_funding_rates
                result = call_tool(compare_funding_rates, "BTC")
            finally:
                for p in internal:
                    p.stop()
        assert result["success"] is True
        assert "lowest_funding" in result
        assert "highest_funding" in result

    def test_specific_exchanges(self):
        internal = _patch_internal_tools()
        with _patch_exchanges(), _patch_get_exchange():
            for p in internal:
                p.start()
            try:
                from crypto_futures_data import compare_funding_rates
                result = call_tool(compare_funding_rates, "BTC", ["binance", "bybit"])
            finally:
                for p in internal:
                    p.stop()
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Tests — analyze_funding_trend
# ---------------------------------------------------------------------------

class TestAnalyzeFundingTrend:
    def test_success(self):
        internal = _patch_internal_tools()
        with _patch_exchanges(), _patch_get_exchange():
            for p in internal:
                p.start()
            try:
                from crypto_futures_data import analyze_funding_trend
                result = call_tool(analyze_funding_trend, "BTC", "binance", 48)
            finally:
                for p in internal:
                    p.stop()
        assert result["success"] is True
        assert "trend" in result

    def test_trend_values(self):
        internal = _patch_internal_tools()
        with _patch_exchanges(), _patch_get_exchange():
            for p in internal:
                p.start()
            try:
                from crypto_futures_data import analyze_funding_trend
                result = call_tool(analyze_funding_trend, "BTC", "binance", 48)
            finally:
                for p in internal:
                    p.stop()
        assert result["trend"] in (
            "RAPIDLY_INCREASING", "INCREASING", "STABLE",
            "DECREASING", "RAPIDLY_DECREASING", "INSUFFICIENT_DATA"
        )


# ---------------------------------------------------------------------------
# Tests — detect_funding_arbitrage
# ---------------------------------------------------------------------------

class TestDetectFundingArbitrage:
    def test_no_opportunity(self):
        internal = _patch_internal_tools()
        with _patch_exchanges(), _patch_get_exchange():
            for p in internal:
                p.start()
            try:
                from crypto_futures_data import detect_funding_arbitrage
                result = call_tool(detect_funding_arbitrage, "BTC")
            finally:
                for p in internal:
                    p.stop()
        assert result["success"] is True

    def test_with_spread(self):
        exchanges = {}
        for i, name in enumerate(["binance", "bybit", "okx", "bitget", "mexc"]):
            ex = _make_mock_exchange()
            ex.fetch_funding_rate.return_value = {
                "fundingRate": 0.0001 + i * 0.001,
                "fundingTimestamp": 1740000000000,
                "timestamp": 1740000000000,
            }
            exchanges[name] = ex
        internal = _patch_internal_tools()
        with patch("crypto_futures_data.RELIABLE_FUTURES_EXCHANGES", exchanges), \
             patch("crypto_futures_data._get_exchange", side_effect=lambda n: exchanges[n]):
            for p in internal:
                p.start()
            try:
                from crypto_futures_data import detect_funding_arbitrage
                result = call_tool(detect_funding_arbitrage, "BTC")
            finally:
                for p in internal:
                    p.stop()
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Tests — _format_symbol helper
# ---------------------------------------------------------------------------

class TestFormatSymbol:
    def test_simple_symbol(self):
        from crypto_futures_data import _format_symbol
        assert _format_symbol("BTC") == "BTC/USDT:USDT"

    def test_with_slash(self):
        from crypto_futures_data import _format_symbol
        assert _format_symbol("BTC/USDT") == "BTC/USDT:USDT"

    def test_already_formatted(self):
        from crypto_futures_data import _format_symbol
        assert _format_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"


# ---------------------------------------------------------------------------
# V2 regression tests — OI labels, OKX Rubik ratios, okx default, serializer, fallback
# ---------------------------------------------------------------------------

import inspect  # noqa: E402

import ccxt  # noqa: E402

import crypto_futures_data as fut  # noqa: E402
from desk_common import tool_serializer  # noqa: E402


class TestOpenInterestLabels:
    def _run(self, oi, markets=None, last=100_000.0):
        ex = _make_mock_exchange()
        ex.fetch_open_interest.return_value = oi
        ex.fetch_ticker.return_value = {"last": last}
        ex.markets = markets if markets is not None else {}
        with patch("crypto_futures_data._get_exchange", return_value=ex):
            return call_tool(fut.get_open_interest, "BTC", "okx")

    def test_usd_comes_from_open_interest_value(self, mock_open_interest):
        result = self._run(mock_open_interest)
        assert result["open_interest_usd"] == 5_000_000_000
        assert result["open_interest_contracts"] == 50000
        assert result["open_interest_base"] == 50000  # USD / price when contract size unknown

    def test_okx_shape_contracts_vs_base(self):
        # live OKX shape: contracts in openInterestAmount, base coin in baseVolume (oiCcy)
        oi = {"openInterestAmount": 3_035_409.38, "openInterestValue": 1_963_093_343.7,
              "baseVolume": 30_354.09, "timestamp": 1794878309086}
        result = self._run(oi, last=64_700.0)
        assert result["open_interest_contracts"] == 3_035_409.38
        assert result["open_interest_base"] == 30_354.09
        assert result["open_interest_usd"] == 1_963_093_343.7

    def test_contract_size_used_when_no_base(self):
        oi = {"openInterestAmount": 1000, "openInterestValue": None, "timestamp": 1}
        result = self._run(oi, markets={"BTC/USDT:USDT": {"contractSize": 0.01}}, last=50_000.0)
        assert result["open_interest_base"] == 10.0
        assert result["open_interest_usd"] == 500_000.0


def _okx_fake(rows_by_path, code="0", calls=None):
    def _fake(url, params=None, **kwargs):
        if calls is not None:
            calls.append((url, params))
        for path, rows in rows_by_path.items():
            if url.endswith(path):
                return {"code": code, "msg": "" if code == "0" else "Parameter error", "data": rows}
        raise AssertionError(f"unexpected url {url}")
    return _fake


class TestOkxRubikRatios:
    def test_long_short_default_is_okx(self):
        calls = []
        fake = _okx_fake({
            "contracts/long-short-account-ratio": [["1794878100000", "2.67"]],
            "contracts/long-short-position-ratio-contract-top-trader": [["1794878100000", "0.91"]],
        }, calls=calls)
        with patch("crypto_futures_data.http_get_json", side_effect=fake):
            result = call_tool(fut.get_long_short_ratio, "BTC")
        assert result["success"] is True
        assert result["exchange"] == "okx"
        assert result["global_long_short_ratio"] == 2.67
        assert result["top_trader_long_short_ratio"] == 0.91
        assert result["divergence"] is True
        assert all("okx.com/api/v5/rubik/stat" in u for u, _ in calls)
        assert calls[0][1] == {"ccy": "BTC", "period": "5m"}
        assert calls[1][1] == {"instId": "BTC-USDT-SWAP", "period": "5m"}

    def test_period_mapping(self):
        calls = []
        fake = _okx_fake({
            "contracts/long-short-account-ratio": [["1", "1.0"]],
            "contracts/long-short-position-ratio-contract-top-trader": [["1", "1.0"]],
        }, calls=calls)
        with patch("crypto_futures_data.http_get_json", side_effect=fake):
            result = call_tool(fut.get_long_short_ratio, "ETH", "okx", "4h")
        assert result["period"] == "1H"           # ccy endpoint only supports 5m/1H/1D
        assert calls[1][1]["period"] == "4H"       # contract endpoint supports 4H

    def test_top_trader_failure_falls_back_to_account_ratio(self):
        def fake(url, params=None, **kw):
            if url.endswith("top-trader"):
                raise fut.HttpError("HTTP 500", 500, url)
            return {"code": "0", "data": [["1", "0.4"]]}
        with patch("crypto_futures_data.http_get_json", side_effect=fake):
            result = call_tool(fut.get_long_short_ratio, "BTC")
        assert result["success"] is True
        assert result["top_trader_long_short_ratio"] == 0.4
        assert result["top_trader_sentiment"] == "EXTREMELY_BEARISH"
        assert "note" in result

    def test_okx_error_code_is_failure(self):
        fake = _okx_fake({"contracts/long-short-account-ratio": []}, code="51000")
        with patch("crypto_futures_data.http_get_json", side_effect=fake):
            result = call_tool(fut.get_long_short_ratio, "BTC")
        assert result["success"] is False
        assert "Parameter error" in result["error"]

    def test_taker_default_is_okx_and_columns_are_sell_then_buy(self):
        calls = []
        # OKX row: [ts, sellVol, buyVol] (verified live against history-trades)
        fake = _okx_fake({"taker-volume": [["1794878100000", "1000", "1600"]]}, calls=calls)
        with patch("crypto_futures_data.http_get_json", side_effect=fake):
            result = call_tool(fut.get_taker_buy_sell_ratio, "BTC")
        assert result["success"] is True
        assert result["exchange"] == "okx"
        assert result["taker_buy_volume"] == 1600.0
        assert result["taker_sell_volume"] == 1000.0
        assert result["buy_sell_ratio"] == 1.6
        assert result["market_pressure"] == "STRONG_BUY_PRESSURE"
        assert calls[0][1] == {"ccy": "BTC", "instType": "CONTRACTS", "period": "5m"}

    def test_binance_geo_block_reports_failure(self):
        with _patch_http(_mock_http_response([], 451)):
            result = call_tool(fut.get_taker_buy_sell_ratio, "BTC", "binance")
        assert result["success"] is False
        assert "451" in result["error"]

    def test_perpetual_stats_uses_okx_ratios_for_okx(self):
        fake = _okx_fake({
            "contracts/long-short-account-ratio": [["1", "1.0"]],
            "contracts/long-short-position-ratio-contract-top-trader": [["1", "1.0"]],
            "taker-volume": [["1", "1000", "1000"]],
        })
        with _patch_get_exchange(), patch("crypto_futures_data.http_get_json", side_effect=fake):
            result = call_tool(fut.get_perpetual_stats, "BTC", "okx")
        assert result["success"] is True
        assert result["long_short_ratio_data"]["source"] == "okx_rubik"
        assert result["taker_ratio_data"]["source"] == "okx_rubik"


class TestFuturesDefaultsAndFallback:
    @pytest.mark.parametrize("tool", [
        "get_funding_rate", "get_funding_rate_history", "get_open_interest",
        "get_long_short_ratio", "get_taker_buy_sell_ratio", "calculate_liquidation_levels",
        "get_perpetual_stats", "analyze_funding_trend",
    ])
    def test_default_exchange_is_okx(self, tool):
        fn = getattr(getattr(fut, tool), "fn", getattr(fut, tool))
        assert inspect.signature(fn).parameters["exchange"].default == "okx"

    def test_server_uses_numpy_safe_serializer(self):
        assert fut.mcp._tool_serializer is tool_serializer

    def test_registry_is_swap_market(self):
        sentinel = MagicMock()
        with patch("crypto_futures_data.get_exchange", return_value=sentinel) as ge:
            assert fut._get_exchange("okx") is sentinel
        ge.assert_called_once_with("okx", "swap")

    def test_funding_falls_back_when_binance_geo_blocked(self):
        binance = _make_mock_exchange()
        binance.fetch_funding_rate.side_effect = ccxt.ExchangeNotAvailable("451 restricted location")
        okx = _make_mock_exchange()
        with patch("crypto_futures_data.RELIABLE_FUTURES_EXCHANGES", {"okx": okx, "binance": binance}), \
             patch("crypto_futures_data._get_exchange", side_effect=lambda n: {"okx": okx, "binance": binance}[n]), \
             patch("crypto_futures_data.fetch_with_fallback",
                   side_effect=lambda fn, exchanges=None, market="swap": (fn(okx), "okx", [])), \
             patch("crypto_futures_data.mark_blocked") as mb:
            result = call_tool(fut.get_funding_rate, "BTC", "binance")
        assert result["success"] is True
        assert result["exchange_used"] == "okx"
        mb.assert_called_once_with("binance")

    def test_compare_ignores_fallback_duplicates(self):
        binance = _make_mock_exchange()
        binance.fetch_funding_rate.side_effect = ccxt.ExchangeNotAvailable("451 restricted location")
        okx = _make_mock_exchange()
        venues = {"okx": okx, "binance": binance}
        with patch("crypto_futures_data.RELIABLE_FUTURES_EXCHANGES", venues), \
             patch("crypto_futures_data._get_exchange", side_effect=lambda n: venues[n]), \
             patch("crypto_futures_data.fetch_with_fallback",
                   side_effect=lambda fn, exchanges=None, market="swap": (fn(okx), "okx", [])), \
             patch("crypto_futures_data.mark_blocked"):
            result = call_tool(fut.compare_funding_rates, "BTC", ["okx", "binance"])
        assert result["exchanges_compared"] == 1
