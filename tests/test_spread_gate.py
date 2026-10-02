"""价差闸门：绝对值不超过 1 bp 才允许挂 maker，演练不下单。"""
import asyncio

import pytest
from pathlib import Path

from parallax_hedge.books import Book, Level
from parallax_hedge.config import Settings
from parallax_hedge.execution import Executor
from parallax_hedge.fills import hedge_side
from parallax_hedge.spread_gate import evaluate_books, hedge_take_price, join_price, open_sides_allowed, close_sides_allowed


def _book(venue, bid, ask):
    return Book(venue, "BTC", [Level(bid, 1)], [Level(ask, 1)])


def test_a_gap_inside_one_bp_buys_the_cheaper_ask():
    # Lighter 卖一更低：买 Lighter，卖 Arcus 买一。价差约 0.24 bp。
    gate = evaluate_books(
        _book("lighter", 209.99, 210.00),
        _book("arcus", 209.995, 210.02),
        1.0,
    )
    assert gate.ok
    assert gate.direction == "long_lighter_short_arcus"
    assert gate.abs_gap_bps < 1.0


def test_a_gap_wider_than_one_bp_does_not_open_or_schedule_a_close():
    gate = evaluate_books(
        _book("lighter", 100.00, 100.02),
        _book("arcus", 100.10, 100.12),
        1.0,
    )
    assert not gate.ok
    assert gate.abs_gap_bps > 1.0
    assert "先不开仓" in gate.reason


def test_equal_asks_have_no_direction():
    gate = evaluate_books(_book("lighter", 100, 101), _book("arcus", 100, 101), 1)
    assert not gate.ok and gate.direction is None


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
    for name, lside, aside, lpx, apx, lb, la, ab, aa in LIVE_EXPENSIVE:
        ok, bps, reason = open_sides_allowed(lside, aside, lpx, apx, 1.0)
        assert not ok, name
        assert bps is not None and (bps >= 0 or abs(bps) > 1.0), name
        assert "不下单" in reason
        gate = evaluate_books(_book("lighter", lb, la), _book("arcus", ab, aa), 1.0)
        assert not gate.ok, name
        assert gate.abs_gap_bps > 1.0


def test_eth_long_lighter_hedge_at_2662_56_is_rejected():
    """面板写 Lighter 多 / Arcus 空，实际成交 Lighter 空 2662.56、Arcus 多 2663.43。

    打算买 Lighter 时，对冲价必须是会吃到的卖一，不是 2662.56 这种买一。
    买 2662.56、卖 2663.43，绝对价差约 3.3 bp，宽于 1 bp，不许开。
    """
    ok, bps, reason = open_sides_allowed("buy", "sell", 2662.56, 2663.43, 1.0)
    assert not ok
    assert bps is not None and bps < 0 and abs(bps) > 1.0
    assert abs(bps) == pytest.approx(3.2666, abs=0.01)
    assert "不下单" in reason
    # 对冲价取卖一，排队价才是买一。两边不能混。
    book = _book("lighter", 2662.56, 2663.70)
    assert hedge_take_price(book, "buy") == 2663.70
    assert join_price(book, "buy") == 2662.56


def test_a_tight_join_longs_the_cheaper_venue():
    lighter = _book("lighter", 100.000, 100.002)
    arcus = _book("arcus", 100.001, 100.004)
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
    settings = Settings(env_path=Path("."), data_dir=Path("."), dry_run=False,
                        max_spread_bps=1)
    ex = Executor(settings, market=None, dry_run=False)
    sent = {"n": 0}

    async def boom(*_a, **_k):
        sent["n"] += 1
        raise AssertionError("不应该发出开仓单")

    ex._arcus_place = boom
    ex._lighter_post_only = boom
    ex.build_arcus_order = boom

    async def run():
        return await ex.place_maker_pair(
            market={"lighter_symbol": "ETH", "lighter_market_index": 0, "arcus_market_id": 2},
            lighter_side="sell", arcus_side="buy",
            lighter_quantity=0.1567, arcus_quantity=0.1567,
            lighter_price=2652.97, arcus_price=2654.44,
            lighter_decimals=(4, 2), quotes={}, action="open", reduce_only=False,
        )

    result = asyncio.get_event_loop().run_until_complete(run())
    assert result.ok is False and result.stage == "spread_wait"
    assert sent["n"] == 0
    assert result.notes and "尚未下任何单" in result.notes[0]
    assert result.quotes["join_gap_bps"] > 1


def test_place_maker_pair_refuses_a_planned_close_when_buy_is_not_below_sell():
    """计划内平仓用同一个价差阈值。买价等于或高于卖价时不下单。"""
    settings = Settings(env_path=Path("."), data_dir=Path("."), dry_run=False,
                        max_spread_bps=1.1)
    ex = Executor(settings, market=None, dry_run=False)
    sent = {"n": 0}

    async def boom(*_a, **_k):
        sent["n"] += 1
        raise AssertionError("不应该发出平仓单")

    ex._arcus_place = boom
    ex._lighter_post_only = boom
    ex.build_arcus_order = boom

    async def run():
        return await ex.place_maker_pair(
            market={"lighter_symbol": "ETH", "lighter_market_index": 0, "arcus_market_id": 2},
            lighter_side="buy", arcus_side="sell",
            lighter_quantity=0.1, arcus_quantity=0.1,
            lighter_price=100.0, arcus_price=100.0,
            lighter_decimals=(4, 2), quotes={}, action="close", reduce_only=True,
        )

    result = asyncio.get_event_loop().run_until_complete(run())
    assert result.ok is False and result.stage == "close_wait"
    assert sent["n"] == 0
    assert "不低于" in result.reason


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
    assert "价差有利" in result.quotes["close_prices"]
    assert "100.03" in result.quotes["close_prices"] and "100" in result.quotes["close_prices"]
    ok, bps, reason = close_sides_allowed("sell", "buy", 100.03, 100.00)
    assert ok and bps < 0 and abs(bps) == pytest.approx(3.0, abs=0.02)
    assert "不超过" not in reason


def test_an_unfavorable_close_wider_than_the_threshold_is_not_sent():
    """平仓买价高于卖价约 3 bp：提前平仓单不发出。"""
    settings = Settings(env_path=Path("."), data_dir=Path("."), dry_run=False, max_spread_bps=1)
    ex = Executor(settings, market=None, dry_run=False)
    sent = {"n": 0}

    async def boom(*_a, **_k):
        sent["n"] += 1
        raise AssertionError("不应该发出平仓单")

    ex._arcus_place = boom
    ex._lighter_post_only = boom
    ex.build_arcus_order = boom
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
    assert result.ok is False and result.stage == "close_wait"
    assert sent["n"] == 0
    assert "不低于" in result.reason
