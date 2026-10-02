"""风控决策层的用例。

锁三道防线：开仓前预检、开仓后用真实强平价复核、孤腿立即平。
另外锁住「两条腿各挂一对止盈止损」的价位计算 —— 只挂单边止损
是对冲策略最经典的死法。
"""
import pytest

from parallax_hedge.positions import HedgeHealth, LegPosition
from parallax_hedge.risk import (
    DEFAULT_MIN_CORRIDOR_PCT,
    estimate_corridor_pct,
    evaluate,
    max_quantity_for,
    pre_open_check,
    protective_levels,
    verify_corridor_after_open,
)


def leg(venue, size, mark, liq, entry=None):
    return LegPosition(
        venue=venue, symbol="SNDK", size=size, entry_price=entry or mark,
        mark_price=mark, liquidation_price=liq, unrealized_pnl=0.0, margin=10.0,
    )


def health(lighter=None, arcus=None, warn=5.0):
    return HedgeHealth(asset="SNDK", lighter=lighter, arcus=arcus, warn_distance_pct=warn)


# ── 走廊公式：用实测仓位校准 ────────────────────────────

def test_corridor_formula_matches_the_real_sndk_position():
    """2026-09-18 实测：SNDK 最大 10×、满杠杆，交易所给的强平距离 5.06%。
    公式 1/10 − 1/20 = 5.0%，吻合。"""
    assert estimate_corridor_pct(10, 10) == pytest.approx(5.0)
    # 最大杠杆更低的币，满杠杆反而走廊更宽
    assert estimate_corridor_pct(6, 6) == pytest.approx(100 * (1 / 6 - 1 / 12))
    assert estimate_corridor_pct(6, 6) == pytest.approx(8.333, rel=1e-3)
    # 减杠杆 → 走廊变宽
    assert estimate_corridor_pct(5, 10) == pytest.approx(15.0)


def test_corridor_is_zero_or_none_on_nonsense():
    assert estimate_corridor_pct(0, 10) is None
    assert estimate_corridor_pct(10, 0) is None
    assert estimate_corridor_pct(100, 10) == 0.0      # 杠杆远超上限 → 没有走廊


# ── 开仓前预检 ──────────────────────────────────────────

def ok_args(**over):
    args = dict(leverage=10, max_leverage=10, quantity=2.3, price=1736.0,
                min_quantity=0.01, lighter_available=500.0, arcus_available=500.0)
    args.update(over)
    return args


def test_pre_open_passes_max_leverage_on_all_three_assets():
    """默认 4% 下限不应误伤三个共有币的满杠杆开仓。"""
    assert pre_open_check(**ok_args(leverage=10, max_leverage=10, quantity=0.1)).ok
    assert pre_open_check(**ok_args(leverage=6, max_leverage=6, quantity=0.1)).ok


def test_pre_open_rejects_a_corridor_that_is_too_narrow():
    d = pre_open_check(**ok_args(leverage=20, max_leverage=20, quantity=0.1))
    assert not d.ok
    assert "走廊" in d.reason and "开仓门槛" in d.reason
    assert d.estimated_corridor_pct == pytest.approx(2.5)


def test_cross_margin_corridor_follows_account_equity_not_leverage():
    """示例：Arcus 权益 400.00，ETH（MMF 2.67%）20× 开 3000 美元。
    逐仓估只有 2.33%；全仓按账户权益估约 10%，能开。"""
    args = ok_args(leverage=20, max_leverage=20, quantity=1.0, price=3000.0)
    args.update(lighter_available=1000.0, arcus_available=400.00,
                maintenance_fraction=0.0267, min_corridor_pct=3.0)
    isolated = pre_open_check(**args)
    assert not isolated.ok and isolated.estimated_corridor_pct == pytest.approx(2.33)
    cross = pre_open_check(**args, arcus_equity=400.00)
    assert cross.ok
    expected = (400.00 - 1.5 - 3000 * 0.0267) / (3000 * 1.0267) * 100
    assert cross.estimated_corridor_pct == pytest.approx(expected)
    assert 9.5 < cross.estimated_corridor_pct < 10.5


