"""对冲刷量引擎 —— 把各层接起来跑。

每个周期对每个任务走一遍：
    快照（费率 + 持仓）→ 风控裁决 → 开仓前预检 → 调度决策 → 执行

所有决策逻辑都在 risk.py / scheduler.py 那些纯函数里，这里只负责
取数据、调用、落库。这样决策部分能离线测透，而这一层的 bug 只会是
接线错误，不会是判断错误。
"""
from __future__ import annotations

import asyncio
import contextlib
import random
import time
from dataclasses import replace
from typing import Any

from .accounts import arcus_balance
from .arcus import account_exposure
from .config import Settings
from .books import basis_bps, executable_prices
from .execution import Executor, PairResult
from .fills import align_quantity, closing_sides, hedge_side
from .funding import choose_direction
from .positions import HedgeHealth
from .risk import (
    DEFAULT_MIN_CORRIDOR_PCT,
    effective_leverages,
    evaluate,
    max_quantity_for,
    pre_open_check,
    sizing_limit,
    verify_corridor_after_open,
)
from .ledger import ledger_rows_for_result
from .scheduler import CycleDecision, TaskState, decide, draw_hold_hours
from .service import FundingService
from .spread_gate import join_price, round_trip_close_net, unrealized_close_ready
from .store import Store

# 开仓后复核的重试节奏：Lighter 是 rollup，账户接口要几秒才反映新仓位。
# 累计约 1.5+3+4.5+6+7.5 = 22.5 秒，足够覆盖正常的结算延迟。
_VERIFY_ATTEMPTS = 5
_VERIFY_DELAY_SEC = 1.5

# 开仓前两所中价最多允许差多少（bps）。正常的跨所基差是个位数到几十 bps；
# 差到 3% 以上，基本是配错了标的（同名不同物、1000 倍合约）或者一边盘口坏了。
MAX_OPEN_BASIS_BPS = 300.0

# 持仓期间挂单补仓（v1.3）：挂单只开出一部分时，后面每一轮继续挂单，补到这一轮的目标数量。
# 离轮换平仓不到「挂单等待时长 + 这么多秒」就不再补 —— 刚补上就要平，白白多一次往返
TOPUP_MIN_LEFT_SEC = 300.0
# 差额小于目标的这个比例就不补了（零头不值得挂单，也可能低于最小下单量）
TOPUP_MIN_FRACTION = 0.02
# 叫停补仓后最多等多久让它收尾（撤单 + 把已成交的对冲完）
TOPUP_STOP_WAIT_SEC = 20.0



def _close_price_line(result: PairResult) -> str:
    """执行记录里要写实际拿去下单的价格，而不是只有标记价估算。"""
    quotes = result.quotes if isinstance(result.quotes, dict) else {}
    preset = quotes.get("close_prices")
    if preset:
        return str(preset)
    parts: list[str] = []
    for leg, name in ((result.lighter, "Lighter"), (result.arcus, "Arcus")):
        if leg is not None and leg.price:
            side = f" {leg.side}" if leg.side else ""
            parts.append(f"{name}{side} {float(leg.price):g}")
    if parts:
        return " / ".join(parts)
    lj, aj = quotes.get("lighter_join"), quotes.get("arcus_join")
    if lj and aj:
        return f"Lighter {float(lj):g} / Arcus {float(aj):g}"
    return ""


