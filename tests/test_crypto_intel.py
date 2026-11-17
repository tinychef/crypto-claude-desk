"""Tests for crypto_intel.py (crypto-intel). Fully mocked — no network."""
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ["DESK_HTTP_CACHE"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers"))

import crypto_intel as ci  # noqa: E402
from desk_common import HttpError  # noqa: E402
from helpers import call_tool  # noqa: E402


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for p in ("CRYPTOPANIC", "LUNARCRUSH", "TOKENOMIST", "DEFILLAMA"):
        for k in (f"DESK_{p}_KEY", f"{p}_API_KEY", f"{p}_KEY"):
            monkeypatch.delenv(k, raising=False)


NOW = datetime.now(timezone.utc)


def _rfc(hours_ago):
    return format_datetime(NOW - timedelta(hours=hours_ago))


def _iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rss(items):
    body = "".join(
        f"<item><title><![CDATA[{t}]]></title><link>{l}</link><pubDate>{d}</pubDate>"
        f"<description><![CDATA[<p>{desc}</p>]]></description></item>"
        for t, l, d, desc in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'


ATOM = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Decrypt</title>
<entry><title>Solana DEX volume hits record</title><link href="https://decrypt.co/sol-dex"/>
<updated>{_iso(3)}</updated><summary>Solana activity surges.</summary></entry>
</feed>"""

FEEDS = {
    "coindesk": _rss([
        ("Bitcoin ETFs see $500M inflows as BTC rallies", "https://cd/1", _rfc(1), "Spot bitcoin funds."),
        ("Ether slips below $3,000", "https://cd/2", _rfc(5), "ETH weakness."),
        ("Old news about BTC", "https://cd/3", _rfc(60), "stale"),
    ]),
    "cointelegraph": _rss([
        ("Bitcoin ETFs See $500M Inflows as BTC Rallies!", "https://ct/1", _rfc(2), "dup story"),
        ("SEC delays decision on altcoin ETF", "https://ct/2", _rfc(4), "regulation"),
    ]),
    "decrypt": ATOM,
}


def _feed_router(fail=("theblock",)):
    def fake(url, params=None, headers=None, **kw):
        for f in fail:
            if f in url:
                raise HttpError("blocked", None, url)
        for k, v in FEEDS.items():
            if k in url:
                return v
        raise AssertionError(url)
    return fake


# ---------------------------------------------------------------------------
# Headlines
# ---------------------------------------------------------------------------

class TestHeadlines:
    def test_merge_dedupe_sort(self):
        with patch("crypto_intel.http_get", side_effect=_feed_router()):
            r = call_tool(ci.get_headlines, None, 24, 15)
        assert r["status"] == "success"
        titles = [h["title"] for h in r["headlines"]]
        assert titles[0].startswith("Bitcoin ETFs see $500M")
        assert len([t for t in titles if "500M" in t]) == 1           # deduplicated
        assert r["headlines"][0]["also_in"] == ["Cointelegraph"]
        assert "Old news about BTC" not in titles                       # outside 24h
        assert "Solana DEX volume hits record" in titles                # Atom parsed
        pubs = [h["published"] for h in r["headlines"]]
        assert pubs == sorted(pubs, reverse=True)
        assert r["feeds_failed"] and r["feeds_failed"][0].startswith("The Block")

    def test_query_filter_expands_symbol(self):
        with patch("crypto_intel.http_get", side_effect=_feed_router()):
            r = call_tool(ci.get_headlines, "BTC", 24, 15)
        assert r["count"] == 1
        assert "bitcoin" in r["headlines"][0]["matched"] or "btc" in r["headlines"][0]["matched"]

    def test_query_matches_description(self):
        with patch("crypto_intel.http_get", side_effect=_feed_router()):
            r = call_tool(ci.get_headlines, "regulation", 24, 15)
        assert [h["title"] for h in r["headlines"]] == ["SEC delays decision on altcoin ETF"]

    def test_hours_and_limit(self):
        with patch("crypto_intel.http_get", side_effect=_feed_router()):
            r = call_tool(ci.get_headlines, None, 72, 2)
        assert r["count"] == 2
        assert r["total_matching"] == 5

    def test_all_feeds_fail(self):
        with patch("crypto_intel.http_get", side_effect=HttpError("blocked", None, "u")):
            r = call_tool(ci.get_headlines)
        assert r["status"] == "unavailable"
        assert len(r["reason"].split(";")) == 4

    def test_malformed_feed_is_partial(self):
        def fake(url, **kw):
            return "<not-xml" if "coindesk" in url else _feed_router(fail=("theblock", "decrypt"))(url, **kw)
        with patch("crypto_intel.http_get", side_effect=fake):
            r = call_tool(ci.get_headlines)
        assert r["status"] == "success"
        assert any(f.startswith("CoinDesk") for f in r["feeds_failed"])

    def test_parse_dt(self):
        assert ci._parse_dt("Mon, 16 Nov 2026 10:00:00 +0000").hour == 10
        assert ci._parse_dt("2026-11-16T10:00:00Z").tzinfo is not None
        assert ci._parse_dt("garbage") is None


# ---------------------------------------------------------------------------
# CryptoPanic
# ---------------------------------------------------------------------------

PANIC = {"count": 2, "results": [
    {"title": "BTC breaks out", "published_at": "2026-11-16T10:00:00Z", "source": {"title": "CoinDesk"},
     "currencies": [{"code": "BTC"}], "url": "https://x/1",
     "votes": {"positive": 10, "negative": 2, "liked": 5, "disliked": 1, "important": 4, "toxic": 0}},
    {"title": "Exchange hacked", "published_at": "2026-11-16T09:00:00Z", "domain": "theblock.co",
     "currencies": [{"code": "BTC"}], "slug": "exchange-hacked",
     "votes": {"positive": 0, "negative": 6, "toxic": 1}},
]}


class TestCryptoPanic:
    def test_missing_key(self):
        r = call_tool(ci.get_crypto_panic, "BTC")
        assert r["status"] == "unavailable"
        assert "DESK_CRYPTOPANIC_KEY" in r["hint"]

    def test_success(self, monkeypatch):
        monkeypatch.setenv("DESK_CRYPTOPANIC_KEY", "secret-token")
        with patch("crypto_intel.http_get_json", return_value=PANIC) as m:
            r = call_tool(ci.get_crypto_panic, "btc,eth", "hot")
        assert r["status"] == "success"
        assert r["bullish_votes"] == 15
        assert r["bearish_votes"] == 10
        assert r["bullish_ratio"] == 0.6
        assert r["posts"][1]["source"] == "theblock.co"
        assert r["posts"][1]["url"].endswith("exchange-hacked")
        params = m.call_args.kwargs["params"]
        assert params["auth_token"] == "secret-token"
        assert params["currencies"] == "BTC,ETH" and params["filter"] == "hot"
        assert "v2" in m.call_args.args[0]

    def test_v1_fallback(self, monkeypatch):
        monkeypatch.setenv("DESK_CRYPTOPANIC_KEY", "k")
        with patch("crypto_intel.http_get_json", side_effect=[HttpError("HTTP 404", 404, "u"), PANIC]) as m:
            r = call_tool(ci.get_crypto_panic, "BTC")
        assert r["status"] == "success"
        assert "/api/v1/" in m.call_args.args[0]

    def test_error_masks_key(self, monkeypatch):
        monkeypatch.setenv("DESK_CRYPTOPANIC_KEY", "supersecret")
        err = HttpError("network error ...?auth_token=supersecret", None, "u")
        with patch("crypto_intel.http_get_json", side_effect=err):
            r = call_tool(ci.get_crypto_panic, "BTC")
        assert r["status"] == "error"
        assert "supersecret" not in r["error"]


# ---------------------------------------------------------------------------
# Social pulse
# ---------------------------------------------------------------------------

def _reddit(posts):
    return {"data": {"children": [{"data": p} for p in posts]}}


BTC_SUB = _reddit([
    {"title": "Daily discussion", "stickied": True, "score": 10, "upvote_ratio": 0.9, "num_comments": 999},
    {"title": "Bullish breakout, new ATH soon", "score": 500, "upvote_ratio": 0.95, "num_comments": 120},
    {"title": "Just bought more, HODL", "score": 300, "upvote_ratio": 0.9, "num_comments": 50},
    {"title": "Fear everywhere, is this a crash?", "score": 100, "upvote_ratio": 0.7, "num_comments": 80},
])
CC_SUB = _reddit([
    {"title": "Bitcoin rally continues", "score": 800, "upvote_ratio": 0.92, "num_comments": 200},
    {"title": "Solana memecoins dump", "score": 50, "upvote_ratio": 0.8, "num_comments": 10},
])


class TestSocialPulse:
    def _router(self, fail=()):
        def fake(url, params=None, headers=None, **kw):
            if "reddit" in url:
                assert headers and "User-Agent" in headers
            if any(f in url for f in fail):
                raise HttpError("blocked", None, url)
            if "/r/Bitcoin/" in url:
                return BTC_SUB
            if "/r/CryptoCurrency/" in url:
                return CC_SUB
            if "lunarcrush" in url:
                assert headers["Authorization"] == "Bearer lc"
                return {"data": {"galaxy_score": 71, "alt_rank": 3, "social_dominance": 25.4, "sentiment": 80}}
            raise AssertionError(url)
        return fake

    def test_success(self):
        with patch("crypto_intel.http_get_json", side_effect=self._router()):
            r = call_tool(ci.get_social_pulse, "BTC")
        assert r["status"] == "success"
        assert r["posts_analyzed"] == 5                    # stickied excluded
        assert r["mentions"] == 4                          # 3 r/Bitcoin + 1 r/CryptoCurrency mentioning bitcoin
        assert r["top_posts"][0]["title"] == "Bitcoin rally continues"
        assert r["sentiment"]["bullish_hits"] > r["sentiment"]["bearish_hits"]
        assert r["sentiment"]["score"] > 0
        assert r["lunarcrush"] is None
        assert r["avg_upvote_ratio"] == pytest.approx((0.95 + 0.9 + 0.7 + 0.92) / 4, abs=0.01)

    def test_lunarcrush_with_key(self, monkeypatch):
        monkeypatch.setenv("DESK_LUNARCRUSH_KEY", "lc")
        with patch("crypto_intel.http_get_json", side_effect=self._router()):
            r = call_tool(ci.get_social_pulse, "BTC")
        assert r["lunarcrush"]["galaxy_score"] == 71
        assert "galaxy 71" in r["interpretation"]

    def test_partial_subreddit_failure(self):
        with patch("crypto_intel.http_get_json", side_effect=self._router(fail=("CryptoCurrency",))):
            r = call_tool(ci.get_social_pulse, "BTC")
        assert r["status"] == "success"
        assert r["mentions"] == 3
        assert any("r/CryptoCurrency" in e for e in r["errors"])

    def test_all_fail_unavailable(self):
        with patch("crypto_intel.http_get_json", side_effect=HttpError("HTTP 403", 403, "u")):
            r = call_tool(ci.get_social_pulse, "BTC")
        assert r["status"] == "unavailable"

    def test_lexicon(self):
        s = ci._lexicon_score(["massive crash and panic", "dump it"])
        assert s["score"] == -1.0


# ---------------------------------------------------------------------------
# Token unlocks
# ---------------------------------------------------------------------------

def _ts(days):
    return int((NOW + timedelta(days=days)).timestamp())


LLAMA = [
    {"name": "Arbitrum", "token": "coingecko:arbitrum", "circSupply": 4_000_000_000, "tPrice": 0.5,
     "events": [
         {"timestamp": _ts(10), "noOfTokens": [92_650_000], "category": "insiders", "unlockType": "cliff"},
         {"timestamp": _ts(-5), "noOfTokens": [1], "category": "past"},
         {"timestamp": _ts(90), "noOfTokens": [1], "category": "too far"},
     ]},
    {"name": "Sui", "token": "coingecko:sui", "circSupply": 3_000_000_000, "tPrice": 3.0,
     "events": [{"timestamp": _ts(3), "noOfTokens": [10_000_000, 5_000_000], "category": "investors"}]},
]


class TestTokenUnlocks:
    def test_unavailable_without_keys(self):
        def fake(url, **kw):
            raise HttpError("HTTP 402" if "llama" in url else "HTTP 401", 402 if "llama" in url else 401, url)
        with patch("crypto_intel.http_get_json", side_effect=fake):
            r = call_tool(ci.get_token_unlocks, "ARB")
        assert r["status"] == "unavailable"
        assert "DESK_TOKENOMIST_KEY" in r["hint"] and "DESK_DEFILLAMA_KEY" in r["hint"]

    def test_defillama_pro_with_symbol(self, monkeypatch):
        monkeypatch.setenv("DESK_DEFILLAMA_KEY", "llk")
        urls = []

        def fake(url, **kw):
            urls.append(url)
            if "tokenomist" in url:
                raise HttpError("HTTP 401", 401, url)
            if url == ci.LLAMA_EMISSIONS_FREE:
                raise HttpError("HTTP 402", 402, url)
            return LLAMA
        with patch("crypto_intel.http_get_json", side_effect=fake):
            r = call_tool(ci.get_token_unlocks, "ARB", 30)
        assert r["status"] == "success"
        assert r["source"] == "defillama-emissions"
        assert "https://pro-api.llama.fi/llk/api/emissions" in urls
        assert len(r["unlocks"]) == 1
        u = r["unlocks"][0]
        assert u["name"] == "Arbitrum"
        assert u["pct_circulating"] == pytest.approx(2.32, abs=0.01)
        assert u["usd_value_m"] == pytest.approx(46.3, abs=0.1)
        assert u["material"] is True
        assert "tokenomist" in r["errors"][0]

    def test_all_tokens_sorted_by_impact(self):
        with patch("crypto_intel.http_get_json", return_value=LLAMA):
            r = call_tool(ci.get_token_unlocks, None, 30)
        assert r["status"] == "success"
        assert [u["name"] for u in r["unlocks"]] == ["Arbitrum", "Sui"]
        assert r["unlocks"][1]["tokens"] == 15_000_000

    def test_tokenomist_success(self, monkeypatch):
        monkeypatch.setenv("DESK_TOKENOMIST_KEY", "tk")
        day = (NOW + timedelta(days=5)).strftime("%Y-%m-%dT00:00:00Z")

        def fake(url, params=None, headers=None, **kw):
            assert headers == {"x-api-key": "tk"}
            if url.endswith("/v4/token/list"):
                return {"data": [{"id": "arbitrum", "symbol": "ARB", "name": "Arbitrum",
                                  "circulatingSupply": 4e9, "price": 0.5}]}
            assert params["tokenId"] == "arbitrum"
            return {"data": [{"unlockDate": day, "cliffUnlocks": {"cliffAmount": 40_000_000, "cliffValue": 20_000_000}}]}
        with patch("crypto_intel.http_get_json", side_effect=fake):
            r = call_tool(ci.get_token_unlocks, "ARB", 30)
        assert r["status"] == "success"
        assert r["source"] == "tokenomist"
        u = r["unlocks"][0]
        assert u["pct_circulating"] == 1.0 and u["usd_value_m"] == 20.0 and u["material"] is False

    def test_unexpected_error(self):
        with patch("crypto_intel.http_get_json", side_effect=ValueError("boom")):
            r = call_tool(ci.get_token_unlocks, "ARB")
        assert r["status"] == "error"
