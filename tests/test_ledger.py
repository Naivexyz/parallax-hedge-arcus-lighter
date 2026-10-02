"""成交账本：按订单号对账。

Lighter 的匹配逻辑照搬 Parallax 线上那套（按 tx_hash 找到订单号再收齐各档），
夹具里的回执格式取自 2026-09-19 的实盘数据库；
Arcus 按 /v1/fills 的 orderId（或 clientId）加总，字段照官方文档。
"""
import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from parallax_hedge import ledger as L
from parallax_hedge.execution import LegResult
from parallax_hedge.store import Store

ACCOUNT = 22370


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def lighter_raw(tx_hash):
    """和实盘一模一样的形状：dict 里 response 是 str(RespSendTx)，落库时再整体 str() 一次。"""
    return {
        "created": "<lighter.transactions.create_order.CreateOrder object at 0x000001DAE13EF950>",
        "response": ("code=200 message='{\"ratelimit\": \"didn\\'t use volume quota\"}' "
                     f"tx_hash='{tx_hash}' predicted_execution_time_ms=1789756504632 "
                     "volume_quota_remaining=None additional_properties={}"),
    }


H = [f"{i:x}" * 80 for i in range(1, 8)]        # 80 位十六进制，和实盘的 tx_hash 一样长
H = [h[:80] for h in H]


# ── Lighter 成交记录 → 单张订单 ──────────────────────────

def trade(tx, *, ask=None, bid=None, size="0.1", price="1590", maker_ask=False,
          taker_fee=None, maker_fee=None, ask_pnl="0", bid_pnl="0", ts=1789756505000,
          ask_id=None, bid_id=None, trade_id=None):
    row = {"tx_hash": tx, "ask_account_id": ask if ask is not None else 1,
           "bid_account_id": bid if bid is not None else 2, "size": size, "price": price,
           "is_maker_ask": maker_ask, "taker_fee": taker_fee, "maker_fee": maker_fee,
           "ask_account_pnl": ask_pnl, "bid_account_pnl": bid_pnl, "timestamp": ts}
    # 实盘成交记录里都有：双方的订单号（ask_id / bid_id）和成交号（trade_id）
    for key, value in (("ask_id", ask_id), ("bid_id", bid_id), ("trade_id", trade_id)):
        if value is not None:
            row[key] = value
    return row


def test_lighter_fills_of_one_order_are_summed_at_their_vwap():
    rows = [trade("0x" + H[0].upper(), bid=ACCOUNT, size="0.1", price="1590"),
            trade(H[0], bid=ACCOUNT, size="0.026", price="1591"),
            trade(H[1], bid=ACCOUNT, size="9", price="1"),           # 别的单
            trade(H[0], ask=999, bid=998, size="5", price="1")]      # 同一笔 tx 里别人的成交
    hit = L.match_lighter_trades(rows, H[0], ACCOUNT)
    assert hit.quantity == pytest.approx(0.126)
    assert hit.price == pytest.approx((0.1 * 1590 + 0.026 * 1591) / 0.126)
    assert hit.side == "buy" and hit.matched == 2


def test_lighter_fee_is_in_millionths_and_follows_the_maker_taker_role():
    # 我们是买方、卖方是 maker（is_maker_ask=True）→ 我们是 taker
    rows = [trade(H[0], bid=ACCOUNT, maker_ask=True, taker_fee=12_000, maker_fee=5)]
    assert L.match_lighter_trades(rows, H[0], ACCOUNT).fee == pytest.approx(0.012)
    # 我们是卖方且是 maker
    rows = [trade(H[0], ask=ACCOUNT, maker_ask=True, taker_fee=12_000, maker_fee=3_000)]
    assert L.match_lighter_trades(rows, H[0], ACCOUNT).fee == pytest.approx(0.003)


def test_lighter_fee_missing_stays_unknown_not_zero():
    rows = [trade(H[0], bid=ACCOUNT)]
    assert L.match_lighter_trades(rows, H[0], ACCOUNT).fee is None


