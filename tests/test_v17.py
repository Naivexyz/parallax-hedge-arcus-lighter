"""v1.7：贵的一边也开、单腿退出是 maker、停机把仓位挂 maker 平掉。"""
import asyncio
import tempfile
from pathlib import Path

import pytest

from parallax_hedge.config import Settings
from parallax_hedge.engine import HedgeEngine
from parallax_hedge.execution import Executor, LegResult
from parallax_hedge.store import Store


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _settings(**kw):
    base = dict(env_path=Path("."), data_dir=Path("."), dry_run=False,
                lighter_account_index=1, pnl_close_usd=0.02, maker_wait_seconds=8,
                order_slippage_bps=10)
    base.update(kw)
    return Settings(**base)


MARKET = {
    "lighter_symbol": "BTC", "lighter_market_index": 1, "arcus_market_id": 2,
    "arcus_symbol": "BTC-USD", "arcus_tick_size": "1", "arcus_tick_tiers": [],
}


def test_would_cross_reprices_one_tick_and_lighter_waits_for_the_fill():
    """Arcus 会吃单就挪一档再挂。Lighter 吃单只能出现在 Arcus 成交之后。"""
    ex = Executor(_settings(maker_wait_seconds=2), market=object(), dry_run=False)
    sent = []
    reads = {"n": 0}

    async def read_positions(symbol, market_id):
        reads["n"] += 1
        if reads["n"] == 1:
            return 0.0, 0.0
        return 0.0, -0.01

    async def fresh(market, lighter_side, arcus_side, closing=False):
        return True, "挂", 80001.0, 80000.0

    async def touch(market, venue, side):
        return 80000.0, 80001.0, 80000.0 if side == "buy" else 80001.0

    async def place_arcus(market, side, quantity, price, reduce_only=False, time_in_force="IOC"):
        sent.append(("arcus", time_in_force, side, float(price), reduce_only))
        n = len([x for x in sent if isinstance(x, tuple) and x[0] == "arcus"])
        if n == 1:
            return LegResult("arcus", False, submitted=False, error="POST_ONLY_WOULD_CROSS", price=price)
        return LegResult(
            "arcus", True, submitted=True,
            raw={"orderId": "a2", "timeInForce": time_in_force}, price=price,
        )

    async def place_lighter(*_a, **_k):
        sent.append("lighter-maker")
        raise AssertionError("Lighter 不能挂 maker")

    async def ioc(market_id, side, quantity, price, decimals, reduce_only=False):
        assert any(x[0] == "arcus" and x[1] == "ALO" for x in sent if isinstance(x, tuple))
        sent.append(("lighter", "IOC", side, quantity, reduce_only))
        return LegResult("lighter", True, submitted=True, raw={"timeInForce": "IOC"}, price=price)

    async def arcus_ioc(*_a, **_k):
        sent.append(("arcus", "IOC"))
        raise AssertionError("Arcus 不能吃单")

    ex.read_positions = read_positions
    ex._fresh_maker_prices = fresh
    ex._touch = touch
    ex._arcus_place = place_arcus
    ex._lighter_post_only = place_lighter
    ex._lighter_ioc = ioc
    ex._arcus_ioc = arcus_ioc
    ex._flatten = arcus_ioc
    ex.build_arcus_order = lambda *_a, **_k: {"signed": True}

    result = run(ex.place_maker_pair(
        market=MARKET, lighter_side="buy", arcus_side="sell",
        lighter_quantity=0.01, arcus_quantity=0.01,
        lighter_price=80001, arcus_price=80000,
        lighter_decimals=(5, 1), quotes={}, action="open", quantity=0.01,
    ))
    arcus = [x for x in sent if isinstance(x, tuple) and x[0] == "arcus"]
    assert arcus[0] == ("arcus", "ALO", "sell", 80000.0, False)
    assert arcus[1][1] == "ALO" and arcus[1][3] == pytest.approx(80001.0)
    assert ("lighter", "IOC", "buy", pytest.approx(0.01), False) in sent
    assert "lighter-maker" not in sent
    assert ("arcus", "IOC") not in sent
    assert result.ok and result.stage == "opened"
    ioc_at = next(i for i, x in enumerate(sent) if isinstance(x, tuple) and x[0] == "lighter")
    assert ioc_at > sent.index(arcus[1])


def test_lighter_is_not_sent_when_arcus_never_rests():
    """Arcus 没挂住：Lighter 一张都不发，也不能先有 Lighter 成交。"""
    ex = Executor(_settings(maker_wait_seconds=2), market=object(), dry_run=False)
    sent = []

    async def read_positions(symbol, market_id):
        return 0.0, 0.0

    async def fresh(market, lighter_side, arcus_side, closing=False):
        return True, "挂", 80001.0, 80000.0

    async def place_arcus(market, side, quantity, price, reduce_only=False, time_in_force="IOC"):
        sent.append(("arcus", time_in_force))
        return LegResult("arcus", False, submitted=False, error="Arcus 拒单")

    async def place_lighter(*_a, **_k):
        sent.append("lighter")
        raise AssertionError("不该发 Lighter")

    async def ioc(*_a, **_k):
        sent.append("ioc")
        raise AssertionError("不该吃 Lighter")

    ex.read_positions = read_positions
    ex._fresh_maker_prices = fresh
    ex._arcus_place = place_arcus
    ex._lighter_post_only = place_lighter
    ex._lighter_ioc = ioc
    ex._arcus_ioc = ioc
    ex.build_arcus_order = lambda *_a, **_k: {"signed": True}

    result = run(ex.place_maker_pair(
        market=MARKET, lighter_side="buy", arcus_side="sell",
        lighter_quantity=0.01, arcus_quantity=0.01,
        lighter_price=80001, arcus_price=80000,
        lighter_decimals=(5, 1), quotes={}, action="open", quantity=0.01,
    ))
    assert result.ok is False and result.stage == "arcus_not_resting"
    assert sent == [("arcus", "ALO")]
    assert "Lighter 未发" in (result.reason or "")


