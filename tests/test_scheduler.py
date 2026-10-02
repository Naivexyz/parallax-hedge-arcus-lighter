"""轮换调度决策的用例。

核心是优先级：安全动作永远压过交易动作，
而孤腿抢救连「任务被停用」都压得过 —— 停用不代表裸腿可以放着不管。
"""
import pytest

from parallax_hedge.funding import DirectionChoice
from parallax_hedge.positions import HedgeHealth, LegPosition
from parallax_hedge.risk import PreOpenDecision, RiskVerdict
from parallax_hedge.scheduler import TaskState, decide

HOUR = 3600.0
NOW = 1_800_000_000.0


def leg(venue, size, mark=1000.0, liq=None):
    return LegPosition(venue=venue, symbol="SNDK", size=size, entry_price=mark,
                       mark_price=mark, liquidation_price=liq,
                       unrealized_pnl=0.0, margin=10.0)


def health(lighter=None, arcus=None):
    return HedgeHealth(asset="SNDK", lighter=lighter, arcus=arcus, warn_distance_pct=5.0)


HEDGED = health(leg("lighter", 0.2, liq=900.0), leg("arcus", -0.2, liq=1100.0))
FLAT = health(leg("lighter", 0.0), leg("arcus", 0.0))
ORPHAN = health(leg("lighter", 0.2, liq=900.0), leg("arcus", 0.0))

GOOD_CHOICE = DirectionChoice(
    symbol="SNDK", direction="long_lighter_short_arcus", net_bps_per_hour=0.384,
    lighter_bps_per_hour=0.04, arcus_bps_per_hour=0.424, tradable=True,
)
BAD_CHOICE = DirectionChoice(
    symbol="SNDK", direction="long_lighter_short_arcus", net_bps_per_hour=0.384,
    lighter_bps_per_hour=0.04, arcus_bps_per_hour=0.424, tradable=False,
    reason="费率存疑",
)
PASS = PreOpenDecision(True, None, 5.0, 10.0, 0.2)
FAIL = PreOpenDecision(False, "走廊太窄", 2.0, 20.0, 0.2)
NO_RISK = RiskVerdict("none", None)


def call(task, health_, risk=NO_RISK, choice=GOOD_CHOICE, pre_open=PASS, qty=0.2, now=NOW):
    return decide(task=task, health=health_, risk=risk, choice=choice,
                  pre_open=pre_open, quantity=qty, now=now)


def task(**kw):
    args = dict(asset="SNDK", enabled=True, rotation_hours=4.0, leverage=10.0)
    args.update(kw)
    return TaskState(**args)


# ── 优先级 ──────────────────────────────────────────────

def test_orphan_beats_everything_including_a_disabled_task():
    """任务被停用不代表裸腿可以放着不管。"""
    risk = RiskVerdict("flatten_orphan", "只剩 lighter 一条腿", urgent=True)
    d = call(task(enabled=False), ORPHAN, risk=risk)
    assert d.plan == "flatten_orphan" and d.urgent
    assert d.orphan_venue == "lighter" and d.orphan_size == pytest.approx(0.2)


def test_orphan_beats_a_position_that_is_not_yet_due():
    risk = RiskVerdict("flatten_orphan", "孤腿", urgent=True)
    d = call(task(opened_at=NOW - HOUR), ORPHAN, risk=risk)
    assert d.plan == "flatten_orphan"


def test_risk_close_beats_the_rotation_timer():
    risk = RiskVerdict("close_both", "逼近强平", urgent=True)
    d = call(task(opened_at=NOW - HOUR), HEDGED, risk=risk)      # 才持仓 1 小时
    assert d.plan == "close" and d.urgent and "强平" in d.reason


# ── 正常轮换 ────────────────────────────────────────────

def test_holds_until_the_rotation_period_elapses():
    d = call(task(opened_at=NOW - 2 * HOUR), HEDGED)
    assert d.plan == "idle" and "距轮换还有 2.00 小时" in d.reason


def test_closes_when_the_rotation_period_is_reached():
    d = call(task(opened_at=NOW - 4 * HOUR), HEDGED)
    assert d.plan == "close" and "轮换周期" in d.reason


def test_opens_when_flat_and_everything_checks_out():
    d = call(task(), FLAT)
    assert d.plan == "open"
    assert d.direction == "long_lighter_short_arcus"
    assert d.quantity == pytest.approx(0.2)
    assert "净资金费" in d.reason


# ── 开仓闸门 ────────────────────────────────────────────

def test_suspect_funding_blocks_opening():
    d = call(task(), FLAT, choice=BAD_CHOICE)
    assert d.plan == "blocked" and "存疑" in d.reason


def test_failed_pre_open_check_blocks_opening():
    d = call(task(), FLAT, pre_open=FAIL)
    assert d.plan == "blocked" and "走廊太窄" in d.reason


def test_missing_funding_blocks_opening():
    d = call(task(), FLAT, choice=None)
    assert d.plan == "blocked"


def test_zero_quantity_blocks_opening():
    d = call(task(), FLAT, qty=0.0)
    assert d.plan == "blocked" and "保证金不足" in d.reason


def test_cooldown_prevents_immediate_reopen():
    """刚平完就重开会连续付两次往返成本。"""
    d = call(task(last_closed_at=NOW - 5), FLAT)
    assert d.plan == "idle" and "冷却" in d.reason
    d2 = call(task(last_closed_at=NOW - 60), FLAT)
    assert d2.plan == "open"


