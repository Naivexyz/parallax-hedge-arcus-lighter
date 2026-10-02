"""订单簿与可成交价 —— 纯逻辑。

2026-09-18 上一个版本的实盘第一课：给两条腿传了【同一个价】（对手所的标记价），
Lighter 的买单限价因此变成 arcus_mark × 1.001。两所之间的基差
（实测 SNDK 有 5.4 bps）加上 Lighter 自己的半价差，一旦超过滑点预算，
买单就永远够不到卖一 —— 表现为连续四次「报单成功、零成交」。

正确做法（照搬 Parallax）：每条腿的限价都来自【该所自己的盘口】，
而且是按【下单数量】走完档位的 VWAP，不是盘口第一档。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Book:
    venue: str
    symbol: str
    bids: list[Level]      # 由高到低
    asks: list[Level]      # 由低到高

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def side(self, side: str) -> list[Level]:
        """买单吃卖档，卖单吃买档。"""
        return self.asks if side == "buy" else self.bids


def aggregate_levels(rows: Any, *, descending: bool) -> list[Level]:
    """把交易所返回的档位归并成统一结构。

    两个所的格式不同：Lighter 是逐笔挂单 {price, remaining_base_amount}，
    Arcus 是 [价, 量] 数组（文档里也出现过 {price, size} 对象，两种都认）。
    同价位可能拆成多条，要合并。
    """
    combined: dict[float, float] = {}
    for row in rows or []:
        try:
            if isinstance(row, (list, tuple)):
                if len(row) < 2:
                    continue
                price, size = float(row[0] or 0), float(row[1] or 0)
            elif isinstance(row, dict):
                price = float(row.get("price", row.get("px", 0)) or 0)
                size = float(
                    row.get("remaining_base_amount", row.get("size", row.get("sz", 0))) or 0
                )
            else:
                continue
        except (TypeError, ValueError):
            continue
        if price > 0 and size > 0:
            combined[price] = combined.get(price, 0.0) + size
    return [
        Level(price=p, size=combined[p])
        for p in sorted(combined, reverse=descending)
    ]


def vwap(levels: list[Level], quantity: float) -> float | None:
    """走完档位吃下 quantity 的平均成交价。深度不够返回 None。

    深度不够时【必须返回 None 而不是退回第一档价】——
    退回第一档会让程序以为能成交，实际只成一部分或完全不成。
    """
    if quantity <= 0:
        return None
    remaining = quantity
    notional = 0.0
    for level in levels:
        take = min(remaining, level.size)
        notional += take * level.price
        remaining -= take
        if remaining <= 1e-12:
            return notional / quantity
    return None


def executable_prices(
    lighter: Book, arcus: Book, *, lighter_side: str, arcus_side: str,
    quantity: float,
) -> tuple[float | None, float | None]:
    """两条腿各自的可成交价。任一边深度不够就返回 None。"""
    return (
        vwap(lighter.side(lighter_side), quantity),
        vwap(arcus.side(arcus_side), quantity),
    )


def basis_bps(lighter: Book, arcus: Book) -> float | None:
    """两所中价的基差（bps）—— 诊断用：滑点预算必须盖得住它。"""
    if not (lighter.best_bid and lighter.best_ask and arcus.best_bid and arcus.best_ask):
        return None
    lighter_mid = (lighter.best_bid + lighter.best_ask) / 2
    arcus_mid = (arcus.best_bid + arcus.best_ask) / 2
    if arcus_mid <= 0:
        return None
    return (lighter_mid - arcus_mid) / arcus_mid * 10_000
