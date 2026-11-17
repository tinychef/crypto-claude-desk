"""Tests for crypto_derivatives.py (crypto-derivatives). Fully mocked — no network."""
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ["DESK_HTTP_CACHE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers"))

import crypto_derivatives as mod  # noqa: E402
from desk_common import HttpError  # noqa: E402
from helpers import call_tool  # noqa: E402


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    for name in ("DESK_COINALYZE_KEY", "COINALYZE_API_KEY", "COINALYZE_KEY"):
        monkeypatch.delenv(name, raising=False)
    mod._post_cache.clear()


@pytest.fixture
def coinalyze_key(monkeypatch):
    monkeypatch.setenv("DESK_COINALYZE_KEY", "test-key")


def _router(routes):
    """http_get_json side_effect: first route whose fragment is in the URL wins."""
    calls = []

    def fake(url, params=None, headers=None, ttl=60, **kw):
        calls.append((url, params, headers))
        for frag, resp in routes.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp(params) if callable(resp) else resp
        raise AssertionError(f"unexpected URL {url}")

    fake.calls = calls
    return fake


# ---------------------------------------------------------------------------
# Coinalyze fixtures
# ---------------------------------------------------------------------------

FUTURE_MARKETS = [
    {"symbol": "BTCUSDT_PERP.A", "exchange": "A", "base_asset": "BTC", "quote_asset": "USDT", "is_perpetual": True},
    {"symbol": "BTCUSDT_PERP.6", "exchange": "6", "base_asset": "BTC", "quote_asset": "USDT", "is_perpetual": True},
    {"symbol": "BTCUSD_PERP.3", "exchange": "3", "base_asset": "BTC", "quote_asset": "USD", "is_perpetual": True},
    {"symbol": "BTCUSDT_261225.A", "exchange": "A", "base_asset": "BTC", "quote_asset": "USDT", "is_perpetual": False},
    {"symbol": "ETHUSDT_PERP.A", "exchange": "A", "base_asset": "ETH", "quote_asset": "USDT", "is_perpetual": True},
]


def _oi_history(params):
    t0 = 1_800_000_000
    out = []
    for sym, (start, end) in {"BTCUSDT_PERP.A": (10e9, 11e9), "BTCUSDT_PERP.6": (5e9, 5.5e9),
                              "BTCUSD_PERP.3": (3e9, 3.3e9)}.items():
        assert sym in params["symbols"]
        out.append({"symbol": sym, "history": [
            {"t": t0, "o": start, "h": start, "l": start, "c": start},
            {"t": t0 + 3600, "o": end, "h": end, "l": end, "c": end}]})
    return out


FUNDING = [{"symbol": "BTCUSDT_PERP.A", "value": 0.01, "update": 1}, {"symbol": "BTCUSDT_PERP.6", "value": 0.02},
           {"symbol": "BTCUSD_PERP.3", "value": -0.01}]


class TestAggregatedOI:
    def test_coinalyze_success(self, coinalyze_key):
        fake = _router({"/future-markets": FUTURE_MARKETS, "/open-interest-history": _oi_history,
                        "/funding-rate": FUNDING})
        with patch("crypto_derivatives.http_get_json", side_effect=fake):
            r = call_tool(mod.get_aggregated_oi, "btc/usdt", interval="1hour", hours=24)
        assert r["status"] == "success" and r["aggregated"] is True
        assert r["total_oi_usd"] == pytest.approx(19.8e9)
        assert r["oi_change_pct_24h"] == pytest.approx(10.0)
        assert r["top_venues"][0]["exchange"] == "Binance"
        assert r["markets_counted"] == 3
        # OI-weighted funding: (0.01*11 + 0.02*5.5 - 0.01*3.3) / 19.8
        assert r["funding_rate_8h_pct"] == pytest.approx((0.11 + 0.11 - 0.033) / 19.8, abs=1e-4)
        # key passed as header, never as a query param
        hdrs = [c[2] for c in fake.calls]
        assert all(h == {"api_key": "test-key"} for h in hdrs)
        assert "leverage building" in r["interpretation"]

    def test_fallback_okx_without_key(self):
        ex = MagicMock()
        ex.fetch_open_interest.side_effect = [{"openInterestValue": 2e9}, {"openInterestValue": 7e8}]
        ex.fetch_funding_rate.return_value = {"fundingRate": 0.0001}
        rubik = {"code": "0", "data": [[str(1_800_000_000_000 - i * 3600_000), str(3e9 - i * 1e7), "1"]
                                       for i in range(60)]}
        with patch("crypto_derivatives.get_exchange", return_value=ex), \
                patch("crypto_derivatives.http_get_json", return_value=rubik):
            r = call_tool(mod.get_aggregated_oi, "BTC", hours=48)
        assert r["status"] == "success" and r["aggregated"] is False
        assert r["total_oi_usd"] == pytest.approx(2.7e9)
        assert r["funding_rate_8h_pct"] == pytest.approx(0.01)
        assert r["oi_change_pct_48h"] == pytest.approx((3e9 - 2.52e9) / 2.52e9 * 100, abs=0.01)

    def test_http_error(self, coinalyze_key):
        with patch("crypto_derivatives.http_get_json", side_effect=HttpError("HTTP 401", 401, "x")):
            r = call_tool(mod.get_aggregated_oi, "BTC")
        assert r["status"] == "error" and "401" in r["error"]

    def test_bad_interval(self):
        r = call_tool(mod.get_aggregated_oi, "BTC", interval="3hour")
        assert r["status"] == "error"