def test_lighter_realized_pnl_comes_from_our_side_of_the_trade():
    rows = [trade(H[0], ask=ACCOUNT, ask_pnl="-0.35", bid_pnl="9")]
    hit = L.match_lighter_trades(rows, H[0], ACCOUNT)
    assert hit.realized_pnl == pytest.approx(-0.35) and hit.side == "sell"


def test_lighter_no_match_returns_none():
    assert L.match_lighter_trades([trade(H[1], bid=ACCOUNT)], H[0], ACCOUNT) is None
    assert L.match_lighter_trades([], H[0], ACCOUNT) is None
    assert L.match_lighter_trades([trade(H[0], bid=ACCOUNT)], None, ACCOUNT) is None


# ── Arcus /v1/fills → 单张订单 ─────────────────────────

def arcus_fill(order_id, size, price, *, side="SELL", fee="0.0451", closed="0.0",
               trade_id=None, client_id=None):
    return {"tradeId": trade_id or f"t{order_id}{size}{price}", "orderId": order_id,
            "clientId": client_id, "marketId": 26, "marketDisplayName": "OAI-USD",
            "side": side, "size": size, "price": price, "fee": fee,
            "closedPnl": closed, "role": "TAKER", "createdAt": 1790000000000000}


def test_arcus_fills_matched_by_order_id():
    rows = [arcus_fill("o-1", "0.1", "1584.4"), arcus_fill("o-1", "0.026", "1584.3"),
            arcus_fill("o-2", "5", "1")]
    hit = L.match_arcus_fills(rows, "o-1")
    assert hit.quantity == pytest.approx(0.126)
    assert hit.price == pytest.approx((0.1 * 1584.4 + 0.026 * 1584.3) / 0.126)
    assert hit.fee == pytest.approx(0.0902)
    assert hit.realized_pnl == pytest.approx(0.0902)   # closedPnl 0 + 手续费加回
    assert hit.side == "sell" and hit.matched == 2


def test_arcus_closed_pnl_is_kept_and_duplicates_are_counted_once():
    row = arcus_fill("o-7", "0.1", "100", side="BUY", fee="0.02", closed="1.25", trade_id="T1")
    hit = L.match_arcus_fills([row, dict(row)], "o-7")      # 同一笔成交出现两次
    assert hit.quantity == pytest.approx(0.1)
    assert hit.realized_pnl == pytest.approx(1.27) and hit.side == "buy"


def test_arcus_order_without_an_order_id_is_matched_by_client_id():
    rows = [arcus_fill("o-9", "0.2", "10", client_id="ph123"), arcus_fill("o-8", "1", "10")]
    hit = L.match_arcus_fills(rows, "cid:ph123")
    assert hit.quantity == pytest.approx(0.2)
    assert L.match_arcus_fills(rows, "cid:nobody") is None
    assert L.match_arcus_fills(rows, None) is None


# ── 执行结果 → 账本行 ────────────────────────────────────

def test_live_arcus_leg_is_booked_with_its_order_id_for_reconciliation():
    leg = LegResult("arcus", True, raw={"orderId": "o-1"}, filled=0.126, side="sell",
                    requested=0.126, price=1584.4, fill_confirmed=True, order_ref="o-1")
    row = L.fill_row_from_leg("OAI", "open", leg, dry_run=False, market_id=42)
    assert row["source"] == "position" and row["status"] == "pending"
    assert row["quantity"] == pytest.approx(0.126) and row["confirmed_qty"] == pytest.approx(0.126)
    assert row["order_ref"] == "o-1"
    assert row["market_id"] is None                  # 市场号只对 Lighter 有意义


def test_an_arcus_leg_with_an_unknown_outcome_is_still_booked():
    """请求超时的腿：没被当成「没发出去」，按 clientId 进账本等对账。"""
    leg = LegResult("arcus", False, submitted=False, uncertain=True, side="sell",
                    requested=0.1, price=10.0, order_ref="cid:ph1")
    row = L.fill_row_from_leg("OAI", "open", leg, dry_run=False, market_id=None)
    assert row is not None and row["order_ref"] == "cid:ph1" and row["status"] == "pending"