def test_other_arcus_positions_narrow_the_cross_corridor():
    """其它 Arcus 仓位按最坏情况算同向亏损 —— 同样的钱，走廊更窄。"""
    args = ok_args(leverage=20, max_leverage=20, quantity=1.0, price=3000.0)
    args.update(lighter_available=1000.0, arcus_available=400.00,
                maintenance_fraction=0.0267, min_corridor_pct=3.0, arcus_equity=400.00)
    alone = pre_open_check(**args).estimated_corridor_pct
    crowded = pre_open_check(**args, arcus_other_notional=3000.0,
                             arcus_other_maintenance=80.0).estimated_corridor_pct
    assert crowded < alone / 2


def test_pre_open_rejects_leverage_above_market_cap():
    d = pre_open_check(**ok_args(leverage=12, max_leverage=10))
    assert not d.ok and "上限" in d.reason


def test_pre_open_rejects_below_min_quantity():
    d = pre_open_check(**ok_args(quantity=0.001))
    assert not d.ok and "最小下单量" in d.reason


def test_pre_open_binds_on_the_smaller_account():
    """仓位由资金小的那边约束。"""
    d = pre_open_check(**ok_args(lighter_available=10.0, arcus_available=5000.0))
    assert not d.ok
    assert "Lighter" in d.reason and "保证金" in d.reason


def test_min_corridor_is_configurable():
    assert pre_open_check(**ok_args(leverage=10, max_leverage=10, quantity=0.1),
                          min_corridor_pct=6.0).ok is False


# ── 最大数量 ────────────────────────────────────────────

def test_max_quantity_uses_the_smaller_side_and_rounds_down():
    q = max_quantity_for(leverage=10, price=1000.0, lighter_available=100.0,
                         arcus_available=500.0, quantity_decimals=2)
    assert q == pytest.approx(0.95)          # 100×10×0.95/1000 = 0.95
    # 精度截断必须向下，不能向上 —— 向上会被交易所拒单
    q2 = max_quantity_for(leverage=10, price=1000.0, lighter_available=100.0,
                          arcus_available=500.0, quantity_decimals=1)
    assert q2 == pytest.approx(0.9)


def test_max_quantity_is_zero_when_no_margin():
    assert max_quantity_for(leverage=10, price=100.0, lighter_available=0.0,
                            arcus_available=500.0, quantity_decimals=2) == 0.0


# ── 开仓后用真实强平价复核 ──────────────────────────────

def test_after_open_accepts_a_wide_enough_corridor():
    h = health(leg("lighter", 2.3, 1730.71, 1639.199),
               leg("arcus", -2.3, 1730.60, 1818.2373))
    check = verify_corridor_after_open(h)
    assert check.ok
    assert check.actual_corridor_pct == pytest.approx(5.06, rel=1e-2)


def test_after_open_rejects_when_reality_is_worse_than_the_estimate():
    """公式说 5%，交易所实际只给 3% —— 立刻平掉，别扛着过夜。"""
    h = health(leg("lighter", 2.3, 1000.0, 900.0),
               leg("arcus", -2.3, 1000.0, 1030.0))     # 空腿只有 3%
    check = verify_corridor_after_open(h, 4.0)
    assert not check.ok
    assert "实际走廊" in check.reason and "arcus" in check.reason


def test_after_open_rejects_insane_liquidation_price():
    h = health(leg("lighter", 2.3, 1000.0, 1100.0),      # 多头强平价在上方 = 异常
               leg("arcus", -2.3, 1000.0, 1100.0))
    assert not verify_corridor_after_open(h).ok


# ── 保护性挂单的价位 ────────────────────────────────────

def test_protective_levels_bracket_the_position():
    h = health(leg("lighter", 2.3, 1000.0, 900.0),        # 多腿，下方 100
               leg("arcus", -2.3, 1000.0, 1100.0))      # 空腿，上方 100
    lv = protective_levels(h, backstop_fraction=0.8)
    assert lv.upper == pytest.approx(1080.0)             # 往上走完 80%
    assert lv.lower == pytest.approx(920.0)
    assert lv.long_venue == "lighter" and lv.short_venue == "arcus"


