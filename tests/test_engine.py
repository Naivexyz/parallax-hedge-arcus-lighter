"""引擎端到端（演练模式）。

验的是接线：快照 → 风控 → 预检 → 调度 → 执行 → 落库这一整条链，
以及「开仓后要记住走廊和开仓时刻」「到点平仓」「孤腿抢救」。
"""
import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from parallax_hedge.config import Settings
from parallax_hedge.engine import HedgeEngine
from parallax_hedge.store import Store

MARKET = {
    "asset": "OAI", "display_name": "OAI", "lighter_symbol": "OPENAI",
    "lighter_market_index": 42, "arcus_symbol": "OAI-USD", "arcus_market_id": 9,
    "arcus_step_size": "0.001", "arcus_tick_size": "0.01", "arcus_tick_tiers": [],
    "arcus_max_order_size": 100000,
    # 故意不给 arcus_mmf：走廊估算退回 1/(2×最大杠杆)，下面的数字（6× → 8.33%）按这个算
    "min_base_quantity": 0.01, "quantity_decimals": 2,
    "lighter_size_decimals": 2, "price_decimals": 2, "max_leverage": 6,
}


def row(lighter_size=0.0, arcus_size=0.0, mark=210.0, lighter_pnl=0.0, arcus_pnl=0.0):
    def leg(size, liq, pnl):
        return {"size": size, "entry_price": mark, "mark_price": mark,
                "liquidation_price": liq if size else None,
                "unrealized_pnl": pnl, "margin": 10.0}
    return {
        "asset": "OAI", "lighter_symbol": "OPENAI", "arcus_symbol": "OAI-USD",
        "lighter_bps_per_hour": 0.04, "arcus_bps_per_hour": 0.424,
        "net_bps_per_hour": 0.384, "direction": "long_lighter_short_arcus",
        "direction_label": "Lighter 多 / Arcus 空", "tradable": True, "reason": None,
        "mark_price": mark,
        "position": {
            "lighter": leg(lighter_size, mark * 0.9, lighter_pnl),
            "arcus": leg(arcus_size, mark * 1.1, arcus_pnl),
        },
    }


class FakeClient:
    def __init__(self, r):
        self.row = r
    async def common_markets(self, force=False):
        return [dict(MARKET)]
    async def lighter_book(self, market_id, symbol, limit=100):
        from parallax_hedge.books import Book, Level
        mark = self.row.get("mark_price") or 210.0
        # Lighter 卖一低于 Arcus 买一：买 Lighter、卖 Arcus，买价严格更便宜。
        # 不再靠「绝对价差小于 1 bp」把买贵卖便宜放过去。
        return Book(venue="lighter", symbol=symbol,
                    bids=[Level(mark, 50)], asks=[Level(mark * 1.000001, 50)])

    async def arcus_book(self, symbol):
        from parallax_hedge.books import Book, Level
        mark = self.row.get("mark_price") or 210.0
        return Book(venue="arcus", symbol=symbol,
                    bids=[Level(mark * 1.000002, 50)], asks=[Level(mark * 1.000003, 50)])
    async def lighter_account(self):
        return {"accounts": [{"account_index": 77, "available_balance": "300"}]}
    async def arcus_account(self):
        return {"equity": "300", "freeCollateral": "300", "_positions": []}


class FakeService:
    def __init__(self, r):
        self.row = r
        self.client = FakeClient(r)
    async def refresh(self, **kw):
        return {"ready": True, "rows": [self.row], "venues": {}}


def make(tmp, r, **over):
    kw = dict(env_path=Path("."), data_dir=Path(tmp), lighter_account_index=77,
              min_corridor_pct=4.0, reopen_cooldown_seconds=60.0, dry_run=True)
    kw.update(over)
    st = Settings(**kw)
    service = FakeService(r)
    store = Store(Path(tmp) / "t.db")
    return HedgeEngine(st, service, store, dry_run=st.dry_run), store, service


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_enabled_task_opens_and_records_state():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row())
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        decisions = run(eng.run_cycle())
        assert decisions[0]["plan"] == "open"
        task = store.get_task("OAI")
        assert task["opened_at"] is not None
        assert task["open_direction"] == "long_lighter_short_arcus"
        # 演练模式记录预检的估算走廊：Arcus 全仓，按账户权益 300 vs 这笔仓位算
        # （夹具没给 MMF，退回 1/(2×6)）
        assert task["corridor_at_open"] == pytest.approx(8.46, abs=0.05)


def test_disabled_task_does_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row())
        store.upsert_task("OAI", enabled=0, leverage=6.0, rotation_hours=4.0)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "idle"
        assert store.get_task("OAI")["opened_at"] is None


def test_holding_position_is_left_alone_until_min_hold():
    """两腿刚成交：未满 3 秒，即使价差已经很窄也不平。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, svc = make(tmp, row(0.2, -0.2))
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 1, corridor_at_open=8.3)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "idle" and "未满最短" in d[0]["reason"]
        assert store.get_task("OAI")["opened_at"] is not None


def test_position_is_closed_when_rotation_is_due():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.2, -0.2))
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 5 * 3600, corridor_at_open=8.3)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "close" and "强制平仓" in d[0]["reason"]
        assert store.get_task("OAI")["opened_at"] is None
        assert store.get_task("OAI")["last_closed_at"] is not None


def test_orphan_leg_is_flattened_even_when_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.2, 0.0))          # 只剩 lighter
        store.upsert_task("OAI", enabled=0, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 3600, corridor_at_open=8.3)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "flatten_orphan"
        assert d[0]["urgent"] is True


def test_narrow_corridor_blocks_opening():
    """市场上限 20×、任务也填 20× → 走廊 2.5%，低于 4% 下限，必须拒绝。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, svc = make(tmp, row())

        async def markets(force=False):
            return [dict(MARKET, max_leverage=20)]
        svc.client.common_markets = markets
        store.upsert_task("OAI", enabled=1, leverage=20.0, rotation_hours=4.0)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "blocked"
        assert "走廊" in d[0]["reason"]


