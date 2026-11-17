#!/usr/bin/env python3
"""Crypto Flows — capital-flow signals: spot ETF flows, Coinbase premium, Kimchi premium, stablecoin supply.

Data sources:
- Farside Investors HTML tables (free, scrape) — ETF daily flows per fund (US$m)
- SoSoValue OpenAPI (free key, header ``x-soso-api-key``) — ETF totals, secondary
- Coinbase Exchange public ticker (free) / ccxt coinbase fallback — US spot price
- Upbit public ticker (free) — Korean KRW price
- Frankfurter (ECB, free) / open.er-api.com (free) — USD/KRW FX
- OKX (+ fallbacks) via ccxt, Kraken USDT/USD via ccxt — global reference price
- DefiLlama stablecoins API (free) — stablecoin supply, mint/burn, per-chain
"""
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastmcp import FastMCP

from desk_common import (
    HttpError,
    base_symbol,
    fetch_with_fallback,
    get_exchange,
    get_key,
    http_get,
    http_get_json,
    tool_serializer,
    unavailable,
)

logger = logging.getLogger(__name__)
mcp = FastMCP("crypto-flows", tool_serializer=tool_serializer)

FARSIDE_URLS = {
    "BTC": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
    "ETH": "https://farside.co.uk/ethereum-etf-flow-all-data/",
    "SOL": "https://farside.co.uk/sol/",
}
SOSOVALUE_URL = "https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart"
SOSOVALUE_TYPES = {"BTC": "us-btc-spot", "ETH": "us-eth-spot", "SOL": "us-sol-spot"}
COINBASE_TICKER = "https://api.exchange.coinbase.com/products/{product}/ticker"
UPBIT_TICKER = "https://api.upbit.com/v1/ticker"
FX_SOURCES = [
    ("frankfurter.dev", "https://api.frankfurter.dev/v1/latest", {"base": "USD", "symbols": "KRW"}),
    ("frankfurter.app", "https://api.frankfurter.app/latest", {"from": "USD", "to": "KRW"}),
    ("open.er-api.com", "https://open.er-api.com/v6/latest/USD", None),
]
STABLE_CHART_URL = "https://stablecoins.llama.fi/stablecoincharts/all"
STABLE_LIST_URL = "https://stablecoins.llama.fi/stablecoins"

