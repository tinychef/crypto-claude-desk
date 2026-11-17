#!/usr/bin/env bash
# autopilot.sh — Run crypto trading desk workflows headless via cron.
#
# Skills are NOT invoked via slash commands in -p mode. This script maps
# workflow names to detailed natural language prompts that Claude can
# execute using CLAUDE.md routing + MCP tools.
#
# Works in both modes:
#   - Installed plugin:  agents/MCP/skills auto-discovered, no extra flags.
#   - Local development: auto-detects and passes --plugin-dir so Claude Code
#     discovers agents/, mcp-servers/, and skills/.
#
# Usage:
#   ./bin/autopilot.sh monitor
#   ./bin/autopilot.sh brief
#   ./bin/autopilot.sh scan ["SOL HYPE ZEC" | 20]
#   ./bin/autopilot.sh quick "BTC ETH SOL"
#   ./bin/autopilot.sh analyze "BTC"
#   ./bin/autopilot.sh portfolio
#
# Crontab examples:
#   0 */4 * * * /path/to/crypto-trading-desk/bin/autopilot.sh monitor
#   37 7 * * *  /path/to/crypto-trading-desk/bin/autopilot.sh brief
#   0 8 * * *   /path/to/crypto-trading-desk/bin/autopilot.sh quick "BTC ETH SOL"
#   0 9 * * 1   /path/to/crypto-trading-desk/bin/autopilot.sh analyze "BTC"
#   0 16 * * 1-5 /path/to/crypto-trading-desk/bin/autopilot.sh portfolio

set -euo pipefail

# ---------------------------------------------------------------------------
# Project root (relative to this script)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# PATH — cron doesn't inherit your shell's PATH
# ---------------------------------------------------------------------------
export PATH="$HOME/.local/bin:$HOME/.claude/local:/usr/local/bin:/usr/bin:/bin:$PATH"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR="$PROJECT_DIR/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y-%m-%d).log"
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"

# ---------------------------------------------------------------------------
# Allowed tools — pre-approve so cron doesn't hang on permission prompts
# ---------------------------------------------------------------------------
MCP_SERVERS="crypto-data crypto-exchange crypto-technical crypto-futures crypto-advanced-indicators crypto-market-microstructure crypto-learning-db crypto-polymarket crypto-defillama crypto-derivatives crypto-macro crypto-flows crypto-onchain crypto-intel crypto-quant"
ALLOWED_TOOLS="Read,Write,Grep,Glob,Task,WebSearch,WebFetch"
for server in $MCP_SERVERS; do
    ALLOWED_TOOLS="$ALLOWED_TOOLS,mcp__$server"
done

TODAY_UTC="$(date -u +%Y-%m-%d)"
# Shared rules appended to every multi-agent prompt (V2 output contract)
RULES="Delegate every agent via the Task tool with subagent_type general-purpose and an explicit model; tell each agent to read agents/<role>.md, follow docs/output-format.md, and not use the Edit tool. Final answer in Spanish, short (docs/output-format.md)."

# ---------------------------------------------------------------------------
# Plugin discovery — detect if running as local dev (not an installed plugin)
# ---------------------------------------------------------------------------
# When installed via `claude plugin install`, Claude Code discovers agents,
# MCP servers, and skills automatically. For local clones / forks, we need
# --plugin-dir so Claude Code loads everything from this directory.
#
# Detection: if plugin.json exists, this is a plugin repo (local or installed).
# We always pass --plugin-dir for safety — it's a no-op if already installed.
PLUGIN_FLAGS=""
if [ -f "$PROJECT_DIR/plugin.json" ] || [ -d "$PROJECT_DIR/agents" ]; then
    PLUGIN_FLAGS="--plugin-dir $PROJECT_DIR"
fi

# ---------------------------------------------------------------------------
# Workflow → Prompt mapping
# ---------------------------------------------------------------------------
WORKFLOW="${1:-}"
ARGS="${2:-}"

case "$WORKFLOW" in
  monitor)
    PROMPT="Run the /monitor loop (skills/monitor/SKILL.md) for my crypto portfolio. ${RULES}