def test_unconfirmed_lighter_leg_is_booked_at_zero_until_the_exchange_says_otherwise():
    """抢救单这种没测成交量的腿，先记 0，交给交易所成交记录定。
    宁可暂时少算，也不能把可能没成交的单算进交易量。"""
    leg = LegResult("lighter", True, raw=lighter_raw(H[0]), side="sell", requested=0.125,
                    price=1590.0, order_ref=H[0])
    row = L.fill_row_from_leg("OAI", "rescue", leg, dry_run=False, market_id=42)
    assert row["quantity"] == 0.0 and row["status"] == "pending"
    assert row["market_id"] == 42 and row["order_ref"] == H[0]
    assert "未证实" in row["note"]


def test_leg_without_an_order_ref_is_finalised_as_an_estimate():
    leg = LegResult("lighter", True, raw={"weird": 1}, filled=0.1, fill_confirmed=True,
                    side="buy", requested=0.1, price=1.0)
    row = L.fill_row_from_leg("OAI", "open", leg, dry_run=False, market_id=42)
    assert row["status"] == "estimated" and row["order_ref"] is None


def test_unsubmitted_legs_are_not_booked_and_dry_run_is_simulated():
    assert L.fill_row_from_leg("OAI", "open", LegResult("arcus", False, submitted=False),
                               dry_run=False, market_id=None) is None
    leg = LegResult("lighter", True, filled=0.2, requested=0.2, price=100.0, side="buy",
                    fill_confirmed=True)
    row = L.fill_row_from_leg("OAI", "open", leg, dry_run=True, market_id=42)
    assert row["source"] == "simulated" and row["status"] == "final"
    assert row["quantity"] == pytest.approx(0.2) and row["order_ref"] is None


# ── 对账 ────────────────────────────────────────────────

STANDARD_ZERO_FEE = {"code": 200, "user_tier_name": "Standard",
                     "current_maker_fee_tick": 0, "current_taker_fee_tick": 0}


class FakeClient:
    def __init__(self, arcus_fills=None, pages=None, fail_arcus=False, fail_lighter=False,
                 limits=STANDARD_ZERO_FEE):
        self.arcus_rows = arcus_fills or []
        self.arcus_since = []
        self.pages = pages or [([], None)]
        self.fail_arcus = fail_arcus
        self.fail_lighter = fail_lighter
        self.limits = limits
        self.lighter_calls = []

    async def lighter_account_limits(self, token):
        if self.limits is None:
            raise RuntimeError("accountLimits 超时")
        return self.limits

    async def arcus_fills(self, since, market_id=None):
        if self.fail_arcus:
            raise RuntimeError("Arcus 超时")
        self.arcus_since.append(since)
        return self.arcus_rows

    async def lighter_trades(self, market_id, token, *, limit=100, cursor=None):
        if self.fail_lighter:
            raise RuntimeError("Lighter 429")
        self.lighter_calls.append((market_id, token, cursor))
        index = 0 if cursor is None else int(cursor)
        return self.pages[index]


class FakeExecutor:
    def __init__(self, fail=False):
        self.fail = fail

    async def lighter_auth_token(self):
        if self.fail:
            raise RuntimeError("签名器没加载")
        return "tok"


def book(store, venue, ref, *, created_at, action="open", quantity=0.0, market_id=42,
         confirmed=None):
    return store.record_fill(asset="OAI", venue=venue, action=action, side=None,
                             dry_run=False, market_id=market_id if venue == "lighter" else None,
                             order_ref=ref, requested_qty=0.126, quantity=quantity,
                             price=1590.0, source="position", status="pending",
                             created_at=created_at, confirmed_qty=confirmed)


def reconcile(store, client, executor=None, now=10_000.0):
    rec = L.FillReconciler(store, client, executor or FakeExecutor(), ACCOUNT,
                           clock=lambda: now)
    return run(rec.run_once())


def fills(store):
    return {r["order_ref"]: dict(r) for r in store.conn.execute("SELECT * FROM fills")}


