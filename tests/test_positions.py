"""强平价监控的用例。

锁的是对冲仓位的那条核心认知：没有方向风险，只有各自的保证金风险；
危险度取两条腿里更危险的那条，处置是双腿同平。
"""
import pytest

from parallax_hedge.arcus import arcus_liquidation_price, cross_distance
from parallax_hedge.positions import (
    HedgeHealth,
    LegPosition,
    parse_arcus_position,
    parse_lighter_position,
    with_mark,
)


def leg(venue, size, mark=100.0, liq=None, entry=100.0):
    return LegPosition(
        venue=venue, symbol="SNDK", size=size, entry_price=entry,
        mark_price=mark, liquidation_price=liq,
        unrealized_pnl=0.0, margin=10.0,
    )


def health(lighter, arcus, warn=5.0):
    return HedgeHealth(asset="SNDK", lighter=lighter, arcus=arcus, warn_distance_pct=warn)


# ── 距离强平多远 ────────────────────────────────────────

def test_distance_is_none_not_zero_when_liq_price_missing():
    """交易所无仓位时会返回 0/null 的强平价。当成「距离 0%」会让面板
    显示成命悬一线，进而触发本不该有的双腿平仓。"""
    assert leg("lighter", 1.0, liq=None).distance_pct is None
    assert leg("lighter", 1.0, liq=0.0).distance_pct is None
    assert leg("lighter", 0.0, liq=90.0).distance_pct is None      # 没持仓
    assert leg("lighter", 1.0, mark=0.0, liq=90.0).distance_pct is None


def test_distance_pct_math():
    assert leg("lighter", 1.0, mark=100.0, liq=90.0).distance_pct == pytest.approx(10.0)
    assert leg("arcus", -1.0, mark=100.0, liq=108.0).distance_pct == pytest.approx(8.0)


# ── 强平价方向自检 ──────────────────────────────────────

def test_liquidation_must_sit_on_the_losing_side():
    assert leg("lighter", 1.0, mark=100.0, liq=90.0).liquidation_side_is_sane      # 多头在下方
    assert leg("arcus", -1.0, mark=100.0, liq=110.0).liquidation_side_is_sane    # 空头在上方
    # 反了 —— 字段读错或交易所异常，绝不能据此判定"还很安全"
    assert not leg("lighter", 1.0, mark=100.0, liq=110.0).liquidation_side_is_sane
    assert not leg("arcus", -1.0, mark=100.0, liq=90.0).liquidation_side_is_sane


def test_suspect_status_wins_over_everything():
    h = health(leg("lighter", 1.0, liq=110.0), leg("arcus", -1.0, liq=110.0))
    assert h.status == "suspect"
    assert "可疑" in h.status_text


# ── 对冲是否成立 ────────────────────────────────────────

def test_hedged_requires_opposite_directions():
    assert health(leg("lighter", 1.0, liq=90.0), leg("arcus", -1.0, liq=110.0)).is_hedged
    # 同向 = 双倍敞口，不是对冲 —— 这是最危险的误判
    both_long = health(leg("lighter", 1.0, liq=90.0), leg("arcus", 1.0, liq=90.0))
    assert not both_long.is_hedged
    assert both_long.status == "unhedged"
    assert both_long.net_size == pytest.approx(2.0)


def test_hedged_requires_matching_sizes():
    assert health(leg("lighter", 1.0, liq=90.0), leg("arcus", -1.005, liq=110.0)).is_hedged
    assert not health(leg("lighter", 1.0, liq=90.0), leg("arcus", -0.5, liq=110.0)).is_hedged


def test_single_leg_is_never_hedged():
    h = health(leg("lighter", 1.0, liq=90.0), leg("arcus", 0.0))
    assert not h.is_hedged
    assert h.status == "unhedged"


# ── 危险度取更危险的那条腿 ──────────────────────────────

def test_min_distance_picks_the_riskier_leg():
    """币价暴涨时空腿危险、多腿在赚钱 —— 约束是空腿。"""
    h = health(
        leg("lighter", 1.0, mark=100.0, liq=80.0),      # 多腿：还有 20%
        leg("arcus", -1.0, mark=100.0, liq=103.0),    # 空腿：只剩 3%
    )
    assert h.min_distance_pct == pytest.approx(3.0)
    assert h.riskiest_leg.venue == "arcus"
    assert h.status == "danger"
    assert "双腿同平" in h.status_text


