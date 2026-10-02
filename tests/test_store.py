"""持久化：老库升级、随机轮换的抽签落库、成交账本的汇总口径。"""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from parallax_hedge.store import Store, summarize_fills

# 升级前线上库的 tasks 表结构（原样照抄）。线上库里有真实持仓状态，
# 升级必须只加列、不丢数据。
OLD_TASKS_SCHEMA = """
CREATE TABLE tasks (
    asset               TEXT PRIMARY KEY,
    enabled             INTEGER NOT NULL DEFAULT 0,
    leverage            REAL    NOT NULL,
    rotation_hours      REAL    NOT NULL,
    notional_usdc       REAL,
    opened_at           REAL,
    corridor_at_open    REAL,
    last_closed_at      REAL,
    open_direction      TEXT,
    open_quantity       REAL,
    created_at          REAL    NOT NULL,
    updated_at          REAL    NOT NULL
);
"""


def fresh(tmp):
    return Store(Path(tmp) / "t.db")


def test_old_database_gains_new_columns_without_losing_the_open_position():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "hedge.db"
        conn = sqlite3.connect(path)
        conn.executescript(OLD_TASKS_SCHEMA)
        conn.execute(
            "INSERT INTO tasks VALUES ('OAI',1,6.0,0.5,200.0,1789783062.9,7.68,NULL,"
            "'long_lighter_short_arcus',0.126,1789752908.0,1789783062.9)"
        )
        conn.commit()
        conn.close()

        store = Store(path)
        task = store.get_task("OAI")
        assert task["opened_at"] == pytest.approx(1789783062.9)
        assert task["open_quantity"] == pytest.approx(0.126)
        assert task["rotation_hours"] == pytest.approx(0.5)
        assert task["rotation_hours_max"] is None       # 老任务 = 固定周期，行为不变
        assert task["hold_hours"] is None
        store.record_fill(asset="OAI", venue="arcus", action="open", side="sell",
                          dry_run=False, market_id=None, order_ref="1",
                          requested_qty=0.1, quantity=0.1, price=100.0,
                          source="receipt", status="pending")
        store.close()
        # 再开一次也不能出错（迁移必须是幂等的）
        Store(path).close()


def test_the_drawn_hold_time_is_stored_at_open_and_cleared_at_close():
    with tempfile.TemporaryDirectory() as tmp:
        store = fresh(tmp)
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=1.0,
                          rotation_hours_max=2.0)
        store.mark_opened("OAI", direction="long_lighter_short_arcus",
                          quantity=0.1, corridor_pct=8.0, hold_hours=1.37)
        assert store.get_task("OAI")["hold_hours"] == pytest.approx(1.37)
        store.mark_closed("OAI")
        task = store.get_task("OAI")
        assert task["hold_hours"] is None and task["opened_at"] is None
        assert task["rotation_hours_max"] == pytest.approx(2.0)     # 设置不受影响


def test_log_cycle_returns_the_row_id():
    with tempfile.TemporaryDirectory() as tmp:
        store = fresh(tmp)
        a = store.log_cycle("OAI", "open", "x")
        b = store.log_cycle("OAI", "close", "y")
        assert b == a + 1


def _fill(store, **kw):
    base = dict(asset="OAI", venue="lighter", action="open", side="buy", dry_run=False,
                market_id=42, order_ref=None, requested_qty=0.1, quantity=0.1,
                price=100.0, source="position", status="pending")
    base.update(kw)
    return store.record_fill(**base)


