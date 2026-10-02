"""Arcus 订单推送「门铃」—— 让挂单成交后能在几百毫秒内去 Lighter 对冲。

只当门铃用，不当账本用：
  · 订阅 orders 频道（wss://api.arcus.xyz/v1/ws，按地址 + 子账户订阅）；
  · 收到任何订单推送就「按门铃」，挂单循环立刻去 REST 读一次仓位，按仓位对冲。
  · 推送里的数量一概不用 —— 成交多少仍然只认 REST 仓位（这个项目一贯的规矩：
    WS 只做加速器，REST 才是权威。推送格式万一和文档不一样，最多是门铃不响，不会对冲错）。
  · 门铃坏了（连不上、没订阅成功）就自动退回每 0.5 秒轮询一次仓位。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

MAX_BACKOFF_SEC = 30.0


class ArcusOrderBell:
    def __init__(self, settings: Any) -> None:
        base = settings.arcus_api_url.rstrip("/")
        self.url = base.replace("https://", "wss://").replace("http://", "ws://") + "/v1/ws"
        self.address = settings.arcus_address
        self.account_index = int(settings.arcus_account_index)
        self.proxy = settings.proxy_for("arcus")
        self.connected = False
        self.subscribed = False
        self.rings = 0
        self.last_error: str | None = None
        self.last_message_at: float | None = None
        self._event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopped = False

    @property
    def healthy(self) -> bool:
        return self.connected and self.subscribed and not self._stopped

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    def ring(self) -> None:
        self.rings += 1
        event, self._event = self._event, asyncio.Event()
        event.set()

    async def wait(self, timeout: float) -> bool:
        """等下一次门铃，最多 timeout 秒。响了返回 True。"""
        event = self._event
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run(self) -> None:
        from websockets.asyncio.client import connect

        backoff = 1.0
        while not self._stopped:
            try:
                async with connect(self.url, proxy=self.proxy or None, open_timeout=15,
                                   ping_interval=20, ping_timeout=20) as ws:
                    self.connected, self.subscribed, self.last_error = True, False, None
                    backoff = 1.0
                    await ws.send(json.dumps({
                        "type": "subscribe", "channel": "orders", "id": self.address,
                        "accountIndex": self.account_index, "snapshot": False,
                    }))
                    async for raw in ws:
                        self.last_message_at = time.time()
                        try:
                            msg = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        kind = str(msg.get("type") or "")
                        if kind == "error":
                            self.last_error = str(msg.get("message") or msg)[:200]
                        elif kind == "subscribed" and msg.get("channel") == "orders":
                            self.subscribed = True
                        elif kind == "channel_data" and msg.get("channel") == "orders":
                            self.ring()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 门铃坏了只是退回轮询
                self.last_error = str(exc)[:200]
            self.connected = self.subscribed = False
            if self._stopped:
                break
            await asyncio.sleep(backoff)
            backoff = min(MAX_BACKOFF_SEC, backoff * 2)

    def stats(self) -> dict[str, Any]:
        return {"healthy": self.healthy, "connected": self.connected,
                "subscribed": self.subscribed, "rings": self.rings,
                "last_error": self.last_error, "last_message_at": self.last_message_at}