def test_a_leverage_above_the_markets_limit_is_clamped_to_it():
    """任务填 20×：每一边各按自己的上限开（这就是「拉满」），不是整单拒掉。
    Arcus 这个市场最高 6×，Lighter 最高 10×。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, svc = make(tmp, row())

        async def details(market_id):
            return {"max_leverage": 10.0}
        svc.client.lighter_market_details = details
        store.upsert_task("OAI", enabled=1, leverage=20.0, rotation_hours=4.0)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "open"
        logged = store.recent_cycles()[0]
        assert "Lighter 10× / Arcus 6×" in logged["reason"]


def test_cooldown_blocks_immediate_reopen():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row())
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          last_closed_at=time.time() - 5)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "idle" and "冷却" in d[0]["reason"]


def test_cycles_are_logged():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row())
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        run(eng.run_cycle())
        logged = store.recent_cycles()
        assert logged and logged[0]["plan"] == "open"
        assert logged[0]["dry_run"] == 1


# ── service 与 engine 之间的字段契约 ──────────────────────
#
# 2026-09-18 的教训：测试夹具里手写了 mark_price，真实的 service 却
# 从来没产出过这个字段。结果 102 个用例全过，一上真机就卡在
# 「缺少开仓前预检结果」。夹具和实现脱节，测试就变成了自说自话。
#
# 这条用例直接拿【真实的 FundingService】造一次快照，断言引擎会读的
# 每个字段都确实存在。

ENGINE_REQUIRED_ROW_KEYS = {
    "asset", "mark_price", "position", "direction", "tradable",
    "net_bps_per_hour", "lighter_bps_per_hour", "arcus_bps_per_hour",
    "max_leverage", "min_base_quantity", "quantity_decimals",
}


def test_service_row_contains_every_field_the_engine_reads():
    import asyncio as _a
    from parallax_hedge.funding import FundingRow
    from parallax_hedge.service import FundingService

    class Client:
        async def common_markets(self, force=False):
            return [dict(MARKET)]
        async def arcus_funding(self):
            return ({"OAI-USD": FundingRow("arcus", "OAI", 0.0000424, 3600)}, {"OAI-USD": 210.0})
        async def lighter_funding(self):
            return {42: FundingRow("lighter", "OPENAI", 0.000032, 28800)}
        async def arcus_account(self):
            return {"equity": "0", "freeCollateral": "0", "_positions": []}
        async def lighter_account(self):
            return {"accounts": [{"account_index": 77, "positions": []}]}
        async def lighter_book(self, market_id, symbol, limit=100):
            from parallax_hedge.books import Book, Level
            return Book(venue="lighter", symbol=symbol,
                        bids=[Level(209.99, 50)], asks=[Level(210.00, 50)])
        async def arcus_book(self, symbol):
            from parallax_hedge.books import Book, Level
            return Book(venue="arcus", symbol=symbol,
                        bids=[Level(209.995, 50)], asks=[Level(210.02, 50)])
        async def aclose(self):
            pass

    with tempfile.TemporaryDirectory() as tmp:
        st = Settings(env_path=Path("."), data_dir=Path(tmp), lighter_account_index=77)
        svc = FundingService(st)
        svc.client = Client()
        snap = _a.get_event_loop().run_until_complete(svc.refresh())
        assert snap["rows"], "快照没有产出任何行"
        row = snap["rows"][0]
        missing = ENGINE_REQUIRED_ROW_KEYS - set(row)
        assert not missing, f"service 少产出了引擎要用的字段：{sorted(missing)}"
        assert row["mark_price"] == pytest.approx(210.0)


def test_engine_opens_against_a_real_service_snapshot():
    """端到端：用真实 service 产出的快照喂给引擎，必须能开仓而不是受阻。"""
    import asyncio as _a
    from parallax_hedge.funding import FundingRow
    from parallax_hedge.service import FundingService

    class Client:
        async def common_markets(self, force=False):
            return [dict(MARKET)]
        async def arcus_funding(self):
            return ({"OAI-USD": FundingRow("arcus", "OAI", 0.0000424, 3600)}, {"OAI-USD": 210.0})
        async def lighter_funding(self):
            return {42: FundingRow("lighter", "OPENAI", 0.000032, 28800)}
        async def arcus_account(self):
            return {"equity": "300", "freeCollateral": "300", "_positions": []}
        async def lighter_account(self):
            return {"accounts": [{"account_index": 77, "available_balance": "300",
                                  "positions": []}]}
        async def lighter_book(self, market_id, symbol, limit=100):
            from parallax_hedge.books import Book, Level
            return Book(venue="lighter", symbol=symbol,
                        bids=[Level(209.99, 50)], asks=[Level(210.00, 50)])
        async def arcus_book(self, symbol):
            from parallax_hedge.books import Book, Level
            return Book(venue="arcus", symbol=symbol,
                        bids=[Level(209.995, 50)], asks=[Level(210.02, 50)])
        async def aclose(self):
            pass

    with tempfile.TemporaryDirectory() as tmp:
        st = Settings(env_path=Path("."), data_dir=Path(tmp), lighter_account_index=77,
                      min_corridor_pct=4.0, dry_run=True)
        svc = FundingService(st)
        svc.client = Client()
        store = Store(Path(tmp) / "t.db")
        eng = HedgeEngine(st, svc, store, dry_run=True)
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=16.0)
        d = _a.get_event_loop().run_until_complete(eng.run_cycle())
        assert d[0]["plan"] == "open", f"应当开仓，实际是 {d[0]['plan']}：{d[0]['reason']}"


# ── 可用保证金的提取 ────────────────────────────────────
#
# Arcus 的可用 = /v1/account 的 freeCollateral（官方定义：权益 − 各仓位初始保证金）。
# 引擎算数量和首页显示用的是同一个函数（accounts.arcus_balance）。

from parallax_hedge.engine import _arcus_available, _lighter_available


def test_arcus_available_is_free_collateral():
    payload = {"equity": "600", "freeCollateral": "523.40", "_positions": []}
    assert _arcus_available(payload) == pytest.approx(523.40)


def test_arcus_available_falls_back_to_net_quote_when_free_collateral_is_absent():
    payload = {"equity": "600", "netQuoteBalance": "42", "_positions": []}
    assert _arcus_available(payload) == pytest.approx(42.0)


def test_arcus_available_never_goes_negative_and_errors_read_as_zero():
    assert _arcus_available({"equity": "1", "freeCollateral": "-3"}) == 0.0
    assert _arcus_available(RuntimeError("超时")) == 0.0
    assert _arcus_available(None) == 0.0


def test_arcus_available_is_the_same_number_the_dashboard_shows():
    """引擎和首页用同一个函数 —— 以前是两份复制的公式，改一处漏一处。"""
    from parallax_hedge.accounts import arcus_balance
    payload = {"equity": "416.44", "freeCollateral": "200.70", "_positions": [
        {"marketId": 9, "size": "-0.464", "averageEntryPrice": "430", "markPx": "431",
         "marginMode": "ISOLATED", "marginUsed": "199.9"}]}
    assert _arcus_available(payload) == pytest.approx(arcus_balance(payload)["available"])


def test_arcus_available_tolerates_errors_and_junk():
    assert _arcus_available(RuntimeError("boom")) == 0.0
    assert _arcus_available(None) == 0.0
    assert _arcus_available({"withdrawable": "abc"}) == 0.0


def test_lighter_available_matches_the_account_index():
    payload = {"accounts": [
        {"account_index": 11, "available_balance": "999"},
        {"account_index": 77, "available_balance": "312.5"},
    ]}
    assert _lighter_available(payload, 77) == pytest.approx(312.5)
    assert _lighter_available(payload, 99) == 0.0      # 索引对不上不能拿别人的钱当自己的


def test_lighter_available_falls_back_through_the_key_list():
    assert _lighter_available(
        {"accounts": [{"account_index": 77, "collateral": "88"}]}, 77
    ) == pytest.approx(88.0)


# ── 演练模式的完整循环 ──────────────────────────────────
#
# 2026-09-18：演练里引擎每 20 秒重复开一次仓，持仓时长永远 0.00 小时。
# 原因是调度器按【真实仓位】判断有没有持仓，而演练不产生真实仓位。
# 后果不只是刷屏 —— 「持仓 → 到期 → 平仓」这条链根本没被验到。

def test_dry_run_does_not_reopen_every_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row())
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        first = run(eng.run_cycle())
        assert first[0]["plan"] == "open"
        opened_at = store.get_task("OAI")["opened_at"]

        second = run(eng.run_cycle())
        assert second[0]["plan"] == "idle", f"不该重复开仓：{second[0]}"
        assert "未满最短" in second[0]["reason"]
        assert store.get_task("OAI")["opened_at"] == opened_at   # 开仓时刻没被刷新


def test_dry_run_runs_the_full_open_hold_close_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row())
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        run(eng.run_cycle())                                      # 开
        assert store.get_task("OAI")["opened_at"] is not None

        # 把开仓时刻拨回 5 小时前，触发到期
        store.upsert_task("OAI", opened_at=time.time() - 5 * 3600)
        closed = run(eng.run_cycle())
        assert closed[0]["plan"] == "close" and "强制平仓" in closed[0]["reason"]
        task = store.get_task("OAI")
        assert task["opened_at"] is None
        assert task["last_closed_at"] is not None

        # 平完立刻又跑一轮 —— 冷却期内不该重开
        after = run(eng.run_cycle())
        assert after[0]["plan"] == "idle" and "冷却" in after[0]["reason"]


def test_simulated_position_carries_a_liquidation_price():
    """模拟持仓要能被风控看懂，否则演练验不到「逼近强平就双腿同平」。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row())
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        run(eng.run_cycle())
        task = store.get_task("OAI")
        sim = eng._simulated_health(task, "OAI", price_hint=210.0)
        assert sim is not None and sim.is_hedged
        assert sim.min_distance_pct == pytest.approx(8.46, abs=0.05)
        # 多腿强平价在下方、空腿在上方
        assert all(leg.liquidation_side_is_sane for leg in sim.open_legs)


