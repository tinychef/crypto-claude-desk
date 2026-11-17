#!/usr/bin/env python3
"""
Crypto Learning DB MCP Server - SQLite-backed cognitive memory.

Replaces unbounded JSON files (portfolio.json, predictions.json,
agent-scorecards.json, patterns.json) with a SQLite database that
supports filtered queries, automatic archival, and summary generation.

This solves the context-window growth problem: agents query only
what they need instead of loading entire files into context.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _discover_db_dir() -> Path:
    """Choose where the SQLite DB lives. Priority:

    1. CRYPTO_DB_DIR env var (explicit override).
    2. CLAUDE_PROJECT_DIR env var (Claude Code sometimes provides the
       user's project root) + /data/db.
    3. Walk up from the original working directory looking for an existing
       ``data/db/learning.db``. This matches a developer running ``claude``
       from a project that already has the DB committed/cached.
    4. Fallback to ``<plugin>/data/db`` — the legacy plugin-local path.

    The discovery is intentionally read-aware: option 3 only matches when
    an existing DB is found, so a brand-new install never accidentally
    creates a DB next to an unrelated project.
    """
    if env := os.environ.get("CRYPTO_DB_DIR"):
        return Path(env)
    if claude_dir := os.environ.get("CLAUDE_PROJECT_DIR"):
        candidate = Path(claude_dir) / "data" / "db"
        if candidate.exists() or (Path(claude_dir) / "data").exists():
            return candidate
    # Walk up from PWD (set by the shell when Claude was launched) looking
    # for an existing data/db/learning.db. Stop after 6 levels to avoid
    # scanning the whole filesystem on misconfigured setups.
    start_pwd = os.environ.get("PWD") or os.environ.get("INIT_CWD")
    if start_pwd:
        cur = Path(start_pwd).resolve()
        for _ in range(6):
            candidate_db = cur / "data" / "db" / "learning.db"
            if candidate_db.exists():
                return candidate_db.parent
            if cur.parent == cur:
                break
            cur = cur.parent
    # Fallback: plugin-local path (legacy default).
    return Path(__file__).resolve().parent.parent / "data" / "db"


DB_DIR = _discover_db_dir()
DB_PATH = DB_DIR / "learning.db"

mcp = FastMCP("crypto-learning-db")

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Predictions DDL is shared by the fresh-schema path and the V1 -> V2 rebuild.
# V2: trade_id is nullable (predictions can come from WAIT/REJECT decisions,
# pending orders or scanner calls), plus ``source`` and ``order_id``.
_PREDICTIONS_COLUMNS_DDL = """
        id              TEXT PRIMARY KEY,
        trade_id        TEXT,  -- NULL for decision/scan/order predictions
        symbol          TEXT NOT NULL,
        agent           TEXT NOT NULL,
        prediction_type TEXT NOT NULL,
        prediction      TEXT NOT NULL,
        target_value    REAL,
        timeframe_hours REAL,
        confidence      REAL,
        status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','correct','incorrect','expired')),
        actual_outcome  TEXT,
        error_margin    REAL,
        evaluation      TEXT,  -- agent's NL analysis of how close the prediction was
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        validated_at    TEXT,
        source          TEXT NOT NULL DEFAULT 'trade'
                        CHECK(source IN ('trade','order','decision','scan')),
        order_id        TEXT,
        FOREIGN KEY (trade_id) REFERENCES trades(id),
        FOREIGN KEY (order_id) REFERENCES orders(id)
"""

PREDICTION_SOURCES = ("trade", "order", "decision", "scan")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS trades (
        id              TEXT PRIMARY KEY,
        symbol          TEXT NOT NULL,
        side            TEXT NOT NULL CHECK(side IN ('long','short')),
        portfolio_type  TEXT NOT NULL CHECK(portfolio_type IN ('spot','futures')),
        entry_price     REAL NOT NULL,
        exit_price      REAL,
        usd_amount      REAL NOT NULL,
        leverage        REAL NOT NULL DEFAULT 1,
        stop_loss       REAL,
        take_profit     REAL,
        strategy_type   TEXT,
        opened_at       TEXT NOT NULL,
        closed_at       TEXT,
        close_reason    TEXT,
        pnl_usd         REAL,
        pnl_percent     REAL,
        result          TEXT CHECK(result IN ('win','loss',NULL)),
        status          TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
        reasoning       TEXT,
        key_assumptions TEXT,  -- JSON array
        agent_signals   TEXT,  -- JSON object
        learning        TEXT,  -- JSON object
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
    CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
    CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_type);

    -- V2: pending limit/stop/conditional orders decided by portfolio-manager.
    CREATE TABLE IF NOT EXISTS orders (
        id              TEXT PRIMARY KEY,          -- order_001, order_002...
        symbol          TEXT NOT NULL,
        portfolio_type  TEXT NOT NULL CHECK(portfolio_type IN ('spot','futures')),
        side            TEXT NOT NULL CHECK(side IN ('long','short')),
        order_type      TEXT NOT NULL DEFAULT 'limit'
                        CHECK(order_type IN ('limit','stop','conditional')),
        trigger_price   REAL,
        condition_text  TEXT,                      -- NL trigger, e.g. "daily close > 67000"
        stop_loss       REAL,
        take_profits    TEXT,                      -- JSON list of {price, pct}
        usd_amount      REAL NOT NULL,             -- capital committed (margin for futures)
        leverage        REAL NOT NULL DEFAULT 1,
        expires_at      TEXT,                      -- ISO UTC
        status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','filled','cancelled','expired')),
        filled_trade_id TEXT,
        fill_price      REAL,
        strategy_type   TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT,
        source_report   TEXT,
        reasoning       TEXT,
        agent_signals   TEXT,                      -- JSON object (copied to the trade)
        learning        TEXT,                      -- JSON object (same shape as trades.learning)
        cancel_reason   TEXT,
        FOREIGN KEY (filled_trade_id) REFERENCES trades(id)
    );

    CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
    CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);

    CREATE TABLE IF NOT EXISTS watchlist (
        symbol          TEXT PRIMARY KEY,
        thesis          TEXT,
        levels          TEXT,                      -- JSON
        bias            TEXT NOT NULL DEFAULT 'neutral'
                        CHECK(bias IN ('bullish','bearish','neutral')),
        added_at        TEXT NOT NULL,
        review_at       TEXT,
        status          TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
        notes           TEXT
    );

    CREATE TABLE IF NOT EXISTS regime_snapshots (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              TEXT NOT NULL,
        btc_price       REAL,
        regime          TEXT NOT NULL,
        breadth_score   REAL,
        fear_greed      REAL,
        btc_funding     REAL,
        macro           TEXT,                      -- JSON
        notes           TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_regime_ts ON regime_snapshots(ts);

    CREATE TABLE IF NOT EXISTS kill_switches (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_text       TEXT NOT NULL,
        metric          TEXT NOT NULL,             -- e.g. btc_daily_close
        operator        TEXT NOT NULL CHECK(operator IN ('<','>','<=','>=')),
        threshold       REAL NOT NULL,
        action_text     TEXT NOT NULL,
        scope           TEXT NOT NULL DEFAULT 'all', -- all | futures | spot | <SYMBOL>
        status          TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','triggered','disabled')),
        created_at      TEXT NOT NULL,
        triggered_at    TEXT
    );

    CREATE TABLE IF NOT EXISTS predictions (""" + _PREDICTIONS_COLUMNS_DDL + """    );

    CREATE TABLE IF NOT EXISTS patterns (
        name            TEXT PRIMARY KEY,
        conditions      TEXT,  -- JSON array
        occurrences     INTEGER NOT NULL DEFAULT 0,
        wins            INTEGER NOT NULL DEFAULT 0,
        losses          INTEGER NOT NULL DEFAULT 0,
        win_rate        REAL NOT NULL DEFAULT 0,
        avg_pnl_percent REAL NOT NULL DEFAULT 0,
        first_seen      TEXT,
        last_seen       TEXT,
        recommendation  TEXT CHECK(recommendation IN ('SEEK','NEUTRAL','AVOID')),
        notes           TEXT
    );

    CREATE TABLE IF NOT EXISTS summaries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        period          TEXT NOT NULL,       -- e.g. '2026-Q1', '2026-02'
        summary_type    TEXT NOT NULL,       -- 'quarterly', 'monthly', 'agent_performance'
        content         TEXT NOT NULL,       -- markdown
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS trade_modifications (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id        TEXT NOT NULL,
        field           TEXT NOT NULL,
        old_value       REAL,
        new_value       REAL,
        reason          TEXT,  -- NL explanation for the change
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (trade_id) REFERENCES trades(id)
    );

    CREATE INDEX IF NOT EXISTS idx_tm_trade ON trade_modifications(trade_id);

    CREATE TABLE IF NOT EXISTS portfolio_state (
        id              INTEGER PRIMARY KEY CHECK(id = 1),
        spot_initial    REAL NOT NULL DEFAULT 10000,
        spot_balance    REAL NOT NULL DEFAULT 10000,
        futures_initial REAL NOT NULL DEFAULT 10000,
        futures_balance REAL NOT NULL DEFAULT 10000,
        currency        TEXT NOT NULL DEFAULT 'USDT',
        total_trades    INTEGER NOT NULL DEFAULT 0,
        wins            INTEGER NOT NULL DEFAULT 0,
        losses          INTEGER NOT NULL DEFAULT 0,
        total_pnl       REAL NOT NULL DEFAULT 0,
        updated_at      TEXT
    );
    """)
    conn.commit()

    # Schema migrations for existing databases.
    for migration in (
        "ALTER TABLE predictions ADD COLUMN evaluation TEXT",
        "DROP TABLE IF EXISTS scorecard_history",
        "DROP TABLE IF EXISTS agent_scorecards",
    ):
        try:
            conn.execute(migration)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column/table already exists or already dropped

    _migrate_predictions_v2(conn)

    # Indexes that depend on V2 columns (must run after the migration).
    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_predictions_trade ON predictions(trade_id);
    CREATE INDEX IF NOT EXISTS idx_predictions_agent ON predictions(agent);
    CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status);
    CREATE INDEX IF NOT EXISTS idx_predictions_order ON predictions(order_id);
    CREATE INDEX IF NOT EXISTS idx_predictions_source ON predictions(source);
    """)

    # Seed default portfolio state if missing.
    cur = conn.execute("SELECT COUNT(*) FROM portfolio_state")
    if cur.fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO portfolio_state(id) VALUES(1)"
        )
    conn.commit()


def _predictions_columns(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["name"]: r for r in conn.execute("PRAGMA table_info(predictions)").fetchall()}


def _migrate_predictions_v2(conn: sqlite3.Connection) -> None:
    """V1 -> V2: make predictions.trade_id nullable, add source + order_id.

    SQLite cannot drop a NOT NULL constraint in place, so a V1 table is
    rebuilt (create new, copy rows, drop old, rename) inside one IMMEDIATE
    transaction with foreign keys off. Idempotent: a V2 table is untouched.
    """
    cols = _predictions_columns(conn)
    if not cols["trade_id"]["notnull"]:
        # Already nullable; only add any missing V2 columns.
        if "source" not in cols:
            conn.execute(
                "ALTER TABLE predictions ADD COLUMN source TEXT NOT NULL DEFAULT 'trade' "
                "CHECK(source IN ('trade','order','decision','scan'))"
            )
        if "order_id" not in cols:
            conn.execute("ALTER TABLE predictions ADD COLUMN order_id TEXT REFERENCES orders(id)")
        conn.commit()
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        cols = _predictions_columns(conn)  # re-check under the write lock
        if cols["trade_id"]["notnull"]:
            conn.execute("DROP TABLE IF EXISTS predictions_v2_new")
            conn.execute("CREATE TABLE predictions_v2_new (" + _PREDICTIONS_COLUMNS_DDL + ")")
            new_cols = [r["name"] for r in conn.execute(
                "PRAGMA table_info(predictions_v2_new)").fetchall()]
            common = ", ".join(c for c in new_cols if c in cols)
            conn.execute(
                f"INSERT INTO predictions_v2_new ({common}) SELECT {common} FROM predictions"
            )
            conn.execute("DROP TABLE predictions")
            conn.execute("ALTER TABLE predictions_v2_new RENAME TO predictions")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _init_db() -> sqlite3.Connection:
    conn = _get_conn()
    _ensure_schema(conn)
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def _rows_to_list(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ===================================================================
# CORE FUNCTIONS (called by both MCP tools and tests)
# ===================================================================


def _insert_trade(
    conn: sqlite3.Connection,
    trade_id: str,
    symbol: str,
    side: str,
    portfolio_type: str,
    entry_price: float,
    usd_amount: float,
    leverage: float = 1,
    stop_loss: float = 0,
    take_profit: float = 0,
    strategy_type: str = "",
    reasoning: str = "",
    key_assumptions: str = "[]",
    agent_signals: str = "{}",
    learning: str = "{}",
) -> None:
    """Insert an open trade and deduct its capital from the book balance.

    Shared by record_trade and fill_order. Does NOT commit: the caller owns
    the transaction (fill_order also flips the order to 'filled' atomically).
    """
    now = _now_iso()
    conn.execute(
        """INSERT INTO trades
           (id, symbol, side, portfolio_type, entry_price, usd_amount,
            leverage, stop_loss, take_profit, strategy_type, opened_at,
            reasoning, key_assumptions, agent_signals, learning, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')""",
        (trade_id, symbol.upper(), side, portfolio_type, entry_price,
         usd_amount, leverage, stop_loss, take_profit, strategy_type,
         now, reasoning, key_assumptions, agent_signals, learning),
    )
    bal_col = "spot_balance" if portfolio_type == "spot" else "futures_balance"
    conn.execute(
        f"UPDATE portfolio_state SET {bal_col} = {bal_col} - ?, "
        "total_trades = total_trades + 1, updated_at = ? WHERE id = 1",
        (usd_amount, now),
    )


