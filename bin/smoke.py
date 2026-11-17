#!/usr/bin/env python3
"""Live smoke test: one call per data tool, prints status per provider.

Usage: uv run --frozen python bin/smoke.py [server ...]
Never crashes on a provider failure: reports success / unavailable / error.
"""
import importlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-servers"))
os.chdir(ROOT)
import logging
logging.disable(logging.CRITICAL)

CALLS = {
    "crypto_exchange_ccxt_ultra": [("get_exchange_prices", {"symbol": "BTC/USDT"})],
    "crypto_ultra_simple": [("get_fear_greed_index", {}), ("get_global_market_stats", {})],
    "crypto_technical_analysis": [("get_support_resistance", {"symbol": "BTC"})],
    "crypto_advanced_indicators": [("calculate_adx", {"symbol": "BTC"})],
    "crypto_futures_data": [("get_funding_rate", {"symbol": "BTC"}), ("get_long_short_ratio", {"symbol": "BTC"})],
    "crypto_market_microstructure": [("calculate_spread_metrics", {"symbol": "BTC"})],
    "crypto_polymarket": [("search_markets", {"query": "bitcoin"})],
    "crypto_defillama": [("get_stablecoins_overview", {})],
    "crypto_derivatives": [("get_okx_positioning", {"symbol": "BTC"}), ("get_options_snapshot", {"currency": "BTC"}),
                           ("get_aggregated_oi", {"symbol": "BTC"}), ("get_hyperliquid_market", {"symbol": "BTC"}),
                           ("get_basis_term_structure", {"symbol": "BTC"})],
    "crypto_macro": [("get_macro_snapshot", {}), ("get_fed_odds", {}), ("get_macro_calendar", {"days_ahead": 21})],
    "crypto_flows": [("get_etf_flows", {"asset": "BTC"}), ("get_coinbase_premium", {"symbol": "BTC"}),
                     ("get_stablecoin_flows", {})],
    "crypto_onchain": [("get_btc_valuation", {}), ("get_network_health", {})],
    "crypto_intel": [("get_headlines", {"query": "bitcoin"}), ("get_social_pulse", {"symbol": "BTC"})],
    "crypto_quant": [("classify_regime", {"symbol": "BTC"}), ("volatility_regime", {"symbol": "BTC"})],
}


def status_of(res):
    if isinstance(res, dict):
        if "status" in res:
            return res["status"]
        if "success" in res:
            return "success" if res["success"] else "error"
        if "error" in res:
            return "error"
    return "success"


def main():
    only = set(sys.argv[1:])
    rows = []
    for mod_name, calls in CALLS.items():
        if only and mod_name not in only and mod_name.replace("crypto_", "crypto-") not in only:
            continue
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            rows.append((mod_name, "(import)", "error", str(e)[:80], 0))
            continue
        for tool, args in calls:
            obj = getattr(mod, tool, None)
            fn = getattr(obj, "fn", obj)
            if fn is None:
                rows.append((mod_name, tool, "missing", "", 0))
                continue
            t0 = time.time()
            try:
                res = fn(**args)
                st = status_of(res)
                detail = ""
                if st != "success" and isinstance(res, dict):
                    detail = str(res.get("reason") or res.get("error") or "")[:80]
            except Exception as e:
                st, detail = "crash", f"{e.__class__.__name__}: {e}"[:80]
            rows.append((mod_name, tool, st, detail, time.time() - t0))
    w = max(len(r[0]) for r in rows) if rows else 10
    for m, t, st, d, dt in rows:
        print(f"{m:<{w}}  {t:<26} {st:<12} {dt:5.1f}s  {d}")
    counts = {}
    for r in rows:
        counts[r[2]] = counts.get(r[2], 0) + 1
    print("\nsummary:", json.dumps(counts))
    return 1 if counts.get("crash") else 0


if __name__ == "__main__":
    sys.exit(main())