def test_matched_rows_take_the_exchanges_numbers():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "arcus", "o-5491", created_at=9_000.0, quantity=0.126)
        book(store, "lighter", H[0], created_at=9_000.0)
        client = FakeClient(
            arcus_fills=[arcus_fill("o-5491", "0.126", "1584.4", fee="0.0540")],
            pages=[([trade(H[0], bid=ACCOUNT, size="0.126", price="1590.6", taker_fee=0)], None)],
        )
        summary = reconcile(store, client)
        assert summary["matched"] == 2 and not summary["errors"]
        rows = fills(store)
        assert rows["o-5491"]["fee"] == pytest.approx(0.054)
        assert rows["o-5491"]["status"] == "final"
        assert rows["o-5491"]["price"] == pytest.approx(1584.4)
        # 成交按时间查：从最早那张单往前再留 5 分钟
        assert client.arcus_since == [9_000.0 - L.ARCUS_FILLS_MARGIN_SEC]
        assert rows[H[0]]["quantity"] == pytest.approx(0.126)   # 原来记的是 0
        assert rows[H[0]]["price"] == pytest.approx(1590.6)     # 原来是参考价 1590
        assert rows[H[0]]["side"] == "buy" and rows[H[0]]["source"] == "exchange"


def test_rows_are_only_checked_after_a_grace_period():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "arcus", "1", created_at=10_000.0 - 5)
        summary = reconcile(store, FakeClient())
        assert summary["checked"] == 0


def test_unmatched_rows_are_given_up_only_when_the_exchange_answered():
    """交易所数据拿到了、却找不到这张单 → 多半是 IOC 零成交，按估计值定稿。
    交易所数据本身没拿到 → 不能下任何结论，继续等。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "arcus", "1", created_at=10.0)
        book(store, "arcus", "2", created_at=9_990.0 - L.MATCH_GRACE_SEC)
        reconcile(store, FakeClient(arcus_fills=[]), now=10_000.0)
        rows = fills(store)
        assert rows["1"]["status"] == "estimated"          # 老的：定稿
        assert rows["2"]["status"] == "pending"            # 新的：再等等

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "arcus", "1", created_at=10.0)
        summary = reconcile(store, FakeClient(fail_arcus=True), now=10_000.0)
        assert fills(store)["1"]["status"] == "pending"    # 接口挂了不定稿
        assert summary["errors"] and "Arcus" in summary["errors"][0]


def test_lighter_rows_wait_when_the_auth_token_cannot_be_made():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "lighter", H[0], created_at=10.0)
        summary = reconcile(store, FakeClient(), executor=FakeExecutor(fail=True))
        assert fills(store)[H[0]]["status"] == "pending"
        assert any("令牌" in e for e in summary["errors"])


# ── v0.2.1：Lighter 对账只对上一部分成交的问题 ────────────
#
# 实盘数据：SNDK 一张 Lighter 空单，仓位实测 0.2776（Arcus 也按 0.2776 对冲上了），
# v0.2.0 的对账却只对上 0.015 就定了稿。一天下来 Lighter 交易量只算出
# $17K，而 Arcus 是 $40K —— 两条腿同样大小，这本身就说明对账不全。

T0 = 1_789_829_000.0          # 真实量级的时间（秒），Lighter 成交时间戳是毫秒


def ms(offset):
    return int((T0 + offset) * 1000)


def test_an_orders_trades_split_across_pages_are_all_collected():
    """一张单扫了好几档、成交跨在两页之间：v0.2.0 看到第一页有它就停了，后半截丢了。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "lighter", H[0], created_at=T0, quantity=0.2776, confirmed=0.2776)
        pages = [
            ([trade(H[1], bid=ACCOUNT, ts=ms(500)),
              trade(H[0], bid=ACCOUNT, size="0.015", ts=ms(1))], "1"),
            ([trade(H[0], bid=ACCOUNT, size="0.2626", ts=ms(1)),
              trade(H[2], bid=ACCOUNT, ts=ms(-1000))], "2"),
            ([trade(H[3], bid=ACCOUNT, ts=ms(-2000))], None),
        ]
        client = FakeClient(pages=pages)
        reconcile(store, client, now=T0 + 1000)
        row = fills(store)[H[0]]
        assert row["status"] == "final"
        assert row["quantity"] == pytest.approx(0.2776)
        # 第 2 页的最早一笔已经早于这张单减去 5 分钟余量 → 不必翻第 3 页
        assert [c[2] for c in client.lighter_calls] == [None, "1"]


