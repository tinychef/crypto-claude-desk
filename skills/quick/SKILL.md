---
name: quick
description: Quick single-agent market check (≤6 lines). Usage: /quick BTC or /quick ETH
user-invocable: true
---

# Quick Market Check

Fast snapshot of $ARGUMENTS with ONE agent. No reports written.

## Workflow

1. Task with `subagent_type: general-purpose`, `model: haiku`:

   "You are the market-monitor agent. Quick snapshot for $ARGUMENTS. Call in ONE parallel batch:
   - get_exchange_prices(symbol='{SYMBOL}/USDT') (crypto-exchange) — price, 24h change, volume
   - get_fear_greed_index() (crypto-data)
   - get_funding_rate(symbol='{SYMBOL}', exchange='okx') (crypto-futures) and get_aggregated_oi(symbol='{SYMBOL}') (crypto-derivatives)
   - classify_regime(symbol='{SYMBOL}') (crypto-quant) and get_support_resistance(symbol='{SYMBOL}/USDT') (crypto-technical)
   No WebSearch unless price moved >5% in 24h (then 1 query for the cause).
   Tool returns unavailable/error → write 'n/d', never invent.
   Reply ONLY in this format (Spanish, ≤6 lines):
   **{SYMBOL} {price}** ({chg24h}%) · régimen: <3 palabras>
   Funding {x}%/8h · OI {Δ24h}% · F&G {n} (<label>)
   Niveles: ↑ {resistance} · ↓ {support}
   ¿`/analyze {SYMBOL}`? sí/no — <motivo en ≤12 palabras>
   Do NOT use the Edit tool."

2. Show the agent's lines as-is (`docs/output-format.md` §4). Nothing else.

## When "¿/analyze?" = sí
Price change >5% in 24h · F&G ≤ 15 or ≥ 85 · funding >0.05% or <-0.05%/8h · OI Δ24h >10% · price within 1% of a key level · regime just flipped.
