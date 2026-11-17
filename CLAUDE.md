# Crypto Trading Desk V2 - Multi-Agent Intelligence System

A cryptocurrency analysis system built with Claude Code's native features: agents, MCP servers, and skills.
**V2:** more data (derivatives, options, macro, ETF flows, on-chain, news), more analysis (regime, breadth, rotation, behavior base rates), persistent pending orders + kill switches, and **short, action-first answers in Spanish** (see `docs/output-format.md`).

## Architecture

```
User Query
    |
    v
[Claude Code - Coordinator]
    |  Reads this CLAUDE.md
    |  Routes by complexity:
    |
    +-- QUICK (1 subagent) ----------> market-monitor (haiku)
    |
    +-- BRIEF (3 parallel) ----------> market-monitor + macro-flows + quant-scanner  (/brief)
    |
    +-- STANDARD (2-3 parallel) -----> market-monitor + technical-analyst + news-sentiment
    |
    +-- FULL ANALYSIS (6-7 subagents) -> 3 phases with file-based coordination
         Phase 1 (parallel): market-monitor || technical-analyst || news-sentiment || macro-flows [|| quant-scanner if --alts]
         Phase 2 (sequential): risk-specialist (reads Phase 1 files)
         Phase 3 (sequential): portfolio-manager (reads all, decides, persists orders + kill switches)
```

## How to Use

### First-Time Setup
- `/setup` - **Run once after installing.** Detects environment, installs uv + Python deps, verifies the 15 MCP servers work. Cross-platform (macOS, Linux, Windows).
- Optional API keys and cloud network allowlist: see `docs/data-sources.md`.

### Slash Commands
- `/brief` - Daily brief: regime, BTC, top 3 opportunities, pending orders, 7-day calendar (≤25 lines)
- `/quick BTC` - Fast market snapshot (1 agent, ≤6 lines)
- `/analyze BTC` - Full phased analysis with trading decision (`--alts` adds the alt scanner, `--full` adds detail)
- `/scan` - Altcoin opportunity scanner (quant-scanner → risk → portfolio-manager)
- `/macro` - Macro + flows + options read: tailwind or headwind
- `/orders` - Pending orders, kill switches and watchlist (list / cancel)
- `/portfolio` - Portfolio status, open trades and pending orders
- `/close-trade trade_001` - Close trade with post-mortem
- `/validate-predictions` - Review pending predictions against market data
- `/monitor` - Autonomous loop: regime snapshot, kill switches, SL/TP, pending-order fills/expiry, predictions
- `/create` - Extend the system with new components

### Natural Language
Ask naturally and the coordinator routes by complexity:
- "How's BTC?" → Quick (market-monitor only)
- "What should I do today?" / "brief" → /brief
- "RSI of ETH?" → Standard (technical-analyst only)
- "Analyze SOL" → Standard (2-3 agents)
- "Full analysis of BTC with recommendation" / "should I buy?" → Full (phases)
- "Which altcoins look good?" → /scan
- "Is macro a headwind?" / "ETF flows?" → macro-flows
- "Market regime? behavior after crashes?" → quant-scanner
- "Check my portfolio" / "my orders" → portfolio-manager / /orders
- "Post-mortem on trade_003" → learning-agent

## Routing Rules

### How Delegation Works

All agents are spawned via the Task tool with `subagent_type: general-purpose` and an explicit `model` parameter. This ensures every subagent inherits all MCP tools from the parent conversation. The agent `.md` files in `agents/` contain the analysis framework — include "Read agents/{name}.md for your full analysis framework" in the prompt.

> **Why `general-purpose`?** Plugin-defined agent types (e.g., `crypto-trading-desk:market-monitor`) don't receive MCP tools due to a known Claude Code limitation ([#4476](https://github.com/anthropics/claude-code/issues/4476)). Using `general-purpose` with explicit `model` works in both plugin and local-clone modes.

Always include "Do NOT use the Edit tool" in every agent prompt.

### Quick Query (single subagent via Task tool)
Use for simple data points. One agent, fast response.

