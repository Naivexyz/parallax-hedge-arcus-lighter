"""资金费归一化与方向选择的用例。

这些用例锁的是「方向选错 = 本该 +5 bps 变成 -15 bps」那个教训，
以及两个所周期不同时必须先年化再比较。
"""
import pytest

from parallax_hedge.funding import (
    SANE_RATE_PER_INTERVAL,
    FundingRow,
    annualize,
    choose_direction,
    detect_period_anomaly,
    LastGoodCache,
)

HOUR = 3600
EIGHT_HOURS = 8 * 3600


def row(venue, rate, interval_sec=HOUR, next_rate=None, symbol="SNDK"):
    return FundingRow(
        venue=venue, symbol=symbol, rate=rate,
        interval_sec=interval_sec, next_rate=next_rate,
    )


# ── 归一化 ──────────────────────────────────────────────

def test_bps_per_hour_converts_from_the_venue_interval():
    # 每小时 0.01% = 1 bps/h
    assert row("arcus", 0.0001, HOUR).bps_per_hour == pytest.approx(1.0)
    # 同样的数字，但结算周期是 8 小时 → 每小时只有 1/8
    assert row("lighter", 0.0001, EIGHT_HOURS).bps_per_hour == pytest.approx(0.125)


def test_annualize_uses_the_interval():
    # 每小时 0.01%，一年 8760 小时 → 87.6%
    assert annualize(0.0001, HOUR) == pytest.approx(0.876)
    assert annualize(0.0001, EIGHT_HOURS) == pytest.approx(0.1095)


def test_different_intervals_must_not_be_compared_raw():
    """周期不同的两个所，直接相减会差 8 倍 —— 这条用例锁住这个坑。"""
    lighter = row("lighter", 0.0008, EIGHT_HOURS)   # 0.08%/8h → 1.0 bps/h
    arcus = row("arcus", 0.0002, HOUR)          # 0.02%/1h → 2.0 bps/h
    # 天真做法：直接比原始数字，lighter(0.0008) > arcus(0.0002)，会选反方向
    assert (lighter.rate or 0) > (arcus.rate or 0)
    choice = choose_direction(lighter, arcus)
    # 归一化到每小时后 arcus 才是高的那边，应当收 arcus、付 lighter
    assert choice.direction == "long_lighter_short_arcus"
    assert choice.net_bps_per_hour == pytest.approx(1.0)


# ── 方向选择 ────────────────────────────────────────────

def test_picks_the_side_that_collects():
    choice = choose_direction(row("lighter", 0.0001), row("arcus", 0.0003))
    assert choice.direction == "long_lighter_short_arcus"
    assert choice.net_bps_per_hour == pytest.approx(2.0)

    choice = choose_direction(row("lighter", 0.0003), row("arcus", 0.0001))
    assert choice.direction == "short_lighter_long_arcus"
    assert choice.net_bps_per_hour == pytest.approx(2.0)


def test_net_funding_is_never_negative_in_the_chosen_direction():
    """两个方向恰好互为相反数，所以差值总能在某一边收到。"""
    for lr, er in [(0.0001, 0.0003), (0.0003, 0.0001), (-0.0002, 0.0001), (0.0, 0.0)]:
        choice = choose_direction(row("lighter", lr), row("arcus", er))
        assert choice.net_bps_per_hour >= 0


def test_predicted_rate_wins_over_current():
    r = row("arcus", 0.0001, next_rate=0.0005)
    assert r.is_predicted
    assert r.bps_per_hour == pytest.approx(5.0)


def test_net_over_hold_scales_with_hours():
    choice = choose_direction(row("lighter", 0.0), row("arcus", 0.0002))
    assert choice.net_bps_per_hour == pytest.approx(2.0)
    assert choice.net_bps_over(4) == pytest.approx(8.0)


# ── 合理性闸门 ──────────────────────────────────────────

def test_absurd_rate_is_flagged_and_blocks_trading():
    bad = row("lighter", SANE_RATE_PER_INTERVAL * 2)
    assert bad.suspect
    assert "单位" in bad.suspect_reason
    choice = choose_direction(bad, row("arcus", 0.0001))
    assert choice.tradable is False
    assert choice.reason