class TestLiquidations:
    def test_missing_key(self):
        r = call_tool(mod.get_liquidations_history, "BTC")
        assert r["status"] == "unavailable" and r["provider"] == "coinalyze"

    def test_success_with_cascade(self, coinalyze_key):
        def liq(params):
            t0 = 1_800_000_000
            hist = [{"t": t0 + i * 3600, "l": 1e6, "s": 1e6} for i in range(10)]
            hist.append({"t": t0 + 10 * 3600, "l": 60e6, "s": 2e6})  # cascade
            return [{"symbol": "BTCUSDT_PERP.A", "history": hist}]

        fake = _router({"/future-markets": FUTURE_MARKETS, "/liquidation-history": liq})
        with patch("crypto_derivatives.http_get_json", side_effect=fake):
            r = call_tool(mod.get_liquidations_history, "BTC", hours=24)
        assert r["status"] == "success"
        assert r["long_liquidations_usd"] == pytest.approx(70e6)
        assert r["short_liquidations_usd"] == pytest.approx(12e6)
        assert len(r["cascades"]) == 1 and r["cascades"][0]["side"] == "longs"
        assert "long flush" in r["interpretation"].lower()

    def test_http_error(self, coinalyze_key):
        with patch("crypto_derivatives.http_get_json", side_effect=HttpError("HTTP 429", 429, "x")):
            r = call_tool(mod.get_liquidations_history, "BTC")
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# Deribit fixtures
# ---------------------------------------------------------------------------

def _code(d):
    return f"{d.day}{d.strftime('%b%y').upper()}"


def _expiries():
    now = datetime.now(timezone.utc)
    d = now + timedelta(days=3)
    while d.weekday() != 4:
        d += timedelta(days=1)
    weekly = d
    m = now + timedelta(days=10)
    while not (m.weekday() == 4 and (m + timedelta(days=7)).month != m.month):
        m += timedelta(days=1)
    return weekly, m


def _opt(name, oi, iv, und=100000.0, vol=10.0):
    return {"instrument_name": name, "open_interest": oi, "volume": vol, "mark_iv": iv,
            "underlying_price": und, "estimated_delivery_price": und}


def _option_book():
    weekly, monthly = _expiries()
    book = []
    for exp, und in ((weekly, 100000.0), (monthly, 100500.0)):
        c = _code(exp)
        for k in range(80000, 125000, 5000):
            put_iv = 50 + max(0, (100000 - k)) / 2000   # puts richer below spot
            call_iv = 50 + max(0, (k - 100000)) / 5000
            book.append(_opt(f"BTC-{c}-{k}-C", 100 if k == 110000 else 10, call_iv, und))
            book.append(_opt(f"BTC-{c}-{k}-P", 80 if k == 90000 else 10, put_iv, und, vol=12))
    book.append({"instrument_name": "BTC-1JAN20-50000-C", "open_interest": 999})  # expired, ignored
    return book


DVOL = {"result": {"data": [[int(time.time() * 1000) - (48 - i) * 3600_000, 50, 52, 49, 50 + i * 0.1]
                            for i in range(49)], "continuation": None}}


