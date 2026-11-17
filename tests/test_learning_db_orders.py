"""
Tests for crypto_learning_db V2: schema migration, pending orders, watchlist,
regime snapshots, kill switches and nullable-trade predictions.

Every test runs against its own temp SQLite file (DB_DIR/DB_PATH monkeypatched),
never against data/db/learning.db.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers"))
# Safety net if this module is imported first: never discover the real DB.
os.environ.setdefault("CRYPTO_DB_DIR", tempfile.mkdtemp())

import crypto_learning_db as db  # noqa: E402
from helpers import call_tool  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "learning.db")
    return tmp_path / "learning.db"


def _future(days: float = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _p1(**overrides):
    """BTC L1 from data/reports/2026-11-16-BTC/decision.md."""
    kwargs = dict(
        symbol="BTC/USDT", portfolio_type="futures", side="long", usd_amount=500,
        order_type="limit", trigger_price=63500, stop_loss=61600,
        take_profits=[{"price": 67500, "pct": 50}, {"price": 71200, "pct": 50}],
        leverage=3, expires_at=_future(7),
        condition_text="live only while BTC has not daily-closed below 61,600",
        reasoning="support-bounce long", strategy_type="mean_reversion",
        learning={"entry_thesis": "oversold bounce at 63-64.5k",
                  "setup_type": "oversold_bounce", "conviction_level": "low"},
        agent_signals={"polymarket": {"fed_25bp": 0.92}},
        source_report="data/reports/2026-11-16-BTC/decision.md",
    )
    kwargs.update(overrides)
    return call_tool(db.place_order, **kwargs)


def _state():
    return call_tool(db.get_portfolio_state)


# ---------------------------------------------------------------------------
# Migration from the V1 schema
# ---------------------------------------------------------------------------

V1_SCHEMA = """
CREATE TABLE trades (
    id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('long','short')),
    portfolio_type TEXT NOT NULL CHECK(portfolio_type IN ('spot','futures')),
    entry_price REAL NOT NULL, exit_price REAL, usd_amount REAL NOT NULL,
    leverage REAL NOT NULL DEFAULT 1, stop_loss REAL, take_profit REAL,
    strategy_type TEXT, opened_at TEXT NOT NULL, closed_at TEXT, close_reason TEXT,
    pnl_usd REAL, pnl_percent REAL, result TEXT CHECK(result IN ('win','loss',NULL)),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    reasoning TEXT, key_assumptions TEXT, agent_signals TEXT, learning TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_trades_symbol ON trades(symbol);
CREATE TABLE predictions (
    id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, symbol TEXT NOT NULL,
    agent TEXT NOT NULL, prediction_type TEXT NOT NULL, prediction TEXT NOT NULL,
    target_value REAL, timeframe_hours REAL, confidence REAL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','correct','incorrect','expired')),
    actual_outcome TEXT, error_margin REAL, {evaluation}
    created_at TEXT NOT NULL DEFAULT (datetime('now')), validated_at TEXT,
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);
CREATE INDEX idx_predictions_trade ON predictions(trade_id);
CREATE TABLE patterns (
    name TEXT PRIMARY KEY, conditions TEXT, occurrences INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
    win_rate REAL NOT NULL DEFAULT 0, avg_pnl_percent REAL NOT NULL DEFAULT 0,
    first_seen TEXT, last_seen TEXT,
    recommendation TEXT CHECK(recommendation IN ('SEEK','NEUTRAL','AVOID')), notes TEXT
);
CREATE TABLE summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, period TEXT NOT NULL,
    summary_type TEXT NOT NULL, content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE trade_modifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT NOT NULL, field TEXT NOT NULL,
    old_value REAL, new_value REAL, reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);