1. market-monitor (haiku): get_portfolio_state(), list_orders(status='pending'), list_kill_switches() from crypto-learning-db; live prices (crypto-exchange) for every symbol with an open trade or pending order plus BTC; BTC last completed daily/weekly close (fetch_ohlcv_data 1d); classify_regime('BTC') and market_breadth() (crypto-quant), get_fear_greed_index(), get_funding_rate('BTC', exchange='okx'); call record_regime_snapshot(btc_price, regime, breadth_score, fear_greed, btc_funding) once. Return JSON with prices and metrics (btc_price, btc_daily_close, btc_weekly_close, btc_funding, fear_greed).
2. If kill switches are active: portfolio-manager (sonnet) calls evaluate_kill_switches(metrics=...) and executes triggered actions (cancel_order, close_trade, trigger_kill_switch).
3. Open trades vs SL/TP: hits → portfolio-manager (opus) close_trade(); profitable trades → risk-specialist (sonnet) may trail the stop with update_trade() (never widen, keep SL >= 1x ATR).
4. Pending orders: portfolio-manager (sonnet) check_orders(prices) -> fill_order(order_id, suggested_fill_price) for triggered ones (judge needs_review conditionals) -> expire_orders(). For each fill, learning-agent (opus) adds trade predictions not already attached from the order.
5. learning-agent (opus): post-mortem for each closed trade; find_expired_predictions(current_prices) and validate each (including predictions without trade_id); near month end generate_summary(summary_type='monthly').
6. Print a monitor report of at most 15 lines in Spanish."
    ;;

  quick)
    if [ -z "$ARGS" ]; then
      echo "Usage: $0 quick \"BTC ETH SOL\"" >&2
      exit 1
    fi
    PROMPT="Quick market snapshot for ${ARGS} (skills/quick/SKILL.md). ${RULES} One market-monitor agent (haiku): get_exchange_prices, get_fear_greed_index, get_funding_rate(exchange='okx'), get_aggregated_oi, classify_regime, get_support_resistance. Reply with at most 6 lines per symbol per docs/output-format.md section 4."
    ;;

  analyze)
    if [ -z "$ARGS" ]; then
      echo "Usage: $0 analyze \"BTC\"" >&2
      exit 1
    fi
    SYM_DIR="$(echo "$ARGS" | tr ' ' '-' | tr '[:lower:]' '[:upper:]')"
    DIR="data/reports/${TODAY_UTC}-${SYM_DIR}"
    PROMPT="Run the /analyze workflow (skills/analyze/SKILL.md) for ${ARGS}. ${RULES}
Phase 1 - in parallel: market-monitor (haiku) -> ${DIR}/market-data.md; technical-analyst (sonnet, classify_regime + volatility_regime, SL >= 1x ATR) -> ${DIR}/technical-analysis.md; news-sentiment (sonnet, crypto-intel first) -> ${DIR}/news-sentiment.md; macro-flows (sonnet) -> ${DIR}/macro-flows.md; if more than one symbol, also quant-scanner (sonnet) -> ${DIR}/alt-scan.md. Verify the files exist.
Phase 2 - risk-specialist (sonnet): read all Phase 1 files, flag any SL < 1x ATR(daily), verdict per setup -> ${DIR}/risk-assessment.md.
Phase 3 - portfolio-manager (opus): read all files, get_portfolio_state, list_orders, list_kill_switches, regime + behavior_base_rates; decide EXECUTE/WAIT/REJECT; record_trade on EXECUTE; place_order + add_kill_switch on WAIT with entries; record_prediction(trade_id=None, source='decision') for the key forecast -> ${DIR}/decision.md.
Phase 4 - if a trade was opened or orders placed: learning-agent (opus) records predictions (source='trade' with trade_id; source='order' with order_id for pending orders).
Phase 5 - write ${DIR}/brief.md (docs/output-format.md section 3, max 25 lines, Spanish; do NOT write full-report.md) and print only the brief."
    ;;

  portfolio)
    PROMPT="Portfolio status (skills/portfolio/SKILL.md), read-only. ${RULES} One portfolio-manager agent (opus): get_portfolio_state(), get_trade_stats(), list_orders(status='pending'), list_watchlist(), list_kill_switches() from crypto-learning-db and live prices for open trades and pending orders. At most 25 lines: balances per book, open trades with P&L, pending orders with distance to trigger, kill switches, watchlist."
    ;;

  brief)
    DIR="data/reports/${TODAY_UTC}-BRIEF"
    PROMPT="Run the daily brief (skills/brief/SKILL.md). ${RULES} Max 3 agents, no portfolio-manager, no trades or orders.