def _record_trade(
    trade_id: str,
    symbol: str,
    side: str,
    portfolio_type: str,
    entry_price: float,
    usd_amount: float,
    leverage: float = 1,
    stop_loss: float = 0,
    take_profit: float = 0,
    strategy_type: str = "",
    reasoning: str = "",
    key_assumptions: str = "[]",
    agent_signals: str = "{}",
    learning: str = "{}",
) -> dict:
    conn = _init_db()
    try:
        _insert_trade(conn, trade_id, symbol, side, portfolio_type, entry_price,
                      usd_amount, leverage, stop_loss, take_profit, strategy_type,
                      reasoning, key_assumptions, agent_signals, learning)
        conn.commit()
        return {"status": "success", "trade_id": trade_id, "message": f"Trade {trade_id} recorded"}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


def _close_trade(
    trade_id: str,
    exit_price: float,
    close_reason: str = "",
) -> dict:
    conn = _init_db()
    try:
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ? AND status = 'open'", (trade_id,)
        ).fetchone()
        if not row:
            return {"status": "error", "error": f"No open trade {trade_id}"}
        t = dict(row)
        entry = t["entry_price"]
        leverage = t["leverage"]
        amount = t["usd_amount"]
        if t["side"] == "long":
            pnl_pct = ((exit_price - entry) / entry) * 100 * leverage
        else:
            pnl_pct = ((entry - exit_price) / entry) * 100 * leverage
        pnl_usd = amount * (pnl_pct / 100)
        result = "win" if pnl_usd >= 0 else "loss"
        now = _now_iso()
        conn.execute(
            """UPDATE trades SET exit_price=?, closed_at=?, close_reason=?,
               pnl_usd=?, pnl_percent=?, result=?, status='closed'
               WHERE id=?""",
            (exit_price, now, close_reason, round(pnl_usd, 2),
             round(pnl_pct, 4), result, trade_id),
        )
        bal_col = "spot_balance" if t["portfolio_type"] == "spot" else "futures_balance"
        win_inc = 1 if result == "win" else 0
        loss_inc = 1 if result == "loss" else 0
        conn.execute(
            f"""UPDATE portfolio_state SET
                {bal_col} = {bal_col} + ? + ?,
                wins = wins + ?, losses = losses + ?,
                total_pnl = total_pnl + ?, updated_at = ?
                WHERE id = 1""",
            (amount, round(pnl_usd, 2), win_inc, loss_inc,
             round(pnl_usd, 2), now),
        )
        conn.commit()
        return {
            "status": "success", "trade_id": trade_id,
            "result": result, "pnl_usd": round(pnl_usd, 2),
            "pnl_percent": round(pnl_pct, 4),
        }
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


def _update_trade(
    trade_id: str,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    notes: str = "",
) -> dict:
    """Update SL/TP on an open trade. Logs every change with NL reason."""
    conn = _init_db()
    try:
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ? AND status = 'open'", (trade_id,)
        ).fetchone()
        if not row:
            return {"status": "error", "error": f"No open trade {trade_id}"}
        t = dict(row)
        now = _now_iso()
        changes: list[dict] = []

        if stop_loss is not None and stop_loss != t["stop_loss"]:
            conn.execute(
                "INSERT INTO trade_modifications (trade_id, field, old_value, new_value, reason, created_at) VALUES (?,?,?,?,?,?)",
                (trade_id, "stop_loss", t["stop_loss"], stop_loss, notes, now),
            )
            conn.execute(
                "UPDATE trades SET stop_loss = ? WHERE id = ?",
                (stop_loss, trade_id),
            )
            changes.append({"field": "stop_loss", "old": t["stop_loss"], "new": stop_loss})

        if take_profit is not None and take_profit != t["take_profit"]:
            conn.execute(
                "INSERT INTO trade_modifications (trade_id, field, old_value, new_value, reason, created_at) VALUES (?,?,?,?,?,?)",
                (trade_id, "take_profit", t["take_profit"], take_profit, notes, now),
            )
            conn.execute(
                "UPDATE trades SET take_profit = ? WHERE id = ?",
                (take_profit, trade_id),
            )
            changes.append({"field": "take_profit", "old": t["take_profit"], "new": take_profit})

        if not changes:
            return {"status": "success", "trade_id": trade_id, "message": "No changes needed"}

        conn.commit()
        return {
            "status": "success",
            "trade_id": trade_id,
            "changes": changes,
            "reason": notes,
        }
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


