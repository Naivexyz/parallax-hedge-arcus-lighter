"""挂单模式：Arcus 挂 ALO、成交多少对冲多少。

替身按【真实因果】模拟：Arcus 仓位只在挂单被打中时变，Lighter 仓位只在对冲单成交时变。
时钟和 sleep 都是假的，45 秒的等待瞬间跑完。
"""
import asyncio
from pathlib import Path

import pytest

from parallax_hedge import arcus as ax
from parallax_hedge import execution as execmod
from parallax_hedge.books import Book, Level
from parallax_hedge.config import Settings
from parallax_hedge.execution import Executor, LegResult
from parallax_hedge.maker import MakerExecutor

execmod._CONFIRM_DELAY_SEC = 0.0
execmod._CONFIRM_RETRIES = 2

MARKET = {
    "asset": "ETH", "lighter_symbol": "ETH", "lighter_market_index": 0,
    "arcus_symbol": "ETH-USD", "arcus_market_id": 2, "quantity_decimals": 4,
    "arcus_step_size": "0.0001", "arcus_tick_size": "0.01", "arcus_tick_tiers": [],
    "arcus_max_order_size": 1000, "arcus_min_order_size": 0.001, "arcus_min_notional": 5,
    "lighter_min_base": 0.005, "lighter_min_quote": 10, "lighter_size_decimals": 4,
    "min_base_quantity": 0.005, "max_leverage": 20, "arcus_mmf": 0.0267,
}


class World:
    def __init__(self, *, bbo=(2686.00, 2686.10), fills=None, cancel_fill=0.0,
                 lighter_ok=True, reject_first=None, stale=None, bbo_script=None,
                 lighter_bbo=(2685.90, 2686.00), lighter_bbo_script=None):
        self.arcus = 0.0
        self.lighter = 0.0
        self.bbo = bbo
        self.bbo_script = list(bbo_script or [])
        # Lighter 对冲 IOC 会吃到的价：买单打卖一。默认卖一低于 Arcus 卖单挂价，价差 < 1 bp。
        self.lighter_bbo = lighter_bbo
        self.lighter_bbo_script = list(lighter_bbo_script or [])
        self.fills = dict(fills or {})        # 第几次读 Arcus 仓位时，活着的挂单成交多少
        self.cancel_fill = cancel_fill        # 撤单那一刻恰好成交的量
        self.lighter_ok = lighter_ok
        self.reject_first = reject_first      # 第一张挂单被拒的原因
        self.stale = list(stale or [])
        self.polls = 0
        self.orders = {}
        self.placed = []
        self.cancels = []
        self.lighter_orders = []
        self.lighter_times = []
        self.fill_times = []
        self.lighter_misses = 0              # 前几笔 Lighter 对冲单落空（IOC 没成交）
        self.now = 0.0

    def live_order(self):
        for oid, o in self.orders.items():
            if o["live"]:
                return oid, o
        return None, None

    def fill(self, amount):
        oid, o = self.live_order()
        if not o or amount <= 0:
            return
        take = min(amount, o["qty"] - o["filled"])
        o["filled"] += take
        self.arcus += take if o["side"] == "buy" else -take
        if o["filled"] >= o["qty"] - 1e-12:
            o["live"] = False


class FakeClient:
    def __init__(self, world):
        self.w = world

    async def arcus_bbo(self, symbol):
        if self.w.bbo_script:
            self.w.bbo = self.w.bbo_script.pop(0)
        return self.w.bbo

    async def arcus_open_orders(self, market_id):
        return self.w.stale

    async def lighter_account(self):
        size = self.w.lighter
        return {"accounts": [{"account_index": 1, "positions": [
            {"symbol": "ETH", "sign": -1 if size < 0 else 1, "position": str(abs(size)),
             "position_value": str(abs(size) * 2686)}]}]}

    async def lighter_book(self, market_id, symbol, limit=100):
        if self.w.lighter_bbo_script:
            self.w.lighter_bbo = self.w.lighter_bbo_script.pop(0)
        bid, ask = self.w.lighter_bbo
        return Book("lighter", symbol, bids=[Level(bid, 10)], asks=[Level(ask, 10)])