def test_stale_open_state_is_cleared_before_deciding():
    """实盘下库里记着有仓、两边却都读不到（被手动平掉/被强平/开仓其实没成）。

    必须在决策【之前】清掉：否则调度器看到「空仓」会当场又开一个新仓，
    陈旧状态被覆盖、冷却期永远不生效。清掉后应当进入冷却而不是开仓。
    """
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(), dry_run=False)   # row() 是空仓
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 5 * 3600, open_quantity=1.0,
                          open_direction="long_lighter_short_arcus",
                          corridor_at_open=8.3)
        d = run(eng.run_cycle())
        task = store.get_task("OAI")
        assert task["opened_at"] is None, "陈旧的开仓状态应被清掉"
        assert task["last_closed_at"] is not None
        # 关键：这一轮不能顺手又开一个新仓
        assert d[0]["plan"] == "idle" and "冷却" in d[0]["reason"]


# ── 名义上限与精度 ──────────────────────────────────────
#
# 2026-09-18：设了 200 USDC 名义上限后，数量变成 0.0914286 / 0.125889 /
# 0.114929 —— 小数位全是乱的。max_quantity_for 内部对齐过，但名义上限
# 那一步 `200/价格` 直接覆盖，把对齐破坏了。演练看不出来，实盘每单必拒
# （历史数据里这类拒单出现过 2433 次）。

def test_notional_cap_keeps_the_quantity_aligned():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(mark=1588.7))
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          notional_usdc=200.0)
        run(eng.run_cycle())
        qty = store.get_task("OAI")["open_quantity"]
        assert qty is not None and qty > 0
        step = 10 ** MARKET["quantity_decimals"]
        # 必须落在精度档位上：乘以 10^decimals 后应当是整数
        assert abs(qty * step - round(qty * step)) < 1e-9, f"数量未对齐：{qty}"
        # 且不得超过名义上限
        assert qty * 1588.7 <= 200.0 + 1e-6


def test_notional_cap_actually_caps():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(mark=100.0))
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          notional_usdc=150.0)
        run(eng.run_cycle())
        qty = store.get_task("OAI")["open_quantity"]
        assert qty * 100.0 <= 150.0 + 1e-6
        assert qty == pytest.approx(1.5)


def test_without_a_cap_the_quantity_is_still_aligned():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(mark=1588.7))
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        run(eng.run_cycle())
        qty = store.get_task("OAI")["open_quantity"]
        step = 10 ** MARKET["quantity_decimals"]
        assert abs(qty * step - round(qty * step)) < 1e-9, f"数量未对齐：{qty}"


# ═══════════════════════════════════════════════════════════
# 随机轮换（2026-09-19）
# ═══════════════════════════════════════════════════════════
import random as _random


def test_opening_draws_a_hold_time_inside_the_range_and_stores_it():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row())
        eng.rng = _random.Random(11)
        expected = _random.Random(11).uniform(1.0, 2.0)
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=1.0,
                          rotation_hours_max=2.0)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "open"
        assert store.get_task("OAI")["hold_hours"] == pytest.approx(expected)


def test_a_fixed_period_task_stores_the_fixed_hold():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row())
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        run(eng.run_cycle())
        assert store.get_task("OAI")["hold_hours"] == pytest.approx(4.0)


def test_changing_hold_settings_applies_on_the_next_cycle():
    """同一个引擎实例，改 Settings 后下一轮就按新的秒数走，不用新建引擎。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.2, -0.2))
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 4, corridor_at_open=8.3,
                          open_direction="long_lighter_short_arcus", open_quantity=0.2)
        eng.settings.min_hold_sec = 30
        eng.settings.max_hold_sec = 90
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "idle" and "30" in d[0]["reason"]
        assert store.get_task("OAI")["opened_at"] is not None
        eng.settings.min_hold_sec = 3
        eng.settings.max_hold_sec = 300
        eng.settings.max_spread_bps = 1.2
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "close" and store.get_task("OAI")["opened_at"] is None


def test_after_min_hold_a_tight_spread_is_maker_closed():
    """满 3 秒后开始找价差。夹具价差不到 1 bp，应该挂 maker 平掉。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.2, -0.2))
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 4, corridor_at_open=8.3,
                          open_direction="long_lighter_short_arcus", open_quantity=0.2)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "close" and "浮盈亏" in d[0]["reason"]
        assert "不超过" not in d[0]["reason"]
        assert "Lighter" in d[0]["reason"] and "Arcus" in d[0]["reason"]
        assert store.get_task("OAI")["opened_at"] is None
        logged = store.recent_cycles()[0]
        assert "浮盈亏" in logged["reason"] and "不超过" not in logged["reason"]
        assert "Lighter" in logged["reason"] and "Arcus" in logged["reason"]


