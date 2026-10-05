"""v1.9：开仓不看资金费。一边会吃单或约 15 秒没成交就翻到另一边，只翻一次。

翻之前必须先确认撤单、重读仓位；有部分成交先在 Lighter 吃单对冲，不再翻。
Lighter 吃单只出现在 Arcus 成交之后。
"""
import asyncio
import time
from pathlib import Path

import pytest

from parallax_hedge.config import Settings
from parallax_hedge.execution import OPEN_SIDE_ATTEMPT_SEC, Executor, LegResult
from parallax_hedge.funding import DirectionChoice
from parallax_hedge.positions import HedgeHealth, LegPosition
from parallax_hedge.risk import PreOpenDecision, RiskVerdict
from parallax_hedge.scheduler import TaskState, decide


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _settings(**kw):
    base = dict(
        env_path=Path("."), data_dir=Path("."), dry_run=False,
        lighter_account_index=1, pnl_close_usd=0.02,
        maker_wait_seconds=30, open_side_attempt_seconds=0.4,
        order_slippage_bps=10,
    )
    base.update(kw)
    return Settings(**base)


MARKET = {
    "lighter_symbol": "BTC", "lighter_market_index": 1, "arcus_market_id": 2,
    "arcus_symbol": "BTC-USD", "arcus_tick_size": "1", "arcus_tick_tiers": [],
}


def test_open_attempt_default_is_fifteen_seconds():
    assert OPEN_SIDE_ATTEMPT_SEC == 15.0
    assert Settings(env_path=Path("."), data_dir=Path(".")).open_side_attempt_seconds == 15.0


def test_funding_does_not_choose_the_open_side():
    """资金费写着 Lighter 空 / Arcus 多，调度器也不把它写进开仓计划。"""
    choice = DirectionChoice(
        symbol="SNDK", direction="short_lighter_long_arcus", net_bps_per_hour=1.9,
        lighter_bps_per_hour=2.0, arcus_bps_per_hour=0.1, tradable=True,
    )
    health = HedgeHealth(
        asset="SNDK",
        lighter=LegPosition("lighter", "SNDK", 0.0, 1000, 1000, None, 0.0, 10),
        arcus=LegPosition("arcus", "SNDK", 0.0, 1000, 1000, None, 0.0, 10),
        warn_distance_pct=5,
    )
    task = TaskState(asset="SNDK", enabled=True, rotation_hours=4, leverage=10)
    d = decide(
        task=task, health=health, risk=RiskVerdict("none", None), choice=choice,
        pre_open=PreOpenDecision(True, None, 5.0, 10.0, 0.2), quantity=0.2,
        now=1_800_000_000.0,
    )
    assert d.plan == "open"
    assert d.direction is None
    assert "资金费不选方向" in d.reason


class Venue:
    """假的 Arcus：记录发单 / 撤单顺序，按剧本给仓位。"""

    def __init__(self, ex, *, fills=None, cross_sides=(), cancel_ok=True):
        self.log = []
        self.position = 0.0
        self.fills = fills or {}          # side -> 挂上后第几次读仓位成交多少（带符号）
        self.cross_sides = set(cross_sides)
        self.cancel_ok = cancel_ok
        self.reads_since_post = 0
        self.side = None
        self.resting = set()
        ex.read_positions = self.read_positions
        ex._arcus_place = self.place
        ex._lighter_ioc = self.ioc
        ex._lighter_post_only = self.forbidden
        ex._arcus_ioc = self.forbidden
        ex._cancel_arcus_confirmed = self.cancel_confirmed
        ex._cancel_maker_resting = self.cancel_plain

    async def read_positions(self, symbol, market_id):
        self.reads_since_post += 1
        plan = self.fills.get(self.side)
        if plan and self.reads_since_post == plan[0]:
            self.position += plan[1]
            self.fills.pop(self.side)
        return 0.0, self.position

    async def place(self, market, side, quantity, price, reduce_only=False, time_in_force="IOC"):
        assert time_in_force == "ALO", "Arcus 只能挂 maker"
        assert not (self.resting - {side}), "另一边还挂着，不能挂这一边"
        self.log.append(("arcus", side))
        if side in self.cross_sides:
            return LegResult("arcus", False, submitted=False,
                             error="POST_ONLY_WOULD_CROSS", price=price)
        self.side = side
        self.reads_since_post = 0
        self.resting.add(side)
        return LegResult("arcus", True, submitted=True,
                         raw={"orderId": f"o-{side}", "timeInForce": "ALO"}, price=price)

    async def ioc(self, market_id, side, quantity, price, decimals, reduce_only=False):
        assert self.position != 0.0, "Arcus 没成交不能吃 Lighter"
        self.log.append(("lighter", side, round(quantity, 8)))
        return LegResult("lighter", True, submitted=True, raw={"timeInForce": "IOC"}, price=price)

    async def cancel_confirmed(self, market, arcus):
        self.log.append(("cancel", arcus.side if arcus else None))
        if self.cancel_ok:
            self.resting.clear()
        return self.cancel_ok

    async def cancel_plain(self, market, arcus, lighter):
        self.log.append(("cancel_plain",))
        self.resting.clear()

    async def forbidden(self, *_a, **_k):
        raise AssertionError("Lighter 不挂 maker，Arcus 不吃单")