| Query Type | Role | Model | Key MCP tools to mention in prompt |
|-----------|------|-------|-----------------------------------|
| Price, volume, market overview | market-monitor | haiku | crypto-exchange, crypto-data, crypto-futures |
| RSI, MACD, indicators, patterns | technical-analyst | sonnet | crypto-technical, crypto-advanced-indicators |
| News, sentiment, FUD/FOMO, unlocks | news-sentiment | sonnet | crypto-intel, crypto-polymarket, WebSearch |
| Macro, ETF flows, options, Fed odds | macro-flows | sonnet | crypto-macro, crypto-flows, crypto-derivatives, crypto-onchain |
| Regime, breadth, rotation, base rates, alt scan | quant-scanner | sonnet | crypto-quant, crypto-exchange, crypto-intel |
| Portfolio status, trade history, orders | portfolio-manager | opus | crypto-learning-db, crypto-exchange |
| Pattern lookup, pre-trade check | learning-agent | opus | crypto-learning-db, crypto-data |

Example delegation:
```
Task(subagent_type="general-purpose", model="haiku", prompt="You are the market-monitor agent. Read agents/market-monitor.md for your framework. Get BTC price using get_exchange_prices(symbol='BTC/USDT')... Do NOT use the Edit tool.")
```

### Standard Analysis (2-3 subagents in parallel)
Use for "analyze X" without explicit "full analysis" or "recommendation" request.
Launch 3 Tasks in parallel (all `subagent_type: general-purpose`):
- market-monitor (model: haiku) + technical-analyst (model: sonnet) + news-sentiment (model: sonnet)
- Coordinator synthesizes results

### Full Analysis with Decision (6-7 subagents in phases)
Use for "full analysis", "should I buy", "recommendation", or `/analyze`.
Spawn subagents via the Task tool in **3 sequential phases** (see `/analyze` skill for detailed prompts):

**Phase 1 - Data Gathering (4-5 Tasks in parallel):**
- market-monitor (haiku) → `data/reports/YYYY-MM-DD-{symbol}/market-data.md`
- technical-analyst (sonnet) → `data/reports/YYYY-MM-DD-{symbol}/technical-analysis.md`
- news-sentiment (sonnet) → `data/reports/YYYY-MM-DD-{symbol}/news-sentiment.md`
- macro-flows (sonnet) → `data/reports/YYYY-MM-DD-{symbol}/macro-flows.md`
- quant-scanner (sonnet, only with `--alts` or several symbols) → `data/reports/YYYY-MM-DD-{symbol}/alt-scan.md`

**Wait for all Phase 1 Task calls to return. Verify files exist on disk.**

**Phase 2 - Risk Assessment (1 Task AFTER Phase 1):**
- risk-specialist (sonnet) reads Phase 1 files → `data/reports/YYYY-MM-DD-{symbol}/risk-assessment.md`

**Wait for Task to return. Verify file exists.**

**Phase 3 - Decision (1 Task AFTER Phase 2):**
- portfolio-manager (opus) reads all files → `data/reports/YYYY-MM-DD-{symbol}/decision.md`
- WAIT decisions with limit entries are persisted with `place_order` + `add_kill_switch` (not prose).

**Phase 5 - Brief:** the coordinator writes `brief.md` and answers the user with the brief only (`docs/output-format.md` §3).

**IMPORTANT:** Do NOT spawn all agents at once. Phase 2/3 agents MUST read files written by previous phases. Spawn them only after confirming previous phase files exist on disk.

### Special Queries (single subagent)
- "Close trade X" → portfolio-manager (close) + learning-agent (post-mortem + prediction validation)
- "What patterns have we seen?" → learning-agent
- "Check my predictions" → learning-agent via /validate-predictions
- "Risk on my position?" → risk-specialist
- "Monitor my trades" / "Check SL/TP" / "did my orders fill?" → /monitor (multi-agent coordination)
- "Cancel order X" / "show kill switches" → /orders
- "Create a new MCP/agent/skill" → system-builder via /create

## Delegation Rules

**ALWAYS delegate via Task with `subagent_type: general-purpose` and explicit `model`.** Include the agent's role and relevant MCP tools in the prompt. Don't call MCP tools directly from the main conversation.

**NEVER over-delegate.** If someone asks "price of BTC?", use one haiku agent. Don't spin up 5 agents for a simple question.

**After every trade close, ALWAYS delegate to learning-agent** for prediction validation. This is how the system learns.