class MakerTestExecutor(Executor):
    def __init__(self, world):
        settings = Settings(env_path=Path("."), data_dir=Path("."), lighter_account_index=1,
                            arcus_address="0x" + "ab" * 20, arcus_api_private_key="11" * 32)
        super().__init__(settings, FakeClient(world), dry_run=False)
        self.w = world

    async def read_arcus_size(self, market_id):
        self.w.polls += 1
        amount = self.w.fills.pop(self.w.polls, 0.0)
        if amount:
            self.w.fill(amount)
            self.w.fill_times.append(self.w.now)
        return self.w.arcus

    async def read_positions(self, lighter_symbol, arcus_market_id):
        return self.w.lighter, self.w.arcus

    async def _arcus_place(self, market, side, quantity, price, reduce_only=False,
                           time_in_force="IOC"):
        # 真签一遍，确保挂单请求本身能构造出来
        order = self.build_arcus_order(market, side, quantity, price, reduce_only, time_in_force)
        self.w.placed.append({"side": side, "qty": quantity, "price": price,
                              "tif": time_in_force, "reduce_only": reduce_only,
                              "body": order["body"]})
        if time_in_force == "IOC":          # 抢救 / 减零头：吃单立刻成交
            self.w.arcus += quantity if side == "buy" else -quantity
            return LegResult("arcus", True, raw={"orderId": f"ioc{len(self.w.placed)}"},
                             side=side, requested=quantity, order_ref=f"ioc{len(self.w.placed)}")
        if self.w.reject_first:
            reason, self.w.reject_first = self.w.reject_first, None
            return LegResult("arcus", False, submitted=False, error=f"Arcus 拒单：{reason}")
        oid = f"o{len(self.w.placed)}"
        self.w.orders[oid] = {"side": side, "qty": quantity, "price": price,
                              "filled": 0.0, "live": True}
        return LegResult("arcus", True, raw={"orderId": oid, "clientId": "ph1"},
                         side=side, requested=quantity, order_ref=oid)

    async def _arcus_cancel(self, market, order_id):
        self.w.cancels.append(order_id)
        self.w.fill(self.w.cancel_fill)            # 撤单那一刻刚好被打中
        self.w.cancel_fill = 0.0
        if order_id in self.w.orders:
            self.w.orders[order_id]["live"] = False
        self.w.stale = [o for o in self.w.stale if o.get("orderId") != order_id]
        return True, "CANCELED"

    async def _lighter_ioc(self, market_id, side, quantity, price, decimals, reduce_only=False):
        self.w.lighter_orders.append((side, quantity, reduce_only, price))
        self.w.lighter_times.append(self.w.now)
        if self.w.lighter_misses:
            self.w.lighter_misses -= 1
        elif self.w.lighter_ok:
            self.w.lighter += quantity if side == "buy" else -quantity
        return LegResult("lighter", True, raw={"response": "code=200 tx_hash='ab'"},
                         side=side, requested=quantity)


def maker(world, wait=45.0):
    ex = MakerTestExecutor(world)

    async def fake_sleep(seconds):
        world.now += seconds
        await asyncio.sleep(0)              # 让旁路的对冲确认任务有机会跑
    return ex, MakerExecutor(ex, wait_seconds=wait, clock=lambda: world.now, sleep=fake_sleep)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def open_(world, quantity=0.5, lighter_decimals=(4, 2), **kw):
    ex, mk = maker(world, **kw)
    r = run(mk.open_pair(market=MARKET, direction="long_lighter_short_arcus", quantity=quantity,
                         lighter_price=2686.0, arcus_price=2686.05, slippage_bps=10.0,
                         lighter_decimals=lighter_decimals))
    return ex, r


# ── 正常成交 ────────────────────────────────────────────

def test_a_full_fill_is_hedged_and_the_pair_is_open():
    w = World(fills={2: 0.5})
    ex, r = open_(w)
    assert r.ok and r.stage == "opened"
    assert w.placed[0]["tif"] == "ALO" and w.placed[0]["side"] == "sell"
    assert w.lighter_orders[0][:3] == ("buy", 0.5, False)
    assert w.arcus == pytest.approx(-0.5) and w.lighter == pytest.approx(0.5)
    assert r.quotes["filled_quantity"] == pytest.approx(0.5)