def test_after_min_hold_a_wide_favorable_spread_is_maker_closed():
    """某一个所一直更贵、缺口远宽于 1 bp：平仓是买便宜卖贵，最短持有后立刻平，不等回到 0。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.2, -0.2))

        async def rich_lighter(market_id, symbol, limit=100):
            from parallax_hedge.books import Book, Level
            # 平多 Lighter：卖在卖一 211.20；买回 Arcus 在买一 210.00。买价更低，约 57 bp。
            return Book(venue="lighter", symbol=symbol,
                        bids=[Level(211.10, 50)], asks=[Level(211.20, 50)])

        async def cheap_arcus(symbol):
            from parallax_hedge.books import Book, Level
            return Book(venue="arcus", symbol=symbol,
                        bids=[Level(210.00, 50)], asks=[Level(210.10, 50)])

        eng.service.client.lighter_book = rich_lighter
        eng.service.client.arcus_book = cheap_arcus
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 4, corridor_at_open=8.3,
                          open_direction="long_lighter_short_arcus", open_quantity=0.2)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "close" and "浮盈亏" in d[0]["reason"]
        assert "不超过" not in d[0]["reason"]
        assert "211.2" in d[0]["reason"] and "210" in d[0]["reason"]
        assert store.get_task("OAI")["opened_at"] is None


def test_net_pnl_window_closes_after_min_hold_and_waits_when_worse():
    """差额 0.02：合计 -0.02 在最短持有后挂 maker 平；-0.05 在最长持有前不平。"""
    def once(net, age):
        with tempfile.TemporaryDirectory() as tmp:
            eng, store, _ = make(
                tmp, row(0.2, -0.2, lighter_pnl=net, arcus_pnl=0.0), dry_run=False,
            )
            eng.settings.pnl_close_usd = 0.02
            spy = CloseSpy()
            eng.executor = spy
            store.upsert_task(
                "OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                opened_at=time.time() - age, corridor_at_open=8.3,
                open_direction="long_lighter_short_arcus", open_quantity=0.2,
            )
            d = run(eng.run_cycle())
            if d[0]["plan"] == "maker_started":
                _drain(eng)
            return d, spy.calls, store.get_task("OAI"), store.recent_cycles()

    closed, calls, task, _cycles = once(-0.02, 4)
    assert calls and calls[0][0] == "place_maker_pair"
    assert calls[0][1]["action"] == "close" and calls[0][1]["reduce_only"] is True
    assert task["opened_at"] is None
    assert "浮盈亏" in closed[0]["reason"]

    waiting, calls, task, cycles = once(-0.05, 30)
    assert calls == []
    assert task["opened_at"] is not None
    assert waiting[0]["plan"] == "idle"
    assert "先不平" in waiting[0]["reason"]
    assert cycles == [] or cycles[0]["plan"] != "close"

    forced, calls, task, cycles = once(-0.05, 301)
    assert calls and calls[0][0] == "place_maker_pair"
    assert all(name != "close_pair" for name, _ in calls)
    assert task["opened_at"] is None
    assert "强制" in forced[0]["reason"]
    assert any("强制" in (c["reason"] or "") for c in cycles)


def test_max_hold_closes_even_when_the_spread_is_wide():
    """满 300 秒仍持仓：不再等价差，强制平仓。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.2, -0.2))

        async def wide_arcus(symbol):
            from parallax_hedge.books import Book, Level
            return Book(venue="arcus", symbol=symbol,
                        bids=[Level(210.20, 50)], asks=[Level(210.30, 50)])

        eng.service.client.arcus_book = wide_arcus
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 301, corridor_at_open=8.3,
                          open_direction="long_lighter_short_arcus", open_quantity=0.2)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "close" and "强制" in d[0]["reason"]
        assert d[0]["urgent"] is False
        assert store.get_task("OAI")["opened_at"] is None


def test_a_full_dry_run_rotation_books_four_simulated_fills():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row())
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        run(eng.run_cycle())                                   # 开
        store.upsert_task("OAI", opened_at=time.time() - 5 * 3600)
        d = run(eng.run_cycle())                               # 到期平
        assert d[0]["plan"] == "close"
        stats = store.fill_stats(dry_run=True)
        assert stats["total"]["fills"] == 4
        assert stats["venues"]["lighter"]["fills"] == 2
        assert stats["venues"]["arcus"]["fills"] == 2
        assert stats["by_asset"]["OAI"]["opens"] == 1
        assert stats["total"]["volume"] > 0
        assert store.fill_stats(dry_run=False)["total"]["fills"] == 0


# ═══════════════════════════════════════════════════════════
# 实盘专属的两道保险
# ═══════════════════════════════════════════════════════════
from parallax_hedge.execution import LegResult, PairResult


class SpyExecutor:
    """实盘替身：记录被调了什么；没配置的调用一律当成测试失败。"""

    def __init__(self, close_result=None):
        self.calls = []
        self.close_result = close_result

    async def close_pair(self, **kw):
        self.calls.append(("close_pair", kw))
        if self.close_result is None:
            raise AssertionError("不该平仓")
        return self.close_result

    async def open_pair(self, **kw):
        self.calls.append(("open_pair", kw))
        raise AssertionError("不该开仓")

    async def flatten_orphan(self, **kw):
        self.calls.append(("flatten_orphan", kw))
        raise AssertionError("不该抢救孤腿")


class ServiceWithAccountError(FakeService):
    async def refresh(self, **kw):
        snap = await super().refresh(**kw)
        snap["accounts"] = {"errors": {"lighter": "/account 请求失败（已重试 3 次）"}}
        return snap


def test_an_account_read_failure_blocks_every_action():
    """Lighter 账户读不到时，快照里 Lighter 那条腿就是「空」—— 和真的空仓分不出来。
    按快照做决定，会把健康的 Arcus 腿当成孤腿市价平掉，Lighter 那边反而成了裸腿。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.0, -0.2), dry_run=False)   # 看起来像孤腿
        eng.service = ServiceWithAccountError(row(0.0, -0.2))
        eng.executor = SpyExecutor()
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 3600, corridor_at_open=8.3)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "blocked" and "账户读取失败" in d[0]["reason"]
        assert eng.executor.calls == []
        assert store.get_task("OAI")["opened_at"] is not None     # 状态也不能被误清


def test_an_untracked_hedged_position_is_adopted_and_timed():
    """库里没有开仓记录、交易所上却有一对对冲好的仓位：以前会永远「闲置」，
    这对仓位永远不会被轮换。现在接管并从此刻开始计时。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.2, -0.2), dry_run=False)
        eng.executor = SpyExecutor()
        eng.rng = _random.Random(5)
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=1.0,
                          rotation_hours_max=2.0)
        d = run(eng.run_cycle())
        task = store.get_task("OAI")
        assert task["opened_at"] is not None
        assert 1.0 <= task["hold_hours"] <= 2.0
        assert task["open_direction"] == "long_lighter_short_arcus"
        assert d[0]["plan"] == "idle" and "未满最短" in d[0]["reason"]
        assert any(c["plan"] == "adopt" for c in store.recent_cycles())
        assert eng.executor.calls == []


