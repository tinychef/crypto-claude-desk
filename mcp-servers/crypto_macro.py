#!/usr/bin/env python3
"""Crypto Macro — dollar, rates, liquidity, equities, Fed odds and the macro calendar for crypto.

Data sources (all free, no key):
- FRED public CSV (fred.stlouisfed.org/graph/fredgraph.csv?id=SERIES): broad dollar, 2Y/10Y, real 10Y,
  10Y-2Y, effective fed funds, M2 (also SP500 / NASDAQ100 / DTWEXBGS as fallbacks for correlation).
- Stooq daily CSV (stooq.com/q/d/l/?s=...&i=d): SPX, NDX, gold, DXY futures.
- Polymarket Gamma (public-search) and Kalshi public trade API: FOMC decision probabilities.
- Exchanges via ccxt (desk_common.fetch_with_fallback): BTC daily closes.
- mcp-servers/data/macro_calendar.json: versioned FOMC/CPI/PPI/NFP dates (+ computed Deribit expiries).
"""
import csv
import io
import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from fastmcp import FastMCP

from desk_common import HttpError, fetch_with_fallback, http_get, http_get_json, tool_serializer

logger = logging.getLogger(__name__)
mcp = FastMCP("crypto-macro", tool_serializer=tool_serializer)

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
STOOQ_CSV = "https://stooq.com/q/d/l/"
GAMMA_BASE = "https://gamma-api.polymarket.com"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_FED_SERIES = "KXFEDDECISION"
CALENDAR_PATH = Path(__file__).resolve().parent / "data" / "macro_calendar.json"
MACRO_TTL = 3600

# series id -> (label, kind) ; kind "rate" = percent units (changes in bp), "level" = changes in %
FRED_SERIES: Dict[str, Tuple[str, str]] = {
    "DTWEXBGS": ("Broad trade-weighted USD index", "level"),
    "DGS2": ("2Y Treasury yield %", "rate"),
    "DGS10": ("10Y Treasury yield %", "rate"),
    "DFII10": ("10Y real yield (TIPS) %", "rate"),
    "T10Y2Y": ("10Y-2Y spread %", "rate"),
    "DFF": ("Effective fed funds %", "rate"),
    "WM2NS": ("M2 money stock, $bn (weekly)", "level"),
}
STOOQ_SYMBOLS: Dict[str, Tuple[str, str]] = {
    "SPX": ("^spx", "S&P 500"),
    "NDX": ("^ndx", "Nasdaq 100"),
    "GOLD": ("xauusd", "Gold spot USD"),
    "DXY": ("dx.f", "US Dollar Index (ICE futures)"),
}
# FRED fallbacks when Stooq is unreachable (no gold: LBMA series discontinued on FRED).
FRED_FALLBACK = {"SPX": "SP500", "NDX": "NASDAQ100", "DXY": "DTWEXBGS"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: Optional[datetime] = None) -> str:
    return (ts or _now()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _r(x: Optional[float], nd: int = 2) -> Optional[float]:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), nd)


def _parse_fred_csv(text: str) -> List[Tuple[date, float]]:
    """FRED fredgraph CSV: header 'observation_date,SERIES' (or legacy 'DATE,SERIES'); '.' = missing."""
    rows = list(csv.reader(io.StringIO(text.strip())))
    if not rows or len(rows[0]) < 2 or "date" not in rows[0][0].lower():
        raise ValueError("unexpected FRED CSV header")
    out = []
    for r in rows[1:]:
        if len(r) < 2 or r[1].strip() in ("", "."):
            continue
        try:
            out.append((date.fromisoformat(r[0].strip()), float(r[1])))
        except ValueError:
            continue
    if not out:
        raise ValueError("FRED series has no observations")
    return out


def _parse_stooq_csv(text: str) -> List[Tuple[date, float]]:
    """Stooq CSV: 'Date,Open,High,Low,Close,Volume'. Non-CSV bodies ('No data', captcha, limits) raise."""
    rows = list(csv.reader(io.StringIO(text.strip())))
    if not rows or not rows[0] or rows[0][0].strip().lower() != "date":
        raise ValueError(f"Stooq returned non-CSV body: {text[:60]!r}")
    header = [h.strip().lower() for h in rows[0]]
    ci = header.index("close")
    out = []
    for r in rows[1:]:
        try:
            out.append((date.fromisoformat(r[0].strip()), float(r[ci])))
        except (ValueError, IndexError):
            continue
    if not out:
        raise ValueError("Stooq series has no observations")
    return out


