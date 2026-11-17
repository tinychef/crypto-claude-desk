---
name: orders
description: List and manage pending orders, kill switches and the watchlist. Usage: /orders · /orders cancel order_003 · /orders watch SOL "tesis" · /orders unwatch SOL · /orders killswitches · /orders disable 2
user-invocable: true
---

# Orders, Kill Switches & Watchlist

Thin wrapper over crypto-learning-db. One cheap agent.

## Parse $ARGUMENTS
| Input | Action |
|---|---|
| empty / `list` | list pending orders + active kill switches + watchlist |
| `cancel <order_id> [motivo]` | `cancel_order(order_id, reason)` |
| `cancel-all [symbol]` | cancel every pending order (optionally for one symbol) — confirm with the user first |
| `killswitches` | `list_kill_switches()` only |
| `disable <kill_switch_id>` | `disable_kill_switch(kill_switch_id)` |
| `watch <SYMBOL> "<tesis>" [niveles] [sesgo] [revisar YYYY-MM-DD]` | `add_watch(symbol, thesis, levels, bias, review_at)` |
| `unwatch <SYMBOL>` | `archive_watch(symbol)` |
| `history` | `list_orders(status='all')` (last 20) |

## Workflow
1. Task with `subagent_type: general-purpose`, `model: haiku`:

   "You are the portfolio-manager agent acting as order clerk. Do exactly this with crypto-learning-db: {action from the table}.
   For listings also call get_exchange_prices (crypto-exchange) for each symbol with a pending order and compute distance to trigger in %.
   Reply in Spanish, ≤20 lines (brevity per docs/output-format.md):
   - Órdenes pendientes: tabla id | activo | libro | dir | tipo | disparo | actual | dist % | SL | TPs | tamaño | caduca
   - Kill switches: id — condición → acción (órdenes/trades vinculados)
   - Watchlist: activo — tesis — niveles — revisar
   For a write action, confirm what changed (IDs). Make no other changes.
   Do NOT use the Edit tool."

2. Show the result as-is. An order within 1% of its trigger → suggest `/monitor`.