def test_danger_threshold_is_configurable():
    h = health(
        leg("lighter", 1.0, mark=100.0, liq=93.0),
        leg("arcus", -1.0, mark=100.0, liq=110.0),
        warn=5.0,
    )
    assert h.status == "ok"
    tighter = health(
        leg("lighter", 1.0, mark=100.0, liq=93.0),
        leg("arcus", -1.0, mark=100.0, liq=110.0),
        warn=10.0,
    )
    assert tighter.status == "danger"


def test_flat_account_is_flat_not_dangerous():
    h = health(leg("lighter", 0.0), leg("arcus", 0.0))
    assert h.status == "flat"
    assert h.min_distance_pct is None


# ── 报文解析 ────────────────────────────────────────────

LIGHTER_PAYLOAD = {"accounts": [{
    "account_index": 77,
    "positions": [
        {"symbol": "SNDK", "sign": -1, "position": "0.30",
         "avg_entry_price": "1607.32", "liquidation_price": "1750.10",
         "unrealized_pnl": "-1.25", "allocated_margin": "48.2",
         "position_value": "482.10"},
        {"symbol": "OTHER", "sign": 1, "position": "5"},
    ],
}]}

# MarketClient.arcus_account() 的输出：/v1/account 的字段 + _positions（/v1/positions 的行）
ARCUS_PAYLOAD = {"equity": "500", "freeCollateral": "400", "_positions": [
    {"marketId": 26, "marketDisplayName": "SNDK-USD", "side": "LONG", "size": "0.3",
     "averageEntryPrice": "1608.49", "markPx": "1608.0", "marginMode": "ISOLATED",
     "marginUsed": "48.3", "leverage": "10"},
    {"marketId": 9, "marketDisplayName": "OAI-USD", "side": "SHORT", "size": "-1.0",
     "averageEntryPrice": "10", "markPx": "10", "marginMode": "ISOLATED", "marginUsed": "2"},
]}
MMF = {26: 0.05, 9: 0.05}


def test_parse_lighter_uses_sign_for_direction():
    p = parse_lighter_position(LIGHTER_PAYLOAD, 77, "SNDK")
    assert p.size == pytest.approx(-0.30)          # sign=-1 → 空
    assert p.liquidation_price == pytest.approx(1750.10)
    assert p.entry_price == pytest.approx(1607.32)


def test_lighter_mark_is_derived_from_position_value():
    """标记价 = position_value / |position| —— 不依赖任何猜来的字段名。"""
    p = parse_lighter_position(LIGHTER_PAYLOAD, 77, "SNDK")
    assert p.mark_price == pytest.approx(482.10 / 0.30)
    # 无持仓时不该算出一个假的标记价
    flat = parse_lighter_position(LIGHTER_PAYLOAD, 77, "NOSUCH")
    assert flat.mark_price is None


def test_parse_lighter_wrong_account_index_returns_none():
    assert parse_lighter_position(LIGHTER_PAYLOAD, 999, "SNDK") is None


def test_parse_lighter_unknown_symbol_is_flat_not_none():
    p = parse_lighter_position(LIGHTER_PAYLOAD, 77, "NOSUCH")
    assert p is not None and not p.is_open


def test_parse_arcus_reads_size_entry_and_mark():
    p = parse_arcus_position(ARCUS_PAYLOAD, 26, mmf_by_market=MMF)
    assert p.size == pytest.approx(0.3)
    assert p.entry_price == pytest.approx(1608.49)
    assert p.mark_price == pytest.approx(1608.0)
    assert p.margin == pytest.approx(48.3)


def test_arcus_isolated_long_liquidation_is_computed_from_margin():
    """Arcus 不给强平价。逐仓多头：E(P) = M + s(P−entry)，E(P) = s·P·mmf 时强平。
    (0.3×1608.49 − 48.3) / (0.3×0.95) = 1523.67，距现价约 5.2%。"""
    p = parse_arcus_position(ARCUS_PAYLOAD, 26, mmf_by_market=MMF)
    assert p.liquidation_price == pytest.approx(1523.67, abs=0.01)
    # 在这个价上，剩余权益恰好等于维持保证金
    equity = 48.3 + 0.3 * (p.liquidation_price - 1608.49)
    assert equity == pytest.approx(0.3 * p.liquidation_price * 0.05)
    assert p.liquidation_side_is_sane


