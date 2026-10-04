"""v1.5：两边都做 maker。浮盈亏差额只读面板，不写死 0.02。

这些用例在旧行为上会失败：孤腿吃单、价差已经亏过差额仍开仓、
盘口走了还不撤 Arcus、最长持有改 IOC。
"""
import asyncio
from pathlib import Path

import pytest

from parallax_hedge.config import Settings
from parallax_hedge.execution import Executor, LegResult
from parallax_hedge.spread_gate import open_round_trip_allowed


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _settings(**kw):
    base = dict(env_path=Path("."), data_dir=Path("."), dry_run=False,
                lighter_account_index=1, pnl_close_usd=0.02, maker_wait_seconds=2)
    base.update(kw)
    return Settings(**base)


def test_open_round_trip_follows_the_panel_value_not_a_constant():
    """买 100、卖 100.03、数量 1，锁住 -0.03。差额 0.02 不开，0.05 开。"""
    bad, net, why = open_round_trip_allowed(100.0, 100.03, 1.0, 0.02)
    assert net == pytest.approx(-0.03)
    assert bad is False and "-0.02" in why
    ok, net2, why2 = open_round_trip_allowed(100.0, 100.03, 1.0, 0.05)
    assert ok and net2 == pytest.approx(-0.03) and "-0.05" in why2
    # 正好等于差额，可以开（和浮盈亏平仓同一条边界）。
    edge, _, _ = open_round_trip_allowed(100.0, 100.02, 1.0, 0.02)
    assert edge is True


def test_place_maker_pair_refuses_an_open_worse_than_the_panel_tolerance():
    sent = []

    async def boom(*_a, **_k):
        sent.append("sent")
        raise AssertionError("差额不够就不该发单")

    tight = _settings(pnl_close_usd=0.02, dry_run=True)
    ex = Executor(tight, market=None, dry_run=True)
    ex._arcus_place = boom
    ex._lighter_post_only = boom

    async def go(executor):
        return await executor.place_maker_pair(
            market={"lighter_symbol": "SPY", "lighter_market_index": 1, "arcus_market_id": 2},
            lighter_side="buy", arcus_side="sell",
            lighter_quantity=1.0, arcus_quantity=1.0,
            lighter_price=100.0, arcus_price=100.03,
            lighter_decimals=(2, 2), quotes={}, action="open", quantity=1.0,
        )

    refused = run(go(ex))
    assert refused.ok is False and refused.stage == "spread_wait"
    assert refused.quotes["pnl_close_usd"] == pytest.approx(0.02)
    assert refused.quotes["estimated_open_net"] == pytest.approx(-0.03)
    assert sent == []

    wider = _settings(pnl_close_usd=0.05, dry_run=True)
    ex2 = Executor(wider, market=None, dry_run=True)
    allowed = run(go(ex2))
    assert allowed.ok and allowed.dry_run
    assert allowed.quotes["estimated_open_net"] == pytest.approx(-0.03)
    assert allowed.lighter.raw["post_only"] and allowed.arcus.raw["timeInForce"] == "ALO"


def test_one_leg_flatten_rests_an_arcus_maker_and_does_not_ioc():
    """孤腿在 Arcus：只挂 ALO，不走 IOC / 市价。"""
    ex = Executor(_settings(pnl_close_usd=0.02), market=object(), dry_run=False)
    sent = []

    async def touch(market, venue, side):
        return 100.0, 100.02, 100.02 if side == "sell" else 100.0

    async def place(market, side, quantity, price, reduce_only=False, time_in_force="IOC"):
        sent.append(("arcus", time_in_force, side, quantity, price, reduce_only))
        return LegResult("arcus", True, raw={"orderId": "m1", "timeInForce": time_in_force})

    async def ioc(*_a, **_k):
        sent.append("ioc")
        raise AssertionError("孤腿不能 IOC")

    async def flatten(*_a, **_k):
        sent.append("flatten")
        raise AssertionError("孤腿不能吃单")

    ex._touch = touch
    ex._arcus_place = place
    ex._arcus_ioc = ioc
    ex._lighter_ioc = ioc
    ex._flatten = flatten

    leg = run(ex.flatten_orphan(
        market={"arcus_symbol": "SPY-USD", "arcus_market_id": 1,
                "lighter_symbol": "SPY", "lighter_market_index": 1},
        venue="arcus", size=-1.0, price=100.0, slippage_bps=10,
        lighter_decimals=(2, 2), entry_price=100.0,
    ))
    assert leg.ok and leg.venue == "arcus"
    assert sent == [("arcus", "ALO", "buy", 1.0, pytest.approx(100.0), True)]


