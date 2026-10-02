"""Arcus 订单推送门铃：连一个本地假服务器，验证订阅报文、响铃、断线后退回轮询。"""
import asyncio
import json
from pathlib import Path

import pytest

websockets = pytest.importorskip("websockets")
from websockets.asyncio.server import serve  # noqa: E402

from parallax_hedge.arcus_ws import ArcusOrderBell  # noqa: E402
from parallax_hedge.config import Settings  # noqa: E402


def settings(port):
    return Settings(env_path=Path("."), data_dir=Path("."), arcus_api_url=f"http://127.0.0.1:{port}",
                    arcus_address="0x" + "ab" * 20, arcus_account_index=2)


def test_the_bell_subscribes_and_rings_on_order_pushes():
    async def scenario():
        received = []
        push = asyncio.Event()

        async def handler(ws):
            received.append(json.loads(await ws.recv()))
            await ws.send(json.dumps({"type": "subscribed", "channel": "orders"}))
            await push.wait()
            await ws.send(json.dumps({"type": "channel_data", "channel": "orders",
                                      "contents": {"orderId": "o1", "status": "PARTIALLY_FILLED"}}))
            await asyncio.sleep(1)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            bell = ArcusOrderBell(settings(port))
            assert bell.url == f"ws://127.0.0.1:{port}/v1/ws"
            bell.start()
            for _ in range(50):
                if bell.healthy:
                    break
                await asyncio.sleep(0.05)
            assert bell.healthy
            waiter = asyncio.ensure_future(bell.wait(2.0))
            await asyncio.sleep(0.05)
            push.set()
            rang = await waiter
            await bell.stop()
            return received, rang, bell
    received, rang, bell = asyncio.get_event_loop().run_until_complete(scenario())
    assert received[0] == {"type": "subscribe", "channel": "orders", "id": "0x" + "ab" * 20,
                           "accountIndex": 2, "snapshot": False}
    assert rang and bell.rings == 1
    assert not bell.healthy                        # 停掉之后不再算健康（挂单会退回轮询）


def test_an_unreachable_bell_is_not_healthy_and_wait_times_out():
    async def scenario():
        bell = ArcusOrderBell(settings(1))         # 没有服务在听
        bell.start()
        await asyncio.sleep(0.3)
        rang = await bell.wait(0.1)
        await bell.stop()
        return bell, rang
    bell, rang = asyncio.get_event_loop().run_until_complete(scenario())
    assert not bell.healthy and rang is False and bell.last_error
