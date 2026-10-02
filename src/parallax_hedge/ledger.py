"""成交账本 —— 首页「交易量 / 手续费 / 磨损」的数据来源。

原则（照搬 Parallax 实盘验证过的做法）：

  1. 每发出一条腿就记一笔：哪个币、哪个所、什么动作、方向、请求量，
     以及订单号（Lighter 的 tx_hash / Arcus 的 orderId）和当下最好的估计值
     （仓位变化量、盘口参考价）。
     Lighter 靠 tx_hash 找到这张单的第一笔成交，再按其中的订单号收齐其余各档；
     Arcus 的 /v1/fills 每一笔都带 orderId，直接按它加总。
  2. 之后拿订单号去【交易所自己的成交记录】里对账，换成准确的成交价、
     成交量、手续费和已实现盈亏。交易所的记录是唯一的真相。
  3. 对不上的单子等一段时间；拿到了交易所数据但超时仍对不上，就按估计值
     定稿，并如实标成「估算」。交易所数据本身拿不到时【不定稿】，下次再试。
"""
from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass
from typing import Any

from . import arcus as ax
from .execution import LegResult, normalize_tx_hash

# 下单后等 2 分钟再对账，给交易所的成交记录留出落库时间。
# （v0.2.0 / v0.2.1 以为「0.2776 只对上 0.015」是记录还没落全，其实不是：
#  一张单吃了好几档时，只有第一笔成交带着我们的 tx_hash —— 见 match_lighter_trades。）
MATCH_GRACE_SEC = 120
GIVE_UP_AFTER_SEC = 30 * 60     # 交易所数据拿到了、超过这么久还对不全，就按估计值定稿
COMPLETE_FRACTION = 0.99        # 对上的成交量至少要到仓位实测量的 99% 才算对全了
LIGHTER_FEE_SCALE = 1_000_000   # Lighter 成交记录里 maker_fee / taker_fee 的单位（照搬 Parallax）
LIGHTER_MAX_PAGES = 10          # 每个市场最多翻 10 页 × 100 条
LIGHTER_PAGE_MARGIN_SEC = 300   # 翻页要翻过最早那张单之前 5 分钟，吸收两边的时钟偏差
ARCUS_FILLS_MARGIN_SEC = 300    # Arcus 成交按时间查，同样往前多留 5 分钟


@dataclass(frozen=True)
class VenueFill:
    """一张订单在交易所成交记录里的汇总。"""

    quantity: float
    price: float
    fee: float | None
    realized_pnl: float
    side: str | None
    matched: int


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def trade_time_seconds(row: dict[str, Any]) -> float | None:
    """成交时间统一换成秒。Lighter 的时间戳是整数，按量级判断是毫秒还是秒。"""
    for key in ("timestamp", "transaction_time", "time"):
        value = _f(row.get(key))
        if value and value > 0:
            if value > 1e14:        # 微秒
                return value / 1e6
            if value > 1e11:        # 毫秒
                return value / 1e3
            return value
    return None


# ── 交易所成交记录 → 单张订单 ────────────────────────────

def _lighter_our_side(row: dict[str, Any], account_index: int) -> str | None:
    """这笔成交里我们是卖方（"ask"）还是买方（"bid"）；都不是返回 None。"""
    try:
        if int(row.get("ask_account_id", -1)) == int(account_index):
            return "ask"
        if int(row.get("bid_account_id", -1)) == int(account_index):
            return "bid"
    except (TypeError, ValueError):
        return None
    return None


def _lighter_order_id(row: dict[str, Any], side: str) -> str | None:
    """我们这一边的订单号（Lighter 的 order index）。缺失或为 0 时返回 None ——
    绝不能拿 0 去匹配，否则所有缺字段的成交都会被当成同一张单。"""
    value = row.get(f"{side}_id_str")
    if value in (None, ""):
        value = row.get(f"{side}_id")
    if value in (None, "") or str(value).strip() in ("", "0"):
        return None
    return str(value).strip()