def test_an_unhedged_pair_is_not_adopted():
    """两条腿对不上不是「接管」的对象 —— 那是风控要处理的。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.2, -0.1), dry_run=False)
        eng.executor = SpyExecutor(close_result=PairResult(True, "closed"))
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=1.0)
        run(eng.run_cycle())
        assert not any(c["plan"] == "adopt" for c in store.recent_cycles())


def test_live_close_books_both_legs_with_their_order_refs():
    tx = "ab" * 40
    lighter = LegResult("lighter", True, filled=0.2, fill_confirmed=True, side="sell",
                        requested=0.2, price=209.9, order_ref=tx)
    arcus = LegResult("arcus", True, filled=0.2, fill_confirmed=True, side="buy",
                      requested=0.2, price=210.05, order_ref="o-123")
    result = PairResult(True, "closed", lighter, arcus,
                        ledger=[("close", lighter), ("close", arcus)])
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.2, -0.2), dry_run=False)
        eng.executor = SpyExecutor(close_result=result)
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 5 * 3600, corridor_at_open=8.3)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "close"
        rows = {r["venue"]: dict(r) for r in store.conn.execute("SELECT * FROM fills")}
        assert rows["lighter"]["order_ref"] == tx and rows["lighter"]["market_id"] == 42
        assert rows["arcus"]["order_ref"] == "o-123" and rows["arcus"]["market_id"] is None
        assert all(r["status"] == "pending" and r["dry_run"] == 0 for r in rows.values())
        cycle_id = store.recent_cycles()[0]["id"]
        assert all(r["cycle_id"] == cycle_id for r in rows.values())


def test_a_bookkeeping_failure_never_breaks_trading():
    """账本是统计用的，仓位才是真的。记账出错只能记一笔错误，不能让这一轮失败。"""
    lighter = LegResult("lighter", True, filled=0.2, fill_confirmed=True, side="sell",
                        requested=0.2, price=209.9, order_ref="cd" * 40)
    result = PairResult(True, "closed", lighter, None, ledger=[("close", lighter)])
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.2, -0.2), dry_run=False)
        eng.executor = SpyExecutor(close_result=result)
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 5 * 3600, corridor_at_open=8.3)

        def broken(**kw):
            raise RuntimeError("磁盘满了")
        store.record_fill = broken
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "close"
        assert store.get_task("OAI")["opened_at"] is None          # 平仓状态照常推进
        assert any(c["plan"] == "error" and "记账" in c["reason"] for c in store.recent_cycles())


# ═══════════════════════════════════════════════════════════
# 杠杆：开仓前把两边设成任务要的值（2026-09-19 实盘问题）
#
# SNDK 任务填了 6× 和 1000 USDC 上限，实际只开出 0.2776（约 $494）：
#   · 程序从来不设杠杆，两边用的是账户里原来的设置；
#   · Lighter 上 ANTH 那对 $1000 的仓位按默认的低杠杆占掉了大部分保证金，
#     只剩约 $86.6 可用 → 86.6 × 6 × 0.95 ÷ 1779 = 0.2776；
#   · Arcus 的 SNDK 实际按 10× 开，强平走廊只有 5.25%。
# ═══════════════════════════════════════════════════════════

class LeverageSpy(SpyExecutor):
    def __init__(self, open_result=None, set_ok=True):
        super().__init__()
        self.open_result = open_result
        self.set_ok = set_ok

    async def set_leverage(self, market, *, lighter_leverage, arcus_leverage):
        self.calls.append(("set_leverage", {"lighter": lighter_leverage,
                                            "arcus": arcus_leverage,
                                            "asset": market["asset"]}))
        return (True, None) if self.set_ok else (False, "Lighter 设置 6× 杠杆被拒：max 3x")

    async def open_pair(self, **kw):
        self.calls.append(("open_pair", kw))
        if self.open_result is None:
            raise AssertionError("不该开仓")
        return self.open_result


class ClientWithLighterLimit(FakeClient):
    def __init__(self, r, lighter_max):
        super().__init__(r)
        self.lighter_max = lighter_max

    async def lighter_market_details(self, market_id):
        return {"max_leverage": self.lighter_max, "default_leverage": 2.0}


def _live_engine(tmp, *, lighter_max=None, executor=None, lighter_avail="300",
                 arcus_avail="300"):
    eng, store, svc = make(tmp, row(), dry_run=False)
    client = ClientWithLighterLimit(row(), lighter_max)

    async def lighter_account():
        return {"accounts": [{"account_index": 77, "available_balance": lighter_avail}]}

    async def arcus_account():
        return {"equity": arcus_avail, "freeCollateral": arcus_avail, "_positions": []}
    client.lighter_account = lighter_account
    client.arcus_account = arcus_account
    svc.client = client
    eng.executor = executor or LeverageSpy()
    return eng, store


def test_leverage_is_set_on_both_venues_before_the_open():
    opened = PairResult(True, "opened", quotes={}, ledger=[])
    with tempfile.TemporaryDirectory() as tmp:
        eng, store = _live_engine(tmp, lighter_max=10.0,
                                  executor=LeverageSpy(open_result=opened))
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        run(eng.run_cycle())
        names = [name for name, _ in eng.executor.calls]
        assert names[:2] == ["set_leverage", "open_pair"]          # 先设杠杆，再下单
        assert eng.executor.calls[0][1] == {"lighter": 6, "arcus": 6, "asset": "OAI"}


def test_each_venue_is_clamped_to_its_own_limit():
    """Lighter 这个市场最高 3×：Lighter 设 3×，Arcus 照样 6×。"""
    opened = PairResult(True, "opened", quotes={}, ledger=[])
    with tempfile.TemporaryDirectory() as tmp:
        eng, store = _live_engine(tmp, lighter_max=3.0,
                                  executor=LeverageSpy(open_result=opened))
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        run(eng.run_cycle())
        assert eng.executor.calls[0][1] == {"lighter": 3, "arcus": 6, "asset": "OAI"}


def test_a_failed_leverage_change_aborts_the_open_before_any_order():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store = _live_engine(tmp, lighter_max=10.0, executor=LeverageSpy(set_ok=False))
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        d = run(eng.run_cycle())
        assert [name for name, _ in eng.executor.calls] == ["set_leverage"]
        assert store.get_task("OAI")["opened_at"] is None
        logged = store.recent_cycles()[0]
        assert logged["plan"] == "open_failed" and "杠杆" in logged["reason"]


def test_size_follows_each_venues_real_leverage():
    """复现实盘：Lighter 可用约 86.65，Arcus 可用约 254，SNDK 价格约 1779。
    两边都按 6× 算，被 Lighter 卡住 → 0.2776，正是实盘那一单的数量。"""
    from parallax_hedge.risk import max_quantity_for
    six = max_quantity_for(leverage=6, price=1779.0, lighter_available=86.65,
                           arcus_available=254.0, quantity_decimals=4)
    assert six == pytest.approx(0.2776, abs=1e-9)
    # Lighter 可用余额变多（ANTH 那对仓位改按 6× 占保证金之后），上限就能用满
    freed = max_quantity_for(leverage=6, price=1779.0, lighter_available=280.0,
                             arcus_available=254.0, quantity_decimals=4,
                             lighter_leverage=6, arcus_leverage=6)
    assert freed * 1779.0 > 1000


# ═══════════════════════════════════════════════════════════
# 开仓记录要写清楚：两边实际杠杆、可用余额、数量是被谁卡住的
# ═══════════════════════════════════════════════════════════

class EchoOpen(LeverageSpy):
    """开仓替身：把引擎递过来的 quotes 原样带回，和真执行器一样。"""

    async def open_pair(self, **kw):
        self.calls.append(("open_pair", kw))
        return PairResult(True, "opened", quotes=kw["quotes"], ledger=[])


def _arcus_free(free):
    async def arcus_account():
        return {"equity": "416.06", "freeCollateral": free, "_positions": [
            {"marketId": 77, "size": "-0.464", "averageEntryPrice": "430", "markPx": "430",
             "marginMode": "ISOLATED", "marginUsed": "199.68"}]}
    return arcus_account


def test_the_open_record_carries_leverage_balances_and_the_sizing_reason():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store = _live_engine(tmp, lighter_max=10.0, executor=EchoOpen(),
                                  lighter_avail="243.5")
        eng.service.client.arcus_account = _arcus_free("216.38")
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          notional_usdc=1000.0)
        run(eng.run_cycle())
        opened = [kw for name, kw in eng.executor.calls if name == "open_pair"]
        # 1000 / 210 = 4.76（两边余额都够，被名义上限卡住）
        assert opened and opened[0]["quantity"] == pytest.approx(4.76)
        logged = store.recent_cycles()[0]
        assert logged["plan"] == "open"
        assert "（杠杆 Lighter 6× / Arcus 6×；按名义上限 1000 USDC）" in logged["reason"]
        quotes = logged["result"]["quotes"]
        assert quotes["arcus_available"] == pytest.approx(216.38)
        assert quotes["lighter_available"] == pytest.approx(243.5)
        assert quotes["sizing"] == "按名义上限 1000 USDC"


def test_the_open_record_names_the_venue_that_capped_the_size():
    """真被余额卡住时，开仓记录直接说是哪一边、可用多少。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store = _live_engine(tmp, lighter_max=10.0, executor=EchoOpen(),
                                  lighter_avail="243.5")
        eng.service.client.arcus_account = _arcus_free("30.32")
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          notional_usdc=1000.0)
        run(eng.run_cycle())
        logged = store.recent_cycles()[0]
        assert "受 Arcus 可用 30.32 USDC 限制" in logged["reason"]


