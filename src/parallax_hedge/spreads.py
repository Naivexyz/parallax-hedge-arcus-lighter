"""两所买卖价差扫描 —— 给「轮换任务」的币种下拉框排序用（价差窄的排前面）。

每个币种各取一次两边的买一卖一，算出各自的价差（bps，按中价）：
  · 吃单模式的一轮成本 ≈ Arcus 手续费 + 两边价差之和
  · 挂单模式里 Arcus 这边是挂单，真正要付的主要是 Lighter 的价差
所以同时给出两个数，下拉框按当前模式对应的那个排序。

不影响交易：扫描很慢（Lighter REST 每次要间隔约 1 秒），放在后台每 10 分钟扫一遍，
和下单共用同一个限流闸门，排在下单请求后面，不会挤占它们。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

SCAN_INTERVAL_SEC = 600


class SpreadScanner:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.spreads: dict[str, dict[str, Any]] = {}
        self.scanned_at: float | None = None
        self.scanning = False

    async def scan_once(self) -> dict[str, dict[str, Any]]:
        self.scanning = True
        try:
            markets = await self.client.common_markets()
            for market in markets:
                asset = market["asset"]
                try:
                    lighter, arcus = await asyncio.gather(
                        self.client.lighter_book(market["lighter_market_index"],
                                                 market["lighter_symbol"], limit=10),
                        self.client.arcus_bbo(market["arcus_symbol"]),
                    )
                except Exception:  # noqa: BLE001 —— 一个币取不到不影响其它币
                    continue
                lighter_bps = spread_bps(lighter.best_bid, lighter.best_ask)
                arcus_bps = spread_bps(*arcus)
                if lighter_bps is None or arcus_bps is None:
                    continue
                self.spreads[asset] = {
                    "lighter_bps": round(lighter_bps, 3),
                    "arcus_bps": round(arcus_bps, 3),
                    "total_bps": round(lighter_bps + arcus_bps, 3),
                    "at": time.time(),
                }
            self.scanned_at = time.time()
            return self.spreads
        finally:
            self.scanning = False


def spread_bps(bid: float | None, ask: float | None) -> float | None:
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 10_000


def sort_key(entry: dict[str, Any] | None, maker: bool) -> float:
    """挂单模式看 Lighter 价差，吃单模式看两边之和；没扫到的排最后。"""
    if not entry:
        return float("inf")
    return float(entry["lighter_bps"] if maker else entry["total_bps"])
