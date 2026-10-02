"""成交判定的用例。

核心教训（2026-09-17 实盘）：订单返回 code=200 + tx_hash 不代表成交，
filled_delta 才算数。这些用例锁住「只认仓位变化」以及
「宁可把极小成交当成交」的取舍。
"""
import math

import pytest

from parallax_hedge.fills import (
    MEANINGFUL_FILL_FRACTION,
    align_quantity,
    classify_fill,
    closing_sides,
    hedge_side,
    positions_match,
    slippage_limit_price,
)


# ── 成交判定 ────────────────────────────────────────────

def test_no_position_change_is_no_fill():
    """昨晚那笔：报单成功、tx_hash 有、仓位纹丝不动。"""
    v = classify_fill(before=0.0, after=0.0, requested_signed=0.2)
    assert v.kind == "none"
    assert not v.should_hedge
    assert not v.is_fill


def test_full_fill():
    v = classify_fill(before=0.0, after=0.2, requested_signed=0.2)
    assert v.kind == "full" and v.should_hedge
    assert v.filled == pytest.approx(0.2)


def test_partial_fill_must_still_be_hedged():
    """这是关键取舍：小额部分成交如果被当成「没成交」而不去对冲，
    就会留下一条裸腿。所以只要动了就必须配对冲。"""
    v = classify_fill(before=0.0, after=0.003, requested_signed=0.2)
    assert v.kind == "partial"
    assert v.should_hedge is True


def test_threshold_is_tight_not_the_two_percent_pair_tolerance():
    """判定阈值是 0.1%，不是配对用的 2%。用 2% 会把 1% 的成交
    误判成没成交 —— 那正是裸腿的来源。"""
    requested = 0.2
    one_percent = requested * 0.01
    v = classify_fill(before=0.0, after=one_percent, requested_signed=requested)
    assert v.is_fill and v.should_hedge
    # 而真正小于 0.1% 的才算没成交
    tiny = requested * MEANINGFUL_FILL_FRACTION * 0.5
    assert classify_fill(before=0.0, after=tiny, requested_signed=requested).kind == "none"


def test_wrong_direction_is_not_our_fill():
    """仓位反向变化多半是别处的操作或交易所异常，不能当成我们的成交。"""
    v = classify_fill(before=0.0, after=-0.2, requested_signed=0.2)
    assert v.kind == "wrong_direction"
    assert not v.should_hedge and "相反" in v.reason


def test_short_side_fill():
    v = classify_fill(before=0.0, after=-0.2, requested_signed=-0.2)
    assert v.kind == "full" and v.should_hedge


def test_fill_on_top_of_an_existing_position():
    v = classify_fill(before=0.5, after=0.7, requested_signed=0.2)
    assert v.kind == "full"
    assert v.filled == pytest.approx(0.2)


def test_unreadable_position_is_treated_as_possibly_filled():
    """读不到仓位时必须按「可能已成交」处理去对冲。
    当成没成交就不配对冲，万一其实成了就是裸腿。"""
    v = classify_fill(before=float("nan"), after=float("nan"), requested_signed=0.2)
    assert v.should_hedge is True
    assert "裸腿" in v.reason


def test_zero_request_is_rejected():
    assert classify_fill(before=0.0, after=0.0, requested_signed=0.0).kind == "none"


# ── 滑点方向 ────────────────────────────────────────────

def test_slippage_leans_the_right_way():
    """让错方向会导致永远不成交。"""
    assert slippage_limit_price(100.0, "buy", 10.0) == pytest.approx(100.1)
    assert slippage_limit_price(100.0, "sell", 10.0) == pytest.approx(99.9)


def test_slippage_rejects_bad_price():
    with pytest.raises(ValueError):
        slippage_limit_price(0.0, "buy", 10.0)


# ── 数量对齐 ────────────────────────────────────────────

def test_align_rounds_down_and_survives_float_error():
    assert align_quantity(0.95, 2) == pytest.approx(0.95)     # 不能变成 0.94
    assert align_quantity(0.9499, 2) == pytest.approx(0.94)   # 真不足一档要向下
    assert align_quantity(2.37, 1) == pytest.approx(2.3)
    assert align_quantity(0.0, 2) == 0.0


# ── 方向翻译 ────────────────────────────────────────────

def test_hedge_sides_are_opposite():
    assert hedge_side("long_lighter_short_arcus") == ("buy", "sell")
    assert hedge_side("short_lighter_long_arcus") == ("sell", "buy")
    with pytest.raises(ValueError):
        hedge_side("nonsense")


def test_closing_sides_reverse_the_position():
    assert closing_sides(0.3, -0.3) == ("sell", "buy")
    assert closing_sides(-0.3, 0.3) == ("buy", "sell")


def test_positions_match_tolerance():
    assert positions_match(0.300, 0.302, 0.3)
    assert not positions_match(0.30, 0.20, 0.3)
    assert not positions_match(float("nan"), 0.3, 0.3)