def test_cancel_resting_on_shutdown_does_not_take():
    class Market:
        pass

    ex = Executor(_settings(), market=Market(), dry_run=False)
    seen = []

    async def orders(market_id):
        return [{"orderId": "resting-1"}]

    async def cancel(market, order_id):
        seen.append(("arcus", order_id, market["arcus_market_id"]))
        return True, "CANCELED"

    async def lighter_all(market_index):
        seen.append(("lighter", market_index))

    async def ioc(*_a, **_k):
        seen.append("ioc")
        raise AssertionError("撤单不能改吃单")

    ex.market.arcus_open_orders = orders
    ex._arcus_cancel = cancel
    ex._lighter_cancel_all = lighter_all
    ex._arcus_ioc = ioc
    ex._lighter_ioc = ioc
    run(ex.cancel_resting_for_shutdown(MARKET))
    assert seen == [("arcus", "resting-1", 2), ("lighter", 1)]


def test_shutdown_flattens_the_three_naked_legs_as_makers():
    """停机：撤掉挂单，SPY 空、BTC 多、ETH 多都挂 post-only，不吃单。"""
    markets = [
        {"asset": "SPY", "lighter_symbol": "SPY", "lighter_market_index": 1,
         "arcus_symbol": "SPY-USD", "arcus_market_id": 10,
         "lighter_size_decimals": 4, "price_decimals": 2},
        {"asset": "BTC", "lighter_symbol": "BTC", "lighter_market_index": 2,
         "arcus_symbol": "BTC-USD", "arcus_market_id": 11,
         "lighter_size_decimals": 5, "price_decimals": 1},
        {"asset": "ETH", "lighter_symbol": "ETH", "lighter_market_index": 3,
         "arcus_symbol": "ETH-USD", "arcus_market_id": 12,
         "lighter_size_decimals": 4, "price_decimals": 2},
    ]
    state = {
        ("SPY", 10): [0.0, -0.5192],
        ("BTC", 11): [0.0, 0.0047],
        ("ETH", 12): [0.1479, 0.0],
    }
    sent = []

    class Client:
        async def common_markets(self, force=False):
            return markets

    class Service:
        def __init__(self):
            self.client = Client()

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        engine = HedgeEngine(_settings(maker_wait_seconds=5), Service(), store, dry_run=False)
        ex = engine.executor

        async def read_positions(symbol, market_id):
            return tuple(state[(symbol, int(market_id))])

        async def touch(market, venue, side):
            return 100.0, 100.02, 100.0 if side == "buy" else 100.02

        async def place(market, side, quantity, price, reduce_only=False, time_in_force="IOC"):
            sent.append(("arcus", time_in_force, side, quantity, reduce_only, market["asset"]))
            state[(market["lighter_symbol"], market["arcus_market_id"])][1] = 0.0
            return LegResult(
                "arcus", True, raw={"orderId": "x", "timeInForce": time_in_force},
                side=side, price=price,
            )

        async def post(market_id, side, quantity, price, decimals, reduce_only=False):
            asset = {1: "SPY", 2: "BTC", 3: "ETH"}[market_id]
            sent.append(("lighter", "POST_ONLY", side, quantity, reduce_only, asset))
            for key, row in state.items():
                if key[0] == asset:
                    row[0] = 0.0
            return LegResult("lighter", True, raw={"post_only": True}, side=side, price=price)

        async def cancel_resting(market):
            sent.append(("cancel", market["asset"]))

        async def ioc(*_a, **_k):
            sent.append("ioc")
            raise AssertionError("停机不能吃单")

        ex.read_positions = read_positions
        ex._touch = touch
        ex._arcus_place = place
        ex._lighter_post_only = post
        ex.cancel_resting_for_shutdown = cancel_resting
        ex._arcus_ioc = ioc
        ex._lighter_ioc = ioc
        ex._flatten = ioc
        report = run(engine.shutdown_flatten())
        store.close()

    assert ("cancel", "SPY") in sent and ("cancel", "BTC") in sent and ("cancel", "ETH") in sent
    assert ("arcus", "ALO", "buy", pytest.approx(0.5192), True, "SPY") in sent
    assert ("arcus", "ALO", "sell", pytest.approx(0.0047), True, "BTC") in sent
    assert ("lighter", "POST_ONLY", "sell", pytest.approx(0.1479), True, "ETH") in sent
    assert "ioc" not in sent
    assert all(item["post_only"] for item in report)
    assert {item["asset"] for item in report} == {"SPY", "BTC", "ETH"}
