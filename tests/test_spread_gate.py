"""所间价差不拦开仓。平仓只看各边自己的盈亏。"""
import asyncio

import pytest
from pathlib import Path

from parallax_hedge.books import Book, Level
from parallax_hedge.config import Settings
from parallax_hedge.execution import Executor
from parallax_hedge.fills import hedge_side
from parallax_hedge.spread_gate import (
    evaluate_books, hedge_take_price, join_price, open_sides_allowed,
    close_sides_allowed, unrealized_close_ready,
)


def _book(venue, bid, ask):
    return Book(venue, "BTC", [Level(bid, 1)], [Level(ask, 1)])


def test_a_gap_inside_one_bp_buys_the_cheaper_ask():
    # Lighter 卖一更低：买 Lighter，卖 Arcus 买一。价差约 0.24 bp。
    gate = evaluate_books(
        _book("lighter", 209.99, 210.00),
        _book("arcus", 210.01, 210.03),
        1.0,
    )
    assert gate.ok
    assert gate.direction == "long_lighter_short_arcus"
    assert gate.abs_gap_bps < 1.0


def test_a_gap_wider_than_one_bp_still_buys_the_cheaper_ask():
    """买价低于卖价，即使绝对价差远宽于 1 bp，也开。bp 阈值不再拦。"""
    gate = evaluate_books(
        _book("lighter", 100.00, 100.02),
        _book("arcus", 100.10, 100.12),
        1.0,
    )
    assert gate.ok
    assert gate.direction == "long_lighter_short_arcus"
    assert gate.abs_gap_bps > 1.0
    assert "先不开仓" not in gate.reason


def test_equal_asks_have_no_direction():
    """卖一相同也不拦。给一个方向，两边都能挂。"""
    gate = evaluate_books(_book("lighter", 100, 101), _book("arcus", 100, 101), 1)
    assert gate.ok and gate.direction == "long_lighter_short_arcus"


def test_dry_run_maker_pair_does_not_sleep_or_sign():
    settings = Settings(env_path=Path("."), data_dir=Path("."), dry_run=True,
                        max_spread_bps=1, min_hold_sec=3, max_hold_sec=300)
    assert settings.dry_run is True
    ex = Executor(settings, market=None, dry_run=True)
    called = {"n": 0}
    ex._get_lighter_signer = lambda: called.__setitem__("n", called["n"] + 1)
    ex._get_arcus_key = lambda: called.__setitem__("n", called["n"] + 1)

    async def run():
        return await ex.place_maker_pair(
            market={"lighter_symbol": "BTC", "lighter_market_index": 1, "arcus_market_id": 1},
            lighter_side="buy", arcus_side="sell",
            lighter_quantity=0.01, arcus_quantity=0.01,
            lighter_price=100.0, arcus_price=100.01,
            lighter_decimals=(5, 1), quotes={}, action="open",
        )

    result = asyncio.get_event_loop().run_until_complete(run())
    assert result.ok and result.dry_run and result.stage == "dry_run"
    assert result.elapsed_ms < 500
    assert "至少持有 3" in result.notes[0] and "300" in result.notes[0]
    assert "同时挂" in result.notes[0]
    assert "不间隔" in result.notes[0]
    assert called["n"] == 0
    assert result.lighter.raw["post_only"] and result.arcus.raw["timeInForce"] == "ALO"


# 2026-10-03 实盘三笔：买在更贵的一边、卖在更便宜的一边，价差都是好几 bp。
# 盘口按成交价还原：买单成交价当作该所买一，卖单成交价当作该所卖一。
LIVE_EXPENSIVE = (
    # ETH: Lighter 空 2652.97，Arcus 多 2654.44
    ("ETH", "sell", "buy", 2652.97, 2654.44, 2652.90, 2652.97, 2654.44, 2654.51),
    # SOL: Lighter 空 117.6800，Arcus 多 117.7580
    ("SOL", "sell", "buy", 117.6800, 117.7580, 117.6700, 117.6800, 117.7580, 117.7700),
    # ZEC: Lighter 多 1202.1500，Arcus 空 1200.6910
    ("ZEC", "buy", "sell", 1202.1500, 1200.6910, 1202.1500, 1202.2500, 1200.6000, 1200.6910),
)


