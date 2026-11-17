---
name: monitor
description: Autonomous monitoring loop. Checks open trades against SL/TP, fills/expires pending orders, evaluates kill switches, records a regime snapshot, validates expired predictions and generates periodic summaries. Run via cron for full autonomy. Usage: /monitor
user-invocable: true
---

# Monitor — autonomous loop

Every agent: Task with `subagent_type: general-purpose` + explicit `model`; every prompt ends with "Follow docs/output-format.md. Do NOT use the Edit tool."

## Step 1: Snapshot (one agent)
`model: haiku` — "You are the market-monitor agent.
1. From crypto-learning-db: get_portfolio_state(), list_orders(status='pending'), list_kill_switches(). Collect every symbol with an open trade or pending order, plus BTC.
2. Prices for each symbol: get_exchange_prices (crypto-exchange). BTC: also the last COMPLETED daily close (fetch_ohlcv_data timeframe='1d', limit=3) and the last completed weekly close if any kill switch uses it.
3. Regime data: classify_regime('BTC') and market_breadth() (crypto-quant), get_fear_greed_index() (crypto-data), get_funding_rate('BTC', exchange='okx') (crypto-futures).
4. Call record_regime_snapshot(btc_price, regime, breadth_score, fear_greed, btc_funding) ONCE.
Return JSON only: {prices: {'BTC/USDT': ..}, metrics: {btc_price, btc_daily_close, btc_weekly_close, btc_funding, fear_greed, futures_realized_loss, <SYMBOL>_price...}, regime, open_trades: [...], pending_orders: [...], kill_switches: [...]}."

## Step 2: Kill switches first
If any active kill switches: `model: sonnet` — "You are the portfolio-manager agent. Call evaluate_kill_switches(metrics='<metrics JSON from Step 1>'). For each triggered switch, execute its action_text within its scope: cancel_order(order_id, reason='kill switch <id>: <rule>') for pending orders; close_trade(trade_id, exit_price=<current price>, close_reason='kill switch <id>: <rule>') for open trades; then trigger_kill_switch(kill_switch_id). Report missing_metrics. Return what was triggered, cancelled and closed."

## Step 3: Open trades vs SL/TP
For each open trade (long: SL hit if price ≤ stop_loss, TP if ≥ take_profit; short: inverse):
- Hit → `model: opus` portfolio-manager: "Close {trade_id} at {price}: close_trade(trade_id, exit_price, close_reason='{SL|TP} hit at {level}')."
- Profitable and not closed → `model: sonnet` risk-specialist: "Trade {trade_id} ({symbol} {side}) entry {entry}, now {price}, SL {sl}, TP {tp}. Use volatility_regime (crypto-quant) and get_support_resistance (crypto-technical). Trail only in the profitable direction, never widen, keep the SL ≥ 1×ATR from price. If adjusting: update_trade(trade_id, stop_loss, notes='<why>')."

## Step 4: Pending orders
If pending orders exist: `model: sonnet` portfolio-manager: "Call check_orders(prices='<prices JSON>'). For each `triggered` order call fill_order(order_id, fill_price=<suggested_fill_price>) — it creates the trade and attaches the order's predictions. For `needs_review` (conditional) orders, judge condition_text against the data and fill or leave. Then expire_orders(). Return filled (order → trade IDs) and expired IDs."
For each fill → `model: opus` learning-agent: "Order {order_id} filled as {trade_id}. Its order predictions are already attached; add only what they miss (Mission 2, source='trade')."

## Step 5: Learning
- Each trade closed in Steps 2-3 → `model: opus` learning-agent: "Post-mortem for {trade_id} (Mission 4): validate its predictions, upsert_pattern, write the post-mortem file."
- Always → `model: opus` learning-agent: "find_expired_predictions(current_prices='<prices JSON>') and validate each (Mission 3), including predictions without trade_id and orders that expired unfilled."
- Last 2 days of month → also "generate_summary(summary_type='monthly')" (and 'quarterly' at quarter end).

## Step 6: Report (≤15 lines, Spanish)
```
## Monitor — {fecha hora UTC}
Régimen: {regime} · BTC {price} (cierre diario {close}) · F&G {n} · funding {x}%
Trades abiertos: N · cerrados ahora: {id PnL, ...}
Órdenes: N pendientes · ejecutadas: {id→trade} · caducadas: {ids}
Kill switches: {disparados + acción | ninguno}
Predicciones validadas: N (✓x / ✗y)
Próximo a vigilar: {orden/trade más cerca de su nivel, distancia %}
```
