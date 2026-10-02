"""实盘预检 —— 不下任何单，只验「切实盘之前会踩的那些坑」。

演练模式跳过的正是最容易出错的一层：签名库、精度对齐、报文格式。
把 DRY_RUN 改成 false 再看会怎样，等于把这些一次性暴露在真钱上。
这里把能离线验的都验掉。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .config import Settings
from .execution import Executor
from .fills import align_quantity
from .risk import (
    effective_leverages,
    estimate_corridor_pct,
    max_quantity_for,
    pre_open_check,
)
from .service import FundingService
from .store import Store


def _check(name: str, ok: bool, detail: str, *, fatal: bool = True) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "fatal": fatal}


async def run_preflight(
    settings: Settings, service: FundingService, store: Store
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    # ── 1. 签名客户端 ──────────────────────────────
    executor = Executor(settings, service.client, dry_run=False)
    try:
        executor._get_lighter_signer()
        checks.append(_check("Lighter 签名客户端", True, "已建立（原生签名库可用）"))
    except Exception as exc:
        checks.append(_check(
            "Lighter 签名客户端", False,
            f"{exc} —— 这是切实盘最常见的拦路虎，Windows x64 需要含官方 DLL 的依赖",
        ))
    # 同一个 Lighter API key 不能被两个程序同时用：nonce 各算各的，必然互相顶掉
    checks.append(_check(
        "Lighter API key 独占", True,
        f"本程序用账户 {settings.lighter_account_index} 的 API key #{settings.lighter_api_key_index}"
        f" —— 如果另一个程序同时在用这个 Lighter 账户，它必须用别的 key 编号",
        fatal=False,
    ))
    try:
        _, public_key = executor._get_arcus_key()
        derived_note = "" if settings.arcus_api_key else "（ARCUS_API_KEY 未填，已由私钥推导）"
        checks.append(_check(
            "Arcus 签名密钥", True,
            f"Ed25519 公钥 {public_key[:8]}…{public_key[-6:]}{derived_note}",
        ))
    except Exception as exc:
        checks.append(_check("Arcus 签名密钥", False, str(exc)))

    # ── 1b. 本机时钟 ──────────────────────────────
    # Arcus 拒收时间戳偏差超过 ±30 秒的签名请求（401），报错信息看不出是时钟问题
    try:
        before = time.time()
        payload = await service.client._request(
            "arcus", "GET", f"{settings.arcus_api_url}/v1/time", label="/v1/time")
        after = time.time()
        server = int(payload["timeNs"]) / 1e9
        drift = (before + after) / 2 - server
        checks.append(_check(
            "本机时钟", abs(drift) < 10,
            f"与 Arcus 服务器相差 {drift:+.1f} 秒"
            + ("" if abs(drift) < 10 else " —— 超过 30 秒会被拒单，请在 Windows 设置里「立即同步」时间"),
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("本机时钟", False, f"读不到 Arcus 服务器时间：{exc}", fatal=False))

    # ── 2. 账户读取与余额 ──────────────────────────
    from .engine import _arcus_available, _lighter_available

    lighter_acct, arcus_acct = await asyncio.gather(
        service.client.lighter_account(), service.client.arcus_account(),
        return_exceptions=True,
    )
    lighter_avail = _lighter_available(lighter_acct, settings.lighter_account_index)
    arcus_avail = _arcus_available(arcus_acct)
    for label, payload, avail in (
        ("Lighter", lighter_acct, lighter_avail),
        ("Arcus", arcus_acct, arcus_avail),
    ):
        if isinstance(payload, Exception):
            checks.append(_check(f"{label} 账户", False, f"读取失败：{payload}"))
        elif avail <= 0:
            checks.append(_check(
                f"{label} 可用保证金", False,
                f"读到 {avail:.2f} USDC —— 要么确实没钱，要么口径读错了",
            ))
        else:
            checks.append(_check(f"{label} 可用保证金", True, f"{avail:.2f} USDC"))

    # ── 3. 市场精度字段 ────────────────────────────
    try:
        markets = {m["asset"]: m for m in await service.client.common_markets()}
        missing = [
            m["asset"] for m in markets.values()
            if m.get("price_decimals") is None or m.get("lighter_size_decimals") is None
            or not m.get("arcus_step_size") or not m.get("arcus_tick_size")
            or not m.get("arcus_mmf")
        ]
        if missing:
            checks.append(_check(
                "市场精度字段", False,
                f"{', '.join(missing)} 缺少精度 —— 报价会算错几个数量级",
            ))
        else:
            checks.append(_check(
                "市场精度字段", True,
                f"{len(markets)} 个共有币种的数量 / 价格精度与维持保证金率都已读到",
            ))
    except Exception as exc:
        checks.append(_check("市场精度字段", False, str(exc)))
        markets = {}

    # ── 4. 逐个任务：会下什么单 ────────────────────
    from .arcus import account_exposure
    mmf_by_market = {int(m["arcus_market_id"]): float(m["arcus_mmf"])
                     for m in markets.values() if m.get("arcus_mmf")}
    snapshot = service.snapshot
    rows = {r["asset"]: r for r in snapshot.get("rows", [])}
    plans: list[dict[str, Any]] = []
    for task in store.list_tasks():
        asset = task["asset"]
        market = markets.get(asset)
        row = rows.get(asset)
        if not market or not row:
            plans.append({"asset": asset, "ok": False, "detail": "不在共有币种里"})
            continue
        price = row.get("mark_price") or 0.0
        leverage = float(task["leverage"] or 1.0)
        decimals = int(market["quantity_decimals"])
        try:
            lighter_max = (await service.client.lighter_market_details(
                int(market["lighter_market_index"])
            )).get("max_leverage")
        except Exception:  # noqa: BLE001
            lighter_max = None
        lighter_lev, arcus_lev = effective_leverages(
            leverage, arcus_max=float(market["max_leverage"]), lighter_max=lighter_max,
        )
        arcus_equity, other_notional, other_mm = account_exposure(
            None if isinstance(arcus_acct, Exception) else arcus_acct, mmf_by_market,
            exclude_market=int(market["arcus_market_id"]))
        quantity = max_quantity_for(
            leverage=leverage, price=price,
            lighter_available=lighter_avail, arcus_available=arcus_avail,
            quantity_decimals=decimals,
            lighter_leverage=lighter_lev, arcus_leverage=arcus_lev,
        )
        if task.get("notional_usdc") and price > 0:
            quantity = align_quantity(
                min(quantity, float(task["notional_usdc"]) / price), decimals
            )
        pre = pre_open_check(
            leverage=leverage, max_leverage=float(market["max_leverage"]),
            quantity=quantity, price=price,
            min_quantity=market["min_base_quantity"],
            lighter_available=lighter_avail, arcus_available=arcus_avail,
            min_corridor_pct=settings.min_corridor_pct,
            lighter_leverage=lighter_lev, arcus_leverage=arcus_lev,
            maintenance_fraction=market.get("arcus_mmf"),
            min_notional=market.get("min_notional"),
            arcus_equity=arcus_equity,
            arcus_other_notional=other_notional,
            arcus_other_maintenance=other_mm,
        )
        corridor_pct = pre.estimated_corridor_pct
        step = 10 ** decimals
        aligned = abs(quantity * step - round(quantity * step)) < 1e-9
        # Arcus 这张单【真签一遍但不发】：精度、单笔上限、密钥都在这里过一遍
        signed = None
        if pre.ok and aligned and quantity > 0 and price > 0:
            try:
                executor.build_arcus_order(market, "buy", quantity, price * 1.001)
                signed = True
            except Exception as exc:  # noqa: BLE001
                signed = str(exc)
        plans.append({
            "asset": asset,
            "enabled": bool(task["enabled"]),
            "leverage": leverage,
            "lighter_leverage": lighter_lev,
            "arcus_leverage": arcus_lev,
            "price": price,
            "quantity": quantity,
            "notional": quantity * price,
            "aligned": aligned,
            "min_quantity": market["min_base_quantity"],
            "corridor_pct": corridor_pct,
            "direction_label": row.get("direction_label"),
            "ok": pre.ok and aligned and signed in (True, None),
            "detail": pre.reason or (
                "数量未对齐精度，实盘会被拒单" if not aligned
                else f"Arcus 单签不出来：{signed}" if isinstance(signed, str)
                else "可以下单（Arcus 单已试签，未发送）"),
        })

    fatal = [c for c in checks if not c["ok"] and c["fatal"]]
    bad_plans = [p for p in plans if p.get("enabled") and not p["ok"]]
    return {
        "dry_run": settings.dry_run,
        "ready": not fatal and not bad_plans,
        "checks": checks,
        "plans": plans,
        "summary": (
            "全部通过，可以切实盘"
            if not fatal and not bad_plans
            else f"{len(fatal)} 项基础检查未过，{len(bad_plans)} 个启用中的任务下不出单"
        ),
    }
