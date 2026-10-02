"""盘口与可成交价的用例。

锁住 2026-09-18 实盘第一课：两条腿必须各用各所的盘口价，
共用一个标记价会导致「报单成功、零成交」。
"""
import pytest

from parallax_hedge.books import (
    Book, Level, aggregate_levels, basis_bps, executable_prices, vwap,
)


def book(venue, bids, asks):
    return Book(venue=venue, symbol="OAI",
                bids=[Level(p, s) for p, s in bids],
                asks=[Level(p, s) for p, s in asks])


# ── 档位归并 ────────────────────────────────────────────

def test_aggregate_handles_both_venues_field_names():
    lighter = aggregate_levels(
        [{"price": "100.5", "remaining_base_amount": "2"},
         {"price": "100.5", "remaining_base_amount": "3"},   # 同价位要合并
         {"price": "100.6", "remaining_base_amount": "1"}],
        descending=False)
    assert [(l.price, l.size) for l in lighter] == [(100.5, 5.0), (100.6, 1.0)]

    hyper = aggregate_levels(
        [{"px": "99.9", "sz": "4"}, {"px": "99.8", "sz": "2"}], descending=True)
    assert [(l.price, l.size) for l in hyper] == [(99.9, 4.0), (99.8, 2.0)]


def test_aggregate_skips_junk():
    assert aggregate_levels([{"price": "0", "size": "5"},
                             {"price": "1", "size": "0"},
                             "not a dict", None], descending=False) == []
    assert aggregate_levels(None, descending=False) == []


# ── VWAP ────────────────────────────────────────────────

def test_vwap_walks_the_book():
    levels = [Level(100.0, 1.0), Level(101.0, 2.0)]
    assert vwap(levels, 1.0) == pytest.approx(100.0)
    assert vwap(levels, 3.0) == pytest.approx((100 + 202) / 3)


def test_vwap_returns_none_when_depth_is_short():
    """深度不够必须返回 None，不能退回第一档 ——
    退回第一档会让程序以为能成交，实际只成一部分甚至完全不成。"""
    assert vwap([Level(100.0, 1.0)], 5.0) is None
    assert vwap([], 1.0) is None
    assert vwap([Level(100.0, 1.0)], 0.0) is None


# ── 每条腿各用各所的价 ──────────────────────────────────

def test_each_leg_uses_its_own_book():
    lighter = book("lighter", [(1742.4, 5)], [(1742.8, 5)])
    arcus = book("arcus", [(1741.4, 5)], [(1741.6, 5)])
    lp, ep = executable_prices(lighter, arcus,
                               lighter_side="buy", arcus_side="sell", quantity=1.0)
    assert lp == pytest.approx(1742.8)      # Lighter 的卖一
    assert ep == pytest.approx(1741.4)      # Arcus 的买一


def test_reversed_direction_flips_both_sides():
    lighter = book("lighter", [(1742.4, 5)], [(1742.8, 5)])
    arcus = book("arcus", [(1741.4, 5)], [(1741.6, 5)])
    lp, ep = executable_prices(lighter, arcus,
                               lighter_side="sell", arcus_side="buy", quantity=1.0)
    assert lp == pytest.approx(1742.4)
    assert ep == pytest.approx(1741.6)


def test_shared_mark_price_breaks_once_the_basis_eats_the_slippage_budget():
    """共用一个标记价时，能不能成交取决于「基差 + 对手半价差」有没有
    超过滑点预算 —— 这是个阈值问题，不是必然失败。

    2026-09-18 我一度断定 4/4 不成交是这个原因，但按 SNDK 的实际数字
    （基差 5.4 bps、半价差 2.3 bps、滑点 10 bps）算下来是能成的。
    所以这条用例锁的是【阈值在哪】，而不是「旧做法一定失败」。
    """
    SLIPPAGE = 10.0

    def fills(basis_bps_value: float, half_spread_bps: float) -> bool:
        arcus_mid = 1000.0
        lighter_mid = arcus_mid * (1 + basis_bps_value / 10_000)
        lighter_ask = lighter_mid * (1 + half_spread_bps / 10_000)
        return arcus_mid * (1 + SLIPPAGE / 10_000) >= lighter_ask

    assert fills(5.4, 2.3)          # SNDK 的实际数字：能成
    assert fills(7.6, 2.3)          # 刚好卡在预算边缘
    assert not fills(8.0, 2.3)      # 越过就成不了
    assert not fills(20.0, 2.3)     # 基差一宽就必然失败

    # 而用各所自己的盘口价，不管基差多宽都够得到
    lighter = book("lighter", [(1742.4, 5)], [(1742.8, 5)])
    arcus = book("arcus", [(1741.4, 5)], [(1741.6, 5)])
    lp, _ = executable_prices(lighter, arcus,
                              lighter_side="buy", arcus_side="sell", quantity=1.0)
    assert lp >= lighter.best_ask


def test_insufficient_depth_yields_none_not_a_bad_price():
    lighter = book("lighter", [(1742.4, 0.1)], [(1742.8, 0.1)])
    arcus = book("arcus", [(1741.4, 5)], [(1741.6, 5)])
    lp, ep = executable_prices(lighter, arcus,
                               lighter_side="buy", arcus_side="sell", quantity=1.0)
    assert lp is None and ep is not None


# ── 基差诊断 ────────────────────────────────────────────

def test_basis_bps_measures_the_gap_that_broke_us():
    """SNDK 实测：Lighter 1742.44 / Arcus 1741.50 —— 约 5.4 bps。"""
    lighter = book("lighter", [(1742.42, 5)], [(1742.46, 5)])
    arcus = book("arcus", [(1741.48, 5)], [(1741.52, 5)])
    assert basis_bps(lighter, arcus) == pytest.approx(5.4, abs=0.3)


def test_basis_none_on_empty_book():
    assert basis_bps(book("lighter", [], []), book("arcus", [(1, 1)], [(2, 1)])) is None