def test_a_book_move_cancels_resting_arcus_before_it_fills():
    """Arcus 已经挂着、还没成交，盘口让往返差于面板差额：先撤，不吃单。"""
    ex = Executor(_settings(pnl_close_usd=0.05), market=object(), dry_run=False)
    sent = []
    phase = {"n": 0}

    async def read_positions(symbol, market_id):
        return 0.0, 0.0

    async def fresh(market, lighter_side, arcus_side, closing=False):
        phase["n"] += 1
        # 前两次（发 Arcus、发 Lighter 之前）还在差额里。第三次盘口拉开。
        if phase["n"] < 3:
            return True, "先过", 100.0, 100.01
        return True, "拉开了", 100.0, 101.0

    async def place_arcus(*a, **k):
        sent.append(("arcus", k.get("time_in_force") if False else a[-1] if a else "ALO"))
        # _arcus_place(market, side, qty, price, reduce_only, tif)
        tif = a[5] if len(a) > 5 else "ALO"
        sent[-1] = ("arcus", tif)
        return LegResult("arcus", True, submitted=True, raw={"orderId": "a1"}, price=100.01)

    async def place_lighter(*a, **k):
        sent.append(("lighter", "POST_ONLY"))
        return LegResult("lighter", True, submitted=True, raw={"client_order_index": 3}, price=100.0)

    async def cancel(*a, **k):
        sent.append("cancel")

    async def ioc(*a, **k):
        sent.append("ioc")
        raise AssertionError("盘口走了不能改吃单")

    ex.read_positions = read_positions
    ex._fresh_maker_prices = fresh
    ex._arcus_place = place_arcus
    ex._lighter_post_only = place_lighter
    ex._arcus_cancel = cancel
    ex._lighter_cancel = cancel
    ex._lighter_ioc = ioc
    ex._arcus_ioc = ioc
    ex._flatten = ioc
    ex.build_arcus_order = lambda *a, **k: {"signed": True}

    result = run(ex.place_maker_pair(
        market={"lighter_symbol": "QQQ", "lighter_market_index": 2, "arcus_market_id": 3,
                "arcus_symbol": "QQQ-USD"},
        lighter_side="buy", arcus_side="sell",
        lighter_quantity=1.0, arcus_quantity=1.0,
        lighter_price=100.0, arcus_price=100.01,
        lighter_decimals=(2, 2), quotes={}, action="open", quantity=1.0,
    ))
    assert result.ok is False and result.stage == "spread_wait"
    assert "cancel" in sent
    assert "ioc" not in sent
    assert sent[0] == ("arcus", "ALO")
    # 撤单发生在成交之前：仓位读数一直是 0，没有吃单。
    assert result.reason and ("差于" in result.reason or "不下" in result.reason or "还回去" in result.reason)


def test_max_hold_does_not_send_a_taker_when_the_touch_fails():
    """最长持有强制平：第二腿的价不合格时撤掉 Arcus，剩余只挂 maker，不 IOC。"""
    ex = Executor(_settings(pnl_close_usd=0.02), market=object(), dry_run=False)
    sent = []
    phase = {"n": 0}

    reads = {"n": 0}

    async def read_positions(symbol, market_id):
        # 第一读是下单前的对冲仓。之后 Arcus 平仓成交，Lighter 还敞着。
        reads["n"] += 1
        if reads["n"] == 1:
            return 1.0, -1.0
        return 1.0, 0.0

    async def fresh(market, lighter_side, arcus_side, closing=False):
        phase["n"] += 1
        if phase["n"] == 1:
            return True, "先挂", 100.0, 100.0
        return False, "盘口没了", 0.0, 0.0

    async def place_arcus(*a, **k):
        tif = a[5] if len(a) > 5 else "?"
        sent.append(("arcus", tif))
        return LegResult("arcus", True, submitted=True, raw={"orderId": "c1"}, price=100.0)

    async def place_lighter(*a, **k):
        sent.append(("lighter", "POST_ONLY"))
        return LegResult("lighter", True, raw={"client_order_index": 9, "post_only": True})

    async def cancel(*a, **k):
        sent.append("cancel")

    async def ioc(*a, **k):
        sent.append("ioc")
        raise AssertionError("最长持有不能改吃单")

    ex.read_positions = read_positions
    ex._fresh_maker_prices = fresh
    ex._arcus_place = place_arcus
    ex._lighter_post_only = place_lighter
    ex._arcus_cancel = cancel
    ex._lighter_cancel = cancel
    ex._lighter_ioc = ioc
    ex._arcus_ioc = ioc
    ex._flatten = ioc
    ex.build_arcus_order = lambda *a, **k: {"signed": True}
    # 没有真实盘口时退出价退回开仓价，仍然是 maker。
    ex._touch = lambda *a, **k: _touch_async()

    async def _touch_async():
        return 99.0, 99.02, 99.0

    quotes = {
        "force_close": True,
        "pnl_close_usd": 0.02,
        "close_entries": {
            "lighter_size": 1.0, "arcus_size": -1.0,
            "lighter_entry": 100.0, "arcus_entry": 100.0,
        },
    }
    result = run(ex.place_maker_pair(
        market={"lighter_symbol": "SPY", "lighter_market_index": 1, "arcus_market_id": 2,
                "arcus_symbol": "SPY-USD"},
        lighter_side="sell", arcus_side="buy",
        lighter_quantity=1.0, arcus_quantity=1.0,
        lighter_price=100.0, arcus_price=100.0,
        lighter_decimals=(2, 2), quotes=quotes, action="close", reduce_only=True,
    ))
    assert result.ok is False
    assert "ioc" not in sent
    assert ("arcus", "ALO") in sent
    assert "cancel" in sent
    assert ("lighter", "POST_ONLY") in sent