def test_arcus_isolated_short_liquidation_is_above_the_price():
    liq = arcus_liquidation_price(size=-0.3, entry_price=1600, mark_price=1600,
                                  margin_mode="ISOLATED", margin_used=48, mmf=0.05)
    assert liq == pytest.approx(528 / 0.315)
    assert 48 - 0.3 * (liq - 1600) == pytest.approx(0.3 * liq * 0.05)


def test_arcus_cross_liquidation_uses_the_whole_account():
    liq = arcus_liquidation_price(size=1.0, entry_price=90, mark_price=100,
                                  margin_mode="CROSS", margin_used=5, mmf=0.05,
                                  account_equity=20)
    # 只有这一条仓位：x = (E − N·mmf) / (N·(1+mmf)) = 15 / 105
    assert liq == pytest.approx(100 * (1 - 15 / 105))
    # 在这个价上，权益恰好等于（按空头口径放大过的）维持保证金 —— 偏保守
    # 其它仓位占着维持保证金时，强平价更近
    closer = arcus_liquidation_price(size=1.0, entry_price=90, mark_price=100,
                                     margin_mode="CROSS", margin_used=5, mmf=0.05,
                                     account_equity=20, other_maintenance=5)
    assert closer > liq


def test_arcus_liquidation_is_not_guessed_without_the_maintenance_rate():
    p = parse_arcus_position(ARCUS_PAYLOAD, 26)          # 没给 MMF
    assert p.is_open and p.liquidation_price is None and p.distance_pct is None


def test_arcus_overcollateralised_long_has_no_liquidation_price():
    assert arcus_liquidation_price(size=1, entry_price=100, mark_price=100,
                                   margin_mode="ISOLATED", margin_used=150, mmf=0.05) is None


def test_arcus_short_written_as_positive_size_with_side_short():
    payload = {"_positions": [{"marketId": 9, "side": "SHORT", "size": "1.0",
                               "averageEntryPrice": "10", "markPx": "10"}]}
    assert parse_arcus_position(payload, 9).size == pytest.approx(-1.0)


def test_arcus_unknown_market_is_flat_not_none():
    p = parse_arcus_position(ARCUS_PAYLOAD, 999, mmf_by_market=MMF)
    assert p is not None and not p.is_open
    assert parse_arcus_position(None, 26) is None


def test_parsed_pair_is_a_valid_hedge():
    lig = parse_lighter_position(LIGHTER_PAYLOAD, 77, "SNDK")   # 标记价已自带
    arc = parse_arcus_position(ARCUS_PAYLOAD, 26, mmf_by_market=MMF)
    h = health(lig, arc)
    assert h.is_hedged
    assert h.status == "ok"
    # 空腿强平价在上方、多腿在下方 —— 方向都对
    assert all(p.liquidation_side_is_sane for p in h.open_legs)
    assert h.riskiest_leg is not None


def test_an_entry_price_in_a_different_unit_is_not_used():
    """文档示例里的开仓价是整数口径 —— 真遇到时宁可没有强平价，也不能算出一个反的。"""
    payload = {"_positions": [{"marketId": 26, "size": "0.3", "averageEntryPrice": "160849000000",
                               "markPx": "1608", "marginMode": "ISOLATED", "marginUsed": "48.3"}]}
    p = parse_arcus_position(payload, 26, mmf_by_market=MMF)
    assert p.is_open and p.liquidation_price is None
    assert p.liquidation_side_is_sane


def test_arcus_cross_counts_other_positions_as_moving_against_us():
    """全仓按最坏情况：其它 Arcus 仓位也同向亏。多一笔同样大的仓位，走廊大约减半。"""
    alone = cross_distance(equity=400.00, notional=3000, mmf=0.0267)
    both = cross_distance(equity=400.00, notional=3000, mmf=0.0267,
                          other_notional=3000, other_maintenance=80.1)
    assert alone == pytest.approx((400.00 - 80.1) / (3000 * 1.0267))
    assert both < alone / 2


def test_parse_uses_the_whole_account_for_cross_positions():
    payload = {"equity": "400.00", "_positions": [
        {"marketId": 2, "size": "1", "averageEntryPrice": "3000", "markPx": "3000",
         "marginMode": "CROSS", "marginUsed": "150"}]}
    p = parse_arcus_position(payload, 2, mmf_by_market={2: 0.0267})
    assert p.distance_pct == pytest.approx((400.00 - 80.1) / (3000 * 1.0267) * 100)
