---
name: setup
description: First-time setup for Crypto Trading Desk. Detects your environment, installs dependencies, and verifies everything works. Run this once after installing the plugin.
user-invocable: true
---

# Crypto Trading Desk - Setup

Run environment detection and dependency setup. This skill makes the plugin work on any platform.

## Workflow

### Step 1: Detect Environment
Run these checks in parallel using Bash:
- `uname -s` (or `ver` on Windows) to detect OS
- `python3 --version` or `python --version` to check Python
- `which uv` or `where uv` to check if uv is installed
- Check if `.venv` directory exists in the plugin root
- Check if `data/db/` directory exists

### Step 2: Install uv (if missing)
Based on detected OS:

**macOS / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell):**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

After installing, verify `uv` is accessible. If it's installed to `~/.local/bin/` but not in PATH (common on macOS GUI apps), offer to create a symlink:
```bash
sudo ln -sf ~/.local/bin/uv /usr/local/bin/uv
```

If the user declines sudo, tell them to restart their terminal and try again.

### Step 3: Install Python dependencies
```bash
uv sync --project <plugin_root>
```
This creates `.venv/` and installs all packages from `uv.lock` (fastmcp, ccxt, numpy, pandas, requests).

If `uv sync` fails, try without `--frozen`:
```bash
uv sync --project <plugin_root>
```

### Step 4: Create data directories
```bash
mkdir -p data/trades data/reports data/logs data/db data/cache .claude/agent-memory/portfolio-manager
```

The SQLite database (`data/db/learning.db`) is created automatically by the crypto-learning-db MCP server on first use. No manual initialization needed.

### Step 5: Verify MCP servers work
Test that one MCP server can start by running:
```bash
uv run --frozen --project <plugin_root> python -c "import fastmcp; import ccxt; import numpy; import pandas; print('OK')"
```

Then check that every server module imports (15 servers):
```bash
cd <plugin_root>/mcp-servers && for m in crypto_ultra_simple crypto_exchange_ccxt_ultra crypto_technical_analysis crypto_futures_data crypto_advanced_indicators crypto_market_microstructure crypto_learning_db crypto_polymarket crypto_defillama crypto_derivatives crypto_macro crypto_flows crypto_onchain crypto_intel crypto_quant; do uv run --frozen --project <plugin_root> python -c "import $m" >/dev/null 2>&1 && echo "OK  $m" || echo "ERR $m"; done
```
Servers: crypto-data, crypto-exchange, crypto-technical, crypto-futures, crypto-advanced-indicators, crypto-market-microstructure, crypto-learning-db, crypto-polymarket, crypto-defillama, crypto-derivatives, crypto-macro, crypto-flows, crypto-onchain, crypto-intel, crypto-quant.
Optional API keys (env `DESK_<PROVIDER>_KEY`, e.g. `DESK_COINALYZE_KEY`): without a key the tool returns `status: unavailable` — not an error. List which keys are set; see `docs/data-sources.md`.

If this fails, diagnose and fix. Common issues:
- Python version too old → tell user to install Python 3.11+
- Missing system dependencies → platform-specific guidance

### Step 6: Check recommended settings
Read `~/.claude/settings.json` and check if MCP tool permissions are allowed.

If not, show the user what to add and offer to configure it.

### Step 7: Report
Present a clear status report:

```
Crypto Trading Desk - Setup Complete

  OS:           macOS 15.3 (arm64)
  Python:       3.12.8
  uv:           0.6.13
  Dependencies: 5/5 installed
  MCP Servers:  15/15 ready (keys: 2/6 optional set)
  Data dirs:    Created
  Database:     SQLite ready ($20,000 paper trading)

  Ready to use:
    /crypto-trading-desk:quick BTC     (fast market check)
    /crypto-trading-desk:brief         (daily brief)
    /crypto-trading-desk:analyze ETH   (full multi-agent analysis)
```

If anything failed, show exactly what went wrong and how to fix it.
