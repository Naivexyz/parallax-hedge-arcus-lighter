"""挂单模式：Arcus 那条腿挂 ALO（只做 maker），成交多少就立刻去 Lighter 吃单对冲多少。

为什么：Arcus 吃单费 2.25 bps、挂单费 0；Lighter 吃单挂单都是 0。
2026-09-26 实盘前 26 笔成交里，Arcus 手续费占了全部成本的 74%。
Arcus 这条腿改成挂单，手续费归零，还能省下 Arcus 那半个买卖价差。

顺序和吃单模式【反过来】：吃单模式先下 Lighter（它是受限的一边），
挂单模式必须先等 Arcus 成交 —— 挂单成不成交、成交多少由市场决定，
只能成交一点、对冲一点。

代价，必须说清楚：
  1. 裸腿窗口：Arcus 成交到 Lighter 对冲单发出之间有一段时间只有一条腿。
     压缩办法（v1.2）：
       · Arcus 订单推送当门铃（arcus_ws.py），一有推送立刻读仓位；门铃坏了每 0.5 秒轮询；
       · 看到新成交【立刻】发 Lighter 对冲单，不等上一笔对冲确认完 ——
         确认放在旁边异步做，只有确认发现某笔没成交才补单。
     正常情况下窗口约为：推送延迟 + 一次仓位查询 + 一次 Lighter 下单，一秒左右。
  2. 逆向选择：静止的挂单更容易在价格往不利方向走的时候被打中。
     省下的手续费不是净赚，有一部分会还回去。
  3. 慢：开 / 平一次要等挂单成交，最多等 MAKER_WAIT_SECONDS 秒。
     挂单任务在后台跑（v1.2），不耽误其它币种每 20 秒一次的风控检查。

兜底规则（任何一条出问题都退回安全状态）：
  · 只认仓位变化，不看挂单回执。
  · Lighter 对冲失败（重试一次、放宽滑点仍然不成）→ 撤掉 Arcus 挂单，
    把没对冲上的 Arcus 仓位立刻吃单平掉。
  · 开仓等到超时一点都没成交 → 撤单，什么都没发生，下一轮再挂，零成本。
  · 平仓等到超时没平完 → 已成交的部分留在 Lighter 对冲上，剩余继续挂 maker，不改吃单（Arcus 吃单要付费）。
  · 风控触发的平仓（离强平太近、两腿不匹配、孤腿）【一律吃单】，不等。
  · 撤单确认不了（网络断了）→ 不再挂新单，交给下一轮的孤腿 / 不匹配检查兜底。
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import time
from dataclasses import dataclass, field
from typing import Any

from . import arcus as ax
from .execution import (
    Executor,
    LegResult,
    PairResult,
    _stamp,
)
from .fills import align_quantity, classify_fill, hedge_side, slippage_limit_price
from .spread_gate import open_sides_allowed

# 轮询 Arcus 仓位的间隔（秒）。/v1/positions 一次 2 权重：
#   门铃正常时 1.5 秒兜底轮询一次（门铃一响立刻查）；门铃坏了 0.5 秒一次（约 240 权重/分钟，上限 1500）
POLL_SEC = 0.5
POLL_SEC_WITH_BELL = 1.5
# Lighter 对冲单发出后，多久还没在仓位上看到就判定「这笔没成交」去补单
HEDGE_CONFIRM_SEC = 2.5
# 查 Lighter 仓位的最小间隔（Lighter REST 本身有 1.05 秒的限流闸门）
LIGHTER_CHECK_SEC = 1.1
# 同一次挂单里 Lighter 对冲补单最多几次，超过就判定对冲失败、走抢救
MAX_HEDGE_RETRIES = 3
# 多久看一次盘口，决定要不要改价（被别人抢到前面了就撤了重挂）
REQUOTE_CHECK_SEC = 3.0
# 最多改价次数（防止在剧烈行情里反复撤挂）
MAX_REQUOTES = 12
# 对冲腿的滑点：宁可多让一点也必须成交 —— 裸腿比滑点贵得多
HEDGE_SLIPPAGE_BPS = 25.0
# 撤单后等多久再读仓位（撤单 202 之后撮合引擎处理需要一点时间）
AFTER_CANCEL_SEC = 0.5
# 我们自己挂的单的 clientId 前缀（ax.new_client_id 生成的都以它开头）
OUR_CLIENT_PREFIX = "ph"


@dataclass
class MakerRun:
    """一次挂单过程的账。"""

    target: float
    filled: float = 0.0                 # Arcus 成交（按仓位变化量）
    hedged: float = 0.0                 # Lighter 已对冲（仓位上已经看到的）
    sent: float = 0.0                   # Lighter 已发出的对冲量（可能还没确认）
    hedge_retries: int = 0
    error: str | None = None
    stale_order: bool = False           # 有挂单撤不掉 / 确认不了
    requotes: int = 0
    arcus_legs: list[LegResult] = field(default_factory=list)
    lighter_legs: list[LegResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def unhedged(self) -> float:
        return max(0.0, self.filled - self.hedged)


class MakerExecutor:
    """挂单模式的开 / 平仓。执行细节全部复用 Executor 的底层动作。"""

    def __init__(self, executor: Executor, *, wait_seconds: float = 120.0,
                 clock: Any = time.monotonic, sleep: Any = asyncio.sleep,
                 bell: Any = None, stop: asyncio.Event | None = None) -> None:
        self.ex = executor
        self.wait_seconds = max(5.0, float(wait_seconds))
        self.clock = clock
        self.sleep = sleep
        self.bell = bell
        # 叫停信号（补仓期间风控要介入时由引擎设置）：
        # 和超时一样走正常收尾 —— 撤单、把已成交的对冲完，而不是硬取消任务
        self.stop = stop

    def _stopped(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    # ── 开仓 ────────────────────────────────────────
    async def open_pair(
        self, *, market: dict[str, Any], direction: str, quantity: float,
        lighter_price: float, arcus_price: float, slippage_bps: float,
        lighter_decimals: tuple[int, int], quotes: dict[str, Any] | None = None,
        action: str = "open",
    ) -> PairResult:
        """action="topup" 时是持仓期间的补仓：流程完全一样，只是账本里记成补仓。"""
        started = self.clock()
        lighter_side, arcus_side = hedge_side(direction)
        quotes = dict(quotes or {}, maker=True, lighter_side=lighter_side,
                      arcus_side=arcus_side, slippage_bps=slippage_bps)
        # 先确认这张单签得出来（密钥、步长、上限）—— 和吃单模式同一道闸
        try:
            self.ex.build_arcus_order(market, arcus_side, quantity, arcus_price,
                                      time_in_force="IOC")
        except Exception as exc:  # noqa: BLE001
            return PairResult(False, "arcus_order_invalid", reason=f"Arcus 这张单签不出来：{exc}",
                              quotes=quotes, notes=["尚未下任何单"])

        run = await self._work(
            market=market, arcus_side=arcus_side, target=quantity, reduce_only=False,
            lighter_side=lighter_side, lighter_reduce_only=False,
            lighter_price=lighter_price, arcus_price=arcus_price,
            slippage_bps=slippage_bps, lighter_decimals=lighter_decimals,
        )
        ledger = [(action, leg) for leg in self._booked(run)]
        elapsed = (self.clock() - started) * 1000

        if run.error or run.stale_order:
            # 没对冲上的 Arcus 仓位立刻吃单平掉 —— 不留裸腿
            rescue = None
            if run.unhedged > 0:
                rescue = await self.ex._flatten(
                    market, "arcus", run.unhedged,
                    "buy" if arcus_side == "sell" else "sell",
                    arcus_price, slippage_bps, lighter_decimals,
                )
                ledger.append(("rescue", rescue))
            return PairResult(
                False, "maker_hedge_failed", self._last(run.lighter_legs, "lighter"),
                self._last(run.arcus_legs, "arcus"), rescue,
                reason=(run.error or "Arcus 挂单撤不掉")
                + (" —— 没对冲上的 Arcus 部分已吃单平掉" if rescue else ""),
                quotes={**quotes, "filled_quantity": run.hedged},
                elapsed_ms=elapsed, notes=run.notes, ledger=ledger,
            )
        # 太零碎、Lighter 下不了单的那点 Arcus 成交：吃单减掉，免得两腿对不齐
        residual = ax.align_arcus_quantity(market, max(0.0, run.filled - max(run.sent, run.hedged)))
        if residual > 0 and float(residual) >= float(market.get("arcus_min_order_size") or 0):
            trim = await self.ex._flatten(
                market, "arcus", float(residual), "buy" if arcus_side == "sell" else "sell",
                arcus_price, slippage_bps, lighter_decimals,
            )
            ledger.append(("rescue", trim))
            run.notes.append(f"Arcus 多成交的零头 {float(residual):g} 已吃单减掉")
        if run.hedged <= 0:
            return PairResult(
                False, "maker_not_filled", reason=(
                    f"挂单 {self.wait_seconds:g} 秒内没有成交，已撤单（没有产生任何成本），下一轮再挂"),
                quotes=quotes, elapsed_ms=elapsed, notes=run.notes, ledger=ledger,
            )
        note = (f"挂单成交 {run.filled:g}，Lighter 对冲 {run.hedged:g}"
                + (f"（计划 {quantity:g}，超时只开出这么多）" if run.hedged < quantity * 0.98 else ""))
        return PairResult(
            True, "opened", self._last(run.lighter_legs, "lighter"),
            self._last(run.arcus_legs, "arcus"),
            quotes={**quotes, "filled_quantity": run.hedged},
            elapsed_ms=elapsed, notes=run.notes + [note], ledger=ledger,
        )

    # ── 平仓 ────────────────────────────────────────
    async def close_pair(
        self, *, market: dict[str, Any], lighter_size: float, arcus_size: float,
        lighter_price: float, arcus_price: float, slippage_bps: float,
        lighter_decimals: tuple[int, int],
    ) -> PairResult:
        """Arcus 挂 maker 平，成交多少就立刻用 Lighter 对冲多少。

        等不到的剩余不改吃单，免得 Arcus 付吃单费。下一轮再挂。
        对冲失败时，没对上的 Arcus 成交仍会吃单减掉，不留裸腿。
        """
        started = self.clock()
        target = min(abs(lighter_size), abs(arcus_size))
        run = None
        if target > 0 and lighter_size * arcus_size < 0:
            run = await self._work(
                market=market, arcus_side="buy" if arcus_size < 0 else "sell",
                target=target, reduce_only=True,
                lighter_side="sell" if lighter_size > 0 else "buy", lighter_reduce_only=True,
                lighter_price=lighter_price, arcus_price=arcus_price,
                slippage_bps=slippage_bps, lighter_decimals=lighter_decimals,
            )
        ledger = [("close", leg) for leg in (self._booked(run) if run else [])]
        notes = list(run.notes) if run else []

        # 按两边当前仓位看：都平掉才算完成。没挂完的剩余不改吃单。
        lighter_now, arcus_now = await self.ex.read_positions(
            market["lighter_symbol"], int(market["arcus_market_id"]))
        if math.isfinite(lighter_now) and math.isfinite(arcus_now) \
                and abs(lighter_now) < 1e-12 and abs(arcus_now) < 1e-12:
            return PairResult(True, "closed", self._last(run.lighter_legs, "lighter") if run else None,
                              self._last(run.arcus_legs, "arcus") if run else None,
                              elapsed_ms=(self.clock() - started) * 1000,
                              notes=notes + ["全部挂单平掉"], ledger=ledger)
        hedged = run.hedged if run else 0.0
        if run is not None and run.unhedged > 0 and (run.error or run.stale_order):
            # 已经成交、Lighter 没对上：把这条腿吃单减掉，不留裸腿。这不是把整仓改吃单。
            rescue = await self.ex._flatten(
                market, "arcus", run.unhedged,
                "sell" if arcus_size < 0 else "buy",
                arcus_price, slippage_bps, lighter_decimals,
            )
            ledger.append(("rescue", rescue))
            return PairResult(
                False, "maker_hedge_failed",
                self._last(run.lighter_legs, "lighter"),
                self._last(run.arcus_legs, "arcus"), rescue,
                reason=(run.error or "Arcus 挂单撤不掉") + " —— 没对冲上的部分已吃单减掉",
                elapsed_ms=(self.clock() - started) * 1000,
                notes=notes, ledger=ledger,
            )
        return PairResult(
            False, "close_wait",
            self._last(run.lighter_legs, "lighter") if run else None,
            self._last(run.arcus_legs, "arcus") if run else None,
            reason=f"挂 maker 已对冲 {hedged:g}，剩余不吃单，下一轮再挂",
            elapsed_ms=(self.clock() - started) * 1000,
            notes=notes + ["剩余仓位继续挂 maker，避免 Arcus 吃单费"],
            ledger=ledger,
        )

    # ── 核心：挂单、盯成交、立刻对冲 ─────────────────
    async def _work(
        self, *, market: dict[str, Any], arcus_side: str, target: float, reduce_only: bool,
        lighter_side: str, lighter_reduce_only: bool, lighter_price: float,
        arcus_price: float, slippage_bps: float, lighter_decimals: tuple[int, int],
    ) -> MakerRun:
        ex = self.ex
        run = MakerRun(target=target)
        market_id = int(market["arcus_market_id"])
        sign = 1.0 if arcus_side == "buy" else -1.0
        lsign = 1.0 if lighter_side == "buy" else -1.0
        deadline = self.clock() + self.wait_seconds
        size_dec = int(lighter_decimals[0])
        minimum = self._hedge_minimum(market, arcus_price)

        await self._sweep_stale(market, run)
        if run.stale_order:
            run.error = "发现撤不掉的旧挂单，本轮不挂"
            return run
        base = await ex.read_arcus_size(market_id)
        if not math.isfinite(base):
            run.error = "读不到 Arcus 仓位，不挂单"
            return run
        lighter_base = await self._lighter_size(market)
        if not math.isfinite(lighter_base):
            run.error = "读不到 Lighter 仓位，不挂单"
            return run

        state: dict[str, Any] = {
            "ref": float(lighter_price), "last_send": None, "check": None,
            "last_check_at": -1e9, "check_started": -1e9,
        }

        async def refresh_filled(leg: LegResult | None) -> None:
            size = await ex.read_arcus_size(market_id)
            if not math.isfinite(size):
                return
            filled = min(max(0.0, (size - base) * sign), target * 1.001)
            if leg is not None and filled > run.filled:
                leg.filled += filled - run.filled
                leg.fill_confirmed = True
            run.filled = max(run.filled, filled)

        async def dispatch() -> None:
            """已成交、还没发对冲单的部分【立刻】发出去，不等前面的对冲确认。"""
            if run.error:
                return
            need = align_quantity(run.filled - run.sent, size_dec)
            if need <= 0 or need < minimum:
                return
            bps = max(slippage_bps, HEDGE_SLIPPAGE_BPS) * (3 if run.hedge_retries else 1)
            leg = await ex._lighter_ioc(
                market["lighter_market_index"], lighter_side, need,
                slippage_limit_price(state["ref"], lighter_side, bps), lighter_decimals,
                reduce_only=lighter_reduce_only,
            )
            _stamp(leg, side=lighter_side, requested=need, reference_price=state["ref"])
            if not leg.submitted and not leg.ok:
                run.hedge_retries += 1
                if run.hedge_retries > MAX_HEDGE_RETRIES:
                    run.error = f"Lighter 对冲单发不出去：{leg.error}"
                return
            run.lighter_legs.append(leg)
            run.sent += need
            state["last_send"] = self.clock()

        async def read_lighter() -> float:
            return await self._lighter_size(market)

        async def verify(force: bool = False) -> None:
            """对冲确认：在旁边查 Lighter 仓位，不挡住下一次成交检测。"""
            task = state["check"]
            if task is not None and task.done():
                state["check"] = None
                try:
                    size = task.result()
                except Exception:  # noqa: BLE001
                    size = float("nan")
                if math.isfinite(size):
                    confirmed = max(0.0, (size - lighter_base) * lsign)
                    self._attribute(run, confirmed)
                    run.hedged = confirmed
                    shortfall = run.sent - confirmed
                    last = state["last_send"]
                    # 只有「对冲单发出 2.5 秒之后才开始的那次查询」还看不到，才算这笔没成交；
                    # 发单之前就开始的查询看不到是正常的，拿它补单会重复对冲
                    if shortfall > 10 ** (-size_dec) / 2 and last is not None \
                            and state["check_started"] - last >= HEDGE_CONFIRM_SEC:
                        # 有对冲单没成交（Lighter IOC 偶尔落空）：按实际确认量重来，放宽滑点补单
                        run.hedge_retries += 1
                        run.sent = confirmed
                        if run.hedge_retries > MAX_HEDGE_RETRIES:
                            run.error = f"Lighter 对冲补了 {MAX_HEDGE_RETRIES} 次仍未成交"
                            return
                        await self._refresh_ref(market, lighter_side, state)
                        await dispatch()
            if state["check"] is None and (
                    force or self.clock() - state["last_check_at"] >= LIGHTER_CHECK_SEC):
                if run.sent > run.hedged or force:
                    state["last_check_at"] = state["check_started"] = self.clock()
                    state["check"] = asyncio.ensure_future(read_lighter())

        async def settle() -> None:
            """收尾：等所有已发出的对冲都在 Lighter 仓位上看到（最多约 10 秒）。"""
            for _ in range(10):
                if run.error:
                    return
                await dispatch()
                if state["check"] is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(state["check"]), 3.0)
                    except Exception:  # noqa: BLE001
                        pass
                await verify(force=True)
                if state["check"] is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(state["check"]), 3.0)
                    except Exception:  # noqa: BLE001
                        pass
                    await verify()
                done = align_quantity(run.filled, size_dec)
                if abs(run.hedged - run.sent) <= 10 ** (-size_dec) / 2 and (
                        run.sent >= done - 1e-12 or done - run.sent < minimum):
                    return
                await self.sleep(HEDGE_CONFIRM_SEC / 2)

        order_id = None
        try:
            while not run.error and self.clock() < deadline and not self._stopped():
                remaining = ax.align_arcus_quantity(market, target - run.filled)
                if remaining <= 0 or float(remaining) < float(market.get("arcus_min_order_size") or 0):
                    break
                # 唯一的发单口：读盘口、用即将发出的那个价过闸、通过了马上发。
                # 中间不再改价，也不把刚才拒绝过的价留给外层循环再发一次。
                leg, stop = await self._place_if_allowed(
                    market, arcus_side, lighter_side, float(remaining), reduce_only, state, run,
                )
                if leg is None:
                    if stop:
                        break
                    await self.sleep(POLL_SEC)
                    continue
                if not leg.ok and not leg.uncertain:
                    text = str(leg.error or "").upper()
                    if "POST_ONLY" in text or "WOULD_CROSS" in text:
                        await self.sleep(0.3)      # 盘口刚好动了，不是故障，下一轮重算价格
                        continue
                    run.error = f"Arcus 挂单被拒：{leg.error}"
                    break
                run.arcus_legs.append(leg)
                order_id = (leg.raw or {}).get("orderId") if isinstance(leg.raw, dict) else None
                price = float(leg.price if leg.price is not None else 0.0)
                last_quote = self.clock()
                live = True
                while live and not run.error and self.clock() < deadline and not self._stopped():
                    # 先看这张已经挂着的单。价不再合格就立刻撤，不要再等 3 秒，
                    # 也不要先睡过一轮让它在坏价上成交。已经成交的部分下面照样对冲。
                    if not reduce_only:
                        prices_ok, prices_why = await self._quote_allowed(
                            market, lighter_side, arcus_side, price, state,
                        )
                        if not prices_ok:
                            self._note(run, prices_why)
                            break
                    if self.clock() - last_quote >= REQUOTE_CHECK_SEC:
                        last_quote = self.clock()
                        bid, ask = await self._bbo(market)
                        new_price = (
                            ax.passive_price(market, arcus_side, bid, ask) if bid and ask else None
                        )
                        outbid = (arcus_side == "sell" and ask and ask < price) or \
                                 (arcus_side == "buy" and bid and bid > price)
                        if outbid and new_price is not None and run.requotes < MAX_REQUOTES:
                            if not reduce_only:
                                prices_ok, prices_why = await self._quote_allowed(
                                    market, lighter_side, arcus_side, float(new_price), state,
                                )
                                if not prices_ok:
                                    # 新价不合格：旧单刚才还合格，留着，但不重挂这口新价。
                                    self._note(run, prices_why)
                                    continue
                            run.requotes += 1
                            break                      # 撤掉后由外层 _place_if_allowed 重挂
                    await self._wait_for_fill()
                    await refresh_filled(leg)
                    await dispatch()                   # 成交了就立刻对冲
                    await verify()
                    if run.filled >= target * 0.999:
                        live = False                   # 全部成交，挂单已经没了
                        break
                if live:
                    if not await self._cancel(market, order_id, leg, run):
                        break
                    order_id = None
                    await self.sleep(AFTER_CANCEL_SEC)
                    await refresh_filled(leg)          # 撤单那一刻被打中的也要对冲
                    await dispatch()
                else:
                    order_id = None
        finally:
            if order_id:
                # 任务被取消（程序关闭）时挂单还活着：尽力撤掉
                with contextlib.suppress(Exception):
                    await ex._arcus_cancel(market, str(order_id))

        if not run.stale_order:
            await refresh_filled(run.arcus_legs[-1] if run.arcus_legs else None)
            await settle()
            await self._fix_over_hedge(market, lighter_side, lighter_reduce_only,
                                       lighter_decimals, run, state, minimum)
        if state["check"] is not None and not state["check"].done():
            state["check"].cancel()
        if run.requotes:
            run.notes.append(f"改价 {run.requotes} 次")
        if run.hedge_retries:
            run.notes.append(f"Lighter 对冲补单 {run.hedge_retries} 次")
        return run


    async def _place_if_allowed(
        self, market: dict[str, Any], arcus_side: str, lighter_side: str,
        quantity: float, reduce_only: bool, state: dict[str, Any], run: MakerRun,
    ) -> tuple[LegResult | None, bool]:
        """读此刻的盘口，闸门用的价必须就是马上要发出去的价。

        返回 (leg, stop)。没发单时 leg 是 None；stop 为真表示不用再试
        （金额不够）。拒绝的价不会落到 _arcus_place。
        """
        bid, ask = await self._bbo(market)
        if not bid or not ask:
            self._note(run, "读不到 Arcus 盘口，不下单")
            return None, False
        raw = ax.passive_price(market, arcus_side, bid, ask)
        if raw is None:
            self._note(run, "挂单价算不出来（盘口交叉或缺档），不下单")
            return None, False
        price = float(raw)
        if not reduce_only:
            prices_ok, prices_why = await self._quote_allowed(
                market, lighter_side, arcus_side, price, state,
            )
            if not prices_ok:
                self._note(run, prices_why)
                return None, False
            if quantity * price < float(market.get("arcus_min_notional") or 0):
                return None, True
        leg = await self.ex._arcus_place(
            market, arcus_side, quantity, price, reduce_only, "ALO",
        )
        _stamp(leg, side=arcus_side, requested=quantity, reference_price=price)
        leg.price, leg.price_is_estimate = price, False   # 挂单成交价就是挂的价
        return leg, False

    @staticmethod
    def _note(run: MakerRun, why: str) -> None:
        if why and (not run.notes or run.notes[-1] != why):
            run.notes.append(why)

    async def _wait_for_fill(self) -> None:
        """等成交信号：门铃正常就等门铃（最多 1.5 秒兜底），否则 0.5 秒轮询一次。"""
        bell = self.bell
        if bell is not None and getattr(bell, "healthy", False):
            await bell.wait(POLL_SEC_WITH_BELL)
        else:
            await self.sleep(POLL_SEC)

    async def _lighter_size(self, market: dict[str, Any]) -> float:
        """只读 Lighter 这一个币的仓位（不碰 Arcus）。读不到返回 nan。"""
        from .positions import parse_lighter_position
        ex = self.ex
        index = ex.settings.lighter_account_index
        try:
            payload = await ex.market.lighter_account()
        except Exception:  # noqa: BLE001
            return float("nan")
        leg = parse_lighter_position(payload, index, market["lighter_symbol"]) if index is not None else None
        return float(leg.size) if leg is not None else float("nan")

    async def _refresh_ref(self, market: dict[str, Any], side: str, state: dict[str, Any]) -> bool:
        """把 state["ref"] 改成 Lighter 对冲 IOC 此刻会吃到的价。

        买打卖一（is_ask=False），卖打买一（is_ask=True）。读失败就保持原值。
        """
        try:
            book = await self.ex.market.lighter_book(market["lighter_market_index"],
                                                     market["lighter_symbol"], limit=20)
            price = book.best_ask if side == "buy" else book.best_bid
            if price and float(price) > 0:
                state["ref"] = float(price)
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _quote_allowed(
        self, market: dict[str, Any], lighter_side: str, arcus_side: str,
        arcus_price: float, state: dict[str, Any],
    ) -> tuple[bool, str]:
        """新挂或改价之前：读得到 Lighter 盘口就挂。所间价差不拒绝。"""
        if not await self._refresh_ref(market, lighter_side, state):
            return False, "读不到 Lighter 实时对冲价，不下单"
        return True, "所间价差不拦开仓"

    @staticmethod
    def _attribute(run: MakerRun, confirmed: float) -> None:
        """账本用：每笔 Lighter 对冲单成交了多少。

        仓位只告诉我们【总共】对冲上多少，分不出是哪一张单落空。
        v1.3 以前按发出顺序往前面的单上分 —— 2026-09-28 QQQ 补仓：第一张单实际只成交 0.0154，
        补发的第二张成交了 0.4453，账本却把 0.4607 全记在第一张上、第二张没记，
        对账时对不全，手续费变成「未知」，净损益和磨损就再也算不出来。
        现在：所有对冲单都整单成交（总量对得上、没补过单）才直接定稿；
        否则每张单都交给对账，按交易所成交记录逐张核实（没成交的那张会对成 0）。
        """
        legs = run.lighter_legs
        if not legs:
            return
        hedge_side = legs[0].side
        net_sent = sum(float(l.requested or 0) * (1 if l.side == hedge_side else -1) for l in legs)
        exact = (run.hedge_retries == 0 and all(l.side == hedge_side for l in legs)
                 and abs(net_sent - confirmed) <= 1e-9 + 1e-6 * max(1.0, abs(net_sent)))
        for leg in legs:
            leg.filled = float(leg.requested or 0) if exact else 0.0
            leg.fill_confirmed = exact

    async def _fix_over_hedge(self, market, lighter_side, reduce_only, lighter_decimals,
                              run: MakerRun, state, minimum) -> None:
        """Lighter 对冲多了（补单和迟到的成交撞在一起）：把多出来的反向减掉。"""
        excess = align_quantity(run.hedged - run.filled, int(lighter_decimals[0]))
        if excess <= 0 or excess < minimum:
            return
        back = "sell" if lighter_side == "buy" else "buy"
        leg = await self.ex._lighter_ioc(
            market["lighter_market_index"], back, excess,
            slippage_limit_price(state["ref"], back, HEDGE_SLIPPAGE_BPS * 3), lighter_decimals,
            reduce_only=True,
        )
        _stamp(leg, side=back, requested=excess, reference_price=state["ref"])
        run.lighter_legs.append(leg)
        run.notes.append(f"Lighter 多对冲了 {excess:g}，已反向减掉")

    def _hedge_minimum(self, market: dict[str, Any], price: float) -> float:
        """Lighter 最小下单量 / 金额 —— 零碎成交攒够了再对冲，否则 Lighter 会拒单。"""
        by_quote = (float(market.get("lighter_min_quote") or 0) / price) if price > 0 else 0.0
        return max(float(market.get("lighter_min_base") or 0), by_quote * 1.02)

    async def _bbo(self, market: dict[str, Any]) -> tuple[float | None, float | None]:
        try:
            return await self.ex.market.arcus_bbo(market["arcus_symbol"])
        except Exception:  # noqa: BLE001
            return None, None

    async def _cancel(self, market: dict[str, Any], order_id: Any,
                      leg: LegResult, run: MakerRun) -> bool:
        """撤单；撤不掉就查挂单列表再撤一次；还不行就标记 stale，停止再挂。"""
        if order_id:
            ok, _ = await self.ex._arcus_cancel(market, str(order_id))
            if ok:
                return True
        await self._sweep_stale(market, run)
        if run.stale_order:
            run.error = run.error or "Arcus 挂单撤不掉（或确认不了），停止挂单"
            return False
        return True

    async def _sweep_stale(self, market: dict[str, Any], run: MakerRun) -> None:
        """撤掉这个市场上我们自己留下的挂单（上次崩溃 / 撤单没确认留下的）。"""
        try:
            orders = await self.ex.market.arcus_open_orders(int(market["arcus_market_id"]))
        except Exception:  # noqa: BLE001
            return          # 查不到挂单列表不致命：后面的仓位检查仍然兜底
        ours = [o for o in orders
                if str(o.get("clientId") or "").lower().startswith(OUR_CLIENT_PREFIX)]
        for order in ours:
            ok, _ = await self.ex._arcus_cancel(market, str(order.get("orderId")))
            if not ok:
                run.stale_order = True
        if ours:
            run.notes.append(f"撤掉了 {len(ours)} 张遗留挂单")
            await self.sleep(AFTER_CANCEL_SEC)

    @staticmethod
    def _booked(run: MakerRun) -> list[LegResult]:
        """进账本的腿：只记真有成交的挂单，和没被确认「落空」的对冲单。
        一点没成交就撤掉的挂单不记 —— 否则账本里全是数量 0 的「待核对」。"""
        arcus = [leg for leg in run.arcus_legs if leg.filled > 0 or leg.uncertain]
        # 没定稿的对冲单一律记上（数量待对账），交易所那边没成交的会对成 0
        lighter = [leg for leg in run.lighter_legs
                   if not (leg.fill_confirmed and leg.filled <= 0)]
        return arcus + lighter

    @staticmethod
    def _last(legs: list[LegResult], venue: str) -> LegResult | None:
        return legs[-1] if legs else None