def test_the_resting_price_never_crosses_the_book():
    """卖单挂在卖一下面一格（盘口有空档时），但必须高于买一 —— 否则就是吃单。"""
    w = World(bbo=(2686.00, 2686.10), fills={2: 0.5})
    open_(w)
    assert w.placed[0]["price"] == pytest.approx(2686.09)
    assert ax.passive_price(MARKET, "sell", 2686.00, 2686.01) == ax.D("2686.01")   # 一格价差：跟卖一
    assert ax.passive_price(MARKET, "buy", 2686.00, 2686.10) == ax.D("2686.01")
    assert ax.passive_price(MARKET, "buy", 2686.00, 2685.00) is None               # 交叉盘口：不挂


def test_partial_fills_are_hedged_piece_by_piece():
    w = World(fills={2: 0.2, 4: 0.3})
    ex, r = open_(w)
    assert r.ok
    assert [o[1] for o in w.lighter_orders] == [pytest.approx(0.2), pytest.approx(0.3)]
    assert w.lighter == pytest.approx(0.5) and w.arcus == pytest.approx(-0.5)


def test_arcus_legs_are_booked_at_the_resting_price_with_fee_free_refs():
    w = World(fills={2: 0.5})
    ex, r = open_(w)
    arcus_legs = [leg for a, leg in r.ledger if leg.venue == "arcus"]
    assert arcus_legs[0].filled == pytest.approx(0.5)
    assert arcus_legs[0].price == pytest.approx(2686.09) and not arcus_legs[0].price_is_estimate
    assert arcus_legs[0].order_ref == "o1"


# ── 没成交 / 部分成交后超时 ─────────────────────────────

def test_no_fill_before_the_deadline_cancels_and_costs_nothing():
    w = World(fills={})
    ex, r = open_(w, wait=10)
    assert r.ok is False and r.stage == "maker_not_filled"
    assert w.cancels and w.lighter_orders == [] and w.arcus == 0.0


def test_a_fill_that_lands_during_the_cancel_is_still_hedged():
    """撤单的那一刻被打中：撤完必须重读仓位，否则就是一条没人管的裸腿。"""
    w = World(fills={}, cancel_fill=0.2)
    ex, r = open_(w, wait=10)
    assert r.ok and w.lighter == pytest.approx(0.2) and w.arcus == pytest.approx(-0.2)
    assert r.quotes["filled_quantity"] == pytest.approx(0.2)


def test_a_residual_too_small_for_lighter_is_trimmed_on_arcus():
    """Lighter 只收 2 位小数：Arcus 成交 0.203 → 对冲 0.20，多出的 0.003 在 Arcus 吃单减掉。"""
    w = World(fills={2: 0.203})
    ex, r = open_(w, wait=10, lighter_decimals=(2, 2))
    assert r.ok
    assert w.lighter == pytest.approx(0.20)
    trim = [p for p in w.placed if p["tif"] == "IOC"]
    assert trim and trim[0]["side"] == "buy" and trim[0]["qty"] == pytest.approx(0.003)
    assert w.arcus == pytest.approx(-0.20)              # 两腿对齐
    assert any("零头" in n for n in r.notes)


# ── 对冲失败：不留裸腿 ─────────────────────────────────

def test_a_failed_hedge_flattens_the_unhedged_arcus_fill():
    w = World(fills={2: 0.5}, lighter_ok=False)
    ex, r = open_(w)
    assert r.ok is False and r.stage == "maker_hedge_failed"
    assert len(w.lighter_orders) >= 2                     # 补过单
    assert w.lighter_orders[1][3] > w.lighter_orders[0][3]   # 补单限价更宽（买单更高）
    rescue = [p for p in w.placed if p["tif"] == "IOC"]
    assert rescue and rescue[-1]["side"] == "buy" and rescue[-1]["reduce_only"] is True
    assert w.arcus == pytest.approx(0.0)                  # Arcus 裸腿已平掉
    assert not any(o["live"] for o in w.orders.values())  # 没有留下活着的挂单