# Browser-like headers: Farside sits behind Cloudflare and rejects obvious bots.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}
GLOBAL_SPOT_VENUES = ["okx", "kucoin", "mexc", "kraken"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _short(e: Exception, n: int = 160) -> str:
    msg = str(e)
    m = re.search(r"host='([^']+)'", msg)
    if "ProxyError" in msg or "Tunnel connection failed" in msg:
        return f"network blocked ({m.group(1) if m else 'host'} not reachable via proxy)"
    return msg[:n]


def _r(x: Optional[float], n: int = 2) -> Optional[float]:
    return None if x is None else round(float(x), n)


# ---------------------------------------------------------------------------
# Farside HTML parsing (stdlib only — no lxml/bs4 in the venv)
# ---------------------------------------------------------------------------

class _TableParser(HTMLParser):
    """Collect every <table> as a list of rows of cell texts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: List[List[List[str]]] = []
        self._depth = 0
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            self.tables.append([])
        elif self._depth and tag == "tr":
            self._row = []
        elif self._depth and tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row and self.tables:
                self.tables[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _parse_date(text: str) -> Optional[datetime]:
    text = text.strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_flow(text: str) -> Optional[float]:
    """Farside cell -> float US$m. '(12.3)' negative, '-' zero, '' unknown."""
    t = (text or "").strip().replace(",", "").replace("$", "")
    if t == "":
        return None
    if t in ("-", "–", "—", "0"):
        return 0.0
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def parse_farside_html(html: str) -> List[Dict[str, Any]]:
    """Return [{date, total, funds:{ticker: flow}}] oldest->newest (US$m)."""
    p = _TableParser()
    p.feed(html)
    best: Tuple[int, List[List[str]]] = (0, [])
    for tbl in p.tables:
        n = sum(1 for row in tbl if row and _parse_date(row[0]))
        if n > best[0]:
            best = (n, tbl)
    table = best[1]
    if not table:
        return []
    header: List[str] = []
    for row in table:
        if row and _parse_date(row[0]):
            break
        if any(c.strip().lower() == "total" for c in row):
            header = row
    out = []
    for row in table:
        if not row:
            continue
        d = _parse_date(row[0])
        if not d:
            continue
        cells = row[1:]
        names = header[1:] if header else [f"col{i}" for i in range(len(cells))]
        funds: Dict[str, Optional[float]] = {}
        total: Optional[float] = None
        for name, cell in zip(names, cells):
            val = _parse_flow(cell)
            if name.strip().lower() == "total":
                total = val
            elif name:
                funds[name.strip()] = val
        if not header and cells:
            total = _parse_flow(cells[-1])
        if total is None:
            # Day not fully reported yet: skip unless every fund is known
            known = [v for v in funds.values() if v is not None]
            if not known or len(known) < len(funds):
                continue
            total = sum(known)
        out.append({"date": d.strftime("%Y-%m-%d"), "total": total,
                    "funds": {k: v for k, v in funds.items() if v is not None}})
    out.sort(key=lambda r: r["date"])
    return out


def _streak(totals: List[float]) -> Dict[str, Any]:
    """Consecutive same-sign days counting back from the latest."""
    days = 0
    direction = None
    for v in reversed(totals):
        if v == 0:
            break
        sign = "inflow" if v > 0 else "outflow"
        if direction is None:
            direction = sign
        if sign != direction:
            break
        days += 1
    return {"direction": direction or "flat", "days": days}


def _post_json(url: str, body: Dict[str, Any], headers: Dict[str, str], timeout: float = 15) -> Any:
    """Minimal POST helper (desk_common only does GET). Raises HttpError."""
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise HttpError(f"network error: {e.__class__.__name__}: {e}", None, url)
    if resp.status_code != 200:
        raise HttpError(f"HTTP {resp.status_code} from {url}", resp.status_code, url)
    try:
        return resp.json()
    except ValueError as e:
        raise HttpError(f"invalid JSON from {url}: {e}", 200, url)


def _sosovalue_rows(asset: str) -> List[Dict[str, Any]]:
    """SoSoValue daily totals -> [{date, total (US$m)}] oldest->newest."""
    headers = {"Content-Type": "application/json"}
    key = get_key("SOSOVALUE")
    if key:
        headers["x-soso-api-key"] = key
    data = _post_json(SOSOVALUE_URL, {"type": SOSOVALUE_TYPES[asset]}, headers)
    if isinstance(data, dict) and data.get("code") not in (0, "0", None):
        raise HttpError(f"SoSoValue error code {data.get('code')}: {data.get('msg')}", 200, SOSOVALUE_URL)
    rows = data.get("data") if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = rows.get("list") or rows.get("data") or []
    out = []
    for r in rows or []:
        try:
            out.append({"date": str(r["date"])[:10], "total": float(r["totalNetInflow"]) / 1e6, "funds": {}})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda r: r["date"])
    return out


def _summarize_etf(rows: List[Dict[str, Any]], days: int) -> Dict[str, Any]:
    window = rows[-days:]
    totals = [r["total"] for r in rows]
    latest = window[-1]
    sum5 = sum(totals[-5:])
    sum10 = sum(totals[-10:])
    streak = _streak(totals)
    fund_sums: Dict[str, float] = {}
    for r in window:
        for k, v in r["funds"].items():
            fund_sums[k] = fund_sums.get(k, 0.0) + v
    ranked = sorted(fund_sums.items(), key=lambda kv: kv[1], reverse=True)
    latest_funds = sorted(((k, v) for k, v in latest["funds"].items() if v), key=lambda kv: abs(kv[1]), reverse=True)
    return {
        "latest": {"date": latest["date"], "net_flow_usd_m": _r(latest["total"], 1),
                   "by_fund_usd_m": {k: _r(v, 1) for k, v in latest_funds[:8]}},
        "daily_net_usd_m": [{"date": r["date"], "net": _r(r["total"], 1)} for r in window],
        "sum_5d_usd_m": _r(sum5, 1),
        "sum_10d_usd_m": _r(sum10, 1),
        "streak": streak,
        "window_top_inflows_usd_m": {k: _r(v, 1) for k, v in ranked[:3] if v > 0},
        "window_top_outflows_usd_m": {k: _r(v, 1) for k, v in reversed(ranked[-3:]) if v < 0},
        "_sum5": sum5,
    }


@mcp.tool()
def get_etf_flows(asset: str = "BTC", days: int = 10) -> Dict[str, Any]:
    """US spot ETF net flows (US$ millions) for BTC, ETH or SOL.

    Returns latest day net flow + per-fund breakdown (IBIT, FBTC, GBTC...), daily net for the
    last `days`, 5d/10d sums and the current inflow/outflow streak. Read: sustained inflows
    (5d > +$500m for BTC) = institutional spot demand; multi-day outflow streaks = de-risking.
    Source: Farside (primary) then SoSoValue totals (secondary, key optional).
    """
    try:
        sym = base_symbol(asset)
        if sym not in FARSIDE_URLS:
            return {"status": "error", "error": f"Unsupported asset '{asset}'. Use BTC, ETH or SOL."}
        days = max(1, min(int(days), 30))
        errors: List[str] = []
        rows: List[Dict[str, Any]] = []
        source = None
        try:
            html = http_get(FARSIDE_URLS[sym], headers=BROWSER_HEADERS, ttl=1800, retries=1)
            rows = parse_farside_html(html)
            source = "farside"
            if not rows:
                errors.append("farside: no flow table parsed")
        except HttpError as e:
            errors.append(f"farside: {_short(e)}")
        if not rows:
            try:
                rows = _sosovalue_rows(sym)
                source = "sosovalue"
                if not rows:
                    errors.append("sosovalue: empty")
            except HttpError as e:
                errors.append(f"sosovalue: {_short(e)}")
        if not rows:
            reason = "; ".join(errors)
            return unavailable("farside/sosovalue", f"ETF flow sources failed: {reason}",
                               hint="Allow farside.co.uk in the network settings, or set DESK_SOSOVALUE_KEY "
                                    "(free key, header x-soso-api-key) for SoSoValue totals.")
        s = _summarize_etf(rows, days)
        sum5 = s.pop("_sum5")
        streak = s["streak"]
        strong = {"BTC": 500, "ETH": 150, "SOL": 30}[sym]
        if sum5 > strong:
            read = "strong institutional inflows (bullish spot demand)"
        elif sum5 > 0:
            read = "mild net inflows"
        elif sum5 > -strong:
            read = "mild net outflows"
        else:
            read = "heavy outflows (institutional de-risking, bearish)"
        return {
            "status": "success", "source": source, "as_of": _now_iso(), "asset": sym, "unit": "USD millions",
            **s,
            "errors": errors or None,
            "interpretation": f"{sym} ETFs 5d net {sum5:+.0f}m: {read}; {streak['days']}-day {streak['direction']} streak.",
        }
    except HttpError as e:
        return {"status": "error", "error": str(e), "source": "farside"}
    except Exception as e:
        logger.exception("get_etf_flows failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


# ---------------------------------------------------------------------------
# Premiums
# ---------------------------------------------------------------------------

def _global_usdt_price(sym: str) -> Tuple[float, str]:
    price, venue, _ = fetch_with_fallback(lambda ex: ex.fetch_ticker(f"{sym}/USDT")["last"],
                                          exchanges=GLOBAL_SPOT_VENUES)
    return float(price), venue


def _usdt_usd() -> Tuple[float, str]:
    """USDT/USD from Kraken; 1.0 if unavailable."""
    try:
        t = get_exchange("kraken").fetch_ticker("USDT/USD")
        px = t.get("last") or t.get("close")
        if px:
            return float(px), "kraken"
    except Exception as e:  # ccxt raises many types
        logger.warning("USDT/USD fetch failed: %s", e)
    return 1.0, "assumed_peg"


def _coinbase_usd(sym: str) -> Tuple[float, str]:
    try:
        data = http_get_json(COINBASE_TICKER.format(product=f"{sym}-USD"), ttl=20, retries=1)
        return float(data["price"]), "coinbase-exchange-api"
    except (HttpError, KeyError, TypeError, ValueError) as first:
        try:
            t = get_exchange("coinbase").fetch_ticker(f"{sym}/USD")
            return float(t["last"]), "ccxt-coinbase"
        except Exception as e:
            raise HttpError(f"Coinbase unreachable (api: {_short(first)}; ccxt: {e.__class__.__name__}: {_short(e, 100)})")


@mcp.tool()
def get_coinbase_premium(symbol: str = "BTC") -> Dict[str, Any]:
    """Coinbase premium: Coinbase SYM-USD vs global SYM/USDT (OKX) converted to USD via Kraken USDT/USD.

    Positive premium = US buyers (incl. ETF/institutional) paying up -> bullish US spot demand.
    Negative = US selling pressure. |premium| < 0.05% is noise.
    """
    try:
        sym = base_symbol(symbol)
        try:
            cb, cb_src = _coinbase_usd(sym)
        except HttpError as e:
            return unavailable("coinbase", str(e), hint="Allow api.exchange.coinbase.com (or api.coinbase.com) in the network settings.")
        glob_usdt, venue = _global_usdt_price(sym)
        usdt_usd, usdt_src = _usdt_usd()
        glob_usd = glob_usdt * usdt_usd
        prem = (cb / glob_usd - 1) * 100
        if prem > 0.1:
            read = "US spot demand strong (bullish)"
        elif prem > 0.05:
            read = "slight US bid"
        elif prem < -0.1:
            read = "US selling / weak US demand (bearish)"
        elif prem < -0.05:
            read = "slight US discount"
        else:
            read = "neutral"
        return {
            "status": "success", "source": f"{cb_src} vs {venue}", "as_of": _now_iso(), "symbol": sym,
            "coinbase_usd": _r(cb, 2), "global_usdt": _r(glob_usdt, 2), "global_venue": venue,
            "usdt_usd": _r(usdt_usd, 5), "usdt_usd_source": usdt_src, "global_usd": _r(glob_usd, 2),
            "premium_usd": _r(cb - glob_usd, 2), "premium_pct": _r(prem, 4),
            "interpretation": f"Coinbase premium {prem:+.3f}%: {read}.",
        }
    except HttpError as e:
        return {"status": "error", "error": str(e), "source": "coinbase"}
    except Exception as e:
        logger.exception("get_coinbase_premium failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


def _usd_krw() -> Tuple[float, str]:
    errors = []
    for name, url, params in FX_SOURCES:
        try:
            data = http_get_json(url, params=params, ttl=3600, retries=1)
            rate = (data.get("rates") or {}).get("KRW")
            if rate:
                return float(rate), name
            errors.append(f"{name}: no KRW rate")
        except HttpError as e:
            errors.append(f"{name}: {_short(e)}")
    raise HttpError("USD/KRW FX unavailable: " + " | ".join(errors))


@mcp.tool()
def get_kimchi_premium(symbol: str = "BTC") -> Dict[str, Any]:
    """Kimchi premium: Upbit KRW price vs global USD price (OKX USDT x Kraken USDT/USD) x USD/KRW FX.

    Also returns the Korean USDT premium (Upbit KRW-USDT vs FX). Read: > +3% = Korean retail froth
    (often late-stage euphoria); < 0% = Korean apathy/selling; 0-2% normal. FX: Frankfurter (ECB) or open.er-api.com.
    """
    try:
        sym = base_symbol(symbol)
        markets = f"KRW-{sym}" + ("" if sym == "USDT" else ",KRW-USDT")
        try:
            data = http_get_json(UPBIT_TICKER, params={"markets": markets}, ttl=20, retries=1)
        except HttpError as e:
            return unavailable("upbit", _short(e), hint="Allow api.upbit.com in the network settings.")
        prices = {d.get("market"): float(d["trade_price"]) for d in data if d.get("trade_price")}
        krw = prices.get(f"KRW-{sym}")
        if not krw:
            return {"status": "error", "error": f"Upbit has no KRW-{sym} market", "source": "upbit"}
        fx, fx_src = _usd_krw()
        glob_usdt, venue = _global_usdt_price(sym)
        usdt_usd, _ = _usdt_usd()
        glob_usd = glob_usdt * usdt_usd
        upbit_usd = krw / fx
        prem = (upbit_usd / glob_usd - 1) * 100
        usdt_prem = None
        if prices.get("KRW-USDT"):
            usdt_prem = (prices["KRW-USDT"] / fx / usdt_usd - 1) * 100
        if prem > 5:
            read = "extreme Korean retail froth (contrarian warning)"
        elif prem > 3:
            read = "elevated Korean retail demand (froth building)"
        elif prem >= 0:
            read = "normal"
        else:
            read = "discount: Korean retail apathy/selling"
        return {
            "status": "success", "source": f"upbit vs {venue}", "as_of": _now_iso(), "symbol": sym,
            "upbit_krw": _r(krw, 0), "usd_krw": _r(fx, 2), "fx_source": fx_src,
            "upbit_usd": _r(upbit_usd, 2), "global_usd": _r(glob_usd, 2),
            "kimchi_premium_pct": _r(prem, 3), "usdt_krw_premium_pct": _r(usdt_prem, 3),
            "interpretation": f"Kimchi premium {prem:+.2f}%: {read}."
                              + (f" USDT premium {usdt_prem:+.2f}% (capital-control/FX demand baseline)." if usdt_prem is not None else ""),
        }
    except HttpError as e:
        return {"status": "error", "error": str(e), "source": "upbit/fx"}
    except Exception as e:
        logger.exception("get_kimchi_premium failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


# ---------------------------------------------------------------------------
# Stablecoins
# ---------------------------------------------------------------------------

def _usd(d: Any) -> float:
    if isinstance(d, dict):
        return float(d.get("peggedUSD") or 0.0)
    return 0.0


def _chart_change(series: List[float], n: int) -> Tuple[Optional[float], Optional[float]]:
    if len(series) <= n:
        return None, None
    now, then = series[-1], series[-1 - n]
    if not then:
        return now - then, None
    return now - then, (now / then - 1) * 100


@mcp.tool()
def get_stablecoin_flows(days: int = 30) -> Dict[str, Any]:
    """Stablecoin supply ("dry powder") from DefiLlama: total USD-pegged supply, 7d/30d/`days` change,
    USDT vs USDC mint/burn, and the top-5 chains gaining/losing stablecoins over 7d.

    Read: expanding supply (+1-2%/30d) = fresh capital waiting to buy crypto (bullish liquidity);
    contracting supply = capital leaving the ecosystem. Chain gains show where liquidity rotates.
    """
    try:
        days = max(1, min(int(days), 365))
        chart = http_get_json(STABLE_CHART_URL, ttl=3600)
        listing = http_get_json(STABLE_LIST_URL, params={"includePrices": "true"}, ttl=3600)
        series = [_usd(p.get("totalCirculatingUSD") or p.get("totalCirculating")) for p in chart or []]
        series = [v for v in series if v > 0]
        if not series:
            return {"status": "error", "error": "empty stablecoin chart", "source": "defillama"}
        total = series[-1]
        ch7, pct7 = _chart_change(series, 7)
        ch30, pct30 = _chart_change(series, 30)
        chn, pctn = _chart_change(series, days)

        assets = [a for a in (listing or {}).get("peggedAssets", []) if a.get("pegType") == "peggedUSD"]
        issuers = {}
        for sym in ("USDT", "USDC"):
            a = next((x for x in assets if x.get("symbol") == sym), None)
            if not a:
                continue
            now = _usd(a.get("circulating"))
            w, m = _usd(a.get("circulatingPrevWeek")), _usd(a.get("circulatingPrevMonth"))
            issuers[sym] = {
                "supply_b": _r(now / 1e9, 2),
                "net_mint_7d_m": _r((now - w) / 1e6, 0) if w else None,
                "net_mint_30d_m": _r((now - m) / 1e6, 0) if m else None,
            }
        chains: Dict[str, List[float]] = {}
        for a in assets:
            for chain, c in (a.get("chainCirculating") or {}).items():
                acc = chains.setdefault(chain, [0.0, 0.0, 0.0])
                acc[0] += _usd(c.get("current"))
                acc[1] += _usd(c.get("circulatingPrevWeek"))
                acc[2] += _usd(c.get("circulatingPrevMonth"))
        deltas = [(name, v[0] - v[1], v[0] - v[2], v[0]) for name, v in chains.items() if v[0] or v[1]]
        deltas.sort(key=lambda t: t[1], reverse=True)

        def _fmt(t):
            return {"chain": t[0], "change_7d_m": _r(t[1] / 1e6, 0), "change_30d_m": _r(t[2] / 1e6, 0),
                    "supply_b": _r(t[3] / 1e9, 2)}

        gainers = [_fmt(t) for t in deltas[:5] if t[1] > 0]
        losers = [_fmt(t) for t in reversed(deltas[-5:]) if t[1] < 0]
        p30 = pct30 if pct30 is not None else 0.0
        if p30 > 2:
            read = "strongly expanding dry powder (bullish liquidity backdrop)"
        elif p30 > 0.5:
            read = "expanding dry powder (supportive)"
        elif p30 > -0.5:
            read = "flat supply (neutral liquidity)"
        else:
            read = "contracting supply (capital leaving, bearish liquidity)"
        mint_bits = [f"{k} {v['net_mint_30d_m']:+,.0f}m/30d" for k, v in issuers.items() if v.get("net_mint_30d_m") is not None]
        return {
            "status": "success", "source": "defillama", "as_of": _now_iso(),
            "total_supply_b": _r(total / 1e9, 2),
            "change_7d_b": _r(ch7 / 1e9, 2) if ch7 is not None else None, "change_7d_pct": _r(pct7, 2),
            "change_30d_b": _r(ch30 / 1e9, 2) if ch30 is not None else None, "change_30d_pct": _r(pct30, 2),
            f"change_{days}d_b": _r(chn / 1e9, 2) if chn is not None else None, f"change_{days}d_pct": _r(pctn, 2),
            "issuers": issuers,
            "top_chain_inflows_7d": gainers,
            "top_chain_outflows_7d": losers,
            "interpretation": f"Stablecoin supply ${total / 1e9:.1f}B, 30d {p30:+.2f}%: {read}. " + ", ".join(mint_bits),
        }
    except HttpError as e:
        return {"status": "error", "error": str(e), "source": "defillama"}
    except Exception as e:
        logger.exception("get_stablecoin_flows failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


if __name__ == "__main__":
    mcp.run()
