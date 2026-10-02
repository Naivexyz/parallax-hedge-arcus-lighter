"""币种下拉框按价差排序。"""
import asyncio

import pytest

from parallax_hedge.books import Book, Level
from parallax_hedge.spreads import SpreadScanner, sort_key, spread_bps
from parallax_hedge.store import summarize_fills


class Client:
    async def common_markets(self):
        return [
            {"asset": "ETH", "lighter_market_index": 0, "lighter_symbol": "ETH", "arcus_symbol": "ETH-USD"},
            {"asset": "SPY", "lighter_market_index": 1, "lighter_symbol": "SPY", "arcus_symbol": "SPY-USD"},
            {"asset": "BAD", "lighter_market_index": 2, "lighter_symbol": "BAD", "arcus_symbol": "BAD-USD"},
        ]

    async def lighter_book(self, market_id, symbol, limit=100):
        if symbol == "BAD":
            raise RuntimeError("超时")
        # 2026-09-26 的实盘盘口：ETH 在 Lighter 宽，SPY 两边都窄
        bid, ask = {"ETH": (2687.52, 2687.82), "SPY": (771.36, 771.37)}[symbol]
        return Book("lighter", symbol, bids=[Level(bid, 1)], asks=[Level(ask, 1)])

    async def arcus_bbo(self, symbol):
        return {"ETH-USD": (2688.62, 2688.63), "SPY-USD": (770.38, 770.39)}.get(symbol, (1, 2))


def test_scan_measures_both_venues_and_skips_failures():
    s = SpreadScanner(Client())
    spreads = asyncio.get_event_loop().run_until_complete(s.scan_once())
    assert set(spreads) == {"ETH", "SPY"}                       # 取不到的不影响别的
    assert spreads["ETH"]["lighter_bps"] == pytest.approx(1.116, abs=0.01)
    assert spreads["SPY"]["total_bps"] == pytest.approx(0.26, abs=0.01)


def test_order_follows_the_mode():
    eth = {"lighter_bps": 1.1, "arcus_bps": 0.04, "total_bps": 1.14}
    qqq = {"lighter_bps": 0.13, "arcus_bps": 1.07, "total_bps": 1.2}
    assert sort_key(eth, maker=False) < sort_key(qqq, maker=False)    # 吃单：看两边之和
    assert sort_key(qqq, maker=True) < sort_key(eth, maker=True)      # 挂单：只看 Lighter
    assert sort_key(None, maker=True) == float("inf")                 # 没扫到的排最后
    assert spread_bps(None, 1) is None and spread_bps(2, 1) is None


def test_one_maker_open_made_of_several_orders_counts_once():
    rows = [
        {"asset": "ETH", "venue": "arcus", "action": "open", "quantity": 0.2, "price": 1,
         "cycle_id": 7, "status": "final", "fee": 0, "realized_pnl": 0},
        {"asset": "ETH", "venue": "arcus", "action": "open", "quantity": 0.3, "price": 1,
         "cycle_id": 7, "status": "final", "fee": 0, "realized_pnl": 0},
        {"asset": "ETH", "venue": "arcus", "action": "open", "quantity": 0.5, "price": 1,
         "cycle_id": 9, "status": "final", "fee": 0, "realized_pnl": 0},
    ]
    assert summarize_fills(rows)["by_asset"]["ETH"]["opens"] == 2
