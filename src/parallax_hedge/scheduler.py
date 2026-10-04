"""轮换调度的决策 —— 纯函数，不碰网络也不碰时钟。

每个周期问一个问题：现在该做什么？
优先级从高到低，安全动作永远压过交易动作：

    1. 孤腿      → 立刻平掉（最高优先级，无条件）
    2. 风险触发  → 双腿同平
    3. 持仓到期  → 双腿同平（正常轮换）
    4. 空仓到点  → 开仓
    5. 其余      → 什么都不做

「到期」和「到点」都以调用方传进来的 now 为准，不在这里读时钟 ——
这样测试能直接构造任意时刻，不用等。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Literal

from .funding import DirectionChoice
from .positions import HedgeHealth
from .risk import PreOpenDecision, RiskVerdict

Plan = Literal["idle", "open", "close", "flatten_orphan", "blocked"]


def draw_hold_hours(
    min_hours: float, max_hours: float | None, rng: random.Random | None = None
) -> float:
    """在 [最短, 最长] 里均匀抽一个持有时长。

    【每次开仓只抽一次，抽完落库】。如果每一轮（20 秒）都重新抽，
    实际持有时长会被系统性地压向下限：每一轮都多一次「抽到比已持有时间短」
    的机会，1~2 小时的设置实际会在 1 小时出头就平掉。
    """
    lo = float(min_hours)
    hi = float(max_hours) if max_hours else lo
    if hi < lo:
        lo, hi = hi, lo
    if hi - lo < 1e-9:
        return lo
    return (rng or random).uniform(lo, hi)


@dataclass(frozen=True)
class TaskState:
    """一个币种任务的状态。时间统一用 epoch 秒。"""

    asset: str
    enabled: bool
    rotation_hours: float                   # 轮换区间下限；固定周期时就是周期本身
    leverage: float
    opened_at: float | None = None          # 当前这轮的开仓时刻
    corridor_at_open_pct: float | None = None
    last_closed_at: float | None = None
    cooldown_seconds: float = 30.0          # 平仓后隔多久才允许再开
    rotation_hours_max: float | None = None # 轮换区间上限；空 = 固定周期
    hold_hours: float | None = None         # 这一轮开仓时抽到的持有时长

    @property
    def rotation_range(self) -> tuple[float, float]:
        lo = float(self.rotation_hours)
        hi = self.rotation_hours_max
        hi = float(hi) if hi and float(hi) > lo else lo
        return lo, hi

    @property
    def is_random(self) -> bool:
        lo, hi = self.rotation_range
        return hi > lo

    def effective_hold_hours(self) -> float:
        """本轮真正生效的持有时长。

        抽到的值会被夹进【当前】的区间 —— 持仓期间改了设置，新设置立刻生效：
        把 1~3 小时改成固定 0.5 小时，已经持有 1 小时的仓位这一轮就会平掉，
        而不是继续按旧的抽签结果等下去。
        没有抽签记录（升级前开的仓）就按区间下限算，和改版前的行为一致。
        """
        lo, hi = self.rotation_range
        if self.hold_hours is None:
            return lo
        return min(max(float(self.hold_hours), lo), hi)

    def held_seconds(self, now: float) -> float | None:
        return None if self.opened_at is None else max(0.0, now - self.opened_at)

    def is_due_to_close(self, now: float) -> bool:
        held = self.held_seconds(now)
        return held is not None and held >= self.effective_hold_hours() * 3600

    def cooldown_remaining(self, now: float) -> float:
        if self.last_closed_at is None:
            return 0.0
        return max(0.0, self.cooldown_seconds - (now - self.last_closed_at))


@dataclass(frozen=True)
class CycleDecision:
    plan: Plan
    reason: str
    urgent: bool = False
    direction: str | None = None
    quantity: float = 0.0
    orphan_venue: str | None = None
    orphan_size: float = 0.0
    # 这次平仓能不能慢慢挂单平：只有正常轮换和停用收尾可以；风控触发的平仓必须立刻吃单
    maker_ok: bool = False
    # 已到最长持有：可以不再等价，跟盘挂 maker。
    # 亏过差额也不改吃单。平时亏过差额就先不发。
    force_close: bool = False

    @property
    def is_action(self) -> bool:
        return self.plan not in ("idle", "blocked")


def decide(
    *,
    task: TaskState,
    health: HedgeHealth,
    risk: RiskVerdict,
    choice: DirectionChoice | None,
    pre_open: PreOpenDecision | None,
    quantity: float,
    now: float,
) -> CycleDecision:
    """一个周期的裁决。"""

    # ── 1. 孤腿：最高优先级，连 enabled 都不看 ──
    # 任务被停掉不代表裸腿可以放着不管。
    if risk.action == "flatten_orphan":
        leg = health.open_legs[0] if health.open_legs else None
        return CycleDecision(
            "flatten_orphan", risk.reason or "只剩一条腿，立刻平掉", urgent=True,
            orphan_venue=leg.venue if leg else None,
            orphan_size=leg.size if leg else 0.0,
        )

    # ── 2. 风险触发：双腿同平 ──
    if risk.action == "close_both":
        return CycleDecision("close", risk.reason or "风险触发，双腿同平", urgent=risk.urgent)

    holding = bool(health.open_legs)

    # ── 任务被停：有仓就平掉收尾，没仓就闲置 ──
    if not task.enabled:
        if holding:
            return CycleDecision("close", "任务已停用，平掉现有仓位收尾", maker_ok=True)
        return CycleDecision("idle", "任务已停用")

    # ── 3. 持仓到期：正常轮换 ──
    if holding:
        target = task.effective_hold_hours()
        lo, hi = task.rotation_range
        if task.is_due_to_close(now):
            held = task.held_seconds(now) or 0.0
            if task.is_random:
                text = (f"持仓 {held / 3600:.2f} 小时，已达本轮随机时长 {target:.2f} 小时"
                        f"（区间 {lo:g}–{hi:g}h）")
            else:
                text = f"持仓 {held / 3600:.2f} 小时，已达轮换周期 {target:g} 小时"
            return CycleDecision("close", text, maker_ok=True)
        held = task.held_seconds(now)
        if held is None:
            # 有仓但不知道什么时候开的。引擎会在决策之前接管并开始计时；
            # 走到这里说明接管条件不满足（比如两条腿对不上），交给风控看着。
            return CycleDecision("idle", "接管已有仓位，按新周期计时")
        left = target * 3600 - held
        suffix = f"（本轮 {target:.2f}h，区间 {lo:g}–{hi:g}h）" if task.is_random else ""
        return CycleDecision("idle", f"持仓中，距轮换还有 {left / 3600:.2f} 小时{suffix}")

    # ── 4. 空仓：够不够条件开 ──
    cooldown = task.cooldown_remaining(now)
    if cooldown > 0:
        return CycleDecision("idle", f"平仓冷却中，还有 {cooldown:.0f} 秒")

    if choice is None:
        return CycleDecision("blocked", "拿不到两边的资金费率，本轮不开")
    if not choice.tradable:
        return CycleDecision("blocked", choice.reason or "费率数据存疑，拒绝开仓")
    if pre_open is None:
        return CycleDecision("blocked", "缺少开仓前预检结果")
    if not pre_open.ok:
        return CycleDecision("blocked", pre_open.reason or "开仓前预检未通过")
    if quantity <= 0:
        return CycleDecision("blocked", "可开数量为 0（保证金不足）")

    return CycleDecision(
        "open",
        f"按 {choice.direction_label} 开仓 {quantity:g}，"
        f"净资金费 {choice.net_bps_per_hour:+.3f} bps/h",
        direction=choice.direction,
        quantity=quantity,
    )


def should_verify_corridor(decision: CycleDecision, opened_ok: bool) -> bool:
    """开仓成功后必须用真实强平价复核走廊 —— 估算可能偏乐观。"""
    return decision.plan == "open" and opened_ok