## Two Books — Spot vs Futures (Always Explicit)

The portfolio is split into TWO BOOKS with different mandates. Whenever you discuss a position, allocation, or new trade, **always state which book**. Never let the portfolio-manager pick a book by accident — it must justify the choice.

| | **SPOT book** | **FUTURES book** |
|---|---|---|
| Job | Long-term conviction, ETF/narrative/on-chain plays | Tactical alpha, hedges, mean reversion, funding plays |
| Holding | Days to months | Hours to days |
| Direction | Long-biased (rare inverse setups) | Long + short equally |
| Leverage | 1x always | 2-5x typical, max 10x |
| SL distance | 8-15% (let thesis breathe) | 2-4% (leverage compensates) |
| R/R min | 3:1 | 2:1 |
| Max open | 5 | 3 |
| Max allocated | 80% of spot balance | 30% of futures balance (margin) |
| Funding cost | None | Tracked per trade |
| Liquidation risk | None | Computed at entry |

**Book selection question for any new trade (the portfolio-manager must answer this explicitly):**
1. Multi-week thesis (narrative, ETF, upgrade, on-chain) → SPOT
2. Multi-day technical setup at a specific level → FUTURES
3. Hedge for existing spot exposure → FUTURES (short)
4. Funding-rate harvest (delta-neutral) → FUTURES
5. "Averaging down" a spot loss → STOP. Re-think or wait.

See `agents/portfolio-manager.md` § "Two Books, Two Mindsets" for the full rules.

## Agents (9)

| Agent | Role | Model | MCPs |
|-------|------|-------|------|
| market-monitor | Fast market data, derivatives color, flows, whale alerts | haiku | data, futures, exchange, derivatives, flows |
| technical-analyst | Regime-aware indicators, levels, setups with ATR-based stops | sonnet | technical, advanced-indicators, exchange, data, quant, learning-db |
| news-sentiment | News feeds, social pulse, unlocks, crowd psychology, event odds | sonnet | intel, polymarket, learning-db (+ WebSearch) |
| macro-flows | Macro (rates, DXY, equities), ETF flows, stablecoins, options, Fed odds, calendar → tailwind/headwind | sonnet | macro, flows, derivatives, onchain, polymarket, defillama |
| quant-scanner | Regime, breadth, sector rotation, relative strength, behavior base rates, seasonality, alt shortlist | sonnet | quant, exchange, technical, futures, derivatives, intel, learning-db |
| risk-specialist | Volatility, options/liquidation risk, correlation, microstructure, on-chain; flags SL < 1×ATR | sonnet | technical, microstructure, derivatives, quant, onchain, flows, data, exchange, learning-db, defillama |
| portfolio-manager | Final decisions, book selection, persists trades / pending orders / kill switches | opus | data, learning-db, polymarket, defillama, quant |
| learning-agent | Predictions (trade/order/decision/scan), order fills, patterns, post-mortems | opus | data, learning-db |
| system-builder | Generate new MCP servers (on `desk_common`), agents, skills + tests | opus | (none) |

## MCP Servers (15 servers, 144 tools)

| Server | Tools | Data Source |
|--------|-------|-------------|
| crypto-data | 11 | CoinGecko (fear/greed, dominance, rankings — NOT live prices); cached + optional demo key |
| crypto-exchange | 15 | CCXT multi-exchange, OKX first (orderbooks, OHLCV, volume, arbitrage) |
| crypto-technical | 14 | CCXT + calculated (RSI, MACD, Bollinger, typed S/R, patterns, signals) |
| crypto-futures | 11 | CCXT futures + OKX Rubik (funding, OI, long/short, taker ratio, liquidation levels) |
| crypto-advanced-indicators | 8 | CCXT (OBV, MFI, ADX, Ichimoku, VWAP, Pivot Points) |
| crypto-market-microstructure | 6 | CCXT (orderbook depth, imbalance, spoofing, market impact) |
| crypto-learning-db | 34 | SQLite (trades, predictions, patterns, summaries, **orders, watchlist, kill switches, regime snapshots**) |
| crypto-polymarket | 6 | Polymarket Gamma (event probabilities; real search + crypto tag filter) |
| crypto-defillama | 7 | DefiLlama (TVL, stablecoins, DEX volume) |
| crypto-derivatives | 7 | Coinalyze (aggregated OI/liquidations), Deribit (options: DVOL, put/call, skew, max pain), OKX basis + Rubik, Hyperliquid |
| crypto-macro | 4 | FRED + Stooq (DXY, yields, M2, SPX/NDX, gold), BTC-macro correlation, Fed odds (Polymarket/Kalshi), macro calendar |
| crypto-flows | 4 | ETF flows (Farside/SoSoValue), Coinbase premium, Kimchi premium, stablecoin mint/burn |
| crypto-onchain | 4 | CoinMetrics (MVRV, NVT, activity), mempool.space; CryptoQuant/Glassnode netflows (paid) |
| crypto-intel | 4 | RSS headlines, CryptoPanic, Reddit/LunarCrush social, token unlocks |
| crypto-quant | 9 | Local analytics on OKX OHLCV: regime, breadth, RS scan, sector rotation, behavior base rates, seasonality, correlation, vol regime, beta |

