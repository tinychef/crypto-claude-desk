---
name: portfolio
description: View portfolio status — balances per book, open trades with live P&L, pending orders, watchlist and kill switches. Usage: /portfolio
user-invocable: true
---

# Portfolio Status

Read-only. One agent.

## Workflow

1. Task with `subagent_type: general-purpose`, `model: opus`:

   "You are the portfolio-manager agent. Read agents/portfolio-manager.md for the two-book rules. READ-ONLY: make no decisions and no DB writes.
   From crypto-learning-db: get_portfolio_state(), get_trade_stats(), list_orders(status='pending'), list_watchlist(), list_kill_switches().
   Live prices via get_exchange_prices (crypto-exchange) for every symbol with an open trade or pending order.
   Reply in Spanish, ≤25 lines (brevity per docs/output-format.md):
   - SPOT: saldo / inicial 10.000 · X/5 abiertos · X% asignado | FUTUROS: saldo / 10.000 · X/3 abiertos · X% margen
   - Trades abiertos: tabla id | activo | libro | dir | entrada | actual | P&L % y USDT | SL | TP (marca ⚠ si está a <2% del SL o TP)
   - Órdenes pendientes: tabla id | activo | libro | dir | disparo | distancia % | SL | caduca
   - Kill switches activos: una línea cada uno (condición → acción)
   - Watchlist: activo — tesis en ≤8 palabras — revisar el {fecha}
   - Cerrados: N (ganados/perdidos) · P&L total · rendimiento global %
   Tool unavailable → 'n/d'. Do NOT use the Edit tool."

2. Show the result as-is. If anything is flagged ⚠, add one line suggesting `/monitor`.