def test_the_three_live_expensive_fills_are_rejected():
    """买在更贵的一边也不拒绝。所间价差不是不开仓的理由。"""
    for name, lside, aside, lpx, apx, lb, la, ab, aa in LIVE_EXPENSIVE:
        ok, bps, reason = open_sides_allowed(lside, aside, lpx, apx, 1.0)
        assert ok, name
        assert bps is not None and bps >= 0, name
        assert "不下单" not in reason
        gate = evaluate_books(_book("lighter", lb, la), _book("arcus", ab, aa), 1.0)
        assert gate.ok, name


def test_eth_long_lighter_hedge_wider_than_one_bp_is_still_allowed():
    """买 2662.56、卖 2663.43，约 3.3 bp。买价更低，不再因宽度拒绝。

    对冲价仍必须是会吃到的卖一，不是买一。买价不低于卖价的那一笔才拒绝。
    """
    ok, bps, reason = open_sides_allowed("buy", "sell", 2662.56, 2663.43, 1.0)
    assert ok
    assert bps is not None and bps < 0 and abs(bps) > 1.0
    assert abs(bps) == pytest.approx(3.2666, abs=0.01)
    assert "不下单" not in reason
    wrong, wrong_bps, wrong_reason = open_sides_allowed("sell", "buy", 2662.56, 2663.43, 1.0)
    assert wrong and wrong_bps > 0 and "不下单" not in wrong_reason
    # 对冲价取卖一，排队价才是买一。两边不能混。
    book = _book("lighter", 2662.56, 2663.70)
    assert hedge_take_price(book, "buy") == 2663.70
    assert join_price(book, "buy") == 2662.56


def test_a_tight_join_longs_the_cheaper_venue():
    lighter = _book("lighter", 100.000, 100.001)
    arcus = _book("arcus", 100.003, 100.005)
    gate = evaluate_books(lighter, arcus, 1.0)
    assert gate.ok
    assert gate.direction == "long_lighter_short_arcus"
    lighter_side, arcus_side = hedge_side(gate.direction)
    ok, bps, _reason = open_sides_allowed(
        lighter_side, arcus_side,
        join_price(lighter, lighter_side), join_price(arcus, arcus_side),
        1.0,
    )
    assert ok and bps < 0 and abs(bps) < 1.0
    # 便宜的一边是 Lighter（卖一更低，买一也更低），多头必须在 Lighter
    assert lighter_side == "buy" and arcus_side == "sell"


def test_place_maker_pair_refuses_the_expensive_side_before_sending():
    """Lighter 买 80001、Arcus 卖 80000：买的更贵也两边都挂 maker。"""
    settings = Settings(env_path=Path("."), data_dir=Path("."), dry_run=True, pnl_close_usd=0.02)
    ex = Executor(settings, market=None, dry_run=True)

    async def run():
        return await ex.place_maker_pair(
            market={"lighter_symbol": "BTC", "lighter_market_index": 1, "arcus_market_id": 2},
            lighter_side="buy", arcus_side="sell",
            lighter_quantity=0.01, arcus_quantity=0.01,
            lighter_price=80001, arcus_price=80000,
            lighter_decimals=(5, 1), quotes={}, action="open", quantity=0.01,
        )

    result = asyncio.get_event_loop().run_until_complete(run())
    assert result.ok and result.dry_run
    assert result.lighter.raw["post_only"] and result.lighter.raw["price"] == 80001
    assert result.arcus.raw["timeInForce"] == "ALO" and result.arcus.raw["price"] == 80000
    assert result.lighter.raw["side"] == "buy" and result.arcus.raw["side"] == "sell"