# ── 停用与接管 ──────────────────────────────────────────

def test_disabled_task_closes_an_existing_position():
    d = call(task(enabled=False, opened_at=NOW - HOUR), HEDGED)
    assert d.plan == "close" and "停用" in d.reason


def test_disabled_and_flat_is_idle():
    assert call(task(enabled=False), FLAT).plan == "idle"


def test_adopting_a_position_with_unknown_open_time_does_not_close_it():
    """程序重启后接管已有仓位：不猜开仓时间，也不能立刻平掉。"""
    d = call(task(opened_at=None), HEDGED)
    assert d.plan == "idle" and "接管" in d.reason


# ═══════════════════════════════════════════════════════════
# 随机轮换：设 1~2 小时，程序在区间里的随机时刻开关仓
# ═══════════════════════════════════════════════════════════
import random as _random

from parallax_hedge.scheduler import draw_hold_hours


def _task(**kw):
    base = dict(asset="OAI", enabled=True, rotation_hours=1.0, leverage=6.0)
    base.update(kw)
    return TaskState(**base)


def test_draws_stay_inside_the_range():
    rng = _random.Random(7)
    draws = [draw_hold_hours(1.0, 2.0, rng) for _ in range(2000)]
    assert min(draws) >= 1.0 and max(draws) <= 2.0
    assert 1.45 < sum(draws) / len(draws) < 1.55            # 均匀分布，均值在中间


def test_no_upper_bound_means_a_fixed_period():
    assert draw_hold_hours(4.0, None) == 4.0
    assert draw_hold_hours(4.0, 4.0) == 4.0


def test_a_reversed_range_is_tolerated():
    rng = _random.Random(1)
    assert 1.0 <= draw_hold_hours(2.0, 1.0, rng) <= 2.0


def test_the_draw_must_happen_once_per_position_not_once_per_cycle():
    """为什么抽签结果要落库、不能每一轮现抽：

    每 20 秒一轮，如果每一轮都重新抽一个时长、抽到比已持有时间短就平，
    那么每一轮都多一次「提前平掉」的机会 —— 1~2 小时的设置，实际会在
    1 小时刚过就平掉，上半段区间几乎永远用不到。
    """
    rng = _random.Random(3)
    step = 20 / 3600

    def per_cycle_redraw() -> float:
        held = 0.0
        while True:
            held += step
            if held >= rng.uniform(1.0, 2.0):
                return held

    biased = sum(per_cycle_redraw() for _ in range(300)) / 300
    fair = sum(draw_hold_hours(1.0, 2.0, rng) for _ in range(300)) / 300
    assert biased < 1.15           # 每轮现抽：几乎全挤在下限附近
    assert 1.4 < fair < 1.6        # 开仓时抽一次：真正落在区间中间


def test_due_time_follows_this_rounds_draw_not_the_minimum():
    now = 100_000.0
    t = _task(rotation_hours=1.0, rotation_hours_max=2.0, hold_hours=1.7,
              opened_at=now - 1.5 * 3600)
    assert not t.is_due_to_close(now)                       # 过了下限，但没到本轮抽到的 1.7
    assert t.is_due_to_close(now + 0.21 * 3600)


def test_changing_the_range_takes_effect_on_the_open_position():
    """持仓期间把 1~3 小时改成固定 0.5 小时：已经持有 1 小时的仓位这一轮就要平，
    而不是继续按旧的抽签结果（2.6 小时）等下去。"""
    now = 100_000.0
    t = _task(rotation_hours=0.5, rotation_hours_max=None, hold_hours=2.6,
              opened_at=now - 1.0 * 3600)
    assert t.effective_hold_hours() == 0.5
    assert t.is_due_to_close(now)
    widened = _task(rotation_hours=3.0, rotation_hours_max=4.0, hold_hours=2.6,
                    opened_at=now - 2.7 * 3600)
    assert widened.effective_hold_hours() == 3.0            # 抽到的值夹进新区间
    assert not widened.is_due_to_close(now)


def test_positions_opened_before_the_upgrade_keep_the_old_behaviour():
    """升级前开的仓没有抽签记录：按区间下限（也就是原来的固定周期）算。"""
    now = 100_000.0
    t = _task(rotation_hours=0.5, hold_hours=None, opened_at=now - 0.51 * 3600)
    assert t.effective_hold_hours() == 0.5 and t.is_due_to_close(now)


def test_reasons_show_the_random_duration_and_the_range():
    now = 100_000.0
    holding = HEDGED
    idle = decide(task=_task(rotation_hours=1.0, rotation_hours_max=2.0, hold_hours=1.37,
                             opened_at=now - 3600),
                  health=holding, risk=RiskVerdict("none", None), choice=None,
                  pre_open=None, quantity=0.0, now=now)
    assert idle.plan == "idle"
    assert "1.37" in idle.reason and "1–2" in idle.reason
    close = decide(task=_task(rotation_hours=1.0, rotation_hours_max=2.0, hold_hours=1.37,
                              opened_at=now - 1.4 * 3600),
                   health=holding, risk=RiskVerdict("none", None), choice=None,
                   pre_open=None, quantity=0.0, now=now)
    assert close.plan == "close" and "随机" in close.reason