def test_a_failed_hedge_on_a_partial_fill_also_cancels_the_resting_order():
    w = World(fills={2: 0.2}, lighter_ok=False)
    ex, r = open_(w)
    assert r.stage == "maker_hedge_failed"
    assert w.cancels and not any(o["live"] for o in w.orders.values())
    assert w.arcus == pytest.approx(0.0) and w.lighter == pytest.approx(0.0)


# ── 改价 / 被拒 / 遗留挂单 ──────────────────────────────

def test_being_outbid_cancels_and_requotes_at_the_new_top():
    w = World(fills={12: 0.5}, bbo_script=[(2686.00, 2686.10), (2686.00, 2686.05)])
    ex, r = open_(w)
    alo = [p for p in w.placed if p["tif"] == "ALO"]
    assert len(alo) >= 2 and alo[1]["price"] < alo[0]["price"]
    assert w.cancels[0] == "o1"
    assert r.ok and w.lighter == pytest.approx(0.5)


def test_a_would_cross_rejection_is_retried_not_treated_as_failure():
    w = World(fills={2: 0.5}, reject_first="POST_ONLY_WOULD_CROSS")
    ex, r = open_(w)
    assert r.ok and w.lighter == pytest.approx(0.5)


def test_a_real_rejection_stops_without_touching_lighter():
    w = World(fills={2: 0.5}, reject_first="UNDERCOLLATERALIZED")
    ex, r = open_(w)
    assert r.ok is False and w.lighter_orders == [] and w.arcus == 0.0
    assert "UNDERCOLLATERALIZED" in r.reason


def test_our_leftover_orders_are_swept_before_quoting():
    w = World(fills={2: 0.5}, stale=[{"orderId": "old1", "clientId": "ph99"},
                                     {"orderId": "manual", "clientId": "mine"}])
    ex, r = open_(w)
    assert w.cancels[0] == "old1" and "manual" not in w.cancels   # 只撤自己的
    assert r.ok


# ── 平仓 ────────────────────────────────────────────────

def close_(world, **kw):
    ex, mk = maker(world, **kw)

    async def taker_close(**kwargs):
        world.taker_close = kwargs
        world.lighter = world.arcus = 0.0
        from parallax_hedge.execution import PairResult
        return PairResult(True, "closed", ledger=[])
    ex.close_pair = taker_close
    r = run(mk.close_pair(market=MARKET, lighter_size=world.lighter, arcus_size=world.arcus,
                          lighter_price=2686.0, arcus_price=2686.05, slippage_bps=10.0,
                          lighter_decimals=(4, 2)))
    return ex, r


def test_a_full_maker_close_needs_no_taker_fallback():
    w = World(fills={2: 0.5})
    w.arcus, w.lighter = -0.5, 0.5
    ex, r = close_(w)
    assert r.ok and r.stage == "closed"
    assert w.placed[0]["side"] == "buy" and w.placed[0]["reduce_only"] is True
    assert w.lighter_orders[0][:3] == ("sell", 0.5, True)
    assert not hasattr(w, "taker_close")


def test_what_the_maker_close_misses_is_not_finished_by_taker():
    """没平完的部分不吃单。已经成交的那一段仍然在 Lighter 对冲上。"""
    w = World(fills={2: 0.2})
    w.arcus, w.lighter = -0.5, 0.5
    ex, r = close_(w, wait=10)
    assert r.ok is False and r.stage == "close_wait"
    assert not hasattr(w, "taker_close")
    assert w.lighter == pytest.approx(0.3)
    assert w.arcus == pytest.approx(-0.3)


# ── 签名 ────────────────────────────────────────────────

def test_cancel_payload_layout():
    payload = ax.build_cancel_payload(address="0xAB" + "cd" * 19, account_index=3,
                                      timestamp=1790359688770490699, market_id=26,
                                      order_id="0a1b")
    assert payload == ('{"ad":"0xab' + "cd" * 19 + '","ai":3,"ct":1790359688770490699,'
                       '"id":"0a1b","m":26,"op":2,"v":1}')
    with pytest.raises(ValueError):
        ax.build_cancel_payload(address="0x", account_index=0, timestamp=1, market_id=1)


