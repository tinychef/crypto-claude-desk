"""Tests for crypto_macro.py (crypto-macro). Fully mocked — no network."""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

os.environ["DESK_HTTP_CACHE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers"))

import crypto_macro as mod  # noqa: E402
from desk_common import HttpError  # noqa: E402
from helpers import call_tool  # noqa: E402

FIXED_NOW = datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc)
END = date(2026, 11, 16)


def _days(n, weekdays_only=False):
    out, d = [], END
    while len(out) < n:
        if not weekdays_only or d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def _linear(start, end, n, weekdays_only=False):
    days = _days(n, weekdays_only)
    vals = np.linspace(start, end, n)
    return list(zip(days, vals))


def fred_csv(points, sid="X", legacy=False, missing_at=None):
    head = "DATE" if legacy else "observation_date"
    lines = [f"{head},{sid}"]
    for i, (d, v) in enumerate(points):
        lines.append(f"{d.isoformat()},{'.' if i == missing_at else round(v, 4)}")
    return "\n".join(lines) + "\n"


def stooq_csv(points):
    lines = ["Date,Open,High,Low,Close,Volume"]
    for d, v in points:
        lines.append(f"{d.isoformat()},{v},{v},{v},{round(v, 4)},0")
    return "\n".join(lines) + "\n"


def _router(fred=None, stooq=None):
    fred, stooq = fred or {}, stooq or {}

    def fake(url, params=None, **kw):
        if "fred.stlouisfed.org" in url:
            r = fred.get(params["id"])
        elif "stooq.com" in url:
            r = stooq.get(params["s"])
        else:
            raise AssertionError(url)
        if r is None:
            raise HttpError(f"HTTP 404 from {url}", 404, url)
        if isinstance(r, Exception):
            raise r
        return r
    return fake


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

class TestParsers:
    def test_fred_both_headers_and_missing(self):
        pts = _linear(1, 2, 5)
        assert len(mod._parse_fred_csv(fred_csv(pts, legacy=True, missing_at=2))) == 4
        assert mod._parse_fred_csv(fred_csv(pts))[-1][1] == pytest.approx(2)

    def test_fred_bad(self):
        with pytest.raises(ValueError):
            mod._parse_fred_csv("<html>nope</html>")

    def test_stooq_non_csv(self):
        with pytest.raises(ValueError):
            mod._parse_stooq_csv("No data")
        assert mod._parse_stooq_csv(stooq_csv(_linear(10, 11, 3)))[0][1] == 10

    def test_changes_rate_vs_level(self):
        pts = _linear(4.0, 4.3, 31)  # +30bp over 30 days
        r = mod._changes(pts, "rate")
        assert r["chg_1m_bp"] == pytest.approx(30, abs=0.1)
        lv = mod._changes(_linear(100, 110, 31), "level")
        assert lv["chg_1m_pct"] == pytest.approx(10, abs=0.01)


# ---------------------------------------------------------------------------
# get_macro_snapshot
# ---------------------------------------------------------------------------

def _snapshot_routes():
    fred = {
        "DTWEXBGS": fred_csv(_linear(125, 122.5, 40), "DTWEXBGS"),    # dollar -2%
        "DGS2": fred_csv(_linear(4.0, 3.8, 40)),
        "DGS10": fred_csv(_linear(4.4, 4.2, 40)),
        "DFII10": fred_csv(_linear(2.0, 1.7, 40)),                    # real yield -30bp
        "T10Y2Y": fred_csv(_linear(0.3, 0.4, 40)),
        "DFF": fred_csv(_linear(4.33, 4.33, 40)),
        "WM2NS": fred_csv(_linear(21500, 21800, 40)),                 # M2 up
    }
    stooq = {
        "^spx": stooq_csv(_linear(5800, 6100, 40, True)),
        "^ndx": stooq_csv(_linear(20000, 21400, 40, True)),
        "xauusd": HttpError("network error", None, "stooq"),
        "dx.f": stooq_csv(_linear(104, 101.5, 40, True)),
    }
    return fred, stooq