class TestOptionsSnapshot:
    def test_success(self):
        fake = _router({"get_book_summary_by_currency": {"result": _option_book()},
                        "get_volatility_index_data": DVOL})
        with patch("crypto_derivatives.http_get_json", side_effect=fake):
            r = call_tool(mod.get_options_snapshot, "BTC")
        assert r["status"] == "success"
        assert r["open_interest"]["calls"] == pytest.approx(2 * (8 * 10 + 100))
        assert r["open_interest"]["puts"] == pytest.approx(2 * (8 * 10 + 80))
        assert r["open_interest"]["put_call_oi_ratio"] == pytest.approx(160 / 180, abs=1e-3)
        assert r["dvol"]["value"] == pytest.approx(54.8)
        assert r["dvol"]["change_24h"] == pytest.approx(2.4, abs=0.01)
        skew = r["skew_25d"]
        assert skew["method"] == "black_scholes_delta_from_mark_iv"
        assert skew["skew_25d"] > 0 and skew["put_strike"] < 100500 < skew["call_strike"]
        assert skew["expiry"] in {_code(e) for e in _expiries()}  # nearest monthly with >= 7 days
        assert len(r["term_structure"]) == 2 and r["term_structure"][0]["atm_iv"] == pytest.approx(50)
        assert "interpretation" in r

    def test_dvol_failure_is_partial(self):
        fake = _router({"get_book_summary_by_currency": {"result": _option_book()},
                        "get_volatility_index_data": HttpError("HTTP 500", 500, "x")})
        with patch("crypto_derivatives.http_get_json", side_effect=fake):
            r = call_tool(mod.get_options_snapshot, "BTC")
        assert r["status"] == "success" and r["dvol"] is None and r["data_gaps"]

    def test_http_error(self):
        with patch("crypto_derivatives.http_get_json", side_effect=HttpError("network error", None, "x")):
            r = call_tool(mod.get_options_snapshot, "ETH")
        assert r["status"] == "error" and r["source"] == "deribit"

    def test_bs_delta_sanity(self):
        assert mod._bs_delta(100, 100, 50, 0.1, True) == pytest.approx(0.53, abs=0.02)
        assert mod._bs_delta(100, 100, 50, 0.1, False) == pytest.approx(-0.47, abs=0.02)


class TestMaxPain:
    def test_known_answer(self):
        weekly, _ = _expiries()
        c = _code(weekly)
        book = [_opt(f"BTC-{c}-90000-C", 5, 50), _opt(f"BTC-{c}-100000-C", 20, 50), _opt(f"BTC-{c}-110000-C", 100, 50),
                _opt(f"BTC-{c}-90000-P", 100, 50), _opt(f"BTC-{c}-100000-P", 20, 50), _opt(f"BTC-{c}-110000-P", 5, 50)]
        with patch("crypto_derivatives.http_get_json", return_value={"result": book}):
            r = call_tool(mod.get_max_pain, "BTC")
        assert r["status"] == "success"
        assert r["max_pain_strike"] == 100000
        assert r["expiry"] == c
        assert r["notional_oi_usd"] == pytest.approx(250 * 100000)
        assert r["top_call_strikes"][0]["strike"] == 110000
        assert r["distance_from_index_pct"] == pytest.approx(0.0)

    def test_explicit_expiry_and_missing(self):
        weekly, monthly = _expiries()
        with patch("crypto_derivatives.http_get_json", return_value={"result": _option_book()}):
            ok = call_tool(mod.get_max_pain, "BTC", expiry=monthly.strftime("%Y-%m-%d"))
            bad = call_tool(mod.get_max_pain, "BTC", expiry="1JAN99")
        assert ok["status"] == "success" and ok["expiry"] == _code(monthly)
        assert bad["status"] == "error" and len(bad["available_expiries"]) == 2

    def test_http_error(self):
        with patch("crypto_derivatives.http_get_json", side_effect=HttpError("HTTP 503", 503, "x")):
            r = call_tool(mod.get_max_pain, "BTC")
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# Basis term structure
# ---------------------------------------------------------------------------

