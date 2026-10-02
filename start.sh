#!/usr/bin/env bash
# macOS / Linux 一键启动：建虚拟环境 → 装依赖 → 生成 .env（演练模式）→ 打开面板
set -e
cd "$(dirname "$0")"
MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"

if [ ! -x .venv/bin/python ]; then
  PY=""
  for c in python3.11 python3.12 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,12) else 1)'; then
      PY="$c"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "需要 Python 3.11 或 3.12：https://www.python.org/downloads/"; exit 1
  fi
  echo "[1/3] 用 $PY 建虚拟环境 ..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip || true
fi

echo "[2/3] 安装 / 检查依赖 ..."
.venv/bin/python -m pip install -q -e . || .venv/bin/python -m pip install -q -e . -i "$MIRROR"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已从 .env.example 生成 .env（DRY_RUN=true，不会下单）。填好密钥后重新运行。"
fi

echo "[3/3] 启动面板 ..."
exec .venv/bin/python -m parallax_hedge