def _executor(**kw):
    ex = Executor(_settings(**kw), market=object(), dry_run=False)
    ex.build_arcus_order = lambda *_a, **_k: {"signed": True}

    async def fresh(market, lighter_side, arcus_side, closing=False):
        if arcus_side == "sell":
            return True, "挂", 80001.0, 80000.0
        return True, "挂", 80000.0, 80001.0

    async def touch(market, venue, side):
        return 80000.0, 80001.0, 80000.0 if side == "buy" else 80001.0

    ex._fresh_maker_prices = fresh
    ex._touch = touch
    return ex


def _open(ex, action="open", **kw):
    args = dict(
        market=MARKET, lighter_side="buy", arcus_side="sell",
        lighter_quantity=0.01, arcus_quantity=0.01,
        lighter_price=80001, arcus_price=80000,
        lighter_decimals=(5, 1), quotes={}, action=action, quantity=0.01,
    )
    args.update(kw)
    return run(ex.place_maker_pair(**args))


def test_would_cross_flips_once_and_second_failure_returns():
    ex = _executor()
    v = Venue(ex, cross_sides={"sell", "buy"})
    result = _open(ex)
    arcus_sides = [x[1] for x in v.log if x[0] == "arcus"]
    assert arcus_sides[0] == "sell" and arcus_sides[-1] == "buy"
    assert arcus_sides.index("buy") > 0
    # 第一边会吃单：先确认没有残单，再挂另一边；第二边失败就返回，不再翻回卖
    first_buy = v.log.index(("arcus", "buy"))
    assert ("cancel", "sell") in v.log[:first_buy]
    assert not any(x[0] == "lighter" for x in v.log)
    assert result.ok is False and result.stage == "arcus_not_resting"
    assert result.quotes.get("open_flipped") is True


def test_unfilled_side_cancels_confirmed_then_flips_and_lighter_follows_the_fill():
    ex = _executor()
    v = Venue(ex, fills={"buy": (2, 0.01)})
    t0 = time.monotonic()
    result = _open(ex)
    elapsed = time.monotonic() - t0
    assert v.log.index(("cancel", "sell")) < v.log.index(("arcus", "buy"))
    ioc_at = v.log.index(("lighter", "sell", 0.01))
    assert ioc_at > v.log.index(("arcus", "buy"))
    assert [x for x in v.log if x[0] == "lighter"] == [("lighter", "sell", 0.01)]
    assert result.ok and result.stage == "opened"
    assert result.quotes.get("filled_direction") == "short_lighter_long_arcus"
    assert result.quotes.get("open_flipped") is True
    assert elapsed < 5.0          # 第一边约 0.4 秒就翻，不是等满 30 秒


def test_both_sides_unfilled_cancel_and_return_without_lighter():
    ex = _executor()
    v = Venue(ex)
    result = _open(ex)
    assert [x[1] for x in v.log if x[0] == "arcus"] == ["sell", "buy"]
    assert v.log[-1] == ("cancel", "buy")
    assert not any(x[0] == "lighter" for x in v.log)
    assert result.ok is False and result.stage == "legs_timeout"
    assert "两边" in result.reason