class HedgeEngine:
    def __init__(
        self, settings: Settings, service: FundingService, store: Store,
        *, dry_run: bool = True,
    ) -> None:
        self.settings = settings
        self.service = service
        self.store = store
        self.dry_run = dry_run
        self.executor = Executor(settings, service.client, dry_run=dry_run)
        self._lock = asyncio.Lock()
        self.last_cycle_at: float | None = None
        self.last_decisions: list[dict[str, Any]] = []
        # 随机轮换用的随机源。单独一个实例，测试里可以换成固定种子。
        self.rng = random.Random()
        # 上一轮 Arcus 账户里有仓位的币种 —— 组合一变就重设各任务的走廊基准
        self._arcus_composition: frozenset[str] | None = None
        # 后台挂单任务（币种 → asyncio 任务）；门铃由 api 启动时注入
        self._jobs: dict[str, asyncio.Future] = {}
        self._job_kind: dict[str, str] = {}
        self._stops: dict[str, asyncio.Event] = {}
        # 补仓完成后，下一轮要重设各任务的强平距离基准（仓位变大是机械变化，不是行情）
        self._rebase_note: str | None = None
        self.bell = None
        # 已经按成交价挂过刮单的币。单腿不是一种持仓状态，不能每轮再报一次、再挂一次。
        self._scratch_posted: dict[str, tuple[str, float]] = {}

    async def aclose(self) -> None:
        # 停机：先叫停挂单任务，再撤掉还挂着的单，并把每个非零仓位挂 maker 平掉。
        for event in self._stops.values():
            event.set()
        for job in self._jobs.values():
            if not job.done():
                job.cancel()
        for job in self._jobs.values():
            with contextlib.suppress(BaseException):
                await job
        with contextlib.suppress(Exception):
            await self.shutdown_flatten()
        await self.executor.aclose()

    async def shutdown_flatten(self) -> list[dict[str, Any]]:
        """进程退出：撤挂单，每个还开着的仓位挂 post-only 退出。

        Arcus 不吃单（吃单费 2.25 bp 是漏损）。Lighter 同样挂 maker。
        仓位还在就撤掉没成交的退出单、按新的买一/卖一再挂，直到平完或等到挂单时限。
        """
        try:
            markets = await self.service.client.common_markets()
        except Exception:  # noqa: BLE001
            markets = []
        report: list[dict[str, Any]] = []
        for market in markets:
            with contextlib.suppress(Exception):
                await self.executor.cancel_resting_for_shutdown(market)
        deadline = time.monotonic() + max(5.0, float(self.settings.maker_wait_seconds))
        while True:
            legs = await self._shutdown_open_legs(markets)
            if not legs:
                break
            if not self.dry_run and time.monotonic() >= deadline:
                for market, venue, size, _decimals, _price in legs:
                    asset = str(market.get("asset") or market.get("lighter_symbol") or "?")
                    self.store.log_cycle(
                        asset, "shutdown_left",
                        f"停机时 {venue} 还剩 {size:g}，maker 退出没在时限内成交，不改吃单",
                        urgent=True, dry_run=self.dry_run,
                    )
                break
            for market, venue, size, decimals, price in legs:
                leg = await self.executor.flatten_orphan(
                    market=market, venue=venue, size=size, price=price,
                    slippage_bps=float(self.settings.order_slippage_bps),
                    lighter_decimals=decimals, entry_price=price,
                )
                asset = str(market.get("asset") or market.get("lighter_symbol") or "?")
                report.append({
                    "asset": asset, "venue": venue, "size": size,
                    "side": leg.side, "ok": leg.ok, "price": leg.price,
                    "post_only": True,
                })
                self.store.log_cycle(
                    asset, "shutdown_flatten",
                    f"停机：{venue} {leg.side or ''} {abs(size):g} 挂 maker 退出，不吃单",
                    dry_run=self.dry_run,
                    result={"ok": leg.ok, "error": leg.error, "price": leg.price},
                )
            if self.dry_run:
                break
            # 只挂一次。刮单还在等成交时不再撤掉重挂，也不再记一笔失败。
            deadline_left = deadline - time.monotonic()
            while deadline_left > 0:
                if not await self._shutdown_open_legs(markets):
                    break
                await asyncio.sleep(min(0.35, deadline_left))
                deadline_left = deadline - time.monotonic()
            break
        return report

    async def _shutdown_open_legs(
        self, markets: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], str, float, tuple[int, int], float]]:
        found = []
        for market in markets:
            try:
                lighter, arcus = await self.executor.read_positions(
                    market["lighter_symbol"], int(market["arcus_market_id"]),
                )
                decimals = await self._lighter_decimals(market)
            except Exception:  # noqa: BLE001
                continue
            for venue, size in (("lighter", lighter), ("arcus", arcus)):
                try:
                    size_f = float(size)
                except (TypeError, ValueError):
                    continue
                if size_f != size_f or abs(size_f) <= 1e-9:
                    continue
                price = await self._shutdown_touch(market, venue, size_f)
                found.append((market, venue, size_f, decimals, price))
        return found

    async def _shutdown_touch(self, market: dict[str, Any], venue: str, size: float) -> float:
        side = "sell" if size > 0 else "buy"
        try:
            _bid, _ask, join = await self.executor._touch(market, venue, side)
        except Exception:  # noqa: BLE001
            return 0.0
        return float(join) if join else 0.0

    # ── 一个周期 ────────────────────────────────────
    async def run_cycle(self) -> list[dict[str, Any]]:
        async with self._lock:
            return await self._run_cycle()

    async def _run_cycle(self) -> list[dict[str, Any]]:
        now = time.time()
        snapshot = await self.service.refresh()
        rows = {r["asset"]: r for r in snapshot.get("rows", [])}
        markets = {m["asset"]: m for m in await self.service.client.common_markets()}
        mmf_by_market = {
            int(m["arcus_market_id"]): float(m["arcus_mmf"])
            for m in markets.values() if m.get("arcus_mmf") and m.get("arcus_market_id") is not None
        }
        decisions: list[dict[str, Any]] = []
        # 任何一边的账户读取失败，那一边的腿在快照里就是「空」—— 和真的空仓分不出来。
        # 这时候按快照做决定，会把一条健康的腿当成孤腿市价平掉（另一边反而成了裸腿），
        # 或者把还在的仓位当成「已经不在了」清掉状态。所以这一轮什么都不做，等下一轮。
        account_errors = (snapshot.get("accounts") or {}).get("errors") or {}
        if not account_errors and not self.dry_run:
            self._rebase_on_composition_change(rows)

        for task_row in self.store.list_tasks():
            asset = task_row["asset"]
            job = self._jobs.get(asset)
            if job is not None and not job.done():
                # 挂单任务还在跑：仓位正在一点点建 / 平，这时候不能拿它做判断
                # （半建好的仓位看起来就是「两腿不匹配」）。任务自己负责兜底。
                # 例外：补仓任务可能一直在跑，风控不能因此停摆 —— 每一轮照样看
                # 强平距离 / 到期 / 停用，需要处理就叫停补仓，等它收尾后按最新仓位走正常流程。
                why = None
                if self._job_kind.get(asset) == "topup" and not account_errors:
                    why = self._topup_should_stop(task_row, rows.get(asset), now)
                if why is None:
                    kind = "挂单补仓中" if self._job_kind.get(asset) == "topup" else "挂单任务进行中"
                    decisions.append({"asset": asset, "plan": "maker_busy",
                                      "reason": f"{kind}（Arcus 成交多少立刻在 Lighter 对冲多少）",
                                      "urgent": False, "result": None})
                    continue
                self._stops[asset].set()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(job), TOPUP_STOP_WAIT_SEC)
                if not job.done():
                    decisions.append({"asset": asset, "plan": "maker_busy",
                                      "reason": f"已叫停补仓（{why}），等它收尾",
                                      "urgent": False, "result": None})
                    continue
                self.store.log_cycle(asset, "topup_stop", f"叫停补仓：{why}",
                                     dry_run=self.dry_run)
                # 收尾期间仓位变了：重读一次再判断
                fresh = await self.service.refresh()
                if (fresh.get("accounts") or {}).get("errors"):
                    continue
                for r in fresh.get("rows", []):
                    if r["asset"] == asset:
                        rows[asset] = r
                task_row = self.store.get_task(asset) or task_row
            row = rows.get(asset)
            market = markets.get(asset)
            if row is None or market is None:
                decisions.append({"asset": asset, "plan": "blocked",
                                  "reason": "该币种当前不在两所共有列表里"})
                continue
            if account_errors and not self.dry_run:
                reason = (
                    f"{'、'.join(sorted(account_errors))} 账户读取失败，持仓状态未知"
                    f" —— 本轮不做任何动作，下一轮重试"
                )
                decisions.append({"asset": asset, "plan": "blocked", "reason": reason})
                self.store.log_cycle(asset, "blocked", reason, dry_run=self.dry_run)
                continue

            health = self._health_for(asset, row)
            corridor_at_open = task_row.get("corridor_at_open")
            # 演练模式下不会产生真实仓位，但调度器是按【真实仓位】判断
            # 有没有持仓的 —— 不补这一步，每一轮都会认为自己空仓而重复开仓，
            # 「持仓 → 到期 → 平仓」这条链就永远验不到。
            if self.dry_run:
                simulated = self._simulated_health(task_row, asset, price_hint=row.get("mark_price"))
                if simulated is not None:
                    health = simulated
            elif task_row.get("opened_at") and not health.open_legs:
                # 实盘下库里记着有仓、两边却都读不到：仓位已经不在了
                # （手动平掉 / 被强平 / 当初那次开仓其实没成）。
                # 必须在【决策之前】清掉，否则调度器看到「空仓」会当场又开一个新仓，
                # 陈旧状态被覆盖、冷却期也永远不生效。清掉后冷却自然接管。
                self.store.mark_closed(asset)
                task_row = self.store.get_task(asset) or task_row
                corridor_at_open = None
            elif (task_row.get("enabled") and not task_row.get("opened_at")
                    and health.is_hedged):
                # 有一对对冲好的仓位，库里却没有开仓记录（手动开的 / 回撤平仓失败 /
                # 以前被误清掉的）。不接管的话调度器只会一直「闲置」，这对仓位
                # 永远不会被轮换。接管 = 从现在开始计时，按任务设置抽一个持有时长。
                hold = draw_hold_hours(
                    float(task_row.get("rotation_hours") or 4.0),
                    task_row.get("rotation_hours_max"), self.rng,
                )
                lighter_leg = health.lighter
                direction = (
                    "long_lighter_short_arcus"
                    if lighter_leg is not None and lighter_leg.size > 0
                    else "short_lighter_long_arcus"
                )
                self.store.mark_opened(
                    asset, direction=direction,
                    quantity=abs(lighter_leg.size) if lighter_leg else 0.0,
                    corridor_pct=None, hold_hours=hold,
                )
                task_row = self.store.get_task(asset) or task_row
                self.store.log_cycle(
                    asset, "adopt",
                    f"接管已有对冲仓位，从现在开始计时（本轮持有 {hold:.2f} 小时）",
                    dry_run=self.dry_run,
                )
            risk = evaluate(
                health,
                corridor_at_open_pct=corridor_at_open,
                min_corridor_pct=self.settings.close_corridor_pct,
            )

            # 开仓所需的数量与预检
            price = self._mark_for(health, row)
            leverage = float(task_row["leverage"] or 1.0)
            quantity, pre = 0.0, None
            available: tuple[float, float] | None = None
            leverages: tuple[int, int] | None = None
            sizing: str | None = None
            if not health.open_legs and price:
                lighter_avail, arcus_avail, arcus_acct = await self._available(with_arcus=True)
                available = (lighter_avail, arcus_avail)
                # Arcus 全仓：走廊看账户权益和账户里已有的 Arcus 仓位
                arcus_equity, other_notional, other_mm = account_exposure(
                    arcus_acct, mmf_by_market, exclude_market=int(market["arcus_market_id"]))
                # 两边真正会设的杠杆（任务值，各自不超过该所上限）。
                # 数量和保证金检查都按它算 —— 按任务值算而交易所实际用的是别的杠杆，
                # 就是 2026-09-19 SNDK 只开出一半的原因。
                leverages = effective_leverages(
                    leverage, arcus_max=float(market["max_leverage"]),
                    lighter_max=await self._lighter_max_leverage(market),
                )
                quantity = max_quantity_for(
                    leverage=leverage, price=price,
                    lighter_available=lighter_avail, arcus_available=arcus_avail,
                    quantity_decimals=market["quantity_decimals"],
                    lighter_leverage=leverages[0], arcus_leverage=leverages[1],
                )
                if task_row.get("notional_usdc"):
                    # 名义上限截断后【必须重新对齐到精度档位】。
                    # 200/1588.7 = 0.12588909… 这种数会被交易所直接拒单 ——
                    # 历史数据里「Lighter 数量最多支持 4 位小数」出现过 2433 次，
                    # 就是这类没对齐的数量。max_quantity_for 内部已经对齐过，
                    # 但截断这一步会把它破坏掉。
                    capped = float(task_row["notional_usdc"]) / price
                    quantity = align_quantity(
                        min(quantity, capped), int(market["quantity_decimals"])
                    )
                # 数量是被谁卡住的（名义上限 / 哪一边的可用保证金）—— 写进开仓记录
                sizing = sizing_limit(
                    price=price, lighter_available=lighter_avail,
                    arcus_available=arcus_avail,
                    lighter_leverage=leverages[0], arcus_leverage=leverages[1],
                    notional_cap=task_row.get("notional_usdc"),
                )
                pre = pre_open_check(
                    leverage=leverage, max_leverage=float(market["max_leverage"]),
                    quantity=quantity, price=price,
                    min_quantity=market["min_base_quantity"],
                    lighter_available=lighter_avail, arcus_available=arcus_avail,
                    min_corridor_pct=self.settings.min_corridor_pct,
                    lighter_leverage=leverages[0], arcus_leverage=leverages[1],
                    maintenance_fraction=market.get("arcus_mmf"),
                    min_notional=market.get("min_notional"),
                    arcus_equity=arcus_equity,
                    arcus_other_notional=other_notional,
                    arcus_other_maintenance=other_mm,
                )

            choice = self._choice_for(row)
            state = TaskState(
                asset=asset,
                enabled=bool(task_row["enabled"]),
                rotation_hours=float(task_row["rotation_hours"] or 4.0),
                rotation_hours_max=task_row.get("rotation_hours_max"),
                hold_hours=task_row.get("hold_hours"),
                leverage=leverage,
                opened_at=task_row.get("opened_at"),
                corridor_at_open_pct=corridor_at_open,
                last_closed_at=task_row.get("last_closed_at"),
                cooldown_seconds=self.settings.reopen_cooldown_seconds,
            )
            decision = decide(
                task=state, health=health, risk=risk, choice=choice,
                pre_open=pre, quantity=quantity, now=now,
            )
            # 3 秒是两腿都成交之后的最短持有，不是开仓两腿的间隔。
            # 满 300 秒仍持仓就强制平，不再等价差。风控 urgent 不改。
            decision = self._hold_clock(
                decision, task_row.get("opened_at"), now, bool(health.open_legs),
                _net_unrealized(health),
            )
            topup_note = None
            if decision.plan == "idle" and health.open_legs:
                topup, topup_note = await self._topup_decision(
                    task_row, market, health, risk, state, price, mmf_by_market, now)
                if topup is not None:
                    decision = topup
            # 随机轮换：在【开仓这一刻】抽一次持有时长并落库，之后不再重抽
            hold = (
                draw_hold_hours(state.rotation_hours, state.rotation_hours_max, self.rng)
                if decision.plan == "open" else None
            )
            reason = decision.reason
            if topup_note:
                reason = f"{reason}；{topup_note}"
            # 数量为 0 时把两边余额写进理由 —— 光说「数量 0」没法排查
            if decision.plan == "blocked" and available and quantity <= 0:
                reason = (
                    f"{reason}（Lighter 可用 {available[0]:.2f} / "
                    f"Arcus 可用 {available[1]:.2f} USDC，价格 {price or 0:.4f}）"
                )
            if decision.plan != "flatten_orphan":
                self._scratch_posted.pop(asset, None)
            else:
                prev = self._scratch_posted.get(asset)
                size = float(decision.orphan_size or 0)
                if (
                    prev is not None and prev[0] == decision.orphan_venue
                    and prev[1] != 0 and size != 0 and (prev[1] > 0) == (size > 0)
                ):
                    decisions.append({
                        "asset": asset, "plan": "idle",
                        "reason": "单腿刮单已挂在成交价，不再重复下单",
                        "urgent": False, "result": None,
                    })
                    continue
            if self._runs_as_maker(decision):
                # 挂单要等成交（最多 MAKER_WAIT_SECONDS 秒）：放到后台跑，
                # 引擎照常每 20 秒检查其它币种的风控，不被这一个币拖住
                stop = asyncio.Event()
                self._stops[asset] = stop
                self._job_kind[asset] = decision.plan
                self._jobs[asset] = asyncio.ensure_future(self._maker_job(
                    decision, market, health, row, price, pre, hold, leverages,
                    available, sizing, reason, stop,
                ))
                decisions.append({"asset": asset, "plan": "maker_started",
                                  "reason": f"{reason}（挂单进行中）", "urgent": False,
                                  "result": None})
                continue
            result = await self._apply(decision, market, health, row, price, pre,
                                       hold_hours=hold, leverages=leverages)
            decisions.append(self._finish(asset, market, decision, result, reason,
                                          available, sizing))

        self.last_cycle_at = now
        self.last_decisions = decisions
        return decisions

    def _finish(self, asset: str, market: dict[str, Any], decision: CycleDecision,
                result: PairResult | None, reason: str,
                available: tuple[float, float] | None, sizing: str | None) -> dict[str, Any]:
        """把一次动作的结果落库（执行记录 + 成交账本），返回给面板的决策记录。"""
        if (decision.plan == "topup" and result is not None
                and result.stage == "maker_not_filled"):
            # 补仓这一轮没等到成交：什么都没发生，不写记录（否则每两分钟一条「未成交」）
            return {"asset": asset, "plan": "maker_wait", "reason": result.reason,
                    "urgent": False, "result": result.to_dict()}
        if (result is not None and decision.plan == "open" and available
                and isinstance(result.quotes, dict)):
            # 开仓当时两边的可用余额一并落库 —— 数量看着不对时不用再去倒推
            result.quotes["lighter_available"] = round(available[0], 4)
            result.quotes["arcus_available"] = round(available[1], 4)
            result.quotes["sizing"] = sizing
        record = {
            "asset": asset, "plan": decision.plan, "reason": reason,
            "urgent": decision.urgent, "result": result.to_dict() if result else None,
            "lighter_available": available[0] if available else None,
            "arcus_available": available[1] if available else None,
        }
        if decision.is_action or decision.plan == "blocked":
            # 记【结果】，不是记【决策】。
            # 2026-09-18：实盘四次开仓全部零成交，日志却都写着「开仓 …」，
            # 看起来像成功 —— 真正的失败原因埋在 result 里没人看得到。
            logged_plan, logged_reason = decision.plan, reason
            if result is not None and isinstance(result.quotes, dict):
                # 价差闸门可能改了方向。面板原文还是资金费方向，对不上实际下出去的边。
                label = result.quotes.get("direction_label")
                if label:
                    for other in ("Lighter 多 / Arcus 空", "Lighter 空 / Arcus 多"):
                        if other != label and other in reason:
                            reason = reason.replace(other, label, 1)
                            logged_reason = reason
                            break
            if result is not None and decision.is_action:
                if result.stage == "maker_not_filled":
                    # 挂单没等到成交、已撤单：没花钱，不是失败
                    logged_plan, logged_reason = "maker_wait", result.reason or "挂单未成交"
                elif result.stage == "spread_wait":
                    # 价差闸门没过：还没下单，不是开仓失败
                    logged_plan, logged_reason = "spread_wait", result.reason or "价差过宽，不开仓"
                elif result.stage == "close_wait":
                    logged_plan, logged_reason = "close_wait", result.reason or "按各边自己的价格估算差于浮盈亏差额，先不平"
                elif result.stage == "legs_timeout":
                    logged_plan, logged_reason = "maker_wait", result.reason or "两腿未都成交，已撤单"
                elif result.stage in ("maker_exit", "flat") or decision.plan == "flatten_orphan":
                    logged_plan = "maker_exit"
                    logged_reason = result.reason or "按成交价挂 post-only 刮单，不吃单"
                elif not result.ok:
                    logged_plan = f"{decision.plan}_failed"
                    logged_reason = f"{result.stage}：{result.reason or '未成功'}"
                elif result.stage == "opened":
                    logged_reason = f"已开仓 — {reason}"
                elif result.stage == "closed":
                    line = ""
                    if isinstance(result.quotes, dict):
                        line = str(result.quotes.get("close_prices") or "")
                    logged_reason = f"已平仓 — {reason}"
                    extra = _close_price_line(result)
                    if extra and extra not in logged_reason:
                        logged_reason += f"；{extra}"
                        record["reason"] = f"{record['reason']}；{extra}"
                elif result.stage == "topped_up":
                    logged_reason = f"已补仓 — {reason}"
                if result.notes and result.quotes.get("maker"):
                    logged_reason += "；" + "；".join(result.notes)
                # 开仓记录里写上两边实际设的杠杆，方便和交易所页面对照
                lev = (result.quotes.get("lighter_leverage"),
                       result.quotes.get("arcus_leverage"))
                if result.ok and result.stage in ("opened", "dry_run") and all(lev):
                    why = f"；{sizing}" if sizing else ""
                    logged_reason += f"（杠杆 Lighter {lev[0]}× / Arcus {lev[1]}×{why}）"
            cycle_id = self.store.log_cycle(
                asset, logged_plan, logged_reason, urgent=decision.urgent,
                dry_run=self.dry_run, result=result.to_dict() if result else None,
            )
            if result is not None:
                self._record_fills(asset, market, result, cycle_id)
        return record

    def _runs_as_maker(self, decision: CycleDecision) -> bool:
        # 演练不睡眠，留在本轮里。风控平仓是 urgent，当场吃单。
        # 浮盈亏到了或持满之后的平仓都挂 maker，放到后台。
        # 没有 place_maker_pair 的替身仍在本轮里调用，避免把「先设杠杆再下单」拆开。
        if self.dry_run or decision.urgent:
            return False
        waiting = decision.plan in ("open", "topup") or (
            decision.plan == "close" and decision.maker_ok)
        if not waiting:
            return False
        if self.settings.arcus_maker:
            return True
        return hasattr(self.executor, "place_maker_pair")

    async def _maker_job(self, decision, market, health, row, price, pre, hold,
                         leverages, available, sizing, reason, stop=None) -> None:
        asset = market["asset"]
        try:
            result = await self._apply(decision, market, health, row, price, pre,
                                       hold_hours=hold, leverages=leverages, stop=stop)
            self._finish(asset, market, decision, result, reason, available, sizing)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 后台任务出错也要留下记录
            with contextlib.suppress(Exception):
                self.store.log_cycle(asset, "error", f"挂单任务出错：{exc}"[:300],
                                     dry_run=self.dry_run)

    def busy_assets(self) -> list[str]:
        return [a for a, t in self._jobs.items() if not t.done()]

    def _record_fills(self, asset: str, market: dict[str, Any],
                      result: PairResult, cycle_id: int | None) -> None:
        """把这一次动作里真正发出去的每一条腿记进成交账本。

        记账失败绝不能影响交易 —— 账本是统计用的，仓位才是真的。
        """
        try:
            for row in ledger_rows_for_result(
                asset, result, dry_run=self.dry_run,
                market_id=market.get("lighter_market_index"), cycle_id=cycle_id,
            ):
                self.store.record_fill(**row)
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                self.store.log_cycle(asset, "error", f"成交记账失败：{exc}"[:300],
                                     dry_run=self.dry_run)

    # ── 执行 ────────────────────────────────────────
    async def _apply(
        self, decision: CycleDecision, market: dict[str, Any],
        health: HedgeHealth, row: dict[str, Any], price: float | None,
        pre: Any = None, *, hold_hours: float | None = None,
        leverages: tuple[int, int] | None = None, stop: asyncio.Event | None = None,
    ) -> PairResult | None:
        asset = market["asset"]
        slippage = self.settings.order_slippage_bps
        decimals = await self._lighter_decimals(market)
        if price is None:
            price = 0.0

        if decision.plan == "flatten_orphan":
            venue = decision.orphan_venue or "lighter"
            entry = None
            if venue == "lighter" and health.lighter is not None:
                entry = health.lighter.entry_price
            elif venue == "arcus" and health.arcus is not None:
                entry = health.arcus.entry_price
            leg = await self.executor.flatten_orphan(
                market=market, venue=venue,
                size=decision.orphan_size, price=float(entry or price or 0), slippage_bps=slippage,
                lighter_decimals=decimals, entry_price=entry or price,
            )
            self._scratch_posted[asset] = (venue, float(decision.orphan_size or 0))
            return PairResult(
                leg.ok, "flatten_orphan", reason=decision.reason, dry_run=self.dry_run,
                lighter=leg if leg.venue == "lighter" else None,
                arcus=leg if leg.venue == "arcus" else None,
                notes=["已按成交价挂 post-only 刮单，不吃单" if leg.ok else "刮单没挂上，不改吃单，也不再循环重试"],
                ledger=[("orphan", leg)],
            )

        if decision.plan == "close":
            lighter = health.lighter.size if health.lighter else 0.0
            arcus = health.arcus.size if health.arcus else 0.0
            if abs(lighter) < 1e-12 and abs(arcus) < 1e-12:
                # 演练模式下本来就没有真实仓位，直接推进状态即可
                self.store.mark_closed(asset)
                return None
            lighter_side, arcus_side = closing_sides(lighter, arcus)
            lighter_entry = health.lighter.entry_price if health.lighter else None
            arcus_entry = health.arcus.entry_price if health.arcus else None
            books = await self._books(market)
            lighter_px = arcus_px = price
            if books is not None:
                lp, ep = executable_prices(
                    books[0], books[1], lighter_side=lighter_side,
                    arcus_side=arcus_side, quantity=max(abs(lighter), abs(arcus)),
                )
                # 平仓拿不到盘口时退回标记价 —— 平仓不能因为取不到价就不做
                lighter_px = lp or price
                arcus_px = ep or price
            # 计划内平仓：买价低于卖价就挂 maker（宽度不限）。不利则继续持有。
            # 风控触发的平仓 urgent，不进这个闸门，下面仍立刻吃单。
            if decision.maker_ok and not decision.urgent:
                return await self._gated_maker_close(
                    decision, market, lighter, arcus, lighter_side, arcus_side,
                    books, decimals, slippage, stop,
                    lighter_entry=lighter_entry, arcus_entry=arcus_entry,
                )
            result = await self.executor.close_pair(
                market=market, lighter_size=lighter, arcus_size=arcus,
                lighter_price=lighter_px, arcus_price=arcus_px,
                slippage_bps=slippage, lighter_decimals=decimals,
            )
            if result.ok:
                self.store.mark_closed(asset)
            return result

        if decision.plan in ("open", "topup"):
            topup = decision.plan == "topup"
            # 每条腿的限价必须来自【该所自己的盘口】，而且是按下单数量
            # 走完档位的 VWAP。共用一个标记价在原理上就是错的：
            # 两所之间有基差，滑点预算一旦盖不住它，单子就永远够不到对手价。
            lighter_side, arcus_side = hedge_side(decision.direction or "")
            # 先设杠杆、再取盘口、再下单。设不上就不开 —— 数量是按这个杠杆算的，
            # 交易所实际用别的杠杆，保证金和强平走廊就全对不上了。
            # 放在取盘口之前：设杠杆要一两秒，别让它夹在报价和下单中间。
            if leverages is not None:
                ok, error = await self.executor.set_leverage(
                    market, lighter_leverage=leverages[0], arcus_leverage=leverages[1],
                )
                if not ok:
                    return PairResult(
                        False, "leverage_failed", reason=error, dry_run=self.dry_run,
                        quotes={"lighter_leverage": leverages[0],
                                "arcus_leverage": leverages[1]},
                        notes=["尚未下任何单"],
                    )
            books = await self._books(market)
            if books is None:
                return PairResult(
                    False, "no_book", reason="拿不到两边盘口，本轮不开",
                    dry_run=self.dry_run,
                )
            lighter_book, arcus_book = books
            lighter_px, arcus_px = executable_prices(
                lighter_book, arcus_book,
                lighter_side=lighter_side, arcus_side=arcus_side,
                quantity=decision.quantity,
            )
            quotes = {
                "lighter_best_bid": lighter_book.best_bid,
                "lighter_best_ask": lighter_book.best_ask,
                "arcus_best_bid": arcus_book.best_bid,
                "arcus_best_ask": arcus_book.best_ask,
                "lighter_vwap": lighter_px,
                "arcus_vwap": arcus_px,
                "basis_bps": basis_bps(lighter_book, arcus_book),
                "shared_mark_was": price,
            }
            if leverages is not None:
                quotes["lighter_leverage"], quotes["arcus_leverage"] = leverages
            if lighter_px is None or arcus_px is None:
                short = "Lighter" if lighter_px is None else "Arcus"
                return PairResult(
                    False, "insufficient_depth",
                    reason=f"{short} 盘口深度不足以吃下 {decision.quantity:g}，本轮不开",
                    dry_run=self.dry_run, quotes=quotes,
                )
            # 基差闸门：两所是按代号自动配对的，同名不等于同一个标的
            # （或者一边是 1000 倍合约）。中价差出这么多，宁可不开。
            basis = quotes.get("basis_bps")
            if basis is None or abs(basis) > MAX_OPEN_BASIS_BPS:
                return PairResult(
                    False, "basis_too_wide",
                    reason=(
                        f"两所中价相差 {basis:+.0f} bps（上限 {MAX_OPEN_BASIS_BPS:g}），"
                        f"疑似不是同一个标的或一边盘口异常，本轮不开"
                        if basis is not None else "有一边盘口缺买一或卖一，本轮不开"
                    ),
                    dry_run=self.dry_run, quotes=quotes, notes=["尚未下任何单"],
                )
            # 方向沿用任务里已经定好的对冲方向。不要求买更便宜的一边。
            # 所间价差（例如 Arcus 80000、Lighter 80001）不拦开仓。
            from .funding import DIRECTION_LABELS
            quotes["direction"] = decision.direction
            quotes["direction_label"] = DIRECTION_LABELS.get(decision.direction or "")
            from .arcus import passive_price
            arcus_join = passive_price(
                market, arcus_side, arcus_book.best_bid or 0, arcus_book.best_ask or 0,
            )
            # 两边都挂 maker：买跟买一，卖跟卖一。不再用 Lighter 的吃单价过闸。
            lighter_join = join_price(lighter_book, lighter_side)
            if arcus_join is None or lighter_join is None:
                return PairResult(
                    False, "spread_wait", reason="挂单价算不出来（盘口交叉或缺档），本轮不下单",
                    dry_run=self.dry_run, quotes=quotes, notes=["尚未下任何单"],
                )
            quotes["lighter_join"] = lighter_join
            quotes["arcus_join"] = float(arcus_join)
            quotes["enforce_cheap_side"] = False
            # 所间价差不拦。两边 maker 一起发。
            if topup:
                # 补仓也要过同一个价差闸门，两边都挂 maker
                result = await self._place_gated(
                    market, decision, lighter_side, arcus_side, lighter_join,
                    float(arcus_join), decimals, quotes, stop, action="topup",
                )
                if not result.ok:
                    return result
                added = float((result.quotes or {}).get("filled_quantity") or 0.0)
                task = self.store.get_task(asset) or {}
                self.store.upsert_task(
                    asset, open_quantity=float(task.get("open_quantity") or 0.0) + added)
                result.stage = "topped_up"
                # 仓位变大，全仓账户里各任务的强平距离都会机械地变近：下一轮重设基准
                self._rebase_note = f"{asset} 补仓 {added:g}"
                return result
            result = await self._place_gated(
                market, decision, lighter_side, arcus_side, lighter_join,
                float(arcus_join), decimals, quotes, stop, action="open",
            )
            if not result.ok:
                return result
            # 挂单模式可能只开出一部分：按实际对冲上的数量记
            opened_quantity = float(
                (result.quotes or {}).get("filled_quantity") or decision.quantity)
            # 开仓后必须用真实强平价复核走廊 —— 估算可能偏乐观。
            #
            # 但「读不到强平价」和「确认走廊太窄」是两回事，不能混为一谈：
            # Lighter 是 rollup，开仓后账户接口要几秒才反映出新仓位。
            # 2026-09-18 原本只等 1 秒就盲平，白付一轮往返成本，
            # 而且盲平本身也可能失败。
            # 现在：重试若干轮；确认太窄才回撤；始终读不到就【保留仓位】，
            # 交给每 20 秒一轮的风控循环去判断（它有绝对下限和孤腿抢救兜底）。
            if not self.dry_run:
                check = None
                for attempt in range(_VERIFY_ATTEMPTS):
                    await asyncio.sleep(_VERIFY_DELAY_SEC * (attempt + 1))
                    fresh = await self.service.refresh()
                    fresh_row = next(
                        (r for r in fresh.get("rows", []) if r["asset"] == asset), None
                    )
                    if fresh_row is None:
                        continue
                    fresh_health = self._health_for(asset, fresh_row)
                    candidate = verify_corridor_after_open(
                        fresh_health, self.settings.close_corridor_pct
                    )
                    # 读到了真实走廊（不管够不够宽）就采信，不再重试
                    if candidate.actual_corridor_pct is not None or candidate.ok:
                        check = candidate
                        break
                if check is None:
                    # 始终读不到：保留仓位，如实标注，让风控循环接管
                    self.store.mark_opened(
                        asset, direction=decision.direction or "",
                        quantity=opened_quantity, corridor_pct=None,
                        hold_hours=hold_hours, target_quantity=decision.quantity,
                    )
                    result.notes.append(
                        f"开仓后 {_VERIFY_ATTEMPTS} 次都读不到强平价，已保留仓位 —— "
                        f"下一轮风控会重新判断（绝对下限与孤腿抢救仍然有效）"
                    )
                    return result
                if True:
                    if not check.ok:
                        back_out = await self.executor.close_pair(
                            market=market,
                            lighter_size=fresh_health.lighter.size if fresh_health.lighter else 0.0,
                            arcus_size=fresh_health.arcus.size if fresh_health.arcus else 0.0,
                            lighter_price=price, arcus_price=price,
                            slippage_bps=slippage, lighter_decimals=decimals,
                        )
                        self.store.mark_closed(asset)
                        result.ok = False
                        result.stage = "corridor_too_narrow_backed_out"
                        result.reason = check.reason
                        # 把回撤平仓自己的结果也记下来 —— 不记的话
                        # 无从判断「平仓单发出去了」还是「真的平掉了」
                        result.rescue = back_out.lighter
                        result.quotes["backout"] = back_out.to_dict()
                        result.ledger.extend(("backout", leg) for _, leg in back_out.ledger)
                        result.notes.append(
                            "开仓后复核不过，已平掉" if back_out.ok
                            else "⚠ 复核不过且平仓失败 —— 仓位可能还在，下一轮风控接管"
                        )
                        return result
                    self.store.mark_opened(
                        asset, direction=decision.direction or "",
                        quantity=opened_quantity,
                        corridor_pct=check.actual_corridor_pct,
                        hold_hours=hold_hours, target_quantity=decision.quantity,
                    )
                    return result
            # 演练模式没有真实仓位可复核，退而记录预检的估算值，
            # 好让持仓期间的相对触发有个参照。
            self.store.mark_opened(
                asset, direction=decision.direction or "",
                quantity=opened_quantity,
                corridor_pct=getattr(pre, "estimated_corridor_pct", None),
                hold_hours=hold_hours, target_quantity=decision.quantity,
            )
            return result
        return None

    # ── 辅助 ────────────────────────────────────────
    def _hold_clock(
        self, decision: CycleDecision, opened_at: float | None, now: float, holding: bool,
        net_pnl: float | None = None,
    ) -> CycleDecision:
        """成交后的持仓时钟。开仓闸门不在这里，风控平仓也不在这里。

        opened_at 记的是两腿都成交、仓位记上的时刻。
        未满 MIN_HOLD_SEC：先不平。
        已满、未到 MAX_HOLD_SEC：两腿浮盈亏合计不低于 -浮盈亏差额就挂 maker 平。
        更差就继续持有。已满 MAX_HOLD_SEC：跟盘挂 maker 平。
        跟盘价亏过差额时也只继续挂 maker，不因为到点改吃单。
        """
        if decision.urgent or decision.plan == "flatten_orphan":
            return decision
        if not holding or not opened_at:
            return decision
        age = now - float(opened_at)
        min_hold = float(self.settings.min_hold_sec)
        max_hold = max(min_hold, float(self.settings.max_hold_sec))
        window = max(0.0, float(self.settings.pnl_close_usd))
        if age >= max_hold:
            net_text = "未知" if net_pnl is None else f"{net_pnl:+.4f}"
            return replace(
                decision, plan="close", urgent=False, maker_ok=True, force_close=True,
                reason=(
                    f"两腿成交后已持有 {age:.0f} 秒，达到最长持有 {max_hold:g} 秒，"
                    f"浮盈亏合计 {net_text} USDC，即使差于 -{window:g} 也强制平仓（跟盘挂 maker，不改吃单）"
                ),
            )
        if age < min_hold:
            if decision.plan == "close":
                return replace(
                    decision, plan="idle", urgent=False, maker_ok=False,
                    reason=(
                        f"两腿已成交，持有 {age:.1f} 秒，未满最短 {min_hold:g} 秒，先不平"
                    ),
                )
            if decision.plan == "idle":
                return replace(
                    decision,
                    reason=(
                        f"两腿已成交，持有 {age:.1f} 秒，未满最短 {min_hold:g} 秒，先不平"
                    ),
                )
            return decision
        if decision.plan in ("idle", "blocked", "close"):
            if net_pnl is not None and unrealized_close_ready(net_pnl, window):
                return replace(
                    decision, plan="close", urgent=False, maker_ok=True,
                    reason=(
                        f"持有 {age:.0f} 秒，已过最短 {min_hold:g} 秒，"
                        f"两腿浮盈亏合计 {net_pnl:+.4f} USDC，不低于 -{window:g}，挂 maker 平仓"
                    ),
                )
            shown = "未知" if net_pnl is None else f"{net_pnl:+.4f}"
            return replace(
                decision, plan="idle", urgent=False, maker_ok=False,
                reason=(
                    f"持有 {age:.0f} 秒，两腿浮盈亏合计 {shown} USDC，"
                    f"差于 -{window:g}，未到最长持有 {max_hold:g} 秒，先不平"
                ),
            )
        return decision

    async def _place_gated(
        self, market, decision, lighter_side, arcus_side, lighter_join, arcus_join,
        decimals, quotes, stop, *, action: str,
    ) -> PairResult:
        """价差闸门已经通过。默认两边挂 maker。

        ARCUS_MAKER 仍走原来的「Arcus 挂单、Lighter 立刻对冲」，但只有闸门通过才进得去。
        测试替身如果没有 place_maker_pair，就退回它已有的 open_pair。
        """
        # 实盘两边都挂 maker。有 place_maker_pair 就走它，不再让一边成交去 IOC 另一边。
        # 没有这个方法的测试替身才退回旧的挂单器或 open_pair。
        fn = getattr(self.executor, "place_maker_pair", None)
        if fn is None and self.settings.arcus_maker and not self.dry_run:
            return await self._maker(stop).open_pair(
                market=market, direction=decision.direction or "",
                quantity=decision.quantity,
                lighter_price=lighter_join, arcus_price=arcus_join,
                slippage_bps=self.settings.order_slippage_bps,
                lighter_decimals=decimals, quotes=quotes, action=action,
            )
        if fn is None:
            return await self.executor.open_pair(
                market=market, direction=decision.direction or "",
                quantity=decision.quantity, lighter_price=lighter_join,
                arcus_price=arcus_join, slippage_bps=self.settings.order_slippage_bps,
                lighter_decimals=decimals, quotes=quotes,
            )
        return await fn(
            market=market, lighter_side=lighter_side, arcus_side=arcus_side,
            lighter_quantity=decision.quantity, arcus_quantity=decision.quantity,
            lighter_price=lighter_join, arcus_price=arcus_join,
            lighter_decimals=decimals, quotes=quotes, reduce_only=False,
            action=action, stop=stop, direction=decision.direction,
            quantity=decision.quantity,
        )

    async def _gated_maker_close(
        self, decision, market, lighter, arcus, lighter_side, arcus_side,
        books, decimals, slippage, stop,
        lighter_entry: float | None = None, arcus_entry: float | None = None,
    ) -> PairResult:
        """计划内平仓：两边挂 maker。发单前用即将发出的价格重算往返，不单看标记浮盈亏。"""
        if books is None:
            return PairResult(
                False, "close_wait", reason="拿不到两边盘口，计划内平仓先等下一轮",
                dry_run=self.dry_run, notes=["尚未下任何单"],
            )
        from .arcus import passive_price
        lighter_book, arcus_book = books
        arcus_join = passive_price(
            market, arcus_side, arcus_book.best_bid or 0, arcus_book.best_ask or 0,
        )
        lighter_join = join_price(lighter_book, lighter_side)
        if arcus_join is None or lighter_join is None:
            return PairResult(
                False, "close_wait", reason="平仓挂单价算不出来，先不平",
                dry_run=self.dry_run,
                notes=["尚未下任何单"],
            )
        window = max(0.0, float(self.settings.pnl_close_usd))
        force = bool(getattr(decision, "force_close", False))
        quotes = {
            "lighter_join": lighter_join,
            "arcus_join": float(arcus_join),
            "pnl_close_usd": window,
            "force_close": force,
            "close_entries": {
                "lighter_size": lighter,
                "arcus_size": arcus,
                "lighter_entry": lighter_entry,
                "arcus_entry": arcus_entry,
            },
        }
        est = round_trip_close_net(
            lighter, lighter_entry, float(lighter_join),
            arcus, arcus_entry, float(arcus_join),
        )
        quotes["estimated_close_net"] = None if est is None else round(est, 6)
        if not force and (est is None or not unrealized_close_ready(est, window)):
            shown = "未知" if est is None else f"{est:+.4f}"
            return PairResult(
                False, "close_wait",
                reason=(
                    f"按各边自己的开仓价对上即将发出的平仓价，合计 {shown} USDC，"
                    f"差于 -{window:g}。所间价差不算这笔亏损。未到最长持有，先不平"
                ),
                dry_run=self.dry_run, quotes=quotes,
                notes=["尚未下任何单"],
            )
        quotes["close_favorable"] = True
        quotes["close_prices"] = (
            f"挂 maker 平仓：Lighter {lighter_side} {float(lighter_join):g} / "
            f"Arcus {arcus_side} {float(arcus_join):g}"
        )
        # 正常平仓两边都挂 maker。不要走「Arcus 一成交就 IOC 对冲 Lighter」。
        # 只有执行器没有 place_maker_pair 的测试替身才退回旧路径。
        fn = getattr(self.executor, "place_maker_pair", None)
        if fn is not None:
            result = await fn(
                market=market, lighter_side=lighter_side, arcus_side=arcus_side,
                lighter_quantity=abs(lighter), arcus_quantity=abs(arcus),
                lighter_price=lighter_join, arcus_price=float(arcus_join),
                lighter_decimals=decimals, quotes=quotes, reduce_only=True,
                action="close", stop=stop,
            )
        elif self.settings.arcus_maker and not self.dry_run:
            result = await self._maker(stop).close_pair(
                market=market, lighter_size=lighter, arcus_size=arcus,
                lighter_price=lighter_join, arcus_price=float(arcus_join),
                slippage_bps=slippage, lighter_decimals=decimals,
            )
        else:
            result = await self.executor.close_pair(
                market=market, lighter_size=lighter, arcus_size=arcus,
                lighter_price=lighter_join, arcus_price=float(arcus_join),
                slippage_bps=slippage, lighter_decimals=decimals,
            )
        if result.ok:
            if not isinstance(result.quotes, dict):
                result.quotes = {}
            # 执行器可能按刷新后的盘口改了价。日志用它实际拿去下单的那两个价。
            used_l = result.quotes.get("lighter_join", lighter_join)
            used_a = result.quotes.get("arcus_join", float(arcus_join))
            result.quotes["close_favorable"] = True
            result.quotes["lighter_join"] = used_l
            result.quotes["arcus_join"] = used_a
            result.quotes["close_prices"] = (
                f"挂 maker 平仓：Lighter {lighter_side} {float(used_l):g} / "
                f"Arcus {arcus_side} {float(used_a):g}"
            )
            line = result.quotes["close_prices"]
            if line not in result.notes:
                result.notes.append(line)
            self.store.mark_closed(market["asset"])
        return result

    def _maker(self, stop: asyncio.Event | None = None):
        from .maker import MakerExecutor
        return MakerExecutor(self.executor, wait_seconds=self.settings.maker_wait_seconds,
                             bell=self.bell, stop=stop)

    @staticmethod
    def _task_state(task_row: dict[str, Any], settings: Settings) -> TaskState:
        return TaskState(
            asset=task_row["asset"], enabled=bool(task_row["enabled"]),
            rotation_hours=float(task_row["rotation_hours"] or 4.0),
            rotation_hours_max=task_row.get("rotation_hours_max"),
            hold_hours=task_row.get("hold_hours"),
            leverage=float(task_row["leverage"] or 1.0),
            opened_at=task_row.get("opened_at"),
            corridor_at_open_pct=task_row.get("corridor_at_open"),
            last_closed_at=task_row.get("last_closed_at"),
            cooldown_seconds=settings.reopen_cooldown_seconds,
        )

    def _topup_should_stop(self, task_row: dict[str, Any], row: dict[str, Any] | None,
                           now: float) -> str | None:
        """补仓进行中，这一轮的风控要不要介入。要介入返回原因。"""
        if not task_row.get("enabled"):
            return "任务已停用"
        if row is None:
            return "该币种不在两所共有列表里了"
        if self._task_state(task_row, self.settings).is_due_to_close(now):
            return "已到轮换时间"
        health = self._health_for(task_row["asset"], row)
        distance = health.min_distance_pct
        if distance is not None and distance < self.settings.close_corridor_pct:
            return f"距强平仅剩 {distance:.2f}%"
        if health.is_hedged:
            # 两腿对得上时才看完整的风控裁决（补仓中间两腿差一点是正常的，不能当成不匹配）
            verdict = evaluate(health, corridor_at_open_pct=task_row.get("corridor_at_open"),
                               min_corridor_pct=self.settings.close_corridor_pct)
            if verdict.action != "none":
                return verdict.reason or "风控触发"
        return None

    async def _topup_decision(self, task_row, market, health, risk, state, price,
                              mmf_by_market, now) -> tuple[CycleDecision | None, str | None]:
        """持仓中、仓位没到这一轮的目标数量：挂单补仓（只在实盘挂单模式下）。

        返回（补仓决策, 面板上显示的补充说明）。
        """
        if (self.dry_run or not self.settings.arcus_maker or not state.enabled
                or risk.action != "none" or not health.is_hedged
                or not state.opened_at or not price):
            return None, None
        dec = int(market["quantity_decimals"])
        target = task_row.get("target_quantity")
        if not target and task_row.get("notional_usdc"):
            # 升级前开的仓 / 接管的仓没有记目标：按名义上限算
            target = align_quantity(float(task_row["notional_usdc"]) / price, dec)
        if not target:
            return None, None
        left = state.effective_hold_hours() * 3600 - (now - float(state.opened_at))
        if left < self.settings.maker_wait_seconds + TOPUP_MIN_LEFT_SEC:
            return None, None
        lighter_size = health.lighter.size if health.lighter else 0.0
        arcus_size = health.arcus.size if health.arcus else 0.0
        current = min(abs(lighter_size), abs(arcus_size))
        direction = ("long_lighter_short_arcus" if lighter_size > 0
                     else "short_lighter_long_arcus")
        if task_row.get("open_direction") and task_row["open_direction"] != direction:
            return None, None
        remaining = align_quantity(float(target) - current, dec)
        if remaining <= 0 or remaining < float(target) * TOPUP_MIN_FRACTION:
            return None, None

        lighter_avail, arcus_avail, arcus_acct = await self._available(with_arcus=True)
        leverage = float(task_row["leverage"] or 1.0)
        leverages = effective_leverages(
            leverage, arcus_max=float(market["max_leverage"]),
            lighter_max=await self._lighter_max_leverage(market),
        )
        room = max_quantity_for(
            leverage=leverage, price=price, lighter_available=lighter_avail,
            arcus_available=arcus_avail, quantity_decimals=dec,
            lighter_leverage=leverages[0], arcus_leverage=leverages[1],
        )
        remaining = align_quantity(min(remaining, room), dec)
        # 全仓走廊按【补完之后】的总仓位估：这个币已有的仓位当作账户里的「其它仓位」算进去
        # （公式里它和新增部分是对称的，等价于按总仓位算）
        equity, other, other_mm = account_exposure(
            arcus_acct, mmf_by_market, exclude_market=int(market["arcus_market_id"]))
        mmf = float(market.get("arcus_mmf") or 1.0 / (2.0 * float(market["max_leverage"])))
        here = current * price
        pre = pre_open_check(
            leverage=leverage, max_leverage=float(market["max_leverage"]),
            quantity=remaining, price=price, min_quantity=market["min_base_quantity"],
            lighter_available=lighter_avail, arcus_available=arcus_avail,
            min_corridor_pct=self.settings.min_corridor_pct,
            lighter_leverage=leverages[0], arcus_leverage=leverages[1],
            maintenance_fraction=market.get("arcus_mmf"),
            min_notional=market.get("min_notional"),
            arcus_equity=equity, arcus_other_notional=other + here,
            arcus_other_maintenance=other_mm + here * mmf,
        )
        if remaining <= 0 or not pre.ok:
            why = pre.reason if remaining > 0 else "可用保证金不够再加仓"
            return None, f"仓位 {current:g} / 目标 {float(target):g}，暂不补仓：{why}"
        return CycleDecision(
            "topup", f"补仓 {remaining:g}（当前 {current:g} / 目标 {float(target):g}）",
            direction=direction, quantity=remaining,
        ), None

    def _rebase_on_composition_change(self, rows: dict[str, Any]) -> None:
        """Arcus 账户里的仓位组合变了，就把各持仓任务的「开仓走廊」基准重设成当前值。

        为什么（2026-09-26 实盘）：ETH 开仓时离强平 15.62%，随后 QQQ、SPY 在同一个
        Arcus 全仓账户里开仓，ETH 的距离被【机械地】压到 7.13% —— 价格没动，
        只是账户里多了两笔仓位。「剩余距离掉到开仓时的一半就平」的规则因此触发，
        白白多平了一轮。组合一变就重设基准，相对触发只对价格变化响应；
        绝对平仓线（CLOSE_CORRIDOR_PCT）不受影响，照样兜底。
        """
        composition = frozenset(
            asset for asset, row in rows.items()
            if abs(float((((row.get("position") or {}).get("arcus")) or {}).get("size") or 0)) > 1e-12
        )
        previous = self._arcus_composition
        self._arcus_composition = composition
        note, self._rebase_note = self._rebase_note, None
        if previous is None or (previous == composition and not note):
            return
        added, removed = sorted(composition - previous), sorted(previous - composition)
        change = "、".join(
            [f"新增 {', '.join(added)}"] * bool(added) + [f"减少 {', '.join(removed)}"] * bool(removed)
            + [note] * bool(note))
        for task in self.store.list_tasks():
            asset = task["asset"]
            if not task.get("opened_at") or asset not in rows:
                continue
            distance = self._health_for(asset, rows[asset]).min_distance_pct
            old = task.get("corridor_at_open")
            if distance is None or (old is not None and abs(distance - float(old)) < 0.01):
                continue
            self.store.upsert_task(asset, corridor_at_open=distance)
            self.store.log_cycle(
                asset, "rebase",
                f"Arcus 账户仓位变了（{change}），强平距离基准从 "
                f"{float(old):.2f}% 重设为 {distance:.2f}%" if old is not None else
                f"Arcus 账户仓位变了（{change}），强平距离基准设为 {distance:.2f}%",
                dry_run=self.dry_run,
            )

    def _simulated_health(
        self, task_row: dict[str, Any], asset: str, price_hint: float | None
    ) -> HedgeHealth | None:
        """演练模式专用：按库里记的开仓状态伪造一份持仓。

        只在 dry_run 下调用。目的是让「持仓 → 到期 → 平仓 → 冷却」整条链
        在演练里真正跑一遍，而不是每轮都从空仓重新开始。
        强平价由开仓走廊反推，好让风控的相对触发也能被验到。
        """
        from .positions import LegPosition

        quantity = task_row.get("open_quantity")
        direction = task_row.get("open_direction")
        if not task_row.get("opened_at") or not quantity or not direction:
            return None
        price = price_hint or 0.0
        if price <= 0:
            return None
        corridor = float(task_row.get("corridor_at_open") or 0.0) / 100.0
        long_is_lighter = direction == "long_lighter_short_arcus"
        lighter_size = float(quantity) if long_is_lighter else -float(quantity)
        arcus_size = -lighter_size

        def leg(venue: str, size: float) -> LegPosition:
            if corridor > 0:
                liq = price * (1 - corridor) if size > 0 else price * (1 + corridor)
            else:
                liq = None
            return LegPosition(
                venue=venue, symbol=asset, size=size, entry_price=price,
                mark_price=price, liquidation_price=liq,
                unrealized_pnl=0.0, margin=None,
            )

        return HedgeHealth(
            asset=asset,
            lighter=leg("lighter", lighter_size),
            arcus=leg("arcus", arcus_size),
            warn_distance_pct=self.settings.close_corridor_pct,
        )

    async def _books(self, market: dict[str, Any]):
        """两边盘口。任一边失败返回 None —— 拿不到价就不该下单。"""
        lighter, arcus = await asyncio.gather(
            self.service.client.lighter_book(
                market["lighter_market_index"], market["lighter_symbol"]
            ),
            self.service.client.arcus_book(market["arcus_symbol"]),
            return_exceptions=True,
        )
        if isinstance(lighter, Exception) or isinstance(arcus, Exception):
            return None
        return lighter, arcus

    def _health_for(self, asset: str, row: dict[str, Any]) -> HedgeHealth:
        pos = row.get("position") or {}
        return HedgeHealth(
            asset=asset,
            lighter=_leg_from(pos.get("lighter"), "lighter", asset),
            arcus=_leg_from(pos.get("arcus"), "arcus", asset),
            warn_distance_pct=self.settings.close_corridor_pct,
        )

    @staticmethod
    def _mark_for(health: HedgeHealth, row: dict[str, Any]) -> float | None:
        for leg in health.open_legs:
            if leg.mark_price:
                return leg.mark_price
        return row.get("mark_price")

    @staticmethod
    def _choice_for(row: dict[str, Any]):
        from .funding import DirectionChoice
        if row.get("net_bps_per_hour") is None:
            return None
        return DirectionChoice(
            symbol=row["asset"], direction=row["direction"],
            net_bps_per_hour=row["net_bps_per_hour"],
            lighter_bps_per_hour=row["lighter_bps_per_hour"],
            arcus_bps_per_hour=row["arcus_bps_per_hour"],
            tradable=row["tradable"], reason=row.get("reason"),
        )

    async def _lighter_max_leverage(self, market: dict[str, Any]) -> float | None:
        """Lighter 这个市场的杠杆上限。查不到返回 None（按任务值设，交易所不收会拒）。"""
        try:
            details = await self.service.client.lighter_market_details(
                int(market["lighter_market_index"])
            )
        except Exception:  # noqa: BLE001 —— 查不到上限不该挡住整轮
            return None
        return details.get("max_leverage")

    async def _available(self, *, with_arcus: bool = False):
        lighter_acct, arcus_acct = await asyncio.gather(
            self.service.client.lighter_account(),
            self.service.client.arcus_account(),
            return_exceptions=True,
        )
        lighter = _lighter_available(lighter_acct, self.settings.lighter_account_index)
        arcus = _arcus_available(arcus_acct)
        if with_arcus:
            return lighter, arcus, (None if isinstance(arcus_acct, Exception) else arcus_acct)
        return lighter, arcus

    @staticmethod
    async def _lighter_decimals(market: dict[str, Any]) -> tuple[int, int]:
        """(数量精度, 价格精度) —— 都取 Lighter 自己的，不能用两所取小的那个。

        quantity_decimals 是两所取 min 后的下单精度；但对 Lighter 签名来说，
        要用它自己的 supported_size_decimals 去换算整数，混用会算错倍数。
        """
        size = int(market.get("lighter_size_decimals") or market.get("quantity_decimals") or 2)
        price = int(market.get("price_decimals") or 2)
        return size, price



