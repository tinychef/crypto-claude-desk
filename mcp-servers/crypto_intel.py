#!/usr/bin/env python3
"""Crypto Intel — news headlines, news sentiment votes, social pulse and token unlocks.

Data sources:
- RSS: CoinDesk, Cointelegraph, Decrypt, The Block (free) — parsed with xml.etree
- CryptoPanic API (free key, query param ``auth_token``; DESK_CRYPTOPANIC_KEY) — news + votes
- Reddit public JSON (free) — subreddit hot posts; LunarCrush v4 (paid, Bearer) optional
- Tokenomist (paid, header ``x-api-key``) / DefiLlama emissions (Pro key in path) — token unlocks
"""
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from desk_common import HttpError, base_symbol, get_key, http_get, http_get_json, tool_serializer, unavailable

logger = logging.getLogger(__name__)
mcp = FastMCP("crypto-intel", tool_serializer=tool_serializer)

RSS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
}
CRYPTOPANIC_URLS = [
    "https://cryptopanic.com/api/developer/v2/posts/",
    "https://cryptopanic.com/api/v1/posts/",
]
REDDIT_HOSTS = ["https://www.reddit.com", "https://old.reddit.com"]
LUNARCRUSH_URL = "https://lunarcrush.com/api4/public/coins/{symbol}/v1"
TOKENOMIST_BASE = "https://api.tokenomist.ai"
LLAMA_EMISSIONS_FREE = "https://api.llama.fi/emissions"
LLAMA_EMISSIONS_PRO = "https://pro-api.llama.fi/{key}/api/emissions"

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
REDDIT_UA = "crypto-desk/2.0 (research bot; read-only)"

SYMBOL_NAMES = {
    "BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "eth", "ether"], "SOL": ["solana", "sol"],
    "XRP": ["xrp", "ripple"], "BNB": ["bnb", "binance coin"], "DOGE": ["dogecoin", "doge"],
    "ADA": ["cardano", "ada"], "AVAX": ["avalanche", "avax"], "LINK": ["chainlink", "link"],
    "DOT": ["polkadot", "dot"], "TON": ["toncoin", "ton"], "SUI": ["sui"], "ARB": ["arbitrum", "arb"],
    "OP": ["optimism"], "APT": ["aptos", "apt"], "TIA": ["celestia", "tia"], "HYPE": ["hyperliquid", "hype"],
    "LTC": ["litecoin", "ltc"], "TRX": ["tron", "trx"], "NEAR": ["near protocol", "near"],
}
SUBREDDITS = {
    "BTC": ["Bitcoin", "CryptoCurrency"],
    "ETH": ["ethereum", "ethtrader", "CryptoCurrency"],
    "SOL": ["solana", "CryptoCurrency"],
}
DEFAULT_SUBREDDITS = ["CryptoCurrency", "CryptoMarkets"]
DEDICATED_SUBS = {"BTC": {"bitcoin"}, "ETH": {"ethereum", "ethtrader"}, "SOL": {"solana"}}

BULLISH_WORDS = {
    "bull", "bullish", "moon", "mooning", "pump", "pumping", "rally", "rallies", "breakout", "ath", "surge",
    "surges", "soar", "soars", "buy", "buying", "bought", "accumulate", "accumulating", "long", "green",
    "rocket", "higher", "uptrend", "inflow", "inflows", "adoption", "approve", "approved", "approval", "record",
    "gain", "gains", "recover", "recovery", "rebound", "hodl", "undervalued", "send",
}
BEARISH_WORDS = {
    "bear", "bearish", "dump", "dumping", "crash", "crashing", "plunge", "plunges", "sell", "selling", "sold",
    "short", "red", "rekt", "liquidated", "liquidation", "liquidations", "fear", "panic", "lower", "downtrend",
    "outflow", "outflows", "hack", "hacked", "exploit", "scam", "ban", "lawsuit", "sec", "fud", "capitulation",
    "collapse", "drop", "drops", "loss", "losses", "overvalued", "bubble", "top", "dead",
}

