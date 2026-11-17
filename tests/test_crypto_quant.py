"""
Tests for crypto_quant.py (crypto-quant) MCP server.

Fully offline: the internal OHLCV helper `_fetch_ohlcv` is patched with deterministic
synthetic series designed to trigger each regime / condition. `_fetch_ohlcv` itself
(cache + backward paging) and `_default_universe` are tested against a mocked ccxt exchange.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["DESK_HTTP_CACHE"] = "0"

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers"))
from helpers import call_tool  # noqa: E402

import crypto_quant as cq  # noqa: E402
from desk_common import HttpError  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

def mk(close, start="2019-01-01", freq="D", spread=0.01, volume=1000.0, opens=None):
    close = np.asarray(close, dtype=float)
    o = np.r_[close[0], close[:-1]] if opens is None else np.asarray(opens, dtype=float)
    idx = pd.date_range(start, periods=len(close), freq=freq, tz="UTC")
    vol = np.full(len(close), volume) if np.isscalar(volume) else np.asarray(volume, dtype=float)
    return pd.DataFrame({
        "open": o,
        "high": np.maximum(o, close) * (1 + spread),
        "low": np.minimum(o, close) * (1 - spread),
        "close": close,
        "volume": vol,
    }, index=idx)


T = np.arange(420)
UP = 100 * np.exp(0.004 * T) * (1 + 0.01 * np.sin(T / 3))
DOWN = 100 * np.exp(-0.002 * T) * (1 + 0.01 * np.sin(T / 3))
RANGE = 100 * (1 + 0.05 * np.sin(2 * np.pi * T / 30))
EUPHORIA = np.r_[np.full(390, 100.0), 100 * np.exp(0.03 * np.arange(1, 31))]
CAPITULATION = np.r_[100 * np.exp(0.001 * np.arange(405)),
                     100 * np.exp(0.001 * 404) * np.exp(-0.035 * np.arange(1, 16))]


def fake_fetch(mapping):
    """mapping: symbol -> DataFrame or callable(timeframe) -> DataFrame."""
    calls = []

    def _f(symbol, timeframe="1d", limit=300):
        sym = symbol.upper()
        calls.append((sym, timeframe, limit))
        if sym not in mapping:
            raise RuntimeError("all exchanges failed: okx: BadSymbol")
        v = mapping[sym]
        df = v(timeframe) if callable(v) else v
        return df.iloc[-limit:], "okx"

    _f.calls = calls
    return _f


@pytest.fixture(autouse=True)
def _clear_cache():
    cq._clear_cache()
    yield
    cq._clear_cache()


# ---------------------------------------------------------------------------
# Internal helpers: OHLCV fetch / cache / paging, universe
# ---------------------------------------------------------------------------

def _rows(start_ms, n, step=86_400_000, px=100.0):
    return [[start_ms + i * step, px, px * 1.01, px * 0.99, px, 10.0] for i in range(n)]


class TestFetchOhlcv:
    def test_cache_hits_within_ttl(self):
        ex = MagicMock()
        ex.id = "okx"
        ex.fetch_ohlcv.return_value = _rows(1_600_000_000_000, 50)

        def fwf(fn, exchanges=None, market="spot"):
            return fn(ex), "okx", []

        with patch.object(cq, "fetch_with_fallback", side_effect=fwf) as m:
            df1, v1 = cq._fetch_ohlcv("btc", "1d", 50)
            df2, v2 = cq._fetch_ohlcv("BTC/USDT", "1d", 50)
        assert m.call_count == 1
        assert v1 == v2 == "okx"
        assert len(df1) == 50 and df1.equals(df2)
        assert list(df1.columns) == ["open", "high", "low", "close", "volume"]
        assert str(df1.index.tz) == "UTC"

    def test_backward_paging_uses_history_candles_on_okx(self):
        newest = _rows(1_600_000_000_000 + 300 * 86_400_000, 300)
        older = _rows(1_600_000_000_000, 300)
        ex = MagicMock()
        ex.id = "okx"
        ex.fetch_ohlcv.side_effect = [newest, older, []]
        rows = cq._paged_ohlcv(ex, "BTC/USDT", "1d", 600)
        assert len(rows) == 600
        assert rows[0][0] < rows[-1][0]
        second = ex.fetch_ohlcv.call_args_list[1]
        assert second.kwargs["params"]["type"] == "HistoryCandles"
        assert second.kwargs["params"]["until"] == newest[0][0] - 1

    def test_paging_stops_when_no_older_data(self):
        ex = MagicMock()
        ex.id = "kraken"
        latest = _rows(1_600_000_000_000, 720)
        ex.fetch_ohlcv.side_effect = [latest, latest]  # kraken ignores 'until'
        rows = cq._paged_ohlcv(ex, "BTC/USDT", "1d", 2000)
        assert len(rows) == 720
        assert ex.fetch_ohlcv.call_count == 2


class TestDefaultUniverse:
    def test_filters_stables_wrapped_leveraged_stocks(self):
        names = {
            "BTC/USDT": 1000, "USDC/USDT": 900, "ETH/USDT": 800, "XNVDA/USDT": 700, "BTC3L/USDT": 650,
            "WBTC/USDT": 600, "SOL/USDT": 500, "XAUT/USDT": 450, "RLUSD/USDT": 400, "JUP/USDT": 300,
            "ETH/EUR": 5000, "PYUSD/USDT": 250, "ETHUP/USDT": 240, "CBBTC/USDT": 230, "DOGE/USDT": 200,
        }
        ex = MagicMock()
        ex.fetch_tickers.return_value = {s: {"quoteVolume": v} for s, v in names.items()}
        ex.markets = {"XNVDA/USDT": {"info": {"instCategory": "3"}}, "ETH/USDT": {"info": {"instCategory": "1"}}}

        def fwf(fn, exchanges=None, market="spot"):
            return fn(ex), "okx", []

        with patch.object(cq, "fetch_with_fallback", side_effect=fwf):
            uni, venue = cq._default_universe(30)
        assert uni == ["BTC", "ETH", "SOL", "JUP", "DOGE"]
        assert venue == "okx"

    def test_btc_always_included(self):
        ex = MagicMock()
        ex.fetch_tickers.return_value = {"ETH/USDT": {"quoteVolume": 10}, "SOL/USDT": {"quoteVolume": 5},
                                         "BTC/USDT": {"quoteVolume": 1}}
        ex.markets = {}
        with patch.object(cq, "fetch_with_fallback", side_effect=lambda fn, **k: (fn(ex), "okx", [])):
            uni, _ = cq._default_universe(2)
        assert uni[0] == "BTC" and len(uni) == 2


# ---------------------------------------------------------------------------
# classify_regime
# ---------------------------------------------------------------------------

class TestClassifyRegime:
    @pytest.mark.parametrize("series,label", [
        (UP, "strong_uptrend"),
        (DOWN, "strong_downtrend"),
        (RANGE, "range"),
        (EUPHORIA, "euphoria"),
        (CAPITULATION, "capitulation"),
    ])
    def test_labels(self, series, label):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(series)})):
            r = call_tool(cq.classify_regime, symbol="BTC")
        assert r["status"] == "success", r
        assert r["regime"] == label
        assert r["rule"]
        assert "interpretation" in r

    def test_uptrend_details_and_mtf_alignment(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(UP)})):
            r = call_tool(cq.classify_regime, symbol="btc/usdt")
        assert r["symbol"] == "BTC"
        t = r["trend"]
        assert t["score"] == 5 and t["max_score"] == 5
        assert t["structure"] == "HH+HL"
        assert t["adx"] > 25 and t["plus_di"] > t["minus_di"]
        assert t["ma50"]["dist_pct"] > 0 and t["ma200"]["slope5_pct"] > 0
        assert r["multi_timeframe"]["agreement"] == "aligned_bullish"
        assert set(r["multi_timeframe"]) == {"4h", "1d", "1w", "agreement"}
        assert r["volatility"]["vol_regime"] in {"low", "normal", "high", "extreme"}
        assert r["drawdown"]["from_90d_high_pct"] <= 0

    def test_capitulation_rule_mentions_thresholds(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(CAPITULATION)})):
            r = call_tool(cq.classify_regime, symbol="BTC")
        assert "RSI" in r["rule"] and "DD90" in r["rule"]
        assert r["momentum"]["rsi14"] <= 30
        assert r["drawdown"]["from_90d_high_pct"] <= -20

    def test_mixed_mtf(self):
        frames = {"1d": mk(UP), "4h": mk(DOWN, freq="4h"), "1w": mk(UP, freq="W")}
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": lambda tf: frames[tf]})):
            r = call_tool(cq.classify_regime, symbol="BTC")
        assert r["multi_timeframe"]["4h"] == "strong_downtrend"
        assert r["multi_timeframe"]["agreement"] == "mixed"

    def test_bad_timeframe(self):
        r = call_tool(cq.classify_regime, symbol="BTC", timeframe="15m")
        assert r["status"] == "error"

    def test_short_history(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(UP[:30])})):
            r = call_tool(cq.classify_regime, symbol="BTC")
        assert r["status"] == "error" and "history" in r["error"]

    def test_fetch_error(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({})):
            r = call_tool(cq.classify_regime, symbol="BTC")
        assert r["status"] == "error" and "all exchanges failed" in r["error"]


# ---------------------------------------------------------------------------
# market_breadth
# ---------------------------------------------------------------------------

class TestMarketBreadth:
    def test_breadth_counts(self):
        data = {"BTC": mk(UP), "ETH": mk(UP * 0.5), "SOL": mk(UP * 2), "DOGE": mk(DOWN)}
        with patch.object(cq, "_fetch_ohlcv", fake_fetch(data)):
            r = call_tool(cq.market_breadth, symbols=["BTC", "ETH", "SOL", "DOGE"])
        assert r["status"] == "success"
        assert r["universe"] == "explicit" and r["n_symbols"] == 4
        assert r["pct_above_ma"]["ma50"] == 75.0
        assert r["pct_above_ma"]["ma200"] == 75.0
        assert r["pct_rsi_below_30"] == 25.0
        assert r["median_return_pct"]["30d"] > 0
        assert 0 <= r["breadth_score"] <= 100
        assert r["breadth_thrust"] is False

    def test_breadth_thrust_detected(self):
        base = 100 * np.exp(-0.003 * np.arange(300))
        thrust = np.r_[base[:294], base[293] * np.exp(0.04 * np.arange(1, 7))]
        data = {s: mk(thrust * (i + 1)) for i, s in enumerate(["BTC", "ETH", "SOL", "XRP", "ADA"])}
        with patch.object(cq, "_fetch_ohlcv", fake_fetch(data)):
            r = call_tool(cq.market_breadth, symbols="BTC,ETH,SOL,XRP,ADA")
        assert r["breadth_thrust"] is True
        assert "THRUST" in r["interpretation"]
        assert r["pct_above_ma"]["ma20"] == 100.0

    def test_washout(self):
        data = {s: mk(CAPITULATION * (i + 1)) for i, s in enumerate(["BTC", "ETH", "SOL"])}
        with patch.object(cq, "_fetch_ohlcv", fake_fetch(data)):
            r = call_tool(cq.market_breadth, symbols=["BTC", "ETH", "SOL"])
        assert r["washout"] is True
        assert r["breadth_score"] < 30

    def test_default_universe_and_skipped(self):
        with patch.object(cq, "_default_universe", return_value=(["BTC", "ETH", "NOPE"], "okx")), \
                patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(UP), "ETH": mk(UP)})):
            r = call_tool(cq.market_breadth)
        assert r["universe"].startswith("top30")
        assert r["skipped"] == ["NOPE"] and r["n_symbols"] == 2

    def test_all_fail(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({})):
            r = call_tool(cq.market_breadth, symbols=["BTC"])
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# relative_strength_scan
# ---------------------------------------------------------------------------

def _rs_universe():
    t = np.arange(300)
    btc = 100 * (1 + 0.02 * np.sin(t / 5))
    leader = btc * np.exp(0.004 * t)
    laggard = btc * np.exp(-0.004 * t)
    # strong uptrend then a sharp 10-day dump: oversold but still above MA200
    quality = np.r_[100 * np.exp(0.004 * t[:290]), 100 * np.exp(0.004 * 289) * np.exp(-0.015 * np.arange(1, 11))]
    pump = btc.copy()
    pump[-5:] = pump[-5:] * 1.8
    vol = np.full(300, 1000.0)
    vol[-7:] = 20000.0
    return {
        "BTC": mk(btc, volume=1e6), "LEAD": mk(leader, volume=1e6), "LAG": mk(laggard, volume=1e6),
        "QUAL": mk(quality, volume=1e6), "PUMP": mk(pump, volume=vol),
    }


class TestRelativeStrengthScan:
    def test_buckets(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch(_rs_universe())):
            r = call_tool(cq.relative_strength_scan, symbols=["LEAD", "LAG", "QUAL", "PUMP"], top_n=2)
        assert r["status"] == "success"
        assert r["vs"] == "BTC" and r["n_symbols"] == 5  # BTC auto-included
        assert [x["sym"] for x in r["leaders"]] == ["PUMP", "LEAD"]
        assert r["leaders"][0]["flags"]
        assert r["laggards"][0]["sym"] == "LAG"
        assert not ({x["sym"] for x in r["leaders"]} & {x["sym"] for x in r["laggards"]})
        assert r["oversold_quality"][0]["sym"] == "QUAL"
        lead = next(x for x in r["table"] if x["sym"] == "LEAD")
        assert lead["rs30"] == pytest.approx((np.exp(0.004 * 30) - 1) * 100, abs=0.2)
        assert lead["rs_slope"] == pytest.approx(0.4, abs=0.02)
        btc = next(x for x in r["table"] if x["sym"] == "BTC")
        assert btc["rs30"] == 0.0
        assert "legend" in r and "interpretation" in r

    def test_pump_flags(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch(_rs_universe())):
            r = call_tool(cq.relative_strength_scan, symbols=["PUMP", "LEAD"], top_n=5)
        pump = next(x for x in r["table"] if x["sym"] == "PUMP")
        assert "volume_spike" in pump["flags"]
        assert "thin" in pump["flags"]  # ~$0.1M/day average
        lead = next(x for x in r["table"] if x["sym"] == "LEAD")
        assert "flags" not in lead

    def test_vs_reference_missing(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"ETH": mk(UP)})):
            r = call_tool(cq.relative_strength_scan, symbols=["ETH"])
        assert r["status"] == "error" and "BTC" in r["error"]


# ---------------------------------------------------------------------------
# sector_rotation
# ---------------------------------------------------------------------------

CATEGORIES = [
    {"id": "layer-1", "name": "Layer 1 (L1)", "market_cap": 2.0e12, "market_cap_change_24h": 1.5,
     "volume_24h": 8.0e10, "top_3_coins_id": ["bitcoin", "ethereum", "solana"]},
    {"id": "meme", "name": "Meme", "market_cap": 5.0e10, "market_cap_change_24h": -4.2,
     "volume_24h": 5.0e9, "top_3_coins_id": ["dogecoin", "shiba-inu", "pepe"]},
    {"id": "ai", "name": "Artificial Intelligence (AI)", "market_cap": 3.0e10, "market_cap_change_24h": 3.1,
     "volume_24h": 2.0e9, "top_3_coins_id": ["bittensor"]},
    {"id": "stables", "name": "Stablecoins", "market_cap": 3.0e11, "market_cap_change_24h": 0.01,
     "volume_24h": 9.0e10, "top_3_coins_id": ["tether"]},
    {"id": "a16z", "name": "Andreessen Horowitz (a16z) Portfolio", "market_cap": 3.0e11,
     "market_cap_change_24h": 9.0, "volume_24h": 1e9, "top_3_coins_id": []},
    {"id": "pm", "name": "Prediction Markets", "market_cap": 4.4e9, "market_cap_change_24h": 71.5,
     "volume_24h": 2e7, "top_3_coins_id": ["rain"]},
    {"id": "tiny", "name": "Tiny", "market_cap": 1e6, "market_cap_change_24h": 50.0, "volume_24h": 1},
    {"id": "nulls", "name": "Nulls", "market_cap": None, "market_cap_change_24h": None},
]


def _sector_data():
    t = np.arange(300)
    out = {}
    for i, s in enumerate(sorted({x for v in cq.SECTOR_MAP.values() for x in v})):
        out[s] = mk(100 * np.exp(0.001 * t))
    for s in cq.SECTOR_MAP["Meme"]:
        out[s] = mk(100 * np.exp(0.01 * t))  # memes rip
    for s in cq.SECTOR_MAP["Gaming"]:
        out[s] = mk(100 * np.exp(-0.01 * t))  # gaming bleeds
    return out


class TestSectorRotation:
    def test_coingecko_and_okx(self):
        with patch.object(cq, "http_get_json", return_value=CATEGORIES) as hg, \
                patch.object(cq, "_okx_bases", return_value=None), \
                patch.object(cq, "_fetch_ohlcv", fake_fetch(_sector_data())):
            r = call_tool(cq.sector_rotation)
        assert r["status"] == "success"
        assert r["path"] == "coingecko+okx_static_map"
        names = [x["name"] for x in r["top_24h"]]
        assert names[0] == "Artificial Intelligence (AI)"
        assert "Stablecoins" not in names and not any("Portfolio" in n for n in names)
        assert "Tiny" not in names and "Nulls" not in names
        assert r["excluded_anomalies"][0]["name"] == "Prediction Markets"
        assert r["bottom_24h"][0]["name"] == "Meme"
        assert r["okx_momentum"][0]["sector"] == "Meme"
        assert r["okx_momentum"][-1]["sector"] == "Gaming"
        assert hg.call_args.kwargs["ttl"] == 900

    def test_demo_key_header(self, monkeypatch):
        monkeypatch.setenv("DESK_COINGECKO_KEY", "demo-123")
        with patch.object(cq, "http_get_json", return_value=CATEGORIES) as hg, \
                patch.object(cq, "_okx_bases", return_value=set()), \
                patch.object(cq, "_fetch_ohlcv", fake_fetch({})):
            r = call_tool(cq.sector_rotation)
        assert hg.call_args.kwargs["headers"]["x-cg-demo-api-key"] == "demo-123"
        assert r["path"] == "coingecko_only" and "okx_error" in r

    def test_fallback_to_static_map(self):
        with patch.object(cq, "http_get_json", side_effect=HttpError("HTTP 429", 429, "u")), \
                patch.object(cq, "_okx_bases", return_value={"DOGE", "SHIB", "AXS", "ETH"}), \
                patch.object(cq, "_fetch_ohlcv", fake_fetch(_sector_data())) as ff:
            r = call_tool(cq.sector_rotation)
        assert r["status"] == "success"
        assert r["path"] == "okx_static_map_fallback"
        assert "429" in r["coingecko_error"]
        assert {x["sector"] for x in r["okx_momentum"]} == {"Meme", "Gaming", "L1"}
        # only OKX-listed members were fetched
        assert {c[0] for c in ff.calls} == {"DOGE", "SHIB", "AXS", "ETH"}

    def test_both_fail(self):
        with patch.object(cq, "http_get_json", side_effect=HttpError("HTTP 500", 500, "u")), \
                patch.object(cq, "_okx_bases", return_value=None), \
                patch.object(cq, "_fetch_ohlcv", fake_fetch({})):
            r = call_tool(cq.sector_rotation)
        assert r["status"] == "error"
        assert "coingecko" in r["error"] and "okx" in r["error"]


# ---------------------------------------------------------------------------
# behavior_base_rates
# ---------------------------------------------------------------------------

def _crash_series(n=1200, every=100, crash_days=6):
    """Flat-ish drift with a sharp crash every `every` days followed by a recovery."""
    r = np.full(n, 0.001)
    starts = list(range(150, n - 60, every))
    for s in starts:
        r[s:s + crash_days] = -0.05
        r[s + crash_days:s + crash_days + 20] = 0.02
    return 100 * np.exp(np.cumsum(r)), starts


class TestBehaviorBaseRates:
    def test_rsi_below_events_and_forward_stats(self):
        close, starts = _crash_series()
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(close, start="2018-01-01")})):
            r = call_tool(cq.behavior_base_rates, symbol="BTC", condition="rsi_below_25", horizons=[7, 30])
        assert r["status"] == "success", r
        assert r["n_events"] == len(starts)
        h7, h30 = r["horizons"]["7d"], r["horizons"]["30d"]
        assert h7["n"] == h30["n"] == len(starts)
        # event fires on day 1 of the crash: 7d is still falling, 30d captures the recovery
        assert h7["mean"] < 0 and h7["hit_rate"] == 0.0
        assert h30["mean"] > 0 and h30["hit_rate"] == 100.0
        assert h30["mae_median"] < -20
        assert "baseline_mean" in h30 and "edge_vs_baseline" in h30
        assert r["history"]["bars"] == 1200
        assert len(r["recent_events"]) == 5
        assert "first day true" in r["definition"]

    def test_small_sample_caveat_and_history_note(self):
        close, _ = _crash_series(n=400)
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(close, start="2018-01-01")})):
            r = call_tool(cq.behavior_base_rates, symbol="BTC", condition="rsi_below_25")
        assert "SMALL SAMPLE" in r["caveat"]
        assert "history_note" in r

    def test_drawdown_condition(self):
        close, starts = _crash_series()
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(close, start="2018-01-01")})):
            r = call_tool(cq.behavior_base_rates, symbol="BTC", condition="drawdown_from_high_20")
        assert r["status"] == "success"
        assert 1 <= r["n_events"] <= len(starts)

    def test_consecutive_red_days(self):
        n = 400
        close = np.full(n, 100.0)
        opens = np.full(n, 100.0)
        for s in (100, 200, 300):  # three 4-day red streaks
            opens[s:s + 4] = 101.0
        with patch.object(cq, "_fetch_ohlcv",
                          fake_fetch({"BTC": mk(close, opens=opens, start="2018-01-01")})):
            r = call_tool(cq.behavior_base_rates, symbol="BTC", condition="consecutive_red_days_3", horizons=[7])
        assert r["n_events"] == 3
        assert [e["date"] for e in r["recent_events"]] == [
            pd.Timestamp("2018-01-01").date().__add__(pd.Timedelta(days=d)).isoformat() for d in (102, 202, 302)]

    def test_ma200_cross(self):
        t = np.arange(700)
        close = 100 * (1 + 0.3 * np.sin(2 * np.pi * t / 250))
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(close, start="2018-01-01")})):
            below = call_tool(cq.behavior_base_rates, symbol="BTC", condition="below_ma200")
            above = call_tool(cq.behavior_base_rates, symbol="BTC", condition="above_ma200")
        assert below["n_events"] >= 1 and above["n_events"] >= 1
        assert "cross" in below["definition"]

    def test_vol_spike_by_direction(self):
        rng = np.random.default_rng(3)
        r = rng.normal(0, 0.01, 800)
        r[[200, 400]] = 0.12
        r[[300, 600]] = -0.12
        close = 100 * np.exp(np.cumsum(r))
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(close, start="2018-01-01")})):
            res = call_tool(cq.behavior_base_rates, symbol="BTC", condition="vol_spike_5", horizons=[7])
        assert res["n_events"] == 4
        assert res["by_direction"]["up_spikes"]["7d"]["n"] == 2
        assert res["by_direction"]["down_spikes"]["7d"]["n"] == 2

    def test_weekend(self):
        idx = pd.date_range("2018-01-01", periods=700, freq="D", tz="UTC")
        r = np.where(idx.dayofweek >= 5, 0.01, -0.002)
        close = 100 * np.exp(np.cumsum(r))
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(close, start="2018-01-01")})):
            res = call_tool(cq.behavior_base_rates, symbol="BTC", condition="weekend")
        assert res["status"] == "success"
        assert res["weekend"]["mean"] > 0 > res["weekday"]["mean"]
        assert res["weekend"]["hit_rate"] == 100.0

    def test_unsupported_condition(self):
        r = call_tool(cq.behavior_base_rates, symbol="BTC", condition="moon_phase_full")
        assert r["status"] == "error" and "Supported" in r["error"]

    def test_dedupe_min_gap(self):
        cond = pd.Series([False, True, False, True, False, False, False, False, False, True, False])
        assert cq._events(cond, min_gap=7) == [1, 9]

    def test_fetch_error(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({})):
            r = call_tool(cq.behavior_base_rates, symbol="BTC")
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# seasonality
# ---------------------------------------------------------------------------

class TestSeasonality:
    def test_dow_hour_month(self):
        rng = np.random.default_rng(11)
        didx = pd.date_range("2018-01-01", periods=1500, freq="D", tz="UTC")
        dr = rng.normal(0, 0.01, len(didx)) + np.where(didx.dayofweek == 0, 0.01, 0.0)
        hidx = pd.date_range("2021-01-01", periods=180 * 24, freq="h", tz="UTC")
        hr = rng.normal(0, 0.002, len(hidx)) + np.where(hidx.hour == 14, 0.004, 0.0)
        frames = {"1d": mk(100 * np.exp(np.cumsum(dr)), start="2018-01-01"),
                  "1h": mk(100 * np.exp(np.cumsum(hr)), start="2021-01-01", freq="h")}
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": lambda tf: frames[tf]})):
            r = call_tool(cq.seasonality, symbol="BTC")
        assert r["status"] == "success"
        dow = {d["k"]: d for d in r["day_of_week"]}
        assert max(dow, key=lambda k: dow[k]["mean"]) == "Mon"
        assert dow["Mon"]["t"] >= 2
        hours = {h["k"]: h for h in r["hour_of_day_utc"]}
        assert len(hours) == 24 and max(hours, key=lambda k: hours[k]["mean"]) == 14
        assert len(r["month_of_year"]) == 12
        assert any(n.startswith("Mon") for n in r["notable"])
        assert any(n.startswith("14h") for n in r["notable"])
        assert not any(n.startswith(("Jan", "Feb", "Mar")) for n in r["notable"])
        assert "caveat" in r

    def test_hourly_unavailable_is_soft(self):
        frames = {"1d": mk(UP, start="2018-01-01")}

        def by_tf(tf):
            if tf not in frames:
                raise RuntimeError("no hourly")
            return frames[tf]
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": by_tf})):
            r = call_tool(cq.seasonality, symbol="BTC")
        assert r["status"] == "success"
        assert r["hour_of_day_utc"] == [] and "unavailable" in r["history"]["hourly"]


# ---------------------------------------------------------------------------
# correlation_matrix & beta_to_btc
# ---------------------------------------------------------------------------

def _corr_universe(n_extra=0):
    rng = np.random.default_rng(5)
    rb = rng.normal(0, 0.02, 300)
    data = {
        "BTC": mk(100 * np.cumprod(1 + rb)),
        "TWOX": mk(100 * np.cumprod(1 + 2 * rb)),
        "INDEP": mk(100 * np.cumprod(1 + rng.normal(0, 0.02, 300))),
    }
    for i in range(n_extra):
        data[f"X{i}"] = mk(100 * np.cumprod(1 + rb + rng.normal(0, 0.002, 300)))
    return data


class TestCorrelation:
    def test_small_universe_full_matrix(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch(_corr_universe())):
            r = call_tool(cq.correlation_matrix, symbols=["TWOX", "INDEP"], days=60)
        assert r["status"] == "success"
        assert r["n_symbols"] == 3  # BTC auto-included
        tbl = {x["sym"]: x for x in r["btc_corr_beta"]}
        assert tbl["TWOX"]["beta"] == pytest.approx(2.0, abs=0.01)
        assert tbl["TWOX"]["corr"] == pytest.approx(1.0, abs=0.01)
        assert abs(tbl["INDEP"]["corr"]) < 0.4
        assert r["matrix"]["BTC"]["TWOX"] == pytest.approx(1.0, abs=0.01)
        assert ["BTC", "TWOX"] in [sorted(c) for c in r["clusters_corr_gt_0.8"]]

    def test_large_universe_compact(self):
        data = _corr_universe(n_extra=12)
        with patch.object(cq, "_fetch_ohlcv", fake_fetch(data)):
            r = call_tool(cq.correlation_matrix, symbols=list(data), days=60)
        assert r["n_symbols"] == 15
        assert "matrix" not in r
        assert len(r["btc_corr_beta"]) == 14
        assert r["clusters_corr_gt_0.8"][0][0] in data and len(r["clusters_corr_gt_0.8"][0]) >= 13

    def test_missing_btc(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"ETH": mk(UP)})):
            r = call_tool(cq.correlation_matrix, symbols=["ETH"])
        assert r["status"] == "error"

    def test_beta_to_btc(self):
        rng = np.random.default_rng(9)
        rb = rng.normal(0, 0.02, 300)
        # 1.5x on BTC up-days, 2.5x on down-days → downside beta > upside beta
        ra = np.where(rb > 0, 1.5 * rb, 2.5 * rb)
        data = {"BTC": mk(100 * np.cumprod(1 + rb)), "ALT": mk(100 * np.cumprod(1 + ra))}
        with patch.object(cq, "_fetch_ohlcv", fake_fetch(data)):
            r = call_tool(cq.beta_to_btc, symbol="ALT", days=120)
        assert r["status"] == "success"
        assert r["upside_beta"] == pytest.approx(1.5, abs=0.05)
        assert r["downside_beta"] == pytest.approx(2.5, abs=0.05)
        assert "harder" in r["interpretation"]

    def test_beta_btc_itself(self):
        r = call_tool(cq.beta_to_btc, symbol="BTC")
        assert r["beta"] == 1.0

    def test_beta_fetch_error(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(UP)})):
            r = call_tool(cq.beta_to_btc, symbol="NOPE")
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# volatility_regime
# ---------------------------------------------------------------------------

class TestVolatilityRegime:
    def test_atr_stops_exact(self):
        n = 420
        idx = pd.date_range("2019-01-01", periods=n, freq="D", tz="UTC")
        df = pd.DataFrame({"open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 1.0}, index=idx)
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": df})):
            r = call_tool(cq.volatility_regime, symbol="BTC")
        assert r["status"] == "success"
        assert r["atr_pct"] == pytest.approx(4.0)
        s = r["stop_distances"]
        assert s["1x_atr"]["pct"] == pytest.approx(4.0)
        assert s["2x_atr"]["pct"] == pytest.approx(8.0)
        assert s["1x_atr"]["long_stop"] == pytest.approx(96.0)
        assert s["1.5x_atr"]["short_stop"] == pytest.approx(106.0)
        assert r["median_daily_range_pct_30d"] == pytest.approx(4.0)
        assert "inside 1x ATR noise" not in r["interpretation"]  # ATR exactly 4% → "roughly 1x ATR"

    def test_vol_expansion_and_percentile(self):
        rng = np.random.default_rng(1)
        r = np.r_[rng.normal(0, 0.01, 410), rng.normal(0, 0.08, 10)]
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(100 * np.exp(np.cumsum(r)))})):
            res = call_tool(cq.volatility_regime, symbol="BTC")
        assert res["realized_vol_ann_pct"]["7d"] > res["realized_vol_ann_pct"]["90d"]
        assert res["vol_term_structure"].startswith("expanding")
        assert res["rv30_percentile_1y"] >= 90 and res["vol_regime"] == "extreme"
        assert res["vol_of_vol"]["std_of_rv7_90d"] > 0
        assert "inside 1x ATR noise" in res["interpretation"]

    def test_error(self):
        with patch.object(cq, "_fetch_ohlcv", fake_fetch({})):
            r = call_tool(cq.volatility_regime, symbol="BTC")
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# Serialization safety
# ---------------------------------------------------------------------------

def test_outputs_are_json_serializable():
    import json
    with patch.object(cq, "_fetch_ohlcv", fake_fetch({"BTC": mk(UP), "ETH": mk(DOWN)})):
        for res in (call_tool(cq.classify_regime, symbol="BTC"),
                    call_tool(cq.market_breadth, symbols=["BTC", "ETH"]),
                    call_tool(cq.relative_strength_scan, symbols=["ETH"]),
                    call_tool(cq.correlation_matrix, symbols=["ETH"]),
                    call_tool(cq.volatility_regime, symbol="ETH")):
            assert res["status"] == "success", res
            json.loads(cq.tool_serializer(res))