def _okx_exchange(index=100000.0):
    now_ms = time.time() * 1000
    d30, d90 = now_ms + 30 * 86400_000, now_ms + 90 * 86400_000
    markets = {
        "BTC/USD:BTC-A": {"symbol": "BTC/USD:BTC-A", "future": True, "base": "BTC", "settle": "BTC", "expiry": d30},
        "BTC/USD:BTC-B": {"symbol": "BTC/USD:BTC-B", "future": True, "base": "BTC", "settle": "BTC", "expiry": d90},
        "BTC/USD:BTC-OLD": {"symbol": "BTC/USD:BTC-OLD", "future": True, "base": "BTC", "settle": "BTC",
                            "expiry": now_ms - 1000},
        "BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "future": False, "swap": True, "base": "BTC"},
    }
    ex = MagicMock()
    ex.load_markets.return_value = markets
    ex.publicGetMarketIndexTickers.return_value = {"data": [{"idxPx": str(index)}]}
    ex.fetch_tickers.return_value = {
        "BTC/USD:BTC-A": {"bid": 100800.0, "ask": 100840.0, "last": 100820.0},   # ~0.82% in 30d ≈ 10%/yr
        "BTC/USD:BTC-B": {"bid": None, "ask": None, "last": 103000.0},         # 3% in 90d ≈ 12%/yr
    }
    return ex


class TestBasis:
    def test_okx_only_with_deribit_gap(self):
        with patch("crypto_derivatives.get_exchange", return_value=_okx_exchange()), \
                patch("crypto_derivatives.http_get_json", side_effect=HttpError("network error", None, "x")):
            r = call_tool(mod.get_basis_term_structure, "BTC")
        assert r["status"] == "success" and r["source"] == "okx"
        assert len(r["curve"]) == 2
        assert r["curve"][0]["annualized_pct"] == pytest.approx(0.82 * 365 / 30, rel=0.02)
        assert r["regime"] == "healthy contango"
        assert any("deribit" in g for g in r["data_gaps"])
        assert all("OLD" not in c["contract"] for c in r["curve"])

    def test_both_venues(self):
        weekly, monthly = _expiries()
        book = {"result": [
            {"instrument_name": "BTC-PERPETUAL", "mark_price": 100100, "estimated_delivery_price": 100000},
            {"instrument_name": f"BTC-{_code(monthly)}", "mark_price": 98000, "estimated_delivery_price": 100000}]}
        ex = _okx_exchange()
        ex.fetch_tickers.return_value = {"BTC/USD:BTC-A": {"bid": 99000.0, "ask": 99000.0},
                                         "BTC/USD:BTC-B": {"last": 97000.0}}
        with patch("crypto_derivatives.get_exchange", return_value=ex), \
                patch("crypto_derivatives.http_get_json", return_value=book):
            r = call_tool(mod.get_basis_term_structure, "BTC")
        assert r["status"] == "success" and r["source"] == "deribit,okx"
        assert len(r["curve"]) == 3
        assert r["regime"] == "backwardation"

    def test_all_venues_fail(self):
        with patch("crypto_derivatives.get_exchange", side_effect=RuntimeError("down")), \
                patch("crypto_derivatives.http_get_json", side_effect=HttpError("HTTP 500", 500, "x")):
            r = call_tool(mod.get_basis_term_structure, "BTC")
        assert r["status"] == "error" and len(r["data_gaps"]) == 2


# ---------------------------------------------------------------------------
# Hyperliquid
# ---------------------------------------------------------------------------

HL = [
    {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                  {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
                  {"name": "DOGE", "szDecimals": 0, "maxLeverage": 10},
                  {"name": "OLD", "szDecimals": 0, "maxLeverage": 3, "isDelisted": True}]},
    [{"funding": "0.0000125", "openInterest": "20000", "prevDayPx": "98000", "dayNtlVlm": "2500000000",
      "premium": "0.0002", "oraclePx": "99980", "markPx": "100000", "midPx": "100000", "impactPxs": ["1", "2"]},
     {"funding": "0.00005", "openInterest": "500000", "prevDayPx": "3000", "dayNtlVlm": "900000000",
      "premium": "-0.0001", "oraclePx": "3001", "markPx": "3000"},
     {"funding": "-0.00002", "openInterest": "1000000", "prevDayPx": "0.2", "dayNtlVlm": "1000",
      "premium": None, "oraclePx": "0.2", "markPx": "0.2"},
     {"funding": "0.1", "openInterest": "1e12", "prevDayPx": "1", "dayNtlVlm": "0", "oraclePx": "1", "markPx": "1"}],
]