def _fred(series_id: str, days: int = 400) -> List[Tuple[date, float]]:
    start = (_now().date() - timedelta(days=days)).isoformat()
    text = http_get(FRED_CSV, params={"id": series_id, "cosd": start}, ttl=MACRO_TTL, retries=1)
    return _parse_fred_csv(text)


def _stooq(symbol: str, days: int = 400) -> List[Tuple[date, float]]:
    today = _now().date()
    text = http_get(STOOQ_CSV, params={"s": symbol, "i": "d", "d1": (today - timedelta(days=days)).strftime("%Y%m%d"),
                                       "d2": today.strftime("%Y%m%d")}, ttl=MACRO_TTL, retries=1)
    return _parse_stooq_csv(text)


def _value_at_or_before(series: List[Tuple[date, float]], target: date) -> Optional[float]:
    val = None
    for d, v in series:
        if d <= target:
            val = v
        else:
            break
    return val


def _changes(series: List[Tuple[date, float]], kind: str) -> Dict[str, Any]:
    series = sorted(series)
    last_d, last_v = series[-1]
    w = _value_at_or_before(series, last_d - timedelta(days=7))
    m = _value_at_or_before(series, last_d - timedelta(days=30))
    out: Dict[str, Any] = {"last": _r(last_v, 3), "date": last_d.isoformat()}
    if kind == "rate":
        out["chg_1w_bp"] = _r((last_v - w) * 100, 1) if w is not None else None
        out["chg_1m_bp"] = _r((last_v - m) * 100, 1) if m is not None else None
    else:
        out["chg_1w_pct"] = _r((last_v / w - 1) * 100, 2) if w else None
        out["chg_1m_pct"] = _r((last_v / m - 1) * 100, 2) if m else None
    return out