def test_paging_goes_on_until_it_is_past_the_oldest_pending_order():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "lighter", H[0], created_at=T0)
        pages = [([trade(H[1], bid=ACCOUNT, ts=ms(4000))], "1"),
                 ([trade(H[2], bid=ACCOUNT, ts=ms(1000))], "2"),
                 ([trade(H[0], bid=ACCOUNT, ts=ms(1))], "3"),
                 ([trade(H[3], bid=ACCOUNT, ts=ms(-1000))], "4"),
                 ([trade(H[4], bid=ACCOUNT, ts=ms(-2000))], None)]
        client = FakeClient(pages=pages)
        reconcile(store, client, now=T0 + 5000)
        assert fills(store)[H[0]]["status"] == "final"
        assert len(client.lighter_calls) == 4          # 第 4 页已经越过这张单的时间，停


def test_a_partial_match_is_not_settled_while_the_record_may_still_be_filling_in():
    """仓位实测 0.2776，交易所记录暂时只有 0.015：不定稿，下一轮再对。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "lighter", H[0], created_at=T0, quantity=0.2776, confirmed=0.2776)
        partial = [([trade(H[0], bid=ACCOUNT, size="0.015", ts=ms(1))], None)]
        summary = reconcile(store, FakeClient(pages=partial), now=T0 + 500)
        row = fills(store)[H[0]]
        assert row["status"] == "pending" and summary["waiting"] == 1
        assert row["quantity"] == pytest.approx(0.2776)          # 不被残缺数据改小
        full = [([trade(H[0], bid=ACCOUNT, size="0.015", ts=ms(1)),
                  trade(H[0], bid=ACCOUNT, size="0.2626", ts=ms(1))], None)]
        reconcile(store, FakeClient(pages=full), now=T0 + 600)
        row = fills(store)[H[0]]
        assert row["status"] == "final" and row["quantity"] == pytest.approx(0.2776)


def test_a_match_that_never_completes_keeps_the_confirmed_quantity():
    """一直对不全：数量按仓位实测，价格用对上那部分的均价，手续费和盈亏不瞎算。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "lighter", H[0], created_at=10.0, quantity=0.2776, confirmed=0.2776)
        partial = [([trade(H[0], ask=ACCOUNT, size="0.015", price="1780.21",
                           ask_pnl="0.5", ts=11 * 1000)], None)]
        reconcile(store, FakeClient(pages=partial), now=10_000.0)
        row = fills(store)[H[0]]
        assert row["status"] == "estimated"
        assert row["quantity"] == pytest.approx(0.2776)
        assert row["price"] == pytest.approx(1780.21)
        # 0 费率账户：手续费就是 0；开仓成交没有已实现盈亏 —— 不再是「未知」（v1.3.1）
        assert row["fee"] == 0.0 and row["realized_pnl"] == 0.0
        assert "0.015" in row["note"]


def test_an_incomplete_lighter_close_keeps_its_pnl_unknown():
    """平仓类成交对不全：手续费照样补 0（0 费率账户），但已实现盈亏不瞎填。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "lighter", H[0], created_at=10.0, action="close",
             quantity=0.2776, confirmed=0.2776)
        partial = [([trade(H[0], ask=ACCOUNT, size="0.015", price="1780.21",
                           ask_pnl="0.5", ts=11 * 1000)], None)]
        reconcile(store, FakeClient(pages=partial), now=10_000.0)
        row = fills(store)[H[0]]
        assert row["status"] == "estimated"
        assert row["fee"] == 0.0 and row["realized_pnl"] is None


def test_a_standard_zero_fee_account_books_missing_fees_as_zero():
    """0 费率账户的成交记录不带手续费字段。不确认的话手续费永远是「未知」，
    净损益和磨损也就永远算不出来 —— v0.2.0 上线后 105 笔 Lighter 成交全是这样。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "lighter", H[0], created_at=9_000.0)
        pages = [([trade(H[0], bid=ACCOUNT, size="0.126", ts=9_001 * 1000)], None)]
        reconcile(store, FakeClient(pages=pages))
        assert fills(store)[H[0]]["fee"] == 0.0