def _net_unrealized(health) -> float | None:
    """两边都有仓、且浮盈亏都读到了，才返回合计。缺一边就不拿 0 去凑。"""
    legs = list(health.open_legs)
    if len(legs) < 2:
        return None
    if any(leg.unrealized_pnl is None for leg in legs):
        return None
    return float(sum(float(leg.unrealized_pnl) for leg in legs))


def _leg_from(data: dict[str, Any] | None, venue: str, asset: str):
    from .positions import LegPosition
    if not data:
        return None
    return LegPosition(
        venue=venue, symbol=asset, size=data.get("size") or 0.0,
        entry_price=data.get("entry_price"), mark_price=data.get("mark_price"),
        liquidation_price=data.get("liquidation_price"),
        unrealized_pnl=data.get("unrealized_pnl"), margin=data.get("margin"),
    )


def _lighter_available(payload: Any, index: int | None) -> float:
    """Lighter 可用保证金 —— 口径与 Parallax 一致。"""
    if isinstance(payload, Exception) or not isinstance(payload, dict) or index is None:
        return 0.0
    for row in payload.get("accounts") or []:
        idx = row.get("account_index", row.get("index"))
        if idx is None or int(idx) != int(index):
            continue
        for key in ("available_balance", "collateral", "total_asset_value"):
            try:
                value = float(row.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return 0.0


def _arcus_available(payload: Any) -> float:
    """Arcus 可用保证金（freeCollateral）—— 直接用首页余额那个函数，只有一份口径。"""
    if isinstance(payload, Exception):
        return 0.0
    balance = arcus_balance(payload)
    return float(balance["available"]) if balance else 0.0