def test_an_alo_order_is_signed_with_tif_code_3_at_the_exact_price():
    ex = MakerTestExecutor(World())
    order = ex.build_arcus_order(MARKET, "sell", 0.5, 2686.09, time_in_force="ALO")
    assert order["body"]["timeInForce"] == "ALO" and order["body"]["price"] == "2686.09"
    assert '"t":3,' in order["payload"] and '"p":268609,' in order["payload"]
    with pytest.raises(ValueError):          # 不在 tick 上的挂单价直接拒绝，不偷偷取整
        ex.build_arcus_order(MARKET, "sell", 0.5, 2686.095, time_in_force="ALO")



# ── v1.2：成交后立刻对冲，不等上一笔确认 ─────────────────

def test_the_hedge_goes_out_in_the_same_step_the_fill_is_seen():
    w = World(fills={2: 0.5})
    open_(w)
    assert w.fill_times and w.lighter_times
    assert w.lighter_times[0] - w.fill_times[0] < 1e-9     # 看到成交的同一步就发出


def test_back_to_back_partial_fills_are_not_held_up_by_confirmation():
    """上一版每笔对冲都要等 Lighter 仓位确认（几秒）才看下一笔成交 —— 那就是十几秒的裸腿。"""
    w = World(fills={2: 0.2, 3: 0.15, 4: 0.15})
    ex, r = open_(w)
    assert r.ok and w.lighter == pytest.approx(0.5)
    delays = [lt - ft for ft, lt in zip(w.fill_times, w.lighter_times)]
    assert len(delays) == 3 and max(delays) < 1e-9
    assert w.lighter_times[2] - w.lighter_times[0] <= 1.0 + 1e-9   # 两次轮询之内全部发出


def test_a_missed_lighter_hedge_is_topped_up_not_abandoned():
    """Lighter IOC 偶尔落空：确认发现没成交就放宽滑点补单，不走抢救。"""
    w = World(fills={2: 0.5})
    w.lighter_misses = 1
    ex, r = open_(w)
    assert r.ok and w.lighter == pytest.approx(0.5) and w.arcus == pytest.approx(-0.5)
    assert len(w.lighter_orders) == 2 and w.lighter_orders[1][3] > w.lighter_orders[0][3]
    assert any("补单" in n for n in r.notes)


class Bell:
    healthy = True

    def __init__(self, world):
        self.w = world
        self.waits = 0

    async def wait(self, timeout):
        self.waits += 1
        self.w.now += 0.05                   # 推送 50 毫秒就到
        await asyncio.sleep(0)
        return True


def test_with_the_bell_fills_are_checked_on_every_push():
    w = World(fills={2: 0.5})
    ex, mk = maker(w)
    mk.bell = Bell(w)
    r = run(mk.open_pair(market=MARKET, direction="long_lighter_short_arcus", quantity=0.5,
                         lighter_price=2686.0, arcus_price=2686.05, slippage_bps=10.0,
                         lighter_decimals=(4, 2)))
    assert r.ok and mk.bell.waits >= 1
    assert w.lighter_times[0] <= 0.2                      # 门铃模式下一百多毫秒就发出了对冲


def test_an_unhealthy_bell_falls_back_to_fast_polling():
    w = World(fills={2: 0.5})
    ex, mk = maker(w)
    mk.bell = Bell(w)
    mk.bell.healthy = False
    r = run(mk.open_pair(market=MARKET, direction="long_lighter_short_arcus", quantity=0.5,
                         lighter_price=2686.0, arcus_price=2686.05, slippage_bps=10.0,
                         lighter_decimals=(4, 2)))
    assert r.ok and mk.bell.waits == 0
    assert w.lighter_times[0] <= 0.5 + 1e-9


# ── 叫停（v1.3 补仓期间风控介入）─────────────────────────