def test_normal_rate_is_tradable():
    choice = choose_direction(row("lighter", 0.0001), row("arcus", 0.0002))
    assert choice.tradable is True
    assert choice.reason is None


def test_missing_side_yields_no_choice():
    assert choose_direction(None, row("arcus", 0.0001)) is None
    assert choose_direction(row("lighter", 0.0001), None) is None
    assert choose_direction(row("lighter", None), row("arcus", 0.0001)) is None


# ── 陈旧数据 ────────────────────────────────────────────

def test_last_good_cache_returns_age_and_expires():
    cache = LastGoodCache()
    cache.put("lighter", [row("lighter", 0.0001)])
    got = cache.get("lighter", stale_max_seconds=180)
    assert got is not None and got[1] < 1.0
    assert cache.get("lighter", stale_max_seconds=0) is None       # 0 = 不沿用
    assert cache.get("arcus", stale_max_seconds=180) is None


# ── 2026-09-18 实盘数据回归 ─────────────────────────────
#
# 用真实抓到的 /api/v1/funding-rates 报文锁住这次的教训：
# Lighter 是 8 小时周期，当成 1 小时会差 8 倍并把方向判反。

LIGHTER_8H = 8 * 3600
LIGHTER_FLOOR = 0.000032          # 56 个市场里 35 个卡在这个地板价上


def test_lighter_floor_rate_is_eight_hourly():
    """0.000032 按 8h 解读是 3.5% 年化（正常基准利率）；
    按 1h 解读是 28% 年化保底 —— 经济上不可能。"""
    correct = row("lighter", LIGHTER_FLOOR, LIGHTER_8H)
    assert correct.bps_per_hour == pytest.approx(0.04)
    assert abs(annualize(LIGHTER_FLOOR, LIGHTER_8H) * 100) == pytest.approx(3.504)

    wrong = row("lighter", LIGHTER_FLOOR, HOUR)
    assert wrong.bps_per_hour == pytest.approx(0.32)
    assert abs(annualize(LIGHTER_FLOOR, HOUR) * 100) == pytest.approx(28.032)


def test_anth_direction_was_flipped_by_the_wrong_period():
    """ANTH：Lighter 地板价 0.000032，Arcus +0.137 bps/h。
    周期用错时 Lighter 看着是 0.32 > 0.137 → 判成做空 Lighter（错）；
    用对时 Lighter 只有 0.04 < 0.137 → 应当做多 Lighter。"""
    arcus = FundingRow(
        venue="arcus", symbol="ANTHROPIC",
        rate=0.0000137, interval_sec=HOUR,
    )
    wrong = choose_direction(row("lighter", LIGHTER_FLOOR, HOUR), arcus)
    assert wrong.direction == "short_lighter_long_arcus"      # 当时面板显示的（错的）

    right = choose_direction(row("lighter", LIGHTER_FLOOR, LIGHTER_8H), arcus)
    assert right.direction == "long_lighter_short_arcus"      # 修正后
    assert right.net_bps_per_hour == pytest.approx(0.097)


def test_period_anomaly_fires_on_the_wrong_assumption():
    """地板价自检：35/56 卡在同一个值、隐含 28% 年化 → 必须报警。"""
    wrong_rows = [row("lighter", LIGHTER_FLOOR, HOUR, symbol=f"M{i}") for i in range(35)]
    wrong_rows += [row("lighter", 0.000096, HOUR, symbol=f"N{i}") for i in range(8)]
    warning = detect_period_anomaly(wrong_rows)
    assert warning is not None
    assert "周期" in warning


def test_period_anomaly_silent_on_the_right_assumption():
    right_rows = [row("lighter", LIGHTER_FLOOR, LIGHTER_8H, symbol=f"M{i}") for i in range(35)]
    right_rows += [row("lighter", 0.000096, LIGHTER_8H, symbol=f"N{i}") for i in range(8)]
    assert detect_period_anomaly(right_rows) is None


def test_period_anomaly_needs_a_real_floor():
    """没有明显地板价（各不相同）时不应误报。"""
    scattered = [row("lighter", 0.00001 * (i + 1), HOUR, symbol=f"M{i}") for i in range(20)]
    assert detect_period_anomaly(scattered) is None