def match_lighter_trades(
    rows: list[dict[str, Any]] | None, tx_hash: str | None, account_index: int
) -> VenueFill | None:
    """把属于这张订单、且我们是买方或卖方的成交加总起来。

    先按 tx_hash 找到这张单的成交，从中取出我们这一边的订单号，再按订单号
    把这张单的【每一档】成交收齐。

    不能只按 tx_hash：一张单吃掉好几档时，只有第一笔成交带着下单那笔交易的
    tx_hash，后面几档带的是 Lighter 内部撮合交易的 hash。只按 tx_hash 就只能
    对上第一档 —— 实盘 62 张多档成交的单全是这样（SNDK 0.2776 只对上 0.015，
    价格正好是当时的买一），过了几个小时、翻完所有页也还是对不全。
    同一笔成交在翻页时出现两次，只算一次。
    """
    target = normalize_tx_hash(tx_hash)
    if not rows or not target:
        return None
    orders: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict) or normalize_tx_hash(row.get("tx_hash")) != target:
            continue
        side = _lighter_our_side(row, account_index)
        order_id = _lighter_order_id(row, side) if side else None
        if order_id:
            orders.add((side, order_id))

    quantity = notional = fees = pnl = 0.0
    fee_fields = matched = 0
    side_seen: str | None = None
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        side = _lighter_our_side(row, account_index)
        if side is None:
            continue
        same_tx = normalize_tx_hash(row.get("tx_hash")) == target
        same_order = (side, _lighter_order_id(row, side)) in orders
        if not (same_tx or same_order):
            continue
        trade_key = row.get("trade_id_str")
        if trade_key in (None, ""):
            trade_key = row.get("trade_id")
        if trade_key not in (None, ""):
            if str(trade_key) in seen:
                continue
            seen.add(str(trade_key))
        price = abs(_f(row.get("price")) or 0.0)
        size = abs(_f(row.get("size") or row.get("base_amount")) or 0.0)
        if price <= 0 or size <= 0:
            continue
        is_ask = side == "ask"
        matched += 1
        quantity += size
        notional += price * size
        side_seen = "sell" if is_ask else "buy"
        maker = (is_ask and bool(row.get("is_maker_ask"))) or (
            not is_ask and not bool(row.get("is_maker_ask"))
        )
        raw_fee = row.get("maker_fee" if maker else "taker_fee")
        if raw_fee not in (None, ""):
            fee_fields += 1
            fees += float(raw_fee) / LIGHTER_FEE_SCALE
        pnl += _f(row.get("ask_account_pnl" if is_ask else "bid_account_pnl")) or 0.0
    if quantity <= 0:
        return None
    return VenueFill(
        quantity=quantity, price=notional / quantity,
        fee=fees if fee_fields else None, realized_pnl=pnl, side=side_seen, matched=matched,
    )


def match_arcus_fills(rows: list[dict[str, Any]] | None, ref: Any) -> VenueFill | None:
    """按订单号（或 cid:clientId）把 /v1/fills 里属于这张订单的成交加总起来。"""
    hit = ax.match_fills(rows, None if ref is None else str(ref))
    if hit is None:
        return None
    quantity, price, fee, pnl, side, matched = hit
    return VenueFill(quantity=quantity, price=price, fee=fee,
                     realized_pnl=pnl, side=side, matched=matched)


# ── 执行结果 → 账本行 ────────────────────────────────────

def fill_row_from_leg(
    asset: str, action: str, leg: LegResult, *, dry_run: bool,
    market_id: int | None, cycle_id: int | None = None,
    created_at: float | None = None,
) -> dict[str, Any] | None:
    """把一条腿翻译成一行账。没真正发出去的腿不记。"""
    if leg is None or not (leg.submitted or leg.uncertain):
        return None
    base = {
        "asset": asset, "venue": leg.venue, "action": action, "side": leg.side,
        "dry_run": dry_run, "market_id": market_id if leg.venue == "lighter" else None,
        "requested_qty": float(leg.requested or 0.0),
        "price": leg.price, "cycle_id": cycle_id, "created_at": created_at,
    }
    if dry_run:
        return {**base, "order_ref": None, "quantity": float(leg.filled or 0.0),
                "source": "simulated", "status": "final", "note": "演练"}
    quantity = float(leg.filled or 0.0) if leg.fill_confirmed else 0.0
    # 仓位变化 / 回执证实过的成交量：对账时拿它判断交易所的记录是不是对全了
    base["confirmed_qty"] = quantity if leg.fill_confirmed and quantity > 0 else None
    ref = leg.order_ref
    source = "position"
    if not ref:
        # 没有订单号就永远对不上账 —— 直接按估计值定稿，并说清楚为什么
        return {**base, "order_ref": None, "quantity": quantity, "source": source,
                "status": "estimated", "note": "回执里没有订单号，无法对账"}
    note = None if leg.fill_confirmed else "成交量未证实，等交易所成交记录"
    return {**base, "order_ref": ref, "quantity": quantity, "source": source,
            "status": "pending", "note": note}