class TestMacroSnapshot:
    def test_success_risk_on_with_gap(self):
        fred, stooq = _snapshot_routes()
        with patch("crypto_macro.http_get", side_effect=_router(fred, stooq)):
            r = call_tool(mod.get_macro_snapshot)
        assert r["status"] == "success"
        assert r["risk_regime"] == "risk-on" and r["risk_score"] >= 4
        assert r["rates_and_liquidity"]["DFII10"]["chg_1m_bp"] < -15
        assert r["markets"]["NDX"]["chg_1m_pct"] > 2
        assert "GOLD" not in r["markets"] and any("GOLD" in g for g in r["data_gaps"])
        assert "Curve +0.40%" in r["interpretation"]

    def test_risk_off(self):
        fred = {"DFII10": fred_csv(_linear(1.7, 2.1, 40)), "DTWEXBGS": fred_csv(_linear(120, 124, 40))}
        stooq = {"^spx": stooq_csv(_linear(6100, 5700, 40, True)), "^ndx": stooq_csv(_linear(21400, 19500, 40, True))}
        with patch("crypto_macro.http_get", side_effect=_router(fred, stooq)):
            r = call_tool(mod.get_macro_snapshot)
        assert r["risk_regime"] == "risk-off"

    def test_all_fail(self):
        with patch("crypto_macro.http_get", side_effect=HttpError("network error", None, "x")):
            r = call_tool(mod.get_macro_snapshot)
        assert r["status"] == "error" and len(r["data_gaps"]) == 11


# ---------------------------------------------------------------------------
# get_crypto_macro_correlation
# ---------------------------------------------------------------------------

def _corr_fixture():
    rng = np.random.default_rng(7)
    tdays = _days(140, weekdays_only=True)
    r_ndx = rng.normal(0, 0.01, len(tdays) - 1)
    ndx = 20000 * np.concatenate([[1], np.cumprod(1 + r_ndx)])
    btc_trade = 90000 * np.concatenate([[1], np.cumprod(1 + 2 * r_ndx)])   # beta 2, corr 1
    dxy = 100 * np.concatenate([[1], np.cumprod(1 - r_ndx)])               # corr -1
    btc_map = dict(zip(tdays, btc_trade))
    ohlcv, d = [], tdays[0]
    while d <= END:
        px = btc_map.get(d, 95000.0)  # weekends: arbitrary, not aligned
        ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
        ohlcv.append([ts, px, px, px, px, 1.0])
        d += timedelta(days=1)
    stooq = {"^ndx": stooq_csv(list(zip(tdays, ndx))), "dx.f": stooq_csv(list(zip(tdays, dxy))),
             "^spx": HttpError("network error", None, "stooq"), "xauusd": "No data"}
    fred = {"SP500": fred_csv(list(zip(tdays, ndx)), "SP500")}  # FRED fallback for SPX
    return ohlcv, fred, stooq


class TestCorrelation:
    def test_success(self):
        ohlcv, fred, stooq = _corr_fixture()
        with patch("crypto_macro.fetch_with_fallback", return_value=(ohlcv, "okx", [])), \
                patch("crypto_macro.http_get", side_effect=_router(fred, stooq)):
            r = call_tool(mod.get_crypto_macro_correlation, 30)
        assert r["status"] == "success"
        c = r["correlations"]
        assert c["NDX"]["corr_30d"] == pytest.approx(1.0, abs=1e-6)
        assert c["NDX"]["corr_90d"] == pytest.approx(1.0, abs=1e-6)
        assert r["btc_beta_to_ndx"] == pytest.approx(2.0, abs=1e-3)
        assert c["DXY"]["corr_30d"] < -0.99
        assert r["series_sources"]["SPX"] == "fred:SP500"
        assert "GOLD" not in c and any("GOLD" in g for g in r["data_gaps"])
        assert "high-beta" in r["interpretation"]

    def test_bad_window(self):
        assert call_tool(mod.get_crypto_macro_correlation, 3)["status"] == "error"

    def test_btc_fetch_fails(self):
        with patch("crypto_macro.fetch_with_fallback", side_effect=RuntimeError("all exchanges failed")):
            r = call_tool(mod.get_crypto_macro_correlation, 30)
        assert r["status"] == "error" and "all exchanges failed" in r["error"]

    def test_no_macro_series(self):
        ohlcv, _, _ = _corr_fixture()
        with patch("crypto_macro.fetch_with_fallback", return_value=(ohlcv, "okx", [])), \
                patch("crypto_macro.http_get", side_effect=HttpError("network error", None, "x")):
            r = call_tool(mod.get_crypto_macro_correlation, 30)
        assert r["status"] == "error" and len(r["data_gaps"]) == 4


