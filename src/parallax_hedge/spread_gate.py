"""Arcus 与 Lighter 的开仓方向闸门，以及平仓用的浮盈亏差额。

开仓、补仓：买更便宜的一边，卖更贵的一边。买价必须严格低于卖价。
两所之间长期存在的价差不是亏损。两边一起挂 maker 开、一起挂 maker 平，
所间价差会原样还回去。不用它拦截开仓。

计划内平仓只看每一边自己的开仓价和自己的 maker 平仓价。
两腿加总差于面板「浮盈亏差额」就先不发，继续挂着。
两边书没动时，这个合计接近 0，可以平。差额默认 0.02：合计 0、-0.02 都平，更差先不平。
正常平仓两边都挂 maker。风控触发的平仓仍然立刻吃单。

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


def maker_exit_price(
    side: str, entry: float, quantity: float, join: float,
    best_bid: float | None, best_ask: float | None, tolerance_usd: float,
) -> float | None:
    """单腿退出的 maker 价。能跟盘口就跟；跟了会亏过差额就往回挂，绝不穿过对手价。

    卖单不能打到买一，买单不能打到卖一。join 在差额以内就用 join。
    否则挂在「刚好亏到差额」的那一档；那一档会吃单时，就停在不吃单的一侧。
    """
    try:
        entry_f = float(entry)
        qty = abs(float(quantity))
        join_f = float(join)
        window = float(tolerance_usd)
    except (TypeError, ValueError):
        return None
    if entry_f != entry_f or qty != qty or join_f != join_f or window != window:
        return None
    if qty <= 0 or entry_f <= 0 or join_f <= 0:
        return None
    if window < 0 or window in (float("inf"), float("-inf")):
        window = 0.0
    bid = None
    ask = None
    try:
        if best_bid is not None and float(best_bid) > 0:
            bid = float(best_bid)
        if best_ask is not None and float(best_ask) > 0:
            ask = float(best_ask)
    except (TypeError, ValueError):
        return None

    def pnl(px: float) -> float:
        if side == "sell":
            return (px - entry_f) * qty
        return (entry_f - px) * qty

    def crosses(px: float) -> bool:
        if side == "sell":
            return bid is not None and px <= bid
        return ask is not None and px >= ask

    if unrealized_close_ready(pnl(join_f), window) and not crosses(join_f):
        return join_f
    if side == "sell":
        limit = entry_f - (window / qty)
    else:
        limit = entry_f + (window / qty)
    if limit <= 0 or limit != limit or limit in (float("inf"), float("-inf")):
        return None
    if not crosses(limit):
        return limit
    # 差额允许的价已经穿到对手价里面：停在不吃单的那一侧，不追。
    nudge = max((bid or ask or join_f) * 1e-8, 1e-8)
    if side == "sell":
        if ask is not None and not crosses(ask) and unrealized_close_ready(pnl(ask), window):
            return ask
        if bid is not None:
            return bid + nudge
        return None
    if bid is not None and not crosses(bid) and unrealized_close_ready(pnl(bid), window):
        return bid
    if ask is not None:
        backed = ask - nudge
        return backed if backed > 0 else None
    return None


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


def round_trip_close_net(
    lighter_size: float, lighter_entry: float | None, lighter_close: float,
    arcus_size: float, arcus_entry: float | None, arcus_close: float,
) -> float | None:
    """每一边自己的开仓价对上自己的 maker 平仓价，再加总。不用标记价。

    带符号数量：多头为正。盈亏 = (平仓价 - 开仓价) × 数量，空头同样成立。
    两所之间的价差不算进这笔亏损。两边平仓价等于各自开仓价时，合计是 0。
    缺开仓价、或价格无效时返回 None。调用方不能改用标记浮盈亏放行。
    """
    total = 0.0
    seen = False
    for size, entry, close in (
        (lighter_size, lighter_entry, lighter_close),
        (arcus_size, arcus_entry, arcus_close),
    ):
        try:
            qty = float(size)
            opened = float(entry) if entry is not None else float("nan")
            px = float(close)
        except (TypeError, ValueError):
            return None
        if abs(qty) <= 1e-12:
            continue
        if opened != opened or px != px or opened <= 0 or px <= 0:
            return None
        if opened in (float("inf"), float("-inf")) or px in (float("inf"), float("-inf")):
            return None
        total += (px - opened) * qty
        seen = True
    return total if seen else None


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
