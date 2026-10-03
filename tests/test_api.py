"""API 层：任务的随机区间校验、首页统计接口的形状。

直接调用路由函数（不起服务器）。真实的 fastapi 和测试替身都能跑。
"""
import asyncio
import tempfile
from pathlib import Path

import pytest

from parallax_hedge.config import Settings


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def endpoint(app, method, path):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in (getattr(route, "methods", None) or ()):
            return route.endpoint
    raise LookupError(f"{method} {path}")


class Client:
    async def common_markets(self, force=False):
        return [{"asset": "OAI", "lighter_market_index": 42}]

    async def aclose(self):
        pass


def make_app(tmp):
    from parallax_hedge.api import create_app
    settings = Settings(env_path=Path("."), data_dir=Path(tmp), lighter_account_index=77)
    app = create_app(settings)
    app.state.service.client = Client()
    return app


def http_error(coro):
    with pytest.raises(Exception) as info:
        run(coro)
    return getattr(info.value, "status_code", None), str(getattr(info.value, "detail", info.value))


def test_task_accepts_a_random_rotation_range():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(tmp)
        save = endpoint(app, "POST", "/api/tasks")
        task = run(save({"asset": "oai", "enabled": True, "leverage": 6,
                         "rotation_hours": 1, "rotation_hours_max": 2}))["task"]
        assert task["rotation_hours"] == 1.0 and task["rotation_hours_max"] == 2.0
        # 只改一个字段不能把区间冲掉
        task = run(save({"asset": "OAI", "enabled": False}))["task"]
        assert task["rotation_hours_max"] == 2.0
        # 清空上限 = 回到固定周期
        task = run(save({"asset": "OAI", "rotation_hours_max": ""}))["task"]
        assert task["rotation_hours_max"] is None
        app.state.store.close()


def test_a_range_whose_maximum_is_below_its_minimum_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(tmp)
        save = endpoint(app, "POST", "/api/tasks")
        status, detail = http_error(save({"asset": "OAI", "leverage": 6,
                                          "rotation_hours": 2, "rotation_hours_max": 1}))
        assert status == 400 and "不能小于" in detail
        run(save({"asset": "OAI", "leverage": 6, "rotation_hours": 1, "rotation_hours_max": 2}))
        # 只改下限、把它抬到上限之上，也要拦住
        status, _ = http_error(save({"asset": "OAI", "rotation_hours": 3}))
        assert status == 400
        status, _ = http_error(save({"asset": "OAI", "rotation_hours_max": "abc"}))
        assert status == 400
        app.state.store.close()


def test_stats_endpoint_shape():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(tmp)
        store = app.state.store
        store.upsert_task("OAI", enabled=1, leverage=6.0, rotation_hours=1.0)
        store.record_fill(asset="OAI", venue="lighter", action="open", side="buy",
                          dry_run=True, market_id=42, order_ref=None, requested_qty=0.1,
                          quantity=0.1, price=1590.0, source="simulated", status="final")
        s = run(endpoint(app, "GET", "/api/stats")())
        assert s["dry_run"] is True
        assert s["ledger"]["venues"]["lighter"]["volume"] == pytest.approx(159.0)
        assert s["ledger"]["total"]["fills"] == 1
        assert s["tasks"] == [{"asset": "OAI", "enabled": True, "opened_at": None}]
        assert "balances" in s and "reconciler" in s
        store.close()


def test_panel_settings_update_the_running_engine_without_a_restart():
    """持仓时钟、浮盈亏差额和扫描间隔写进引擎正在读的 Settings。下一轮就用，不用重启。"""
    import inspect

    from parallax_hedge.api import create_app
    # 循环每轮读 settings 对象上的属性，不是启动时抄进局部变量的秒数
    source = inspect.getsource(create_app)
    assert "await asyncio.sleep(max(5.0, settings.engine_cycle_seconds))" in source
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(tmp)
        env = Path(tmp) / ".env"
        env.write_text("DRY_RUN=true\nMIN_HOLD_SEC=9\nUNRELATED=keep-me\n", encoding="utf-8")
        app.state.settings.env_path = env
        update = endpoint(app, "POST", "/api/settings")
        body = run(update({
            "min_hold_sec": 3,
            "max_hold_sec": 300,
            "pnl_close_usd": 0.02,
            "engine_cycle_seconds": 25,
        }))
        assert body["restart_required"] is False
        assert body["persisted"] is True
        assert body["min_hold_sec"] == pytest.approx(3)
        assert body["max_hold_sec"] == pytest.approx(300)
        assert body["pnl_close_usd"] == pytest.approx(0.02)
        assert "max_spread_bps" not in body
        assert body["engine_cycle_seconds"] == pytest.approx(25)
        settings = app.state.settings
        assert app.state.engine.settings is settings
        assert app.state.engine.executor.settings is settings
        assert settings.pnl_close_usd == pytest.approx(0.02)
        assert settings.engine_cycle_seconds == pytest.approx(25)
        body = run(update({"pnl_close_usd": 0.05}))
        assert settings.pnl_close_usd == pytest.approx(0.05)
        assert settings.min_hold_sec == pytest.approx(3)
        assert settings.max_hold_sec == pytest.approx(300)
        assert settings.engine_cycle_seconds == pytest.approx(25)
        status, detail = http_error(update({"max_spread_bps": 1.1}))
        assert status == 400 and "没有要更新" in detail
        assert settings.pnl_close_usd == pytest.approx(0.05)
        body = run(update({"engine_cycle_seconds": 5}))
        assert body["engine_cycle_seconds"] == pytest.approx(5)
        assert settings.engine_cycle_seconds == pytest.approx(5)
        assert settings.min_hold_sec == pytest.approx(3)
        assert settings.max_hold_sec == pytest.approx(300)
        assert settings.pnl_close_usd == pytest.approx(0.05)
        saved = env.read_text(encoding="utf-8")
        assert "UNRELATED=keep-me" in saved
        assert "MAX_SPREAD_BPS" not in saved
        assert "PNL_CLOSE_USD=0.05" in saved
        assert "MIN_HOLD_SEC=3" in saved
        assert "MAX_HOLD_SEC=300" in saved
        assert "ENGINE_CYCLE_SECONDS=5" in saved
        listed = run(endpoint(app, "GET", "/api/tasks")())
        assert "max_spread_bps" not in listed
        assert listed["pnl_close_usd"] == pytest.approx(0.05)
        assert listed["min_hold_sec"] == pytest.approx(3)
        assert listed["engine_cycle_seconds"] == pytest.approx(5)
        assert listed["cycle_seconds"] == pytest.approx(5)
        status, detail = http_error(update({"max_hold_sec": 1, "min_hold_sec": 3}))
        assert status == 400 and "不能小于" in detail
        assert settings.max_hold_sec == pytest.approx(300)
        status, detail = http_error(update({"engine_cycle_seconds": 4}))
        assert status == 400 and "不能小于 5" in detail
        assert settings.engine_cycle_seconds == pytest.approx(5)
        assert "ENGINE_CYCLE_SECONDS=5" in env.read_text(encoding="utf-8")
        status, _ = http_error(update({"engine_cycle_seconds": "abc"}))
        assert status == 400
        status, _ = http_error(update({"pnl_close_usd": "abc"}))
        assert status == 400
        status, _ = http_error(update({"min_hold_sec": -1}))
        assert status == 400
        status, _ = http_error(update({"pnl_close_usd": -0.1}))
        assert status == 400
        app.state.store.close()
