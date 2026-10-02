"""Arcus 与 Lighter 的可执行价差闸门。

开仓、补仓：绝对价差不超过 MAX_SPREAD_BPS（默认 1 bp）才允许挂 maker。
方向：买更便宜的卖一，卖更贵的买一，而且两条腿必须在不同的所。

计划内平仓不看这个绝对值。平仓买价低于卖价（价差有利、在收价差）就立刻挂 maker，
差多少 bp 都平；买价不低于卖价（要付价差）就不提前平，一直持有到最长持有时间。
最短持有（两腿都成交之后）仍然先拦住，包括有利的平仓。

闸门不挑币种：面板里已经能交易的重叠市场都走同一套，包括美股永续，不单限 BTC、ETH。
风控触发的平仓和持满后的强制平仓不走这里 —— 那两条路必须立刻吃单。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .books import Book


@dataclass(frozen=True)
class SpreadGate:
    ok: bool
    reason: str
    direction: str | None = None
    gap_bps: float | None = None
    abs_gap_bps: float | None = None
    gap_usd: float | None = None
    long_venue: str | None = None
    short_venue: str | None = None
    long_ask: float | None = None
    short_bid: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "direction": self.direction,
            "gap_bps": None if self.gap_bps is None else round(self.gap_bps, 4),
            "abs_gap_bps": None if self.abs_gap_bps is None else round(self.abs_gap_bps, 4),
            "gap_usd": None if self.gap_usd is None else round(self.gap_usd, 6),
            "long_venue": self.long_venue,
            "short_venue": self.short_venue,
            "long_ask": self.long_ask,
            "short_bid": self.short_bid,
        }


def join_price(book: Book, side: str) -> float | None:
    """Maker 排队价：买单跟买一，卖单跟卖一。缺那一档就没法挂。"""
    price = book.best_bid if side == "buy" else book.best_ask
    if price is None or price <= 0:
        return None
    return float(price)


def hedge_take_price(book: Book, side: str) -> float | None:
    """IOC 对冲会吃到的价：买单打卖一，卖单打买一。

    不是排队价。买单的买一是卖出才会碰到的价格，拿它当「买价」会把
    贵的一边算成便宜的一边。
    """
    price = book.best_ask if side == "buy" else book.best_bid
    if price is None or price <= 0:
        return None
    return float(price)


def evaluate_books(lighter: Book, arcus: Book, max_bps: float) -> SpreadGate:
    """用卖一买一量跨所价差。绝对值不超过 max_bps 才算通过。

    可执行价差 = 便宜那边的卖一 − 贵那边的买一。
    正数是买价高于卖价（要付价差），负数是两边已经交叉。
    阈值看绝对值：离 0 超过 MAX_SPREAD_BPS 就不开仓。
    计划内平仓不走这里，改看平仓两边的买价和卖价谁更便宜。
    """
    lighter_bid, lighter_ask = lighter.best_bid, lighter.best_ask
    arcus_bid, arcus_ask = arcus.best_bid, arcus.best_ask
    if not all(p and p > 0 for p in (lighter_bid, lighter_ask, arcus_bid, arcus_ask)):
        return SpreadGate(False, "有一边盘口缺买一或卖一，本轮不下单")

    if lighter_ask < arcus_ask:
        long_venue, short_venue = "lighter", "arcus"
        long_ask, short_bid = float(lighter_ask), float(arcus_bid)
        direction = "long_lighter_short_arcus"
        long_name, short_name = "Lighter", "Arcus"
    elif arcus_ask < lighter_ask:
        long_venue, short_venue = "arcus", "lighter"
        long_ask, short_bid = float(arcus_ask), float(lighter_bid)
        direction = "short_lighter_long_arcus"
        long_name, short_name = "Arcus", "Lighter"
    else:
        return SpreadGate(False, "两边卖一相同，分不出便宜的一边，本轮不下单")

    gap_usd = long_ask - short_bid
    mid = (long_ask + short_bid) / 2.0
    if mid <= 0:
        return SpreadGate(False, "价差中价无效，本轮不下单")
    gap_bps = gap_usd / mid * 10_000.0
    abs_bps = abs(gap_bps)
    limit = float(max_bps)
    fields = dict(
        direction=direction, gap_bps=gap_bps, abs_gap_bps=abs_bps, gap_usd=gap_usd,
        long_venue=long_venue, short_venue=short_venue,
        long_ask=long_ask, short_bid=short_bid,
    )
    if abs_bps > limit + 1e-9:
        return SpreadGate(
            False,
            f"跨所价差 {abs_bps:.2f} bp，宽于 {limit:g} bp，先不开仓",
            **fields,
        )
    return SpreadGate(
        True,
        f"跨所价差 {abs_bps:.2f} bp，不超过 {limit:g} bp，买{long_name}、卖{short_name}",
        **fields,
    )


def buy_sell_prices(
    lighter_side: str, arcus_side: str, lighter_price: float, arcus_price: float,
) -> tuple[float, float] | None:
    """按两条腿的方向取出即将买入的价格和即将卖出的价格。不是一买一卖就返回 None。"""
    if lighter_side == "buy" and arcus_side == "sell":
        return float(lighter_price), float(arcus_price)
    if lighter_side == "sell" and arcus_side == "buy":
        return float(arcus_price), float(lighter_price)
    return None


def order_prices_allowed(
    buy_price: float, sell_price: float, max_bps: float,
) -> tuple[bool, float | None, str]:
    """开仓 / 补仓发出去之前的最后一道检查，用的是即将成交的挂单价，不是卖一。

    gap = buy_price - sell_price，bps = gap / mid * 10000。
    买价不低于卖价，或者绝对价差宽于 max_bps，都不许发单。
    正好等于 max_bps 允许（和 evaluate_books 同一条边界）。
    """
    try:
        buy = float(buy_price)
        sell = float(sell_price)
    except (TypeError, ValueError):
        return False, None, "买卖价无效，不下单"
    if buy != buy or sell != sell or buy <= 0 or sell <= 0 or buy == float("inf") or sell == float("inf"):
        return False, None, "买卖价无效，不下单"
    mid = (buy + sell) / 2.0
    if mid <= 0:
        return False, None, "价差中价无效，不下单"
    bps = (buy - sell) / mid * 10_000.0
    if buy >= sell:
        return False, bps, (
            f"买价 {buy:g} 不低于卖价 {sell:g}（{bps:.2f} bp），这是贵的一边，不下单"
        )
    limit = float(max_bps)
    if abs(bps) > limit + 1e-9:
        return False, bps, (
            f"买价 {buy:g} 与卖价 {sell:g} 相差 {abs(bps):.2f} bp，宽于 {limit:g} bp，不下单"
        )
    return True, bps, f"挂单价差 {abs(bps):.2f} bp，买更便宜的一边、卖更贵的一边"


def open_sides_allowed(
    lighter_side: str, arcus_side: str, lighter_price: float, arcus_price: float,
    max_bps: float,
) -> tuple[bool, float | None, str]:
    """把两条腿的挂单价收成买价 / 卖价，再做最后一道检查。"""
    prices = buy_sell_prices(lighter_side, arcus_side, lighter_price, arcus_price)
    if prices is None:
        return False, None, "两条腿不是一买一卖，不下单"
    return order_prices_allowed(prices[0], prices[1], max_bps)


def close_prices_allowed(
    buy_price: float, sell_price: float,
) -> tuple[bool, float | None, str]:
    """计划内平仓：只看这一次平仓是在收价差还是在付价差。

    买价 < 卖价：有利。买更便宜的一边、卖更贵的一边。无论多少 bp 都立刻挂 maker。
    不看 MAX_SPREAD_BPS，不等缺口缩回开仓阈值，也不等价差回到 0。
    某一个所一直更贵，是马上平的理由，不是继续等的理由。
    买价 >= 卖价：不利，要付价差（或平价）。不许提前平，只在最长持有后强制平仓。
    """
    try:
        buy = float(buy_price)
        sell = float(sell_price)
    except (TypeError, ValueError):
        return False, None, "买卖价无效，先不平"
    if buy != buy or sell != sell or buy <= 0 or sell <= 0 or buy == float("inf") or sell == float("inf"):
        return False, None, "买卖价无效，先不平"
    mid = (buy + sell) / 2.0
    if mid <= 0:
        return False, None, "价差中价无效，先不平"
    bps = (buy - sell) / mid * 10_000.0
    if buy >= sell:
        return False, bps, (
            f"买价 {buy:g} 不低于卖价 {sell:g}（{bps:.2f} bp），平仓要付价差，"
            f"未到最长持有，先不平"
        )
    return True, bps, (
        f"价差有利：平仓买价 {buy:g} 低于卖价 {sell:g}（{abs(bps):.2f} bp），"
        f"不等价差回到 0，也不看开仓阈值，立即挂 maker"
    )


def close_sides_allowed(
    lighter_side: str, arcus_side: str, lighter_price: float, arcus_price: float,
) -> tuple[bool, float | None, str]:
    """计划内平仓用即将挂出的买价和卖价。有利就过，不利就等。"""
    prices = buy_sell_prices(lighter_side, arcus_side, lighter_price, arcus_price)
    if prices is None:
        return False, None, "两条腿不是一买一卖，先不平"
    return close_prices_allowed(prices[0], prices[1])