def _get_trade_modifications(trade_id: str = "", limit: int = 20) -> dict:
    """Get modification history for a trade (or all trades)."""
    conn = _init_db()
    try:
        if trade_id:
            rows = conn.execute(
                "SELECT * FROM trade_modifications WHERE trade_id = ? ORDER BY created_at DESC LIMIT ?",
                (trade_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trade_modifications ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {"status": "success", "modifications": _rows_to_list(rows)}
    finally:
        conn.close()


def _query_trades(
    symbol: str = "",
    status: str = "",
    strategy_type: str = "",
    result: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    conn = _init_db()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if status:
            clauses.append("status = ?")
            params.append(status)
        if strategy_type:
            clauses.append("strategy_type = ?")
            params.append(strategy_type)
        if result:
            clauses.append("result = ?")
            params.append(result)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY opened_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM trades {where}", params
        ).fetchone()[0]
        return {"status": "success", "trades": _rows_to_list(rows), "total": total}
    finally:
        conn.close()


def _get_portfolio_state() -> dict:
    conn = _init_db()
    try:
        state = _row_to_dict(
            conn.execute("SELECT * FROM portfolio_state WHERE id = 1").fetchone()
        )
        open_count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'open'"
        ).fetchone()[0]
        open_trades = _rows_to_list(
            conn.execute(
                "SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at DESC"
            ).fetchall()
        )
        pending = _rows_to_list(conn.execute(
            """SELECT id, symbol, portfolio_type, side, order_type, trigger_price,
                      stop_loss, usd_amount, leverage, expires_at
               FROM orders WHERE status = 'pending' ORDER BY created_at"""
        ).fetchall())
        reserved = {"spot": 0.0, "futures": 0.0}
        for o in pending:
            reserved[o["portfolio_type"]] += o["usd_amount"] or 0
        kill_switches = _rows_to_list(conn.execute(
            """SELECT id, metric, operator, threshold, scope
               FROM kill_switches WHERE status = 'active' ORDER BY id"""
        ).fetchall())
        return {
            "status": "success",
            "portfolio": state,
            "open_trades_count": open_count,
            "open_trades": open_trades,
            "pending_orders_count": len(pending),
            "pending_orders": pending,
            "pending_reserved_usd": {k: round(v, 2) for k, v in reserved.items()},
            "active_kill_switches_count": len(kill_switches),
            "active_kill_switches": kill_switches,
        }
    finally:
        conn.close()


def _record_prediction(
    prediction_id: str,
    trade_id: str | None,
    symbol: str,
    agent: str,
    prediction_type: str,
    prediction: str,
    target_value: float = 0,
    timeframe_hours: float = 0,
    confidence: float = 0.5,
    source: str = "",
    order_id: str | None = None,
) -> dict:
    """Record a prediction. trade_id may be None for WAIT/REJECT decisions,
    pending orders or scanner calls. ``source`` defaults to 'trade' when a
    trade_id is given, 'order' when only order_id is given, else 'decision'."""
    trade_id = trade_id or None
    order_id = order_id or None
    if not source:
        source = "trade" if trade_id else ("order" if order_id else "decision")
    source = source.lower()
    if source not in PREDICTION_SOURCES:
        return {"status": "error",
                "error": f"source must be one of {', '.join(PREDICTION_SOURCES)}"}
    if source == "trade" and not trade_id:
        return {"status": "error", "error": "source='trade' requires a trade_id"}
    if source == "order" and not order_id:
        return {"status": "error", "error": "source='order' requires an order_id"}
    conn = _init_db()
    try:
        conn.execute(
            """INSERT INTO predictions
               (id, trade_id, symbol, agent, prediction_type, prediction,
                target_value, timeframe_hours, confidence, created_at,
                source, order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (prediction_id, trade_id, symbol.upper(), agent, prediction_type,
             prediction, target_value, timeframe_hours, confidence, _now_iso(),
             source, order_id),
        )
        conn.commit()
        return {"status": "success", "prediction_id": prediction_id,
                "source": source, "trade_id": trade_id, "order_id": order_id}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


def _validate_prediction(
    prediction_id: str,
    actual_outcome: str,
    is_correct: bool,
    error_margin: float = 0,
    evaluation: str = "",
) -> dict:
    """Validate a prediction. Pure recording — no formula scoring.

    The agent provides:
    - actual_outcome: what happened in the market
    - is_correct: simple boolean for counting
    - error_margin: numerical difference if applicable
    - evaluation: agent's NL reasoning about how close the prediction was,
      why it worked/failed, and what we can learn from it
    """
    conn = _init_db()
    try:
        pred = _row_to_dict(
            conn.execute("SELECT * FROM predictions WHERE id = ?", (prediction_id,)).fetchone()
        )
        if not pred:
            return {"status": "error", "error": f"No prediction {prediction_id}"}
        if pred["status"] != "pending":
            return {"status": "error", "error": f"Prediction {prediction_id} already validated as '{pred['status']}'"}

        new_status = "correct" if is_correct else "incorrect"
        now = _now_iso()
        conn.execute(
            """UPDATE predictions SET status=?, actual_outcome=?,
               error_margin=?, evaluation=?, validated_at=? WHERE id=?""",
            (new_status, actual_outcome, error_margin, evaluation, now,
             prediction_id),
        )

        conn.commit()
        return {
            "status": "success",
            "prediction_id": prediction_id,
            "agent": pred["agent"],
            "result": new_status,
        }
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


def _query_predictions(
    trade_id: str = "",
    agent: str = "",
    status: str = "",
    symbol: str = "",
    limit: int = 20,
    source: str = "",
    order_id: str = "",
) -> dict:
    conn = _init_db()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if trade_id:
            clauses.append("trade_id = ?")
            params.append(trade_id)
        if source:
            clauses.append("source = ?")
            params.append(source.lower())
        if order_id:
            clauses.append("order_id = ?")
            params.append(order_id)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM predictions {where} ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return {"status": "success", "predictions": _rows_to_list(rows)}
    finally:
        conn.close()


def _upsert_pattern(
    name: str,
    conditions: str = "[]",
    is_win: bool = True,
    pnl_percent: float = 0,
    notes: str = "",
) -> dict:
    conn = _init_db()
    try:
        existing = _row_to_dict(
            conn.execute("SELECT * FROM patterns WHERE name = ?", (name,)).fetchone()
        )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if existing:
            occ = existing["occurrences"] + 1
            wins = existing["wins"] + (1 if is_win else 0)
            losses = existing["losses"] + (0 if is_win else 1)
            wr = wins / occ if occ > 0 else 0
            old_avg = existing["avg_pnl_percent"] or 0
            avg_pnl = ((old_avg * existing["occurrences"]) + pnl_percent) / occ
            rec = "SEEK" if wr > 0.6 else ("NEUTRAL" if wr >= 0.4 else "AVOID")
            conn.execute(
                """UPDATE patterns SET occurrences=?, wins=?, losses=?,
                   win_rate=?, avg_pnl_percent=?, last_seen=?,
                   recommendation=?, notes=CASE WHEN ?='' THEN notes ELSE ? END
                   WHERE name=?""",
                (occ, wins, losses, round(wr, 4), round(avg_pnl, 2),
                 now, rec, notes, notes, name),
            )
            if conditions != "[]":
                conn.execute(
                    "UPDATE patterns SET conditions = ? WHERE name = ?",
                    (conditions, name),
                )
        else:
            wr = 1.0 if is_win else 0.0
            rec = "SEEK" if is_win else "AVOID"
            conn.execute(
                """INSERT INTO patterns
                   (name, conditions, occurrences, wins, losses, win_rate,
                    avg_pnl_percent, first_seen, last_seen, recommendation, notes)
                   VALUES (?,?,1,?,?,?,?,?,?,?,?)""",
                (name, conditions, 1 if is_win else 0, 0 if is_win else 1,
                 wr, pnl_percent, now, now, rec,
                 notes or "Initial observation - need more data"),
            )
        conn.commit()
        return {"status": "success", "pattern": name, "action": "updated" if existing else "created"}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


def _query_patterns(
    symbol: str = "",
    min_win_rate: float = 0,
    min_occurrences: int = 0,
    recommendation: str = "",
    limit: int = 10,
) -> dict:
    conn = _init_db()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("conditions LIKE ?")
            params.append(f"%{symbol.upper()}%")
        if min_win_rate > 0:
            clauses.append("win_rate >= ?")
            params.append(min_win_rate)
        if min_occurrences > 0:
            clauses.append("occurrences >= ?")
            params.append(min_occurrences)
        if recommendation:
            clauses.append("recommendation = ?")
            params.append(recommendation.upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM patterns {where} ORDER BY win_rate DESC, occurrences DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return {"status": "success", "patterns": _rows_to_list(rows)}
    finally:
        conn.close()


def _generate_summary(period: str = "", summary_type: str = "monthly") -> dict:
    conn = _init_db()
    try:
        now = datetime.now(timezone.utc)
        if not period:
            if summary_type == "quarterly":
                q = (now.month - 1) // 3 + 1
                period = f"{now.year}-Q{q}"
            else:
                period = now.strftime("%Y-%m")

        # Gather stats for the period
        if summary_type == "quarterly":
            year, qn = period.split("-Q")
            q = int(qn)
            start_month = (q - 1) * 3 + 1
            end_month = q * 3
            date_start = f"{year}-{start_month:02d}-01"
            if end_month == 12:
                date_end = f"{int(year)+1}-01-01"
            else:
                date_end = f"{year}-{end_month+1:02d}-01"
        else:
            date_start = f"{period}-01"
            parts = period.split("-")
            y, m = int(parts[0]), int(parts[1])
            if m == 12:
                date_end = f"{y+1}-01-01"
            else:
                date_end = f"{y}-{m+1:02d}-01"

        trades = _rows_to_list(conn.execute(
            """SELECT * FROM trades WHERE status='closed'
               AND closed_at >= ? AND closed_at < ?
               ORDER BY closed_at""",
            (date_start, date_end),
        ).fetchall())

        total = len(trades)
        wins = sum(1 for t in trades if t["result"] == "win")
        losses = total - wins
        total_pnl = sum(t["pnl_usd"] or 0 for t in trades)
        win_rate = wins / total if total > 0 else 0

        # Agent performance in period (from predictions directly)
        agent_stats = _rows_to_list(conn.execute(
            """SELECT agent, COUNT(*) as total,
                      SUM(CASE WHEN status='correct' THEN 1 ELSE 0 END) as correct
               FROM predictions
               WHERE status IN ('correct','incorrect')
                 AND validated_at >= ? AND validated_at < ?
               GROUP BY agent""",
            (date_start, date_end),
        ).fetchall())

        # Top patterns
        top_patterns = _rows_to_list(conn.execute(
            """SELECT name, occurrences, win_rate, recommendation
               FROM patterns WHERE last_seen >= ? AND last_seen < ?
               ORDER BY occurrences DESC LIMIT 5""",
            (date_start, date_end),
        ).fetchall())

        # Strategy breakdown
        strat_stats = _rows_to_list(conn.execute(
            """SELECT strategy_type,
                      COUNT(*) as total,
                      SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
                      ROUND(AVG(pnl_percent),2) as avg_pnl
               FROM trades WHERE status='closed'
               AND closed_at >= ? AND closed_at < ?
               GROUP BY strategy_type""",
            (date_start, date_end),
        ).fetchall())

        # Build markdown summary
        lines = [
            f"# Trading Summary: {period} ({summary_type})",
            "",
            f"**Total trades:** {total} | **Wins:** {wins} | "
            f"**Losses:** {losses} | **Win Rate:** {win_rate:.0%}",
            f"**Total PnL:** ${total_pnl:+,.2f}",
            "",
            "## Agent Performance",
            "| Agent | Predictions | Correct | Accuracy |",
            "|-------|------------|---------|----------|",
        ]
        for a in agent_stats:
            acc = a["correct"] / a["total"] if a["total"] > 0 else 0
            lines.append(
                f"| {a['agent']} | {a['total']} | {a['correct']} | {acc:.0%} |"
            )
        lines += ["", "## Top Patterns"]
        for p in top_patterns:
            lines.append(
                f"- **{p['name']}**: {p['occurrences']} occurrences, "
                f"{p['win_rate']:.0%} win rate -> {p['recommendation']}"
            )
        lines += ["", "## Strategy Breakdown"]
        for s in strat_stats:
            lines.append(
                f"- **{s['strategy_type'] or 'unknown'}**: {s['total']} trades, "
                f"{s['wins']} wins, avg PnL {s['avg_pnl']}%"
            )

        content = "\n".join(lines)

        # Store summary
        conn.execute(
            "DELETE FROM summaries WHERE period = ? AND summary_type = ?",
            (period, summary_type),
        )
        conn.execute(
            """INSERT INTO summaries (period, summary_type, content, created_at)
               VALUES (?,?,?,?)""",
            (period, summary_type, content, _now_iso()),
        )
        conn.commit()
        return {"status": "success", "period": period, "summary": content}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


def _get_summary(period: str = "", summary_type: str = "monthly") -> dict:
    conn = _init_db()
    try:
        if not period:
            row = conn.execute(
                "SELECT * FROM summaries WHERE summary_type = ? ORDER BY created_at DESC LIMIT 1",
                (summary_type,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM summaries WHERE period = ? AND summary_type = ?",
                (period, summary_type),
            ).fetchone()
        if not row:
            return {"status": "success", "summary": None, "message": "No summary found"}
        return {"status": "success", "summary": _row_to_dict(row)}
    finally:
        conn.close()


def _get_trade_stats(symbol: str = "", strategy_type: str = "") -> dict:
    conn = _init_db()
    try:
        clauses = ["status = 'closed'"]
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if strategy_type:
            clauses.append("strategy_type = ?")
            params.append(strategy_type)
        where = " AND ".join(clauses)

        row = conn.execute(
            f"""SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) as losses,
                    ROUND(AVG(pnl_percent),2) as avg_pnl_pct,
                    ROUND(SUM(pnl_usd),2) as total_pnl,
                    ROUND(AVG(CASE WHEN result='win' THEN pnl_percent END),2) as avg_win_pct,
                    ROUND(AVG(CASE WHEN result='loss' THEN pnl_percent END),2) as avg_loss_pct
                FROM trades WHERE {where}""",
            params,
        ).fetchone()
        stats = _row_to_dict(row) if row else {}
        total = stats.get("total", 0) or 0
        wins = stats.get("wins", 0) or 0
        stats["win_rate"] = round(wins / total, 4) if total > 0 else 0
        return {"status": "success", "stats": stats}
    finally:
        conn.close()


def _migrate_from_json(json_dir: str = "") -> dict:
    if not json_dir:
        json_dir = str(Path(__file__).resolve().parent.parent / "data" / "trades")
    json_path = Path(json_dir)
    conn = _init_db()
    migrated = {"trades": 0, "predictions": 0, "patterns": 0}
    try:
        # Portfolio / trades
        pf = json_path / "portfolio.json"
        if not pf.exists():
            pf = json_path / "portfolio.json.example"
        if pf.exists():
            data = json.loads(pf.read_text())
            portfolios = data.get("portfolios", {})
            spot = portfolios.get("spot", {})
            futures = portfolios.get("futures", {})
            stats = data.get("stats", {})
            conn.execute(
                """UPDATE portfolio_state SET
                   spot_initial=?, spot_balance=?,
                   futures_initial=?, futures_balance=?,
                   total_trades=?, wins=?, losses=?, total_pnl=?, updated_at=?
                   WHERE id=1""",
                (spot.get("initial_balance", 10000), spot.get("current_balance", 10000),
                 futures.get("initial_balance", 10000), futures.get("current_balance", 10000),
                 stats.get("total_trades", 0), stats.get("wins", 0),
                 stats.get("losses", 0), stats.get("total_pnl", 0), _now_iso()),
            )
            for t in data.get("open_trades", []) + data.get("closed_trades", []):
                status = "closed" if t.get("closed_at") else "open"
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO trades
                           (id, symbol, side, portfolio_type, entry_price, exit_price,
                            usd_amount, leverage, stop_loss, take_profit, strategy_type,
                            opened_at, closed_at, close_reason, pnl_usd, pnl_percent,
                            result, status, reasoning, key_assumptions, agent_signals, learning)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (t.get("id"), t.get("symbol"), t.get("side"),
                         t.get("portfolio_type", "spot"), t.get("entry_price"),
                         t.get("exit_price"), t.get("usd_amount"),
                         t.get("leverage", 1), t.get("stop_loss"),
                         t.get("take_profit"), t.get("strategy_type"),
                         t.get("opened_at", _now_iso()), t.get("closed_at"),
                         t.get("close_reason"), t.get("pnl_usd"),
                         t.get("pnl_percent"), t.get("result"), status,
                         t.get("reasoning"),
                         json.dumps(t.get("key_assumptions", [])),
                         json.dumps(t.get("agent_signals", {})),
                         json.dumps(t.get("learning", {}))),
                    )
                    migrated["trades"] += 1
                except sqlite3.IntegrityError:
                    pass

        # Predictions
        pred_file = json_path / "predictions.json"
        if not pred_file.exists():
            pred_file = json_path / "predictions.json.example"
        if pred_file.exists():
            data = json.loads(pred_file.read_text())
            for p in data.get("predictions", []):
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO predictions
                           (id, trade_id, symbol, agent, prediction_type, prediction,
                            target_value, timeframe_hours, confidence, status,
                            actual_outcome, error_margin, created_at, validated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (p.get("id"), p.get("trade_id"), p.get("symbol"),
                         p.get("agent"), p.get("prediction_type"), p.get("prediction"),
                         p.get("target_value"), p.get("timeframe_hours"),
                         p.get("confidence"), p.get("status", "pending"),
                         p.get("actual_outcome"), p.get("error_margin"),
                         p.get("created_at", _now_iso()), p.get("validated_at")),
                    )
                    migrated["predictions"] += 1
                except sqlite3.IntegrityError:
                    pass

        # Patterns
        pat_file = json_path / "patterns.json"
        if not pat_file.exists():
            pat_file = json_path / "patterns.json.example"
        if pat_file.exists():
            data = json.loads(pat_file.read_text())
            for p in data.get("patterns", []):
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO patterns
                           (name, conditions, occurrences, wins, losses, win_rate,
                            avg_pnl_percent, first_seen, last_seen, recommendation, notes)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (p.get("name"), json.dumps(p.get("conditions", [])),
                         p.get("occurrences", 0), p.get("wins", 0),
                         p.get("losses", 0), p.get("win_rate", 0),
                         p.get("avg_pnl_percent", 0), p.get("first_seen"),
                         p.get("last_seen"), p.get("recommendation"),
                         p.get("notes")),
                    )
                    migrated["patterns"] += 1
                except sqlite3.IntegrityError:
                    pass

        conn.commit()
        return {"status": "success", "migrated": migrated}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": str(e)}
    finally:
        conn.close()


def _get_prediction_track_record(
    symbol: str = "",
    strategy_type: str = "",
    prediction_type: str = "",
    agent: str = "",
    days_windows: str = "[7,30,90]",
    include_evaluations: int = 5,
) -> dict:
    """Prediction-centric track record. Queries predictions JOIN trades directly.

    Replaces the old agent-centric get_agent_scorecards, get_recency_scores,
    and get_agent_performance. The key shift: agent is a FILTER, not the
    primary key. The question is "how has this type of setup performed?"
    not "how much do I trust this agent?"

    Args:
        symbol: Filter by symbol (e.g., "BTC/USDT")
        strategy_type: Filter by strategy (e.g., "swing", "breakout")
        prediction_type: Filter by prediction type (e.g., "price_direction")
        agent: Filter by agent (optional — agent is ONE filter, not the key)
        days_windows: JSON array of day windows for time-bucketed accuracy
        include_evaluations: Number of recent NL evaluations to include
    """
    conn = _init_db()
    try:
        windows = json.loads(days_windows) if days_windows else [7, 30, 90]

        # Build base WHERE clause
        base_clauses: list[str] = ["p.status IN ('correct','incorrect')"]
        base_params: list[Any] = []
        if symbol:
            base_clauses.append("p.symbol = ?")
            base_params.append(symbol.upper())
        if strategy_type:
            base_clauses.append("t.strategy_type = ?")
            base_params.append(strategy_type)
        if prediction_type:
            base_clauses.append("p.prediction_type = ?")
            base_params.append(prediction_type)
        if agent:
            base_clauses.append("p.agent = ?")
            base_params.append(agent)

        base_where = " AND ".join(base_clauses)

        # Time-windowed accuracy
        windows_data: dict[str, Any] = {}
        for days in windows:
            r = conn.execute(
                f"""SELECT COUNT(*) as total,
                           SUM(CASE WHEN p.status='correct' THEN 1 ELSE 0 END) as correct
                    FROM predictions p
                    LEFT JOIN trades t ON p.trade_id = t.id
                    WHERE {base_where}
                      AND p.validated_at >= datetime('now', ?)""",
                base_params + [f"-{days} days"],
            ).fetchone()
            total = r[0] or 0
            correct = r[1] or 0
            windows_data[f"{days}d"] = {
                "total": total,
                "correct": correct,
                "accuracy": round(correct / total, 4) if total > 0 else None,
            }

        # Global (all-time)
        r = conn.execute(
            f"""SELECT COUNT(*) as total,
                       SUM(CASE WHEN p.status='correct' THEN 1 ELSE 0 END) as correct
                FROM predictions p
                LEFT JOIN trades t ON p.trade_id = t.id
                WHERE {base_where}""",
            base_params,
        ).fetchone()
        total_all = r[0] or 0
        correct_all = r[1] or 0
        windows_data["global"] = {
            "total": total_all,
            "correct": correct_all,
            "accuracy": round(correct_all / total_all, 4) if total_all > 0 else None,
        }

        # Recent NL evaluations for context
        if include_evaluations > 0:
            evals = _rows_to_list(conn.execute(
                f"""SELECT p.id, p.symbol, p.agent, p.prediction_type,
                           p.prediction, p.actual_outcome, p.evaluation,
                           p.status, p.error_margin, p.validated_at,
                           t.strategy_type, t.result as trade_result
                    FROM predictions p
                    LEFT JOIN trades t ON p.trade_id = t.id
                    WHERE {base_where}
                          AND p.evaluation IS NOT NULL AND p.evaluation != ''
                    ORDER BY p.validated_at DESC LIMIT ?""",
                base_params + [include_evaluations],
            ).fetchall())
            windows_data["recent_evaluations"] = evals

        filters = {}
        if symbol:
            filters["symbol"] = symbol
        if strategy_type:
            filters["strategy_type"] = strategy_type
        if prediction_type:
            filters["prediction_type"] = prediction_type
        if agent:
            filters["agent"] = agent

        return {
            "status": "success",
            "filters": filters,
            "windows": windows_data,
        }
    finally:
        conn.close()


def _find_expired_predictions(current_prices: str = "{}") -> dict:
    """Find predictions past their timeframe and return context for agent evaluation.
    Does NOT auto-validate — returns data so the agent can reason about each one
    and call validate_prediction() with its own credit judgment.

    current_prices: JSON like {"BTC/USDT": 98500, "ETH/USDT": 3200}
    """
    conn = _init_db()
    try:
        prices = json.loads(current_prices) if current_prices else {}

        # Find pending predictions whose timeframe has expired
        rows = conn.execute(
            """SELECT * FROM predictions
               WHERE status = 'pending'
                 AND timeframe_hours > 0
                 AND datetime(created_at, '+' || CAST(timeframe_hours AS INTEGER) || ' hours')
                     < datetime('now')
               ORDER BY created_at""",
        ).fetchall()

        expired = []
        for row in rows:
            p = dict(row)
            symbol = p["symbol"]
            current_price = prices.get(symbol)
            context: dict[str, Any] = {
                "prediction_id": p["id"],
                "trade_id": p["trade_id"],
                "source": p.get("source"),
                "order_id": p.get("order_id"),
                "agent": p["agent"],
                "prediction_type": p["prediction_type"],
                "prediction": p["prediction"],
                "target_value": p["target_value"],
                "timeframe_hours": p["timeframe_hours"],
                "confidence": p["confidence"],
                "created_at": p["created_at"],
                "symbol": symbol,
            }
            if current_price is not None and p["target_value"]:
                target = p["target_value"]
                context["current_price"] = current_price
                context["price_diff_pct"] = round(
                    ((current_price - target) / target) * 100, 2
                )
            expired.append(context)

        # Also find non-price predictions that expired (sentiment, funding, etc.)
        non_price_rows = conn.execute(
            """SELECT * FROM predictions
               WHERE status = 'pending'
                 AND timeframe_hours > 0
                 AND (target_value IS NULL OR target_value = 0)
                 AND datetime(created_at, '+' || CAST(timeframe_hours AS INTEGER) || ' hours')
                     < datetime('now')
               ORDER BY created_at""",
        ).fetchall()

        # These are already included in the main query above,
        # but we tag them so the agent knows they lack price data
        non_price_ids = {dict(r)["id"] for r in non_price_rows}
        for item in expired:
            if item["prediction_id"] in non_price_ids:
                item["has_price_target"] = False
            else:
                item["has_price_target"] = True

        return {
            "status": "success",
            "total_expired": len(expired),
            "expired_predictions": expired,
            "prices_provided": list(prices.keys()),
        }
    finally:
        conn.close()


def _get_db_stats() -> dict:
    conn = _init_db()
    try:
        tables = {}
        for table in ("trades", "predictions", "patterns", "summaries",
                       "trade_modifications", "portfolio_state", "orders",
                       "watchlist", "regime_snapshots", "kill_switches"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            tables[table] = count
        db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        return {
            "status": "success",
            "db_path": str(DB_PATH),
            "db_size_bytes": db_size,
            "db_size_kb": round(db_size / 1024, 1),
            "tables": tables,
        }
    finally:
        conn.close()


# ===================================================================
# V2: ORDERS, WATCHLIST, REGIME SNAPSHOTS, KILL SWITCHES
# ===================================================================

BOOK_MIN_RR = {"spot": 3.0, "futures": 2.0}
ORDER_TYPES = ("limit", "stop", "conditional")
ORDER_STATUSES = ("pending", "filled", "cancelled", "expired")
KILL_SWITCH_STATUSES = ("active", "triggered", "disabled")
_OPERATORS = {
    "<": lambda v, t: v < t,
    ">": lambda v, t: v > t,
    "<=": lambda v, t: v <= t,
    ">=": lambda v, t: v >= t,
}


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO date/datetime (naive = UTC). Raises ValueError if invalid."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_json(value: Any, default: Any) -> Any:
    """Accept a dict/list (native MCP JSON) or a JSON string."""
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _base_symbol(symbol: str) -> str:
    s = str(symbol).upper()
    for sep in ("/", ":", "-"):
        s = s.split(sep)[0]
    return s


def _lookup(mapping: dict, symbol: str) -> float | None:
    """Price lookup tolerant of 'BTC' vs 'BTC/USDT' vs 'btc/usdt:usdt'."""
    upper = {str(k).upper(): v for k, v in mapping.items()}
    if symbol.upper() in upper and upper[symbol.upper()] is not None:
        return float(upper[symbol.upper()])
    base = _base_symbol(symbol)
    for k, v in upper.items():
        if v is not None and _base_symbol(k) == base:
            return float(v)
    return None


def _next_id(conn: sqlite3.Connection, table: str, prefix: str) -> str:
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE id LIKE ?", (f"{prefix}_%",)
    ).fetchall()
    nums = [int(r[0][len(prefix) + 1:]) for r in rows
            if r[0][len(prefix) + 1:].isdigit()]
    return f"{prefix}_{(max(nums) if nums else 0) + 1:03d}"


def _normalize_take_profits(take_profits: Any) -> list[dict]:
    """Return [{price, pct}] from a list of dicts/numbers or a JSON string."""
    raw = _load_json(take_profits, [])
    if isinstance(raw, (dict, int, float)):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("take_profits must be a list of {price, pct}")
    out: list[dict] = []
    for item in raw:
        if isinstance(item, (int, float)):
            price, pct = float(item), None
        elif isinstance(item, dict) and item.get("price") is not None:
            price = float(item["price"])
            pct = float(item["pct"]) if item.get("pct") is not None else None
        else:
            raise ValueError(f"invalid take_profit entry: {item!r}")
        if price <= 0:
            raise ValueError("take_profit prices must be > 0")
        if pct is not None and not 0 < pct <= 100:
            raise ValueError("take_profit pct must be in (0, 100]")
        out.append({"price": price, "pct": pct})
    total_pct = sum(tp["pct"] or 0 for tp in out)
    if total_pct > 100.0001:
        raise ValueError(f"take_profit pct values sum to {total_pct:g} (> 100)")
    return out


def _order_view(row: sqlite3.Row | dict, details: bool = False) -> dict:
    o = dict(row)
    o["take_profits"] = _load_json(o.get("take_profits"), [])
    if details:
        o["learning"] = _load_json(o.get("learning"), {})
        o["agent_signals"] = _load_json(o.get("agent_signals"), {})
    else:
        for k in ("learning", "agent_signals"):
            o.pop(k, None)
    return o


def _place_order(
    symbol: str,
    portfolio_type: str,
    side: str,
    usd_amount: float,
    order_type: str = "limit",
    trigger_price: float | None = None,
    condition_text: str = "",
    stop_loss: float | None = None,
    take_profits: Any = None,
    leverage: float = 1,
    expires_at: str | None = None,
    reasoning: str = "",
    learning: Any = None,
    source_report: str = "",
    strategy_type: str = "",
    agent_signals: Any = None,
    order_id: str = "",
) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    book = (portfolio_type or "").lower()
    side = (side or "").lower()
    order_type = (order_type or "").lower()
    symbol = (symbol or "").strip().upper()

    if not symbol:
        errors.append("symbol is required")
    if book not in ("spot", "futures"):
        errors.append("portfolio_type must be 'spot' or 'futures'")
    if side not in ("long", "short"):
        errors.append("side must be 'long' or 'short'")
    if order_type not in ORDER_TYPES:
        errors.append(f"order_type must be one of {', '.join(ORDER_TYPES)}")
    if usd_amount is None or usd_amount <= 0:
        errors.append("usd_amount must be > 0")
    leverage = 1 if leverage is None else float(leverage)
    if book == "spot" and leverage != 1:
        errors.append("SPOT book is always 1x: leverage must be 1")
    if book == "futures" and not 1 <= leverage <= 10:
        errors.append("FUTURES leverage must be between 1 and 10")
    if trigger_price is not None and trigger_price <= 0:
        errors.append("trigger_price must be > 0")
    if order_type in ("limit", "stop") and trigger_price is None:
        errors.append(f"{order_type} orders need a trigger_price")
    if order_type == "conditional" and not (condition_text or "").strip():
        errors.append("conditional orders need condition_text (the NL trigger)")
    if stop_loss is not None and stop_loss <= 0:
        errors.append("stop_loss must be > 0")
    if stop_loss is not None and trigger_price and side in ("long", "short"):
        if side == "long" and not trigger_price > stop_loss:
            errors.append("long order: trigger_price must be above stop_loss")
        if side == "short" and not trigger_price < stop_loss:
            errors.append("short order: trigger_price must be below stop_loss")
    try:
        tps = _normalize_take_profits(take_profits)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        errors.append(str(e))
        tps = []
    if trigger_price and tps and side in ("long", "short"):
        bad = [tp["price"] for tp in tps
               if (side == "long" and tp["price"] <= trigger_price)
               or (side == "short" and tp["price"] >= trigger_price)]
        if bad:
            where = "above" if side == "long" else "below"
            errors.append(f"{side} order: take_profits must be {where} trigger_price (got {bad})")
    exp_dt = None
    try:
        exp_dt = _parse_ts(expires_at)
    except ValueError:
        errors.append(f"expires_at is not an ISO date/datetime: {expires_at!r}")
    if exp_dt and exp_dt <= datetime.now(timezone.utc):
        errors.append(f"expires_at {exp_dt.isoformat()} is already in the past")
    try:
        learning_obj = _load_json(learning, {})
        signals_obj = _load_json(agent_signals, {})
    except (ValueError, TypeError) as e:
        errors.append(f"learning/agent_signals must be JSON: {e}")
        learning_obj, signals_obj = {}, {}
    if errors:
        return {"status": "error", "error": "; ".join(errors), "errors": errors}

    # R/R from the first TP (and blended if every TP has a pct). Warn, don't block.
    rr_first = rr_blended = None
    min_rr = BOOK_MIN_RR[book]
    if trigger_price and stop_loss and tps:
        risk = abs(trigger_price - stop_loss)
        rr_first = round(abs(tps[0]["price"] - trigger_price) / risk, 2)
        if all(tp["pct"] for tp in tps):
            weight = sum(tp["pct"] for tp in tps)
            reward = sum(abs(tp["price"] - trigger_price) * tp["pct"] for tp in tps) / weight
            rr_blended = round(reward / risk, 2)
        if rr_first < min_rr:
            msg = f"R/R to TP1 {rr_first}:1 is below the {book} minimum {min_rr:g}:1"
            if rr_blended is not None:
                msg += f" (blended {rr_blended}:1)"
            warnings.append(msg)
    if stop_loss is None:
        warnings.append("no stop_loss set: every trade needs an invalidation level")
    if not tps:
        warnings.append("no take_profits set: R/R cannot be computed")
    if not exp_dt:
        warnings.append("no expires_at: the order stays pending until cancelled")

    conn = _init_db()
    try:
        oid = order_id or _next_id(conn, "orders", "order")
        bal_col = "spot_balance" if book == "spot" else "futures_balance"
        balance = conn.execute(
            f"SELECT {bal_col} FROM portfolio_state WHERE id = 1").fetchone()[0]
        reserved = conn.execute(
            "SELECT COALESCE(SUM(usd_amount),0) FROM orders "
            "WHERE status='pending' AND portfolio_type=?", (book,)).fetchone()[0]
        if usd_amount + reserved > balance:
            warnings.append(
                f"pending {book} orders would commit {usd_amount + reserved:,.2f} "
                f"vs {balance:,.2f} available")
        now = _now_iso()
        conn.execute(
            """INSERT INTO orders
               (id, symbol, portfolio_type, side, order_type, trigger_price,
                condition_text, stop_loss, take_profits, usd_amount, leverage,
                expires_at, status, strategy_type, created_at, updated_at,
                source_report, reasoning, agent_signals, learning)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)""",
            (oid, symbol, book, side, order_type, trigger_price,
             condition_text or "", stop_loss, json.dumps(tps), usd_amount,
             leverage, exp_dt.isoformat() if exp_dt else None,
             strategy_type or "", now, now, source_report or "", reasoning or "",
             json.dumps(signals_obj), json.dumps(learning_obj)),
        )
        conn.commit()
        return {
            "status": "success",
            "order_id": oid,
            "symbol": symbol,
            "portfolio_type": book,
            "side": side,
            "order_type": order_type,
            "trigger_price": trigger_price,
            "notional_usd": round(usd_amount * leverage, 2),
            "rr_first_tp": rr_first,
            "rr_blended": rr_blended,
            "min_rr": min_rr,
            "expires_at": exp_dt.isoformat() if exp_dt else None,
            "warnings": warnings,
        }
    except sqlite3.IntegrityError as e:
        conn.rollback()
        return {"status": "error", "error": f"IntegrityError: {e}"}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}
    finally:
        conn.close()


def _list_orders(status: str = "pending", symbol: str | None = None,
                 include_details: bool = False, limit: int = 50) -> dict:
    status = (status or "all").lower()
    if status != "all" and status not in ORDER_STATUSES:
        return {"status": "error",
                "error": f"status must be 'all' or one of {', '.join(ORDER_STATUSES)}"}
    conn = _init_db()
    try:
        if status == "all":
            rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC",
                (status,)).fetchall()
        orders = [_order_view(r, include_details) for r in rows
                  if not symbol or _base_symbol(r["symbol"]) == _base_symbol(symbol)]
        return {"status": "success", "count": len(orders), "orders": orders[:limit]}
    finally:
        conn.close()


def _check_orders(prices: Any, now: str | None = None) -> dict:
    """Which pending orders would trigger at these prices / are past expiry. Read-only."""
    try:
        price_map = _load_json(prices, {})
        now_dt = _parse_ts(now) or datetime.now(timezone.utc)
    except (ValueError, TypeError) as e:
        return {"status": "error", "error": f"invalid input: {e}"}
    if not isinstance(price_map, dict):
        return {"status": "error", "error": "prices must be a {symbol: price} object"}
    conn = _init_db()
    try:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status = 'pending' ORDER BY created_at").fetchall()
    finally:
        conn.close()

    triggered, waiting, expired, needs_review, no_price = [], [], [], [], []
    for r in rows:
        o = dict(r)
        exp = _parse_ts(o["expires_at"]) if o["expires_at"] else None
        if exp and exp <= now_dt:
            expired.append({"order_id": o["id"], "symbol": o["symbol"],
                            "expires_at": o["expires_at"]})
            continue
        price = _lookup(price_map, o["symbol"])
        if price is None:
            no_price.append(o["id"])
            continue
        trig = o["trigger_price"]
        entry = {
            "order_id": o["id"], "symbol": o["symbol"],
            "portfolio_type": o["portfolio_type"], "side": o["side"],
            "order_type": o["order_type"], "trigger_price": trig,
            "current_price": price,
            "distance_pct": round((price - trig) / trig * 100, 2) if trig else None,
        }
        if o["condition_text"]:
            entry["condition_text"] = o["condition_text"]
        if o["order_type"] == "conditional" or not trig:
            needs_review.append(entry)
            continue
        long_ = o["side"] == "long"
        if o["order_type"] == "limit":
            hit = price <= trig if long_ else price >= trig
        else:  # stop: breakout entry
            hit = price >= trig if long_ else price <= trig
        if hit:
            entry["suggested_fill_price"] = trig
            triggered.append(entry)
        else:
            waiting.append(entry)

    parts = [f"{len(triggered)} triggered", f"{len(expired)} expired",
             f"{len(waiting)} waiting"]
    if needs_review:
        parts.append(f"{len(needs_review)} conditional need NL review")
    if no_price:
        parts.append(f"{len(no_price)} without a price")
    return {
        "status": "success",
        "as_of": now_dt.isoformat(),
        "triggered": triggered,
        "expired": expired,
        "needs_review": needs_review,
        "waiting": waiting,
        "no_price": no_price,
        "interpretation": (
            ", ".join(parts) + ". Nothing was changed: verify each triggered order's "
            "condition_text, then call fill_order (suggested_fill_price = the limit/stop "
            "level; use the real price if the market gapped through it) and expire_orders."),
    }


def _fill_order(order_id: str, fill_price: float, trade_id: str = "") -> dict:
    if fill_price is None or fill_price <= 0:
        return {"status": "error", "error": "fill_price must be > 0"}
    conn = _init_db()
    try:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return {"status": "error", "error": f"No order {order_id}"}
        o = dict(row)
        if o["status"] != "pending":
            return {"status": "error",
                    "error": f"Order {order_id} is '{o['status']}'; only pending orders can be filled"}
        warnings: list[str] = []
        now = _now_iso()
        exp = _parse_ts(o["expires_at"]) if o["expires_at"] else None
        if exp and exp <= datetime.now(timezone.utc):
            warnings.append("order is past expires_at; only fill if the fill happened before expiry")
        sl = o["stop_loss"]
        if sl and ((o["side"] == "long" and fill_price <= sl)
                   or (o["side"] == "short" and fill_price >= sl)):
            warnings.append(f"fill_price {fill_price} is already beyond stop_loss {sl}")
        tps = _load_json(o["take_profits"], [])
        tp1 = tps[0]["price"] if tps else 0
        learning = _load_json(o["learning"], {})
        if isinstance(learning, dict):
            learning["from_order"] = {
                "order_id": order_id, "order_type": o["order_type"],
                "trigger_price": o["trigger_price"], "fill_price": fill_price,
                "take_profits": tps, "condition_text": o["condition_text"],
                "source_report": o["source_report"], "placed_at": o["created_at"],
            }
        tid = trade_id or _next_id(conn, "trades", "trade")
        bal_col = "spot_balance" if o["portfolio_type"] == "spot" else "futures_balance"
        balance = conn.execute(
            f"SELECT {bal_col} FROM portfolio_state WHERE id = 1").fetchone()[0]
        if o["usd_amount"] > balance:
            warnings.append(f"usd_amount {o['usd_amount']} exceeds {bal_col} {balance}")

        _insert_trade(
            conn, tid, o["symbol"], o["side"], o["portfolio_type"], fill_price,
            o["usd_amount"], o["leverage"], sl or 0, tp1, o["strategy_type"] or "",
            o["reasoning"] or "", "[]", o["agent_signals"] or "{}", json.dumps(learning),
        )
        conn.execute(
            """UPDATE orders SET status='filled', filled_trade_id=?, fill_price=?,
               updated_at=? WHERE id=?""",
            (tid, fill_price, now, order_id),
        )
        linked = conn.execute(
            "UPDATE predictions SET trade_id = ? WHERE order_id = ? AND trade_id IS NULL",
            (tid, order_id),
        ).rowcount
        conn.commit()
        new_balance = conn.execute(
            f"SELECT {bal_col} FROM portfolio_state WHERE id = 1").fetchone()[0]
        slip = None
        if o["trigger_price"]:
            slip = round((fill_price - o["trigger_price"]) / o["trigger_price"] * 100, 3)
        return {
            "status": "success",
            "order_id": order_id,
            "trade_id": tid,
            "symbol": o["symbol"],
            "portfolio_type": o["portfolio_type"],
            "side": o["side"],
            "fill_price": fill_price,
            "slippage_vs_trigger_pct": slip,
            "stop_loss": sl,
            "take_profit": tp1,
            "take_profits": tps,
            "leverage": o["leverage"],
            "usd_amount": o["usd_amount"],
            f"{bal_col}_after": new_balance,
            "predictions_linked": linked,
            "warnings": warnings,
        }
    except sqlite3.IntegrityError as e:
        conn.rollback()
        return {"status": "error", "error": f"IntegrityError: {e}"}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}
    finally:
        conn.close()


def _cancel_order(order_id: str, reason: str) -> dict:
    if not (reason or "").strip():
        return {"status": "error", "error": "reason is required (it feeds post-mortems)"}
    conn = _init_db()
    try:
        row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return {"status": "error", "error": f"No order {order_id}"}
        if row["status"] != "pending":
            return {"status": "error",
                    "error": f"Order {order_id} is '{row['status']}'; only pending orders can be cancelled"}
        conn.execute(
            "UPDATE orders SET status='cancelled', cancel_reason=?, updated_at=? WHERE id=?",
            (reason.strip(), _now_iso(), order_id),
        )
        conn.commit()
        return {"status": "success", "order_id": order_id, "order_status": "cancelled",
                "reason": reason.strip()}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}
    finally:
        conn.close()


def _expire_orders(now: str | None = None) -> dict:
    try:
        now_dt = _parse_ts(now) or datetime.now(timezone.utc)
    except ValueError as e:
        return {"status": "error", "error": f"invalid now: {e}"}
    conn = _init_db()
    try:
        rows = conn.execute(
            "SELECT id, symbol, expires_at FROM orders "
            "WHERE status = 'pending' AND expires_at IS NOT NULL").fetchall()
        expired = []
        stamp = _now_iso()
        for r in rows:
            if _parse_ts(r["expires_at"]) <= now_dt:
                conn.execute(
                    "UPDATE orders SET status='expired', updated_at=? WHERE id=?",
                    (stamp, r["id"]))
                expired.append({"order_id": r["id"], "symbol": r["symbol"],
                                "expires_at": r["expires_at"]})
        conn.commit()
        return {"status": "success", "as_of": now_dt.isoformat(),
                "expired_count": len(expired), "expired": expired}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}
    finally:
        conn.close()


# ----- Watchlist -----------------------------------------------------------

_BIAS_ALIASES = {"long": "bullish", "short": "bearish", "bull": "bullish", "bear": "bearish"}


def _add_watch(symbol: str, thesis: str, levels: Any = None, bias: str = "neutral",
               review_at: str | None = None, notes: str = "") -> dict:
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {"status": "error", "error": "symbol is required"}
    bias = _BIAS_ALIASES.get((bias or "neutral").lower(), (bias or "neutral").lower())
    if bias not in ("bullish", "bearish", "neutral"):
        return {"status": "error", "error": "bias must be bullish, bearish or neutral"}
    try:
        levels_obj = _load_json(levels, {})
        review_dt = _parse_ts(review_at)
    except (ValueError, TypeError) as e:
        return {"status": "error", "error": f"invalid levels/review_at: {e}"}
    conn = _init_db()
    try:
        exists = conn.execute(
            "SELECT 1 FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
        review = review_dt.isoformat() if review_dt else None
        if exists:
            conn.execute(
                """UPDATE watchlist SET thesis=?, levels=?, bias=?, review_at=?,
                   notes=?, status='active' WHERE symbol=?""",
                (thesis, json.dumps(levels_obj), bias, review, notes, symbol))
        else:
            conn.execute(
                """INSERT INTO watchlist (symbol, thesis, levels, bias, added_at,
                   review_at, status, notes) VALUES (?,?,?,?,?,?,'active',?)""",
                (symbol, thesis, json.dumps(levels_obj), bias, _now_iso(), review, notes))
        conn.commit()
        return {"status": "success", "symbol": symbol,
                "action": "updated" if exists else "created", "bias": bias,
                "review_at": review}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}
    finally:
        conn.close()


def _list_watchlist(status: str = "active") -> dict:
    status = (status or "all").lower()
    if status not in ("all", "active", "archived"):
        return {"status": "error", "error": "status must be active, archived or all"}
    conn = _init_db()
    try:
        if status == "all":
            rows = conn.execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM watchlist WHERE status = ? ORDER BY added_at DESC",
                (status,)).fetchall()
        now = datetime.now(timezone.utc)
        items = []
        for r in rows:
            w = dict(r)
            w["levels"] = _load_json(w["levels"], {})
            w["review_due"] = bool(w["review_at"] and _parse_ts(w["review_at"]) <= now)
            items.append(w)
        return {"status": "success", "count": len(items), "watchlist": items,
                "review_due": [w["symbol"] for w in items if w["review_due"]]}
    finally:
        conn.close()


def _archive_watch(symbol: str) -> dict:
    symbol = (symbol or "").strip().upper()
    conn = _init_db()
    try:
        cur = conn.execute(
            "UPDATE watchlist SET status='archived' WHERE symbol = ?", (symbol,))
        conn.commit()
        if cur.rowcount == 0:
            return {"status": "error", "error": f"{symbol} is not on the watchlist"}
        return {"status": "success", "symbol": symbol, "watch_status": "archived"}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}
    finally:
        conn.close()


# ----- Regime snapshots ----------------------------------------------------


def _record_regime_snapshot(btc_price: float, regime: str,
                            breadth_score: float | None = None,
                            fear_greed: float | None = None,
                            btc_funding: float | None = None,
                            macro: Any = None, notes: str = "") -> dict:
    regime = (regime or "").strip()
    if not regime:
        return {"status": "error", "error": "regime is required"}
    if btc_price is None or btc_price <= 0:
        return {"status": "error", "error": "btc_price must be > 0"}
    try:
        macro_obj = _load_json(macro, {})
    except (ValueError, TypeError) as e:
        return {"status": "error", "error": f"macro must be JSON: {e}"}
    conn = _init_db()
    try:
        prev = conn.execute(
            "SELECT regime, btc_price, ts FROM regime_snapshots ORDER BY ts DESC, id DESC LIMIT 1"
        ).fetchone()
        ts = _now_iso()
        cur = conn.execute(
            """INSERT INTO regime_snapshots (ts, btc_price, regime, breadth_score,
               fear_greed, btc_funding, macro, notes) VALUES (?,?,?,?,?,?,?,?)""",
            (ts, btc_price, regime, breadth_score, fear_greed, btc_funding,
             json.dumps(macro_obj), notes))
        conn.commit()
        return {
            "status": "success", "snapshot_id": cur.lastrowid, "ts": ts,
            "regime": regime,
            "previous_regime": prev["regime"] if prev else None,
            "regime_changed": bool(prev and prev["regime"] != regime),
        }
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}
    finally:
        conn.close()


def _get_regime_history(days: int = 30, limit: int = 60) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _init_db()
    try:
        rows = conn.execute(
            "SELECT * FROM regime_snapshots WHERE ts >= ? ORDER BY ts ASC, id ASC",
            (cutoff,)).fetchall()
    finally:
        conn.close()
    snaps = []
    for r in rows:
        s = dict(r)
        s["macro"] = _load_json(s["macro"], {})
        snaps.append(s)
    changes = [
        {"ts": cur["ts"], "from": prev["regime"], "to": cur["regime"],
         "btc_price": cur["btc_price"]}
        for prev, cur in zip(snaps, snaps[1:]) if prev["regime"] != cur["regime"]
    ]
    btc_change = None
    if len(snaps) >= 2 and snaps[0]["btc_price"]:
        btc_change = round((snaps[-1]["btc_price"] - snaps[0]["btc_price"])
                           / snaps[0]["btc_price"] * 100, 2)
    return {
        "status": "success",
        "days": days,
        "count": len(snaps),
        "latest": snaps[-1] if snaps else None,
        "regime_changes": changes[-10:],
        "btc_change_pct": btc_change,
        "snapshots": list(reversed(snaps))[:limit],  # newest first
    }


# ----- Kill switches -------------------------------------------------------


def _add_kill_switch(rule_text: str, metric: str, operator: str, threshold: float,
                     action_text: str, scope: str = "all") -> dict:
    errors = []
    if not (rule_text or "").strip():
        errors.append("rule_text is required")
    if not (metric or "").strip():
        errors.append("metric is required")
    if operator not in _OPERATORS:
        errors.append("operator must be one of <, >, <=, >=")
    if threshold is None:
        errors.append("threshold is required")
    if not (action_text or "").strip():
        errors.append("action_text is required")
    if errors:
        return {"status": "error", "error": "; ".join(errors), "errors": errors}
    scope = (scope or "all").strip()
    scope = scope.lower() if scope.lower() in ("all", "futures", "spot") else scope.upper()
    conn = _init_db()
    try:
        cur = conn.execute(
            """INSERT INTO kill_switches (rule_text, metric, operator, threshold,
               action_text, scope, status, created_at) VALUES (?,?,?,?,?,?,'active',?)""",
            (rule_text.strip(), metric.strip().lower(), operator, float(threshold),
             action_text.strip(), scope, _now_iso()))
        conn.commit()
        return {"status": "success", "kill_switch_id": cur.lastrowid,
                "metric": metric.strip().lower(), "operator": operator,
                "threshold": float(threshold), "scope": scope}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}
    finally:
        conn.close()


def _list_kill_switches(status: str = "active") -> dict:
    status = (status or "all").lower()
    if status != "all" and status not in KILL_SWITCH_STATUSES:
        return {"status": "error",
                "error": f"status must be 'all' or one of {', '.join(KILL_SWITCH_STATUSES)}"}
    conn = _init_db()
    try:
        if status == "all":
            rows = conn.execute("SELECT * FROM kill_switches ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM kill_switches WHERE status = ? ORDER BY id", (status,)).fetchall()
        return {"status": "success", "count": len(rows), "kill_switches": _rows_to_list(rows)}
    finally:
        conn.close()


def _evaluate_kill_switches(metrics: Any) -> dict:
    """Compare active rules against supplied metric values. Read-only."""
    try:
        raw = _load_json(metrics, {})
    except (ValueError, TypeError) as e:
        return {"status": "error", "error": f"metrics must be JSON: {e}"}
    if not isinstance(raw, dict):
        return {"status": "error", "error": "metrics must be a {metric: value} object"}
    values = {str(k).strip().lower(): v for k, v in raw.items() if v is not None}
    conn = _init_db()
    try:
        rules = _rows_to_list(conn.execute(
            "SELECT * FROM kill_switches WHERE status = 'active' ORDER BY id").fetchall())
    finally:
        conn.close()
    triggered, clear, missing = [], [], set()
    for k in rules:
        if k["metric"] not in values:
            missing.add(k["metric"])
            continue
        value = float(values[k["metric"]])
        item = {"kill_switch_id": k["id"], "rule_text": k["rule_text"],
                "metric": k["metric"], "operator": k["operator"],
                "threshold": k["threshold"], "value": value, "scope": k["scope"]}
        if _OPERATORS[k["operator"]](value, k["threshold"]):
            item["action_text"] = k["action_text"]
            triggered.append(item)
        else:
            item["distance_pct"] = (round((value - k["threshold"]) / k["threshold"] * 100, 2)
                                    if k["threshold"] else None)
            clear.append(item)
    interp = (f"{len(triggered)} of {len(rules)} active kill switches triggered"
              + (f"; no value supplied for {', '.join(sorted(missing))}" if missing else "")
              + ". Nothing was changed: execute each action_text, then trigger_kill_switch(id).")
    return {"status": "success", "triggered": triggered, "clear": clear,
            "missing_metrics": sorted(missing), "interpretation": interp}


def _set_kill_switch_status(kill_switch_id: int, new_status: str) -> dict:
    conn = _init_db()
    try:
        row = conn.execute(
            "SELECT * FROM kill_switches WHERE id = ?", (kill_switch_id,)).fetchone()
        if not row:
            return {"status": "error", "error": f"No kill switch {kill_switch_id}"}
        if row["status"] != "active":
            return {"status": "error",
                    "error": f"Kill switch {kill_switch_id} is already '{row['status']}'"}
        now = _now_iso()
        conn.execute(
            "UPDATE kill_switches SET status = ?, triggered_at = ? WHERE id = ?",
            (new_status, now if new_status == "triggered" else None, kill_switch_id))
        conn.commit()
        return {"status": "success", "kill_switch_id": kill_switch_id,
                "kill_switch_status": new_status, "rule_text": row["rule_text"],
                "action_text": row["action_text"], "scope": row["scope"]}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "error": f"{e.__class__.__name__}: {e}"}
    finally:
        conn.close()


def _trigger_kill_switch(kill_switch_id: int) -> dict:
    return _set_kill_switch_status(kill_switch_id, "triggered")


def _disable_kill_switch(kill_switch_id: int) -> dict:
    return _set_kill_switch_status(kill_switch_id, "disabled")


# ===================================================================
# MCP TOOL WRAPPERS (thin delegation to core functions)
# ===================================================================


@mcp.tool()
def record_trade(trade_id: str, symbol: str, side: str, portfolio_type: str,
                 entry_price: float, usd_amount: float, leverage: float = 1,
                 stop_loss: float = 0, take_profit: float = 0,
                 strategy_type: str = "", reasoning: str = "",
                 key_assumptions: str = "[]", agent_signals: str = "{}",
                 learning: str = "{}") -> dict:
    """Record a new open trade and deduct from portfolio balance."""
    return _record_trade(trade_id, symbol, side, portfolio_type, entry_price,
                         usd_amount, leverage, stop_loss, take_profit,
                         strategy_type, reasoning, key_assumptions,
                         agent_signals, learning)


@mcp.tool()
def close_trade(trade_id: str, exit_price: float,
                close_reason: str = "") -> dict:
    """Close an open trade, calculate PnL, update portfolio balance."""
    return _close_trade(trade_id, exit_price, close_reason)


@mcp.tool()
def update_trade(trade_id: str, stop_loss: float | None = None,
                 take_profit: float | None = None, notes: str = "") -> dict:
    """Update SL/TP on an open trade. Every change is logged with the reason.
    Use for trailing stops, TP adjustments, or risk management changes.
    The agent should explain WHY in the notes field."""
    return _update_trade(trade_id, stop_loss, take_profit, notes)


@mcp.tool()
def get_trade_modifications(trade_id: str = "", limit: int = 20) -> dict:
    """Get the modification history for a trade. Shows all SL/TP changes
    with timestamps and NL reasons. Useful for post-mortem analysis."""
    return _get_trade_modifications(trade_id, limit)


@mcp.tool()
def query_trades(symbol: str = "", status: str = "", strategy_type: str = "",
                 result: str = "", limit: int = 20, offset: int = 0) -> dict:
    """Query trades with optional filters. Returns only matching rows, not everything."""
    return _query_trades(symbol, status, strategy_type, result, limit, offset)


@mcp.tool()
def get_portfolio_state() -> dict:
    """Get current portfolio balances and overall stats."""
    return _get_portfolio_state()


@mcp.tool()
def record_prediction(prediction_id: str, trade_id: str | None, symbol: str,
                      agent: str, prediction_type: str, prediction: str,
                      target_value: float = 0, timeframe_hours: float = 0,
                      confidence: float = 0.5, source: str = "",
                      order_id: str | None = None) -> dict:
    """Record a testable prediction from an agent.
    trade_id may be null for calls that did not open a trade: source='decision'
    (WAIT/REJECT verdicts), 'scan' (scanner calls) or 'order' (pass order_id of a
    pending order; its predictions are attached to the trade when the order fills).
    source defaults to 'trade' if trade_id is set, 'order' if only order_id, else 'decision'."""
    return _record_prediction(prediction_id, trade_id, symbol, agent,
                              prediction_type, prediction, target_value,
                              timeframe_hours, confidence, source, order_id)


@mcp.tool()
def validate_prediction(prediction_id: str, actual_outcome: str,
                        is_correct: bool, error_margin: float = 0,
                        evaluation: str = "") -> dict:
    """Validate a prediction. Pure recording — no formula scoring.
    The agent writes a natural language evaluation explaining how close
    the prediction was, why it worked/failed, and lessons learned.
    This evaluation is stored and available for future agents to read."""
    return _validate_prediction(prediction_id, actual_outcome, is_correct,
                                error_margin, evaluation)


@mcp.tool()
def query_predictions(trade_id: str = "", agent: str = "", status: str = "",
                      symbol: str = "", limit: int = 20, source: str = "",
                      order_id: str = "") -> dict:
    """Query predictions with optional filters (source: trade/order/decision/scan)."""
    return _query_predictions(trade_id, agent, status, symbol, limit, source, order_id)


@mcp.tool()
def get_prediction_track_record(symbol: str = "", strategy_type: str = "",
                                prediction_type: str = "", agent: str = "",
                                days_windows: str = "[7,30,90]",
                                include_evaluations: int = 5) -> dict:
    """Prediction-centric track record. Queries predictions JOIN trades directly.
    The key question: "how has this type of setup performed?" — not "how much
    do I trust this agent?" Agent is ONE filter, not the primary key.
    Returns accuracy by time window (7d, 30d, 90d, global) plus recent NL
    evaluations for context. Use this to assess setup reliability before trading."""
    return _get_prediction_track_record(symbol, strategy_type, prediction_type,
                                        agent, days_windows, include_evaluations)


@mcp.tool()
def upsert_pattern(name: str, conditions: str = "[]", is_win: bool = True,
                   pnl_percent: float = 0, notes: str = "") -> dict:
    """Create or update a trading pattern after a trade closes."""
    return _upsert_pattern(name, conditions, is_win, pnl_percent, notes)


@mcp.tool()
def query_patterns(symbol: str = "", min_win_rate: float = 0,
                   min_occurrences: int = 0, recommendation: str = "",
                   limit: int = 10) -> dict:
    """Query patterns with filters. Agents use this instead of reading the entire file."""
    return _query_patterns(symbol, min_win_rate, min_occurrences,
                           recommendation, limit)


@mcp.tool()
def generate_summary(period: str = "", summary_type: str = "monthly") -> dict:
    """Generate and store a summary of trading activity for a period.
    If period is empty, defaults to current month (YYYY-MM) or quarter (YYYY-QN)."""
    return _generate_summary(period, summary_type)


@mcp.tool()
def get_summary(period: str = "", summary_type: str = "monthly") -> dict:
    """Retrieve a previously generated summary."""
    return _get_summary(period, summary_type)


@mcp.tool()
def get_trade_stats(symbol: str = "", strategy_type: str = "") -> dict:
    """Get aggregated trade statistics with optional filters."""
    return _get_trade_stats(symbol, strategy_type)


@mcp.tool()
def migrate_from_json(json_dir: str = "") -> dict:
    """Migrate data from JSON files (portfolio.json, predictions.json, etc.) into SQLite.
    Only needs to be run once. Safe to re-run (skips existing records)."""
    return _migrate_from_json(json_dir)


@mcp.tool()
def find_expired_predictions(current_prices: str = "{}") -> dict:
    """Find predictions past their timeframe and return context for evaluation.
    Does NOT auto-validate. Returns each expired prediction with target vs current
    price context so the agent can reason about credit and call validate_prediction().
    current_prices: JSON like '{"BTC/USDT": 98500, "ETH/USDT": 3200}'"""
    return _find_expired_predictions(current_prices)


@mcp.tool()
def get_db_stats() -> dict:
    """Get database size and row counts for monitoring."""
    return _get_db_stats()


# ----- V2 tools: orders, watchlist, regime, kill switches -----------------


@mcp.tool()
def place_order(symbol: str, portfolio_type: str, side: str, usd_amount: float,
                order_type: str = "limit", trigger_price: float | None = None,
                condition_text: str = "", stop_loss: float | None = None,
                take_profits: list | str | None = None, leverage: float = 1,
                expires_at: str | None = None, reasoning: str = "",
                learning: dict | str | None = None, source_report: str = "",
                strategy_type: str = "", agent_signals: dict | str | None = None,
                order_id: str = "") -> dict:
    """Store a pending order (WAIT-with-a-limit decisions) instead of a trade.
    order_type: limit (long fills at price <= trigger, short at >=), stop (breakout:
    long fills at price >= trigger, short at <=) or conditional (NL condition_text,
    reviewed by the agent). usd_amount = capital committed (margin for futures;
    notional = usd_amount * leverage). take_profits: [{"price": 67500, "pct": 50}, ...].
    expires_at: ISO UTC. learning/agent_signals: same JSON as record_trade, copied to the
    trade on fill. Book rules enforced: spot 1x, futures 1-10x, SL on the losing side.
    Returns order_id (order_001...), rr_first_tp / rr_blended and warnings
    (R/R below 3:1 spot or 2:1 futures warns but does not block)."""
    return _place_order(symbol, portfolio_type, side, usd_amount, order_type,
                        trigger_price, condition_text, stop_loss, take_profits,
                        leverage, expires_at, reasoning, learning, source_report,
                        strategy_type, agent_signals, order_id)


@mcp.tool()
def list_orders(status: str = "pending", symbol: str | None = None,
                include_details: bool = False) -> dict:
    """List orders by status (pending/filled/cancelled/expired/all), optionally for one
    symbol ('BTC' matches 'BTC/USDT'). include_details adds learning + agent_signals."""
    return _list_orders(status, symbol, include_details)


@mcp.tool()
def check_orders(prices: dict | str, now: str | None = None) -> dict:
    """Read-only check of pending orders against current prices, e.g.
    {"BTC/USDT": 63400, "HYPE": 64.1}. Returns triggered (with suggested_fill_price),
    expired (past expires_at), needs_review (conditional orders: judge condition_text),
    waiting (with distance_pct to trigger) and no_price. Does NOT change anything:
    call fill_order / expire_orders afterwards. Used by /monitor."""
    return _check_orders(prices, now)


@mcp.tool()
def fill_order(order_id: str, fill_price: float, trade_id: str = "") -> dict:
    """Fill a pending order: creates the open trade (same path as record_trade: SL,
    first TP as take_profit, leverage, reasoning, agent_signals, learning + a
    learning.from_order block), deducts the book balance, marks the order filled and
    attaches the order's predictions to the new trade. trade_id auto-generated
    (trade_NNN) if empty. Record the trade's own predictions afterwards."""
    return _fill_order(order_id, fill_price, trade_id)