def test_place_maker_pair_close_is_not_blocked_by_price_direction():
    """计划内平仓不看买价是否低于卖价。演练里两边都记成 maker，不吃单。"""
    settings = Settings(env_path=Path("."), data_dir=Path("."), dry_run=True,
                        min_hold_sec=3, max_hold_sec=300, pnl_close_usd=0.02)
    ex = Executor(settings, market=None, dry_run=True)

    async def run():
        return await ex.place_maker_pair(
            market={"lighter_symbol": "ETH", "lighter_market_index": 0, "arcus_market_id": 2},
            lighter_side="buy", arcus_side="sell",
            lighter_quantity=0.1, arcus_quantity=0.1,
            lighter_price=100.0, arcus_price=100.0,
            lighter_decimals=(4, 2), quotes={}, action="close", reduce_only=True,
        )

    result = asyncio.get_event_loop().run_until_complete(run())
    assert result.ok and result.dry_run and result.stage == "closed"
    assert result.lighter.raw["post_only"] and result.arcus.raw["timeInForce"] == "ALO"
    assert "不低于" not in (result.reason or "")


def test_a_favorable_close_wider_than_the_threshold_is_sent():
    """平仓买价比卖价便宜约 3 bp：宽于 1 bp 阈值，仍然把 maker 平仓单发出去。"""
    settings = Settings(env_path=Path("."), data_dir=Path("."), dry_run=False,
                        max_spread_bps=1, maker_wait_seconds=2, lighter_account_index=1)
    ex = Executor(settings, market=object(), dry_run=False)
    sent = []
    reads = {"n": 0}

    async def read_positions(symbol, market_id):
        reads["n"] += 1
        if reads["n"] == 1:
            return 0.1, -0.1
        return 0.0, 0.0

    async def fresh(market, lighter_side, arcus_side, closing=False):
        assert closing is True
        return True, "有利", 100.03, 100.00

    async def place_arcus(*a, **k):
        sent.append(("arcus", a, k))
        from parallax_hedge.execution import LegResult
        return LegResult("arcus", True, raw={"orderId": "c1"})

    async def place_lighter(*a, **k):
        sent.append(("lighter", a, k))
        from parallax_hedge.execution import LegResult
        return LegResult("lighter", True, raw={"client_order_index": 7})

    ex.read_positions = read_positions
    ex._fresh_maker_prices = fresh
    ex._arcus_place = place_arcus
    ex._lighter_post_only = place_lighter
    ex.build_arcus_order = lambda *a, **k: {"signed": True}

    async def run():
        return await ex.place_maker_pair(
            market={"lighter_symbol": "ETH", "lighter_market_index": 0, "arcus_market_id": 2,
                    "arcus_symbol": "ETH-USD"},
            lighter_side="sell", arcus_side="buy",
            lighter_quantity=0.1, arcus_quantity=0.1,
            lighter_price=100.03, arcus_price=100.00,
            lighter_decimals=(4, 2), quotes={}, action="close", reduce_only=True,
        )

    result = asyncio.get_event_loop().run_until_complete(run())
    assert result.ok and result.stage == "closed"
    assert [name for name, *_ in sent] == ["arcus", "lighter"]
    assert "挂 maker 平仓" in result.quotes["close_prices"]
    assert "100.03" in result.quotes["close_prices"] and "100" in result.quotes["close_prices"]
    ok, bps, reason = close_sides_allowed("sell", "buy", 100.03, 100.00)
    assert ok and bps < 0 and abs(bps) == pytest.approx(3.0, abs=0.02)
    assert "不超过" not in reason


def test_pnl_window_boundary():
    """合计 -0.02、差额 0.02 可以平；-0.05 不行。+1 与 -1 合计 0，可以平。"""
    assert unrealized_close_ready(0.0, 0.02)
    assert unrealized_close_ready(-0.02, 0.02)
    assert unrealized_close_ready(1.0 + -1.0, 0.02)
    assert not unrealized_close_ready(-0.05, 0.02)
    assert not unrealized_close_ready(-0.0200001, 0.02)


