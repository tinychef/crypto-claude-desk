"""Tests for crypto_onchain.py (crypto-onchain). Fully mocked — no network."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ["DESK_HTTP_CACHE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers"))

import crypto_onchain as oc  # noqa: E402
from desk_common import HttpError  # noqa: E402
from helpers import call_tool  # noqa: E402


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for p in ("CRYPTOQUANT", "GLASSNODE"):
        for k in (f"DESK_{p}_KEY", f"{p}_API_KEY", f"{p}_KEY"):
            monkeypatch.delenv(k, raising=False)


def _cm_rows(n=400, with_nvt=True):
    rows = []
    for i in range(n):
        r = {"asset": "btc", "time": f"day-{i:05d}",  # only needs to sort chronologically
             "CapMVRVCur": str(1.0 + i * 0.005),          # 1.0 .. 2.995, last is the max
             "CapMrktCurUSD": str(1.2e12),
             "AdrActCnt": str(800_000 if i < n - 7 else 880_000),
             "TxCnt": str(400_000)}
        if with_nvt:
            r["NVTAdj"] = str(50 + (i % 10))
        rows.append(r)
    return rows


# ---------------------------------------------------------------------------
# BTC valuation
# ---------------------------------------------------------------------------

class TestBtcValuation:
    def test_success(self):
        with patch("crypto_onchain.http_get_json", return_value={"data": _cm_rows()}) as m:
            r = call_tool(oc.get_btc_valuation)
        assert r["status"] == "success"
        assert r["mvrv"] == pytest.approx(2.995, abs=1e-3)
        assert r["mvrv_percentile"] == 100.0
        assert r["mvrv_30d_ago"] == pytest.approx(2.845, abs=1e-3)
        assert r["market_cap_b"] == 1200.0
        assert r["realized_cap_b"] == pytest.approx(1200 / 2.995, abs=0.1)
        assert r["nupl_proxy"] == pytest.approx(1 - 1 / 2.995, abs=1e-3)
        assert r["zone"].startswith("elevated")
        assert r["nvt_adj"] is not None and r["nvt_adj_90d_median"] is not None
        assert r["active_addresses"]["chg_7d_vs_prev30d_pct"] == pytest.approx(10.0, abs=0.1)
        assert "MVRV 2.99" in r["interpretation"] or "MVRV 3.00" in r["interpretation"]
        params = m.call_args.kwargs["params"]
        assert "NVTAdj" in params["metrics"] and params["frequency"] == "1d"

    def test_metric_fallback_without_nvt(self):
        calls = []

        def fake(url, params=None, **kw):
            calls.append(params["metrics"])
            if "NVTAdj" in params["metrics"]:
                raise HttpError("HTTP 403 from cm", 403, url)
            return {"data": _cm_rows(with_nvt=False)}

        with patch("crypto_onchain.http_get_json", side_effect=fake):
            r = call_tool(oc.get_btc_valuation)
        assert r["status"] == "success"
        assert len(calls) == 2
        assert r["nvt_adj"] is None
        assert "NVTAdj" in r["note"]

    def test_zones(self):
        assert oc._mvrv_zone(0.8).startswith("deep value")
        assert oc._mvrv_zone(1.2).startswith("undervalued")
        assert oc._mvrv_zone(2.0).startswith("neutral")
        assert oc._mvrv_zone(4.0).startswith("overheated")

    def test_http_error(self):
        with patch("crypto_onchain.http_get_json", side_effect=HttpError("HTTP 500", 500, "u")):
            r = call_tool(oc.get_btc_valuation)
        assert r["status"] == "error"

    def test_network_blocked_is_unavailable(self):
        with patch("crypto_onchain.http_get_json", side_effect=HttpError("network error", None, "u")):
            r = call_tool(oc.get_btc_valuation)
        assert r["status"] == "unavailable"
        assert "coinmetrics" in r["hint"]

    def test_no_mvrv(self):
        with patch("crypto_onchain.http_get_json", return_value={"data": []}):
            r = call_tool(oc.get_btc_valuation)
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# Network health
# ---------------------------------------------------------------------------

HASHRATES = {"hashrates": [{"timestamp": 1_700_000_000 + i * 86400, "avgHashrate": (600 + i) * 1e18} for i in range(90)],
             "currentHashrate": 700e18, "currentDifficulty": 95e12}


def _mempool(fail=()):
    def fake(url, params=None, **kw):
        for f in fail:
            if f in url:
                raise HttpError("blocked", None, url)
        if "fees/recommended" in url:
            return {"fastestFee": 12, "halfHourFee": 8, "hourFee": 5, "economyFee": 3, "minimumFee": 1}
        if url.endswith("/mempool"):
            return {"count": 45_000, "vsize": 30_000_000, "total_fee": 25_000_000}
        if "difficulty-adjustment" in url:
            return {"progressPercent": 55.5, "difficultyChange": 2.31, "previousRetarget": -1.2,
                    "remainingBlocks": 900, "estimatedRetargetDate": 1_800_000_000_000}
        if "hashrate/3m" in url:
            return HASHRATES
        if "blockchain.info" in url:
            return {"values": [{"x": i, "y": (600 + i) * 1e6} for i in range(90)]}
        raise AssertionError(url)
    return fake


class TestNetworkHealth:
    def test_success(self):
        with patch("crypto_onchain.http_get_json", side_effect=_mempool()):
            r = call_tool(oc.get_network_health)
        assert r["status"] == "success"
        assert r["fees_sat_vb"]["fastestFee"] == 12
        assert r["mempool"] == {"tx_count": 45_000, "vsize_mvb": 30.0, "total_fee_btc": 0.25}
        assert r["difficulty_adjustment"]["estimated_change_pct"] == 2.31
        assert r["difficulty_adjustment"]["eta"].startswith("2027-01-15")
        assert r["hashrate"]["current_eh_s"] == 700.0
        assert r["hashrate"]["chg_7d_vs_30d_ago_pct"] > 0
        assert r["errors"] is None
        assert "miner confidence" in r["interpretation"]

    def test_partial_and_hashrate_fallback(self):
        with patch("crypto_onchain.http_get_json", side_effect=_mempool(fail=("fees/", "hashrate/3m"))):
            r = call_tool(oc.get_network_health)
        assert r["status"] == "success"
        assert "fees_sat_vb" not in r
        assert r["hashrate"]["source"] == "blockchain.com"
        assert r["hashrate"]["current_eh_s"] == pytest.approx(689.0)
        assert len(r["errors"]) == 2

    def test_all_fail_unavailable(self):
        with patch("crypto_onchain.http_get_json", side_effect=HttpError("blocked", None, "u")):
            r = call_tool(oc.get_network_health)
        assert r["status"] == "unavailable"
        assert "mempool.space" in r["hint"]

    def test_unexpected_exception(self):
        with patch("crypto_onchain.http_get_json", side_effect=ValueError("boom")):
            r = call_tool(oc.get_network_health)
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# Exchange netflows (paid)
# ---------------------------------------------------------------------------

class TestExchangeNetflows:
    def test_no_key_unavailable(self):
        with patch("crypto_onchain.http_get_json", side_effect=HttpError("HTTP 401", 401, "u")) as m:
            r = call_tool(oc.get_exchange_netflows, "BTC", 7)
        assert r["status"] == "unavailable"
        assert "CryptoQuant" in r["hint"] and "$99" in r["hint"]
        # no auth header sent when no key is configured (cloud proxy may inject one)
        for call in m.call_args_list:
            assert not call.kwargs.get("headers")

    def test_cryptoquant_success(self, monkeypatch):
        monkeypatch.setenv("DESK_CRYPTOQUANT_KEY", "cq-key")
        data = {"result": {"data": [{"date": f"2026-11-{10 + i}", "netflow_total": v}
                                    for i, v in enumerate([-500, -300, 200, -100])]}}
        with patch("crypto_onchain.http_get_json", return_value=data) as m:
            r = call_tool(oc.get_exchange_netflows, "BTC", 7)
        assert r["status"] == "success"
        assert r["source"] == "cryptoquant"
        assert r["sum"] == -700.0
        assert r["outflow_days"] == 3
        assert r["daily"][0]["date"] == "2026-11-10"
        assert m.call_args.kwargs["headers"]["Authorization"] == "Bearer cq-key"
        assert "accumulation" in r["interpretation"]

    def test_glassnode_fallback(self, monkeypatch):
        monkeypatch.setenv("DESK_GLASSNODE_KEY", "gn-key")

        def fake(url, params=None, headers=None, **kw):
            if "cryptoquant" in url:
                raise HttpError("HTTP 401", 401, url)
            assert headers == {"X-Api-Key": "gn-key"}
            return [{"t": 1_794_700_800, "v": 1200.5}, {"t": 1_794_787_200, "v": 300.0}]

        with patch("crypto_onchain.http_get_json", side_effect=fake):
            r = call_tool(oc.get_exchange_netflows, "BTC", 7)
        assert r["status"] == "success"
        assert r["source"] == "glassnode"
        assert r["sum"] == 1500.5
        assert "sell pressure" in r["interpretation"]


# ---------------------------------------------------------------------------
# ETH activity
# ---------------------------------------------------------------------------

class TestEthActivity:
    def test_success(self):
        rows = [{"time": f"2026-08-{i:03d}", "AdrActCnt": str(400_000 if i < 113 else 500_000),
                 "TxCnt": "1200000", "FeeTotNtv": "300", "CapMrktCurUSD": "4e11", "PriceUSD": "3300"}
                for i in range(120)]
        with patch("crypto_onchain.http_get_json", return_value={"data": rows}):
            r = call_tool(oc.get_eth_activity)
        assert r["status"] == "success"
        assert r["active_addresses"]["chg_7d_vs_prev30d_pct"] == pytest.approx(25.0)
        assert r["tx_count"]["chg_7d_vs_prev30d_pct"] == 0.0
        assert r["fees_eth"]["avg_7d"] == 300.0
        assert "rising usage" in r["interpretation"]

    def test_http_error(self):
        with patch("crypto_onchain.http_get_json", side_effect=HttpError("HTTP 500", 500, "u")):
            r = call_tool(oc.get_eth_activity)
        assert r["status"] == "error"

    def test_blocked_unavailable(self):
        with patch("crypto_onchain.http_get_json", side_effect=HttpError("blocked", None, "u")):
            r = call_tool(oc.get_eth_activity)
        assert r["status"] == "unavailable"