# ═══════════════════════════════════════════════════════════
# Arcus 版新增的闸门
# ═══════════════════════════════════════════════════════════

def test_a_wide_basis_blocks_the_open_before_any_order():
    """两所按代号自动配对 —— 同名不等于同一个标的。中价差 3% 以上就不开。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store = _live_engine(tmp, lighter_max=10.0, executor=EchoOpen())

        async def far_book(symbol, levels=50):
            from parallax_hedge.books import Book, Level
            return Book(venue="arcus", symbol=symbol,
                        bids=[Level(250.0, 50)], asks=[Level(250.1, 50)])
        eng.service.client.arcus_book = far_book
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        run(eng.run_cycle())
        assert "open_pair" not in [name for name, _ in eng.executor.calls]
        logged = store.recent_cycles()[0]
        assert logged["plan"] == "open_failed" and "中价相差" in logged["reason"]
        assert store.get_task("OAI")["opened_at"] is None


def test_the_corridor_estimate_uses_arcus_maintenance_margin():
    from parallax_hedge.risk import estimate_corridor_pct
    # BTC：MMF 2%，10× → 10% − 2% = 8%
    assert estimate_corridor_pct(10, 20, 0.02) == pytest.approx(8.0)
    # 没给 MMF 时退回 1/(2×最大杠杆)
    assert estimate_corridor_pct(6, 6) == pytest.approx(100 / 6 - 100 / 12)


def test_an_order_below_the_minimum_notional_is_blocked():
    from parallax_hedge.risk import pre_open_check
    pre = pre_open_check(leverage=5, max_leverage=10, quantity=0.01, price=100,
                         min_quantity=0.001, lighter_available=100, arcus_available=100,
                         min_notional=10)
    assert not pre.ok and "最小下单金额" in pre.reason


# ═══════════════════════════════════════════════════════════
# 挂单模式的路由：开仓 / 正常轮换平仓走挂单；风控平仓一律吃单
# ═══════════════════════════════════════════════════════════

class RecordingMaker:
    def __init__(self, calls):
        self.calls = calls

    async def open_pair(self, **kw):
        self.calls.append(("maker_open", kw))
        return PairResult(True, "opened", quotes={**kw["quotes"], "filled_quantity": 1.5},
                          ledger=[])

    async def close_pair(self, **kw):
        self.calls.append(("maker_close", kw))
        return PairResult(True, "closed", ledger=[])


def _drain(eng):
    """等后台挂单任务跑完。"""
    async def wait():
        for job in list(eng._jobs.values()):
            await job
    run(wait())


def _with_maker(eng):
    calls = eng.executor.calls
    eng._maker = lambda stop=None: RecordingMaker(calls)
    eng.settings.arcus_maker = True
    return calls


def test_maker_mode_opens_through_the_maker_and_records_the_filled_size():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store = _live_engine(tmp, lighter_max=10.0, executor=EchoOpen())
        calls = _with_maker(eng)
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "maker_started"                 # 放到后台，不挡住这一轮
        _drain(eng)
        names = [n for n, _ in calls]
        assert "maker_open" in names and "open_pair" not in names
        assert store.get_task("OAI")["open_quantity"] == pytest.approx(1.5)   # 实际成交的量


def test_max_hold_forces_a_maker_close_instead_of_a_taker():
    """持有已超过 300 秒：强制挂 maker 平，不改吃单，免得 Arcus 付吃单费。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.2, -0.2, lighter_pnl=-1.0), dry_run=False)
        eng.executor = SpyExecutor(close_result=PairResult(True, "closed"))
        calls = _with_maker(eng)
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 5 * 3600, corridor_at_open=8.3,
                          open_direction="long_lighter_short_arcus", open_quantity=0.2)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "maker_started" and "强制" in d[0]["reason"]
        assert d[0]["urgent"] is False
        _drain(eng)
        assert [n for n, _ in calls] == ["maker_close"]
        assert store.get_task("OAI")["opened_at"] is None


def test_a_risk_close_never_waits_for_a_maker_fill():
    """离强平太近时必须立刻吃单平，不能挂着等 45 秒。"""
    r = row(0.2, -0.2)
    r["position"]["arcus"]["liquidation_price"] = 210.0 * 1.01     # 只剩 1%
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, r, dry_run=False)
        eng.executor = SpyExecutor(close_result=PairResult(True, "closed"))
        calls = _with_maker(eng)
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 3600, corridor_at_open=8.3)
        run(eng.run_cycle())
        assert [n for n, _ in calls] == ["close_pair"]


# ═══════════════════════════════════════════════════════════
# Arcus 账户仓位组合变了 → 重设走廊基准（2026-09-26 ETH 被提前平掉的问题）
# ═══════════════════════════════════════════════════════════

def test_a_new_position_on_the_shared_account_rebases_instead_of_closing():
    r = row(0.2, -0.2)
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, svc = make(tmp, r, dry_run=False)
        eng.executor = SpyExecutor()
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 1, corridor_at_open=15.62)
        r["position"]["arcus"]["liquidation_price"] = 210.0 * 1.1562
        r["position"]["lighter"]["liquidation_price"] = 210.0 * 0.8
        run(eng.run_cycle())                                  # 第一轮：记下组合 {OAI}
        # 同一个 Arcus 账户里开了另一个币：OAI 的距离被机械地压到 7.13%，价格没动
        other = row(0.1, -0.1)
        other["asset"] = "SPY"
        svc.row = r
        r["position"]["arcus"]["liquidation_price"] = 210.0 * 1.0713

        async def refresh(**kw):
            return {"ready": True, "rows": [r, other], "venues": {}}
        svc.refresh = refresh
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "idle", d[0]["reason"]         # 没有被相对触发平掉
        assert store.get_task("OAI")["corridor_at_open"] == pytest.approx(7.13, abs=0.01)
        assert any(c["plan"] == "rebase" and "新增 SPY" in c["reason"]
                   for c in store.recent_cycles())


def test_the_absolute_close_line_still_fires_after_a_rebase():
    r = row(0.2, -0.2)
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, svc = make(tmp, r, dry_run=False)
        eng.executor = SpyExecutor(close_result=PairResult(True, "closed"))
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 600, corridor_at_open=15.0)
        eng._arcus_composition = frozenset()                  # 上一轮账户是空的
        r["position"]["arcus"]["liquidation_price"] = 210.0 * 1.01   # 只剩 1% < 1.5%
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "close" and d[0]["urgent"]



class SlowMaker:
    """挂单还没等到成交：一直挂着，直到测试放行。"""

    def __init__(self, calls, gate):
        self.calls, self.gate = calls, gate

    async def open_pair(self, **kw):
        self.calls.append(("maker_open", kw))
        await self.gate.wait()
        return PairResult(False, "maker_not_filled", reason="挂单 120 秒内没有成交，已撤单",
                          quotes={**kw["quotes"], "maker": True}, ledger=[])


