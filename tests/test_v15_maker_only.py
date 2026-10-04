"""两边都做 maker。所间价差不拦开仓。浮盈亏差额只看每一边自己的价格变化。

孤腿不吃单，盘口方向反了要撤，最长持有不改 IOC。
"""
import asyncio
from pathlib import Path

import pytest

from parallax_hedge.config import Settings
from parallax_hedge.execution import Executor, LegResult, _close_send_allowed
from parallax_hedge.spread_gate import round_trip_close_net, unrealized_close_ready


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _settings(**kw):
    base = dict(env_path=Path("."), data_dir=Path("."), dry_run=False,
                lighter_account_index=1, pnl_close_usd=0.02, maker_wait_seconds=2)
    base.update(kw)
    return Settings(**base)


def test_a_standing_premium_still_posts_both_makers():
    """买 100、卖 100.17（约 17 bp）。所间价差不拦。差额 0.02 也两边都挂 maker。"""
    sent = []

    async def boom(*_a, **_k):
        sent.append("sent")
        raise AssertionError("演练不该真的发单")

    ex = Executor(_settings(pnl_close_usd=0.02, dry_run=True), market=None, dry_run=True)
    ex._arcus_place = boom
    ex._lighter_post_only = boom

    result = run(ex.place_maker_pair(
        market={"lighter_symbol": "SPY", "lighter_market_index": 1, "arcus_market_id": 2},
        lighter_side="buy", arcus_side="sell",
        lighter_quantity=1.0, arcus_quantity=1.0,
        lighter_price=100.0, arcus_price=100.17,
        lighter_decimals=(2, 2), quotes={}, action="open", quantity=1.0,
    ))
    assert result.ok and result.dry_run and result.stage == "dry_run"
    assert sent == []
    assert result.lighter.raw["post_only"] is True
    assert result.lighter.raw["side"] == "buy" and result.lighter.raw["price"] == pytest.approx(100.0)
    assert result.arcus.raw["timeInForce"] == "ALO"
    assert result.arcus.raw["side"] == "sell" and result.arcus.raw["price"] == pytest.approx(100.17)
    assert "estimated_open_net" not in result.quotes


def test_paired_maker_close_ignores_the_cross_venue_premium():
    """各边平仓价等于自己的开仓价，两所差 17 bp，合计是 0，差额 0.02 允许平。"""
    net = round_trip_close_net(1.0, 100.0, 100.0, -1.0, 100.17, 100.17)
    assert net == pytest.approx(0.0)
    assert unrealized_close_ready(net, 0.02)
    # 只有一边自己的价格动了：Lighter 从 100 平到 99.97，亏 0.03，差额 0.02 先不平。
    moved = round_trip_close_net(1.0, 100.0, 99.97, -1.0, 100.17, 100.17)
    assert moved == pytest.approx(-0.03)
    assert not unrealized_close_ready(moved, 0.02)
    assert unrealized_close_ready(moved, 0.05)

    ex = Executor(_settings(pnl_close_usd=0.02, dry_run=True), market=None, dry_run=True)
    quotes = {
        "pnl_close_usd": 0.02,
        "force_close": False,
        "close_entries": {
            "lighter_size": 1.0, "arcus_size": -1.0,
            "lighter_entry": 100.0, "arcus_entry": 100.17,
        },
    }
    result = run(ex.place_maker_pair(
        market={"lighter_symbol": "SPY", "lighter_market_index": 1, "arcus_market_id": 2},
        lighter_side="sell", arcus_side="buy",
        lighter_quantity=1.0, arcus_quantity=1.0,
        lighter_price=100.0, arcus_price=100.17,
        lighter_decimals=(2, 2), quotes=quotes, action="close", reduce_only=True,
    ))
    assert result.ok and result.stage == "closed"
    assert result.lighter.raw["post_only"] and result.arcus.raw["timeInForce"] == "ALO"
    assert result.lighter.raw["price"] == pytest.approx(100.0)
    assert result.arcus.raw["price"] == pytest.approx(100.17)
    allowed, why = _close_send_allowed(quotes, 100.0, 100.17)
    assert allowed and "+0.0000" in why
    blocked, why_bad = _close_send_allowed(quotes, 99.97, 100.17)
    assert blocked is False and "-0.0300" in why_bad
    assert quotes["pnl_close_usd"] == pytest.approx(0.02)


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
    """买的一边不再更便宜：先撤还没成交的 Arcus，不吃单。所间价差变宽本身不撤。"""
    ex = Executor(_settings(pnl_close_usd=0.05), market=object(), dry_run=False)
    sent = []
    phase = {"n": 0}

    async def read_positions(symbol, market_id):
        return 0.0, 0.0

    async def fresh(market, lighter_side, arcus_side, closing=False):
        phase["n"] += 1
        # 前两次买 100、卖 100.20，价差宽也过。第三次买价不再低于卖价。
        if phase["n"] < 3:
            return True, "先过", 100.0, 100.20
        return False, "买价 102 不低于卖价 100.2，不下单", 102.0, 100.20

    async def place_arcus(*a, **k):
        sent.append(("arcus", k.get("time_in_force") if False else a[-1] if a else "ALO"))
        # _arcus_place(market, side, qty, price, reduce_only, tif)
        tif = a[5] if len(a) > 5 else "ALO"
        sent[-1] = ("arcus", tif)
        return LegResult("arcus", True, submitted=True, raw={"orderId": "a1"}, price=a[3] if len(a) > 3 else 100.20)

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
        lighter_price=100.0, arcus_price=100.20,
        lighter_decimals=(2, 2), quotes={}, action="open", quantity=1.0,
    ))
    assert result.ok is False and result.stage == "spread_wait"
    assert "cancel" in sent
    assert "ioc" not in sent
    assert sent[0] == ("arcus", "ALO")
    # 撤单发生在成交之前：仓位读数一直是 0，没有吃单。
    assert result.reason and ("不低于" in result.reason or "不下" in result.reason)


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
