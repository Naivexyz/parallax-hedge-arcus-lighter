"""双腿执行 —— 开仓、平仓、孤腿抢救、保护性挂单。

编排顺序照搬 Parallax 实盘验证过的那套（2026-09-17 它刚救下一条裸腿）：

    1. 先读两边真实仓位（before）
    2. 【先下 Lighter】—— 它是受限的一边（限流、链上交易、约 7% 不成交）
    3. 轮询真实仓位，直到 Lighter 那边发生变化
    4. 用 fills.classify_fill 判定，【不看订单返回】
    5. 没成交且 Arcus 没动过 → 直接返回，【根本不向 Arcus 下单】
    6. 成交了 → 按【实际成交量】而不是请求量去 Arcus 对冲
    7. Arcus 失败 → 立刻市价平掉 Lighter 那条腿（抢救）

第 6 步用实际成交量是关键：IOC 可能只成一部分，按请求量对冲会
多出一截反向敞口。
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import arcus as ax
from .config import Settings
from .exchanges import ExchangeError, MarketClient
from .fills import (
    MEANINGFUL_FILL_FRACTION,
    FillVerdict,
    align_quantity,
    classify_fill,
    closing_sides,
    hedge_side,
    positions_match,
    slippage_limit_price,
)
from .positions import parse_arcus_position, parse_lighter_position
from .spread_gate import (
    join_price, maker_exit_price, open_sides_allowed,
    round_trip_close_net, unrealized_close_ready,
)

# 下单后轮询仓位的节奏。Lighter 是链上交易，确认要一点时间；
# Parallax 用的是 4 次重试，这里沿用。
_CONFIRM_RETRIES = 4
_CONFIRM_DELAY_SEC = 0.45

# Arcus 下单回执 202 = 已转给撮合引擎，仓位接口要稍等才反映；
# 对冲腿按仓位确认时多给几次机会（累计约 3 秒），免得把「还没刷新」当成「没成交」。
_ARCUS_CONFIRM_RETRIES = 6

# Arcus 下单 / 设杠杆的 HTTP 超时。下单【绝不重试】—— 超时的时候单子可能已经发出去了，
# 重发就是两张单。超时一律按「结果未知」处理，交给仓位去判定。
_ARCUS_WRITE_TIMEOUT_SEC = 12.0


def arcus_limit_price(market: dict[str, Any], price: float, side: str) -> float:
    """Arcus 限价：对齐到该价位的 tick，方向朝着更激进的一侧（买往上、卖往下）。"""
    try:
        return float(ax.round_arcus_price(market, price, side))
    except ValueError as exc:
        raise ExchangeError(str(exc)) from exc


# Lighter 回执里的 tx_hash。回执是 str(response)，存进 dict 再转字符串时
# 引号前面会多出一个反斜杠（tx_hash=\'bfe6…\'），正则要容得下它。
_LIGHTER_TX_HASH_RE = re.compile(r"tx_hash\s*[=:]\s*\\?['\"]?(?:0x)?([0-9a-fA-F]{32,})")


def normalize_tx_hash(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    return text or None


def lighter_tx_hash(raw: Any) -> str | None:
    """从 Lighter 下单回执里取出 tx_hash —— 之后靠它去交易所成交记录里对账。"""
    candidates: list[Any] = []
    if isinstance(raw, dict):
        candidates.extend(raw.get(k) for k in ("tx_hash", "response", "created"))
    candidates.append(raw)
    for item in candidates:
        if not isinstance(item, str):
            continue
        hit = _LIGHTER_TX_HASH_RE.search(item)
        if hit:
            return normalize_tx_hash(hit.group(1))
    return None


def _is_open(size: float) -> bool:
    return math.isfinite(size) and abs(size) > 1e-12


def _is_flat(after: float, before: float) -> bool:
    """平仓后这条腿算不算平干净了。读不到（nan）一律不算 —— 不能当成 0。"""
    if not math.isfinite(after):
        return False
    return abs(after) <= max(1e-9, abs(before) * MEANINGFUL_FILL_FRACTION)


@dataclass
class LegResult:
    venue: str
    ok: bool
    submitted: bool = True
    raw: Any = None
    error: str | None = None
    filled: float = 0.0
    # ── 账本字段：成交量 / 手续费统计靠它们 ──
    side: str | None = None
    requested: float = 0.0
    # 成交均价。两边的下单回执都不带成交均价：先填参考价（盘口 VWAP），
    # 之后由交易所成交记录校正（Lighter 按 tx_hash，Arcus 按 orderId）。
    price: float | None = None
    price_is_estimate: bool = True
    fill_confirmed: bool = False        # filled 是否已被仓位变化或交易所回执证实
    order_ref: str | None = None        # Lighter tx_hash / Arcus orderId —— 对账用
    # 结果未知：请求可能已经发出（超时、断流）。这种腿【必须按仓位判定】，
    # 不能当成「没下出去」—— 当成没下，万一其实成交了就是一条没人管的裸腿。
    uncertain: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue, "ok": self.ok, "submitted": self.submitted,
            "error": self.error, "filled": self.filled,
            "side": self.side, "requested": self.requested,
            "price": self.price, "price_is_estimate": self.price_is_estimate,
            "fill_confirmed": self.fill_confirmed, "order_ref": self.order_ref,
            "uncertain": self.uncertain,
            "raw": None if self.raw is None else str(self.raw)[:400],
        }


def order_ref_of(leg: LegResult) -> str | None:
    if leg.venue == "lighter":
        return lighter_tx_hash(leg.raw)
    raw = leg.raw if isinstance(leg.raw, dict) else {}
    return ax.order_ref(raw.get("orderId"), raw.get("clientId"))


def _stamp(leg: LegResult, *, side: str, requested: float,
           reference_price: float | None) -> LegResult:
    """补齐账本字段。

    放在【调用方】而不是下单函数里 —— 下单函数在测试里会被替身整个换掉，
    写在里面的话，替身跑出来的账本就是空的，测了等于没测。
    """
    leg.side = leg.side or side
    if not leg.requested:
        leg.requested = abs(float(requested))
    if leg.price is None and reference_price and reference_price > 0:
        leg.price = float(reference_price)
        leg.price_is_estimate = True
    if leg.order_ref is None:
        leg.order_ref = order_ref_of(leg)
    return leg


def _simulate_fill(leg: LegResult) -> None:
    """演练模式：按请求量、参考价记一笔模拟成交。"""
    if leg.submitted:
        leg.filled = leg.requested
        leg.fill_confirmed = True


@dataclass
class PairResult:
    ok: bool
    stage: str
    lighter: LegResult | None = None
    arcus: LegResult | None = None
    rescue: LegResult | None = None
    reason: str | None = None
    dry_run: bool = False
    elapsed_ms: float = 0.0
    notes: list[str] = field(default_factory=list)
    # 诊断：实际发出的限价 + 当时两边的买一卖一。
    # 没有这个，「报单成功但零成交」只能靠猜。
    quotes: dict[str, Any] = field(default_factory=dict)
    # 这一次动作里【真正发出去】的每一条腿：(动作, 腿)。
    # 引擎据此逐条落账，首页的成交量 / 手续费就从这里来。
    ledger: list[tuple[str, LegResult]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "stage": self.stage, "reason": self.reason,
            "dry_run": self.dry_run, "elapsed_ms": round(self.elapsed_ms, 1),
            "notes": self.notes,
            "quotes": self.quotes,
            "lighter": self.lighter.to_dict() if self.lighter else None,
            "arcus": self.arcus.to_dict() if self.arcus else None,
            "rescue": self.rescue.to_dict() if self.rescue else None,
        }



def _close_send_allowed(quotes: dict[str, Any], lighter_px: float, arcus_px: float) -> tuple[bool, str]:
    """计划内平仓：每一边自己的开仓价对上自己即将发出的平仓价，再加总。

    两所之间的价差不算亏损。没有开仓价时不在这里拦（引擎已经拦过，旧测试也不带开仓价）。
    最长持有强制平仓不拦。合计差于面板浮盈亏差额就返回 False，调用方撤未成交的 maker，不改吃单。
    """
    if quotes.get("force_close"):
        # 到点可以不再等价，挂 maker 跟盘。仍然不能改吃单。
        return True, "已到最长持有，允许挂 maker 平仓"
    entries = quotes.get("close_entries")
    if not isinstance(entries, dict) or not entries:
        return True, ""
    window = quotes.get("pnl_close_usd")
    if window is None:
        return False, "没有读到面板浮盈亏差额，先不平"
    net = round_trip_close_net(
        entries.get("lighter_size"), entries.get("lighter_entry"), lighter_px,
        entries.get("arcus_size"), entries.get("arcus_entry"), arcus_px,
    )
    if net is None or not unrealized_close_ready(net, window):
        shown = "未知" if net is None else f"{net:+.4f}"
        return False, (
            f"按即将发出的平仓价估算往返 {shown} USDC，"
            f"差于 -{float(window):g}，先不平"
        )
    return True, f"按即将发出的平仓价估算往返 {net:+.4f} USDC"


class Executor:
    """两个所的下单端。dry_run=True 时只记录意图，不提交任何订单。"""

    def __init__(self, settings: Settings, market: MarketClient, *, dry_run: bool = True) -> None:
        self.settings = settings
        self.market = market
        self.dry_run = dry_run
        self._lighter_signer: Any = None
        self._arcus_key: Any = None
        self._arcus_api_key: str | None = None
        self._arcus_http: Any = None
        self._lighter_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._last_lighter_order_at = 0.0
        self._client_order_index = int(time.time() * 1000)
        self._auth_token: str | None = None
        self._auth_token_expires_at = 0.0

    # ── 签名客户端（惰性初始化）──────────────────────
    def _require_live(self) -> None:
        if self.dry_run:
            raise ExchangeError("dry_run 模式下不应触及签名客户端")

    def _get_lighter_signer(self) -> Any:
        if self._lighter_signer is not None:
            return self._lighter_signer
        try:
            from lighter.signer_client import SignerClient
        except (ImportError, OSError) as exc:
            raise ExchangeError(
                "未加载 Lighter 原生签名库；Windows x64 需要安装含官方 DLL 的依赖"
            ) from exc
        if self.settings.lighter_account_index is None:
            raise ExchangeError("缺少 LIGHTER_ACCOUNT_INDEX")
        if self.settings.lighter_api_key_index is None:
            raise ExchangeError("缺少 LIGHTER_API_KEY_INDEX")
        if not self.settings.lighter_api_private_key:
            raise ExchangeError("缺少 LIGHTER_API_PRIVATE_KEY")
        self._lighter_signer = SignerClient(
            url=self.settings.lighter_base_url,
            account_index=self.settings.lighter_account_index,
            api_private_keys={
                self.settings.lighter_api_key_index: self.settings.lighter_api_private_key
            },
        )
        return self._lighter_signer

    def _get_arcus_key(self) -> tuple[Any, str]:
        """(Ed25519 私钥, 公钥 hex)。

        ARCUS_API_KEY 没填就由私钥推导 —— 能推导的不让人填。
        填了就必须和私钥是同一对，否则拒绝下单：
        对不上的时候签出来的单交易所一定拒，而且报错信息很难看懂。
        """
        if self._arcus_key is not None and self._arcus_api_key:
            return self._arcus_key, self._arcus_api_key
        if not ax.ADDRESS_RE.match(self.settings.arcus_address or ""):
            raise ExchangeError("ARCUS_ADDRESS 必须是 0x 开头的 40 位钱包公开地址")
        if not 0 <= int(self.settings.arcus_account_index) <= 9:
            raise ExchangeError("ARCUS_ACCOUNT_INDEX 必须是 0~9")
        try:
            key = ax.load_private_key(
                self.settings.arcus_api_private_key, self.settings.arcus_api_private_key_file
            )
        except ValueError as exc:
            raise ExchangeError(str(exc)) from exc
        derived = ax.public_key_hex(key)
        configured = (self.settings.arcus_api_key or "").lower()
        if configured and configured != derived:
            raise ExchangeError(
                "ARCUS_API_KEY 与 ARCUS_API_PRIVATE_KEY 不是同一对 Ed25519 密钥，已拒绝下单"
            )
        self._arcus_key, self._arcus_api_key = key, derived
        return key, derived

    def _arcus_client(self) -> Any:
        if self._arcus_http is None:
            import httpx
            self._arcus_http = httpx.AsyncClient(
                timeout=httpx.Timeout(_ARCUS_WRITE_TIMEOUT_SEC, connect=10.0),
                proxy=self.settings.proxy_for("arcus"),
                headers={"User-Agent": "Parallax-Hedge-Arcus/1.0",
                         "Content-Type": "application/json", "Accept": "application/json"},
            )
        return self._arcus_http

    async def aclose(self) -> None:
        if self._arcus_http is not None:
            await self._arcus_http.aclose()
            self._arcus_http = None

    async def reset_credentials(self) -> None:
        """账户密钥变了就丢掉已经建好的签名器。下一笔单重新读 Settings，这里不下单。"""
        self._lighter_signer = None
        self._arcus_key = None
        self._arcus_api_key = None
        self._auth_token = None
        self._auth_token_expires_at = 0.0
        http = self._arcus_http
        self._arcus_http = None
        if http is not None:
            await http.aclose()

    async def _arcus_post(self, path: str, body: dict[str, Any],
                          timestamp: int, signature: str) -> tuple[int, Any]:
        """发一个签过名的 POST，【只发一次】。返回 (HTTP 状态码, JSON)。

        网络层异常原样抛出，由调用方按「结果未知」处理 —— 这里绝不重试。
        """
        _, api_key = self._get_arcus_key()
        client = self._arcus_client()
        response = await client.post(
            f"{self.settings.arcus_api_url}{path}",
            params={"address": self.settings.arcus_address},
            content=json.dumps(body, separators=(",", ":")),
            headers={"X-API-Key": api_key, "X-Timestamp": str(timestamp),
                     "X-Signature": signature},
        )
        try:
            data = response.json()
        except ValueError:
            data = {"error": response.text[:300]}
        return response.status_code, data

    async def set_leverage(
        self, market: dict[str, Any], *, lighter_leverage: int, arcus_leverage: int,
    ) -> tuple[bool, str | None]:
        """开仓前把两边这个市场的杠杆设成任务要的值。

        上一个版本 2026-09-19 之前从来不设：两边用的是账户里各市场原来的设置，
        任务上写的杠杆没生效 —— 低杠杆那边保证金被吃掉，下一个任务只开得出一半；
        高杠杆那边强平走廊比预想的窄。

        保证金模式：两边都用【全仓】。
          · 全仓时整个 Arcus 账户的钱都给这条腿兜底，同样的仓位离强平远得多
            （例：400 USDC 账户开 3000 美元 ETH，逐仓 20× 只有约 2.3%，全仓约 10%）；
          · 强平价由程序按账户权益复算，并按最坏情况把账户里其它 Arcus 仓位
            也算成同向亏损（见 arcus.cross_distance）；
          · 杠杆设置在全仓下只决定开仓要占多少初始保证金，不决定强平距离。
          · 从逐仓切回全仓交易所总是允许的。
        任何一边设置失败都返回 False —— 不知道实际杠杆，就不知道强平走廊有多宽，不开。
        """
        if self.dry_run:
            return True, None
        symbol = market["arcus_symbol"]
        ok, error = await self._set_arcus_leverage(market, int(arcus_leverage))
        if not ok:
            return False, f"Arcus 设置 {symbol} {arcus_leverage}× 全仓失败：{error}"
        try:
            async with self._lighter_lock:
                signer = self._get_lighter_signer()
                _, response, error = await signer.update_leverage(
                    int(market["lighter_market_index"]), signer.CROSS_MARGIN_MODE,
                    int(lighter_leverage),
                )
        except Exception as exc:
            return False, f"Lighter 设置 {market['lighter_symbol']} {lighter_leverage}× 杠杆失败：{exc}"
        if error or response is None or getattr(response, "code", 0) != 200:
            return False, (
                f"Lighter 设置 {market['lighter_symbol']} {lighter_leverage}× 杠杆被拒："
                f"{error or response}"
            )
        return True, None

    async def _set_arcus_leverage(self, market: dict[str, Any], leverage: int) -> tuple[bool, str | None]:
        """POST /v1/setLeverage（旧式签名：ts + "setLeverage" + canonicalJSON(body)）。

        200 APPLIED = 已生效；202 ACK = 已转给撮合引擎、还没确认 ——
        ACK 时去 /v1/leverages 查一下，确认杠杆和全仓都到位了才算数。
        """
        try:
            key, _ = self._get_arcus_key()
            body = {
                "address": self.settings.arcus_address,
                "accountIndex": int(self.settings.arcus_account_index),
                "marketId": int(market["arcus_market_id"]),
                "leverage": int(leverage),
                "isolated": False,
            }
            ts = ax.timestamp_ns()
            signature = ax.sign_hex(key, ax.legacy_message(ts, "setLeverage", body))
            status, data = await self._arcus_post("/v1/setLeverage", body, ts, signature)
        except ExchangeError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 —— 网络问题：没设上就不开
            return False, f"请求失败：{exc}"
        state = str((data or {}).get("status") or "").upper() if isinstance(data, dict) else ""
        if status == 200 and state in ("APPLIED", ""):
            return True, None
        if status == 202 or state == "ACK":
            return await self._confirm_arcus_leverage(market, leverage)
        reason = (data or {}).get("rejectReason") if isinstance(data, dict) else None
        return False, f"HTTP {status} {reason or data}"

    async def _confirm_arcus_leverage(self, market: dict[str, Any], leverage: int) -> tuple[bool, str | None]:
        last = None
        for _ in range(4):
            await asyncio.sleep(0.5)
            try:
                payload = await self.market._request(
                    "arcus", "GET", f"{self.settings.arcus_api_url}/v1/leverages",
                    label="/v1/leverages",
                    params={"address": self.settings.arcus_address,
                            "accountIndex": str(self.settings.arcus_account_index),
                            "market": str(int(market["arcus_market_id"]))},
                )
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                continue
            for row in (payload or {}).get("leverages") or []:
                if int(row.get("marketId", -1)) != int(market["arcus_market_id"]):
                    continue
                last = row
                if int(row.get("leverage") or 0) == int(leverage) and (
                    row.get("isolated") is False
                    or str(row.get("marginMode") or "").upper() == "CROSS"
                ):
                    return True, None
        return False, f"交易所确认超时（最后读到：{last}）"

    async def lighter_auth_token(self) -> str:
        """查成交记录用的短期令牌 —— 交易所要求主账户 / 子账户查成交必须带。

        照搬 Parallax：用签名器生成，10 分钟有效，提前 1 分钟换新。
        签名器同时在给下单签名，所以要拿同一把锁。
        """
        self._require_live()
        now = time.time()
        if self._auth_token and now < self._auth_token_expires_at - 60:
            return self._auth_token
        async with self._lighter_lock:
            signer = self._get_lighter_signer()
            token, error = signer.create_auth_token_with_expiry(
                api_key_index=self.settings.lighter_api_key_index
            )
        if error or not token:
            raise ExchangeError(f"Lighter 成交查询签名失败：{error or '未生成认证令牌'}")
        self._auth_token = token
        self._auth_token_expires_at = now + 10 * 60
        return token

    async def warm(self) -> None:
        """提前把签名客户端建好，别让它们的初始化延迟落在下单路径上。

        Lighter 的 SignerClient 内部有个 asyncio 感知的 nonce 管理器，
        必须在事件循环线程上构造 —— 不能丢进 to_thread。
        """
        if self.dry_run:
            return
        async with self._init_lock:
            self._get_lighter_signer()
            self._get_arcus_key()

    # ── 读仓位 ──────────────────────────────────────
    async def read_positions(self, lighter_symbol: str, arcus_market_id: int) -> tuple[float, float]:
        """返回两边的带符号仓位。读不到时返回 nan —— 调用方必须按
        「可能已成交」处理，不能当成 0。"""
        lighter_acct, arcus_acct = await asyncio.gather(
            self.market.lighter_account(), self.market.arcus_account(),
            return_exceptions=True,
        )
        lighter = float("nan")
        arcus = float("nan")
        index = self.settings.lighter_account_index
        if not isinstance(lighter_acct, Exception) and index is not None:
            leg = parse_lighter_position(lighter_acct, index, lighter_symbol)
            if leg is not None:
                lighter = leg.size
        if not isinstance(arcus_acct, Exception):
            leg = parse_arcus_position(arcus_acct, arcus_market_id)
            if leg is not None:
                arcus = leg.size
        return lighter, arcus

    async def _await_position_change(
        self, lighter_symbol: str, arcus_market_id: int,
        before_lighter: float, requested_signed: float,
    ) -> tuple[float, float, FillVerdict]:
        """轮询直到 Lighter 仓位出现变化，或重试用尽。"""
        lighter = before_lighter
        arcus = float("nan")
        verdict = classify_fill(
            before=before_lighter, after=before_lighter, requested_signed=requested_signed
        )
        for attempt in range(_CONFIRM_RETRIES):
            await asyncio.sleep(_CONFIRM_DELAY_SEC * (1 if attempt == 0 else 1.5))
            lighter, arcus = await self.read_positions(lighter_symbol, arcus_market_id)
            verdict = classify_fill(
                before=before_lighter, after=lighter, requested_signed=requested_signed
            )
            if verdict.is_fill:
                break
        return lighter, arcus, verdict

    async def _await_arcus_change(
        self, lighter_symbol: str, arcus_market_id: int,
        before_arcus: float, requested_signed: float,
    ) -> tuple[float, FillVerdict]:
        """对冲腿发出后，轮询 Arcus 仓位直到出现变化。"""
        after = before_arcus
        verdict = classify_fill(before=before_arcus, after=before_arcus,
                                requested_signed=requested_signed)
        for attempt in range(_ARCUS_CONFIRM_RETRIES):
            await asyncio.sleep(_CONFIRM_DELAY_SEC * (1 if attempt == 0 else 1.2))
            _, after = await self.read_positions(lighter_symbol, arcus_market_id)
            verdict = classify_fill(before=before_arcus, after=after,
                                    requested_signed=requested_signed)
            if verdict.kind == "full" or (verdict.is_fill and math.isfinite(verdict.filled)):
                break
        return after, verdict

    # ── 单腿下单 ────────────────────────────────────
    async def _lighter_ioc(
        self, market_id: int, side: str, quantity: float,
        price: float, decimals: tuple[int, int], reduce_only: bool = False,
    ) -> LegResult:
        size_decimals, price_decimals = decimals
        if self.dry_run:
            return LegResult("lighter", True, raw={
                "dry_run": True, "side": side, "quantity": quantity, "price": price,
                "reduce_only": reduce_only,
            })
        async with self._lighter_lock:
            signer = self._get_lighter_signer()
            aligned = align_quantity(quantity, size_decimals)
            if aligned <= 0:
                return LegResult("lighter", False, submitted=False,
                                 error=f"数量 {quantity:g} 对齐后为 0")
            base_amount = int(round(aligned * (10 ** size_decimals)))
            wire_price = int(round(price * (10 ** price_decimals)))
            self._client_order_index += 1
            try:
                created, response, error = await signer.create_order(
                    market_index=market_id,
                    client_order_index=self._client_order_index,
                    base_amount=base_amount,
                    price=wire_price,
                    is_ask=side == "sell",
                    order_type=signer.ORDER_TYPE_MARKET,
                    time_in_force=signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                    order_expiry=signer.DEFAULT_IOC_EXPIRY,
                    reduce_only=reduce_only,
                )
            finally:
                self._last_lighter_order_at = time.monotonic()
            ok = response is not None and getattr(response, "code", 0) == 200 and not error
            # 注意：ok 只代表【报单被接受】，不代表成交 ——
            # 2026-09-17 那笔就是 code=200 + tx_hash 但零成交。
            raw = {"created": str(created), "response": str(response)}
            return LegResult(
                "lighter", ok, raw=raw,
                error=error or (None if ok else "Lighter 拒单"),
                side=side, requested=aligned, order_ref=lighter_tx_hash(raw),
            )

    def build_arcus_order(
        self, market: dict[str, Any], side: str, quantity: float, price: float,
        reduce_only: bool = False, time_in_force: str = "IOC",
    ) -> dict[str, Any]:
        """对齐精度 + 签名 + 组装请求体，但不发送。实盘预检也用它「只签不发」。

        价格按方向对齐到 tick（买往上、卖往下），数量向下对齐到 stepSize。
        """
        key, _ = self._get_arcus_key()
        if time_in_force == "ALO":
            # 挂单价由调用方算好（ax.passive_price，已对齐 tick 且在自己这一侧），
            # 这里不能再按「更激进」取整 —— 往对手方向挪一格可能就变成吃单被拒
            limit = ax.D(price)
            ax.to_units(limit, market["arcus_tick_size"])
        else:
            limit = ax.round_arcus_price(market, price, side)
        qty = ax.align_arcus_quantity(market, quantity)
        if qty <= 0:
            raise ExchangeError(f"数量 {quantity:g} 对齐到 Arcus 步长 {market['arcus_step_size']} 后为 0")
        max_size = float(market.get("arcus_max_order_size") or 0)
        if max_size and float(qty) > max_size:
            raise ExchangeError(f"数量 {qty} 超过 Arcus 单笔上限 {max_size:g}")
        tick_units = ax.to_units(limit, market["arcus_tick_size"])
        step_units = ax.to_units(qty, market["arcus_step_size"])
        ts = ax.timestamp_ns()
        gtt = ax.good_til_us()
        client_id = ax.new_client_id()
        order_side = "SELL" if side == "sell" else "BUY"
        payload = ax.build_place_payload(
            address=self.settings.arcus_address,
            account_index=self.settings.arcus_account_index,
            client_id=client_id, timestamp=ts, good_til_us_value=gtt,
            market_id=int(market["arcus_market_id"]), price_ticks=tick_units,
            quantity_quantums=step_units, reduce_only=reduce_only,
            side=order_side, time_in_force=time_in_force,
        )
        body = {
            "address": self.settings.arcus_address,
            "accountIndex": int(self.settings.arcus_account_index),
            "marketId": int(market["arcus_market_id"]),
            "orderSide": order_side,
            "orderType": "LIMIT",
            "quantity": ax.fmt_decimal(qty),
            "price": ax.fmt_decimal(limit),
            "timeInForce": time_in_force,
            # HTTP 里 goodTilTime 是【微秒】十进制字符串；签名载荷里的 g 是纳秒
            "goodTilTime": str(gtt),
            "reduceOnly": bool(reduce_only),
            "clientId": client_id,
            "timestamp": ts,
        }
        return {
            "body": body, "payload": payload, "timestamp": ts,
            "signature": ax.sign_hex(key, payload), "client_id": client_id,
            "quantity": float(qty), "price": float(limit),
        }

    async def _arcus_ioc(
        self, market: dict[str, Any], side: str, quantity: float,
        price: float, reduce_only: bool = False,
    ) -> LegResult:
        """Arcus IOC 限价单。成交与否最终【看仓位】，回执只作参考。"""
        return await self._arcus_place(market, side, quantity, price, reduce_only, "IOC")

    async def _arcus_place(
        self, market: dict[str, Any], side: str, quantity: float,
        price: float, reduce_only: bool = False, time_in_force: str = "IOC",
    ) -> LegResult:
        """发一张 Arcus 限价单（IOC 吃单 / ALO 只挂单）。只发一次，绝不重试。"""
        if self.dry_run:
            return LegResult("arcus", True, raw={
                "dry_run": True, "side": side, "quantity": quantity, "price": price,
                "reduce_only": reduce_only,
            })
        try:
            order = self.build_arcus_order(market, side, quantity, price, reduce_only,
                                           time_in_force)
        except (ExchangeError, ValueError, KeyError) as exc:
            # 还没发出去：签名 / 精度层面的问题
            return LegResult("arcus", False, submitted=False, error=str(exc),
                             side=side, requested=abs(quantity))
        try:
            status, data = await self._arcus_post(
                "/v1/placeOrder", order["body"], order["timestamp"], order["signature"]
            )
        except Exception as exc:  # noqa: BLE001
            # 超时 / 断流：单子可能已经到了交易所。标记为结果未知，交给仓位判定。
            return LegResult(
                "arcus", False, raw={"clientId": order["client_id"], "error": str(exc)},
                error=f"Arcus 下单请求中断，结果未知（{exc}）",
                side=side, requested=order["quantity"], uncertain=True,
                order_ref=ax.order_ref(None, order["client_id"]),
            )
        receipt = ax.parse_order_response(status, data, order["client_id"])
        raw = {"http": status, "orderId": receipt.order_id, "clientId": receipt.client_id,
               "status": receipt.status, "body": data}
        # 202 时回执里没有成交量；有的话先记上，稍后仍按仓位复核
        filled = receipt.filled or 0.0
        return LegResult(
            "arcus", receipt.accepted, raw=raw, error=receipt.error,
            filled=filled if receipt.accepted else 0.0,
            side=side, requested=order["quantity"],
            order_ref=ax.order_ref(receipt.order_id, receipt.client_id),
            # 被交易所当场拒掉的单不可能成交；没被拒的都有可能成交
            submitted=receipt.accepted or bool(receipt.order_id),
        )

    async def _arcus_cancel(self, market: dict[str, Any], order_id: str) -> tuple[bool, str]:
        """撤一张 Arcus 挂单。返回 (请求是否被受理, 状态)。

        状态 FILLED = 想撤的时候已经成交了 —— 那不是「撤掉了」，是有仓位，
        调用方必须重新读仓位去对冲。所以调用方撤完一律重读仓位，不看这里的状态做决定。
        """
        try:
            key, _ = self._get_arcus_key()
            ts = ax.timestamp_ns()
            payload = ax.build_cancel_payload(
                address=self.settings.arcus_address,
                account_index=self.settings.arcus_account_index,
                timestamp=ts, market_id=int(market["arcus_market_id"]), order_id=str(order_id),
            )
            body = {"kind": "orderId", "orderId": str(order_id),
                    "address": self.settings.arcus_address,
                    "marketId": int(market["arcus_market_id"]),
                    "accountIndex": int(self.settings.arcus_account_index), "timestamp": ts}
            status, data = await self._arcus_post("/v1/cancelOrder", body, ts,
                                                  ax.sign_hex(key, payload))
        except Exception as exc:  # noqa: BLE001
            return False, f"撤单请求失败：{exc}"
        state = str((data or {}).get("status") or "").upper() if isinstance(data, dict) else ""
        return status in (200, 202), state or f"HTTP {status}"

    async def read_arcus_size(self, market_id: int) -> float:
        """只读 Arcus 一个市场的仓位（挂单轮询用，不碰 Lighter 的限流闸门）。读不到返回 nan。"""
        try:
            return float(await self.market.arcus_position_size(int(market_id)))
        except Exception:  # noqa: BLE001
            return float("nan")

    # ── 双腿开仓 ────────────────────────────────────
    async def open_pair(
        self, *, market: dict[str, Any], direction: str, quantity: float,
        lighter_price: float, arcus_price: float,
        slippage_bps: float, lighter_decimals: tuple[int, int],
        quotes: dict[str, Any] | None = None,
    ) -> PairResult:
        started = time.monotonic()
        lighter_side, arcus_side = hedge_side(direction)
        quotes = dict(quotes or {})
        quotes["lighter_limit"] = slippage_limit_price(lighter_price, lighter_side, slippage_bps)
        # 记录【真正发出去的】限价，而不是对齐前那个 —— 定位「报单成功零成交」全靠它
        quotes["arcus_limit_raw"] = slippage_limit_price(arcus_price, arcus_side, slippage_bps)
        try:
            quotes["arcus_limit"] = arcus_limit_price(
                market, quotes["arcus_limit_raw"], arcus_side
            )
        except ExchangeError as exc:
            # 价格算不出来就【在下第一单之前】退出。_run_cycle 没有 try/except，
            # 让异常穿出去会打断整轮所有币种的任务。
            return PairResult(
                False, "bad_arcus_price", reason=str(exc),
                dry_run=self.dry_run, quotes=quotes,
                elapsed_ms=(time.monotonic() - started) * 1000,
                notes=["尚未下任何单"],
            )
        quotes["lighter_side"] = lighter_side
        quotes["arcus_side"] = arcus_side
        quotes["slippage_bps"] = slippage_bps
        # 引擎已经把 enforce_cheap_side 放进 quotes。直接调用开仓的测试不带这个标记，
        # 仍按它们原来的价格走；实盘开仓 / 补仓走到这里时，发单前再拒一次贵的一边。
        if quotes.get("enforce_cheap_side"):
            prices_ok, join_bps, prices_why = open_sides_allowed(
                lighter_side, arcus_side, lighter_price, arcus_price, None,
            )
            quotes["join_gap_bps"] = None if join_bps is None else round(join_bps, 4)
            if not prices_ok:
                return PairResult(
                    False, "spread_wait", reason=prices_why, dry_run=self.dry_run,
                    quotes=quotes,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    notes=["买卖价没有通过最后一道价差检查，尚未下任何单"],
                )
        lighter_symbol = market["lighter_symbol"]
        arcus_id = int(market["arcus_market_id"])
        notes: list[str] = []

        if not self.dry_run:
            # 先把 Arcus 那张单【签好但不发】：私钥不对、精度对不上、超出单笔上限
            # 这类问题必须在下 Lighter 之前暴露。否则 Lighter 成交了才发现 Arcus
            # 下不出去，只能再花一笔去抢救裸腿。
            try:
                self.build_arcus_order(market, arcus_side, quantity, quotes["arcus_limit"])
            except (ExchangeError, ValueError, KeyError) as exc:
                return PairResult(
                    False, "arcus_order_invalid", reason=f"Arcus 这张单签不出来：{exc}",
                    dry_run=False, quotes=quotes,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    notes=["尚未下任何单"],
                )

        before_lighter, before_arcus = await self.read_positions(lighter_symbol, arcus_id)
        requested_signed = quantity if lighter_side == "buy" else -quantity

        # 第 2 步：先下 Lighter
        lighter = await self._lighter_ioc(
            market["lighter_market_index"], lighter_side, quantity,
            slippage_limit_price(lighter_price, lighter_side, slippage_bps),
            lighter_decimals,
        )
        _stamp(lighter, side=lighter_side, requested=quantity, reference_price=lighter_price)

        if self.dry_run:
            arcus = await self._arcus_ioc(
                market, arcus_side, quantity,
                slippage_limit_price(arcus_price, arcus_side, slippage_bps),
            )
            _stamp(arcus, side=arcus_side, requested=quantity, reference_price=arcus_price)
            _simulate_fill(lighter)
            _simulate_fill(arcus)
            return PairResult(
                True, "dry_run", lighter, arcus, dry_run=True, quotes=quotes,
                elapsed_ms=(time.monotonic() - started) * 1000,
                notes=[f"演练：Lighter {lighter_side} {quantity:g} / Arcus {arcus_side} {quantity:g}"],
                ledger=[("open", lighter), ("open", arcus)],
            )

        # 第 3~4 步：只认仓位变化
        after_lighter, after_arcus, verdict = await self._await_position_change(
            lighter_symbol, arcus_id, before_lighter, requested_signed
        )
        lighter.filled = 0.0 if verdict.kind == "none" else abs(verdict.filled)
        # 读不到仓位时 filled 是 nan：不算「已证实」，交给成交记录对账
        lighter.fill_confirmed = math.isfinite(lighter.filled)
        if not lighter.fill_confirmed:
            lighter.filled = 0.0

        # 第 5 步：没成交 → 根本不向 Arcus 下单
        arcus_untouched = positions_match(after_arcus, before_arcus, quantity)
        if not verdict.should_hedge and arcus_untouched:
            return PairResult(
                False, "lighter_not_filled", lighter,
                LegResult("arcus", False, submitted=False,
                          raw={"blocked_by": "lighter_not_filled"},
                          error="Lighter 未成交，因此未向 Arcus 下单"),
                reason=verdict.reason or lighter.error or "Lighter IOC 未产生仓位变化",
                quotes=quotes,
                elapsed_ms=(time.monotonic() - started) * 1000,
                notes=["两边均无仓位变化，已安全退出"],
                ledger=[("open", lighter)] if lighter.submitted else [],
            )

        # 第 6 步：按【实际成交量】对冲，不是请求量
        hedge_quantity = quantity
        if verdict.kind == "partial" and verdict.filled == verdict.filled:  # 非 nan
            hedge_quantity = align_quantity(abs(verdict.filled), market["quantity_decimals"])
            notes.append(
                f"Lighter 只成交 {hedge_quantity:g}/{quantity:g}，Arcus 按实际成交量对冲"
            )
        if hedge_quantity <= 0:
            hedge_quantity = quantity

        arcus = await self._arcus_ioc(
            market, arcus_side, hedge_quantity,
            slippage_limit_price(arcus_price, arcus_side, slippage_bps),
        )
        _stamp(arcus, side=arcus_side, requested=hedge_quantity, reference_price=arcus_price)

        # Arcus 也必须按【仓位变化】确认成交。报单被接受 ≠ 成交：
        # 上一个版本 2026-09-18 就是只看了回执，程序以为对冲好了，实际留着一条裸腿。
        # 请求中断（结果未知）的也要看仓位 —— 它可能已经成交了。
        arcus_requested = hedge_quantity if arcus_side == "buy" else -hedge_quantity
        if arcus.ok or arcus.uncertain:
            after_arcus2, arcus_verdict = await self._await_arcus_change(
                lighter_symbol, arcus_id, after_arcus, arcus_requested,
            )
            measured = 0.0 if arcus_verdict.kind == "none" else abs(arcus_verdict.filled)
            if math.isfinite(measured):
                arcus.filled = measured
                arcus.fill_confirmed = True
            if arcus_verdict.should_hedge:
                if arcus.uncertain:
                    arcus.ok = True
                    arcus.submitted = True
                    arcus.error = None
                    notes.append("Arcus 下单请求中断，但仓位确认已成交")
            else:
                arcus.ok = False
                arcus.error = (
                    f"Arcus 报单被接受但仓位没有变化（{arcus_verdict.reason}）"
                    if not arcus.uncertain else
                    f"Arcus 下单请求中断，仓位也没有变化（{arcus_verdict.reason}）"
                )

        # 第 7 步：Arcus 没成交 → 立刻抢救 Lighter 那条腿
        if not arcus.ok:
            rescue = await self._flatten(
                market, "lighter", hedge_quantity,
                "sell" if lighter_side == "buy" else "buy",
                lighter_price, slippage_bps, lighter_decimals,
            )
            ledger = [("open", lighter)]
            # 被交易所拒掉的单（没有订单号）不可能有成交，不进账本；
            # 有订单号的带着订单号进账本，交给对账去定
            if arcus.submitted and arcus.order_ref:
                ledger.append(("open", arcus))
            if rescue.submitted:
                ledger.append(("rescue", rescue))
            return PairResult(
                False, "arcus_failed_rescued", lighter, arcus, rescue,
                reason=f"Arcus 下单失败：{arcus.error} —— 已市价平掉 Lighter 那条腿",
                quotes=quotes,
                elapsed_ms=(time.monotonic() - started) * 1000,
                notes=notes + ["Lighter 曾裸露，已抢救" if rescue.ok else "⚠ 抢救失败，Lighter 仍裸露"],
                ledger=ledger,
            )

        return PairResult(
            True, "opened", lighter, arcus, quotes=quotes,
            elapsed_ms=(time.monotonic() - started) * 1000, notes=notes,
            ledger=[("open", lighter), ("open", arcus)],
        )

    # ── 平仓 ────────────────────────────────────────
    async def close_pair(
        self, *, market: dict[str, Any], lighter_size: float, arcus_size: float,
        lighter_price: float, arcus_price: float, slippage_bps: float,
        lighter_decimals: tuple[int, int],
    ) -> PairResult:
        """双腿同时平掉。两条腿都用 reduce_only，防止方向算错时反手开仓。

        成交同样【只认仓位变化】：两条腿都要在仓位上看到归零才算平掉。
        （上一个版本 2026-09-19 之前平仓只看报单返回，一夜 22 次平仓的
        Lighter 腿全记成 filled=0 —— 那是「没测」，不是「没成交」。）
        """
        started = time.monotonic()
        lighter_side, arcus_side = closing_sides(lighter_size, arcus_size)

        async def already_flat(venue: str) -> LegResult:
            # 已经是 0 的腿不下单 —— 发一张数量 0 的单只会换来一条报错
            return LegResult(venue, True, submitted=False,
                             raw={"skipped": "already_flat"}, fill_confirmed=True)

        lighter_task = (
            self._lighter_ioc(
                market["lighter_market_index"], lighter_side, abs(lighter_size),
                slippage_limit_price(lighter_price, lighter_side, slippage_bps),
                lighter_decimals, reduce_only=True,
            ) if _is_open(lighter_size) else already_flat("lighter")
        )
        arcus_task = (
            self._arcus_ioc(
                market, arcus_side, abs(arcus_size),
                slippage_limit_price(arcus_price, arcus_side, slippage_bps),
                reduce_only=True,
            ) if _is_open(arcus_size) else already_flat("arcus")
        )
        lighter, arcus = await asyncio.gather(lighter_task, arcus_task)
        _stamp(lighter, side=lighter_side, requested=abs(lighter_size),
               reference_price=lighter_price)
        _stamp(arcus, side=arcus_side, requested=abs(arcus_size),
               reference_price=arcus_price)
        ledger = [("close", leg) for leg in (lighter, arcus) if leg.submitted or leg.uncertain]

        if self.dry_run:
            _simulate_fill(lighter)
            _simulate_fill(arcus)
            return PairResult(
                True, "closed", lighter, arcus, dry_run=True,
                elapsed_ms=(time.monotonic() - started) * 1000, ledger=ledger,
            )

        after_lighter, after_arcus = await self._await_flat(
            market["lighter_symbol"], int(market["arcus_market_id"]),
            lighter_size, arcus_size,
        )
        still_open: list[str] = []
        for name, leg, before, after in (
            ("Lighter", lighter, lighter_size, after_lighter),
            ("Arcus", arcus, arcus_size, after_arcus),
        ):
            if math.isfinite(after) and (leg.submitted or leg.uncertain):
                # reduce_only 不会反手，所以成交量就是仓位绝对值缩小了多少
                leg.filled = max(0.0, abs(before) - abs(after))
                leg.fill_confirmed = True
            if not (_is_flat(after, before)
                    or (not leg.submitted and not leg.uncertain and not _is_open(after))):
                still_open.append(name)
        ok = not still_open
        return PairResult(
            ok, "closed" if ok else "close_partial", lighter, arcus,
            reason=None if ok else (
                f"{'、'.join(still_open)} 腿平仓后仍有仓位（或读不到仓位）"
                f" —— 不标记为已平，下一轮会重新检查并处理孤腿"
            ),
            dry_run=False,
            elapsed_ms=(time.monotonic() - started) * 1000,
            notes=[f"平仓后仓位：Lighter {after_lighter:g} / Arcus {after_arcus:g}"],
            ledger=ledger,
        )

    async def _await_flat(
        self, lighter_symbol: str, arcus_market_id: int,
        lighter_before: float, arcus_before: float,
    ) -> tuple[float, float]:
        """轮询直到两条腿都归零，或重试用尽。节奏和开仓确认一致。"""
        after_lighter = after_arcus = float("nan")
        for attempt in range(_CONFIRM_RETRIES):
            await asyncio.sleep(_CONFIRM_DELAY_SEC * (1 if attempt == 0 else 1.5))
            after_lighter, after_arcus = await self.read_positions(
                lighter_symbol, arcus_market_id
            )
            lighter_done = _is_flat(after_lighter, lighter_before) or (
                not _is_open(lighter_before) and not _is_open(after_lighter)
            )
            arcus_done = _is_flat(after_arcus, arcus_before) or (
                not _is_open(arcus_before) and not _is_open(after_arcus)
            )
            if lighter_done and arcus_done:
                break
        return after_lighter, after_arcus

    async def _flatten(
        self, market: dict[str, Any], venue: str, quantity: float, side: str,
        price: float, slippage_bps: float, lighter_decimals: tuple[int, int],
    ) -> LegResult:
        """单腿市价平掉 —— 只用于抢救裸腿，滑点放宽到 3 倍。"""
        wide = slippage_bps * 3
        if venue == "lighter":
            leg = await self._lighter_ioc(
                market["lighter_market_index"], side, quantity,
                slippage_limit_price(price, side, wide), lighter_decimals,
                reduce_only=True,
            )
        else:
            leg = await self._arcus_ioc(
                market, side, quantity,
                slippage_limit_price(price, side, wide), reduce_only=True,
            )
        _stamp(leg, side=side, requested=quantity, reference_price=price)
        if self.dry_run:
            _simulate_fill(leg)
        return leg

    async def _lighter_post_only(
        self, market_id: int, side: str, quantity: float,
        price: float, decimals: tuple[int, int], reduce_only: bool = False,
    ) -> LegResult:
        """Lighter 只挂单。演练模式到不了这里。"""
        size_decimals, price_decimals = decimals
        async with self._lighter_lock:
            signer = self._get_lighter_signer()
            aligned = align_quantity(quantity, size_decimals)
            if aligned <= 0:
                return LegResult("lighter", False, submitted=False,
                                 error=f"数量 {quantity:g} 对齐后为 0")
            base_amount = int(round(aligned * (10 ** size_decimals)))
            scale = 10 ** price_decimals
            # 买往下取整、卖往上取整，避免把 post-only 价取到对手价对面被拒
            if side == "buy":
                wire_price = int(math.floor(price * scale + 1e-9))
            else:
                wire_price = int(math.ceil(price * scale - 1e-9))
            if wire_price <= 0:
                return LegResult("lighter", False, submitted=False, error="挂单价无效")
            self._client_order_index += 1
            client_index = self._client_order_index
            try:
                created, response, error = await signer.create_order(
                    market_index=market_id,
                    client_order_index=client_index,
                    base_amount=base_amount,
                    price=wire_price,
                    is_ask=side == "sell",
                    order_type=signer.ORDER_TYPE_LIMIT,
                    time_in_force=signer.ORDER_TIME_IN_FORCE_POST_ONLY,
                    order_expiry=signer.DEFAULT_28_DAY_ORDER_EXPIRY,
                    reduce_only=reduce_only,
                )
            finally:
                self._last_lighter_order_at = time.monotonic()
            ok = response is not None and getattr(response, "code", 0) == 200 and not error
            raw = {"created": str(created), "response": str(response),
                   "client_order_index": client_index}
            return LegResult(
                "lighter", ok, raw=raw,
                error=error or (None if ok else "Lighter 拒单"),
                side=side, requested=aligned, order_ref=lighter_tx_hash(raw),
            )

    async def _lighter_cancel(self, market_id: int, order_index: int) -> None:
        if self.dry_run:
            return
        async with self._lighter_lock:
            signer = self._get_lighter_signer()
            await signer.cancel_order(market_index=market_id, order_index=order_index)

    async def _sleep_gap(self, seconds: float, stop: asyncio.Event | None) -> bool:
        """等到间隔走完。返回 False 表示被叫停，调用方要撤单。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                return False
            await asyncio.sleep(min(0.5, deadline - time.monotonic()))
        return not (stop is not None and stop.is_set())

    async def place_maker_pair(
        self, *, market: dict[str, Any], lighter_side: str, arcus_side: str,
        lighter_quantity: float, arcus_quantity: float,
        lighter_price: float, arcus_price: float,
        lighter_decimals: tuple[int, int], quotes: dict[str, Any] | None = None,
        reduce_only: bool = False, action: str = "open",
        stop: asyncio.Event | None = None,
        direction: str | None = None, quantity: float | None = None,
    ) -> PairResult:
        """两边一起挂 maker。开仓不在两腿之间插入间隔。

        3 秒 / 300 秒是成交之后的持仓时钟（MIN_HOLD_SEC / MAX_HOLD_SEC），
        由引擎在平仓决策里用，不在这里睡眠。
        挂单若一直不成交，最多等 MAKER_WAIT_SECONDS 再撤，避免留下单腿。
        演练模式只记录意图，不睡眠、不签名、不提交。
        """
        started = time.monotonic()
        quotes = dict(quotes or {})
        min_hold = max(0.0, float(self.settings.min_hold_sec))
        max_hold = max(min_hold, float(self.settings.max_hold_sec))
        quotes["maker_rule"] = {
            "both_maker": True,
            "legs_together": True,
            "min_hold_sec": min_hold,
            "max_hold_sec": max_hold,
            "lighter_side": lighter_side,
            "arcus_side": arcus_side,
            "lighter_join": lighter_price,
            "arcus_join": arcus_price,
            "reduce_only": reduce_only,
        }
        # 开仓、补仓：买价必须严格低于卖价。价差多少 bp 不再拦截。
        # 计划内平仓不看买卖谁贵，引擎已经按浮盈亏决定要平，这里只挂 maker。
        # 风控强制平仓走 close_pair，不进这里。
        if action in ("open", "topup"):
            prices_ok, join_bps, prices_why = open_sides_allowed(
                lighter_side, arcus_side, lighter_price, arcus_price, None,
            )
            quotes["join_gap_bps"] = None if join_bps is None else round(join_bps, 4)
            # 所间价差不拦开仓。买价必须严格低于卖价，宽多少都可以挂。
            if not prices_ok:
                return PairResult(
                    False, "spread_wait",
                    reason=prices_why, dry_run=self.dry_run,
                    quotes=quotes,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    notes=["买卖价没有通过最后一道价差检查，尚未下任何单"],
                )
        stage_ok = {"open": "dry_run", "topup": "opened", "close": "closed"}.get(action, "dry_run")
        note = (
            f"演练：买价低于卖价才开仓，两边同时挂 maker（买跟买一、卖跟卖一），开仓不间隔。"
            f"两腿都成交后至少持有 {min_hold:g} 秒；"
            f"之后两腿浮盈亏合计不低于差额就挂 maker 平，不看价差；"
            f"满 {max_hold:g} 秒仍未平掉就跟盘挂 maker 平，亏过差额也不改吃单。本次未发单。"
        )
        if action == "close":
            quotes["close_favorable"] = True
            quotes["close_prices"] = (
                f"挂 maker 平仓：Lighter {lighter_side} {float(lighter_price):g} / "
                f"Arcus {arcus_side} {float(arcus_price):g}"
            )
        if self.dry_run:
            lighter = LegResult("lighter", True, raw={
                "dry_run": True, "side": lighter_side, "quantity": lighter_quantity,
                "price": lighter_price, "reduce_only": reduce_only, "post_only": True,
            })
            arcus = LegResult("arcus", True, raw={
                "dry_run": True, "side": arcus_side, "quantity": arcus_quantity,
                "price": arcus_price, "reduce_only": reduce_only, "timeInForce": "ALO",
            })
            _stamp(lighter, side=lighter_side, requested=lighter_quantity,
                   reference_price=lighter_price)
            _stamp(arcus, side=arcus_side, requested=arcus_quantity,
                   reference_price=arcus_price)
            _simulate_fill(lighter)
            _simulate_fill(arcus)
            quotes["filled_quantity"] = float(quantity if quantity is not None else lighter_quantity)
            live_stage = "closed" if action == "close" else stage_ok
            return PairResult(
                True, live_stage, lighter, arcus, dry_run=True, quotes=quotes,
                elapsed_ms=(time.monotonic() - started) * 1000,
                notes=[note],
                ledger=[(action if action != "topup" else "open", lighter),
                        (action if action != "topup" else "open", arcus)],
            )

        # 实盘：先把 Arcus 签好。签不出来就不要去挂 Lighter。
        if arcus_quantity > 0:
            try:
                self.build_arcus_order(
                    market, arcus_side, arcus_quantity, arcus_price, reduce_only, "ALO",
                )
            except (ExchangeError, ValueError, KeyError) as exc:
                return PairResult(
                    False, "arcus_order_invalid", reason=f"Arcus 挂单签不出来：{exc}",
                    dry_run=False, quotes=quotes,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    notes=["尚未下任何单"],
                )

        lighter_symbol = market["lighter_symbol"]
        arcus_id = int(market["arcus_market_id"])
        before_lighter, before_arcus = await self.read_positions(lighter_symbol, arcus_id)
        # 读仓位之后价格可能已经变了。用此刻盘口重算，闸门不过就不发；
        # 发出去的必须是刚刚检查过的那两个价，不能用函数开头那份旧价。
        fresh = await self._fresh_maker_prices(
            market, lighter_side, arcus_side, closing=(action == "close"),
        )
        if fresh is None:
            return PairResult(
                False, "close_wait" if action == "close" else "spread_wait",
                reason="读不到两边盘口，不下单", dry_run=False,
                quotes=quotes, elapsed_ms=(time.monotonic() - started) * 1000,
                notes=["买卖价没有通过最后一道价差检查，尚未下任何单"],
            )
        prices_ok, prices_why, lighter_price, arcus_price = fresh
        quotes["lighter_join"] = lighter_price
        quotes["arcus_join"] = arcus_price
        quotes["maker_rule"]["lighter_join"] = lighter_price
        quotes["maker_rule"]["arcus_join"] = arcus_price
        if not prices_ok:
            return PairResult(
                False, "close_wait" if action == "close" else "spread_wait",
                reason=prices_why, dry_run=False,
                quotes=quotes, elapsed_ms=(time.monotonic() - started) * 1000,
                notes=["买卖价没有通过最后一道价差检查，尚未下任何单"],
            )
        if action == "close":
            net_ok, net_why = _close_send_allowed(quotes, float(lighter_price), float(arcus_price))
            if not net_ok:
                return PairResult(
                    False, "close_wait", reason=net_why, dry_run=False, quotes=quotes,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    notes=["按即将发出的两个价格估算过不了浮盈亏差额，尚未下任何单"],
                )
            quotes["close_favorable"] = True
            quotes["close_prices"] = (
                f"挂 maker 平仓：Lighter {lighter_side} {float(lighter_price):g} / "
                f"Arcus {arcus_side} {float(arcus_price):g}"
            )
        t0 = time.monotonic()
        arcus = LegResult("arcus", True, submitted=False, raw={"skipped": "flat"})
        if arcus_quantity > 0:
            arcus = await self._arcus_place(
                market, arcus_side, arcus_quantity, arcus_price, reduce_only, "ALO",
            )
            _stamp(arcus, side=arcus_side, requested=arcus_quantity, reference_price=arcus_price)
        if arcus_quantity > 0 and not arcus.ok and not arcus.uncertain:
            return PairResult(
                False, "arcus_not_resting", arcus=arcus, reason=arcus.error or "Arcus 挂单被拒",
                quotes=quotes, elapsed_ms=(time.monotonic() - started) * 1000,
                notes=["Lighter 尚未下单"],
                ledger=[("open" if action != "close" else "close", arcus)] if arcus.submitted else [],
            )
        if stop is not None and stop.is_set():
            await self._cancel_maker_resting(market, arcus, None)
            return PairResult(
                False, "maker_stopped", arcus=arcus, reason="挂单被叫停，已撤未成交的单",
                quotes=quotes, elapsed_ms=(time.monotonic() - started) * 1000,
            )

        # 第二腿马上挂，不插入 3 秒。3 秒是成交后的最短持有，不是下单间隔。
        # Arcus 那一腿已经在路上，发 Lighter 之前用此刻的价再查一次。
        fresh = await self._fresh_maker_prices(
            market, lighter_side, arcus_side, closing=(action == "close"),
        )
        posted_arcus = float(arcus.price) if arcus.price else arcus_price
        leg_ok, leg_why = False, "读不到两边盘口，不下单"
        if fresh is not None:
            _ok, _why, live_l, _live_a = fresh
            if action == "close":
                # 平仓不因为买价高于卖价撤掉。但第二腿必须仍能通过浮盈亏差额。
                # 过不了就撤掉已经挂出、还没成交的那一腿，不改吃单，除非最长持有强制平。
                if quotes.get("force_close"):
                    leg_ok = bool(_ok) and float(live_l) > 0
                    leg_why = _why
                else:
                    leg_ok, leg_why = _close_send_allowed(quotes, float(live_l), posted_arcus)
                    if not _ok:
                        leg_ok, leg_why = False, _why or leg_why
            else:
                leg_ok, _bps, leg_why = open_sides_allowed(
                    lighter_side, arcus_side, float(live_l), posted_arcus, None,
                )
            if leg_ok:
                lighter_price = float(live_l)
                if action == "close":
                    quotes["lighter_join"] = lighter_price
                    quotes["arcus_join"] = posted_arcus
                    quotes["close_prices"] = (
                        f"挂 maker 平仓：Lighter {lighter_side} {float(lighter_price):g} / "
                        f"Arcus {arcus_side} {float(posted_arcus):g}"
                    )
        if not leg_ok:
            await self._cancel_maker_resting(market, arcus, None)
            after_l, after_a = await self.read_positions(lighter_symbol, arcus_id)
            # 不成交的单撤掉就行。已经成交的一条腿挂 maker 退出，开仓和平仓都不吃单。
            await self._settle_one_leg_as_maker(
                market, action, lighter_side, arcus_side,
                before_lighter, before_arcus, after_l, after_a,
                lighter_price, posted_arcus, lighter_decimals, quotes,
            )
            note = "Lighter 尚未下单；未成交的 maker 已撤，成交的部分改挂 maker 退出，不吃单"
            return PairResult(
                False, "close_wait" if action == "close" else "spread_wait",
                arcus=arcus, reason=leg_why, quotes=quotes,
                elapsed_ms=(time.monotonic() - started) * 1000,
                notes=[note],
            )
        # Lighter 用刚对过已挂 Arcus 价的那个价。Arcus 不再改价。
        lighter = LegResult("lighter", True, submitted=False, raw={"skipped": "flat"})
        if lighter_quantity > 0:
            lighter = await self._lighter_post_only(
                market["lighter_market_index"], lighter_side, lighter_quantity,
                lighter_price, lighter_decimals, reduce_only,
            )
            _stamp(lighter, side=lighter_side, requested=lighter_quantity,
                   reference_price=lighter_price)

        lighter_signed = lighter_quantity if lighter_side == "buy" else -lighter_quantity
        arcus_signed = arcus_quantity if arcus_side == "buy" else -arcus_quantity
        # 这是挂单等到成交的上限，不是持仓 300 秒。持仓时钟在引擎里。
        deadline = t0 + max(1.0, float(self.settings.maker_wait_seconds))
        filled_l = filled_a = False
        after_l, after_a = before_lighter, before_arcus
        while time.monotonic() < deadline:
            after_l, after_a = await self.read_positions(lighter_symbol, arcus_id)
            filled_l = lighter_quantity <= 0 or classify_fill(
                before=before_lighter, after=after_l, requested_signed=lighter_signed,
            ).kind == "full"
            filled_a = arcus_quantity <= 0 or classify_fill(
                before=before_arcus, after=after_a, requested_signed=arcus_signed,
            ).kind == "full"
            if filled_l and filled_a:
                break
            fresh = await self._fresh_maker_prices(
                market, lighter_side, arcus_side, closing=(action == "close"),
            )
            if fresh is not None:
                still_ok, still_why, live_l, live_a = fresh
                # 开仓：挂出去的价如果变成买得不便宜，就撤。已成交的腿仍然对冲。
                # 平仓不因为价差方向撤单，免得改去吃单。一边先成交时，收尾仍对冲那条腿。
                posted_ok, posted_why = True, ""
                if action != "close" and arcus.price and arcus.submitted:
                    posted_ok, _bps, posted_why = open_sides_allowed(
                        lighter_side, arcus_side, float(live_l), float(arcus.price), None,
                    )
                if action == "close" and not quotes.get("force_close") and (filled_l ^ filled_a):
                    # 一边已经成交：另一边必须还挂在能通过差额的价格上。过不了就撤，不吃单。
                    check_l = float(lighter.price) if lighter.price else float(live_l)
                    check_a = float(arcus.price) if arcus.price else float(live_a)
                    net_ok, net_why = _close_send_allowed(quotes, check_l, check_a)
                    if not net_ok:
                        await self._cancel_maker_resting(market, arcus, lighter)
                        after_l, after_a = await self.read_positions(lighter_symbol, arcus_id)
                        await self._settle_one_leg_as_maker(
                            market, action, lighter_side, arcus_side,
                            before_lighter, before_arcus, after_l, after_a,
                            float(live_l), float(live_a), lighter_decimals, quotes,
                        )
                        return PairResult(
                            False, "close_wait", lighter, arcus,
                            reason=net_why, quotes=quotes,
                            elapsed_ms=(time.monotonic() - started) * 1000,
                            notes=["一边已成交，另一边挂单价过不了浮盈亏差额，已撤未成交 maker，剩余改挂 maker，不吃单"],
                        )
                if (
                    action in ("open", "topup") and still_ok and posted_ok
                    and not filled_l and not filled_a
                    and quotes.get("_requotes", 0) < 3
                ):
                    moved = False
                    if lighter.submitted and lighter.price and abs(float(live_l) - float(lighter.price)) > max(1e-8, abs(float(lighter.price)) * 1e-6):
                        moved = True
                    if arcus.submitted and arcus.price and abs(float(live_a) - float(arcus.price)) > max(1e-8, abs(float(arcus.price)) * 1e-6):
                        moved = True
                    if moved:
                        quotes["_requotes"] = int(quotes.get("_requotes", 0)) + 1
                        await self._cancel_maker_resting(market, arcus, lighter)
                        after_l, after_a = await self.read_positions(lighter_symbol, arcus_id)
                        filled_l = lighter_quantity <= 0 or classify_fill(
                            before=before_lighter, after=after_l, requested_signed=lighter_signed,
                        ).kind == "full"
                        filled_a = arcus_quantity <= 0 or classify_fill(
                            before=before_arcus, after=after_a, requested_signed=arcus_signed,
                        ).kind == "full"
                        if filled_l or filled_a:
                            await self._settle_one_leg_as_maker(
                                market, action, lighter_side, arcus_side,
                                before_lighter, before_arcus, after_l, after_a,
                                float(live_l), float(live_a), lighter_decimals, quotes,
                            )
                            return PairResult(
                                False, "spread_wait", lighter, arcus,
                                reason="改价时已有一条腿成交，已撤另一边并挂 maker 退出，不追价吃单",
                                quotes=quotes, elapsed_ms=(time.monotonic() - started) * 1000,
                                notes=["未成交的撤掉重挂；不成交的不吃单"],
                            )
                        arcus = await self._arcus_place(
                            market, arcus_side, arcus_quantity, float(live_a), reduce_only, "ALO",
                        )
                        _stamp(arcus, side=arcus_side, requested=arcus_quantity, reference_price=float(live_a))
                        lighter = await self._lighter_post_only(
                            market["lighter_market_index"], lighter_side, lighter_quantity,
                            float(live_l), lighter_decimals, reduce_only,
                        )
                        _stamp(lighter, side=lighter_side, requested=lighter_quantity, reference_price=float(live_l))
                        lighter_price, arcus_price = float(live_l), float(live_a)
                        quotes["lighter_join"] = lighter_price
                        quotes["arcus_join"] = arcus_price
                        continue
                if (not still_ok or not posted_ok) and not (filled_l and filled_a):
                    await self._cancel_maker_resting(market, arcus, lighter)
                    after_l, after_a = await self.read_positions(lighter_symbol, arcus_id)
                    await self._settle_one_leg_as_maker(
                        market, action, lighter_side, arcus_side,
                        before_lighter, before_arcus, after_l, after_a,
                        lighter_price, arcus_price, lighter_decimals, quotes,
                    )
                    note = "挂单价格已不再通过检查，已撤未成交 maker；成交的部分改挂 maker，不吃单"
                    why = still_why if not still_ok else posted_why
                    return PairResult(
                        False, "close_wait" if action == "close" else "spread_wait",
                        lighter, arcus, reason=why or "价差不再合格，已撤单",
                        quotes=quotes, elapsed_ms=(time.monotonic() - started) * 1000,
                        notes=[note],
                    )
            if stop is not None and stop.is_set():
                await self._cancel_maker_resting(market, arcus, lighter)
                return PairResult(
                    False, "maker_stopped", lighter, arcus,
                    reason="挂单被叫停，已撤未成交的单", quotes=quotes,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
            after_l, after_a = await self.read_positions(lighter_symbol, arcus_id)
            filled_l = lighter_quantity <= 0 or classify_fill(
                before=before_lighter, after=after_l, requested_signed=lighter_signed,
            ).kind == "full"
            filled_a = arcus_quantity <= 0 or classify_fill(
                before=before_arcus, after=after_a, requested_signed=arcus_signed,
            ).kind == "full"
            if filled_l and filled_a:
                break
            if not await self._sleep_gap(1.0, stop):
                await self._cancel_maker_resting(market, arcus, lighter)
                return PairResult(
                    False, "maker_stopped", lighter, arcus,
                    reason="挂单被叫停，已撤未成交的单", quotes=quotes,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )

        if not (filled_l and filled_a):
            await self._cancel_maker_resting(market, arcus, lighter)
            after_l, after_a = await self.read_positions(lighter_symbol, arcus_id)
            filled_l = lighter_quantity <= 0 or classify_fill(
                before=before_lighter, after=after_l, requested_signed=lighter_signed,
            ).kind == "full"
            filled_a = arcus_quantity <= 0 or classify_fill(
                before=before_arcus, after=after_a, requested_signed=arcus_signed,
            ).kind == "full"
            await self._settle_one_leg_as_maker(
                market, action, lighter_side, arcus_side,
                before_lighter, before_arcus, after_l, after_a,
                lighter_price, arcus_price, lighter_decimals, quotes,
            )
            if action == "close" and not quotes.get("force_close"):
                if (filled_l or filled_a) and not (filled_l and filled_a):
                    return PairResult(
                        False, "close_wait", lighter, arcus,
                        reason="一边已成交，另一边未在通过差额的价格上成交，已撤未成交 maker，剩余改挂 maker，不吃单",
                        quotes=quotes, elapsed_ms=(time.monotonic() - started) * 1000,
                        notes=["到最长持有也只跟盘挂 maker，不因为计时改吃单"],
                        ledger=[("close", leg) for leg in (lighter, arcus) if leg.submitted],
                    )
            else:
                after_l, after_a = await self.read_positions(lighter_symbol, arcus_id)
                filled_l = lighter_quantity <= 0 or classify_fill(
                    before=before_lighter, after=after_l, requested_signed=lighter_signed,
                ).kind == "full"
                filled_a = arcus_quantity <= 0 or classify_fill(
                    before=before_arcus, after=after_a, requested_signed=arcus_signed,
                ).kind == "full"
        if filled_l and filled_a:
            if lighter.submitted:
                lighter.filled = lighter_quantity
                lighter.fill_confirmed = True
            if arcus.submitted:
                arcus.filled = arcus_quantity
                arcus.fill_confirmed = True
            quotes["filled_quantity"] = float(quantity if quantity is not None else lighter_quantity)
            ok_stage = "closed" if action == "close" else "opened"
            return PairResult(
                True, ok_stage, lighter, arcus, quotes=quotes,
                elapsed_ms=(time.monotonic() - started) * 1000,
                notes=["两腿都已成交（开仓同时挂单，不插入持有间隔）"],
                ledger=[(action if action != "topup" else "open", leg)
                        for leg in (lighter, arcus) if leg.submitted],
            )
        return PairResult(
            False, "legs_timeout", lighter, arcus,
            reason=f"挂单 {self.settings.maker_wait_seconds:g} 秒内两腿没有都成交，已撤销未成交的挂单",
            quotes=quotes, elapsed_ms=(time.monotonic() - started) * 1000,
            notes=["未双成交，不保留半套对冲单"],
            ledger=[(action if action != "topup" else "open", leg)
                    for leg in (lighter, arcus) if leg.submitted],
        )


    async def _fresh_maker_prices(
        self, market: dict[str, Any], lighter_side: str, arcus_side: str,
        *, closing: bool = False,
    ) -> tuple[bool, str, float, float] | None:
        """重读两边盘口，算出马上要挂的价。读失败返回 None（调用方不下新单）。

        平仓只要算得出挂单价就过。开仓仍要求买价严格低于卖价，不看 bp 宽度。
        """
        try:
            bid, ask = await self.market.arcus_bbo(market["arcus_symbol"])
            book = await self.market.lighter_book(
                market["lighter_market_index"], market["lighter_symbol"], limit=20,
            )
        except Exception:  # noqa: BLE001
            return None
        if not bid or not ask:
            return False, "读不到 Arcus 盘口，不下单", 0.0, 0.0
        raw = ax.passive_price(market, arcus_side, bid, ask)
        lighter_px = join_price(book, lighter_side)
        if raw is None or lighter_px is None:
            return False, "挂单价算不出来（盘口交叉或缺档），不下单", 0.0, 0.0
        if closing:
            return True, "平仓不看价差，挂 maker", float(lighter_px), float(raw)
        ok, _bps, why = open_sides_allowed(
            lighter_side, arcus_side, float(lighter_px), float(raw), None,
        )
        return ok, why, float(lighter_px), float(raw)

    async def _touch(self, market: dict[str, Any], venue: str, side: str) -> tuple[float | None, float | None, float | None]:
        """返回 (bid, ask, join)。读失败时三个都是 None，调用方不得改吃单。"""
        try:
            if venue == "arcus":
                bid, ask = await self.market.arcus_bbo(market["arcus_symbol"])
            else:
                book = await self.market.lighter_book(
                    market["lighter_market_index"], market["lighter_symbol"], limit=20,
                )
                bid, ask = book.best_bid, book.best_ask
        except Exception:  # noqa: BLE001
            return None, None, None
        try:
            bid_f = float(bid) if bid else None
            ask_f = float(ask) if ask else None
        except (TypeError, ValueError):
            return None, None, None
        join = ask_f if side == "sell" else bid_f
        if join is None or join <= 0:
            return bid_f, ask_f, None
        if venue == "arcus":
            raw = ax.passive_price(market, side, bid_f or 0, ask_f or 0)
            if raw is None:
                return bid_f, ask_f, None
            join = float(raw)
        return bid_f, ask_f, join

    async def _post_reduce_maker(
        self, market: dict[str, Any], venue: str, quantity: float, side: str,
        entry: float, lighter_decimals: tuple[int, int],
    ) -> LegResult:
        """单腿只挂 maker 减仓。挂单价亏过面板差额就往回挂，绝不 IOC。"""
        window = float(self.settings.pnl_close_usd)
        bid, ask, join = await self._touch(market, venue, side)
        if join is None:
            # 没有盘口也不能改吃单。用开仓价挂在不亏过差额的一侧，等下一轮重挂。
            join = float(entry) if entry and entry > 0 else 0.0
            bid, ask = None, None
        px = maker_exit_price(side, entry, quantity, join or entry, bid, ask, window)
        if px is None or px <= 0:
            return LegResult(venue, False, submitted=False, error="maker 退出价算不出来，不吃单")
        if self.dry_run:
            raw = {
                "dry_run": True, "side": side, "quantity": quantity, "price": px,
                "reduce_only": True, "post_only": True, "timeInForce": "ALO",
            }
            leg = LegResult(venue, True, raw=raw)
            _stamp(leg, side=side, requested=quantity, reference_price=px)
            _simulate_fill(leg)
            return leg
        if venue == "lighter":
            leg = await self._lighter_post_only(
                market["lighter_market_index"], side, quantity, px,
                lighter_decimals, reduce_only=True,
            )
        else:
            leg = await self._arcus_place(
                market, side, quantity, px, True, "ALO",
            )
        _stamp(leg, side=side, requested=quantity, reference_price=px)
        if leg.price is None:
            leg.price = px
        return leg

    async def _settle_one_leg_as_maker(
        self, market: dict[str, Any], action: str, lighter_side: str, arcus_side: str,
        before_lighter: float, before_arcus: float, after_lighter: float, after_arcus: float,
        lighter_price: float, arcus_price: float, lighter_decimals: tuple[int, int],
        quotes: dict[str, Any] | None = None,
    ) -> None:
        """只有一条腿变了仓：不吃单。

        开仓：把新成交的那条腿挂 maker 减掉。
        平仓：已经平掉的那条留着，还敞着的那条继续挂 maker 减，不把成交的那条买回来。
        """
        quotes = quotes or {}
        changed_l = (
            math.isfinite(before_lighter) and math.isfinite(after_lighter)
            and abs(after_lighter - before_lighter) > 1e-12
        )
        changed_a = (
            math.isfinite(before_arcus) and math.isfinite(after_arcus)
            and abs(after_arcus - before_arcus) > 1e-12
        )
        if changed_l == changed_a:
            return
        if action == "close":
            # 没平掉的那条腿还敞着。按原平仓方向再挂 maker，不反向。
            if not changed_l and abs(after_lighter) > 1e-12:
                entry = lighter_price
                entries = quotes.get("close_entries") if isinstance(quotes.get("close_entries"), dict) else {}
                if entries.get("lighter_entry"):
                    entry = float(entries["lighter_entry"])
                await self._post_reduce_maker(
                    market, "lighter", abs(after_lighter), lighter_side, entry, lighter_decimals,
                )
            if not changed_a and abs(after_arcus) > 1e-12:
                entry = arcus_price
                entries = quotes.get("close_entries") if isinstance(quotes.get("close_entries"), dict) else {}
                if entries.get("arcus_entry"):
                    entry = float(entries["arcus_entry"])
                await self._post_reduce_maker(
                    market, "arcus", abs(after_arcus), arcus_side, entry, lighter_decimals,
                )
            return
        if changed_l:
            delta = after_lighter - before_lighter
            side = "sell" if delta > 0 else "buy"
            await self._post_reduce_maker(
                market, "lighter", abs(delta), side, float(lighter_price), lighter_decimals,
            )
        if changed_a:
            delta = after_arcus - before_arcus
            side = "sell" if delta > 0 else "buy"
            await self._post_reduce_maker(
                market, "arcus", abs(delta), side, float(arcus_price), lighter_decimals,
            )

    async def _flatten_naked_maker_leg(
        self, market: dict[str, Any], lighter_side: str, arcus_side: str,
        before_lighter: float, before_arcus: float, after_lighter: float, after_arcus: float,
        lighter_price: float, arcus_price: float, lighter_decimals: tuple[int, int],
    ) -> None:
        """兼容旧调用：开仓单腿只挂 maker 退出，不再吃单。"""
        await self._settle_one_leg_as_maker(
            market, "open", lighter_side, arcus_side,
            before_lighter, before_arcus, after_lighter, after_arcus,
            lighter_price, arcus_price, lighter_decimals, None,
        )

    async def _cancel_maker_resting(
        self, market: dict[str, Any], arcus: LegResult | None, lighter: LegResult | None,
    ) -> None:
        if arcus is not None and isinstance(arcus.raw, dict) and arcus.raw.get("orderId"):
            await self._arcus_cancel(market, str(arcus.raw["orderId"]))
        if lighter is not None and isinstance(lighter.raw, dict):
            index = lighter.raw.get("client_order_index")
            if index is not None:
                await self._lighter_cancel(int(market["lighter_market_index"]), int(index))

    async def flatten_orphan(
        self, *, market: dict[str, Any], venue: str, size: float,
        price: float, slippage_bps: float, lighter_decimals: tuple[int, int],
        entry_price: float | None = None,
    ) -> LegResult:
        """孤腿：挂 maker 退出，不市价、不 IOC。

        风控里的双腿强平仍走 close_pair 吃单。这里只处理单腿。
        跟盘口会亏过面板差额时往回挂，不穿过对手价。
        """
        side = "sell" if size > 0 else "buy"
        entry = float(entry_price) if entry_price else float(price)
        return await self._post_reduce_maker(
            market, venue, abs(size), side, entry, lighter_decimals,
        )