def test_a_waiting_maker_job_does_not_block_the_next_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store = _live_engine(tmp, lighter_max=10.0, executor=EchoOpen())
        eng.settings.arcus_maker = True
        calls = eng.executor.calls

        async def scenario():
            gate = asyncio.Event()
            eng._maker = lambda stop=None: SlowMaker(calls, gate)
            store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
            first = await eng.run_cycle()
            await asyncio.sleep(0)
            second = await eng.run_cycle()                 # 挂单还在等：这一轮照常跑完
            gate.set()
            for job in list(eng._jobs.values()):
                await job
            return first, second
        first, second = run(scenario())
        assert first[0]["plan"] == "maker_started"
        assert second[0]["plan"] == "maker_busy"           # 同一个币不重复下单
        assert [n for n, _ in calls].count("maker_open") == 1
        logged = store.recent_cycles()[0]
        assert logged["plan"] == "maker_wait"              # 灰色「未成交」，不是「开仓失败」


# ═══════════════════════════════════════════════════════════
# v1.3 挂单只开出一部分 → 持仓期间继续挂单补到目标数量
# ═══════════════════════════════════════════════════════════

class TopupMaker:
    def __init__(self, calls, filled=0.5, gate=None, stop=None):
        self.calls, self.filled, self.gate, self.stop = calls, filled, gate, stop

    async def open_pair(self, **kw):
        self.calls.append(("maker_open", kw))
        if self.gate is not None:
            await self.gate.wait()
        if not self.filled:
            return PairResult(False, "maker_not_filled", reason="挂单没有成交，已撤单",
                              quotes={**kw["quotes"], "maker": True}, ledger=[])
        return PairResult(True, "opened", quotes={**kw["quotes"], "maker": True,
                                                  "filled_quantity": self.filled}, ledger=[])


def _holding(tmp, *, target=1.0, held=1.0, lighter=0.2, arcus=-0.2, **task):
    eng, store = _live_engine(tmp, lighter_max=10.0, executor=SpyExecutor(
        close_result=PairResult(True, "closed")))
    eng.service.row = row(lighter, arcus)
    eng.settings.arcus_maker = True
    opened = time.time() - held
    store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                      opened_at=opened, corridor_at_open=8.3, open_quantity=abs(lighter),
                      open_direction="long_lighter_short_arcus", target_quantity=target, **task)
    return eng, store, opened


def test_a_partial_open_is_topped_up_while_holding():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, opened = _holding(tmp)
        calls = eng.executor.calls
        eng._maker = lambda stop=None: TopupMaker(calls)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "maker_started" and "补仓 0.8" in d[0]["reason"]
        _drain(eng)
        (name, kw), = calls
        assert name == "maker_open" and kw["action"] == "topup"
        assert kw["quantity"] == pytest.approx(0.8)
        assert kw["direction"] == "long_lighter_short_arcus"
        task = store.get_task("OAI")
        assert task["open_quantity"] == pytest.approx(0.7)          # 0.2 + 补上的 0.5
        assert task["opened_at"] == pytest.approx(opened)           # 不重新计时
        logged = store.recent_cycles()[0]
        assert logged["plan"] == "topup" and logged["reason"].startswith("已补仓")
        assert eng._rebase_note == "OAI 补仓 0.5"                   # 下一轮重设走廊基准


def test_nothing_to_top_up_once_the_target_is_reached():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = _holding(tmp, target=0.2)
        eng._maker = lambda stop=None: TopupMaker(eng.executor.calls)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "idle" and eng.executor.calls == []


def test_no_top_up_once_max_hold_forces_a_close():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = _holding(tmp, held=301)
        calls = eng.executor.calls

        class Closing(TopupMaker):
            async def close_pair(self, **kw):
                calls.append(("maker_close", kw))
                return PairResult(True, "closed", ledger=[])

        eng._maker = lambda stop=None: Closing(calls)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "maker_started" and "强制" in d[0]["reason"]
        _drain(eng)
        assert [n for n, _ in calls] == ["maker_close"]
        assert store.get_task("OAI")["opened_at"] is None


def test_positions_without_a_recorded_target_use_the_notional_cap():
    """升级前开的仓（没记目标）：按任务的名义上限算目标，210 USDC / 210 = 1.0。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = _holding(tmp, target=None, notional_usdc=210.0)
        calls = eng.executor.calls
        eng._maker = lambda stop=None: TopupMaker(calls)
        run(eng.run_cycle())
        _drain(eng)
        assert calls[0][1]["quantity"] == pytest.approx(0.8)


def test_an_unfilled_top_up_leaves_no_log_line():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = _holding(tmp)
        eng._maker = lambda stop=None: TopupMaker(eng.executor.calls, filled=0)
        run(eng.run_cycle())
        _drain(eng)
        assert store.recent_cycles() == []
        assert store.get_task("OAI")["open_quantity"] == pytest.approx(0.2)


def test_top_up_respects_the_open_corridor_threshold():
    """补完之后的总仓位估算走廊不够开仓门槛，就不补。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = _holding(tmp, target=1.0)
        # 权益 300、补完总仓位 210：估算走廊约 130%，门槛设到 200% 让它不过
        eng.settings.min_corridor_pct = 200.0
        eng._maker = lambda stop=None: TopupMaker(eng.executor.calls)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "idle" and "暂不补仓" in d[0]["reason"]
        assert eng.executor.calls == []


