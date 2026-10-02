"""两条腿的持仓与强平价监控。

这一步仍然只读 —— 不下单、不平仓，只把两边的强平价算出来摆在面上，
让人先确认数字读得对。

设计上最重要的一条（来自 2026-09-18 的方案确认）：
    对冲仓位【没有方向风险，只有各自的保证金风险】。
    币价暴涨时空腿亏损、保证金被吃掉、面临强平，而另一所的多腿在赚钱。
    净敞口是零，但一边可以在另一边正浮盈的时候把你爆掉。
所以危险度看的是【两条腿里更危险的那一条】，而处置动作是【双腿同时平掉】，
绝不单腿止损 —— 单腿平掉的瞬间，另一条腿就变成了满仓裸单边。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .arcus import account_exposure, arcus_liquidation_price


@dataclass(frozen=True)
class LegPosition:
    """一条腿上的持仓快照。"""

    venue: str
    symbol: str
    size: float                  # 带符号：正=多，负=空
    entry_price: float | None
    mark_price: float | None
    liquidation_price: float | None
    unrealized_pnl: float | None
    margin: float | None

    @property
    def is_open(self) -> bool:
        return abs(self.size) > 1e-12

    @property
    def side(self) -> str:
        if not self.is_open:
            return "无持仓"
        return "多" if self.size > 0 else "空"

    @property
    def distance_pct(self) -> float | None:
        """现价距强平价还有百分之几。越小越危险。

        交易所在没有持仓、或仓位小到算不出强平价时会返回 0 或 null，
        这时候【不能当成"距离 0%"】—— 那会让面板显示成命悬一线。
        """
        if not self.is_open:
            return None
        mark = self.mark_price
        liq = self.liquidation_price
        if not mark or not liq or mark <= 0 or liq <= 0:
            return None
        return abs(mark - liq) / mark * 100.0

    @property
    def liquidation_side_is_sane(self) -> bool:
        """强平价必须在亏损的那一侧：多头的强平价低于现价，空头的高于现价。

        方向不对说明字段读错了或者交易所返回异常 —— 宁可标红也不要
        拿一个反的强平价去做"还很安全"的判断。
        """
        if not self.is_open or not self.mark_price or not self.liquidation_price:
            return True
        if self.size > 0:
            return self.liquidation_price < self.mark_price
        return self.liquidation_price > self.mark_price


@dataclass(frozen=True)
class HedgeHealth:
    """一个币种上两条腿合起来的健康度。"""

    asset: str
    lighter: LegPosition | None
    arcus: LegPosition | None
    warn_distance_pct: float

    @property
    def legs(self) -> list[LegPosition]:
        return [p for p in (self.lighter, self.arcus) if p is not None]

    @property
    def open_legs(self) -> list[LegPosition]:
        return [p for p in self.legs if p.is_open]

    @property
    def is_hedged(self) -> bool:
        """两条腿方向相反、数量接近才算对冲上了。"""
        if len(self.open_legs) != 2:
            return False
        a, b = self.open_legs
        if a.size * b.size >= 0:            # 同向 = 没对冲，是双倍敞口
            return False
        return abs(abs(a.size) - abs(b.size)) <= 0.02 * max(abs(a.size), abs(b.size))

    @property
    def net_size(self) -> float:
        return sum(p.size for p in self.open_legs)

    @property
    def min_distance_pct(self) -> float | None:
        """两条腿里更危险的那一条的距离 —— 这才是约束。"""
        values = [p.distance_pct for p in self.open_legs if p.distance_pct is not None]
        return min(values) if values else None

    @property
    def riskiest_leg(self) -> LegPosition | None:
        candidates = [p for p in self.open_legs if p.distance_pct is not None]
        if not candidates:
            return None
        return min(candidates, key=lambda p: p.distance_pct or float("inf"))

    @property
    def total_unrealized(self) -> float:
        return sum(p.unrealized_pnl or 0.0 for p in self.open_legs)

    @property
    def status(self) -> str:
        """flat / danger / unhedged / suspect / ok"""
        if not self.open_legs:
            return "flat"
        if any(not p.liquidation_side_is_sane for p in self.open_legs):
            return "suspect"
        distance = self.min_distance_pct
        if distance is not None and distance <= self.warn_distance_pct:
            return "danger"
        if not self.is_hedged:
            return "unhedged"
        return "ok"

    @property
    def status_text(self) -> str:
        return {
            "flat": "无持仓",
            "ok": "对冲中",
            "danger": "逼近强平 —— 应双腿同平",
            "unhedged": "两腿不匹配 —— 存在裸露敞口",
            "suspect": "强平价方向异常 —— 数据可疑",
        }[self.status]


def parse_lighter_position(
    payload: dict[str, Any] | None, account_index: int, symbol: str
) -> LegPosition | None:
    """从 /api/v1/account 的返回里取出一条腿。

    数量的符号由 sign 字段给（sign<0 = 空），position 本身是绝对值。
    """
    if not isinstance(payload, dict):
        return None
    row = None
    for candidate in payload.get("accounts") or []:
        idx = candidate.get("account_index", candidate.get("index"))
        if idx is not None and int(idx) == int(account_index):
            row = candidate
            break
    if row is None:
        return None
    for position in row.get("positions") or []:
        if str(position.get("symbol") or "").upper() != symbol.upper():
            continue
        size = abs(_f(position.get("position")) or 0.0)
        sign = -1.0 if (_f(position.get("sign")) or 1.0) < 0 else 1.0
        # 标记价直接由 position_value ÷ |position| 反推 —— position_value 就是
        # 按标记价计的名义额。这样不必再去 orderBookDetails 里猜标记价叫什么字段，
        # 也就避开了 2026-09-18 那种「字段/口径猜错」的整类错误。
        value = _f(position.get("position_value"))
        mark = (value / size) if (value and size > 1e-12) else None
        return LegPosition(
            venue="lighter",
            symbol=symbol,
            size=size * sign,
            entry_price=_f(position.get("avg_entry_price")),
            mark_price=mark,
            liquidation_price=_f(position.get("liquidation_price")),
            unrealized_pnl=_f(position.get("unrealized_pnl")),
            margin=_f(position.get("allocated_margin")),
        )
    return LegPosition(
        venue="lighter", symbol=symbol, size=0.0, entry_price=None,
        mark_price=None, liquidation_price=None, unrealized_pnl=None, margin=None,
    )


def parse_arcus_position(
    payload: dict[str, Any] | None, market_id: int, *,
    mmf_by_market: dict[int, float] | None = None,
) -> LegPosition | None:
    """从 MarketClient.arcus_account() 的快照里取出一条腿。

    · size 带符号（文档：多为正、空为负）；实测也见过 size 为正、side=SHORT
      的写法，按 side 纠正一次。
    · Arcus 不返回强平价，用 arcus_liquidation_price 按维持保证金率算。
      算强平价要知道这个市场的 MMF —— 由调用方从 /v1/markets 传进来；
      没传就不算（宁可显示「—」，也不给一个猜出来的数）。
    """
    if not isinstance(payload, dict):
        return None
    rows = payload.get("_positions") or []
    mmfs = mmf_by_market or {}
    flat = LegPosition(
        venue="arcus", symbol=str(market_id), size=0.0, entry_price=None,
        mark_price=None, liquidation_price=None, unrealized_pnl=None, margin=None,
    )
    target = None
    for row in rows:
        try:
            if int(row.get("marketId")) == int(market_id):
                target = row
                break
        except (TypeError, ValueError):
            continue
    if target is None:
        return flat
    size = _signed_size(target)
    if abs(size) <= 1e-12:
        return flat
    entry = _f(target.get("averageEntryPrice"))
    mark = _f(target.get("markPx"))
    mark = mark if mark and mark > 0 else None
    margin = _f(target.get("marginUsed"))
    # 官方文档的示例里开仓价是放大过的整数（"50000000000"），实测是普通小数。
    # 万一哪天真返回了整数口径，拿它算出的强平价会离谱到「方向反了」，
    # 风控就会判数据可疑、双腿平掉、下轮再开 —— 白白循环付手续费。
    # 开仓价和标记价差 5 倍以上就不认这个开仓价：强平价宁可空着，也不用错的。
    if entry and mark and not (0.2 <= entry / mark <= 5.0):
        entry = None
    unrealized = (size * (mark - entry)) if (mark and entry) else _f(target.get("unrealizedPnl"))

    # 全仓时整个账户一起算：权益，以及其它仓位的名义额和维持保证金
    _, other_notional, other_mm = account_exposure(
        payload, mmfs, exclude_market=int(market_id))
    liquidation = arcus_liquidation_price(
        size=size, entry_price=entry, mark_price=mark,
        margin_mode=target.get("marginMode"), margin_used=margin,
        mmf=mmfs.get(int(market_id)),
        account_equity=_f(payload.get("equity")),
        other_maintenance=other_mm, other_notional=other_notional,
    )
    return LegPosition(
        venue="arcus",
        symbol=str(target.get("marketDisplayName") or market_id),
        size=size,
        entry_price=entry,
        mark_price=mark,
        liquidation_price=liquidation,
        unrealized_pnl=unrealized,
        margin=margin,
    )


def _signed_size(row: dict[str, Any]) -> float:
    size = _f(row.get("size")) or 0.0
    if str(row.get("side") or "").upper() == "SHORT" and size > 0:
        size = -size
    return size


def with_mark(position: LegPosition | None, mark: float | None) -> LegPosition | None:
    """把行情价补进持仓快照 —— 距离强平多远要靠它算。"""
    if position is None or mark is None:
        return position
    return LegPosition(
        venue=position.venue, symbol=position.symbol, size=position.size,
        entry_price=position.entry_price, mark_price=mark,
        liquidation_price=position.liquidation_price,
        unrealized_pnl=position.unrealized_pnl, margin=position.margin,
    )


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result