def test_a_stop_signal_ends_quoting_early_but_still_hedges_what_filled():
    """叫停和超时走同一条收尾：撤单、已成交的照样对冲，不留裸腿。"""
    w = World(fills={2: 0.2})
    ex, mk = maker(w, wait=120)
    stop = asyncio.Event()
    mk.stop = stop
    original = ex.read_arcus_size

    async def read(market_id):
        size = await original(market_id)
        if w.polls >= 3:
            stop.set()                    # 成交 0.2 之后风控要求停下
        return size
    ex.read_arcus_size = read
    r = run(mk.open_pair(market=MARKET, direction="long_lighter_short_arcus", quantity=0.5,
                         lighter_price=2686.0, arcus_price=2686.05, slippage_bps=10.0,
                         lighter_decimals=(4, 2), action="topup"))
    assert r.ok and w.now < 60                                   # 没等满 120 秒
    assert w.cancels                                              # 挂单撤掉了
    assert w.lighter == pytest.approx(0.2) and w.arcus == pytest.approx(-0.2)
    assert {a for a, _ in r.ledger} == {"topup"}                  # 账本记成补仓


# ── 账本：补过单时每张对冲单都交给对账（v1.3.1）─────────────

def test_after_a_missed_hedge_every_lighter_order_goes_to_reconciliation():
    """仓位分不出是哪张单落空：不能把总量硬分给前面的单（2026-09-28 QQQ 补仓的账就是这么错的）。"""
    from parallax_hedge.ledger import ledger_rows_for_result
    w = World(fills={2: 0.5})
    w.lighter_misses = 1
    ex, r = open_(w)
    rows = [x for x in ledger_rows_for_result("ETH", r, dry_run=False, market_id=0)
            if x["venue"] == "lighter"]
    assert len(rows) == 2                                          # 落空的那张也记上，等对账
    assert all(x["confirmed_qty"] is None for x in rows)            # 不按仓位硬分


def test_clean_hedges_are_booked_as_confirmed():
    from parallax_hedge.ledger import ledger_rows_for_result
    w = World(fills={2: 0.2, 4: 0.3})
    ex, r = open_(w)
    rows = [x for x in ledger_rows_for_result("ETH", r, dry_run=False, market_id=0)
            if x["venue"] == "lighter"]
    assert [x["confirmed_qty"] for x in rows] == [pytest.approx(0.2), pytest.approx(0.3)]


def test_open_does_not_quote_the_expensive_side():
    """买 Lighter 的价高于卖 Arcus 的价：贵的一边也挂 maker，不因为价差拒绝。"""
    w = World(bbo=(2679.90, 2680.00), fills={2: 0.5})
    ex, r = open_(w)
    assert r.ok and r.stage == "opened"
    alo = [p for p in w.placed if p["tif"] == "ALO"]
    assert alo and alo[0]["side"] == "sell"
    assert not any(p["tif"] == "IOC" for p in w.placed)
    assert w.lighter_orders and w.lighter_orders[0][0] == "buy"
    assert not any("不下单" in n for n in r.notes)


def test_eth_prints_post_when_the_buy_is_cheaper_even_if_wide():
    """买 Lighter 卖一 2662.56、卖 Arcus 2663.43，约 3.3 bp。买得更便宜就挂。

    不再因为宽于 1 bp 拒绝。对冲仍然打卖一，不是买一。
    """
    w = World(bbo=(2663.30, 2663.44), lighter_bbo=(2662.40, 2662.56),
              fills={2: 0.1502})
    ex, r = open_(w, quantity=0.1502, wait=8)
    assert r.ok and r.stage == "opened"
    alo = [p for p in w.placed if p["tif"] == "ALO"]
    assert alo and alo[0]["side"] == "sell" and alo[0]["price"] == pytest.approx(2663.43)
    assert w.lighter_orders and w.lighter_orders[0][0] == "buy"
    assert w.lighter == pytest.approx(0.1502) and w.arcus == pytest.approx(-0.1502)


def test_a_requote_rechecks_the_live_lighter_hedge_and_still_hedges_a_fill():
    """盘口挪了可以跟新的买一/卖一。所间价差变宽不拒绝第二口，也不把 Arcus 改成吃单。
    已经成交的部分仍然对冲，方向仍是买 Lighter、卖 Arcus。"""
    w = World(
        bbo=(2663.40, 2663.55),
        bbo_script=[(2663.40, 2663.55), (2663.30, 2663.44)],
        lighter_bbo=(2663.20, 2663.40),
        lighter_bbo_script=[(2663.20, 2663.40), (2663.20, 2663.40), (2664.00, 2664.20)],
        fills={2: 0.1502},
    )
    ex, r = open_(w, quantity=0.3004, wait=12)
    alo = [p for p in w.placed if p["tif"] == "ALO"]
    assert alo and all(p["side"] == "sell" for p in alo)
    assert alo[0]["price"] == pytest.approx(2663.54)
    assert not any(p["tif"] == "IOC" for p in w.placed)
    assert w.lighter_orders and w.lighter_orders[0][0] == "buy"
    assert w.lighter == pytest.approx(0.1502)
    assert w.arcus == pytest.approx(-0.1502)
    assert r.ok
    assert not any("不下单" in n for n in r.notes)


