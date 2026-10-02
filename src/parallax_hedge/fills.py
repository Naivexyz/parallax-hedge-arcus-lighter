"""成交判定 —— 纯逻辑，不碰网络。

这一层的存在理由，是 2026-09-17 那笔实盘教训：
Lighter 的订单返回了 code=200 和 tx_hash，看起来完全成功，
但 filled_delta 是 0 —— 一张都没成。

所以【永远不要用订单返回判断成交】，只认真实仓位的变化。
这是 Parallax 实盘验证过的做法，照搬。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# 判定「确实成交了」的下限：请求量的 0.1%。
#
# 注意这个数【远小于】配对时用的 2% 容差，两者不能混用：
# 用 2% 去判断「有没有成交」，会把一笔小额部分成交误判成「没成交」，
# 于是不给它对冲 —— 结果留下一条裸腿。宁可把极小的成交也当成交去对冲。
MEANINGFUL_FILL_FRACTION = 0.001
# 两条腿数量匹配的容差：2%
PAIR_MATCH_FRACTION = 0.02

FillKind = Literal["full", "partial", "none", "wrong_direction"]


@dataclass(frozen=True)
class FillVerdict:
    kind: FillKind
    filled: float          # 带符号的成交量
    requested: float       # 带符号的请求量
    should_hedge: bool     # 要不要给它配对冲腿
    reason: str | None = None

    @property
    def is_fill(self) -> bool:
        return self.kind in ("full", "partial")


def classify_fill(
    *,
    before: float,
    after: float,
    requested_signed: float,
    meaningful_fraction: float = MEANINGFUL_FILL_FRACTION,
) -> FillVerdict:
    """比对下单前后的真实仓位，判定成交情况。

    before/after 是【带符号】的仓位（正=多，负=空）。
    requested_signed 是这一笔想要的带符号变化量。
    """
    magnitude = abs(requested_signed)
    if magnitude <= 0:
        return FillVerdict("none", 0.0, requested_signed, False, "请求量为 0")
    if not (math.isfinite(before) and math.isfinite(after)):
        # 读不到仓位时【必须当成可能已经成交】去处理，不能当成没成交 ——
        # 当成没成交就不会去对冲，万一其实成了就是裸腿。
        return FillVerdict(
            "partial", float("nan"), requested_signed, True,
            "读不到仓位，按「可能已成交」处理以免留下裸腿",
        )

    delta = after - before
    threshold = max(1e-9, magnitude * meaningful_fraction)
    if abs(delta) <= threshold:
        return FillVerdict("none", 0.0, requested_signed, False, "仓位没有变化")

    expected_sign = 1.0 if requested_signed > 0 else -1.0
    if delta * expected_sign <= 0:
        # 方向反了 —— 多半是别处的操作或交易所异常，不能拿它当我们的成交
        return FillVerdict(
            "wrong_direction", delta, requested_signed, False,
            f"仓位变化 {delta:+g} 与请求方向 {expected_sign:+g} 相反",
        )

    if abs(abs(delta) - magnitude) <= max(1e-9, magnitude * meaningful_fraction):
        return FillVerdict("full", delta, requested_signed, True)
    return FillVerdict(
        "partial", delta, requested_signed, True,
        f"部分成交 {abs(delta):g}/{magnitude:g}",
    )


def positions_match(a: float, b: float, reference: float,
                    tolerance_fraction: float = PAIR_MATCH_FRACTION) -> bool:
    """两个仓位数量是否算「一致」（用于确认对手腿没被动过）。"""
    if not (math.isfinite(a) and math.isfinite(b)):
        return False
    return abs(a - b) <= max(1e-9, abs(reference) * tolerance_fraction)


def slippage_limit_price(price: float, side: str, slippage_bps: float) -> float:
    """给 IOC 单加滑点上限。

    买单往上让、卖单往下让 —— 让的方向弄反会导致永远不成交。
    """
    if price <= 0:
        raise ValueError("价格必须为正")
    factor = slippage_bps / 10_000.0
    return price * (1.0 + factor) if side == "buy" else price * (1.0 - factor)


def align_quantity(quantity: float, decimals: int) -> float:
    """向下对齐到交易所的数量精度。

    必须向下：向上会超出可用保证金被拒单。
    不能用 // ：浮点下 0.95 // 0.01 == 94 而不是 95，每次都少一档。
    """
    if quantity <= 0:
        return 0.0
    scale = 10.0 ** decimals
    return math.floor(quantity * scale + 1e-9) / scale


def hedge_side(direction: str) -> tuple[str, str]:
    """把方向翻译成两个所各自的买卖方向。

    返回 (lighter_side, arcus_side)。
    """
    if direction == "long_lighter_short_arcus":
        return "buy", "sell"
    if direction == "short_lighter_long_arcus":
        return "sell", "buy"
    raise ValueError(f"未知方向：{direction}")


def closing_sides(lighter_size: float, arcus_size: float) -> tuple[str, str]:
    """平仓时两条腿各自该下什么方向 —— 与持仓方向相反。"""
    return (
        "sell" if lighter_size > 0 else "buy",
        "sell" if arcus_size > 0 else "buy",
    )
