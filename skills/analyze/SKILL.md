---
name: analyze
description: Full multi-agent crypto analysis with a trading decision and persisted orders. Usage: /analyze BTC · /analyze ETH SOL · /analyze BTC --alts · add --full for per-agent detail
user-invocable: true
---

# Full Crypto Analysis (V2)

Analyze $ARGUMENTS in 5 phases. Each phase writes a file; later phases read those files. Final answer to the user = the brief only (`docs/output-format.md` §3, ≤25 lines, Spanish).

**Delegation rules:** every agent is a Task with `subagent_type: general-purpose` and an explicit `model`. Every prompt starts with "You are the <role> agent. Read agents/<role>.md for your framework." and ends with "Follow docs/output-format.md. Do NOT use the Edit tool."

## Phase 0: Setup
- Parse: symbols (first = primary `{SYMBOL}`), flags `--alts`, `--full`.
- `DIR = data/reports/YYYY-MM-DD-{SYMBOL}/` (UTC date; multi-symbol → `YYYY-MM-DD-{SYM1}-{SYM2}`). Create it.
- `SCAN = true` if `--alts` or more than one symbol.

## Phase 1: Data gathering (parallel — launch all in ONE response)
1. **market-monitor** (`haiku`): "Market data for {SYMBOLS}: prices (crypto-exchange), F&G/dominance (crypto-data), aggregated OI, liquidations, OKX positioning (crypto-derivatives), funding (crypto-futures, exchange='okx'), Coinbase premium (crypto-flows). Write {DIR}/market-data.md."
2. **technical-analyst** (`sonnet`): "Technical analysis for {SYMBOLS}. Start with get_prediction_track_record(agent='technical-analyst', symbol='{SYMBOL}/USDT'). Use classify_regime + volatility_regime (crypto-quant) first; every SL ≥ 1×ATR. Indicators from crypto-technical and crypto-advanced-indicators. Write {DIR}/technical-analysis.md."
3. **news-sentiment** (`sonnet`): "News and sentiment for {SYMBOLS}. Start with get_prediction_track_record(agent='news-sentiment', ...). crypto-intel first (get_headlines, get_crypto_panic, get_social_pulse, get_token_unlocks), then ≤4 WebSearch for gaps, Polymarket for event odds. Write {DIR}/news-sentiment.md."
4. **macro-flows** (`sonnet`): "Macro and flows backdrop for {SYMBOLS}: crypto-macro, crypto-flows, crypto-derivatives (options), crypto-onchain, Polymarket. Verdict A FAVOR/NEUTRAL/EN CONTRA for 1-7d and 2-6w. Write {DIR}/macro-flows.md."
5. *(only if SCAN)* **quant-scanner** (`sonnet`): "Scan alts (universe: {extra symbols or 'top 40 liquid OKX USDT pairs'}): regime, breadth, sector_rotation, relative_strength_scan, behavior_base_rates, correlation_matrix. Max 5 complete setups with ATR-based SL. Write {DIR}/alt-scan.md."

**Verify** with Glob that the files exist. A missing file (timeout) is passed to Phase 2 as a data gap — do not re-run.

## Phase 2: Risk (after Phase 1 files exist)
6. **risk-specialist** (`sonnet`): "FIRST read every .md in {DIR} (already written). Then get_prediction_track_record(agent='risk-specialist', ...). Stress-test each setup: volatility_regime, correlation_matrix (crypto-quant); options/liquidations (crypto-derivatives); microstructure; stablecoin flows; get_btc_valuation. Flag any SL < 1×ATR(daily). Verdict OK/AJUSTAR/RECHAZAR per setup. Write {DIR}/risk-assessment.md."

Verify `risk-assessment.md` exists.

## Phase 3: Decision (after Phase 2)
7. **portfolio-manager** (`opus`): "FIRST read every .md in {DIR}. get_portfolio_state(), list_orders(status='pending'), list_kill_switches(). Verify price with get_exchange_prices. Read regime (classify_regime) and behavior_base_rates for the current condition, plus the macro-flows verdict. Decide EXECUTE/WAIT/REJECT per the two-book rules. EXECUTE → record_trade(). WAIT with entries → place_order() for each (expiry + cancel condition) and add_kill_switch() for each measurable kill switch. Always record_prediction(trade_id=None, source='decision') for the key forecast. Write {DIR}/decision.md (§2). Return the verdict line, the action table and all persisted IDs."

## Phase 4: Predictions (if a trade was opened OR orders were placed)
8. **learning-agent** (`opus`): "Read {DIR}/decision.md (and alt-scan.md if present). Record testable predictions (Mission 2): trades opened → source='trade' with trade_id; pending orders → trade_id=None, source='order', order_id (fills before expiry? TP1 before SL?); alt-scan setups not taken → source='scan'. Skip any already recorded by the PM. Return the prediction IDs."

Skip Phase 4 on a REJECT with no orders (the PM's own prediction is enough).

## Phase 5: Brief (coordinator — no agent)
1. Read `{DIR}/decision.md` and the JSON blocks of the other reports (not the full prose).
2. Write `{DIR}/brief.md` per `docs/output-format.md` §3: veredicto · one action table · 3-5 "por qué" bullets each with a number (regime/base rate, macro-flows, flows/derivatives, risk) · triggers and kill-switches · datos faltantes · "Paper trading — no es asesoramiento financiero." Do NOT write `full-report.md`.
3. Answer the user with the brief only (≤25 lines, Spanish), plus one line with the persisted order/kill-switch/prediction IDs and the report folder path.
4. `--full`: append ≤3 lines per agent summary and the file paths.