def test_each_leg_gets_both_a_stop_and_a_take_profit():
    """任一边界都要同时清掉两条腿 —— 这是不留裸腿的关键。"""
    long_leg = leg("lighter", 2.3, 1000.0, 900.0)
    short_leg = leg("arcus", -2.3, 1000.0, 1100.0)
    lv = protective_levels(health(long_leg, short_leg), backstop_fraction=0.8)

    long_orders = lv.for_leg(long_leg)
    short_orders = lv.for_leg(short_leg)
    # 涨到上边界：空腿止损 + 多腿止盈，两条腿一起走
    assert short_orders["stop_loss"] == pytest.approx(lv.upper)
    assert long_orders["take_profit"] == pytest.approx(lv.upper)
    # 跌到下边界：多腿止损 + 空腿止盈
    assert long_orders["stop_loss"] == pytest.approx(lv.lower)
    assert short_orders["take_profit"] == pytest.approx(lv.lower)


def test_no_protective_levels_without_a_real_hedge():
    assert protective_levels(health(leg("lighter", 2.3, 1000.0, 900.0), None)) is None
    both_long = health(leg("lighter", 2.3, 1000.0, 900.0),
                       leg("arcus", 2.3, 1000.0, 900.0))
    assert protective_levels(both_long) is None


# ── 持仓期间的裁决 ──────────────────────────────────────

def test_orphan_leg_is_flattened_immediately_and_urgently():
    """兜底单在程序离线时打掉了一条腿 —— 剩下的正在裸奔。"""
    h = health(leg("lighter", 2.3, 1000.0, 900.0), leg("arcus", 0.0, 1000.0, None))
    v = evaluate(h, corridor_at_open_pct=10.0)
    assert v.action == "flatten_orphan"
    assert v.urgent and "裸露" in v.reason


def test_same_direction_legs_are_closed_not_treated_as_hedged():
    h = health(leg("lighter", 2.3, 1000.0, 900.0), leg("arcus", 2.3, 1000.0, 900.0))
    v = evaluate(h, corridor_at_open_pct=10.0)
    assert v.action == "close_both" and v.urgent


def test_relative_trigger_closes_both_at_half_the_opening_corridor():
    h = health(leg("lighter", 2.3, 1000.0, 940.0),        # 6%
               leg("arcus", -2.3, 1000.0, 1049.0))      # 4.9% ← 更危险
    v = evaluate(h, corridor_at_open_pct=10.0)            # 一半 = 5%
    assert v.action == "close_both"
    assert "arcus" in v.reason and "双腿同平" in v.reason


def test_healthy_position_is_left_alone():
    h = health(leg("lighter", 2.3, 1000.0, 900.0), leg("arcus", -2.3, 1000.0, 1100.0))
    assert evaluate(h, corridor_at_open_pct=10.0).action == "none"


def test_absolute_floor_fires_even_with_a_narrow_opening_corridor():
    """开仓时走廊本来就窄的话，相对触发可能永远不响 —— 绝对下限兜住。"""
    h = health(leg("lighter", 2.3, 1000.0, 986.0),        # 1.4%，低于平仓线 1.5%
               leg("arcus", -2.3, 1000.0, 1100.0))
    v = evaluate(h, corridor_at_open_pct=2.6)             # 一半 = 1.3%，相对触发不响
    assert v.action == "close_both" and v.urgent
    assert "平仓线" in v.reason


def test_flat_account_needs_no_action():
    assert evaluate(health(), corridor_at_open_pct=10.0).action == "none"


def test_missing_liquidation_price_does_not_trigger_a_close():
    """拿不到强平价时必须按兵不动，不能当成「距离 0」而误平。"""
    h = health(leg("lighter", 2.3, 1000.0, None), leg("arcus", -2.3, 1000.0, None))
    v = evaluate(h, corridor_at_open_pct=10.0)
    assert v.action == "none"


def test_quantity_rounding_survives_float_error():
    """0.95 / 0.01 在浮点下是 94.9999…，天真的 floor 会少开一档。"""
    q = max_quantity_for(leverage=10, price=1000.0, lighter_available=100.0,
                         arcus_available=500.0, quantity_decimals=2)
    assert q == pytest.approx(0.95)
    # 但真正不足一档的仍要向下走，不能靠 epsilon 蒙混过关
    q2 = max_quantity_for(leverage=10, price=1000.0, lighter_available=99.9,
                          arcus_available=500.0, quantity_decimals=2)
    assert q2 == pytest.approx(0.94)


