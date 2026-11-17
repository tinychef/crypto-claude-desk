---
name: brief
description: Daily morning brief (≤25 lines, Spanish) — regime, BTC, macro/flows tide, top 3 opportunities, pending orders status and the next 7 days of events. 3 agents, no decisions. Usage: /brief
user-invocable: true
---

# Daily Brief

Cheap daily read. 3 agents max, no portfolio-manager, no trades. If an actionable setup appears, suggest `/analyze X` — do not decide here.

Every agent: Task with `subagent_type: general-purpose` + explicit `model`; prompt starts "You are the <role> agent. Read agents/<role>.md for your framework." and ends "Follow docs/output-format.md. Do NOT use the Edit tool."
`DIR = data/reports/YYYY-MM-DD-BRIEF/` (UTC date). Create it.

## Step 1: Three agents in parallel (ONE response)
1. **market-monitor** (`haiku`): "BTC and ETH snapshot: price, 24h, funding, aggregated OI Δ, liquidation bias, Coinbase premium, F&G. Also list_orders(status='pending') from crypto-learning-db and get_exchange_prices for each order symbol; give each order's distance to trigger in %. Write {DIR}/market-data.md."
2. **macro-flows** (`sonnet`): "Macro & flows verdict for crypto, 1-7d and 2-6w. Include get_macro_calendar for the next 7 days and get_fed_odds. Write {DIR}/macro-flows.md."
3. **quant-scanner** (`sonnet`): "Daily scan: classify_regime('BTC'), market_breadth, sector_rotation, relative_strength_scan on the top ~40 liquid OKX USDT pairs. Return the TOP 3 opportunities only, each complete (book, side, entry, ATR-based SL, TPs, R/R, base rate) or fewer if fewer qualify. Write {DIR}/alt-scan.md."

Verify the three files exist (Glob). Missing → note under "Datos faltantes".

## Step 2: Synthesize (coordinator, no agent)
Read only the JSON blocks at the end of the three files. Write `{DIR}/brief.md` and answer the user with exactly this (≤25 lines):

```markdown
## Brief {dd-mmm} — Régimen: {btc_regime} · Breadth {x}% > MA50
**BTC {price}** ({chg}%) · funding {x}% · OI {Δ}% · F&G {n} · Coinbase premium {x}%
**Viento:** 1-7d {A FAVOR|NEUTRAL|EN CONTRA} ({driver}) · 2-6w {…} ({driver})

| # | Activo | Libro | Dir | Entrada | SL | TPs | R/R | Base rate |
|---|---|---|---|---|---|---|---|---|
(top 3; si ninguno califica: "Sin setups que pasen las reglas hoy")

**Órdenes pendientes:** {id activo disparo — a X% · caduca} (o "ninguna")
**Calendario 7d:** {fecha evento} · …
**Acción:** `/analyze {X}` — {por qué, ≤12 palabras}  (o "Nada que hacer hoy")
**Datos faltantes:** … (si los hay)
```

Suggest `/analyze X` only when a setup is complete, R/R passes, and its entry is within ~3% of price or an order is within 1% of its trigger.