DEFILLAMA_GECKO = {
    "ARB": "arbitrum", "OP": "optimism", "SUI": "sui", "APT": "aptos", "TIA": "celestia", "SOL": "solana",
    "AVAX": "avalanche-2", "STRK": "starknet", "ZK": "zksync", "JUP": "jupiter-exchange-solana",
    "W": "wormhole", "PYTH": "pyth-network", "ENA": "ethena", "SEI": "sei-network", "WLD": "worldcoin-wld",
    "DYDX": "dydx-chain", "IMX": "immutable-x", "ONDO": "ondo-finance", "EIGEN": "eigenlayer", "HYPE": "hyperliquid",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().replace(microsecond=0).isoformat()


def _short(e: Exception, n: int = 140) -> str:
    msg = str(e)
    m = re.search(r"host='([^']+)'", msg)
    if "ProxyError" in msg or "Tunnel connection failed" in msg:
        return f"network blocked ({m.group(1) if m else 'host'} not reachable via proxy)"
    return msg[:n]


def _r(x: Optional[float], n: int = 2) -> Optional[float]:
    return None if x is None else round(float(x), n)


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(el: ET.Element, *names: str) -> str:
    for child in el:
        if _local(child.tag) in names:
            if _local(child.tag) == "link" and child.get("href"):
                return child.get("href", "")
            return (child.text or "").strip()
    return ""


def _parse_dt(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def parse_feed(xml_text: str, source: str) -> List[Dict[str, Any]]:
    """Parse RSS 2.0 or Atom into [{title, source, published (datetime), link, summary}]."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    out = []
    for it in items:
        title = _strip_html(_child_text(it, "title"))
        if not title:
            continue
        out.append({
            "title": title,
            "source": source,
            "published": _parse_dt(_child_text(it, "pubDate", "published", "updated", "date")),
            "link": _child_text(it, "link", "guid"),
            "summary": _strip_html(_child_text(it, "description", "summary"))[:400],
        })
    return out


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", title.lower())).strip()


def _similar(a: str, b: str) -> bool:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.8


def _keywords(query: Optional[str]) -> List[str]:
    if not query:
        return []
    words = [w.strip().lower() for w in re.split(r"[,\s]+", query) if w.strip()]
    out: List[str] = []
    for w in words:
        out.append(w)
        out.extend(SYMBOL_NAMES.get(w.upper(), []))
    seen = set()
    return [w for w in out if not (w in seen or seen.add(w))]


def _matches(text: str, kws: List[str]) -> List[str]:
    low = text.lower()
    return [k for k in kws if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", low)]


@mcp.tool()
def get_headlines(query: Optional[str] = None, hours: int = 24, limit: int = 15) -> Dict[str, Any]:
    """Latest crypto headlines merged from CoinDesk, Cointelegraph, Decrypt and The Block RSS,
    deduplicated, newest first. `query` = keywords/symbols (e.g. "BTC ETF"; symbols expand to names).

    Each item: title, source, published (ISO UTC), link, matched keywords. Use it to spot catalysts;
    stories covered by several outlets (`also_in`) are the ones the market is actually reading.
    """
    try:
        hours = max(1, min(int(hours), 168))
        limit = max(1, min(int(limit), 50))
        kws = _keywords(query)
        cutoff = _now() - timedelta(hours=hours)
        items: List[Dict[str, Any]] = []
        failed = []
        for source, url in RSS_FEEDS.items():
            try:
                body = http_get(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/rss+xml, application/xml, text/xml"},
                                ttl=300, retries=1)
                items.extend(parse_feed(body, source))
            except (HttpError, ET.ParseError) as e:
                failed.append(f"{source}: {_short(e)}")
        if not items and failed:
            return unavailable("rss", "; ".join(failed),
                               hint="Allow coindesk.com, cointelegraph.com, decrypt.co, theblock.co in the network settings.")
        items.sort(key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        kept: List[Dict[str, Any]] = []
        norms: List[str] = []
        for it in items:
            if it["published"] and it["published"] < cutoff:
                continue
            matched = _matches(f"{it['title']} {it['summary']}", kws) if kws else []
            if kws and not matched:
                continue
            n = _norm_title(it["title"])
            dup = next((i for i, prev in enumerate(norms) if prev == n or _similar(prev, n)), None)
            if dup is not None:
                also = kept[dup].setdefault("also_in", [])
                if it["source"] != kept[dup]["source"] and it["source"] not in also:
                    also.append(it["source"])
                continue
            norms.append(n)
            kept.append({
                "title": it["title"], "source": it["source"],
                "published": it["published"].replace(microsecond=0).isoformat() if it["published"] else None,
                "link": it["link"], "matched": matched or None,
            })
        result = kept[:limit]
        by_source: Dict[str, int] = {}
        for it in result:
            by_source[it["source"]] = by_source.get(it["source"], 0) + 1
        return {
            "status": "success", "source": "rss", "as_of": _now_iso(), "query": query, "hours": hours,
            "count": len(result), "total_matching": len(kept), "by_source": by_source,
            "feeds_failed": failed or None, "headlines": result,
            "interpretation": (f"{len(kept)} unique headlines in last {hours}h"
                               + (f" matching '{query}'" if query else "")
                               + (f"; {len(failed)} feed(s) failed" if failed else "") + "."),
        }
    except Exception as e:
        logger.exception("get_headlines failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


# ---------------------------------------------------------------------------
# CryptoPanic
# ---------------------------------------------------------------------------

@mcp.tool()
def get_crypto_panic(currencies: str = "BTC", filter: str = "important") -> Dict[str, Any]:
    """CryptoPanic aggregated news with community votes (free key: DESK_CRYPTOPANIC_KEY).

    `filter`: rising | hot | bullish | bearish | important | saved | lol. Returns posts with
    bullish/bearish vote counts and an overall bullish ratio (0-1; > 0.6 bullish crowd, < 0.4 bearish).
    """
    key = get_key("CRYPTOPANIC")
    if not key:
        return unavailable("cryptopanic", "missing API key (query-param auth, cannot be proxy-injected)",
                           hint="Get a free token at cryptopanic.com/developers/api and set DESK_CRYPTOPANIC_KEY.")
    try:
        params = {"auth_token": key, "currencies": ",".join(base_symbol(c) for c in currencies.split(",") if c.strip()),
                  "public": "true", "kind": "news"}
        if filter:
            params["filter"] = filter
        data = None
        errors = []
        for url in CRYPTOPANIC_URLS:
            try:
                data = http_get_json(url, params=params, ttl=300, retries=1)
                break
            except HttpError as e:
                errors.append(_short(e).replace(key, "***"))
        if data is None:
            return {"status": "error", "error": "; ".join(errors), "source": "cryptopanic"}
        posts = []
        bull = bear = 0
        for p in (data.get("results") or [])[:20]:
            v = p.get("votes") or {}
            pb = int(v.get("positive", 0) or 0) + int(v.get("liked", 0) or 0)
            pn = int(v.get("negative", 0) or 0) + int(v.get("disliked", 0) or 0) + int(v.get("toxic", 0) or 0)
            bull += pb
            bear += pn
            src = p.get("source") or {}
            posts.append({
                "title": p.get("title"), "published": p.get("published_at"),
                "source": src.get("title") or src.get("domain") or p.get("domain"),
                "currencies": [c.get("code") for c in (p.get("currencies") or []) if c.get("code")] or None,
                "bullish_votes": pb, "bearish_votes": pn, "important": v.get("important"),
                "url": p.get("url") or (f"https://cryptopanic.com/news/{p['slug']}" if p.get("slug") else None),
            })
        ratio = bull / (bull + bear) if (bull + bear) else None
        if ratio is None:
            read = "no votes yet"
        elif ratio > 0.6:
            read = "bullish crowd"
        elif ratio < 0.4:
            read = "bearish crowd"
        else:
            read = "mixed"
        return {
            "status": "success", "source": "cryptopanic", "as_of": _now_iso(), "currencies": params["currencies"],
            "filter": filter, "posts": posts[:15], "bullish_votes": bull, "bearish_votes": bear,
            "bullish_ratio": _r(ratio, 2),
            "interpretation": f"{len(posts)} posts, votes {bull} bull / {bear} bear: {read}.",
        }
    except Exception as e:
        logger.exception("get_crypto_panic failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {str(e).replace(key, '***')}"}


# ---------------------------------------------------------------------------
# Social
# ---------------------------------------------------------------------------

def _reddit_hot(sub: str) -> List[Dict[str, Any]]:
    last: Optional[HttpError] = None
    for host in REDDIT_HOSTS:
        try:
            data = http_get_json(f"{host}/r/{sub}/hot.json", params={"limit": 50, "raw_json": 1},
                                 headers={"User-Agent": REDDIT_UA}, ttl=600, retries=1)
            return [c.get("data", {}) for c in (data.get("data") or {}).get("children", [])]
        except HttpError as e:
            last = e
    raise last or HttpError("reddit: no data")


def _lexicon_score(texts: List[str]) -> Dict[str, Any]:
    bull = bear = 0
    for t in texts:
        words = re.findall(r"[a-z]+", t.lower())
        bull += sum(1 for w in words if w in BULLISH_WORDS)
        bear += sum(1 for w in words if w in BEARISH_WORDS)
    score = (bull - bear) / (bull + bear) if (bull + bear) else 0.0
    return {"bullish_hits": bull, "bearish_hits": bear, "score": round(score, 2)}


def _lunarcrush(sym: str) -> Optional[Dict[str, Any]]:
    key = get_key("LUNARCRUSH")
    if not key:
        return None
    data = http_get_json(LUNARCRUSH_URL.format(symbol=sym), headers={"Authorization": f"Bearer {key}"},
                         ttl=900, retries=1)
    d = (data or {}).get("data") or {}
    return {k: d.get(k) for k in ("galaxy_score", "alt_rank", "social_dominance", "sentiment",
                                  "interactions_24h", "social_volume_24h") if d.get(k) is not None}


@mcp.tool()
def get_social_pulse(symbol: str = "BTC") -> Dict[str, Any]:
    """Retail social pulse from Reddit hot posts (r/Bitcoin, r/ethereum, r/solana, r/CryptoCurrency...):
    mentions, avg upvote ratio, top titles and a lexicon sentiment score (-1 bearish .. +1 bullish).
    Adds LunarCrush galaxy score / social dominance when DESK_LUNARCRUSH_KEY is set.

    Read contrarian at extremes: score > +0.5 with high engagement = euphoria; < -0.5 = capitulation/fear.
    """
    try:
        sym = base_symbol(symbol)
        subs = SUBREDDITS.get(sym, DEFAULT_SUBREDDITS)
        names = SYMBOL_NAMES.get(sym, [sym.lower()])
        posts: List[Dict[str, Any]] = []
        failed = []
        for sub in subs:
            try:
                for p in _reddit_hot(sub):
                    if p.get("stickied"):
                        continue
                    p["_sub"] = sub
                    posts.append(p)
            except HttpError as e:
                failed.append(f"r/{sub}: {_short(e)}")
        lunar = None
        lunar_err = None
        try:
            lunar = _lunarcrush(sym)
        except HttpError as e:
            lunar_err = _short(e)
        if not posts and not lunar:
            return unavailable("reddit", "; ".join(failed) or "no posts",
                               hint="Allow www.reddit.com in the network settings; optional DESK_LUNARCRUSH_KEY.")
        dedicated = DEDICATED_SUBS.get(sym, set())
        relevant = [p for p in posts
                    if p["_sub"].lower() in dedicated
                    or _matches(f"{p.get('title', '')} {(p.get('selftext') or '')[:500]}", names)]
        ratios = [float(p["upvote_ratio"]) for p in relevant if p.get("upvote_ratio") is not None]
        comments = [int(p.get("num_comments") or 0) for p in relevant]
        top = sorted(relevant, key=lambda p: p.get("score") or p.get("ups") or 0, reverse=True)[:5]
        senti = _lexicon_score([p.get("title", "") for p in relevant])
        s = senti["score"]
        mood = ("euphoric" if s > 0.5 else "bullish" if s > 0.15 else "fearful" if s < -0.5
                else "bearish" if s < -0.15 else "neutral")
        out: Dict[str, Any] = {
            "status": "success", "source": "reddit" + ("+lunarcrush" if lunar else ""), "as_of": _now_iso(),
            "symbol": sym, "subreddits": subs, "posts_analyzed": len(posts), "mentions": len(relevant),
            "avg_upvote_ratio": _r(sum(ratios) / len(ratios), 2) if ratios else None,
            "avg_comments": _r(sum(comments) / len(comments), 0) if comments else None,
            "sentiment": senti,
            "top_posts": [{"title": p.get("title"), "sub": p["_sub"], "score": p.get("score"),
                           "comments": p.get("num_comments")} for p in top],
            "lunarcrush": lunar,
            "errors": (failed + ([f"lunarcrush: {lunar_err}"] if lunar_err else [])) or None,
        }
        lc = f"; LunarCrush galaxy {lunar.get('galaxy_score')}" if lunar and lunar.get("galaxy_score") is not None else ""
        out["interpretation"] = f"{len(relevant)} {sym} posts on Reddit hot, lexicon score {s:+.2f} ({mood}){lc}."
        return out
    except Exception as e:
        logger.exception("get_social_pulse failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


# ---------------------------------------------------------------------------
# Token unlocks
# ---------------------------------------------------------------------------

def _first(d: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        cur: Any = d
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, ""):
            return cur
    return None


def _to_dt(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        ts = float(v) / (1000 if v > 1e11 else 1)
        return datetime.fromtimestamp(ts, timezone.utc)
    return _parse_dt(str(v)) if not re.fullmatch(r"\d+(\.\d+)?", str(v)) else _to_dt(float(v))


def _tokenomist_unlocks(sym: str, days_ahead: int) -> List[Dict[str, Any]]:
    """Tokenomist v4 (header x-api-key). Needs a symbol (events are per token)."""
    headers = {}
    key = get_key("TOKENOMIST")
    if key:
        headers["x-api-key"] = key
    tokens = http_get_json(f"{TOKENOMIST_BASE}/v4/token/list", headers=headers, ttl=86400, retries=1)
    token_list = tokens.get("data") if isinstance(tokens, dict) else tokens
    match = next((t for t in token_list or [] if str(t.get("symbol", "")).upper() == sym), None)
    if not match:
        return []
    token_id = match.get("id") or match.get("tokenId")
    start = _now().strftime("%Y-%m-%d")
    end = (_now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    ev = http_get_json(f"{TOKENOMIST_BASE}/v4/unlock/events", params={"tokenId": token_id, "start": start, "end": end},
                       headers=headers, ttl=3600, retries=1)
    events = ev.get("data") if isinstance(ev, dict) else ev
    circ = _first(match, "circulatingSupply", "circSupply")
    price = _first(match, "price", "tokenPrice")
    out = []
    for e in events or []:
        dt = _to_dt(_first(e, "unlockDate", "date", "timestamp"))
        amount = _first(e, "cliffUnlocks.cliffAmount", "unlockAmount", "amount", "tokens")
        value = _first(e, "cliffUnlocks.cliffValue", "unlockValue", "valueUSD", "value")
        pct = _first(e, "cliffUnlocks.percentOfCirculatingSupply", "percentOfCirculating", "unlockPercentage")
        amount_f = float(amount) if amount is not None else None
        if pct is None and amount_f and circ:
            pct = amount_f / float(circ) * 100
        if value is None and amount_f and price:
            value = amount_f * float(price)
        out.append({"symbol": sym, "name": match.get("name"), "date": dt, "tokens": amount_f,
                    "usd_value": float(value) if value is not None else None,
                    "pct_circulating": float(pct) if pct is not None else None,
                    "category": _first(e, "allocationName", "category", "unlockType")})
    return out


def _llama_unlocks(sym: Optional[str], days_ahead: int) -> List[Dict[str, Any]]:
    """DefiLlama emissions (free endpoint is Pro-only as of 2026-11: HTTP 402)."""
    try:
        data = http_get_json(LLAMA_EMISSIONS_FREE, ttl=3600, retries=1)
    except HttpError:
        key = get_key("DEFILLAMA")
        if not key:
            raise
        data = http_get_json(LLAMA_EMISSIONS_PRO.format(key=key), ttl=3600, retries=1)
    now = _now()
    horizon = now + timedelta(days=days_ahead)
    gecko = DEFILLAMA_GECKO.get(sym or "", "")
    out = []
    for p in data or []:
        psym = str(p.get("symbol") or "").upper()
        token = str(p.get("token") or "")
        gid = str(p.get("gecko_id") or (token.split(":", 1)[1] if token.startswith("coingecko:") else ""))
        name = p.get("name") or gid
        if sym and not (psym == sym or (gecko and gid == gecko) or str(name).upper() == sym):
            continue
        circ = _first(p, "circSupply", "circulatingSupply")
        price = _first(p, "tPrice", "price")
        for e in p.get("events") or []:
            dt = _to_dt(e.get("timestamp"))
            if not dt or dt < now or dt > horizon:
                continue
            n = e.get("noOfTokens")
            amount = sum(float(x) for x in n if x is not None) if isinstance(n, list) else (float(n) if n else None)
            out.append({
                "symbol": psym or sym or None, "name": name, "date": dt, "tokens": amount,
                "usd_value": amount * float(price) if amount and price else None,
                "pct_circulating": amount / float(circ) * 100 if amount and circ else None,
                "category": e.get("category") or e.get("unlockType"),
            })
    return out


@mcp.tool()
def get_token_unlocks(symbol: Optional[str] = None, days_ahead: int = 30) -> Dict[str, Any]:
    """Upcoming token unlocks within `days_ahead` days: date, tokens, USD value, % of circulating supply.

    Sources: Tokenomist (paid, x-api-key; needs a symbol) then DefiLlama emissions (Pro key
    DESK_DEFILLAMA_KEY; the free endpoint now returns 402). Read: unlocks > 2% of circulating
    supply (or > $50m) are material sell-pressure events — avoid longs into them.
    """
    try:
        sym = base_symbol(symbol) if symbol else None
        days_ahead = max(1, min(int(days_ahead), 180))
        errors = []
        rows: List[Dict[str, Any]] = []
        source = None
        if sym:
            try:
                rows = _tokenomist_unlocks(sym, days_ahead)
                source = "tokenomist"
            except HttpError as e:
                errors.append(f"tokenomist: {_short(e)}")
        if not source:
            try:
                rows = _llama_unlocks(sym, days_ahead)
                source = "defillama-emissions"
            except HttpError as e:
                errors.append(f"defillama: {_short(e)}")
        if not source:
            return unavailable("tokenomist/defillama", "; ".join(errors),
                               hint="Token unlocks need a paid key: Tokenomist API (DESK_TOKENOMIST_KEY, ~$100+/mo) "
                                    "or DefiLlama Pro (DESK_DEFILLAMA_KEY, ~$300/mo).")
        rows = [r for r in rows if r["date"] is None or r["date"] >= _now() - timedelta(days=1)]
        rows.sort(key=lambda r: (-(r["pct_circulating"] or 0), r["date"] or _now()) if not sym
                  else (r["date"] or _now()))
        rows = rows[:15]
        material = [r for r in rows if (r["pct_circulating"] or 0) >= 2 or (r["usd_value"] or 0) >= 50e6]
        unlocks = [{
            "symbol": r["symbol"], "name": r["name"],
            "date": r["date"].strftime("%Y-%m-%d") if r["date"] else None,
            "tokens": _r(r["tokens"], 0), "usd_value_m": _r((r["usd_value"] or 0) / 1e6, 1) if r["usd_value"] else None,
            "pct_circulating": _r(r["pct_circulating"], 2), "category": r["category"],
            "material": r in material,
        } for r in rows]
        if not unlocks:
            interp = f"No unlocks found in the next {days_ahead}d" + (f" for {sym}" if sym else "") + "."
        else:
            interp = (f"{len(unlocks)} unlock(s) in next {days_ahead}d, {len(material)} material (>2% circ or >$50m)"
                      + (f"; next: {unlocks[0]['date']}" if sym else "") + ".")
        return {"status": "success", "source": source, "as_of": _now_iso(), "symbol": sym,
                "days_ahead": days_ahead, "unlocks": unlocks, "errors": errors or None, "interpretation": interp}
    except Exception as e:
        logger.exception("get_token_unlocks failed")
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}


if __name__ == "__main__":
    mcp.run()
