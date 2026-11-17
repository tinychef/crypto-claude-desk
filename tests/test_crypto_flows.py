"""Tests for crypto_flows.py (crypto-flows). Fully mocked — no network."""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ["DESK_HTTP_CACHE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers"))

import crypto_flows as cf  # noqa: E402
from desk_common import HttpError  # noqa: E402
from helpers import call_tool  # noqa: E402


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for k in ("DESK_SOSOVALUE_KEY", "SOSOVALUE_API_KEY", "SOSOVALUE_KEY"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FARSIDE_HTML = """
<html><body>
<table class="etf"><tr><td>nav</td></tr></table>
<table class="etf">
 <thead>
  <tr><th></th><th>Blackrock</th><th>Fidelity</th><th>Grayscale</th><th></th></tr>
  <tr><th>Date</th><th>IBIT</th><th>FBTC</th><th>GBTC</th><th>Total</th></tr>
  <tr><th>Fee</th><th>0.25%</th><th>0.25%</th><th>1.50%</th><th></th></tr>
 </thead>
 <tbody>
  <tr><td>10 Nov 2026</td><td>100.0</td><td>(20.0)</td><td>-</td><td>80.0</td></tr>
  <tr><td>11 Nov 2026</td><td>(50.5)</td><td>(10.0)</td><td>(5.0)</td><td>(65.5)</td></tr>
  <tr><td>12 Nov 2026</td><td>1,200.0</td><td>30.0</td><td>(30.0)</td><td>1,200.0</td></tr>
  <tr><td>13 Nov 2026</td><td>200.0</td><td>-</td><td>(10.0)</td><td>190.0</td></tr>
  <tr><td>14 Nov 2026</td><td>50.0</td><td>25.0</td><td>-</td><td>75.0</td></tr>
  <tr><td>17 Nov 2026</td><td>40.0</td><td></td><td></td><td></td></tr>
  <tr><td>Total</td><td>1,539.5</td><td>25.0</td><td>(45.0)</td><td>1,479.5</td></tr>
  <tr><td>Average</td><td>1.0</td><td>1.0</td><td>1.0</td><td>1.0</td></tr>
 </tbody>
</table></body></html>
"""


def _kraken(usdt_usd=1.0):
    ex = MagicMock()
    ex.fetch_ticker.return_value = {"last": usdt_usd}
    return ex


# ---------------------------------------------------------------------------
# ETF flows
# ---------------------------------------------------------------------------

class TestFarsideParser:
    def test_parses_rows_and_negatives(self):
        rows = cf.parse_farside_html(FARSIDE_HTML)
        assert [r["date"] for r in rows] == ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-14"]
        assert rows[1]["total"] == -65.5
        assert rows[1]["funds"]["IBIT"] == -50.5
        assert rows[0]["funds"]["GBTC"] == 0.0
        assert rows[2]["funds"]["IBIT"] == 1200.0  # thousands separator

    def test_incomplete_latest_day_skipped(self):
        rows = cf.parse_farside_html(FARSIDE_HTML)
        assert "2026-11-17" not in [r["date"] for r in rows]

    def test_empty_html(self):
        assert cf.parse_farside_html("<html></html>") == []

    def test_parse_flow_values(self):
        assert cf._parse_flow("(1,234.5)") == -1234.5
        assert cf._parse_flow("-") == 0.0
        assert cf._parse_flow("") is None
        assert cf._parse_flow("n/a") is None


class TestGetEtfFlows:
    def test_success_farside(self):
        with patch("crypto_flows.http_get", return_value=FARSIDE_HTML):
            r = call_tool(cf.get_etf_flows, "BTC", 10)
        assert r["status"] == "success"
        assert r["source"] == "farside"
        assert r["latest"]["date"] == "2026-11-14"
        assert r["latest"]["net_flow_usd_m"] == 75.0
        assert r["latest"]["by_fund_usd_m"] == {"IBIT": 50.0, "FBTC": 25.0}
        assert r["sum_5d_usd_m"] == pytest.approx(80 - 65.5 + 1200 + 190 + 75, abs=0.1)
        assert r["streak"] == {"direction": "inflow", "days": 3}
        assert "IBIT" in r["window_top_inflows_usd_m"]
        assert "GBTC" in r["window_top_outflows_usd_m"]
        assert "strong institutional inflows" in r["interpretation"]

    def test_days_window(self):
        with patch("crypto_flows.http_get", return_value=FARSIDE_HTML):
            r = call_tool(cf.get_etf_flows, "BTC", 2)
        assert len(r["daily_net_usd_m"]) == 2

    def test_sosovalue_fallback(self):
        soso = {"code": 0, "data": [
            {"date": "2026-11-13", "totalNetInflow": -100_000_000},
            {"date": "2026-11-14", "totalNetInflow": -50_000_000},
        ]}
        with patch("crypto_flows.http_get", side_effect=HttpError("blocked", None, "u")), \
                patch("crypto_flows._post_json", return_value=soso) as post:
            r = call_tool(cf.get_etf_flows, "ETH", 10)
        assert r["status"] == "success"
        assert r["source"] == "sosovalue"
        assert r["latest"]["net_flow_usd_m"] == -50.0
        assert r["streak"] == {"direction": "outflow", "days": 2}
        assert post.call_args[0][1] == {"type": "us-eth-spot"}
        assert "x-soso-api-key" not in post.call_args[0][2]

    def test_sosovalue_key_header(self, monkeypatch):
        monkeypatch.setenv("DESK_SOSOVALUE_KEY", "k123")
        with patch("crypto_flows.http_get", side_effect=HttpError("blocked", None, "u")), \
                patch("crypto_flows._post_json", return_value={"code": 0, "data": [{"date": "2026-11-14", "totalNetInflow": 1e6}]}) as post:
            call_tool(cf.get_etf_flows, "BTC")
        assert post.call_args[0][2]["x-soso-api-key"] == "k123"

    def test_unavailable_when_both_fail(self):
        with patch("crypto_flows.http_get", side_effect=HttpError("HTTP 403", 403, "u")), \
                patch("crypto_flows._post_json", side_effect=HttpError("HTTP 401", 401, "u")):
            r = call_tool(cf.get_etf_flows, "BTC")
        assert r["status"] == "unavailable"
        assert "farside" in r["reason"] and "sosovalue" in r["reason"]

    def test_unsupported_asset(self):
        r = call_tool(cf.get_etf_flows, "DOGE")
        assert r["status"] == "error"

    def test_streak_helper(self):
        assert cf._streak([1, -2, -3]) == {"direction": "outflow", "days": 2}
        assert cf._streak([1, 0]) == {"direction": "flat", "days": 0}


# ---------------------------------------------------------------------------
# Coinbase premium
# ---------------------------------------------------------------------------

class TestCoinbasePremium:
    def test_success(self):
        with patch("crypto_flows.http_get_json", return_value={"price": "100100.0"}), \
                patch("crypto_flows.fetch_with_fallback", return_value=(100000.0, "okx", [])), \
                patch("crypto_flows.get_exchange", return_value=_kraken(1.0)):
            r = call_tool(cf.get_coinbase_premium, "BTC")
        assert r["status"] == "success"
        assert r["premium_pct"] == pytest.approx(0.1, abs=1e-4)
        assert r["premium_usd"] == 100.0
        assert r["global_venue"] == "okx"
        assert "slight US bid" in r["interpretation"] or "US spot demand" in r["interpretation"]

    def test_usdt_adjustment(self):
        # USDT at 0.999 -> global USD = 99,900 -> premium ~ +0.2%
        with patch("crypto_flows.http_get_json", return_value={"price": "100100.0"}), \
                patch("crypto_flows.fetch_with_fallback", return_value=(100000.0, "okx", [])), \
                patch("crypto_flows.get_exchange", return_value=_kraken(0.999)):
            r = call_tool(cf.get_coinbase_premium, "btc/usdt")
        assert r["symbol"] == "BTC"
        assert r["premium_pct"] == pytest.approx(0.2002, abs=1e-3)
        assert "bullish" in r["interpretation"]

    def test_ccxt_fallback(self):
        cb = MagicMock()
        cb.fetch_ticker.return_value = {"last": 99_800.0}

        def ex(name, market="spot"):
            return cb if name == "coinbase" else _kraken(1.0)

        with patch("crypto_flows.http_get_json", side_effect=HttpError("blocked", None, "u")), \
                patch("crypto_flows.fetch_with_fallback", return_value=(100000.0, "okx", [])), \
                patch("crypto_flows.get_exchange", side_effect=ex):
            r = call_tool(cf.get_coinbase_premium, "BTC")
        assert r["status"] == "success"
        assert r["source"].startswith("ccxt-coinbase")
        assert r["premium_pct"] == pytest.approx(-0.2, abs=1e-4)

    def test_unavailable(self):
        with patch("crypto_flows.http_get_json", side_effect=HttpError("blocked", None, "u")), \
                patch("crypto_flows.get_exchange", side_effect=RuntimeError("no net")):
            r = call_tool(cf.get_coinbase_premium, "BTC")
        assert r["status"] == "unavailable"
        assert r["provider"] == "coinbase"

    def test_global_price_failure_is_error(self):
        with patch("crypto_flows.http_get_json", return_value={"price": "1"}), \
                patch("crypto_flows.fetch_with_fallback", side_effect=RuntimeError("all exchanges failed")):
            r = call_tool(cf.get_coinbase_premium, "BTC")
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# Kimchi premium
# ---------------------------------------------------------------------------

UPBIT = [{"market": "KRW-BTC", "trade_price": 145_600_000.0}, {"market": "KRW-USDT", "trade_price": 1420.0}]


def _router(fx_fail=0):
    calls = {"fx": 0}

    def fake(url, params=None, headers=None, **kw):
        if "upbit" in url:
            return UPBIT
        calls["fx"] += 1
        if calls["fx"] <= fx_fail:
            raise HttpError("fx down", 503, url)
        return {"rates": {"KRW": 1400.0}}
    return fake


class TestKimchiPremium:
    def test_success(self):
        with patch("crypto_flows.http_get_json", side_effect=_router()), \
                patch("crypto_flows.fetch_with_fallback", return_value=(100000.0, "okx", [])), \
                patch("crypto_flows.get_exchange", return_value=_kraken(1.0)):
            r = call_tool(cf.get_kimchi_premium, "BTC")
        assert r["status"] == "success"
        # 145.6M / 1400 = 104,000 USD -> +4%
        assert r["upbit_usd"] == 104000.0
        assert r["kimchi_premium_pct"] == pytest.approx(4.0, abs=1e-3)
        assert r["usdt_krw_premium_pct"] == pytest.approx(1.4286, abs=1e-3)
        assert r["fx_source"] == "frankfurter.dev"
        assert "froth" in r["interpretation"]

    def test_fx_fallback(self):
        with patch("crypto_flows.http_get_json", side_effect=_router(fx_fail=2)), \
                patch("crypto_flows.fetch_with_fallback", return_value=(100000.0, "okx", [])), \
                patch("crypto_flows.get_exchange", return_value=_kraken(1.0)):
            r = call_tool(cf.get_kimchi_premium, "BTC")
        assert r["status"] == "success"
        assert r["fx_source"] == "open.er-api.com"

    def test_fx_all_fail_is_error(self):
        with patch("crypto_flows.http_get_json", side_effect=_router(fx_fail=99)), \
                patch("crypto_flows.fetch_with_fallback", return_value=(100000.0, "okx", [])):
            r = call_tool(cf.get_kimchi_premium, "BTC")
        assert r["status"] == "error"
        assert "USD/KRW" in r["error"]

    def test_upbit_unreachable(self):
        with patch("crypto_flows.http_get_json", side_effect=HttpError("blocked", None, "u")):
            r = call_tool(cf.get_kimchi_premium, "BTC")
        assert r["status"] == "unavailable"
        assert r["provider"] == "upbit"

    def test_missing_market(self):
        with patch("crypto_flows.http_get_json", return_value=[{"market": "KRW-USDT", "trade_price": 1400.0}]):
            r = call_tool(cf.get_kimchi_premium, "FOO")
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# Stablecoins
# ---------------------------------------------------------------------------

def _chart(n=40, start=200e9, step=1e9):
    return [{"date": str(1_700_000_000 + i * 86400), "totalCirculatingUSD": {"peggedUSD": start + i * step}}
            for i in range(n)]


LISTING = {"peggedAssets": [
    {"symbol": "USDT", "pegType": "peggedUSD",
     "circulating": {"peggedUSD": 150e9}, "circulatingPrevWeek": {"peggedUSD": 149e9},
     "circulatingPrevMonth": {"peggedUSD": 145e9},
     "chainCirculating": {
         "Tron": {"current": {"peggedUSD": 70e9}, "circulatingPrevWeek": {"peggedUSD": 69e9}, "circulatingPrevMonth": {"peggedUSD": 66e9}},
         "Ethereum": {"current": {"peggedUSD": 80e9}, "circulatingPrevWeek": {"peggedUSD": 80.5e9}, "circulatingPrevMonth": {"peggedUSD": 79e9}},
     }},
    {"symbol": "USDC", "pegType": "peggedUSD",
     "circulating": {"peggedUSD": 60e9}, "circulatingPrevWeek": {"peggedUSD": 61e9},
     "circulatingPrevMonth": {"peggedUSD": 62e9},
     "chainCirculating": {
         "Ethereum": {"current": {"peggedUSD": 40e9}, "circulatingPrevWeek": {"peggedUSD": 41e9}, "circulatingPrevMonth": {"peggedUSD": 41e9}},
         "Base": {"current": {"peggedUSD": 20e9}, "circulatingPrevWeek": {"peggedUSD": 19e9}, "circulatingPrevMonth": {"peggedUSD": 18e9}},
     }},
    {"symbol": "EURC", "pegType": "peggedEUR", "circulating": {"peggedEUR": 1e8}, "chainCirculating": {}},
]}


class TestStablecoinFlows:
    def _run(self, days=30, chart=None):
        def fake(url, params=None, **kw):
            return chart if "stablecoincharts" in url else LISTING
        with patch("crypto_flows.http_get_json", side_effect=fake):
            return call_tool(cf.get_stablecoin_flows, days)

    def test_success(self):
        r = self._run(chart=_chart())
        assert r["status"] == "success"
        assert r["total_supply_b"] == 239.0
        assert r["change_7d_b"] == 7.0
        assert r["change_30d_b"] == 30.0
        assert r["change_30d_pct"] == pytest.approx(30 / 209 * 100, abs=0.01)
        assert r["issuers"]["USDT"]["net_mint_7d_m"] == 1000.0
        assert r["issuers"]["USDT"]["net_mint_30d_m"] == 5000.0
        assert r["issuers"]["USDC"]["net_mint_30d_m"] == -2000.0
        gainers = [g["chain"] for g in r["top_chain_inflows_7d"]]
        losers = [g["chain"] for g in r["top_chain_outflows_7d"]]
        assert gainers == ["Tron", "Base"]
        assert losers == ["Ethereum"]
        assert r["top_chain_outflows_7d"][0]["change_7d_m"] == -1500.0
        assert "expanding" in r["interpretation"]

    def test_custom_days_and_short_history(self):
        r = self._run(days=90, chart=_chart(n=40))
        assert r["change_90d_b"] is None
        assert r["change_30d_b"] is not None

    def test_contracting_read(self):
        r = self._run(chart=_chart(start=250e9, step=-0.5e9))
        assert "contracting" in r["interpretation"]

    def test_http_error(self):
        with patch("crypto_flows.http_get_json", side_effect=HttpError("HTTP 500", 500, "u")):
            r = call_tool(cf.get_stablecoin_flows)
        assert r["status"] == "error"
        assert r["source"] == "defillama"

    def test_empty_chart(self):
        r = self._run(chart=[])
        assert r["status"] == "error"