def test_a_tight_book_still_buys_the_cheaper_lighter_ask():
    """买价更便宜、绝对价差不到 1 bp：Lighter 做多，Arcus 做空。"""
    w = World(bbo=(2686.00, 2686.10), lighter_bbo=(2685.90, 2686.00), fills={2: 0.5})
    ex, r = open_(w)
    assert r.ok and r.stage == "opened"
    assert w.placed[0]["side"] == "sell" and w.placed[0]["tif"] == "ALO"
    assert w.lighter_orders[0][0] == "buy"
    assert w.lighter == pytest.approx(0.5) and w.arcus == pytest.approx(-0.5)


def _open_short_lighter(world, quantity=0.5, wait=45.0):
    ex, mk = maker(world, wait=wait)
    r = run(mk.open_pair(
        market=MARKET, direction="short_lighter_long_arcus", quantity=quantity,
        lighter_price=2686.2, arcus_price=2686.01, slippage_bps=10.0,
        lighter_decimals=(4, 2),
    ))
    return ex, r


def test_several_rejected_quotes_are_not_followed_by_a_place():
    """买价不低于卖价也不拒绝。连续几口都挂 ALO，不把 Arcus 改成吃单。"""
    w = World(
        bbo=(2686.50, 2686.70),
        bbo_script=[
            (2686.50, 2686.70),   # 买 2686.51，卖 2686.20
            (2686.80, 2687.00),   # 买 2686.81，卖 2685.00
            (2686.20, 2686.40),   # 买 2686.21，卖 2684.40
            (2684.00, 2684.20),   # 买 2684.01，卖 2685.20，约 4.4 bp
        ],
        lighter_bbo=(2686.20, 2686.30),
        lighter_bbo_script=[
            (2686.20, 2686.30),
            (2685.00, 2685.10),
            (2684.40, 2684.50),
            (2683.50, 2683.60),
        ],
        fills={2: 0.5, 3: 0.5, 4: 0.5, 6: 0.5, 8: 0.5},
    )
    ex, r = _open_short_lighter(w, quantity=0.5, wait=6)
    alo = [p for p in w.placed if p["tif"] == "ALO"]
    assert alo
    assert all(p["side"] == "buy" and p["tif"] == "ALO" for p in alo)
    assert not any(p["tif"] == "IOC" for p in w.placed)
    assert not any("不下单" in n for n in r.notes)


def test_a_stale_live_order_is_cancelled_before_it_can_fill():
    """所间价差变差不撤已经挂着的 maker。成交的部分仍然对冲，Arcus 不改吃单。
    """
    w = World(
        bbo=(2686.00, 2686.10),
        lighter_bbo=(2686.20, 2686.30),
        lighter_bbo_script=[
            (2686.20, 2686.30),
            (2685.00, 2685.10),   # 对冲价变差，买价不再低于卖价，也不撤
            (2684.50, 2684.60),
            (2684.00, 2684.10),
        ],
        fills={4: 0.5},
    )
    ex, r = _open_short_lighter(w, quantity=0.5, wait=8)
    alo = [p for p in w.placed if p["tif"] == "ALO"]
    assert alo and alo[0]["side"] == "buy"
    assert alo[0]["price"] == pytest.approx(2686.01)
    assert not any(p["tif"] == "IOC" for p in w.placed)
    assert w.cancels == []
    assert w.lighter_orders and w.lighter_orders[0][0] == "sell"
    assert r.ok
    assert not any("不下单" in n for n in r.notes)
    assert any("挂单成交" in n for n in r.notes)
