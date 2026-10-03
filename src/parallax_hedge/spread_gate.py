"""Arcus 与 Lighter 的开仓方向闸门，以及平仓用的浮盈亏差额。

开仓、补仓：买更便宜的一边，卖更贵的一边。买价必须严格低于卖价。
不再用绝对价差多少 bp 决定开不开。宽，但买得更便宜，仍然开。

计划内平仓不看价差，也不看 bp。两腿都成交并过了最短持有之后，
看两边浮盈亏加总：不低于「负的浮盈亏差额」就挂 maker 平。
差额 0.02 时，合计 0、-0.02 都平，-0.05 先不平。持满最长持有仍强制平。
平仓挂 maker，不因为价差方向把单撤掉。风控触发的平仓仍然立刻吃单。

闸门不挑币种：面板里已经能交易的重叠市场都走同一套，包括美股永续。
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


def evaluate_books(lighter: Book, arcus: Book, max_bps: float | None = None) -> SpreadGate:
    """用卖一买一量跨所方向。买价必须严格低于卖价才算通过。

    可执行价差 = 便宜那边的卖一 − 贵那边的买一。
    买价不低于卖价就不开。价差有多宽（多少 bp）不再拦截。
    max_bps 保留参数只是为了旧调用方，不参与判断。
    计划内平仓不走这里，改看两边浮盈亏合计。
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
    fields = dict(
        direction=direction, gap_bps=gap_bps, abs_gap_bps=abs_bps, gap_usd=gap_usd,
        long_venue=long_venue, short_venue=short_venue,
        long_ask=long_ask, short_bid=short_bid,
    )
    if long_ask >= short_bid:
        return SpreadGate(
            False,
            f"买价 {long_ask:g} 不低于卖价 {short_bid:g}（{gap_bps:.2f} bp），"
            f"这是贵的一边，本轮不下单",
            **fields,
        )
    return SpreadGate(
        True,
        f"买{long_name} {long_ask:g}、卖{short_name} {short_bid:g}"
        f"（价差 {abs_bps:.2f} bp，不看 bp 阈值）",
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
    buy_price: float, sell_price: float, max_bps: float | None = None,
) -> tuple[bool, float | None, str]:
    """开仓 / 补仓发出去之前的最后一道检查，用的是即将成交的挂单价，不是卖一。

    买价必须严格低于卖价。绝对价差多少 bp 不再拦截，max_bps 不参与判断。
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
    return True, bps, (
        f"买价 {buy:g} 低于卖价 {sell:g}（{abs(bps):.2f} bp），"
        f"不看 bp 阈值，买更便宜的一边、卖更贵的一边"
    )


def open_sides_allowed(
    lighter_side: str, arcus_side: str, lighter_price: float, arcus_price: float,
    max_bps: float,
) -> tuple[bool, float | None, str]:
    """把两条腿的挂单价收成买价 / 卖价，再做最后一道检查。"""
    prices = buy_sell_prices(lighter_side, arcus_side, lighter_price, arcus_price)
    if prices is None:
        return False, None, "两条腿不是一买一卖，不下单"
    return order_prices_allowed(prices[0], prices[1], max_bps)


def unrealized_close_ready(net_pnl: float, window_usd: float) -> bool:
    """最短持有之后，两腿浮盈亏合计是否可以挂 maker 平。

    window 是面板上的「浮盈亏差额」（USDC）。合计不低于 -window 就平。
    差额 0.02：合计 0 平，-0.02 平，-0.05 先不平。持满最长持有不走这里。
    """
    try:
        net = float(net_pnl)
        window = float(window_usd)
    except (TypeError, ValueError):
        return False
    if net != net or window != window or net in (float("inf"), float("-inf")):
        return False
    if window < 0:
        window = 0.0
    return net + 1e-9 >= -window


def close_prices_allowed(
    buy_price: float, sell_price: float,
) -> tuple[bool, float | None, str]:
    """旧的平仓价差方向检查。计划内平仓不再用它决定平不平。

    保留函数是为了对照历史成交。新的平仓看 unrealized_close_ready。
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