def test_a_paid_fee_tier_leaves_missing_fees_unknown():
    for limits in ({"code": 200, "user_tier_name": "Premium",
                    "current_maker_fee_tick": 0, "current_taker_fee_tick": 0},
                   {"code": 200, "user_tier_name": "Standard",
                    "current_maker_fee_tick": 0, "current_taker_fee_tick": 20}):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            book(store, "lighter", H[0], created_at=9_000.0)
            pages = [([trade(H[0], bid=ACCOUNT, size="0.126", ts=9_001 * 1000)], None)]
            reconcile(store, FakeClient(pages=pages, limits=limits))
            row = fills(store)[H[0]]
            assert row["status"] == "final" and row["fee"] is None, limits


def test_an_unknown_fee_tier_does_not_finalise_the_row_yet():
    """账户费率这次没查到：先别定稿 —— 定了稿，这笔的手续费就永远是「未知」。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "lighter", H[0], created_at=9_000.0)
        pages = [([trade(H[0], bid=ACCOUNT, size="0.126", ts=9_001 * 1000)], None)]
        summary = reconcile(store, FakeClient(pages=pages, limits=None))
        assert fills(store)[H[0]]["status"] == "pending"
        assert any("费率" in e for e in summary["errors"])
        reconcile(store, FakeClient(pages=pages))           # 下一轮查到了 0 费率
        row = fills(store)[H[0]]
        assert row["status"] == "final" and row["fee"] == 0.0


def test_rows_younger_than_two_minutes_are_not_checked_yet():
    """交易所的成交记录要一点时间才落全。v0.2.0 只等 15 秒，对上的往往是半截。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "lighter", H[0], created_at=10_000.0 - 30)
        summary = reconcile(store, FakeClient(pages=[([trade(H[0], bid=ACCOUNT)], None)]))
        assert summary["checked"] == 0
        assert fills(store)[H[0]]["status"] == "pending"


# ── v0.2.2：一张单吃了好几档，只有第一笔带着我们的 tx_hash ─────────
#
# 实盘 62 张 Lighter 单都是这个形状：对上的量恰好是盘口第一档
# （SNDK 0.2776 只对上 0.015，价格正好是当时的买一 1780.21），过了几个小时、
# 翻完所有页也还是对不全 —— 不是记录没落全，而是后面几档的成交带的是
# Lighter 内部撮合交易的 hash。同一张单的每一笔成交，我们这一边的订单号都一样。

ORDER = 281474977071865
INTERNAL = ["a1" * 40, "b2" * 40]


def sweep(tx, *, order=ORDER, ts=None):
    """一张卖单吃了三档：第一笔带下单的 tx_hash，后两笔带内部撮合的 hash。"""
    ts = ms(1) if ts is None else ts
    return [
        trade(tx, ask=ACCOUNT, ask_id=order, bid_id=11, size="0.015", price="1780.21",
              ask_pnl="0.1", trade_id=1, ts=ts),
        trade(INTERNAL[0], ask=ACCOUNT, ask_id=order, bid_id=12, size="0.1",
              price="1780.2", ask_pnl="0.2", trade_id=2, ts=ts),
        trade(INTERNAL[1], ask=ACCOUNT, ask_id=order, bid_id=13, size="0.1626",
              price="1780.19", ask_pnl="0.3", trade_id=3, ts=ts),
    ]


def test_every_level_of_a_sweeping_order_is_collected_by_its_order_id():
    rows = sweep(H[0]) + [
        trade(H[1], ask=ACCOUNT, ask_id=ORDER + 7, size="5", trade_id=4),   # 我们的另一张单
        trade(INTERNAL[0], ask=999, bid=998, ask_id=55, bid_id=66, size="9",
              trade_id=5),                                               # 同一笔内部撮合里别人的成交
    ]
    hit = L.match_lighter_trades(rows, H[0], ACCOUNT)
    assert hit.quantity == pytest.approx(0.2776)
    assert hit.matched == 3 and hit.side == "sell"
    assert hit.price == pytest.approx((0.015 * 1780.21 + 0.1 * 1780.2 + 0.1626 * 1780.19) / 0.2776)
    assert hit.realized_pnl == pytest.approx(0.6)