def _risk_read(fred: Dict[str, Dict[str, Any]], mkts: Dict[str, Dict[str, Any]]) -> Tuple[str, int, List[str]]:
    score, drivers = 0, []

    def bump(cond_up, cond_down, up_txt, down_txt):
        nonlocal score
        if cond_up:
            score += 1
            drivers.append(up_txt)
        elif cond_down:
            score -= 1
            drivers.append(down_txt)

    for k in ("SPX", "NDX"):
        c = (mkts.get(k) or {}).get("chg_1m_pct")
        if c is not None:
            bump(c > 2, c < -2, f"{k} {c:+.1f}% 1m (risk-on)", f"{k} {c:+.1f}% 1m (risk-off)")
    dxy = (mkts.get("DXY") or {}).get("chg_1m_pct")
    if dxy is None:
        dxy = (fred.get("DTWEXBGS") or {}).get("chg_1m_pct")
    if dxy is not None:
        bump(dxy < -1, dxy > 1, f"dollar {dxy:+.1f}% 1m (tailwind)", f"dollar {dxy:+.1f}% 1m (headwind)")
    ry = (fred.get("DFII10") or {}).get("chg_1m_bp")
    if ry is not None:
        bump(ry < -15, ry > 15, f"real 10Y {ry:+.0f}bp 1m (easing)", f"real 10Y {ry:+.0f}bp 1m (tightening)")
    m2 = (fred.get("WM2NS") or {}).get("chg_1m_pct")
    if m2 is not None:
        bump(m2 > 0.5, m2 < -0.3, f"M2 {m2:+.2f}% 1m (liquidity up)", f"M2 {m2:+.2f}% 1m (liquidity down)")
    label = "risk-on" if score >= 2 else "risk-off" if score <= -2 else "mixed/neutral"
    return label, score, drivers


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_macro_snapshot() -> Dict[str, Any]:
    """Macro backdrop for crypto: FRED (broad dollar, 2Y, 10Y, real 10Y, 10Y-2Y, fed funds, M2) and Stooq
    (SPX, NDX, gold, DXY) with last value, 1w and 1m change (rates in bp, levels in %), plus a risk-on/off
    score from equities, dollar, real yields and M2. Each series fails independently (see data_gaps).
    Read: weaker dollar + falling real yields + rising equities/M2 = tailwind for BTC; the reverse = headwind."""
    try:
        fred: Dict[str, Dict[str, Any]] = {}
        mkts: Dict[str, Dict[str, Any]] = {}
        gaps: List[str] = []
        for sid, (label, kind) in FRED_SERIES.items():
            try:
                fred[sid] = {"label": label, **_changes(_fred(sid), kind)}
            except (HttpError, ValueError) as e:
                gaps.append(f"FRED {sid}: {str(e)[:100]}")
        for key, (sym, label) in STOOQ_SYMBOLS.items():
            try:
                mkts[key] = {"label": label, **_changes(_stooq(sym), "level")}
            except (HttpError, ValueError) as e:
                gaps.append(f"Stooq {key}: {str(e)[:100]}")
        if not fred and not mkts:
            return {"status": "error", "error": "all macro sources failed", "data_gaps": gaps, "source": "fred,stooq"}
        label, score, drivers = _risk_read(fred, mkts)
        curve = (fred.get("T10Y2Y") or {}).get("last")
        extra = ""
        if curve is not None:
            extra = " Curve inverted (recession signal)." if curve < 0 else f" Curve +{curve:.2f}% (un-inverted)."
        out = {
            "status": "success", "source": "fred,stooq", "as_of": _iso(), "rates_and_liquidity": fred,
            "markets": mkts, "risk_regime": label, "risk_score": score, "drivers": drivers,
            "interpretation": f"Macro {label} (score {score:+d}): " + ("; ".join(drivers) or "no strong signal") +
                              "." + extra,
        }
        if gaps:
            out["data_gaps"] = gaps
        return out
    except Exception as e:
        logger.exception("get_macro_snapshot failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _daily_series(asset: str, days: int) -> Tuple[List[Tuple[date, float]], str]:
    sym, _ = STOOQ_SYMBOLS[asset]
    try:
        return _stooq(sym, days), f"stooq:{sym}"
    except (HttpError, ValueError) as e:
        fb = FRED_FALLBACK.get(asset)
        if not fb:
            raise
        logger.info("stooq %s failed (%s), falling back to FRED %s", asset, e, fb)
        return _fred(fb, days), f"fred:{fb}"


def _corr_beta(x: np.ndarray, y: np.ndarray) -> Tuple[Optional[float], Optional[float]]:
    if len(x) < 5:
        return None, None
    if np.std(x) == 0 or np.std(y) == 0:
        return None, None
    corr = float(np.corrcoef(x, y)[0, 1])
    beta = float(np.cov(x, y, ddof=1)[0, 1] / np.var(y, ddof=1))
    return corr, beta


@mcp.tool()
def get_crypto_macro_correlation(window_days: int = 30) -> Dict[str, Any]:
    """Rolling correlation of BTC daily returns vs SPX, NDX, DXY and gold over `window_days` and 90 aligned
    trading days, plus BTC beta to NDX. Read: corr to NDX > 0.5 = BTC trades as high-beta tech (watch equities/
    Fed); corr to DXY < -0.3 = dollar is a driver; |corr| < 0.2 = BTC idiosyncratic (crypto-native flows dominate)."""
    try:
        if not isinstance(window_days, int) or window_days < 10 or window_days > 365:
            return {"status": "error", "error": "window_days must be an integer between 10 and 365"}
        long_win = max(90, window_days)
        cal_days = int(long_win * 1.5) + 30
        ohlcv, venue, _ = fetch_with_fallback(lambda ex: ex.fetch_ohlcv("BTC/USDT", "1d", limit=min(cal_days, 1000)))
        btc = {datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).date(): float(c[4]) for c in ohlcv}
        results: Dict[str, Any] = {}
        sources: Dict[str, str] = {"BTC": venue}
        gaps: List[str] = []
        for asset in ("SPX", "NDX", "DXY", "GOLD"):
            try:
                series, src = _daily_series(asset, cal_days)
            except (HttpError, ValueError) as e:
                gaps.append(f"{asset}: {str(e)[:100]}")
                continue
            sources[asset] = src
            other = dict(series)
            common = sorted(d for d in other if d in btc)
            if len(common) < 12:
                gaps.append(f"{asset}: only {len(common)} overlapping days")
                continue
            b = np.array([btc[d] for d in common])
            o = np.array([other[d] for d in common])
            rb, ro = np.diff(b) / b[:-1], np.diff(o) / o[:-1]
            entry: Dict[str, Any] = {}
            for label, n in {f"{window_days}d": window_days, "90d": 90}.items():
                k = min(n, len(rb))
                corr, beta = _corr_beta(rb[-k:], ro[-k:])
                entry[f"corr_{label}"] = _r(corr, 3)
                entry[f"beta_{label}"] = _r(beta, 2)
                if k < n:
                    entry[f"obs_{label}"] = k
            results[asset] = entry
        if not results:
            return {"status": "error", "error": "no macro series available for correlation", "data_gaps": gaps,
                    "source": "stooq,fred"}
        reads = []
        ndx = (results.get("NDX") or results.get("SPX") or {}).get(f"corr_{window_days}d")
        if ndx is not None:
            reads.append(f"BTC-equities corr {ndx:+.2f}: " + ("trading as high-beta risk asset" if ndx > 0.5 else
                                                             "moderately linked to equities" if ndx > 0.2 else
                                                             "decoupled from equities"))
        dxy = (results.get("DXY") or {}).get(f"corr_{window_days}d")
        if dxy is not None:
            reads.append(f"BTC-dollar corr {dxy:+.2f}: " + ("dollar strength is a headwind" if dxy < -0.3 else
                                                           "dollar not a key driver"))
        gold = (results.get("GOLD") or {}).get(f"corr_{window_days}d")
        if gold is not None and gold > 0.4:
            reads.append(f"trading with gold ({gold:+.2f}) — debasement narrative active")
        beta = (results.get("NDX") or {}).get(f"beta_{window_days}d")
        out = {"status": "success", "source": "ccxt,stooq,fred", "as_of": _iso(), "window_days": window_days,
               "note": "Windows count aligned trading days (BTC weekend moves fold into Monday's return).",
               "correlations": results, "btc_beta_to_ndx": beta, "series_sources": sources,
               "interpretation": "; ".join(reads) or "insufficient data"}
        if gaps:
            out["data_gaps"] = gaps
        return out
    except HttpError as e:
        return {"status": "error", "error": str(e), "source": "stooq,fred"}
    except Exception as e:
        logger.exception("get_crypto_macro_correlation failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _parse_json_list(val: Any) -> List[Any]:
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            v = json.loads(val)
            return v if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _polymarket_fed(now: datetime, max_meetings: int = 2) -> List[Dict[str, Any]]:
    data = http_get_json(f"{GAMMA_BASE}/public-search", params={"q": "fed decision", "limit_per_type": 20},
                         ttl=300)
    events = []
    for ev in (data or {}).get("events") or []:
        title = str(ev.get("title", ""))
        if ev.get("closed") or "fed" not in title.lower() or "decision" not in title.lower():
            continue
        try:
            end = datetime.fromisoformat(str(ev.get("endDate")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if end < now:
            continue
        outcomes = []
        for m in ev.get("markets") or []:
            if m.get("closed"):
                continue
            prices = _parse_json_list(m.get("outcomePrices"))
            names = _parse_json_list(m.get("outcomes"))
            if not prices:
                continue
            idx = names.index("Yes") if "Yes" in names else 0
            try:
                p = float(prices[idx])
            except (ValueError, IndexError):
                continue
            outcomes.append({"outcome": m.get("groupItemTitle") or m.get("question"), "probability": round(p, 4),
                             "volume_usd": _r(float(m.get("volume") or 0), 0)})
        if outcomes:
            outcomes.sort(key=lambda o: -o["probability"])
            events.append({"venue": "polymarket", "event": title, "slug": ev.get("slug"),
                           "resolves_by": _iso(end), "outcomes": outcomes,
                           "sum_probability": round(sum(o["probability"] for o in outcomes), 3)})
    events.sort(key=lambda e: e["resolves_by"])
    return events[:max_meetings]


def _kalshi_price(m: Dict[str, Any]) -> Optional[float]:
    def get(name):
        if m.get(f"{name}_dollars") not in (None, ""):
            try:
                return float(m[f"{name}_dollars"])
            except (TypeError, ValueError):
                return None
        v = m.get(name)
        return float(v) / 100 if isinstance(v, (int, float)) else None

    bid, ask, last = get("yes_bid"), get("yes_ask"), get("last_price")
    if bid and ask:
        return round((bid + ask) / 2, 4)
    return last


def _kalshi_fed(now: datetime) -> List[Dict[str, Any]]:
    data = http_get_json(f"{KALSHI_BASE}/markets", params={"series_ticker": KALSHI_FED_SERIES, "status": "open",
                                                          "limit": 100}, ttl=300)
    by_event: Dict[str, List[Dict[str, Any]]] = {}
    for m in (data or {}).get("markets") or []:
        by_event.setdefault(m.get("event_ticker", "?"), []).append(m)
    events = []
    for ev, ms in by_event.items():
        close = min(str(m.get("close_time") or "") for m in ms)
        outcomes = []
        for m in ms:
            p = _kalshi_price(m)
            if p is not None:
                outcomes.append({"outcome": m.get("yes_sub_title") or m.get("subtitle") or m.get("title") or
                                 m.get("ticker"), "probability": p})
        if outcomes:
            outcomes.sort(key=lambda o: -o["probability"])
            events.append({"venue": "kalshi", "event": ev, "closes": close, "outcomes": outcomes,
                           "sum_probability": round(sum(o["probability"] for o in outcomes), 3)})
    events.sort(key=lambda e: e["closes"])
    return events[:2]


def _load_calendar() -> Dict[str, Any]:
    with open(CALENDAR_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _next_fomc(today: date) -> Optional[Dict[str, Any]]:
    try:
        cal = _load_calendar()
    except (OSError, ValueError):
        return None
    fomc = sorted((e for e in cal.get("events", []) if e.get("type") == "FOMC" and e["date"] >= today.isoformat()),
                  key=lambda e: e["date"])
    if not fomc:
        return None
    e = fomc[0]
    return {"date": e["date"], "confirmed": e.get("confirmed", False),
            "days_until": (date.fromisoformat(e["date"]) - today).days}


@mcp.tool()
def get_fed_odds() -> Dict[str, Any]:
    """Market-implied odds for the next FOMC decisions from Polymarket ('Fed Decision in <Month>?' events) and
    Kalshi (KXFEDDECISION series), with the next meeting date from the macro calendar. Each venue fails
    independently. Read: the dominant outcome and its probability is what is priced; a surprise vs this
    pricing moves BTC (hawkish surprise = risk-off). Polymarket per-outcome prices may not sum exactly to 1."""
    try:
        now = _now()
        venues: List[Dict[str, Any]] = []
        gaps: List[str] = []
        try:
            venues.extend(_polymarket_fed(now))
        except HttpError as e:
            gaps.append(f"polymarket: {e}")
        try:
            venues.extend(_kalshi_fed(now))
        except HttpError as e:
            gaps.append(f"kalshi: {e}")
        nxt = _next_fomc(now.date())
        if not venues:
            out = {"status": "error", "error": "no Fed odds from any venue", "data_gaps": gaps,
                   "source": "polymarket,kalshi", "next_fomc": nxt}
            return out
        first = venues[0]
        top = first["outcomes"][0]
        label = str(top["outcome"]).lower()
        tone = "hawkish" if ("increase" in label or "hike" in label) else \
            "dovish" if ("decrease" in label or "cut" in label) else "on hold"
        read = (f"Next decision priced {tone}: '{top['outcome']}' {top['probability']*100:.0f}% "
                f"({first['venue']}, {first['event']}).")
        if len(first["outcomes"]) > 1:
            alt = first["outcomes"][1]
            read += f" Runner-up '{alt['outcome']}' {alt['probability']*100:.0f}%."
        out = {"status": "success", "source": ",".join(sorted({v["venue"] for v in venues})), "as_of": _iso(),
               "next_fomc": nxt, "markets": venues, "interpretation": read}
        if gaps:
            out["data_gaps"] = gaps
        return out
    except Exception as e:
        logger.exception("get_fed_odds failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _us_dst(d: date) -> bool:
    """US daylight saving: second Sunday of March to first Sunday of November."""
    def nth_sunday(year, month, n):
        first = date(year, month, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    return nth_sunday(d.year, 3, 2) <= d < nth_sunday(d.year, 11, 1)


def _et_to_utc(d: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    offset = 4 if _us_dst(d) else 5
    return datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc) + timedelta(hours=offset)


def _last_friday(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() != 4:
        d -= timedelta(days=1)
    return d


def _deribit_expiries(start: date, end: date) -> List[Dict[str, Any]]:
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        d = _last_friday(y, m)
        if start <= d <= end:
            quarterly = m in (3, 6, 9, 12)
            out.append({"datetime_utc": datetime(d.year, d.month, d.day, 8, 0, tzinfo=timezone.utc),
                        "date": d.isoformat(), "type": "OPTIONS_EXPIRY",
                        "title": f"Deribit {'quarterly' if quarterly else 'monthly'} options/futures expiry (08:00 UTC)",
                        "impact": "high" if quarterly else "medium", "confirmed": True,
                        "source": "computed: last Friday of month 08:00 UTC"})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


@mcp.tool()
def get_macro_calendar(days_ahead: int = 21) -> Dict[str, Any]:
    """Upcoming macro events in the next `days_ahead` days: FOMC decisions, CPI, PPI, NFP (versioned JSON,
    `confirmed: false` = estimated date) plus computed Deribit monthly/quarterly expiries (last Friday 08:00 UTC).
    Sorted, with days_until and impact. Read: avoid opening fresh leveraged size in the 24h before high-impact
    events; vol typically compresses into FOMC/CPI and expands after."""
    try:
        if not isinstance(days_ahead, int) or days_ahead < 1 or days_ahead > 365:
            return {"status": "error", "error": "days_ahead must be an integer between 1 and 365"}
        now = _now()
        today = now.date()
        end = today + timedelta(days=days_ahead)
        cal = _load_calendar()
        events: List[Dict[str, Any]] = []
        for e in cal.get("events", []):
            try:
                d = date.fromisoformat(e["date"])
            except (KeyError, ValueError):
                continue
            if not (today <= d <= end):
                continue
            dt = _et_to_utc(d, e.get("time_et") or "08:30")
            events.append({**{k: e[k] for k in ("date", "type", "title", "impact", "confirmed") if k in e},
                           "datetime_utc": dt})
        events.extend(_deribit_expiries(today, end))
        events = [e for e in events if e["datetime_utc"] >= now - timedelta(hours=12)]
        events.sort(key=lambda e: e["datetime_utc"])
        for e in events:
            e["hours_until"] = _r((e["datetime_utc"] - now).total_seconds() / 3600, 1)
            e["days_until"] = _r((e["datetime_utc"] - now).total_seconds() / 86400, 1)
            e["datetime_utc"] = _iso(e["datetime_utc"])
            e.pop("source", None)
        high = [e for e in events if e.get("impact") == "high"]
        if high:
            h = high[0]
            read = f"Next high-impact: {h['title']} in {h['days_until']}d ({h['datetime_utc']})."
            if h["hours_until"] is not None and h["hours_until"] < 48:
                read += " Within 48h — reduce leverage / widen stops."
        else:
            read = f"No high-impact events in the next {days_ahead} days."
        unconfirmed = sum(1 for e in events if not e.get("confirmed", False))
        if unconfirmed:
            read += f" {unconfirmed} date(s) estimated (confirmed=false)."
        return {"status": "success", "source": f"macro_calendar.json v{cal.get('version', '?')} + computed expiries",
                "as_of": _iso(), "days_ahead": days_ahead, "count": len(events), "events": events[:40],
                "interpretation": read}
    except (OSError, ValueError) as e:
        return {"status": "error", "error": f"calendar file error: {e}", "source": "macro_calendar.json"}
    except Exception as e:
        logger.exception("get_macro_calendar failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


if __name__ == "__main__":
    mcp.run()
