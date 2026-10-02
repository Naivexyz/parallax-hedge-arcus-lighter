"""启动入口：python -m parallax_hedge"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .api import create_app
from .config import Settings, sync_env_file


def _base_dir() -> Path:
    # PyInstaller 打包后 __file__ 在临时解包目录里，配置要从 exe 所在目录找
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[2]


def main() -> None:
    base = _base_dir()
    env_path = base / ".env"
    data_dir = base / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    added = sync_env_file(env_path, base / ".env.example")
    settings = Settings.load(env_path, data_dir)

    url = f"http://{settings.host}:{settings.port}/"
    if os.environ.get("PARALLAX_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print("Parallax Hedge · Arcus × RHC Lighter —— 对冲刷量控制台")
    print(f"  配置：{env_path}")
    if added:
        print(f"  已补齐 {len(added)} 个新增设置（默认值）：{', '.join(added)}")
    print(f"  模式：{'演练（不下单）' if settings.dry_run else '★ 实盘（会真实下单）★'}")
    print(f"  面板：{url}")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
