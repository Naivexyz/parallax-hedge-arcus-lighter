"""Arcus 市场列表的解析与两所配对（不联网：把 _request 换成替身）。"""
import asyncio
from pathlib import Path

import pytest

from parallax_hedge.books import aggregate_levels
from parallax_hedge.config import Settings, normalize_proxy
from parallax_hedge.exchanges import (
    ARCUS_FUNDING_INTERVAL_SEC,
    MarketClient,
    arcus_market_fields,
)


def arcus_row(base, mid, **kw):
    row = {
        "marketDisplayName": f"{base}-USD", "marketId": mid, "status": "ONLINE",
        "type": "PERPETUAL", "baseAsset": base, "quoteAsset": "USD",
        "tickSize": "0.01", "stepSize": "0.001", "minOrderSize": "0.01",
        "minOrderNotional": "5", "maxOrderSize": "10000",
        "markPrice": "100", "fundingRate": "0.0000125", "nextFundingRate": "0.00002",
        "initialMarginFraction": "0.1", "maintenanceMarginFraction": "0.05",
        "offHoursInitialMarginFraction": "0.15", "isOutsideRth": False,
        "category": "CRYPTO",
    }
    row.update(kw)
    return row


def lighter_row(symbol, mid, **kw):
    row = {"symbol": symbol, "market_id": mid, "status": "active", "market_type": "perp",
           "supported_size_decimals": 2, "supported_price_decimals": 3,
           "min_base_amount": "0.05", "min_quote_amount": "10"}
    row.update(kw)
    return row


def test_market_fields_take_the_current_session_margin():
    m = arcus_market_fields(arcus_row("SOL", 3))
    assert m["max_leverage"] == 10 and m["arcus_mmf"] == 0.05
    assert m["arcus_size_decimals"] == 3
    off = arcus_market_fields(arcus_row("NVDA", 21, isOutsideRth=True, category="EQUITIES"))
    assert off["max_leverage"] == 6            # 1 / 0.15，休市时段上限降低
    assert off["arcus_mmf"] == 0.05            # 维持保证金率不变
    assert off["arcus_off_hours"] is True


def test_market_fields_skip_what_cannot_be_traded():
    assert arcus_market_fields(arcus_row("X", 1, status="OFFLINE")) is None
    assert arcus_market_fields(arcus_row("X", 1, type="SPOT")) is None
    assert arcus_market_fields(arcus_row("X", 1, stepSize="0.5")) is None   # 非 10 的幂


class Client(MarketClient):
    def __init__(self, arcus_rows, lighter_rows):
        super().__init__(Settings(env_path=Path("."), data_dir=Path(".")))
        self.arcus_rows = arcus_rows
        self.lighter_rows = lighter_rows
        self.calls = []

    async def _request(self, venue, method, url, *, label, **kw):
        self.calls.append(label)
        if label == "/v1/markets":
            return {"markets": self.arcus_rows}
        if label == "/orderBooks":
            return {"order_books": self.lighter_rows}
        raise AssertionError(label)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_common_markets_match_by_base_asset_and_take_the_stricter_limits():
    client = Client(
        [arcus_row("SOL", 3), arcus_row("BTC", 1, stepSize="0.0001"), arcus_row("ONLYARC", 50)],
        [lighter_row("SOL", 2), lighter_row("BTC", 1, supported_size_decimals=5),
         lighter_row("ONLYLIG", 9), lighter_row("ETH/USDC", 99)],
    )
    markets = {m["asset"]: m for m in run(client.common_markets())}
    assert set(markets) == {"BTC", "SOL"}
    sol = markets["SOL"]
    assert sol["arcus_symbol"] == "SOL-USD" and sol["arcus_market_id"] == 3
    assert sol["lighter_market_index"] == 2
    assert sol["quantity_decimals"] == 2                 # min(Lighter 2, Arcus 3)
    assert sol["min_base_quantity"] == pytest.approx(0.05)
    assert sol["min_notional"] == pytest.approx(10)      # max(Arcus 5, Lighter 10)
    assert markets["BTC"]["quantity_decimals"] == 4      # min(Lighter 5, Arcus 4)


def test_inactive_lighter_markets_are_not_matched():
    client = Client([arcus_row("SOL", 3)], [lighter_row("SOL", 2, status="inactive")])
    with pytest.raises(Exception):
        run(client.common_markets())


def test_arcus_funding_is_hourly_and_prefers_the_prediction():
    client = Client([arcus_row("SOL", 3)], [])
    rows, marks = run(client.arcus_funding())
    r = rows["SOL-USD"]
    assert r.interval_sec == ARCUS_FUNDING_INTERVAL_SEC == 3600
    assert r.rate == pytest.approx(0.0000125) and r.effective_rate == pytest.approx(0.00002)
    assert r.bps_per_hour == pytest.approx(0.2)
    assert marks["SOL-USD"] == 100.0


def test_the_markets_endpoint_is_hit_once_per_round():
    """/v1/markets 一次 20 权重，共有币种和费率同一轮只打一次。"""
    client = Client([arcus_row("SOL", 3)], [lighter_row("SOL", 2)])

    async def both():
        await client.common_markets(force=True)
        await client.arcus_funding()
    run(both())
    assert client.calls.count("/v1/markets") == 1


def test_arcus_book_rows_can_be_pairs_or_objects():
    levels = aggregate_levels([["100.5", "2"], {"price": "100.5", "size": "1"}, ["99", "0"]],
                              descending=True)
    assert [(l.price, l.size) for l in levels] == [(100.5, 3.0)]


def test_proxy_shorthands_are_normalised():
    assert normalize_proxy("1.2.3.4:1080:user:p@ss") == "socks5://user:p%40ss@1.2.3.4:1080"
    assert normalize_proxy("1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert normalize_proxy("socks5://a:b@h:1") == "socks5://a:b@h:1"
    assert normalize_proxy("  ") == ""
    s = Settings(env_path=Path("."), data_dir=Path("."), arcus_proxy="1.2.3.4:1080:u:p",
                 global_proxy="http://9.9.9.9:1")
    assert s.proxy_for("arcus").startswith("socks5://")
    assert s.proxy_for("lighter") == "http://9.9.9.9:1"
    assert s.proxy_label("arcus") == "独立代理 1.*.*.4"