# ── 两边各自的杠杆（2026-09-19）──────────────────────────
from parallax_hedge.risk import effective_leverages


def test_effective_leverage_is_the_task_value_capped_per_venue():
    assert effective_leverages(6, arcus_max=10, lighter_max=20) == (6, 6)
    assert effective_leverages(6, arcus_max=6, lighter_max=3) == (3, 6)
    assert effective_leverages(20, arcus_max=6, lighter_max=None) == (20, 6)   # 上限不明：按任务值
    assert effective_leverages(6.9, arcus_max=10, lighter_max=10) == (6, 6)    # Hyperliquid 只收整数
    assert effective_leverages(0.5, arcus_max=10, lighter_max=10) == (1, 1)


def test_max_quantity_uses_each_venues_own_leverage():
    q = max_quantity_for(leverage=6, price=100.0, lighter_available=100.0,
                         arcus_available=100.0, quantity_decimals=2,
                         lighter_leverage=3, arcus_leverage=6)
    assert q == pytest.approx(2.85)          # Lighter 100×3×0.95/100，小的那边说了算
    assert max_quantity_for(leverage=6, price=100.0, lighter_available=100.0,
                            arcus_available=100.0, quantity_decimals=2) == pytest.approx(5.7)


def test_pre_open_margin_is_checked_per_venue_and_corridor_uses_arcus():
    d = pre_open_check(leverage=6, max_leverage=10, quantity=5.0, price=100.0,
                       min_quantity=0.01, lighter_available=150.0, arcus_available=150.0,
                       lighter_leverage=3, arcus_leverage=6)
    assert not d.ok and "Lighter" in d.reason                 # 500/3 = 166.7 > 150
    ok = pre_open_check(leverage=6, max_leverage=10, quantity=5.0, price=100.0,
                        min_quantity=0.01, lighter_available=170.0, arcus_available=90.0,
                        lighter_leverage=3, arcus_leverage=6)
    assert ok.ok
    assert ok.estimated_corridor_pct == pytest.approx((1 / 6 - 1 / 20) * 100)


# ── v0.2.2：开仓记录写明数量是被什么卡住的 ─────────────────────

def test_sizing_limit_names_what_capped_the_quantity():
    from parallax_hedge.risk import sizing_limit
    kw = dict(price=1779.3, lighter_leverage=10, arcus_leverage=10)
    assert sizing_limit(lighter_available=243.5, arcus_available=216.38,
                        notional_cap=1000, **kw) == "按名义上限 1000 USDC"
    # 2026-09-20 SNDK 那一单的真实情形（旧公式算出的 Arcus 可用）
    assert sizing_limit(lighter_available=243.5, arcus_available=16.70,
                        notional_cap=1000, **kw) == "受 Arcus 可用 16.70 USDC 限制"
    assert sizing_limit(lighter_available=50, arcus_available=216.38,
                        notional_cap=1000, **kw) == "受 Lighter 可用 50.00 USDC 限制"
    assert sizing_limit(lighter_available=243.5, arcus_available=216.38,
                        notional_cap=None, **kw) == "受 Arcus 可用 216.38 USDC 限制"


def test_sizing_limit_compares_available_times_each_venues_leverage():
    """比的是「可用 × 各自杠杆」：Arcus 余额多，但杠杆低，照样是它卡。"""
    from parallax_hedge.risk import sizing_limit
    assert sizing_limit(price=100, lighter_available=100, arcus_available=150,
                        lighter_leverage=10, arcus_leverage=3,
                        notional_cap=None) == "受 Arcus 可用 150.00 USDC 限制"
    # 名义上限只有在 5% 余量之内也够得着时才算「按名义上限」
    assert sizing_limit(price=100, lighter_available=100, arcus_available=100,
                        lighter_leverage=10, arcus_leverage=10,
                        notional_cap=960) == "受 Lighter 可用 100.00 USDC 限制"
    assert sizing_limit(price=100, lighter_available=100, arcus_available=100,
                        lighter_leverage=10, arcus_leverage=10,
                        notional_cap=950) == "按名义上限 950 USDC"
    assert sizing_limit(price=0, lighter_available=1, arcus_available=1,
                        lighter_leverage=1, arcus_leverage=1, notional_cap=None) is None