def test_an_unfavorable_close_is_still_a_maker_order():
    """平仓买价高于卖价也不改吃单，演练仍记 maker。"""
    settings = Settings(env_path=Path("."), data_dir=Path("."), dry_run=True, pnl_close_usd=0.02)
    ex = Executor(settings, market=None, dry_run=True)
    ok, bps, _reason = close_sides_allowed("sell", "buy", 100.00, 100.03)
    assert not ok and bps > 0 and abs(bps) == pytest.approx(3.0, abs=0.02)

    async def run():
        return await ex.place_maker_pair(
            market={"lighter_symbol": "ETH", "lighter_market_index": 0, "arcus_market_id": 2},
            lighter_side="sell", arcus_side="buy",
            lighter_quantity=0.1, arcus_quantity=0.1,
            lighter_price=100.00, arcus_price=100.03,
            lighter_decimals=(4, 2), quotes={}, action="close", reduce_only=True,
        )

    result = asyncio.get_event_loop().run_until_complete(run())
    assert result.ok and result.stage == "closed" and result.dry_run
    assert result.lighter.raw["post_only"] and result.arcus.raw["timeInForce"] == "ALO"


def test_sol_second_maker_that_fails_the_window_is_not_ioc_hedged():
    """Arcus 先挂上之后，Lighter 的价会把 3.345 SOL 的往返打到约 -0.12。
    撤掉未成交的 maker，不能 IOC。
    """
    from parallax_hedge.execution import LegResult
    from parallax_hedge.spread_gate import round_trip_close_net

    qty = 3.345
    quotes = {
        "pnl_close_usd": 0.02,
        "force_close": False,
        "close_entries": {
            "lighter_size": qty,
            "arcus_size": -qty,
            "lighter_entry": 119.766,
            "arcus_entry": 119.757,
        },
    }
    bad = round_trip_close_net(qty, 119.766, 119.739, -qty, 119.757, 119.766)
    assert bad < -0.02

    settings = Settings(env_path=Path("."), data_dir=Path("."), dry_run=False,
                        pnl_close_usd=0.02, maker_wait_seconds=2, lighter_account_index=1)
    ex = Executor(settings, market=object(), dry_run=False)
    sent = []
    phase = {"n": 0}

    async def read_positions(symbol, market_id):
        return qty, -qty

    async def fresh(market, lighter_side, arcus_side, closing=False):
        phase["n"] += 1
        # 第一次刷新还过得了；挂出 Arcus 之后 Lighter 变成 119.739，过不了。
        return True, "按这个价会亏过差额", 119.739, 119.766

    async def place_arcus(*a, **k):
        sent.append("arcus")
        raise AssertionError("差额不够就不该先挂 Arcus")

    async def place_lighter(*a, **k):
        sent.append("lighter")
        raise AssertionError("不该再挂 Lighter")

    async def ioc(*a, **k):
        sent.append("ioc")
        raise AssertionError("不该 IOC 对冲")

    async def flatten(*a, **k):
        sent.append("flatten")
        raise AssertionError("不该吃单对冲")

    ex.read_positions = read_positions
    ex._fresh_maker_prices = fresh
    ex._arcus_place = place_arcus
    ex._lighter_post_only = place_lighter
    ex._lighter_ioc = ioc
    ex._flatten = flatten
    async def cancel(*a, **k):
        sent.append("cancel")

    ex._arcus_cancel = cancel
    ex.build_arcus_order = lambda *a, **k: {"signed": True}

    async def run():
        return await ex.place_maker_pair(
            market={"lighter_symbol": "SOL", "lighter_market_index": 0, "arcus_market_id": 2,
                    "arcus_symbol": "SOL-USD"},
            lighter_side="sell", arcus_side="buy",
            lighter_quantity=qty, arcus_quantity=qty,
            lighter_price=119.764, arcus_price=119.757,
            lighter_decimals=(3, 3), quotes=quotes, action="close", reduce_only=True,
        )

    result = asyncio.get_event_loop().run_until_complete(run())
    assert result.ok is False and result.stage == "close_wait"
    assert sent == []
    assert "尚未下任何单" in (result.notes or [""])[0]
    assert "先不平" in (result.reason or "")