CREATE TABLE portfolio_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    spot_initial REAL NOT NULL DEFAULT 10000, spot_balance REAL NOT NULL DEFAULT 10000,
    futures_initial REAL NOT NULL DEFAULT 10000, futures_balance REAL NOT NULL DEFAULT 10000,
    currency TEXT NOT NULL DEFAULT 'USDT', total_trades INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
    total_pnl REAL NOT NULL DEFAULT 0, updated_at TEXT
);
"""


def _make_v1_db(path: Path, with_evaluation: bool = True) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(V1_SCHEMA.format(
        evaluation="evaluation TEXT," if with_evaluation else ""))
    conn.execute(
        """INSERT INTO trades (id, symbol, side, portfolio_type, entry_price,
           usd_amount, leverage, stop_loss, take_profit, strategy_type, opened_at,
           status, learning) VALUES
           ('trade_001','BTC/USDT','long','futures',97000,1000,3,95000,101000,
            'swing','2026-10-01T00:00:00+00:00','open','{"setup_type":"swing"}')""")
    eval_col = ", evaluation" if with_evaluation else ""
    eval_val = ", 'close call'" if with_evaluation else ""
    conn.execute(
        f"""INSERT INTO predictions (id, trade_id, symbol, agent, prediction_type,
           prediction, target_value, timeframe_hours, confidence, status,
           actual_outcome, validated_at{eval_col}) VALUES
           ('pred_001','trade_001','BTC/USDT','technical-analyst','price_target',
            'BTC to 101k',101000,72,0.7,'correct','hit 101.2k',
            '2026-10-03T00:00:00+00:00'{eval_val})""")
    conn.execute(
        """INSERT INTO predictions (id, trade_id, symbol, agent, prediction_type,
           prediction) VALUES ('pred_002','trade_001','BTC/USDT','news-sentiment',
           'sentiment','no FUD')""")
    conn.execute(
        "INSERT INTO patterns (name, occurrences, wins, win_rate) VALUES ('p1', 3, 2, 0.67)")
    conn.execute(
        """INSERT INTO trade_modifications (trade_id, field, old_value, new_value, reason)
           VALUES ('trade_001','stop_loss',95000,96000,'trail')""")
    conn.execute(
        """INSERT INTO portfolio_state (id, spot_balance, futures_balance, total_trades)
           VALUES (1, 8000, 9000, 1)""")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("with_evaluation", [True, False])
def test_migrates_v1_db_without_data_loss(temp_db, with_evaluation):
    _make_v1_db(temp_db, with_evaluation)
    conn = db._init_db()
    try:
        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(predictions)")}
        assert cols["trade_id"]["notnull"] == 0
        assert {"source", "order_id", "evaluation"} <= set(cols)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"orders", "watchlist", "regime_snapshots", "kill_switches"} <= tables
        assert "predictions_v2_new" not in tables
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_predictions_trade", "idx_predictions_order"} <= indexes

        p1 = dict(conn.execute("SELECT * FROM predictions WHERE id='pred_001'").fetchone())
        assert p1["trade_id"] == "trade_001"
        assert p1["status"] == "correct"
        assert p1["target_value"] == 101000
        assert p1["actual_outcome"] == "hit 101.2k"
        assert p1["source"] == "trade"
        assert p1["order_id"] is None
        if with_evaluation:
            assert p1["evaluation"] == "close call"
        assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM trade_modifications").fetchone()[0] == 1
        st = dict(conn.execute("SELECT * FROM portfolio_state").fetchone())
        assert (st["spot_balance"], st["futures_balance"]) == (8000, 9000)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()

    # Idempotent: second init changes nothing.
    db._init_db().close()
    assert db._query_predictions(trade_id="trade_001")["predictions"].__len__() == 2

    # New capabilities work on the migrated DB.
    r = call_tool(db.record_prediction, prediction_id="pred_003", trade_id=None,
                  symbol="BTC/USDT", agent="portfolio-manager",
                  prediction_type="price_direction", prediction="WAIT: tags 63.5k first",
                  source="decision")
    assert r["status"] == "success"
    assert _p1()["status"] == "success"
    # Old FK still enforced on trade_id.
    bad = db._record_prediction("pred_004", "trade_999", "BTC/USDT", "x", "t", "p")
    assert bad["status"] == "error"


def test_fresh_db_is_v2_and_idempotent(temp_db):
    db._init_db().close()
    db._init_db().close()
    conn = sqlite3.connect(str(temp_db))
    cols = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(predictions)")}
    conn.close()
    assert cols["trade_id"] == 0
    stats = call_tool(db.get_db_stats)
    for t in ("orders", "watchlist", "regime_snapshots", "kill_switches"):
        assert stats["tables"][t] == 0


# ---------------------------------------------------------------------------
# Predictions without a trade
# ---------------------------------------------------------------------------


def test_prediction_sources_and_inference():
    r = call_tool(db.record_prediction, prediction_id="d1", trade_id=None,
                  symbol="btc/usdt", agent="pm", prediction_type="direction",
                  prediction="WAIT")
    assert r["source"] == "decision" and r["trade_id"] is None
    r = call_tool(db.record_prediction, prediction_id="s1", trade_id="",
                  symbol="HYPE/USDT", agent="quant-scanner",
                  prediction_type="direction", prediction="HYPE bounce", source="scan")
    assert r["status"] == "success" and r["source"] == "scan"
    assert len(call_tool(db.query_predictions, source="scan")["predictions"]) == 1
    assert len(call_tool(db.query_predictions, source="decision")["predictions"]) == 1


@pytest.mark.parametrize("kwargs,msg", [
    (dict(trade_id=None, source="trade"), "requires a trade_id"),
    (dict(trade_id=None, source="order"), "requires an order_id"),
    (dict(trade_id=None, source="rumour"), "source must be one of"),
])
def test_prediction_source_validation(kwargs, msg):
    r = db._record_prediction("x1", symbol="BTC", agent="a", prediction_type="t",
                              prediction="p", **kwargs)
    assert r["status"] == "error" and msg in r["error"]


def test_tradeless_prediction_in_track_record_and_expiry():
    db._record_prediction("d1", None, "BTC/USDT", "pm", "direction", "WAIT",
                          target_value=63500, timeframe_hours=1, source="decision")
    conn = db._init_db()
    conn.execute("UPDATE predictions SET created_at = datetime('now','-3 hours')")
    conn.commit()
    conn.close()
    exp = call_tool(db.find_expired_predictions, current_prices='{"BTC/USDT": 63000}')
    assert exp["total_expired"] == 1
    assert exp["expired_predictions"][0]["source"] == "decision"
    assert exp["expired_predictions"][0]["trade_id"] is None
    db._validate_prediction("d1", "tagged 63.4k", True, evaluation="good wait")
    tr = call_tool(db.get_prediction_track_record, symbol="BTC/USDT")
    assert tr["windows"]["global"]["total"] == 1
    assert tr["windows"]["recent_evaluations"][0]["trade_result"] is None


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


def test_place_order_success_rr_and_ids():
    r = _p1()
    assert r["status"] == "success"
    assert r["order_id"] == "order_001"
    assert r["rr_first_tp"] == pytest.approx(2.11)      # 4000 / 1900
    assert r["rr_blended"] == pytest.approx(3.08)       # 5850 / 1900
    assert r["min_rr"] == 2.0
    assert r["notional_usd"] == 1500
    assert not any("R/R" in w for w in r["warnings"])
    assert _p1()["order_id"] == "order_002"

    o = call_tool(db.list_orders, include_details=True)["orders"][-1]
    assert o["take_profits"] == [{"price": 67500.0, "pct": 50.0},
                                 {"price": 71200.0, "pct": 50.0}]
    assert o["learning"]["setup_type"] == "oversold_bounce"
    assert o["status"] == "pending"
    assert o["condition_text"].startswith("live only")


def test_place_order_short_below_min_rr_warns_but_stores():
    r = call_tool(db.place_order, symbol="BTC/USDT", portfolio_type="futures",
                  side="short", usd_amount=950, trigger_price=71500, stop_loss=74000,
                  take_profits='[{"price": 67000, "pct": 50}, {"price": 62500, "pct": 50}]',
                  leverage=3, expires_at=_future(5))
    assert r["status"] == "success"
    assert r["rr_first_tp"] == 1.8
    assert r["rr_blended"] == 2.7
    assert any("below the futures minimum 2:1" in w and "blended 2.7:1" in w
               for w in r["warnings"])


def test_place_order_spot_min_rr_and_float_tps():
    r = call_tool(db.place_order, symbol="BTC/USDT", portfolio_type="spot", side="long",
                  usd_amount=770, trigger_price=63000, stop_loss=55500,
                  take_profits=[74500, 77400, 81700])
    assert r["status"] == "success"
    assert r["rr_first_tp"] == pytest.approx(1.53)
    assert r["rr_blended"] is None
    assert any("spot minimum 3:1" in w for w in r["warnings"])
    assert any("expires_at" in w for w in r["warnings"])


def test_place_conditional_order_without_trigger():
    r = call_tool(db.place_order, symbol="BTC/USDT", portfolio_type="futures",
                  side="long", usd_amount=400, order_type="conditional",
                  condition_text="daily close > 67000 on >10k BTC volume",
                  stop_loss=64300, leverage=3)
    assert r["status"] == "success"
    assert r["rr_first_tp"] is None


@pytest.mark.parametrize("overrides,msg", [
    (dict(portfolio_type="spot", leverage=2), "SPOT book is always 1x"),
    (dict(leverage=11), "between 1 and 10"),
    (dict(leverage=0.5), "between 1 and 10"),
    (dict(stop_loss=64000), "trigger_price must be above stop_loss"),
    (dict(side="short", stop_loss=61000, take_profits=[60000]),
     "trigger_price must be below stop_loss"),
    (dict(take_profits=[{"price": 62000, "pct": 100}]), "must be above trigger_price"),
    (dict(trigger_price=None), "limit orders need a trigger_price"),
    (dict(order_type="conditional", condition_text=""), "need condition_text"),
    (dict(order_type="market"), "order_type must be one of"),
    (dict(portfolio_type="margin"), "portfolio_type must be"),
    (dict(side="buy"), "side must be"),
    (dict(usd_amount=0), "usd_amount must be > 0"),
    (dict(symbol=""), "symbol is required"),
    (dict(expires_at="2020-01-01T00:00:00Z"), "already in the past"),
    (dict(expires_at="next tuesday"), "not an ISO date"),
    (dict(take_profits=[{"price": 67500, "pct": 70}, {"price": 71200, "pct": 50}]),
     "sum to 120"),
    (dict(take_profits="not json"), "Expecting value"),
])
def test_place_order_validation_errors(overrides, msg):
    r = _p1(**overrides)
    assert r["status"] == "error"
    assert msg in r["error"]
    assert call_tool(db.list_orders)["count"] == 0


def test_place_order_duplicate_custom_id():
    assert _p1(order_id="P1")["order_id"] == "P1"
    r = _p1(order_id="P1")
    assert r["status"] == "error" and "IntegrityError" in r["error"]


def test_place_order_warns_when_book_overcommitted():
    _p1(usd_amount=6000)
    r = _p1(usd_amount=6000)
    assert any("would commit 12,000.00" in w for w in r["warnings"])


# ---------------------------------------------------------------------------
# list_orders / check_orders
# ---------------------------------------------------------------------------


def test_list_orders_filters():
    _p1()
    _p1(symbol="HYPE/USDT", trigger_price=63.5, stop_loss=57.5,
        take_profits=[{"price": 74, "pct": 50}, {"price": 79.7, "pct": 50}], leverage=2)
    call_tool(db.cancel_order, order_id="order_001", reason="test")
    assert call_tool(db.list_orders)["count"] == 1
    assert call_tool(db.list_orders, status="all")["count"] == 2
    assert call_tool(db.list_orders, status="cancelled")["orders"][0]["id"] == "order_001"
    assert call_tool(db.list_orders, status="all", symbol="HYPE")["count"] == 1
    assert "learning" not in call_tool(db.list_orders)["orders"][0]
    assert call_tool(db.list_orders, status="open")["status"] == "error"


def _place(side, order_type, trigger, symbol="BTC/USDT"):
    sl = trigger * (0.95 if side == "long" else 1.05)
    tp = trigger * (1.15 if side == "long" else 0.85)
    r = call_tool(db.place_order, symbol=symbol, portfolio_type="futures", side=side,
                  usd_amount=100, order_type=order_type, trigger_price=trigger,
                  stop_loss=sl, take_profits=[tp], leverage=2)
    assert r["status"] == "success", r
    return r["order_id"]


@pytest.mark.parametrize("side,order_type,trigger,price,hit", [
    ("long", "limit", 63500, 63400, True),
    ("long", "limit", 63500, 63500, True),
    ("long", "limit", 63500, 64000, False),
    ("short", "limit", 71500, 71600, True),
    ("short", "limit", 71500, 70000, False),
    ("long", "stop", 67000, 67100, True),
    ("long", "stop", 67000, 66000, False),
    ("short", "stop", 61000, 60900, True),
    ("short", "stop", 61000, 62000, False),
])
def test_check_orders_trigger_logic(side, order_type, trigger, price, hit):
    oid = _place(side, order_type, trigger)
    r = call_tool(db.check_orders, prices={"BTC/USDT": price})
    assert r["status"] == "success"
    ids_triggered = [o["order_id"] for o in r["triggered"]]
    ids_waiting = [o["order_id"] for o in r["waiting"]]
    if hit:
        assert ids_triggered == [oid]
        assert r["triggered"][0]["suggested_fill_price"] == trigger
    else:
        assert ids_waiting == [oid]
        assert r["waiting"][0]["distance_pct"] == round((price - trigger) / trigger * 100, 2)


def test_check_orders_expiry_conditional_no_price_and_read_only():
    _p1(expires_at=_future(1))                                  # order_001
    call_tool(db.place_order, symbol="ETH/USDT", portfolio_type="futures", side="long",
              usd_amount=100, order_type="conditional", condition_text="reclaim 3000",
              trigger_price=3000, stop_loss=2800)               # order_002
    _place("long", "limit", 60, symbol="SOL/USDT")              # order_003
    now = _future(2)
    r = call_tool(db.check_orders, prices='{"btc": 60000, "ETH/USDT:USDT": 2950}', now=now)
    assert [o["order_id"] for o in r["expired"]] == ["order_001"]
    assert [o["order_id"] for o in r["needs_review"]] == ["order_002"]
    assert r["needs_review"][0]["condition_text"] == "reclaim 3000"
    assert r["no_price"] == ["order_003"]
    assert r["triggered"] == []
    assert "Nothing was changed" in r["interpretation"]
    assert call_tool(db.list_orders)["count"] == 3        # still pending


def test_check_orders_symbol_alias_and_errors():
    oid = _place("long", "limit", 63.5, symbol="HYPE/USDT")
    r = call_tool(db.check_orders, prices={"HYPE": 63.0})
    assert r["triggered"][0]["order_id"] == oid
    assert call_tool(db.check_orders, prices="{bad")["status"] == "error"
    assert call_tool(db.check_orders, prices="[1, 2]")["status"] == "error"


# ---------------------------------------------------------------------------
# fill_order
# ---------------------------------------------------------------------------


def test_fill_order_end_to_end():
    _p1()
    pr = call_tool(db.record_prediction, prediction_id="pred_o1", trade_id=None,
                   symbol="BTC/USDT", agent="technical-analyst",
                   prediction_type="price_target", prediction="tags 67.5k",
                   target_value=67500, timeframe_hours=168, order_id="order_001")
    assert pr["source"] == "order"

    r = call_tool(db.fill_order, order_id="order_001", fill_price=63450)
    assert r["status"] == "success", r
    assert r["trade_id"] == "trade_001"
    assert r["futures_balance_after"] == 9500
    assert r["predictions_linked"] == 1
    assert r["slippage_vs_trigger_pct"] == pytest.approx(-0.079)
    assert r["warnings"] == []

    trade = db._query_trades(status="open")["trades"][0]
    assert trade["id"] == "trade_001"
    assert trade["entry_price"] == 63450
    assert trade["stop_loss"] == 61600
    assert trade["take_profit"] == 67500
    assert trade["leverage"] == 3
    assert trade["usd_amount"] == 500
    assert trade["portfolio_type"] == "futures"
    assert trade["strategy_type"] == "mean_reversion"
    learning = json.loads(trade["learning"])
    assert learning["entry_thesis"].startswith("oversold bounce")
    assert learning["from_order"]["order_id"] == "order_001"
    assert learning["from_order"]["trigger_price"] == 63500
    assert json.loads(trade["agent_signals"])["polymarket"]["fed_25bp"] == 0.92

    order = call_tool(db.list_orders, status="filled")["orders"][0]
    assert order["filled_trade_id"] == "trade_001"
    assert order["fill_price"] == 63450

    pred = db._query_predictions(order_id="order_001")["predictions"][0]
    assert pred["trade_id"] == "trade_001" and pred["source"] == "order"

    st = _state()
    assert st["portfolio"]["total_trades"] == 1
    assert st["open_trades_count"] == 1
    assert st["pending_orders_count"] == 0

    # Filled trade closes through the normal path.
    c = db._close_trade("trade_001", 67500, "TP1")
    assert c["result"] == "win"
    assert c["pnl_usd"] == pytest.approx(500 * (67500 - 63450) / 63450 * 3, abs=0.01)


def test_fill_order_spot_and_trade_id_sequence():
    db._record_trade("trade_001", "ETH/USDT", "long", "spot", 3000, 1000)
    r = call_tool(db.place_order, symbol="BTC/USDT", portfolio_type="spot", side="long",
                  usd_amount=770, trigger_price=63000, stop_loss=55500,
                  take_profits=[{"price": 74500, "pct": 34}])
    f = call_tool(db.fill_order, order_id=r["order_id"], fill_price=63000)
    assert f["trade_id"] == "trade_002"
    assert f["spot_balance_after"] == 10000 - 1000 - 770
    assert _state()["portfolio"]["futures_balance"] == 10000


def test_fill_order_errors_and_atomicity():
    assert call_tool(db.fill_order, order_id="order_404", fill_price=1)["status"] == "error"
    _p1()
    assert "fill_price must be > 0" in call_tool(db.fill_order, order_id="order_001",
                                                 fill_price=0)["error"]
    db._record_trade("trade_001", "ETH/USDT", "long", "futures", 3000, 100)
    bal = _state()["portfolio"]["futures_balance"]
    dup = call_tool(db.fill_order, order_id="order_001", fill_price=63500,
                    trade_id="trade_001")
    assert dup["status"] == "error" and "IntegrityError" in dup["error"]
    assert call_tool(db.list_orders)["orders"][0]["status"] == "pending"
    assert _state()["portfolio"]["futures_balance"] == bal

    assert call_tool(db.fill_order, order_id="order_001", fill_price=63500)["status"] == "success"
    again = call_tool(db.fill_order, order_id="order_001", fill_price=63500)
    assert again["status"] == "error" and "'filled'" in again["error"]


def test_fill_order_warns_beyond_stop():
    _p1()
    r = call_tool(db.fill_order, order_id="order_001", fill_price=61000)
    assert r["status"] == "success"
    assert any("beyond stop_loss" in w for w in r["warnings"])


# ---------------------------------------------------------------------------
# cancel_order / expire_orders
# ---------------------------------------------------------------------------


def test_cancel_order():
    _p1()
    assert "reason is required" in call_tool(db.cancel_order, order_id="order_001",
                                             reason=" ")["error"]
    r = call_tool(db.cancel_order, order_id="order_001",
                  reason="kill switch 1: BTC daily close < 61600")
    assert r["status"] == "success"
    o = call_tool(db.list_orders, status="cancelled")["orders"][0]
    assert o["cancel_reason"].startswith("kill switch 1")
    assert call_tool(db.cancel_order, order_id="order_001", reason="x")["status"] == "error"
    assert call_tool(db.cancel_order, order_id="nope", reason="x")["status"] == "error"


def test_expire_orders():
    _p1(expires_at=_future(1))       # order_001 -> expires
    _p1(expires_at=_future(10))      # order_002 -> stays
    _p1(expires_at=None)             # order_003 -> no expiry
    _p1(expires_at=_future(1))       # order_004 -> cancelled first
    call_tool(db.cancel_order, order_id="order_004", reason="manual")
    r = call_tool(db.expire_orders, now=_future(2))
    assert r["status"] == "success"
    assert [e["order_id"] for e in r["expired"]] == ["order_001"]
    statuses = {o["id"]: o["status"] for o in call_tool(db.list_orders, status="all")["orders"]}
    assert statuses == {"order_001": "expired", "order_002": "pending",
                        "order_003": "pending", "order_004": "cancelled"}
    assert call_tool(db.expire_orders)["expired_count"] == 0     # real now
    assert call_tool(db.expire_orders, now="garbage")["status"] == "error"


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------


def test_watchlist_lifecycle():
    r = call_tool(db.add_watch, symbol="zec/usdt", thesis="privacy rotation",
                  levels={"buy_zone": [800, 850], "invalidation": 700}, bias="long",
                  review_at="2020-01-01")
    assert r["status"] == "success" and r["action"] == "created" and r["bias"] == "bullish"
    added_at = call_tool(db.list_watchlist)["watchlist"][0]["added_at"]

    r = call_tool(db.add_watch, symbol="ZEC/USDT", thesis="updated", bias="neutral",
                  review_at=_future(3))
    assert r["action"] == "updated"
    w = call_tool(db.list_watchlist)
    assert w["count"] == 1
    assert w["watchlist"][0]["added_at"] == added_at
    assert w["watchlist"][0]["thesis"] == "updated"
    assert w["review_due"] == []

    call_tool(db.add_watch, symbol="SOL/USDT", thesis="TVL stabilising",
              levels='{"entry": [67, 71.5]}', review_at="2020-01-01")
    w = call_tool(db.list_watchlist)
    assert w["review_due"] == ["SOL/USDT"]
    sol = [x for x in w["watchlist"] if x["symbol"] == "SOL/USDT"][0]
    assert sol["levels"] == {"entry": [67, 71.5]}

    assert call_tool(db.archive_watch, symbol="sol/usdt")["status"] == "success"
    assert call_tool(db.list_watchlist)["count"] == 1
    assert call_tool(db.list_watchlist, status="archived")["count"] == 1
    assert call_tool(db.list_watchlist, status="all")["count"] == 2
    assert call_tool(db.archive_watch, symbol="DOGE")["status"] == "error"
    # Re-adding reactivates.
    call_tool(db.add_watch, symbol="SOL/USDT", thesis="back")
    assert call_tool(db.list_watchlist)["count"] == 2


def test_watchlist_validation():
    assert call_tool(db.add_watch, symbol="", thesis="x")["status"] == "error"
    assert "bias must be" in call_tool(db.add_watch, symbol="BTC", thesis="x",
                                       bias="moon")["error"]
    assert call_tool(db.add_watch, symbol="BTC", thesis="x",
                     review_at="soon")["status"] == "error"
    assert call_tool(db.list_watchlist, status="weird")["status"] == "error"


# ---------------------------------------------------------------------------
# Regime snapshots
# ---------------------------------------------------------------------------


def test_regime_snapshots_and_history():
    r1 = call_tool(db.record_regime_snapshot, btc_price=64730, regime="risk_off",
                   breadth_score=22, fear_greed=11, btc_funding=0.000045,
                   macro={"fed_25bp_prob": 0.92})
    assert r1["status"] == "success"
    assert r1["previous_regime"] is None and r1["regime_changed"] is False
    r2 = call_tool(db.record_regime_snapshot, btc_price=66000, regime="risk_off")
    assert r2["regime_changed"] is False
    r3 = call_tool(db.record_regime_snapshot, btc_price=68000, regime="neutral",
                   macro='{"dxy": 104}')
    assert r3["previous_regime"] == "risk_off" and r3["regime_changed"] is True

    # An old snapshot outside the window is excluded.
    conn = db._init_db()
    conn.execute(
        "INSERT INTO regime_snapshots (ts, btc_price, regime) VALUES (?,?,?)",
        ((datetime.now(timezone.utc) - timedelta(days=60)).isoformat(), 90000, "risk_on"))
    conn.commit()
    conn.close()

    h = call_tool(db.get_regime_history, days=30)
    assert h["status"] == "success"
    assert h["count"] == 3
    assert h["latest"]["regime"] == "neutral"
    assert h["latest"]["macro"] == {"dxy": 104}
    assert h["snapshots"][0]["regime"] == "neutral"          # newest first
    assert h["snapshots"][-1]["macro"] == {"fed_25bp_prob": 0.92}
    assert [(c["from"], c["to"]) for c in h["regime_changes"]] == [("risk_off", "neutral")]
    assert h["btc_change_pct"] == pytest.approx(5.05)
    assert call_tool(db.get_regime_history, days=90)["count"] == 4


def test_regime_validation():
    assert call_tool(db.record_regime_snapshot, btc_price=64000, regime=" ")["status"] == "error"
    assert call_tool(db.record_regime_snapshot, btc_price=0, regime="x")["status"] == "error"
    assert call_tool(db.record_regime_snapshot, btc_price=1, regime="x",
                     macro="{oops")["status"] == "error"
    assert call_tool(db.get_regime_history)["latest"] is None


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------


def _decision_kill_switches():
    a = call_tool(db.add_kill_switch, rule_text="BTC daily close below 61,600",
                  metric="BTC_daily_close", operator="<", threshold=61600,
                  action_text="close P1/P3, cancel futures longs, re-run analysis",
                  scope="Futures")
    b = call_tool(db.add_kill_switch, rule_text="BTC weekly close below 55,500",
                  metric="btc_weekly_close", operator="<", threshold=55500,
                  action_text="close all spot BTC", scope="spot")
    c = call_tool(db.add_kill_switch, rule_text="BTC daily close above MA200 72,900",
                  metric="btc_daily_close", operator=">=", threshold=72900,
                  action_text="cancel P4", scope="btc/usdt")
    return a, b, c


def test_add_and_list_kill_switches():
    a, b, c = _decision_kill_switches()
    assert a["status"] == "success" and a["kill_switch_id"] == 1
    assert a["metric"] == "btc_daily_close" and a["scope"] == "futures"
    assert c["scope"] == "BTC/USDT"
    ks = call_tool(db.list_kill_switches)
    assert ks["count"] == 3
    assert ks["kill_switches"][1]["threshold"] == 55500


@pytest.mark.parametrize("overrides,msg", [
    (dict(operator="=="), "operator must be one of"),
    (dict(rule_text=""), "rule_text is required"),
    (dict(metric=" "), "metric is required"),
    (dict(action_text=""), "action_text is required"),
    (dict(threshold=None), "threshold is required"),
])
def test_add_kill_switch_validation(overrides, msg):
    kwargs = dict(rule_text="r", metric="m", operator="<", threshold=1, action_text="a")
    kwargs.update(overrides)
    r = call_tool(db.add_kill_switch, **kwargs)
    assert r["status"] == "error" and msg in r["error"]


def test_evaluate_kill_switches():
    _decision_kill_switches()
    r = call_tool(db.evaluate_kill_switches, metrics={"BTC_DAILY_CLOSE": 61200})
    assert r["status"] == "success"
    assert [t["kill_switch_id"] for t in r["triggered"]] == [1]
    assert r["triggered"][0]["action_text"].startswith("close P1/P3")
    assert [c["kill_switch_id"] for c in r["clear"]] == [3]
    assert r["clear"][0]["distance_pct"] == pytest.approx(-16.05)
    assert r["missing_metrics"] == ["btc_weekly_close"]

    # >= boundary, JSON string input
    r = call_tool(db.evaluate_kill_switches,
                  metrics='{"btc_daily_close": 72900, "btc_weekly_close": 70000}')
    assert [t["kill_switch_id"] for t in r["triggered"]] == [3]
    assert r["missing_metrics"] == []

    # Read-only
    assert call_tool(db.list_kill_switches)["count"] == 3
    assert call_tool(db.evaluate_kill_switches, metrics="nope")["status"] == "error"


def test_trigger_and_disable_kill_switch():
    _decision_kill_switches()
    r = call_tool(db.trigger_kill_switch, kill_switch_id=1)
    assert r["status"] == "success" and r["kill_switch_status"] == "triggered"
    assert call_tool(db.trigger_kill_switch, kill_switch_id=1)["status"] == "error"
    assert call_tool(db.trigger_kill_switch, kill_switch_id=99)["status"] == "error"
    trig = call_tool(db.list_kill_switches, status="triggered")["kill_switches"][0]
    assert trig["triggered_at"]
    assert call_tool(db.disable_kill_switch, kill_switch_id=3)["status"] == "success"
    # Only #2 remains active and evaluated.
    r = call_tool(db.evaluate_kill_switches,
                  metrics={"btc_daily_close": 50000, "btc_weekly_close": 50000})
    assert [t["kill_switch_id"] for t in r["triggered"]] == [2]
    assert call_tool(db.list_kill_switches, status="all")["count"] == 3
    assert call_tool(db.list_kill_switches, status="bogus")["status"] == "error"


# ---------------------------------------------------------------------------
# Portfolio state + the real 2026-11-16 decision
# ---------------------------------------------------------------------------


def test_decision_2026_11_16_is_storable_and_monitorable():
    expiry = _future(7)
    p1 = _p1(expires_at=expiry)
    p2 = call_tool(db.place_order, symbol="BTC/USDT", portfolio_type="spot", side="long",
                   usd_amount=770, trigger_price=63000, stop_loss=55500,
                   take_profits=[{"price": 74500, "pct": 33.3}, {"price": 77400, "pct": 33.3},
                                 {"price": 81700, "pct": 33.4}],
                   condition_text="SL on weekly close < 55,500", strategy_type="base_accumulation")
    p3 = call_tool(db.place_order, symbol="HYPE/USDT", portfolio_type="futures", side="long",
                   usd_amount=400, trigger_price=63.5, stop_loss=57.5,
                   take_profits=[{"price": 74.0, "pct": 50}, {"price": 79.7, "pct": 50}],
                   leverage=2, expires_at=expiry)
    p4 = call_tool(db.place_order, symbol="BTC/USDT", portfolio_type="futures", side="short",
                   usd_amount=950, trigger_price=71500, stop_loss=74000,
                   take_profits=[{"price": 67000, "pct": 50}, {"price": 62500, "pct": 50}],
                   leverage=3, condition_text="cancel on daily close > 72,900")
    assert all(x["status"] == "success" for x in (p1, p2, p3, p4))
    assert p3["rr_blended"] == pytest.approx(2.23, abs=0.01)
    _decision_kill_switches()

    st = _state()
    assert st["pending_orders_count"] == 4
    assert st["pending_reserved_usd"] == {"spot": 770.0, "futures": 1850.0}
    assert st["active_kill_switches_count"] == 3
    assert {o["id"] for o in st["pending_orders"]} == {
        "order_001", "order_002", "order_003", "order_004"}
    assert st["portfolio"]["futures_balance"] == 10000      # nothing reserved off-balance

    chk = call_tool(db.check_orders, prices={"BTC/USDT": 63400, "HYPE/USDT": 69.4})
    assert [o["order_id"] for o in chk["triggered"]] == ["order_001"]
    assert {o["order_id"] for o in chk["waiting"]} == {"order_002", "order_003", "order_004"}
