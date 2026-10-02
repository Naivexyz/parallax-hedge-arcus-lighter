"""首页余额的口径。数字要和交易所页面上看到的对得上。"""
import pytest

from parallax_hedge.accounts import combine_balances, arcus_balance, lighter_balance


def lighter_payload():
    # 字段取自实盘账户接口；OPENAI 那条仓位 2026-09-19 截图上是 0.1260
    return {"accounts": [{
        "account_index": 22370,
        "total_asset_value": "456.95", "collateral": "457.00", "available_balance": "356.78",
        "positions": [
            {"symbol": "OPENAI", "position": "0.1260", "sign": 1, "unrealized_pnl": "-0.05"},
            {"symbol": "SNDK", "position": "0", "sign": 1, "unrealized_pnl": "0"},
        ],
    }]}


def test_lighter_equity_available_and_unrealized():
    b = lighter_balance(lighter_payload(), 22370)
    assert b["equity"] == pytest.approx(456.95)
    assert b["available"] == pytest.approx(356.78)
    assert b["unrealized_pnl"] == pytest.approx(-0.05)
    assert b["open_positions"] == 1                      # 数量 0 的空位不算持仓


def test_lighter_falls_back_to_collateral_and_rejects_other_accounts():
    payload = lighter_payload()
    del payload["accounts"][0]["total_asset_value"]
    assert lighter_balance(payload, 22370)["equity"] == pytest.approx(457.0)
    assert lighter_balance(payload, 1) is None           # 不是自己的账户
    assert lighter_balance(None, 22370) is None
    assert lighter_balance(payload, None) is None


def arcus_payload():
    """/v1/account + /v1/positions 合并后的快照（MarketClient.arcus_account 的输出形状）。"""
    return {
        "equity": "412.50", "freeCollateral": "300.25", "netQuoteBalance": "520.0",
        "_positions": [
            {"marketId": 26, "marketDisplayName": "SNDK-USD", "side": "SHORT",
             "size": "-0.5", "averageEntryPrice": "200", "markPx": "198",
             "marginMode": "ISOLATED", "marginUsed": "20"},
            {"marketId": 1, "marketDisplayName": "BTC-USD", "side": "LONG",
             "size": "0", "averageEntryPrice": "0", "markPx": "0"},
        ],
    }


def test_arcus_equity_is_equity_and_available_is_free_collateral():
    b = arcus_balance(arcus_payload())
    assert b["equity"] == pytest.approx(412.50)
    assert b["available"] == pytest.approx(300.25)
    assert b["open_positions"] == 1                      # 数量 0 的不算持仓
    # 空 0.5，200 开、现价 198：赚 1
    assert b["unrealized_pnl"] == pytest.approx(1.0)
    assert b["mode"] == "ISOLATED"


def test_arcus_short_written_as_positive_size_with_side_short():
    payload = arcus_payload()
    payload["_positions"][0]["size"] = "0.5"            # 实测见过的写法
    assert arcus_balance(payload)["unrealized_pnl"] == pytest.approx(1.0)


def test_arcus_empty_account_404_reads_as_zero():
    b = arcus_balance({"equity": "0", "freeCollateral": "0", "_positions": [], "_empty": True})
    assert b["equity"] == 0 and b["available"] == 0 and b["open_positions"] == 0


def test_available_is_never_negative():
    payload = arcus_payload()
    payload["freeCollateral"] = "-5"
    assert arcus_balance(payload)["available"] == 0.0


def test_no_total_when_either_side_is_missing():
    lighter = lighter_balance(lighter_payload(), 22370)
    arcus = arcus_balance(arcus_payload())
    total = combine_balances(lighter, arcus)
    assert total["equity"] == pytest.approx(456.95 + 412.50)
    assert total["available"] == pytest.approx(356.78 + 300.25)
    assert combine_balances(lighter, None) is None
    assert combine_balances(None, arcus) is None