def test_risk_still_runs_during_a_top_up_and_stops_it():
    """补仓在后台一直挂着时，离强平太近照样立刻处理：先叫停补仓，再吃单双平。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = _holding(tmp)
        calls = eng.executor.calls
        seen = {}

        def factory(stop=None):
            seen["stop"] = stop
            return TopupMaker(calls, filled=0, gate=stop)    # 一直挂着，直到被叫停
        eng._maker = factory

        async def scenario():
            first = await eng.run_cycle()
            await asyncio.sleep(0)
            r = row(0.2, -0.2)
            r["position"]["arcus"]["liquidation_price"] = 210.0 * 1.01     # 只剩 1%
            eng.service.row = r
            second = await eng.run_cycle()
            return first, second
        first, second = run(scenario())
        assert first[0]["plan"] == "maker_started"
        assert seen["stop"].is_set()
        assert [n for n, _ in calls] == ["maker_open", "close_pair"]
        assert second[0]["plan"] == "close" and second[0]["urgent"]
        assert any(c["plan"] == "topup_stop" for c in store.recent_cycles())


def test_a_healthy_top_up_is_left_running():
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = _holding(tmp)
        calls = eng.executor.calls
        gate = asyncio.Event()
        eng._maker = lambda stop=None: TopupMaker(calls, gate=gate)

        async def scenario():
            await eng.run_cycle()
            await asyncio.sleep(0)
            second = await eng.run_cycle()
            gate.set()
            for job in list(eng._jobs.values()):
                await job
            return second
        second = run(scenario())
        assert second[0]["plan"] == "maker_busy" and "补仓" in second[0]["reason"]
        assert [n for n, _ in calls] == ["maker_open"]


def test_the_three_live_books_open_the_cheaper_side_even_when_wide():
    """所间价差十几 bp 也要开。两边一起挂 maker，这个价差不是锁住的亏损。

    方向仍是买更便宜的一边。面板差额保持 0.02，不因为价差宽就改成不开。
    """
    books = (
        (2652.90, 2652.97, 2654.44, 2654.51),
        (117.6700, 117.6800, 117.7580, 117.7700),
        (1202.1500, 1202.2500, 1200.6000, 1200.6910),
    )
    for lb, la, ab, aa in books:
        with tempfile.TemporaryDirectory() as tmp:
            eng, store, _ = make(tmp, row())
            eng.settings.pnl_close_usd = 0.02

            async def lighter_book(market_id, symbol, limit=100, _lb=lb, _la=la):
                from parallax_hedge.books import Book, Level
                return Book(venue="lighter", symbol=symbol,
                            bids=[Level(_lb, 50)], asks=[Level(_la, 50)])

            async def arcus_book(symbol, _ab=ab, _aa=aa):
                from parallax_hedge.books import Book, Level
                return Book(venue="arcus", symbol=symbol,
                            bids=[Level(_ab, 50)], asks=[Level(_aa, 50)])

            eng.service.client.lighter_book = lighter_book
            eng.service.client.arcus_book = arcus_book
            store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0)
            d = run(eng.run_cycle())
            assert d[0]["plan"] == "open", d[0]
            task = store.get_task("OAI")
            assert task["opened_at"] is not None
            cheap = "long_lighter_short_arcus" if la < aa else "short_lighter_long_arcus"
            assert task["open_direction"] == cheap
            logged = store.recent_cycles()[0]
            assert logged["plan"] == "open"
            assert "还回去" not in (logged["reason"] or "")


def _override_books(eng, lighter_bid, lighter_ask, arcus_bid, arcus_ask):
    from parallax_hedge.books import Book, Level

    async def lighter_book(market_id, symbol, limit=100):
        return Book(venue="lighter", symbol=symbol,
                    bids=[Level(lighter_bid, 50)], asks=[Level(lighter_ask, 50)])

    async def arcus_book(symbol):
        return Book(venue="arcus", symbol=symbol,
                    bids=[Level(arcus_bid, 50)], asks=[Level(arcus_ask, 50)])

    eng.service.client.lighter_book = lighter_book
    eng.service.client.arcus_book = arcus_book


class CloseSpy:
    """只记录计划内 maker 平仓有没有被发出去。不连接交易所。"""

    def __init__(self):
        self.calls = []

    async def place_maker_pair(self, **kw):
        self.calls.append(("place_maker_pair", kw))
        return PairResult(True, "closed", quotes=dict(kw.get("quotes") or {}))

    async def close_pair(self, **kw):
        self.calls.append(("close_pair", kw))
        raise AssertionError("未到最长持有，不该吃单平仓")

    async def open_pair(self, **kw):
        raise AssertionError("不该开仓")

    async def flatten_orphan(self, **kw):
        raise AssertionError("不该抢救孤腿")


def test_favorable_close_wider_than_the_threshold_is_sent_before_max_hold():
    """平仓买 209.99、卖 210.06，约 3.3 bp 有利，宽于 1 bp 阈值：最短持有过后立刻挂 maker。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, row(0.2, -0.2), dry_run=False)
        spy = CloseSpy()
        eng.executor = spy
        _override_books(eng, 210.05, 210.06, 209.99, 210.00)
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 1, corridor_at_open=8.3,
                          open_direction="long_lighter_short_arcus", open_quantity=0.2)
        early = run(eng.run_cycle())
        assert early[0]["plan"] == "idle" and "未满最短" in early[0]["reason"]
        assert spy.calls == []

        store.upsert_task("OAI", opened_at=time.time() - 4)
        started = run(eng.run_cycle())
        assert started[0]["plan"] == "maker_started"
        _drain(eng)
        assert [name for name, _ in spy.calls] == ["place_maker_pair"]
        kw = spy.calls[0][1]
        assert kw["action"] == "close" and kw["reduce_only"] is True
        assert kw["lighter_price"] == pytest.approx(210.06)
        assert kw["arcus_price"] == pytest.approx(209.99)
        assert store.get_task("OAI")["opened_at"] is None
        logged = store.recent_cycles()[0]
        assert logged["plan"] == "close"
        assert "浮盈亏" in logged["reason"]
        assert "不超过" not in logged["reason"]
        assert "210.06" in logged["reason"] and "209.99" in logged["reason"]


def test_a_loss_worse_than_the_window_does_not_close_before_max_hold():
    """合计 -0.05、差额 0.02：盘口再有利也不提前平。"""
    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(
            tmp, row(0.2, -0.2, lighter_pnl=1.0, arcus_pnl=-1.05), dry_run=False,
        )
        eng.settings.pnl_close_usd = 0.02
        spy = CloseSpy()
        eng.executor = spy
        _override_books(eng, 210.05, 210.06, 209.99, 210.00)
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
                          opened_at=time.time() - 30, corridor_at_open=8.3,
                          open_direction="long_lighter_short_arcus", open_quantity=0.2)
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "idle" and "先不平" in d[0]["reason"]
        assert spy.calls == []
        assert store.get_task("OAI")["opened_at"] is not None


def test_sol_mark_pnl_inside_the_window_does_not_send_when_live_close_loses_more():
    """2026-10 SOL：标记浮盈亏 -0.0167 在 0.02 以内，但按即将发出的平仓价
    （Arcus 买 119.766、Lighter 卖 119.739，3.345）往返约 -0.12。不能发单。
    最长持有到了仍然可以强制挂 maker 平。
    """
    from parallax_hedge.spread_gate import round_trip_close_net

    mark = 119.75
    qty = 3.345
    lighter_entry, arcus_entry = 119.766, 119.757
    lighter_ask, arcus_bid = 119.739, 119.766
    net = round_trip_close_net(qty, lighter_entry, lighter_ask, -qty, arcus_entry, arcus_bid)
    assert net == pytest.approx(-0.12042, abs=1e-4)
    assert net < -0.02

    def sol_row():
        def leg(size, entry, pnl, liq_mult):
            return {
                "size": size, "entry_price": entry, "mark_price": mark,
                "liquidation_price": mark * liq_mult, "unrealized_pnl": pnl, "margin": 10.0,
            }
        base = row(qty, -qty, mark=mark, lighter_pnl=-0.0167, arcus_pnl=0.0)
        base["position"]["lighter"] = leg(qty, lighter_entry, -0.0167, 0.9)
        base["position"]["arcus"] = leg(-qty, arcus_entry, 0.0, 1.1)
        return base

    with tempfile.TemporaryDirectory() as tmp:
        eng, store, _ = make(tmp, sol_row(), dry_run=False)
        eng.settings.arcus_maker = True
        eng.settings.pnl_close_usd = 0.02

        async def markets(force=False):
            return [dict(MARKET, arcus_tick_size="0.001")]

        eng.service.client.common_markets = markets
        _override_books(eng, 119.730, lighter_ask, arcus_bid, 119.767)
        spy = CloseSpy()
        eng.executor = spy
        store.upsert_task(
            "OAI", enabled=1, leverage=6.0, rotation_hours=4.0,
            opened_at=time.time() - 30, corridor_at_open=8.3,
            open_direction="long_lighter_short_arcus", open_quantity=qty,
        )
        d = run(eng.run_cycle())
        assert d[0]["plan"] == "maker_started"
        _drain(eng)
        assert spy.calls == []
        assert store.get_task("OAI")["opened_at"] is not None
        logged = store.recent_cycles()[0]
        assert logged["plan"] == "close_wait"
        assert "先不平" in logged["reason"]
        assert "-0.1204" in logged["reason"] or "-0.120" in logged["reason"]

        store.upsert_task("OAI", opened_at=time.time() - 301)
        forced = run(eng.run_cycle())
        assert forced[0]["plan"] == "maker_started" and "强制" in forced[0]["reason"]
        _drain(eng)
        assert [name for name, _ in spy.calls] == ["place_maker_pair"]
        kw = spy.calls[0][1]
        assert kw["action"] == "close" and kw["reduce_only"] is True
        assert kw["quotes"]["force_close"] is True
        assert kw["lighter_side"] == "sell" and kw["arcus_side"] == "buy"
        assert store.get_task("OAI")["opened_at"] is None