@mcp.tool()
def cancel_order(order_id: str, reason: str) -> dict:
    """Cancel a pending order. reason is required (e.g. 'kill switch 1: BTC daily
    close < 61600') and is kept for post-mortems."""
    return _cancel_order(order_id, reason)


@mcp.tool()
def expire_orders(now: str | None = None) -> dict:
    """Mark every pending order whose expires_at is past (or <= now, ISO) as expired.
    Returns the expired order ids."""
    return _expire_orders(now)


@mcp.tool()
def add_watch(symbol: str, thesis: str, levels: dict | list | str | None = None,
              bias: str = "neutral", review_at: str | None = None,
              notes: str = "") -> dict:
    """Add (or update/reactivate) a watchlist entry: thesis, key levels JSON
    (e.g. {"buy_zone": [62, 66], "invalidation": 57.5}), bias bullish/bearish/neutral,
    review_at ISO date."""
    return _add_watch(symbol, thesis, levels, bias, review_at, notes)


@mcp.tool()
def list_watchlist(status: str = "active") -> dict:
    """List watchlist entries (active/archived/all). review_due lists the symbols whose
    review_at has passed."""
    return _list_watchlist(status)


@mcp.tool()
def archive_watch(symbol: str) -> dict:
    """Archive a watchlist entry (thesis played out or invalidated)."""
    return _archive_watch(symbol)