class TestHyperliquid:
    def test_single_coin(self):
        with patch("crypto_derivatives._http_post_json", return_value=HL) as post:
            r = call_tool(mod.get_hyperliquid_market, "btc")
        assert post.call_args.args[1] == {"type": "metaAndAssetCtxs"}
        assert r["status"] == "success" and r["coin"] == "BTC"
        assert r["funding_annualized_pct"] == pytest.approx(0.0000125 * 24 * 365 * 100, abs=0.01)
        assert r["open_interest_usd"] == pytest.approx(2e9)
        assert r["change_24h_pct"] == pytest.approx(2.04, abs=0.01)
        assert r["premium_pct"] == pytest.approx(0.02)

    def test_all_top_by_oi_excludes_delisted(self):
        with patch("crypto_derivatives._http_post_json", return_value=HL):
            r = call_tool(mod.get_hyperliquid_market, "ALL")
        coins = [m["coin"] for m in r["top_by_oi"]]
        assert coins == ["BTC", "ETH", "DOGE"]
        assert "ETH" in r["interpretation"]  # 0.005%/h ≈ 43.8%/yr → crowded longs

    def test_unknown_coin_and_error(self):
        with patch("crypto_derivatives._http_post_json", return_value=HL):
            assert call_tool(mod.get_hyperliquid_market, "XYZ")["status"] == "error"
        with patch("crypto_derivatives._http_post_json", side_effect=HttpError("HTTP 429", 429, "x")):
            r = call_tool(mod.get_hyperliquid_market, "BTC")
        assert r["status"] == "error" and r["source"] == "hyperliquid"

    def test_post_helper_raises_http_error(self):
        resp = MagicMock(status_code=500)
        session = MagicMock()
        session.post.return_value = resp
        with patch.object(mod, "_post_session", session):
            with pytest.raises(HttpError):
                mod._http_post_json("https://api.hyperliquid.xyz/info", {"type": "x"})


# ---------------------------------------------------------------------------
# OKX Rubik positioning
# ---------------------------------------------------------------------------

def _rows(n, fn):
    return [[str(1_800_000_000_000 - i * 3600_000)] + fn(i) for i in range(n)]  # newest first


class TestOkxPositioning:
    def test_success(self):
        fake = _router({
            "long-short-account-ratio": {"code": "0", "data": _rows(30, lambda i: [str(2.5 - i * 0.01)])},
            "taker-volume": {"code": "0", "data": _rows(30, lambda i: ["100", "130"])},  # [sell, buy]
            "open-interest-volume": {"code": "0", "data": _rows(30, lambda i: [str(3e9 - i * 1e7), "2e8"])},
        })
        with patch("crypto_derivatives.http_get_json", side_effect=fake):
            r = call_tool(mod.get_okx_positioning, "BTC-USDT", period="1H")
        assert r["status"] == "success"
        assert r["long_short_account_ratio"]["latest"] == 2.5
        assert r["long_short_account_ratio"]["change"] == pytest.approx(0.23)
        assert r["taker_volume"]["latest_buy_sell_ratio"] == pytest.approx(1.3)
        assert r["open_interest"]["change_pct_24p"] == pytest.approx((3e9 - 2.77e9) / 2.77e9 * 100, abs=0.01)
        assert "heavily long" in r["interpretation"] and "taker buying" in r["interpretation"]
        assert fake.calls[1][1] == {"ccy": "BTC", "instType": "CONTRACTS", "period": "1H"}

    def test_partial_failure(self):
        fake = _router({"long-short-account-ratio": {"code": "51000", "msg": "bad", "data": []},
                        "taker-volume": HttpError("HTTP 500", 500, "x"),
                        "open-interest-volume": {"code": "0", "data": _rows(5, lambda i: ["1e9", "1"])}})
        with patch("crypto_derivatives.http_get_json", side_effect=fake):
            r = call_tool(mod.get_okx_positioning, "ETH")
        assert r["status"] == "success" and len(r["data_gaps"]) == 2 and "open_interest" in r

    def test_all_fail_and_bad_period(self):
        with patch("crypto_derivatives.http_get_json", side_effect=HttpError("HTTP 500", 500, "x")):
            assert call_tool(mod.get_okx_positioning, "BTC")["status"] == "error"
        assert call_tool(mod.get_okx_positioning, "BTC", period="4H")["status"] == "error"


def test_server_name_and_tools():
    assert mod.mcp.name == "crypto-derivatives"
