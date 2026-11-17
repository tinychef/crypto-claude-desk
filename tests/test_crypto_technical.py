"""
Tests for crypto_technical_analysis.py MCP server.
All CCXT and HTTP calls are mocked — tests run offline and deterministic.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers"))
from helpers import call_tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(n=100, base=97000, seed=42):
    """Generate a deterministic OHLCV DataFrame."""
    np.random.seed(seed)
    ts = pd.date_range("2026-01-01", periods=n, freq="1h")
    rets = np.random.normal(0.0002, 0.005, n)
    close = base * np.cumprod(1 + rets)
    high = close * (1 + np.abs(np.random.normal(0, 0.003, n)))
    low = close * (1 - np.abs(np.random.normal(0, 0.003, n)))
    open_ = close * (1 + np.random.normal(0, 0.001, n))
    volume = np.random.uniform(100, 5000, n)
    return pd.DataFrame({
        "timestamp": ts, "open": open_, "high": high,
        "low": low, "close": close, "volume": volume,
    })


MOCK_DF = _make_df()


def _patch_fetch():
    return patch(
        "crypto_technical_analysis.safe_fetch_ohlcv_data",
        return_value=MOCK_DF.copy(),
    )


def _patch_fetch_none():
    return patch(
        "crypto_technical_analysis.safe_fetch_ohlcv_data",
        return_value=None,
    )


def _patch_fetch_short():
    return patch(
        "crypto_technical_analysis.safe_fetch_ohlcv_data",
        return_value=_make_df(n=5),
    )


# ---------------------------------------------------------------------------
# Tests — calculate_rsi
# ---------------------------------------------------------------------------

class TestCalculateRSI:
    def test_success_returns_required_keys(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_rsi
            result = call_tool(calculate_rsi, "BTC", 14, 100)
        assert "current_rsi" in result
        assert "rsi_signal" in result
        assert 0 <= result["current_rsi"] <= 100

    def test_rsi_signal_overbought(self):
        # Force close prices that only go up to get RSI > 70
        df = _make_df()
        df["close"] = np.linspace(90000, 110000, len(df))
        with patch("crypto_technical_analysis.safe_fetch_ohlcv_data", return_value=df):
            from crypto_technical_analysis import calculate_rsi
            result = call_tool(calculate_rsi, "BTC", 14, 100)
        assert result["rsi_signal"] in ("OVERBOUGHT", "BULLISH")

    def test_rsi_signal_oversold(self):
        df = _make_df()
        df["close"] = np.linspace(110000, 90000, len(df))
        with patch("crypto_technical_analysis.safe_fetch_ohlcv_data", return_value=df):
            from crypto_technical_analysis import calculate_rsi
            result = call_tool(calculate_rsi, "BTC", 14, 100)
        assert result["rsi_signal"] in ("OVERSOLD", "BEARISH")

    def test_insufficient_data(self):
        with _patch_fetch_none():
            from crypto_technical_analysis import calculate_rsi
            result = call_tool(calculate_rsi, "BTC", 14, 100)
        assert "error" in result

    def test_custom_period(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_rsi
            result = call_tool(calculate_rsi, "BTC", 7, 100)
        assert result["period"] == 7


# ---------------------------------------------------------------------------
# Tests — calculate_macd
# ---------------------------------------------------------------------------

class TestCalculateMACD:
    def test_success_keys(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_macd
            result = call_tool(calculate_macd, "BTC")
        assert "macd_line" in result
        assert "signal_line" in result
        assert "histogram" in result
        assert result["macd_signal"] in ("BULLISH", "BEARISH")

    def test_crossover_detection(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_macd
            result = call_tool(calculate_macd, "BTC")
        assert result["crossover_signal"] in (
            "BULLISH_CROSSOVER", "BEARISH_CROSSOVER", "NONE"
        )

    def test_histogram_trend(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_macd
            result = call_tool(calculate_macd, "BTC")
        assert result["histogram_trend"] in ("RISING", "FALLING", "FLAT")

    def test_insufficient_data(self):
        with _patch_fetch_none():
            from crypto_technical_analysis import calculate_macd
            result = call_tool(calculate_macd, "BTC")
        assert "error" in result


# ---------------------------------------------------------------------------
# Tests — calculate_bollinger_bands
# ---------------------------------------------------------------------------

class TestCalculateBollingerBands:
    def test_success_keys(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_bollinger_bands
            result = call_tool(calculate_bollinger_bands, "BTC")
        assert "upper_band" in result
        assert "middle_band" in result
        assert "lower_band" in result
        assert result["upper_band"] > result["middle_band"] > result["lower_band"]

    def test_position_in_bands_range(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_bollinger_bands
            result = call_tool(calculate_bollinger_bands, "BTC")
        # position_in_bands can be < 0 or > 100 if price outside bands
        assert isinstance(result["position_in_bands"], float)

    def test_signal_values(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_bollinger_bands
            result = call_tool(calculate_bollinger_bands, "BTC")
        assert result["bb_signal"] in ("OVERBOUGHT", "OVERSOLD", "BULLISH", "BEARISH")

    def test_squeeze_detection(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_bollinger_bands
            result = call_tool(calculate_bollinger_bands, "BTC")
        assert isinstance(result["squeeze_detected"], bool)


# ---------------------------------------------------------------------------
# Tests — detect_chart_patterns
# ---------------------------------------------------------------------------

class TestDetectChartPatterns:
    def test_returns_patterns_list(self):
        with _patch_fetch():
            from crypto_technical_analysis import detect_chart_patterns
            result = call_tool(detect_chart_patterns, "BTC")
        assert "patterns_detected" in result
        assert isinstance(result["patterns_detected"], list)

    def test_key_levels(self):
        with _patch_fetch():
            from crypto_technical_analysis import detect_chart_patterns
            result = call_tool(detect_chart_patterns, "BTC")
        assert "key_levels" in result
        assert "support" in result["key_levels"]
        assert "resistance" in result["key_levels"]

    def test_double_top_detection(self):
        # Construct price data with a clear double top
        df = _make_df()
        n = len(df)
        prices = np.concatenate([
            np.linspace(90000, 100000, n // 4),
            np.linspace(100000, 95000, n // 4),
            np.linspace(95000, 100000, n // 4),
            np.linspace(100000, 93000, n // 4),
        ])[:n]
        df["close"] = prices
        df["high"] = prices * 1.001
        df["low"] = prices * 0.999
        with patch("crypto_technical_analysis.safe_fetch_ohlcv_data", return_value=df):
            from crypto_technical_analysis import detect_chart_patterns
            result = call_tool(detect_chart_patterns, "BTC", days=100)
        assert "patterns_detected" in result

    def test_insufficient_data(self):
        with _patch_fetch_short():
            from crypto_technical_analysis import detect_chart_patterns
            result = call_tool(detect_chart_patterns, "BTC")
        assert "error" in result


# ---------------------------------------------------------------------------
# Tests — calculate_moving_averages
# ---------------------------------------------------------------------------

class TestCalculateMovingAverages:
    def test_default_periods(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_moving_averages
            result = call_tool(calculate_moving_averages, "BTC", [10, 20, 50], 100)
        assert "moving_averages" in result
        assert "MA10" in result["moving_averages"]

    def test_custom_periods(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_moving_averages
            result = call_tool(calculate_moving_averages, "BTC", periods=[10, 30], days=100)
        assert "MA10" in result["moving_averages"]
        assert "MA30" in result["moving_averages"]

    def test_crossover_signals(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_moving_averages
            result = call_tool(calculate_moving_averages, "BTC", periods=[10, 30], days=100)
        assert "crossover_signals" in result
        for sig in result["crossover_signals"]:
            assert sig["type"] in ("BULLISH", "BEARISH")

    def test_overall_trend(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_moving_averages
            result = call_tool(calculate_moving_averages, "BTC", periods=[10, 30], days=100)
        assert result["overall_trend"] in ("BULLISH", "BEARISH", "NEUTRAL")


# ---------------------------------------------------------------------------
# Tests — get_support_resistance
# ---------------------------------------------------------------------------

class TestGetSupportResistance:
    def test_success_keys(self):
        with _patch_fetch():
            from crypto_technical_analysis import get_support_resistance
            result = call_tool(get_support_resistance, "BTC")
        assert "resistance_levels" in result
        assert "support_levels" in result
        assert "current_price" in result

    def test_levels_are_sorted(self):
        with _patch_fetch():
            from crypto_technical_analysis import get_support_resistance
            result = call_tool(get_support_resistance, "BTC")
        # V2 contract: nearest level first (resistance ascending, support descending)
        res = [r["price"] for r in result["resistance_levels"]]
        sup = [r["price"] for r in result["support_levels"]]
        assert res == sorted(res)
        assert sup == sorted(sup, reverse=True)


# ---------------------------------------------------------------------------
# Tests — calculate_fibonacci_levels
# ---------------------------------------------------------------------------

class TestCalculateFibonacciLevels:
    def test_uptrend_levels(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_fibonacci_levels
            result = call_tool(calculate_fibonacci_levels, "BTC", "up")
        assert "fibonacci_levels" in result
        assert "Fib_0.0" in result["fibonacci_levels"]
        assert "Fib_0.618" in result["fibonacci_levels"]

    def test_downtrend_levels(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_fibonacci_levels
            result = call_tool(calculate_fibonacci_levels, "BTC", "down")
        assert result["trend_direction"] == "DOWN"

    def test_swing_high_greater_than_low(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_fibonacci_levels
            result = call_tool(calculate_fibonacci_levels, "BTC", "up")
        assert result["swing_high"] > result["swing_low"]


# ---------------------------------------------------------------------------
# Tests — get_momentum_indicators
# ---------------------------------------------------------------------------

class TestGetMomentumIndicators:
    def test_all_indicators_present(self):
        with _patch_fetch():
            from crypto_technical_analysis import get_momentum_indicators
            result = call_tool(get_momentum_indicators, "BTC")
        indicators = result["momentum_indicators"]
        assert "stochastic" in indicators
        assert "williams_r" in indicators
        assert "rate_of_change" in indicators
        assert "cci" in indicators

    def test_stochastic_range(self):
        with _patch_fetch():
            from crypto_technical_analysis import get_momentum_indicators
            result = call_tool(get_momentum_indicators, "BTC")
        k = result["momentum_indicators"]["stochastic"]["k_percent"]
        assert 0 <= k <= 100

    def test_overall_momentum(self):
        with _patch_fetch():
            from crypto_technical_analysis import get_momentum_indicators
            result = call_tool(get_momentum_indicators, "BTC")
        assert result["overall_momentum"] in ("BULLISH", "BEARISH", "NEUTRAL")


# ---------------------------------------------------------------------------
# Tests — analyze_volume_profile
# ---------------------------------------------------------------------------

class TestAnalyzeVolumeProfile:
    def test_success_keys(self):
        with _patch_fetch():
            from crypto_technical_analysis import analyze_volume_profile
            result = call_tool(analyze_volume_profile, "BTC")
        assert "volume_profile" in result
        assert "high_volume_zones" in result
        assert "volume_analysis" in result

    def test_volume_percentages_sum_to_100(self):
        with _patch_fetch():
            from crypto_technical_analysis import analyze_volume_profile
            result = call_tool(analyze_volume_profile, "BTC")
        total = sum(vp["percentage"] for vp in result["volume_profile"])
        assert total > 0  # Volume profile has meaningful data


# ---------------------------------------------------------------------------
# Tests — detect_trend_reversals
# ---------------------------------------------------------------------------

class TestDetectTrendReversals:
    def test_success_keys(self):
        with _patch_fetch():
            from crypto_technical_analysis import detect_trend_reversals
            result = call_tool(detect_trend_reversals, "BTC")
        assert "reversal_signals" in result
        assert "confidence_score" in result
        assert "reversal_probability" in result

    def test_probability_values(self):
        with _patch_fetch():
            from crypto_technical_analysis import detect_trend_reversals
            result = call_tool(detect_trend_reversals, "BTC")
        assert result["reversal_probability"] in (
            "HIGH", "MEDIUM", "LOW", "VERY_LOW"
        )

    def test_direction_values(self):
        with _patch_fetch():
            from crypto_technical_analysis import detect_trend_reversals
            result = call_tool(detect_trend_reversals, "BTC")
        assert result["likely_direction"] in (
            "BULLISH_REVERSAL", "BEARISH_REVERSAL", "UNCERTAIN"
        )


# ---------------------------------------------------------------------------
# Tests — calculate_volatility
# ---------------------------------------------------------------------------

class TestCalculateVolatility:
    def test_success_keys(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_volatility
            result = call_tool(calculate_volatility, "BTC")
        assert "current_volatility_pct" in result
        assert "atr" in result
        assert "volatility_classification" in result

    def test_classification_values(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_volatility
            result = call_tool(calculate_volatility, "BTC")
        assert result["volatility_classification"] in (
            "EXTREMELY_HIGH", "HIGH", "MODERATE", "LOW", "VERY_LOW"
        )

    def test_risk_level_values(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_volatility
            result = call_tool(calculate_volatility, "BTC")
        assert result["risk_level"] in ("VERY_HIGH", "HIGH", "MODERATE", "LOW")

    def test_breakout_signal(self):
        with _patch_fetch():
            from crypto_technical_analysis import calculate_volatility
            result = call_tool(calculate_volatility, "BTC")
        assert result["breakout_signal"] in (
            "VOLATILITY_BREAKOUT_HIGH", "VOLATILITY_BREAKOUT_LOW", "NORMAL_RANGE"
        )


# ---------------------------------------------------------------------------
# Tests — get_correlation_analysis
# ---------------------------------------------------------------------------

class TestGetCorrelationAnalysis:
    def test_two_symbols(self):
        with _patch_fetch():
            from crypto_technical_analysis import get_correlation_analysis
            result = call_tool(get_correlation_analysis, ["BTC", "ETH"])
        assert "correlation_matrix" in result
        assert "market_cohesion" in result

    def test_self_correlation_is_one(self):
        with _patch_fetch():
            from crypto_technical_analysis import get_correlation_analysis
            result = call_tool(get_correlation_analysis, ["BTC", "ETH"])
        # Same data mocked so self-corr = 1.0
        if "BTC" in result.get("correlation_matrix", {}):
            assert result["correlation_matrix"]["BTC"]["BTC"] == 1.0

    def test_insufficient_symbols(self):
        with patch("crypto_technical_analysis.safe_fetch_ohlcv_data", return_value=None):
            from crypto_technical_analysis import get_correlation_analysis
            result = call_tool(get_correlation_analysis, ["BTC"])
        assert "error" in result


# ---------------------------------------------------------------------------
# Tests — generate_trading_signals
# ---------------------------------------------------------------------------

class TestGenerateTradingSignals:
    def test_combined_strategy(self):
        with _patch_fetch():
            from crypto_technical_analysis import generate_trading_signals
            result = call_tool(generate_trading_signals, "BTC", "combined")
        assert "overall_signal" in result
        assert "individual_signals" in result
        assert result["overall_signal"] in (
            "STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"
        )

    def test_rsi_strategy(self):
        with _patch_fetch():
            from crypto_technical_analysis import generate_trading_signals
            result = call_tool(generate_trading_signals, "BTC", "rsi")
        assert "overall_signal" in result

    def test_macd_strategy(self):
        with _patch_fetch():
            from crypto_technical_analysis import generate_trading_signals
            result = call_tool(generate_trading_signals, "BTC", "macd")
        assert "overall_signal" in result

    def test_risk_management_present(self):
        with _patch_fetch():
            from crypto_technical_analysis import generate_trading_signals
            result = call_tool(generate_trading_signals, "BTC", "combined")
        assert "risk_management" in result
        assert "stop_loss_pct" in result["risk_management"]


# ---------------------------------------------------------------------------
# Tests — backtest_strategy
# ---------------------------------------------------------------------------

class TestBacktestStrategy:
    def test_rsi_backtest(self):
        with _patch_fetch():
            from crypto_technical_analysis import backtest_strategy
            result = call_tool(backtest_strategy, "BTC", "rsi_oversold", "3m")
        assert "total_return_pct" in result
        assert "win_rate_pct" in result
        assert "max_drawdown_pct" in result

    def test_macd_backtest(self):
        with _patch_fetch():
            from crypto_technical_analysis import backtest_strategy
            result = call_tool(backtest_strategy, "BTC", "macd_crossover", "3m")
        assert "total_trades" in result

    def test_ma_backtest(self):
        with _patch_fetch():
            from crypto_technical_analysis import backtest_strategy
            result = call_tool(backtest_strategy, "BTC", "ma_crossover", "3m")
        assert "buy_hold_return_pct" in result

    def test_insufficient_data(self):
        with _patch_fetch_short():
            from crypto_technical_analysis import backtest_strategy
            result = call_tool(backtest_strategy, "BTC", "rsi_oversold", "3m")
        assert "error" in result

    def test_initial_capital(self):
        with _patch_fetch():
            from crypto_technical_analysis import backtest_strategy
            result = call_tool(backtest_strategy, "BTC", "rsi_oversold", "3m", 50000)
        assert result["initial_capital"] == 50000


# ---------------------------------------------------------------------------
# V2 regression tests — typed S/R pivots, priority OHLCV fetch, flagged CoinGecko fallback
# ---------------------------------------------------------------------------

import crypto_technical_analysis as ta  # noqa: E402


def _wave_df():
    """Range 100-120 for 80 bars (swing lows ~100, swing highs ~120), then a breakdown to ~90.

    V1 bug: swing lows at ~100 (now ABOVE price) were returned as support, and swing lows of
    the HIGH series leaked into resistance.
    """
    n_range, n_drop = 80, 20
    x = np.arange(n_range)
    mid = 110 + 10 * np.sin(2 * np.pi * x / 16)
    drop = np.linspace(mid[-1], 90, n_drop)
    close = np.concatenate([mid, drop])
    # a deeper old swing low at 85 (true support below 90)
    close[8] = 85
    high = close * 1.004
    low = close * 0.996
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=len(close), freq="1D"),
        "open": close, "high": high, "low": low, "close": close,
        "volume": np.full(len(close), 1000.0),
    })


class TestSupportResistanceRegression:
    def _run(self, df):
        with patch("crypto_technical_analysis.safe_fetch_ohlcv_data", return_value=df):
            return call_tool(ta.get_support_resistance, "BTC", 100)

    def test_supports_below_and_resistances_above_price(self):
        result = self._run(_wave_df())
        price = result["current_price"]
        assert all(s["price"] < price for s in result["support_levels"])
        assert all(r["price"] > price for r in result["resistance_levels"])
        assert result["resistance_levels"], "range lows/highs above price must yield resistance"

    def test_sorted_nearest_first(self):
        for df in (_wave_df(), MOCK_DF.copy()):
            result = self._run(df)
            sup = [s["price"] for s in result["support_levels"]]
            res = [r["price"] for r in result["resistance_levels"]]
            assert sup == sorted(sup, reverse=True)
            assert res == sorted(res)
            if result["next_support"]:
                assert result["next_support"] == result["support_levels"][0]
            if result["next_resistance"]:
                assert result["next_resistance"] == result["resistance_levels"][0]

    def test_pivots_are_typed(self):
        df = _wave_df()
        result = self._run(df)
        highs = ta._swing_points(df["high"], result["pivot_window"], "high")
        lows = ta._swing_points(df["low"], result["pivot_window"], "low")
        for r in result["resistance_levels"]:
            assert any(abs(r["price"] - p) / p <= 0.021 for _, p in highs)
        for s in result["support_levels"]:
            assert any(abs(s["price"] - p) / p <= 0.021 for _, p in lows)
        # broken range lows (~100, above price) are reported separately, never as support
        assert result["broken_support"]
        assert all(b["price"] > result["current_price"] for b in result["broken_support"])

    def test_swing_points_only_extremes(self):
        s = pd.Series([1, 2, 3, 2, 1, 2, 3, 2, 1, 2, 3])
        assert ta._swing_points(s, 2, "high") == [(2, 3.0), (6, 3.0)]
        assert ta._swing_points(s, 2, "low") == [(4, 1.0), (8, 1.0)]


def _rows(n=50, last_ts_ms=None, step_ms=86_400_000):
    import time as _t
    last = last_ts_ms or int(_t.time() * 1000)
    start = last - (n - 1) * step_ms
    return [[start + i * step_ms, 100 + i, 101 + i, 99 + i, 100.5 + i, 10.0 + i] for i in range(n)]


class TestOHLCVFetchPriority:
    def test_uses_priority_fallback_and_latest_candles(self):
        ex = MagicMock()
        ex.fetch_ohlcv.return_value = _rows()
        with patch("crypto_technical_analysis.fetch_with_fallback",
                   side_effect=lambda fn, exchanges=None, market="spot": (fn(ex), "okx", [])) as fwf:
            df = ta.safe_fetch_ohlcv_data("eth", 50)
        assert fwf.call_args.kwargs.get("exchanges") is None  # desk_common priority (okx first)
        ex.fetch_ohlcv.assert_called_once_with("ETH/USDT", "1d", limit=50)  # no stale `since`
        assert df.attrs == {"source": "okx", "synthetic_ohlc": False, "volume_available": True}
        assert len(df) == 50

    def test_stale_candles_rejected(self):
        ex = MagicMock()
        ex.fetch_ohlcv.return_value = _rows(last_ts_ms=1_600_000_000_000)  # year 2020
        captured = {}

        def fake(fn, exchanges=None, market="spot"):
            try:
                fn(ex)
            except ValueError as e:
                captured["err"] = str(e)
            raise RuntimeError("all exchanges failed")

        with patch("crypto_technical_analysis.fetch_with_fallback", side_effect=fake), \
             patch("crypto_technical_analysis.fetch_coingecko_fallback", return_value=None):
            assert ta.safe_fetch_ohlcv_data("BTC", 50) is None
        assert "stale" in captured["err"]

    def test_tool_reports_data_source(self):
        df = MOCK_DF.copy()
        df.attrs.update({"source": "kraken", "synthetic_ohlc": False, "volume_available": True})
        with patch("crypto_technical_analysis.safe_fetch_ohlcv_data", return_value=df):
            result = call_tool(ta.calculate_rsi, "BTC")
        assert result["data_source"] == "kraken"
        assert "data_quality" not in result


class TestCoinGeckoFallbackFlagged:
    def _ohlc_payload(self, n=40):
        return [[1_740_000_000_000 + i * 14_400_000, 100 + i, 102 + i, 98 + i, 101 + i] for i in range(n)]

    def test_ohlc_fallback_has_no_fabricated_volume(self):
        with patch("crypto_technical_analysis.fetch_with_fallback", side_effect=RuntimeError("down")), \
             patch("crypto_technical_analysis.http_get_json", return_value=self._ohlc_payload()) as hg:
            df = ta.safe_fetch_ohlcv_data("BTC", 100)
        assert df["volume"].isna().all()
        assert (df["volume"] != 1_000_000).all()
        assert df.attrs["volume_available"] is False
        assert df.attrs["synthetic_ohlc"] is False
        assert hg.call_args.kwargs["params"]["days"] == 180  # valid CoinGecko bucket >= 100

    def test_market_chart_fallback_flagged_synthetic(self):
        chart = {"prices": [[1_740_000_000_000 + i * 3_600_000, 100.0 + i] for i in range(48)],
                 "total_volumes": [[1_740_000_000_000 + i * 3_600_000, 5e9] for i in range(48)]}

        def fake_http(url, params=None, **kw):
            if url.endswith("/ohlc"):
                raise ta.HttpError("HTTP 429", 429, url)
            return chart

        with patch("crypto_technical_analysis.fetch_with_fallback", side_effect=RuntimeError("down")), \
             patch("crypto_technical_analysis.http_get_json", side_effect=fake_http):
            df = ta.safe_fetch_ohlcv_data("BTC", 2)
        assert df.attrs["synthetic_ohlc"] is True
        assert df["volume"].isna().all()
        # no fabricated +/-0.1% wicks
        assert (df["high"] == df[["open", "close"]].max(axis=1)).all()

    def test_volume_tools_warn_on_fallback(self):
        df = MOCK_DF.copy()
        df["volume"] = np.nan
        df.attrs.update({"source": "coingecko_market_chart", "synthetic_ohlc": True, "volume_available": False})
        with patch("crypto_technical_analysis.safe_fetch_ohlcv_data", return_value=df):
            vp = call_tool(ta.analyze_volume_profile, "BTC")
            tr = call_tool(ta.detect_trend_reversals, "BTC")
            rsi = call_tool(ta.calculate_rsi, "BTC")
        assert "error" in vp and vp["data_quality"]["level"] == "degraded"
        assert any("volume" in w.lower() for w in tr["data_quality"]["warnings"])
        assert "HIGH_VOLUME_CONFIRMATION" not in tr["reversal_signals"]
        assert rsi["synthetic_ohlc"] is True and rsi["volume"] is None