def ledger_rows_for_result(
    asset: str, result: Any, *, dry_run: bool, market_id: int | None,
    cycle_id: int | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for action, leg in getattr(result, "ledger", None) or []:
        row = fill_row_from_leg(asset, action, leg, dry_run=dry_run,
                                market_id=market_id, cycle_id=cycle_id)
        if row is not None:
            rows.append(row)
    return rows


# ── 对账 ────────────────────────────────────────────────

class FillReconciler:
    """每隔一段时间，拿待对账的单子去两个所的成交记录里核对。"""

    def __init__(self, store: Any, client: Any, executor: Any, account_index: int | None,
                 *, clock: Any = time.time) -> None:
        self.store = store
        self.client = client
        self.executor = executor
        self.account_index = account_index
        self.clock = clock
        self.last_summary: dict[str, Any] = {}
        self._zero_fee: bool | None = None
        self._zero_fee_checked_at = 0.0

    async def run_once(self) -> dict[str, Any]:
        now = self.clock()
        pending = self.store.pending_fills(older_than=now - MATCH_GRACE_SEC)
        summary: dict[str, Any] = {
            "at": now, "checked": len(pending), "matched": 0,
            "gave_up": 0, "waiting": 0, "errors": [],
        }
        if pending:
            await self._arcus([r for r in pending if r["venue"] == "arcus"], now, summary)
            await self._lighter([r for r in pending if r["venue"] == "lighter"], now, summary)
        if self._zero_fee:
            # 0 费率账户：对不全的 Lighter 行手续费补 0（含之前定稿的），净损益才算得出来
            with contextlib.suppress(Exception):
                self.store.backfill_lighter_zero_fee()
        self.last_summary = summary
        return summary

    def _settle(self, row: dict[str, Any], hit: VenueFill | None, now: float,
                fetched: bool, summary: dict[str, Any]) -> None:
        confirmed = row.get("confirmed_qty")
        too_old = now - float(row["created_at"]) > GIVE_UP_AFTER_SEC
        ceiling = max(float(confirmed or 0.0), float(row.get("requested_qty") or 0.0))
        if hit is not None and ceiling > 0 and hit.quantity > ceiling * (2 - COMPLETE_FRACTION):
            # 对上的成交比下单量还多：IOC 单不可能超量成交，只能是对错了单。
            # 宁可按仓位实测记成估算，也不能让对错的成交把交易量虚增。
            self.store.give_up_fill(
                row["id"],
                f"交易所记录对上 {hit.quantity:g}，超过下单量 {ceiling:g}，"
                "疑似对错了单，数量按仓位实测，手续费和盈亏不计",
            )
            summary["gave_up"] += 1
            return
        if hit is not None and confirmed and hit.quantity < float(confirmed) * COMPLETE_FRACTION:
            # 交易所只对上了一部分：仓位明明动了 confirmed 这么多，记录却不够。
            # 【不能】拿残缺的数据定稿 —— v0.2.0 就是这么把 Lighter 交易量少算一半多的。
            if too_old:
                self.store.give_up_fill(
                    row["id"],
                    f"交易所成交记录只对上 {hit.quantity:g}/{float(confirmed):g}，"
                    "数量按仓位实测，手续费和盈亏不计",
                    price=hit.price,
                )
                summary["gave_up"] += 1
            else:
                self.store.touch_fill(row["id"])
                summary["waiting"] += 1
            return
        if hit is not None:
            self.store.settle_fill(
                row["id"], quantity=hit.quantity, price=hit.price, fee=hit.fee,
                realized_pnl=hit.realized_pnl, side=hit.side,
            )
            summary["matched"] += 1
        elif fetched and too_old:
            self.store.give_up_fill(
                row["id"], "交易所成交记录里找不到这张单（多半是 IOC 零成交），按估计值定稿"
            )
            summary["gave_up"] += 1
        else:
            self.store.touch_fill(row["id"])
            summary["waiting"] += 1

    async def _arcus(self, rows: list[dict[str, Any]], now: float,
                     summary: dict[str, Any]) -> None:
        """Arcus：按时间一次查回最早那张待对账单之后的全部成交，再按 orderId 分拣。"""
        if not rows:
            return
        fills, fetched = None, False
        since = min(float(r["created_at"]) for r in rows) - ARCUS_FILLS_MARGIN_SEC
        try:
            fills = await self.client.arcus_fills(since)
            fetched = isinstance(fills, list)
        except Exception as exc:
            summary["errors"].append(f"Arcus 成交记录：{exc}")
        for row in rows:
            hit = match_arcus_fills(fills, row.get("order_ref")) if fetched else None
            self._settle(row, hit, now, fetched, summary)

    async def _lighter(self, rows: list[dict[str, Any]], now: float,
                       summary: dict[str, Any]) -> None:
        if not rows:
            return
        token = None
        if self.account_index is not None:
            try:
                token = await self.executor.lighter_auth_token()
            except Exception as exc:
                summary["errors"].append(f"Lighter 令牌：{exc}")
        zero_fee = await self._lighter_zero_fee(token, now, summary) if token else None
        by_market: dict[Any, list[dict[str, Any]]] = {}
        for row in rows:
            by_market.setdefault(row.get("market_id"), []).append(row)
        for market_id, group in by_market.items():
            trades, fetched = [], False
            if token and market_id is not None:
                try:
                    trades = await self._lighter_pages(int(market_id), token, group)
                    fetched = True
                except Exception as exc:
                    summary["errors"].append(f"Lighter 成交记录（市场 {market_id}）：{exc}")
            for row in group:
                hit = (match_lighter_trades(trades, row.get("order_ref"), self.account_index)
                       if fetched else None)
                if hit is not None and hit.fee is None:
                    if zero_fee:
                        # 成交记录里没有手续费字段，而账户确认是 0 费率的标准账户
                        hit = VenueFill(hit.quantity, hit.price, 0.0, hit.realized_pnl,
                                        hit.side, hit.matched)
                    elif zero_fee is None and now - float(row["created_at"]) <= GIVE_UP_AFTER_SEC:
                        # 账户费率这次没查到：先别定稿，不然这笔的手续费就永远是「未知」了
                        self.store.touch_fill(row["id"])
                        summary["waiting"] += 1
                        continue
                self._settle(row, hit, now, fetched, summary)

    async def _lighter_zero_fee(self, token: str, now: float,
                                summary: dict[str, Any]) -> bool | None:
        """Lighter 账户是不是 0 费率（标准账户 + 挂单吃单费率档都是 0）。一小时查一次。

        成交记录在 0 费率账户上不带手续费字段 —— 不查清楚的话，手续费永远是「未知」，
        净损益和磨损也就永远算不出来。口径照搬 Parallax。
        """
        if self._zero_fee is not None and now - self._zero_fee_checked_at < 3600:
            return self._zero_fee
        try:
            limits = await self.client.lighter_account_limits(token)
        except Exception as exc:
            summary["errors"].append(f"Lighter 账户费率：{exc}")
            return self._zero_fee          # 查不到就沿用上次的结论；从没查到过是 None（未知）
        tier = str(limits.get("user_tier_name") or limits.get("user_tier") or "").lower()
        self._zero_fee = (
            tier == "standard"
            and int(limits.get("current_maker_fee_tick") or 0) == 0
            and int(limits.get("current_taker_fee_tick") or 0) == 0
        )
        self._zero_fee_checked_at = now
        return self._zero_fee

    async def _lighter_pages(self, market_id: int, token: str,
                             group: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按时间倒序翻页，一直翻到【越过最早那张待对账单子的时间】为止。

        v0.2.0 在「每张单都至少见到一笔成交」时就停了 —— 一张单扫了好几档、
        成交跨在两页之间时，后半截就被漏掉。现在必须翻过它的时间点：
        按时间倒序排列，翻过去了就说明它的每一笔成交都已经拿到。
        """
        oldest = min(float(r["created_at"]) for r in group) - LIGHTER_PAGE_MARGIN_SEC
        collected: list[dict[str, Any]] = []
        cursor = None
        for _ in range(LIGHTER_MAX_PAGES):
            page, cursor = await self.client.lighter_trades(market_id, token, cursor=cursor)
            collected.extend(page)
            if not cursor or not page:
                break
            times = [t for t in (trade_time_seconds(r) for r in page) if t]
            if times and min(times) < oldest:
                break
        return collected


ARCUS_PNL_FEE_KEY = "arcus_pnl_fee_fix_v1"


def repair_arcus_double_counted_fees(store: Any) -> int:
    """一次性修复（v1.0.1）：v1.0.0 把 Arcus 的 closedPnl 原样当成「价差损益」，
    而 closedPnl 已经扣过手续费，首页净损益里 Arcus 手续费被扣了两遍。
    把已定稿的 Arcus 行的已实现盈亏加回手续费。只跑一次。"""
    if store.get_meta(ARCUS_PNL_FEE_KEY):
        return 0
    cursor = store.conn.execute(
        "UPDATE fills SET realized_pnl = realized_pnl + fee"
        " WHERE venue='arcus' AND source='exchange' AND fee IS NOT NULL"
        " AND realized_pnl IS NOT NULL"
    )
    store.conn.commit()
    store.set_meta(ARCUS_PNL_FEE_KEY, json.dumps({"at": time.time(), "rows": cursor.rowcount}))
    return cursor.rowcount