def test_order_ids_given_as_strings_link_the_same_way():
    rows = [dict(r) for r in sweep(H[0])]
    for r in rows:
        r["ask_id_str"] = str(r.pop("ask_id"))
    assert L.match_lighter_trades(rows, H[0], ACCOUNT).quantity == pytest.approx(0.2776)


def test_a_trade_seen_on_two_pages_is_counted_once():
    rows = sweep(H[0]) + sweep(H[0])[1:]          # 翻页时后两笔又出现了一次
    hit = L.match_lighter_trades(rows, H[0], ACCOUNT)
    assert hit.quantity == pytest.approx(0.2776) and hit.matched == 3


def test_a_missing_or_zero_order_id_never_links_other_trades():
    rows = [trade(H[0], ask=ACCOUNT, ask_id=0, size="0.015", trade_id=1),
            trade(INTERNAL[0], ask=ACCOUNT, ask_id=0, size="5", trade_id=2),
            trade(INTERNAL[1], ask=ACCOUNT, size="7", trade_id=3)]
    assert L.match_lighter_trades(rows, H[0], ACCOUNT).quantity == pytest.approx(0.015)


def test_the_counterpartys_order_id_is_not_ours():
    """只认【我们这一边】的订单号：这笔里我们是买方（bid_id=77），
    对手卖单的 ask_id 碰巧等于我们那张卖单的号，也不能算进来。"""
    rows = sweep(H[0]) + [trade(INTERNAL[0], bid=ACCOUNT, bid_id=77, ask_id=ORDER,
                                size="3", trade_id=9)]
    assert L.match_lighter_trades(rows, H[0], ACCOUNT).quantity == pytest.approx(0.2776)


def test_the_reconciler_settles_a_sweeping_order_in_full():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "lighter", H[0], created_at=T0, quantity=0.2776, confirmed=0.2776)
        reconcile(store, FakeClient(pages=[(sweep(H[0]), None)]), now=T0 + 500)
        row = fills(store)[H[0]]
        assert row["status"] == "final" and row["source"] == "exchange"
        assert row["quantity"] == pytest.approx(0.2776)
        assert row["fee"] == 0.0                              # 标准账户 0 费率
        assert row["realized_pnl"] == pytest.approx(0.6)


def test_a_match_bigger_than_the_order_is_not_trusted():
    """IOC 单不可能成交超过下单量；对上的比下单量还多，只能是对错了单。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        book(store, "lighter", H[0], created_at=T0, quantity=0.126, confirmed=0.126)
        summary = reconcile(store, FakeClient(pages=[(sweep(H[0]), None)]), now=T0 + 500)
        row = fills(store)[H[0]]
        assert row["status"] == "estimated" and summary["gave_up"] == 1
        assert row["quantity"] == pytest.approx(0.126)
        assert row["fee"] == 0.0 and "超过下单量" in row["note"]     # 0 费率账户


def test_arcus_closed_pnl_already_contains_the_fee():
    """实盘数据（2026-09-26）：ETH 空 0.7438，2688.62 开、2690.55 平，平仓 closedPnl −1.8858，
    手续费 0.4503。价差损益应是 −1.4355，不能再把手续费扣一遍。"""
    row = arcus_fill("o-c", "0.7438", "2690.55", side="BUY", fee="0.450276995",
                     closed="-1.885810995")
    hit = L.match_arcus_fills([row], "o-c")
    assert hit.realized_pnl == pytest.approx((2688.62 - 2690.55) * 0.7438, abs=1e-4)


def test_repair_adds_the_fee_back_once():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "t.db")
        store.record_fill(asset="ETH", venue="arcus", action="close", side="buy", dry_run=False,
                          market_id=None, order_ref="o1", requested_qty=1, quantity=1,
                          price=10.0, source="exchange", status="final", fee=0.45,
                          realized_pnl=-1.8858)
        assert L.repair_arcus_double_counted_fees(store) == 1
        assert L.repair_arcus_double_counted_fees(store) == 0
        row = store.conn.execute("SELECT realized_pnl FROM fills").fetchone()
        assert row[0] == pytest.approx(-1.4358)
