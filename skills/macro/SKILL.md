---
name: macro
description: Macro and flows backdrop for crypto — DXY/rates/equities, ETF flows, stablecoins, options positioning, Fed odds and the 7-day calendar, with a "viento a favor / en contra" verdict. One agent. Usage: /macro
user-invocable: true
---

# Macro & Flows

One agent, no decisions.

1. `DIR = data/reports/YYYY-MM-DD-MACRO/` (UTC). Create it.
2. Task with `subagent_type: general-purpose`, `model: sonnet`:

   "You are the macro-flows agent. Read agents/macro-flows.md for your framework.
   Run the full macro + flows read ($ARGUMENTS if given, else BTC/ETH): get_macro_snapshot, get_crypto_macro_correlation, get_fed_odds, get_macro_calendar (crypto-macro); get_etf_flows, get_coinbase_premium, get_stablecoin_flows (crypto-flows); get_options_snapshot, get_max_pain, get_basis_term_structure (crypto-derivatives); get_btc_valuation (crypto-onchain); Polymarket Fed markets.
   Write {DIR}/macro-flows.md.
   Return ONLY (Spanish, ≤12 lines):
   **Viento 1-7d:** {A FAVOR|NEUTRAL|EN CONTRA} ({fuerza}/10) — {2 datos}
   **Viento 2-6w:** {…} — {2 datos}
   Macro: DXY {x} ({Δ5d}) · 10Y {x}% · SPX {Δ5d}% · corr BTC-SPX 30d {x}
   Flujos: ETF 5d {±M$} · stablecoins 7d {±M$} · Coinbase premium {x}%
   Opciones: DVOL {x} · skew 25d {x} · put/call {x} · max pain {x} ({fecha})
   Fed {fecha}: recorte {p}% · pausa {p}% · subida {p}%
   Calendario 7d: {fecha evento} · …
   Qué lo cambiaría: {trigger medible}
   Tool unavailable → 'n/d'. Follow docs/output-format.md. Do NOT use the Edit tool."

3. Show the result as-is.
