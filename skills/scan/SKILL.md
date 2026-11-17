---
name: scan
description: Alt-coin scanner with decision — quant-scanner shortlist → risk-specialist stress test → portfolio-manager decides and persists limit orders. Usage: /scan · /scan 20 · /scan SOL HYPE ZEC NEAR · add --full for detail
user-invocable: true
---

# Alt Scan

Find the best alt setups and leave them as persisted orders (or reject them). Final answer = brief (`docs/output-format.md` §3, ≤25 lines, Spanish).

Every agent: Task with `subagent_type: general-purpose` + explicit `model`; prompt starts "You are the <role> agent. Read agents/<role>.md for your framework." and ends "Follow docs/output-format.md. Do NOT use the Edit tool."

## Step 0
- `$ARGUMENTS`: empty → universe = top 40 liquid OKX USDT pairs; a number N → top N; symbols → exactly those.
- `DIR = data/reports/YYYY-MM-DD-ALTS/` (UTC). Create it.

## Step 1: Scan
**quant-scanner** (`sonnet`): "Universe: {universe}. Regime, breadth, sector_rotation, relative_strength_scan, behavior_base_rates, correlation_matrix, token unlocks. Max 5 complete setups (ATR-based SL, R/R per book) + one-line rejects. Write {DIR}/alt-scan.md."
Verify the file. If it has 0 setups → skip to Step 4 with "NO HACER NADA" and the condition that would change it.

## Step 2: Risk on the shortlist only
**risk-specialist** (`sonnet`): "FIRST read {DIR}/alt-scan.md. Stress-test ONLY its setups: volatility_regime, correlation_matrix (incl. BTC and open positions from get_portfolio_state), funding/liquidations (crypto-derivatives), orderbook depth, unlocks. Flag SL < 1×ATR(daily). OK/AJUSTAR/RECHAZAR per setup. Write {DIR}/risk-assessment.md."

## Step 3: Decide and persist
**portfolio-manager** (`opus`): "FIRST read {DIR}/alt-scan.md and risk-assessment.md. get_portfolio_state(), list_orders(status='pending'), list_kill_switches(); verify prices (get_exchange_prices). Apply the two-book rules and caps (pending orders count as filled; correlated alts = one bet). For each surviving setup: EXECUTE (record_trade) only if price is at the entry now, otherwise place_order(portfolio_type, usd_amount, trigger_price, stop_loss, take_profits, expires_at, condition_text) and add_kill_switch(rule_text, metric, operator, threshold, action_text, scope) for measurable invalidations. record_prediction(trade_id=None, source='scan') for the main call of each rejected-but-notable setup. Write {DIR}/decision.md (§2). Return verdict line, action table, persisted IDs."
If orders were placed or trades opened → **learning-agent** (`opus`): "Read {DIR}/decision.md. Record predictions for new trades (source='trade') and orders (source='order', order_id) per Mission 2. Skip duplicates."

## Step 4: Brief (coordinator)
Write `{DIR}/brief.md` per §3 and answer with it only (≤25 lines), plus one line with persisted IDs and the folder path. `--full` → add ≤3 lines per agent.