All servers share `mcp-servers/desk_common/` (proxy-aware cached HTTP, ccxt factory with geo-block fallback, serializer). Any tool may return `status: "unavailable"` (missing key / host not allowed) — agents report it as a data gap and never invent the number. Providers, keys, costs and the cloud allowlist: `docs/data-sources.md`.

## Skills (12)

| Skill | Usage | Description |
|-------|-------|-------------|
| `/setup` | `/setup` | First-time environment setup (cross-platform) |
| `/brief` | `/brief` | Daily brief: regime + BTC + top 3 opportunities + orders + calendar (≤25 lines) |
| `/analyze` | `/analyze BTC [--alts] [--full]` | Full phased analysis with decision → `brief.md` |
| `/quick` | `/quick ETH` | Fast single-agent market check (≤6 lines) |
| `/scan` | `/scan [N or symbols]` | Altcoin scanner → risk → decision with persisted orders |
| `/macro` | `/macro` | Macro / flows / options tailwind-headwind read |
| `/orders` | `/orders [cancel order_001]` | Pending orders, kill switches, watchlist |
| `/portfolio` | `/portfolio` | Portfolio status, open trades, pending orders |
| `/close-trade` | `/close-trade trade_001` | Close trade with post-mortem + learning |
| `/validate-predictions` | `/validate-predictions` | Review pending predictions against market data |
| `/monitor` | `/monitor` | Regime snapshot, kill switches, SL/TP, order fills/expiry, predictions, summaries |
| `/create` | `/create a DeFi tracker` | Extend the system with new components (+ tests for MCP servers) |

Workflow scripts (Workflow tool): `.claude/workflows/daily-brief.js`, `.claude/workflows/alt-scan.js`, `.claude/workflows/close-learning.js`.

## Design Decisions

1. **Hybrid routing** — Quick queries use single subagents (cheap, fast). Full analysis uses 6-7 phased subagents (parallel where possible, sequential where needed).
2. **Model optimization** — haiku for data scouts, sonnet for analysis, opus for final decisions. ~40-60% token savings vs all-sonnet. Model is passed explicitly via Task `model` parameter.
3. **General-purpose subagents** — All agents use `subagent_type: general-purpose` to inherit MCP tools. Plugin agent types don't pass MCP tools to subagents ([Claude Code #4476](https://github.com/anthropics/claude-code/issues/4476)). Agent `.md` files define the analysis framework; the coordinator includes their instructions in the Task prompt.
4. **Persistent memory** — portfolio-manager and learning-agent use `memory: project` to build institutional knowledge across sessions.
5. **File-based coordination** — Agents write structured Markdown reports to a shared directory. Later-phase agents read those files before starting their analysis.
6. **Zero orchestration code** — No Python coordinator, no Agent SDK. Claude Code is the coordinator via CLAUDE.md + agents/.
7. **Dual distribution** — Works as a Claude Code plugin (`claude plugin install`) or as a local clone (`--plugin-dir`). Both modes use the same `general-purpose` delegation pattern.
8. **SQLite cognitive memory** — Trades, predictions, and patterns stored in SQLite (`data/db/learning.db`). Agents query only what they need via `crypto-learning-db` MCP, preventing context window overflow. Scales to unlimited trades.

## Cognitive Learning System