# ---------------------------------------------------------------------------
# get_fed_odds
# ---------------------------------------------------------------------------

def _pm_market(label, yes):
    return {"question": f"Will the Fed {label}?", "groupItemTitle": label, "outcomes": '["Yes", "No"]',
            "outcomePrices": json.dumps([str(yes), str(round(1 - yes, 4))]), "closed": False, "volume": "1000"}


PM_SEARCH = {"events": [
    {"title": "Fed decision in January?", "slug": "old", "closed": True, "endDate": "2026-01-28T00:00:00Z",
     "markets": [_pm_market("No change", 1)]},
    {"title": "Fed Decision in January?", "slug": "jan", "closed": False, "endDate": "2027-01-28T04:59:00Z",
     "markets": [_pm_market("No change", 0.35), _pm_market("25 bps increase", 0.62)]},
    {"title": "Fed Decision in December?", "slug": "dec", "closed": False, "endDate": "2026-12-10T04:59:00Z",
     "markets": [_pm_market("25 bps increase", 0.915), _pm_market("No change", 0.0325),
                 _pm_market("25 bps decrease", 0.0045)]},
    {"title": "Bitcoin above 100k?", "slug": "btc", "closed": False, "endDate": "2026-12-31T00:00:00Z",
     "markets": [_pm_market("Yes", 0.2)]},
], "pagination": {}}

KALSHI = {"markets": [
    {"ticker": "KXFEDDECISION-26DEC-H25", "event_ticker": "KXFEDDECISION-26DEC", "yes_sub_title": "Hike 25bps",
     "yes_bid": 88, "yes_ask": 90, "last_price": 89, "close_time": "2026-12-09T18:55:00Z"},
    {"ticker": "KXFEDDECISION-26DEC-H0", "event_ticker": "KXFEDDECISION-26DEC", "yes_sub_title": "Hold",
     "yes_bid_dollars": "0.0500", "yes_ask_dollars": "0.0700", "close_time": "2026-12-09T18:55:00Z"},
    {"ticker": "KXFEDDECISION-27JAN-H0", "event_ticker": "KXFEDDECISION-27JAN", "yes_sub_title": "Hold",
     "yes_bid": 0, "yes_ask": 0, "last_price": 40, "close_time": "2027-01-27T18:55:00Z"},
]}


def _fed_router(pm=PM_SEARCH, kalshi=KALSHI):
    def fake(url, params=None, **kw):
        if "polymarket" in url:
            if isinstance(pm, Exception):
                raise pm
            assert params["q"] == "fed decision"
            return pm
        if "kalshi" in url:
            if isinstance(kalshi, Exception):
                raise kalshi
            assert params["series_ticker"] == "KXFEDDECISION"
            return kalshi
        raise AssertionError(url)
    return fake


