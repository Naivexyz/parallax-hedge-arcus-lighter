"""两边账户的余额 —— 首页的「账户权益 / 可用 / 浮动盈亏」。

  · Lighter：口径照搬 Parallax 线上验证过的那套 —— 权益取 total_asset_value
    （没有就退回 collateral），可用取 available_balance。
  · Arcus：权益取 /v1/account 的 equity，可用取 freeCollateral
    （官方定义：equity − Σ 各仓位初始保证金要求，就是还能拿来开新仓的钱）。
    与实盘验证过的口径一致。

引擎算「能开多大」也调用这里的 arcus_balance —— 首页显示的可用和开仓用的可用
是同一个数，不会各算各的。
"""
from __future__ import annotations

from typing import Any


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def lighter_balance(payload: Any, account_index: int | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or account_index is None:
        return None
    row = None
    for candidate in payload.get("accounts") or []:
        idx = candidate.get("account_index", candidate.get("index"))
        if idx is not None and int(idx) == int(account_index):
            row = candidate
            break
    if row is None:
        return None
    positions = [
        p for p in row.get("positions") or []
        if isinstance(p, dict) and abs(_num(p.get("position"))) > 1e-12
    ]
    equity = _num(row.get("total_asset_value")) or _num(row.get("collateral"))
    return {
        "equity": equity,
        "available": _num(row.get("available_balance")),
        "unrealized_pnl": sum(_num(p.get("unrealized_pnl")) for p in positions),
        "open_positions": len(positions),
        "mode": None,
    }


def arcus_balance(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    positions = [
        p for p in payload.get("_positions") or []
        if isinstance(p, dict) and abs(_num(p.get("size"))) > 1e-12
    ]
    equity = _num(payload.get("equity"))
    free = payload.get("freeCollateral")
    available = _num(free) if free not in (None, "") else _num(payload.get("netQuoteBalance"))
    unrealized = 0.0
    for p in positions:
        size = _num(p.get("size"))
        if str(p.get("side") or "").upper() == "SHORT" and size > 0:
            size = -size
        entry, mark = _num(p.get("averageEntryPrice")), _num(p.get("markPx"))
        unrealized += size * (mark - entry) if (entry and mark) else _num(p.get("unrealizedPnl"))
    modes = {str(p.get("marginMode") or "").upper() for p in positions} - {""}
    return {
        "equity": equity,
        "available": max(0.0, available),
        "unrealized_pnl": unrealized,
        "open_positions": len(positions),
        "mode": "/".join(sorted(modes)) or None,
    }


def combine_balances(lighter: dict[str, Any] | None,
                     arcus: dict[str, Any] | None) -> dict[str, Any] | None:
    """两边合计。任何一边读不到就不给合计 —— 半个合计比没有合计更误导人。"""
    if lighter is None or arcus is None:
        return None
    return {
        key: _num(lighter.get(key)) + _num(arcus.get(key))
        for key in ("equity", "available", "unrealized_pnl", "open_positions")
    }