def test_the_same_order_is_only_booked_once():
    """回填和实时记录会重叠，同一个订单号只能记一次。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = fresh(tmp)
        assert _fill(store, order_ref="abc") is not None
        assert _fill(store, order_ref="abc", action="rescue") is None
        assert _fill(store, venue="arcus", order_ref="abc") is not None   # 不同所可以同号
        assert store.fill_stats(dry_run=False)["total"]["fills"] == 2


def test_pending_rows_are_settled_or_given_up():
    with tempfile.TemporaryDirectory() as tmp:
        store = fresh(tmp)
        a = _fill(store, order_ref="a", created_at=100.0)
        b = _fill(store, order_ref="b", created_at=100.0)
        _fill(store, order_ref="c", created_at=10_000.0)
        assert {r["order_ref"] for r in store.pending_fills(older_than=200.0)} == {"a", "b"}
        store.settle_fill(a, quantity=0.1, price=101.0, fee=0.02, realized_pnl=0.0, side="buy")
        store.give_up_fill(b, "对不上")
        assert {r["order_ref"] for r in store.pending_fills(older_than=20_000.0)} == {"c"}
        stats = store.fill_stats(dry_run=False)["venues"]["lighter"]
        assert stats["pending"] == 1 and stats["estimated"] == 1


def test_volume_and_fees_add_up_per_venue_and_in_total():
    rows = [
        {"asset": "OAI", "venue": "lighter", "action": "open", "quantity": 0.1, "price": 1000.0,
         "fee": 0.0, "realized_pnl": 0.0, "status": "final", "created_at": 1.0},
        {"asset": "OAI", "venue": "arcus", "action": "open", "quantity": 0.1, "price": 998.0,
         "fee": 0.03, "realized_pnl": 0.0, "status": "final", "created_at": 2.0},
        {"asset": "ANTH", "venue": "arcus", "action": "open", "quantity": 0.2, "price": 2000.0,
         "fee": 0.05, "realized_pnl": 0.0, "status": "final", "created_at": 3.0},
        # 失败的尝试：Lighter 成交了、Arcus 没成交（随后被抢救平掉）
        {"asset": "SNDK", "venue": "lighter", "action": "open", "quantity": 0.01, "price": 1700.0,
         "fee": 0.0, "realized_pnl": 0.0, "status": "final", "created_at": 4.0},
    ]
    s = summarize_fills(rows)
    assert s["venues"]["lighter"]["volume"] == pytest.approx(100.0 + 17.0)
    assert s["venues"]["arcus"]["volume"] == pytest.approx(99.8 + 400.0)
    assert s["total"]["volume"] == pytest.approx(616.8)
    assert s["total"]["fees"] == pytest.approx(0.08)
    assert s["by_asset"]["OAI"]["total"]["volume"] == pytest.approx(199.8)
    assert s["by_asset"]["OAI"]["opens"] == 1
    assert s["by_asset"]["ANTH"]["opens"] == 1
    assert s["by_asset"]["SNDK"]["opens"] == 0          # 失败的尝试不算一次开仓
    assert s["since"] == pytest.approx(1.0)


def test_net_pnl_is_withheld_until_both_venues_closing_fills_are_known():
    """对冲的两条腿一赚一亏。只拿到 Arcus 那边就算净损益，
    会把一轮亏损显示成「赚了」—— 一夜实盘里 ANTH 的 Arcus 腿就是 +4.40。"""
    open_legs = [
        {"asset": "ANTH", "venue": "lighter", "action": "open", "quantity": 0.23, "price": 2180.0,
         "fee": 0.0, "realized_pnl": None, "status": "pending"},
        {"asset": "ANTH", "venue": "arcus", "action": "open", "quantity": 0.23, "price": 2172.0,
         "fee": 0.04, "realized_pnl": 0.0, "status": "final"},
    ]
    arcus_close = {"asset": "ANTH", "venue": "arcus", "action": "close", "quantity": 0.23,
                     "price": 2165.0, "fee": 0.04, "realized_pnl": 1.61, "status": "final"}
    lighter_close = {"asset": "ANTH", "venue": "lighter", "action": "close", "quantity": 0.23,
                     "price": 2170.0, "fee": 0.0, "realized_pnl": None, "status": "pending"}

    half = summarize_fills(open_legs + [arcus_close, lighter_close])
    assert half["total"]["net_pnl"] is None
    assert half["total"]["wear_per_10k"] is None
    assert half["total"]["pnl_unknown"] == 1

    lighter_close = {**lighter_close, "realized_pnl": -2.30, "status": "final"}
    full = summarize_fills(open_legs + [arcus_close, lighter_close])
    assert full["total"]["net_pnl"] == pytest.approx(1.61 - 2.30 - 0.08)
    assert full["total"]["wear_per_10k"] == pytest.approx(
        0.77 / full["total"]["volume"] * 10_000)


def test_an_unknown_fee_also_withholds_net_pnl():
    rows = [{"asset": "OAI", "venue": "arcus", "action": "close", "quantity": 0.1,
             "price": 100.0, "fee": None, "realized_pnl": 0.5, "status": "pending"}]
    assert summarize_fills(rows)["total"]["net_pnl"] is None


def test_zero_quantity_rows_do_not_count_as_fills():
    """IOC 零成交的单子会在账本里留一行（数量 0），不能算进成交笔数或交易量。"""
    rows = [{"asset": "OAI", "venue": "lighter", "action": "open", "quantity": 0.0,
             "price": 100.0, "fee": None, "realized_pnl": None, "status": "estimated"}]
    s = summarize_fills(rows)
    assert s["total"]["fills"] == 0 and s["total"]["volume"] == 0
    assert s["total"]["fee_unknown"] == 0       # 没成交就谈不上手续费未知


def test_simulated_and_live_fills_never_mix():
    with tempfile.TemporaryDirectory() as tmp:
        store = fresh(tmp)
        _fill(store, dry_run=True, source="simulated", status="final")
        _fill(store, order_ref="x")
        assert store.fill_stats(dry_run=True)["total"]["fills"] == 1
        assert store.fill_stats(dry_run=False)["total"]["fills"] == 1


def test_meta_is_a_simple_key_value_store():
    with tempfile.TemporaryDirectory() as tmp:
        store = fresh(tmp)
        assert store.get_meta("k") is None
        store.set_meta("k", "1")
        store.set_meta("k", "2")
        assert store.get_meta("k") == "2"