class TestFedOdds:
    def test_success_both_venues(self):
        with patch("crypto_macro.http_get_json", side_effect=_fed_router()), \
                patch("crypto_macro._now", return_value=FIXED_NOW):
            r = call_tool(mod.get_fed_odds)
        assert r["status"] == "success" and r["source"] == "kalshi,polymarket"
        pm = [m for m in r["markets"] if m["venue"] == "polymarket"]
        assert [m["slug"] for m in pm] == ["dec", "jan"]          # sorted by date, closed/irrelevant dropped
        assert pm[0]["outcomes"][0] == {"outcome": "25 bps increase", "probability": 0.915, "volume_usd": 1000.0}
        ks = [m for m in r["markets"] if m["venue"] == "kalshi"]
        assert ks[0]["event"] == "KXFEDDECISION-26DEC"
        assert ks[0]["outcomes"][0]["probability"] == pytest.approx(0.89)
        assert ks[0]["outcomes"][1]["probability"] == pytest.approx(0.06)
        assert ks[1]["outcomes"][0]["probability"] == pytest.approx(0.40)  # last_price fallback
        assert r["next_fomc"]["date"] == "2026-12-09" and r["next_fomc"]["days_until"] == 22
        assert "hawkish" in r["interpretation"]

    def test_kalshi_down_is_partial(self):
        with patch("crypto_macro.http_get_json",
                   side_effect=_fed_router(kalshi=HttpError("network error", None, "kalshi"))), \
                patch("crypto_macro._now", return_value=FIXED_NOW):
            r = call_tool(mod.get_fed_odds)
        assert r["status"] == "success" and r["source"] == "polymarket"
        assert any("kalshi" in g for g in r["data_gaps"])

    def test_all_down(self):
        with patch("crypto_macro.http_get_json", side_effect=HttpError("HTTP 500", 500, "x")):
            r = call_tool(mod.get_fed_odds)
        assert r["status"] == "error" and len(r["data_gaps"]) == 2


# ---------------------------------------------------------------------------
# get_macro_calendar
# ---------------------------------------------------------------------------

class TestMacroCalendar:
    def test_calendar_file_is_valid(self):
        cal = json.loads(mod.CALENDAR_PATH.read_text())
        assert cal["version"] and cal["events"]
        types = {e["type"] for e in cal["events"]}
        assert {"FOMC", "CPI", "PPI", "NFP"} <= types
        for e in cal["events"]:
            date.fromisoformat(e["date"])
            assert e["impact"] in ("high", "medium", "low") and isinstance(e["confirmed"], bool)
        assert sum(1 for e in cal["events"] if e["type"] == "FOMC" and e["date"].startswith("2027")) == 8

    def test_upcoming_events(self):
        with patch("crypto_macro._now", return_value=FIXED_NOW):
            r = call_tool(mod.get_macro_calendar, 30)
        assert r["status"] == "success"
        ev = r["events"]
        assert [e["type"] for e in ev][:5] == ["OPTIONS_EXPIRY", "NFP", "FOMC", "CPI", "PPI"]
        assert ev[0]["datetime_utc"] == "2026-11-27T08:00:00Z" and ev[0]["impact"] == "medium"
        fomc = ev[2]
        assert fomc["datetime_utc"] == "2026-12-09T19:00:00Z"   # 14:00 ET in winter
        assert fomc["days_until"] == pytest.approx(22.8, abs=0.05)
        assert ev == sorted(ev, key=lambda e: e["datetime_utc"])
        assert "NFP" in r["interpretation"]

    def test_quarterly_expiry_and_dst(self):
        assert mod._last_friday(2026, 12) == date(2026, 12, 25)
        q = mod._deribit_expiries(date(2026, 12, 1), date(2027, 1, 31))
        assert [e["impact"] for e in q] == ["high", "medium"]
        assert mod._et_to_utc(date(2027, 6, 9), "14:00").hour == 18
        assert mod._et_to_utc(date(2027, 1, 27), "14:00").hour == 19

    def test_invalid_args_and_missing_file(self, tmp_path):
        assert call_tool(mod.get_macro_calendar, 0)["status"] == "error"
        with patch.object(mod, "CALENDAR_PATH", tmp_path / "missing.json"):
            r = call_tool(mod.get_macro_calendar, 21)
        assert r["status"] == "error" and "calendar" in r["error"]


def test_server_name():
    assert mod.mcp.name == "crypto-macro"