def test_partial_fill_is_hedged_on_lighter_and_does_not_flip():
    """第一边只成交 0.004：Lighter 先吃 0.004，剩余撤掉，不翻到另一边。"""
    ex = _executor()
    v = Venue(ex, fills={"sell": (1, -0.004)})
    result = _open(ex)
    assert ("arcus", "buy") not in v.log
    assert ("lighter", "buy", 0.004) in v.log
    assert v.log.index(("lighter", "buy", 0.004)) < v.log.index(("cancel", "sell"))
    assert result.ok and result.stage == "opened"
    assert result.quotes["filled_quantity"] == pytest.approx(0.004)
    assert result.quotes.get("partial_fill") is True
    assert result.quotes.get("open_flipped") is not True


def test_fill_during_the_cancel_is_hedged_and_does_not_flip():
    """撤单那一刻才成交：撤完重读仓位看到成交，Lighter 吃单，不翻边。"""
    ex = _executor()
    v = Venue(ex)

    async def cancel_and_fill(market, arcus):
        v.log.append(("cancel", arcus.side))
        v.position += -0.01
        v.resting.clear()
        return True

    ex._cancel_arcus_confirmed = cancel_and_fill
    result = _open(ex)
    assert ("arcus", "buy") not in v.log
    assert v.log.index(("cancel", "sell")) < v.log.index(("lighter", "buy", 0.01))
    assert result.ok and result.stage == "opened"


def test_unconfirmed_cancel_never_posts_the_other_side():
    ex = _executor()
    v = Venue(ex, cancel_ok=False)
    result = _open(ex)
    assert ("arcus", "buy") not in v.log
    assert not any(x[0] == "lighter" for x in v.log)
    assert result.ok is False and result.stage == "legs_timeout"
    assert "撤单没确认" in result.reason


def test_close_does_not_flip_and_waits_maker_wait():
    ex = _executor(maker_wait_seconds=1)
    v = Venue(ex)
    result = _open(ex, action="close", lighter_side="sell", arcus_side="buy", reduce_only=True)
    assert [x[1] for x in v.log if x[0] == "arcus"] == ["buy"]
    assert result.ok is False and result.stage == "legs_timeout"
    assert result.quotes.get("open_flipped") is not True


def test_confirmed_cancel_checks_open_orders_and_clears_leftovers():
    class Market:
        def __init__(self):
            self.orders = [
                {"orderId": "o-sell", "clientId": "ph1"},
                {"orderId": "old", "clientId": "ph0"},
                {"orderId": "manual", "clientId": "mine"},
            ]

        async def arcus_open_orders(self, market_id):
            return list(self.orders)

    m = Market()
    ex = Executor(_settings(), market=m, dry_run=False)
    cancelled = []

    async def arcus_cancel(market, oid):
        cancelled.append(oid)
        m.orders = [o for o in m.orders if o["orderId"] != oid]
        return True, "CANCELED"

    ex._arcus_cancel = arcus_cancel
    leg = LegResult("arcus", True, raw={"orderId": "o-sell", "clientId": "ph1"})
    assert run(ex._cancel_arcus_confirmed(MARKET, leg, gap=0)) is True
    assert "o-sell" in cancelled and "old" in cancelled and "manual" not in cancelled

    class Down:
        async def arcus_open_orders(self, market_id):
            raise RuntimeError("timeout")

    ex2 = Executor(_settings(), market=Down(), dry_run=False)
    ex2._arcus_cancel = arcus_cancel
    leg2 = LegResult("arcus", True, raw={"orderId": "x", "clientId": "ph2"})
    assert run(ex2._cancel_arcus_confirmed(MARKET, leg2, attempts=2, gap=0)) is False
    # 当场被拒、从没挂住：没有残单
    rejected = LegResult("arcus", False, submitted=False, error="POST_ONLY_WOULD_CROSS")
    assert run(ex2._cancel_arcus_confirmed(MARKET, rejected)) is True
