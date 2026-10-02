"""FastAPI 应用：面板 + 引擎循环 + 对账循环。"""
from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import (
    ACCOUNT_FIELDS,
    Settings,
    account_settings_updates,
    account_settings_view,
    apply_account_settings,
    format_env_number,
    update_env_values,
)
from .engine import HedgeEngine
from .ledger import FillReconciler, repair_arcus_double_counted_fees
from .preflight import run_preflight
from .service import FundingService
from .store import Store

WEB_DIR = Path(__file__).parent / "web"



def _redacted_account_view(settings: Settings) -> dict[str, Any]:
    """序列化之后再查一遍，避免密钥或完整地址混进 GET / POST 响应。"""
    import json

    view = account_settings_view(settings)
    blob = json.dumps(view, ensure_ascii=False)
    for item in ACCOUNT_FIELDS:
        if item.kind not in ("secret", "identifier"):
            continue
        raw = str(getattr(settings, item.attr) or "").strip()
        if len(raw) >= 8 and raw in blob:
            raise HTTPException(500, "账户设置响应被拒绝：包含了不该返回的原文")
    return view


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Parallax Hedge · Arcus × Lighter", version=__version__)
    service = FundingService(settings)
    store = Store(settings.data_dir / "hedge.db")
    engine = HedgeEngine(settings, service, store, dry_run=settings.dry_run)
    fixed = repair_arcus_double_counted_fees(store)
    if fixed:
        store.log_cycle("-", "ledger", f"修正账本：{fixed} 条 Arcus 成交的价差损益不再重复扣手续费",
                        dry_run=settings.dry_run)
    reconciler = FillReconciler(
        store, service.client, engine.executor, settings.lighter_account_index
    )
    app.state.settings = settings
    app.state.service = service
    app.state.store = store
    app.state.engine = engine
    app.state.reconciler = reconciler
    app.state.refresh_task = None
    app.state.engine_task = None
    app.state.ledger_task = None
    from .spreads import SpreadScanner, SCAN_INTERVAL_SEC, sort_key
    spreads = SpreadScanner(service.client)
    app.state.spread_task = None

    async def spread_loop() -> None:
        await asyncio.sleep(15.0)          # 让引擎先跑起来
        while True:
            with contextlib.suppress(Exception):
                await spreads.scan_once()
            await asyncio.sleep(SCAN_INTERVAL_SEC)

    async def loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await service.refresh()
            await asyncio.sleep(max(5.0, settings.funding_refresh_seconds))

    async def engine_loop() -> None:
        # 先等一轮费率快照就绪，别让引擎在没有数据时空转
        await asyncio.sleep(3.0)
        while True:
            try:
                await engine.run_cycle()
            except Exception as exc:  # 单轮失败不能让循环停掉
                store.log_cycle("-", "error", str(exc)[:300], dry_run=settings.dry_run)
            await asyncio.sleep(max(5.0, settings.engine_cycle_seconds))

    async def ledger_loop() -> None:
        """成交账本对账：拿订单号去两个所的成交记录里换准确的价格和手续费。

        和交易循环分开跑 —— 对账慢了、失败了，都不能拖住下单。
        演练模式没有真实成交，不需要对账。
        """
        if settings.dry_run:
            return
        await asyncio.sleep(20.0)
        while True:
            try:
                await reconciler.run_once()
            except Exception as exc:  # noqa: BLE001
                reconciler.last_summary = {"at": time.time(), "errors": [str(exc)[:300]]}
            await asyncio.sleep(60.0)

    app.state.bell = None

    @app.on_event("startup")
    async def _startup() -> None:
        if settings.arcus_maker and not settings.dry_run and settings.arcus_configured:
            # Arcus 订单推送当门铃：挂单一成交立刻去读仓位、对冲（连不上就退回 0.5 秒轮询）
            from .arcus_ws import ArcusOrderBell
            app.state.bell = ArcusOrderBell(settings)
            app.state.bell.start()
            engine.bell = app.state.bell
        app.state.refresh_task = asyncio.create_task(loop())
        app.state.engine_task = asyncio.create_task(engine_loop())
        app.state.ledger_task = asyncio.create_task(ledger_loop())
        app.state.spread_task = asyncio.create_task(spread_loop())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        for name in ("refresh_task", "engine_task", "ledger_task", "spread_task"):
            task = getattr(app.state, name, None)
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await engine.aclose()
        if app.state.bell is not None:
            await app.state.bell.stop()
        await service.aclose()
        store.close()

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return {
            "version": __version__,
            "pair": "Arcus × RHC Lighter",
            "port": settings.port,
            "proxies": {
                "arcus": settings.proxy_label("arcus"),
                "lighter": settings.proxy_label("lighter"),
            },
            "refresh_seconds": settings.funding_refresh_seconds,
            "stale_max_seconds": settings.funding_stale_max_seconds,
            # 当前【实际生效】的设置 —— 不管 .env 里写没写，这里是准的
            "effective": {
                "DRY_RUN": settings.dry_run,
                "MIN_CORRIDOR_PCT（开仓门槛）": settings.min_corridor_pct,
                "CLOSE_CORRIDOR_PCT（平仓线）": settings.close_corridor_pct,
                "ENGINE_CYCLE_SECONDS": settings.engine_cycle_seconds,
                "REOPEN_COOLDOWN_SECONDS": settings.reopen_cooldown_seconds,
                "ORDER_SLIPPAGE_BPS": settings.order_slippage_bps,
                "ARCUS_MAKER（Arcus 挂单）": settings.arcus_maker,
                "MAKER_WAIT_SECONDS": settings.maker_wait_seconds,
                "FUNDING_STALE_MAX_SECONDS": settings.funding_stale_max_seconds,
            },
        }

    @app.get("/api/funding")
    async def funding(
        refresh: bool = Query(False, description="强制立刻重新拉取"),
        cost_bps: float | None = Query(None, description="往返成本假设 bps"),
    ) -> JSONResponse:
        if refresh or not service.snapshot.get("generated_at"):
            snap = await service.refresh(round_trip_cost_bps=cost_bps)
        else:
            snap = service.snapshot
            if cost_bps is not None and cost_bps != snap.get("round_trip_cost_bps"):
                snap = await service.refresh(round_trip_cost_bps=cost_bps)
        return JSONResponse(snap)

    # ── 任务 ────────────────────────────────────────
    @app.get("/api/tasks")
    async def list_tasks() -> dict[str, Any]:
        snapshot = service.snapshot
        managed = {t["asset"] for t in store.list_tasks() if t.get("enabled")}
        # 有持仓、但没有启用中的任务在管 —— 面板会标红，引擎却不会动手。
        # 这个落差最容易让人误以为「程序在看着」，所以必须显式报出来。
        unmanaged = [
            {
                "asset": r["asset"],
                "status": (r.get("position") or {}).get("status"),
                "status_text": (r.get("position") or {}).get("status_text"),
                "min_distance_pct": (r.get("position") or {}).get("min_distance_pct"),
            }
            for r in snapshot.get("rows", [])
            if (r.get("position") or {}).get("status") not in (None, "flat")
            and r["asset"] not in managed
        ]
        return {
            "dry_run": engine.dry_run,
            "unmanaged": unmanaged,
            "min_corridor_pct": settings.min_corridor_pct,
            "close_corridor_pct": settings.close_corridor_pct,
            "maker": settings.arcus_maker,
            "maker_wait_seconds": settings.maker_wait_seconds,
            "cycle_seconds": settings.engine_cycle_seconds,
            "engine_cycle_seconds": settings.engine_cycle_seconds,
            "max_spread_bps": settings.max_spread_bps,
            "min_hold_sec": settings.min_hold_sec,
            "max_hold_sec": settings.max_hold_sec,
            "spread_gate": True,
            "busy": engine.busy_assets(),
            "bell": app.state.bell.stats() if app.state.bell is not None else None,
            "last_cycle_at": engine.last_cycle_at,
            "tasks": store.list_tasks(),
            "decisions": engine.last_decisions,
        }

    @app.post("/api/settings")
    async def update_runtime_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """面板上的持仓时钟、价差阈值和开仓扫描间隔。

        引擎、执行器每轮读的都是这同一个 Settings 对象，所以改完下一轮就生效，
        不用重启进程。开仓扫描间隔由引擎循环每轮读取 settings.engine_cycle_seconds
        （不是启动时抄下来的局部变量），实际休眠不会短于 5 秒。
        买价仍必须严格低于卖价，这条不在这里放宽。
        """
        labels = {
            "min_hold_sec": "最短持有",
            "max_hold_sec": "最长持有",
            "max_spread_bps": "价差阈值",
            "engine_cycle_seconds": "开仓扫描间隔",
        }
        if not any(key in payload for key in labels):
            raise HTTPException(400, "没有要更新的设置")
        parsed: dict[str, float] = {}
        for key, label in labels.items():
            if key not in payload or payload[key] in ("", None):
                continue
            try:
                number = float(payload[key])
            except (TypeError, ValueError):
                raise HTTPException(400, f"{label} 不是有效数字") from None
            if number != number or number in (float("inf"), float("-inf")):
                raise HTTPException(400, f"{label} 不是有效数字")
            parsed[key] = number
        if not parsed:
            raise HTTPException(400, "没有要更新的设置")
        min_hold = parsed.get("min_hold_sec", float(settings.min_hold_sec))
        max_hold = parsed.get("max_hold_sec", float(settings.max_hold_sec))
        spread = parsed.get("max_spread_bps", float(settings.max_spread_bps))
        cycle = parsed.get("engine_cycle_seconds", float(settings.engine_cycle_seconds))
        if min_hold < 0:
            raise HTTPException(400, "最短持有不能小于 0 秒")
        if max_hold < min_hold:
            raise HTTPException(
                400, f"最长持有 {max_hold:g} 秒不能小于最短 {min_hold:g} 秒"
            )
        if spread < 0:
            raise HTTPException(400, "价差阈值不能小于 0")
        # 只在这次真的要改间隔时卡 5 秒。循环本身是 max(5, ...)，
        # 但面板不能存一个引擎实际做不到的更短间隔。已有的更小值不拦其它字段的保存。
        if "engine_cycle_seconds" in parsed and cycle < 5:
            raise HTTPException(400, "开仓扫描间隔不能小于 5 秒")
        settings.min_hold_sec = min_hold
        settings.max_hold_sec = max_hold
        settings.max_spread_bps = spread
        settings.engine_cycle_seconds = cycle
        persisted = update_env_values(settings.env_path, {
            "MIN_HOLD_SEC": format_env_number(min_hold),
            "MAX_HOLD_SEC": format_env_number(max_hold),
            "MAX_SPREAD_BPS": format_env_number(spread),
            "ENGINE_CYCLE_SECONDS": format_env_number(cycle),
        })
        return {
            "min_hold_sec": settings.min_hold_sec,
            "max_hold_sec": settings.max_hold_sec,
            "max_spread_bps": settings.max_spread_bps,
            "engine_cycle_seconds": settings.engine_cycle_seconds,
            "restart_required": False,
            "persisted": persisted,
        }

    @app.get("/api/account-settings")
    async def get_account_settings() -> dict[str, Any]:
        """本地面板读取账户配置。密钥和完整地址、完整长标识都不在响应里。"""
        return _redacted_account_view(settings)

    @app.post("/api/account-settings")
    async def save_account_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """写入程序启动时读取的那个 .env，并更新当前进程里的 Settings。

        不下单。请求体里的密钥不写日志。留空的密钥表示保持原值。
        地址、索引、API 地址是每次请求现读 Settings 的，所以不用重启。
        签名器缓存会丢掉，下一笔单用新密钥。代理变了就重建只读连接。
        Arcus 订单门铃在启动时把地址和代理抄走了，已经连上的话要重启才重新订阅。
        """
        try:
            updates = account_settings_updates(settings, payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        persisted = False
        if updates:
            env_path = settings.env_path
            if env_path.is_dir():
                raise HTTPException(400, "配置路径不是文件")
            if not env_path.exists():
                env_path.parent.mkdir(parents=True, exist_ok=True)
                env_path.write_text("", encoding="utf-8")
            persisted = update_env_values(env_path, updates)
            apply_account_settings(settings, updates)
            await engine.executor.reset_credentials()
            reconciler.account_index = settings.lighter_account_index
            if {"ARCUS_PROXY", "LIGHTER_PROXY", "GLOBAL_PROXY"}.intersection(updates):
                rebind = getattr(service.client, "rebind_clients", None)
                if rebind is not None:
                    await rebind()
        view = _redacted_account_view(settings)
        view["persisted"] = persisted
        view["changed"] = sorted(updates)
        view["restart_required"] = False
        view["note"] = (
            "已写入 .env，并更新当前进程的账户配置，不用为了地址、索引或密钥重启。"
            "下一笔签名会用新密钥；改了代理的话只读连接已按新代理重建。"
            "若 Arcus 订单门铃已经连着，它仍用启动时的地址和代理，重新订阅需要重启进程。"
        )
        return view

    @app.post("/api/tasks")
    async def upsert_task(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        asset = str(payload.get("asset") or "").upper()
        if not asset:
            raise HTTPException(400, "缺少 asset")
        markets = {m["asset"] for m in await service.client.common_markets()}
        if asset not in markets:
            raise HTTPException(400, f"{asset} 不在两所共有币种里")
        fields: dict[str, Any] = {}
        optional_float = lambda v: None if v in ("", None) else float(v)  # noqa: E731
        for key, cast in (("enabled", lambda v: int(bool(v))),
                          ("leverage", float), ("rotation_hours", float),
                          ("rotation_hours_max", optional_float),
                          ("notional_usdc", optional_float)):
            if key in payload:
                try:
                    fields[key] = cast(payload[key])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{key} 不是有效数字") from None
        if fields.get("rotation_hours") is not None and fields["rotation_hours"] <= 0:
            raise HTTPException(400, "轮换周期必须大于 0")
        if fields.get("leverage") is not None and fields["leverage"] <= 0:
            raise HTTPException(400, "杠杆必须大于 0")
        # 随机区间：最长为空 = 固定周期；填了就不能比最短还短
        existing = store.get_task(asset) or {}
        low = fields.get("rotation_hours", existing.get("rotation_hours"))
        high = fields.get("rotation_hours_max", existing.get("rotation_hours_max"))
        if high is not None:
            if high <= 0:
                raise HTTPException(400, "轮换周期最长值必须大于 0")
            if low is not None and high < low:
                raise HTTPException(400, f"轮换周期最长 {high:g} 小时不能小于最短 {low:g} 小时")
        return {"task": store.upsert_task(asset, **fields)}

    @app.delete("/api/tasks/{asset}")
    async def delete_task(asset: str) -> dict[str, Any]:
        task = store.get_task(asset.upper())
        if task and task.get("opened_at"):
            raise HTTPException(400, "该任务还有持仓，请先停用让它平仓收尾")
        store.delete_task(asset.upper())
        return {"ok": True}

    @app.post("/api/preflight")
    async def preflight() -> dict[str, Any]:
        """实盘预检：不下任何单，只验签名、余额、精度、以及会下出什么单。"""
        return await run_preflight(settings, service, store)

    @app.post("/api/cycle")
    async def run_cycle_now() -> dict[str, Any]:
        try:
            return {"decisions": await engine.run_cycle()}
        except Exception as exc:  # noqa: BLE001 —— 手动按钮也不能把错误吞成一个 500
            store.log_cycle("-", "error", str(exc)[:300], dry_run=settings.dry_run)
            return {"decisions": [], "error": str(exc)[:300]}

    @app.get("/api/spreads")
    async def get_spreads() -> dict[str, Any]:
        """共有币种按价差从窄到宽排好（下拉框用）。"""
        maker = settings.arcus_maker
        assets = [r["asset"] for r in service.snapshot.get("rows", [])]
        assets += [a for a in spreads.spreads if a not in assets]
        ordered = sorted(assets, key=lambda a: (sort_key(spreads.spreads.get(a), maker), a))
        return {
            "maker": maker,
            "scanned_at": spreads.scanned_at,
            "scanning": spreads.scanning,
            "assets": [{"asset": a, **(spreads.spreads.get(a) or {})} for a in ordered],
        }

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        """首页：两边余额 + 两边各自的交易量 / 手续费 / 已实现盈亏 + 合计 + 按任务拆分。"""
        snapshot = service.snapshot
        tasks = store.list_tasks()
        return {
            "dry_run": engine.dry_run,
            "generated_at": time.time(),
            "balances": snapshot.get("balances") or {},
            "account_errors": (snapshot.get("accounts") or {}).get("errors") or {},
            "ledger": store.fill_stats(dry_run=engine.dry_run),
            "tasks": [
                {"asset": t["asset"], "enabled": bool(t.get("enabled")),
                 "opened_at": t.get("opened_at")}
                for t in tasks
            ],
            "reconciler": reconciler.last_summary,
        }

    @app.get("/api/cycles")
    async def cycles(limit: int = Query(60, ge=1, le=500)) -> dict[str, Any]:
        return {"cycles": store.recent_cycles(limit)}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