@mcp.tool()
def record_regime_snapshot(btc_price: float, regime: str,
                           breadth_score: float | None = None,
                           fear_greed: float | None = None,
                           btc_funding: float | None = None,
                           macro: dict | str | None = None,
                           notes: str = "") -> dict:
    """Store a market-regime snapshot (e.g. regime='risk_off_downtrend', breadth 0-100,
    F&G 0-100, BTC funding rate, macro JSON like {"dxy": 106.2, "fed_hike_prob": 0.92}).
    Returns previous_regime and regime_changed."""
    return _record_regime_snapshot(btc_price, regime, breadth_score, fear_greed,
                                   btc_funding, macro, notes)


@mcp.tool()
def get_regime_history(days: int = 30) -> dict:
    """Regime snapshots for the last N days (newest first, max 60), the latest snapshot,
    regime_changes (transitions) and BTC % change over the window."""
    return _get_regime_history(days)


@mcp.tool()
def add_kill_switch(rule_text: str, metric: str, operator: str, threshold: float,
                    action_text: str, scope: str = "all") -> dict:
    """Store a machine-checkable kill switch, e.g. rule_text='BTC daily close below
    61,600', metric='btc_daily_close', operator='<', threshold=61600,
    action_text='close P1/P3, cancel futures longs, re-run full analysis',
    scope all/futures/spot/<SYMBOL>."""
    return _add_kill_switch(rule_text, metric, operator, threshold, action_text, scope)


@mcp.tool()
def list_kill_switches(status: str = "active") -> dict:
    """List kill switches by status (active/triggered/disabled/all)."""
    return _list_kill_switches(status)


@mcp.tool()
def evaluate_kill_switches(metrics: dict | str) -> dict:
    """Read-only: compare active kill switches with metric values, e.g.
    {"btc_daily_close": 61200, "futures_realized_loss": 150}. Returns triggered rules
    (with action_text), clear rules (distance_pct) and missing_metrics. Does NOT change
    anything: act, then call trigger_kill_switch(id)."""
    return _evaluate_kill_switches(metrics)


@mcp.tool()
def trigger_kill_switch(kill_switch_id: int) -> dict:
    """Mark an active kill switch as triggered (after its action was executed)."""
    return _trigger_kill_switch(kill_switch_id)


@mcp.tool()
def disable_kill_switch(kill_switch_id: int) -> dict:
    """Disable an active kill switch that no longer applies (e.g. its trade closed)."""
    return _disable_kill_switch(kill_switch_id)



if __name__ == "__main__":
    mcp.run()
