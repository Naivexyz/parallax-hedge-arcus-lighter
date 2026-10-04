"""风控决策层 —— 纯逻辑，不碰网络。

三道防线，缺一不可：

  1. 开仓前预检：按杠杆估出走廊宽度，太窄就拒绝开仓。
  2. 开仓后复核：用真实仓位重算走廊 —— Lighter 用交易所返回的强平价，
     Arcus 不返回强平价，用它返回的实际保证金、开仓价和维持保证金率复算；
     如果比估算差，立刻双腿平掉。这一步是估算失效时的兜底，代价只是一次往返成本。
  3. 孤腿立即平：任何时候发现只剩一条腿，无条件市价平掉。

另外还有两层平仓触发，放在不同距离上，失效方式互补：
  · 程序监控（主力）：剩余距离掉到开仓时的一半就双腿同平。
  · 交易所挂单（兜底）：贴着强平价放，平时不该触发 ——
    一旦触发说明程序已经死了。两条腿【各挂一对止盈止损】，
    因为任一边界都要同时清掉两条腿；只挂单边止损 = 另一条腿裸奔。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .positions import HedgeHealth, LegPosition

# 开仓门槛：估算走廊低于它就不开。Arcus 这边是全仓，走廊看的是
# 「账户权益 ÷ 仓位名义额」，不是杠杆设置（见 arcus.cross_distance）。
DEFAULT_MIN_CORRIDOR_PCT = 3.0
# 平仓线：持仓期间离强平小于它就两边一起平
DEFAULT_CLOSE_CORRIDOR_PCT = 1.5
# 程序监控的触发点：剩余距离掉到开仓走廊的这个比例就双腿同平
DEFAULT_MONITOR_CLOSE_FRACTION = 0.5
# 交易所兜底单的位置：往强平价方向走完这个比例
DEFAULT_BACKSTOP_FRACTION = 0.8

Action = Literal["none", "close_both", "flatten_orphan", "reject"]


def estimate_corridor_pct(
    leverage: float, max_leverage: float, maintenance_fraction: float | None = None,
) -> float | None:
    """按杠杆估算 Arcus 那条腿（逐仓）的强平距离（百分比）。

        强平距离 ≈ 1 / 杠杆 − 维持保证金率

    逐仓开仓时划进去的保证金 = 名义 / 杠杆；价格往不利方向走到
    剩余权益只够维持保证金时强平，所以距离约等于 1/杠杆 − MMF。
    Arcus 的 /v1/markets 直接给了 maintenanceMarginFraction，优先用它。
    没给时退回 1 / (2 × 最大杠杆) —— 上一个版本（Hyperliquid）实测校准过的经验值，
    对 Arcus 同样偏保守（BTC：IMF 0.05 / MMF 0.02 → 1/40=0.025 > 0.02）。

    这只是【估算】，用于开仓前快速否决明显不行的配置；
    真正算数的是开仓后用实际仓位（保证金、开仓价）复算的强平价做的复核。
    """
    if leverage <= 0 or max_leverage <= 0:
        return None
    if maintenance_fraction and 0 < maintenance_fraction < 1:
        maintenance = float(maintenance_fraction)
    else:
        maintenance = 1.0 / (2.0 * max_leverage)
    corridor = 1.0 / leverage - maintenance
    return corridor * 100.0 if corridor > 0 else 0.0


@dataclass(frozen=True)
class PreOpenDecision:
    ok: bool
    reason: str | None
    estimated_corridor_pct: float | None
    leverage: float
    quantity: float


def effective_leverages(
    task_leverage: float, *, arcus_max: float | None, lighter_max: float | None,
) -> tuple[int, int]:
    """两边真正要设的杠杆 (Lighter, Arcus)。

    按任务填的值，各自不超过该所该市场的上限（想「拉满」就在任务里填上限），
    取整数 —— Arcus 的 setLeverage 只收整数，两边统一口径。
    美股等 RWA 市场休市时 Arcus 的上限会降（初始保证金率 ×1.5），
    这里用的是【当前时段】的上限。
    某一边的上限查不到时按任务值设，交易所不接受会拒掉，开仓随之中止。
    """
    target = max(1, int(math.floor(float(task_leverage) + 1e-9)))

    def clamp(limit: float | None) -> int:
        if not limit or limit <= 0:
            return target
        return max(1, min(target, int(math.floor(float(limit) + 1e-9))))

    return clamp(lighter_max), clamp(arcus_max)


def pre_open_check(
    *,
    leverage: float,
    max_leverage: float,
    quantity: float,
    price: float,
    min_quantity: float,
    lighter_available: float,
    arcus_available: float,
    min_corridor_pct: float = DEFAULT_MIN_CORRIDOR_PCT,
    lighter_leverage: float | None = None,
    arcus_leverage: float | None = None,
    maintenance_fraction: float | None = None,
    min_notional: float | None = None,
    arcus_equity: float | None = None,
    arcus_other_notional: float = 0.0,
    arcus_other_maintenance: float = 0.0,
) -> PreOpenDecision:
    """开仓前的硬闸门。任何一条不过就不开，并说明原因。

    lighter_leverage / arcus_leverage：两边实际会设的杠杆。不给就都按 leverage。
    强平走廊：给了 arcus_equity 就按全仓（账户权益 ÷ 名义额）估，
    否则退回按杠杆估（1/杠杆 − 维持保证金率，相当于逐仓）；
    保证金两边各按各的杠杆算。
    min_notional：两所最小下单金额里较大的那个（Arcus 5 USD / Lighter 10 USDC 之类）。
    """
    lighter_lev = float(lighter_leverage or leverage)
    leverage = float(arcus_leverage or leverage)
    if arcus_equity is not None and quantity > 0 and price > 0:
        # 全仓：按账户权益 vs 这笔仓位（加上账户里已有的 Arcus 仓位）估。
        # 开仓手续费和滑点先从权益里扣掉（按名义额的 5 bps 预留）。
        from .arcus import cross_distance
        notional = quantity * price
        mmf = maintenance_fraction if maintenance_fraction else 1.0 / (2.0 * max_leverage)
        x = cross_distance(
            equity=float(arcus_equity) - notional * 0.0005, notional=notional, mmf=mmf,
            other_notional=arcus_other_notional, other_maintenance=arcus_other_maintenance,
        )
        corridor = None if x is None else x * 100.0
    else:
        corridor = estimate_corridor_pct(leverage, max_leverage, maintenance_fraction)

    def no(reason: str) -> PreOpenDecision:
        return PreOpenDecision(False, reason, corridor, leverage, quantity)

    if leverage <= 0:
        return no("杠杆必须大于 0")
    if leverage > max_leverage:
        return no(f"杠杆 {leverage:g}× 超过该市场上限 {max_leverage:g}×")
    if quantity < min_quantity:
        return no(f"数量 {quantity:g} 低于最小下单量 {min_quantity:g}")
    if price <= 0:
        return no("没有可用的价格")
    if min_notional and quantity * price < float(min_notional):
        return no(
            f"名义金额 {quantity * price:.2f} USDC 低于两所最小下单金额 {float(min_notional):g}"
        )
    if corridor is None or corridor < min_corridor_pct:
        basis = (f"按 Arcus 账户权益 {float(arcus_equity):.2f} USDC（全仓）"
                 if arcus_equity is not None else f"按 {leverage:g}× 杠杆")
        return no(
            f"{basis}估算的强平走廊只有 {corridor or 0:.2f}%，"
            f"低于开仓门槛 {min_corridor_pct:g}% —— 行情一动就会被强制平仓，"
            f"每次都要付一遍往返成本"
        )

    # 两边都要放得下这笔仓位的保证金，各按各的杠杆算
    notional = quantity * price
    for side, available, lev in (("Lighter", lighter_available, lighter_lev),
                                 ("Arcus", arcus_available, leverage)):
        if lev <= 0:
            return no(f"{side} 杠杆必须大于 0")
        required = notional / lev
        if available < required:
            return no(
                f"{side} 可用保证金 {available:.2f} USDC 不足以支撑 "
                f"{notional:.2f} USDC 的仓位（按 {lev:g}× 需要 {required:.2f}）"
            )
    return PreOpenDecision(True, None, corridor, leverage, quantity)


def max_quantity_for(
    *,
    leverage: float,
    price: float,
    lighter_available: float,
    arcus_available: float,
    quantity_decimals: int,
    safety_factor: float = 0.95,
    lighter_leverage: float | None = None,
    arcus_leverage: float | None = None,
) -> float:
    """资金小的那一边能开的最大数量。

    两边各按【各自实际会设的杠杆】算能放多大，取小的那个。
    留 5% 余量：保证金占用会因为成交价滑动和手续费略高于估算，
    卡着上限开会被交易所以「保证金不足」直接拒单。
    """
    lighter_lev = float(lighter_leverage or leverage)
    arcus_lev = float(arcus_leverage or leverage)
    if price <= 0 or lighter_lev <= 0 or arcus_lev <= 0:
        return 0.0
    capacity = min(lighter_available * lighter_lev, arcus_available * arcus_lev)
    if capacity <= 0:
        return 0.0
    raw = capacity * safety_factor / price
    # 必须向下取整到精度档位（向上会被交易所以保证金不足拒单），
    # 但不能直接用 raw // step：浮点下 0.95 // 0.01 == 94 而不是 95，
    # 每次都会白白少开一档 —— 对「每次开满」的用法这是每次都发生的损失。
    # 加 1e-9 只吸收浮点误差，真正的 0.9499 仍会正确地落到 0.94。
    scale = 10.0 ** quantity_decimals
    units = math.floor(raw * scale + 1e-9)
    return max(0.0, units / scale)


def sizing_limit(
    *,
    price: float,
    lighter_available: float,
    arcus_available: float,
    lighter_leverage: float,
    arcus_leverage: float,
    notional_cap: float | None,
    safety_factor: float = 0.95,
) -> str | None:
    """这次开仓的数量是被什么卡住的 —— 写进开仓记录。

    数量看着不对时（2026-09-20 SNDK 只开出 0.0891），一眼就能看出是名义上限，
    还是哪一边的可用保证金在卡，不用事后去倒推。比的是「可用 × 该所杠杆」，
    和 max_quantity_for 同一个口径。
    """
    if price <= 0:
        return None
    lighter_cap = max(0.0, lighter_available) * lighter_leverage
    arcus_cap = max(0.0, arcus_available) * arcus_leverage
    if notional_cap and float(notional_cap) <= min(lighter_cap, arcus_cap) * safety_factor:
        return f"按名义上限 {float(notional_cap):g} USDC"
    if lighter_cap <= arcus_cap:
        return f"受 Lighter 可用 {lighter_available:.2f} USDC 限制"
    return f"受 Arcus 可用 {arcus_available:.2f} USDC 限制"


@dataclass(frozen=True)
class CorridorCheck:
    ok: bool
    reason: str | None
    actual_corridor_pct: float | None


def verify_corridor_after_open(
    health: HedgeHealth, min_corridor_pct: float = DEFAULT_CLOSE_CORRIDOR_PCT
) -> CorridorCheck:
    """开仓后用交易所真实返回的强平价复核走廊。

    公式可能错、维持保证金规则可能改、两个所的规则可能不同 ——
    但交易所自己报出来的强平价不会错。这一步比预检更重要：
    不过就立刻双腿平掉，代价只是一次往返成本，比扛着一个
    走廊过窄的仓位过夜便宜得多。
    """
    if not health.open_legs:
        return CorridorCheck(True, None, None)
    if any(not p.liquidation_side_is_sane for p in health.open_legs):
        return CorridorCheck(False, "强平价方向异常，数据不可信", None)
    actual = health.min_distance_pct
    if actual is None:
        return CorridorCheck(False, "拿不到强平价，无法确认走廊宽度", None)
    if actual < min_corridor_pct:
        venue = health.riskiest_leg.venue if health.riskiest_leg else "?"
        return CorridorCheck(
            False,
            f"实际走廊只有 {actual:.2f}%（{venue} 腿），低于下限 "
            f"{min_corridor_pct:g}% —— 开出来就已经在平仓线以内，立刻平掉",
            actual,
        )
    return CorridorCheck(True, None, actual)


@dataclass(frozen=True)
class ProtectiveLevels:
    """两条腿各一对止盈止损的触发价。

    任一边界都会【同时】清掉两条腿：
      涨到上边界 → 空腿止损 + 多腿止盈
      跌到下边界 → 多腿止损 + 空腿止盈
    只给受伤的那条腿挂止损，触发后另一条腿就裸奔了 —— 这是
    对冲策略最经典的死法，所以四个单必须成套下。
    """

    upper: float
    lower: float
    long_venue: str
    short_venue: str

    def for_leg(self, leg: LegPosition) -> dict[str, float]:
        """返回这条腿的 {stop_loss, take_profit} 触发价。"""
        if leg.size > 0:                       # 多头：跌了止损、涨了止盈
            return {"stop_loss": self.lower, "take_profit": self.upper}
        return {"stop_loss": self.upper, "take_profit": self.lower}


def protective_levels(
    health: HedgeHealth, backstop_fraction: float = DEFAULT_BACKSTOP_FRACTION
) -> ProtectiveLevels | None:
    """把兜底单放在「往强平价走完 backstop_fraction」的位置。

    留出的那一段是给滑点和链上确认延迟的 —— 贴着强平价放，
    触发时很可能已经来不及成交就被交易所强平了。
    """
    if not health.is_hedged:
        return None
    longs = [p for p in health.open_legs if p.size > 0]
    shorts = [p for p in health.open_legs if p.size < 0]
    if len(longs) != 1 or len(shorts) != 1:
        return None
    long_leg, short_leg = longs[0], shorts[0]
    if not (long_leg.mark_price and short_leg.mark_price):
        return None
    if not (long_leg.liquidation_price and short_leg.liquidation_price):
        return None
    if not (long_leg.liquidation_side_is_sane and short_leg.liquidation_side_is_sane):
        return None

    # 上边界由空腿的强平价决定，下边界由多腿的
    upper = short_leg.mark_price + (
        short_leg.liquidation_price - short_leg.mark_price
    ) * backstop_fraction
    lower = long_leg.mark_price - (
        long_leg.mark_price - long_leg.liquidation_price
    ) * backstop_fraction
    if not (lower < min(long_leg.mark_price, short_leg.mark_price) < upper):
        return None
    return ProtectiveLevels(
        upper=upper, lower=lower,
        long_venue=long_leg.venue, short_venue=short_leg.venue,
    )


@dataclass(frozen=True)
class RiskVerdict:
    action: Action
    reason: str | None
    urgent: bool = False


def evaluate(
    health: HedgeHealth,
    *,
    corridor_at_open_pct: float | None,
    monitor_close_fraction: float = DEFAULT_MONITOR_CLOSE_FRACTION,
    min_corridor_pct: float = DEFAULT_CLOSE_CORRIDOR_PCT,
) -> RiskVerdict:
    """持仓期间每个周期的裁决。返回该做什么。"""
    legs = health.open_legs
    if not legs:
        return RiskVerdict("none", None)

    # 孤腿 —— 最高优先级，无条件立刻平掉。
    # 多半是兜底单在程序离线时打掉了一条，剩下的这条正在裸奔。
    if len(legs) == 1:
        leg = legs[0]
        return RiskVerdict(
            "flatten_orphan",
            f"只剩 {leg.venue} 一条腿（{leg.side} {abs(leg.size):g}），"
            f"另一条已不在 —— 当前是满仓单边裸露，立刻挂 maker 平掉，不吃单",
            urgent=True,
        )

    if not health.is_hedged:
        return RiskVerdict(
            "close_both",
            f"两条腿不构成对冲（净敞口 {health.net_size:+g}）—— 平掉重来",
            urgent=True,
        )

    if any(not p.liquidation_side_is_sane for p in legs):
        return RiskVerdict(
            "close_both", "强平价方向异常，数据不可信 —— 平掉止损", urgent=True
        )

    distance = health.min_distance_pct
    if distance is None:
        return RiskVerdict("none", "暂时拿不到强平价，本轮不做判断")

    # 绝对下限：无论开仓时多宽，掉到这个线都必须走
    if distance < min_corridor_pct:
        venue = health.riskiest_leg.venue if health.riskiest_leg else "?"
        return RiskVerdict(
            "close_both",
            f"{venue} 腿距强平仅剩 {distance:.2f}%，低于平仓线 {min_corridor_pct:g}%",
            urgent=True,
        )

    # 相对触发：剩余距离掉到开仓走廊的一半
    if corridor_at_open_pct and corridor_at_open_pct > 0:
        threshold = corridor_at_open_pct * monitor_close_fraction
        if distance < threshold:
            venue = health.riskiest_leg.venue if health.riskiest_leg else "?"
            return RiskVerdict(
                "close_both",
                f"{venue} 腿距强平 {distance:.2f}%，已不足开仓时 "
                f"{corridor_at_open_pct:.2f}% 的 {monitor_close_fraction:.0%} —— 双腿同平",
            )
    return RiskVerdict("none", None)