Step 1 - in parallel:
  1. market-monitor (haiku): BTC/ETH price, 24h change, funding (exchange='okx'), get_aggregated_oi, get_liquidations_history, get_coinbase_premium, get_fear_greed_index; list_orders(status='pending') with live prices and distance to trigger -> ${DIR}/market-data.md
  2. macro-flows (sonnet): crypto-macro, crypto-flows, crypto-derivatives options, crypto-onchain; verdict A FAVOR/NEUTRAL/EN CONTRA for 1-7d and 2-6w; get_macro_calendar next 7 days -> ${DIR}/macro-flows.md
  3. quant-scanner (sonnet): classify_regime('BTC'), market_breadth, sector_rotation, relative_strength_scan on the top 40 liquid OKX USDT pairs; top 3 complete setups only (ATR-based SL, R/R per book, base rate) -> ${DIR}/alt-scan.md
Step 2 - you synthesize from the JSON blocks: write ${DIR}/brief.md and print it (max 25 lines, Spanish): regime + breadth, BTC line, macro/flows verdict per horizon, top-3 table (asset, book, side, entry, SL, TPs, R/R, base rate), pending orders status, 7-day calendar, one suggested action (/analyze X only if a complete setup has its entry within 3% of price), data gaps."
    ;;

  scan)
    UNIVERSE="${ARGS:-top 40 liquid OKX USDT pairs}"
    DIR="data/reports/${TODAY_UTC}-ALTS"
    PROMPT="Run the alt scan (skills/scan/SKILL.md) on: ${UNIVERSE}. ${RULES}
1. quant-scanner (sonnet): regime, breadth, sector_rotation, relative_strength_scan, behavior_base_rates, correlation_matrix, token unlocks; max 5 complete setups with ATR-based SL -> ${DIR}/alt-scan.md. If 0 setups, print 'NO HACER NADA' with the condition that would change it and stop.
2. risk-specialist (sonnet): read alt-scan.md, stress-test only those setups, flag SL < 1x ATR(daily), OK/AJUSTAR/RECHAZAR -> ${DIR}/risk-assessment.md.
3. portfolio-manager (opus): read both files, get_portfolio_state, list_orders, list_kill_switches, verify prices; apply the two-book caps; record_trade only if price is at the entry, otherwise place_order (expiry + cancel condition) and add_kill_switch; record_prediction(trade_id=None, source='scan') for notable rejects -> ${DIR}/decision.md.
4. If orders or trades were created: learning-agent (opus) records their predictions.
5. Write ${DIR}/brief.md (docs/output-format.md section 3, max 25 lines, Spanish) and print only the brief plus the persisted IDs."
    ;;

  *)
    echo "Crypto Trading Desk — Autopilot" >&2
    echo "" >&2
    echo "Usage: $0 <workflow> [args]" >&2
    echo "" >&2
    echo "Workflows:" >&2
    echo "  monitor              SL/TP, pending orders, kill switches, regime snapshot, predictions" >&2
    echo "  brief                Daily brief (regime, BTC, macro/flows, top 3 setups, orders)" >&2
    echo "  scan [\"SOL HYPE\"|N]  Alt scanner -> risk -> decision (persists orders)" >&2
    echo "  quick \"BTC ETH\"      Fast market snapshot" >&2
    echo "  analyze \"BTC\"        Full multi-agent analysis with decision + brief" >&2
    echo "  portfolio            Portfolio status and open trades" >&2
    echo "" >&2
    echo "Crontab examples:" >&2
    echo "  0 */4 * * * $0 monitor" >&2
    echo "  37 7 * * *  $0 brief" >&2
    echo "  0 8 * * *   $0 quick \"BTC ETH SOL\"" >&2
    echo "  0 9 * * 1   $0 analyze \"BTC\"" >&2
    echo "  0 16 * * 1-5 $0 portfolio" >&2
    echo "" >&2
    echo "Custom workflows:" >&2
    echo "  Add a new case block in the WORKFLOW section of this script." >&2
    echo "  Each workflow is a natural language prompt — describe what you" >&2
    echo "  want Claude to do using the agents and MCP tools available." >&2
    exit 1
    ;;
esac

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
echo "[$TIMESTAMP] Running: $WORKFLOW ${ARGS:-}" >> "$LOG_FILE"

# shellcheck disable=SC2086
claude -p "$PROMPT" \
    --allowedTools "$ALLOWED_TOOLS" \
    $PLUGIN_FLAGS \
    --output-format text \
    >> "$LOG_FILE" 2>&1

EXIT_CODE=$?

TIMESTAMP_END="$(date '+%Y-%m-%d %H:%M:%S')"
echo "[$TIMESTAMP_END] Finished $WORKFLOW (exit: $EXIT_CODE)" >> "$LOG_FILE"
echo "---" >> "$LOG_FILE"

exit $EXIT_CODE