The system learns from every trade through a **SQLite-backed cognitive memory** (`crypto-learning-db` MCP server).

### Storage: SQLite Database (`data/db/learning.db`)

| Table | Purpose |
|-------|---------|
| `trades` | All open and closed trades with full metadata |
| `predictions` | Testable predictions from each agent (with NL evaluations) |
| `trade_modifications` | SL/TP change history with NL reasons |
| `patterns` | Named trading patterns with win rates |
| `summaries` | Monthly/quarterly performance summaries |
| `portfolio_state` | Current balances and aggregate stats |
| `orders` | Pending limit/stop/conditional orders with SL/TPs, expiry and fill link (V2) |
| `kill_switches` | Measurable portfolio rules (e.g. `btc_daily_close < 61600`) evaluated by /monitor (V2) |
| `watchlist` | Symbols with thesis, levels and review date (V2) |
| `regime_snapshots` | Periodic BTC regime / breadth / F&G / funding / macro snapshot (V2) |

### Three Learning Layers

1. **Operational memory** (SQLite queries): What the agent needs right now for a specific decision
2. **Strategic memory** (summaries table): Compressed knowledge from past periods, generated via `generate_summary()`
3. **Meta-learning** (`get_prediction_track_record()`): "How reliable is this type of setup in these conditions?"

### Adaptive Learning (NL-driven)

The system learns through **natural language reasoning, not formulas**. MCP tools store data; agents do all interpretation.

- `validate_prediction(evaluation="...")` stores the learning-agent's NL analysis of how close a prediction was and why. No formulas, no credit scores — just the agent's written reasoning.
- `get_prediction_track_record(symbol="...", strategy_type="...")` provides setup-centric accuracy by time window (7d, 30d, 90d, global) PLUS recent NL evaluations. The key question is "how reliable is this setup?" not "how much do I trust this agent?"
- `find_expired_predictions()` surfaces predictions past their timeframe with price context. The agent evaluates each one and validates.
- portfolio-manager reads track records + evaluations and reasons in NL about setup reliability. Claude is the consensus engine.

### Learning Loop

Decision → `place_order()` + `add_kill_switch()` + `record_prediction(source='decision')` → `/monitor` fills (`fill_order`) or expires orders → open trade → `record_trade()` + `record_prediction()` → close trade → `close_trade()` + `validate_prediction(evaluation="...")` → NL evaluation stored → `upsert_pattern()` → `generate_summary()` periodically → next trade: `get_prediction_track_record()` returns setup accuracy + evaluations → agent reasons about setup reliability → `find_expired_predictions()` catches missed validations.

## Headless / Autopilot Mode

Skills are **NOT** invoked via `/skill-name` in `-p` (print) mode. For scheduled execution via cron, use `bin/autopilot.sh` which maps workflow names to detailed natural language prompts:

```bash
./bin/autopilot.sh brief             # Daily brief (regime, BTC, top 3, orders, calendar)
./bin/autopilot.sh monitor           # Kill switches, SL/TP, pending orders, predictions
./bin/autopilot.sh scan              # Altcoin scanner with decision
./bin/autopilot.sh quick "BTC ETH"   # Fast market snapshot
./bin/autopilot.sh analyze "BTC"     # Full phased analysis
./bin/autopilot.sh portfolio         # Portfolio status
```

The wrapper handles PATH, working directory, tool permissions (`--allowedTools`), and logging.

## Output Guidelines (V2 — see `docs/output-format.md`)

- **Answer the user in Spanish by default, short and action-first**: one-line verdict (EJECUTAR / ESPERAR / DESCARTAR / NO HACER NADA), one action table (asset, book, side, entry, SL, TPs, size, R/R, expiry), 3-5 reasons with numbers, triggers + kill switches, data gaps. ≤25 lines. Detail only on `--full` or when asked.
- Analyst reports: ≤40 lines + JSON `signals` block; saved to `data/reports/`.
- Every pending order has an expiry and a cancel condition, and is persisted with `place_order`.
- Stops must sit outside daily noise (≥1×ATR) — use `volatility_regime`.
- Use behavior base rates and regime when claiming "oversold = bounce".
- Never invent data: `unavailable` / `error` → list under "Datos faltantes".
- Learning-agent builds pattern library over time via persistent memory.
