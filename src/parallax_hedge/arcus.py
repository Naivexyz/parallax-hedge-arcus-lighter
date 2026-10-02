"""Arcus 专属的纯逻辑：签名、精度对齐、强平价、回执解析。不碰网络。

所有规则来自 docs.arcus.xyz，签名格式与实盘验证过的
JS 版签名实现逐字节一致（见 build_place_payload）。

Arcus 有几处容易静默出错的地方：

  1. 资金费每小时结算，fundingRate / nextFundingRate 本身就是【每小时】费率。
  2. /v1/positions【不返回强平价】。强平价要自己按维持保证金率算
     （arcus_liquidation_price / cross_distance），算法写在函数注释里。
  3. 下单是 Ed25519 签名一段【按键排序的紧凑 JSON】（ordersign 载荷），
     时间戳是纳秒整数；设杠杆是另一种「旧式」签名：ts + "setLeverage" + canonicalJSON(body)。
  4. 价格档位按价格区间分段（tickTiers），不是全市场统一一个 tick。
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")

# 下单时间戳：纳秒，进程内严格递增（同一纳秒下两单会被当成重放）
_ts_lock = threading.Lock()
_last_ts_ns = 0


def timestamp_ns() -> int:
    global _last_ts_ns
    with _ts_lock:
        now = time.time_ns()
        _last_ts_ns = now if now > _last_ts_ns else _last_ts_ns + 1
        return _last_ts_ns


def good_til_us(days: float = 40) -> int:
    """GTT 到期时间（微秒）。IOC 也必须带这个字段，照 arcus-signing.js 给 40 天。"""
    days = max(32.0, min(180.0, float(days)))
    return int((time.time() + days * 86400) * 1_000_000)


# ── 十进制工具 ─────────────────────────────────────────────

def D(value: Any) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"无效数字：{value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"无效数字：{value!r}")
    return result


def fmt_decimal(value: Decimal) -> str:
    """去掉多余的 0 和科学计数法 —— 交易所只认普通十进制字符串。"""
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def step_decimals(step: str | float) -> int | None:
    """步长是 10 的整数次幂时返回对应的小数位数；否则返回 None。

    两所共用「数量小数位」这一个精度口径（下单量 = 两边都能接受的数），
    这要求 Arcus 的 stepSize 是 1、0.1、0.01 … 这种形状。
    2026-09 实测 58 个市场全部满足；万一哪天出现 0.5 这种步长，
    common_markets 会把它排除，而不是按错的精度下单。
    """
    d = D(step)
    if d <= 0:
        return None
    exponent = d.normalize().as_tuple()
    if exponent.digits != (1,):
        return None
    return max(0, -int(exponent.exponent))


def choose_tick(market: dict[str, Any], price: float) -> Decimal:
    """按价格选档位。tickTiers 按 upToPrice 从小到大排，最后一档不带 upToPrice。"""
    tiers = market.get("arcus_tick_tiers") or []
    px = D(price)
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        up_to = tier.get("upToPrice")
        tick = tier.get("tick")
        if tick in (None, ""):
            continue
        if up_to in (None, "") or px <= D(up_to):
            return D(tick)
    return D(market.get("arcus_tick_size") or "0.01")


def round_arcus_price(market: dict[str, Any], price: float, side: str) -> Decimal:
    """把限价对齐到 Arcus 的价格档位，【方向朝着更激进的一侧】。

    买单往上、卖单往下。原因：
    四舍五入最多偏半格，当半格比滑点预算还宽时（便宜币），会把买单限价
    压到卖一之下 —— 报单成功、零成交。按方向取整最多多付一格，
    而一格是交易所自己的最小刻度，本来就绕不开。
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"Arcus 限价对齐需要买卖方向，收到 {side!r}")
    px = D(price)
    if px <= 0:
        raise ValueError("Arcus 限价必须大于 0")
    tick = choose_tick(market, float(px))
    rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
    aligned = (px / tick).to_integral_value(rounding=rounding) * tick
    if aligned <= 0:
        raise ValueError("Arcus 限价对齐后无效")
    return aligned


def align_arcus_quantity(market: dict[str, Any], quantity: float) -> Decimal:
    """数量向下对齐到 stepSize（向上会超出保证金或反手）。"""
    step = D(market.get("arcus_step_size") or "1")
    q = D(quantity)
    if q <= 0:
        return Decimal(0)
    return (q / step).to_integral_value(rounding=ROUND_FLOOR) * step


def to_units(value: Decimal, unit: str | Decimal) -> int:
    """价格 / 数量换成签名要的整数（ticks / quantums）。必须整除，不整除说明对齐错了。"""
    u = D(unit)
    units = value / u
    if units != units.to_integral_value():
        raise ValueError(f"{value} 不是 {u} 的整数倍")
    return int(units)


# ── 签名 ──────────────────────────────────────────────────

_TIF_CODE = {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}


def build_place_payload(
    *, address: str, account_index: int, client_id: str | None, timestamp: int,
    good_til_us_value: int, market_id: int, price_ticks: int, quantity_quantums: int,
    reduce_only: bool, side: str, time_in_force: str,
) -> str:
    """ordersign 载荷。键顺序、有无空格都必须与交易所逐字节一致。

    与 arcus-signing.js 的 buildPlacePayload 一一对应：
      g 是【纳秒】（HTTP body 里的 goodTilTime 是微秒字符串，这里要 ×1000）；
      c（clientId）可选，给了就要小写。
    """
    c = f',"c":{json.dumps(str(client_id).lower())}' if client_id else ""
    return (
        f'{{"ad":{json.dumps(str(address).lower())},"ai":{int(account_index)}{c},'
        f'"ct":{int(timestamp)},"g":{int(good_til_us_value) * 1000},"m":{int(market_id)},'
        f'"op":1,"p":{int(price_ticks)},"q":{int(quantity_quantums)},'
        f'"r":{1 if reduce_only else 0},"s":{1 if str(side).upper() == "SELL" else 0},'
        f'"t":{_TIF_CODE[str(time_in_force).upper()]},"v":1}}'
    )


def build_cancel_payload(
    *, address: str, account_index: int, timestamp: int, market_id: int,
    order_id: str | None = None, client_id: str | None = None,
) -> str:
    """撤单的 ordersign 载荷（op=2），和 arcus-signing.js 的 buildCancelPayload 逐字节一致。
    orderId 和 clientId 必须且只能给一个。"""
    if bool(order_id) == bool(client_id):
        raise ValueError("Arcus 撤单必须且只能指定 orderId 或 clientId")
    c = f',"c":{json.dumps(str(client_id).lower())}' if client_id else ""
    i = f',"id":{json.dumps(str(order_id))}' if order_id else ""
    return (
        f'{{"ad":{json.dumps(str(address).lower())},"ai":{int(account_index)}{c},'
        f'"ct":{int(timestamp)}{i},"m":{int(market_id)},"op":2,"v":1}}'
    )


def passive_price(market: dict[str, Any], side: str, bid: float, ask: float) -> Decimal | None:
    """挂单价：排到自己这一侧的最前面，但绝不越过对手价（越过就变成吃单，会被 ALO 拒掉）。

    卖：盘口有空档就挂在卖一下面一格，否则跟卖一；买：镜像。
    返回 None 表示盘口不正常（交叉或缺一边），这轮不挂。
    """
    if not bid or not ask or bid <= 0 or ask <= 0 or ask <= bid:
        return None
    b, a = D(bid), D(ask)
    tick = choose_tick(market, float((a + b) / 2))
    if side == "sell":
        price = a - tick if a - b > tick else a
        price = (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        return price if price > b else None
    price = b + tick if a - b > tick else b
    price = (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    return price if price < a else None


def canonical_json(value: Any) -> str:
    """旧式签名（setLeverage 等）用的规范 JSON：键排序、无空白。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def legacy_message(timestamp: int, action: str, body: dict[str, Any]) -> str:
    return f"{int(timestamp)}{action}{canonical_json(body)}"


_PKCS8_ED25519_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def load_private_key(value: str = "", file: str = ""):
    """Ed25519 私钥：64 位 seed hex、PKCS#8 PEM、PKCS#8 DER(hex/base64) 都认。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import base64

    raw = (value or "").strip()
    if file:
        path = Path(file)
        if not path.exists():
            raise ValueError(f"找不到 Arcus 私钥文件：{file}")
        raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError("缺少 Arcus Ed25519 API 私钥（ARCUS_API_PRIVATE_KEY）")
    raw = raw.replace("\\n", "\n")
    try:
        if "PRIVATE KEY-----" in raw:
            key = serialization.load_pem_private_key(raw.encode(), password=None)
        else:
            clean = re.sub(r"\s+", "", raw)
            clean = clean[2:] if clean.lower().startswith("0x") else clean
            if re.fullmatch(r"[0-9a-fA-F]{64}", clean):
                return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(clean))
            if re.fullmatch(r"[0-9a-fA-F]{128}", clean):
                # 有些工具导出的是 64 字节「seed + 公钥」：前 32 字节是私钥，后 32 字节必须是它的公钥
                key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(clean[:64]))
                if public_key_hex(key) != clean[64:].lower():
                    raise ValueError("128 位私钥的后半段和前半段推导出的公钥对不上")
                return key
            if re.fullmatch(r"[0-9a-fA-F]+", clean) and len(clean) % 2 == 0:
                der = bytes.fromhex(clean)
            else:
                der = base64.b64decode(clean)
            if len(der) == 48 and der.startswith(_PKCS8_ED25519_PREFIX):
                return Ed25519PrivateKey.from_private_bytes(der[16:])
            key = serialization.load_der_private_key(der, password=None)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            "Arcus Ed25519 API 私钥格式无效：支持 64 / 128 位十六进制、PKCS#8 PEM/DER(hex/base64) 或私钥文件"
        ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Arcus API 私钥不是 Ed25519 密钥")
    return key


def public_key_hex(private_key) -> str:
    from cryptography.hazmat.primitives import serialization
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()


def sign_hex(private_key, message: str) -> str:
    return private_key.sign(message.encode("utf-8")).hex()


def new_client_id() -> str:
    """clientId：小写字母数字，36 位以内。带时间前缀，排查时能看出先后。"""
    return f"ph{int(time.time() * 1000):x}{secrets.token_hex(4)}"[:36]


# ── 强平价 ────────────────────────────────────────────────

def cross_distance(
    *, equity: float | None, notional: float, mmf: float | None,
    other_notional: float = 0.0, other_maintenance: float = 0.0,
) -> float | None:
    """全仓：价格往不利方向走多少（小数）会被强平。整个 Arcus 账户一起算。

    按【最坏情况】算：账户里其它 Arcus 仓位也跟着一起亏（加密币大多同涨同跌，
    而它们在 Arcus 这一侧并没有互相对冲）。价格不利变动 x 时：
        权益      E − (N + O)·x
        维持保证金 (N·mmf + Om)·(1 + x)       （空头方向更严，取它）
    两者相等时强平：
        x = (E − N·mmf − Om) / (N + O + N·mmf + Om)
    N = 本仓位名义额，O = 其它仓位名义额合计，Om = 其它仓位维持保证金合计。
    只有这一条仓位时就是熟悉的 (E − N·mmf) / (N·(1 + mmf))。
    """
    if equity is None or notional <= 0 or not mmf or not 0 < mmf < 1:
        return None
    own_mm = notional * mmf
    x = (equity - own_mm - other_maintenance) / (
        notional + other_notional + own_mm + other_maintenance)
    return x if x > 0 else 0.0


def arcus_liquidation_price(
    *, size: float, entry_price: float | None, mark_price: float | None,
    margin_mode: str | None, margin_used: float | None, mmf: float | None,
    account_equity: float | None = None, other_maintenance: float = 0.0,
    other_notional: float = 0.0,
) -> float | None:
    """Arcus 不给强平价，按官方规则自己算。算不出来就返回 None —— 不要猜。

    规则（docs.arcus.xyz 保证金 / 强平两页）：权益低于维持保证金要求就强平，
    维持保证金 = |仓位| × 标记价 × MMF，用标记价算。

    全仓（本程序开仓一律设全仓）：整个账户一起算，见 cross_distance（最坏情况：
    其它 Arcus 仓位同向亏损）。

    逐仓（接管别人手动开的逐仓仓位时才会遇到）：
      这条仓位自己的权益 = 划进来的保证金 marginUsed + 仓位浮盈亏
        E(P) = M + s·(P − entry)，令 E(P) = |s|·P·mmf：
        多（s>0）：P = (s·entry − M) / (s·(1 − mmf))
        空（s<0）：P = (M + |s|·entry) / (|s|·(1 + mmf))

    positions.liquidation_side_is_sane 会拦住方向算反的结果。
    """
    if not size or not mmf or mmf <= 0 or mmf >= 1:
        return None
    s = float(size)
    q = abs(s)
    mode = str(margin_mode or "").upper()
    if mode == "ISOLATED":
        if not entry_price or entry_price <= 0 or margin_used is None or margin_used <= 0:
            return None
        if s > 0:
            price = (s * entry_price - margin_used) / (s * (1 - mmf))
        else:
            price = (margin_used + q * entry_price) / (q * (1 + mmf))
        return price if price > 0 else None
    if not mark_price or mark_price <= 0:
        return None
    x = cross_distance(equity=account_equity, notional=q * mark_price, mmf=mmf,
                       other_notional=other_notional, other_maintenance=other_maintenance)
    if x is None:
        return None
    price = mark_price * (1 - x) if s > 0 else mark_price * (1 + x)
    # 多头保证金充足到价格归零都不爆时 —— 没有强平价
    return price if price > 0 else None


def account_exposure(payload: Any, mmf_by_market: dict[int, float], *,
                     exclude_market: int | None = None,
                     default_mmf: float = 0.1) -> tuple[float | None, float, float]:
    """(权益, 其它仓位名义额合计, 其它仓位维持保证金合计)。开仓前估算走廊用。

    不在共有币种里的仓位不知道 MMF，按 10% 保守算。
    """
    if not isinstance(payload, dict):
        return None, 0.0, 0.0
    equity = _num(payload.get("equity"))
    notional = maintenance = 0.0
    for row in payload.get("_positions") or []:
        try:
            market_id = int(row.get("marketId"))
        except (TypeError, ValueError):
            continue
        if exclude_market is not None and market_id == exclude_market:
            continue
        size = abs(_num(row.get("size")) or 0.0)
        mark = _num(row.get("markPx")) or _num(row.get("averageEntryPrice")) or 0.0
        if size <= 0 or mark <= 0:
            continue
        notional += size * mark
        maintenance += size * mark * mmf_by_market.get(market_id, default_mmf)
    return equity, notional, maintenance


# ── 下单回执 ──────────────────────────────────────────────

# 这些状态出现在 200 回执里时，说明单子已经有了定论
_FILLED_STATES = {"FILLED", "PARTIALLY_FILLED"}


@dataclass(frozen=True)
class ArcusReceipt:
    accepted: bool             # 交易所收下了这张单（202 或 200 且非拒单）
    order_id: str | None
    client_id: str | None
    status: str
    filled: float | None       # 回执里写明的成交量；202 时为 None（要看仓位）
    error: str | None


def parse_order_response(http_status: int, data: Any, client_id: str | None) -> ArcusReceipt:
    """把 placeOrder 的回执解析成统一结构。

    202 = 网关已校验并转给撮合引擎，body 只有 orderId/clientId —— 成交与否【看仓位】。
    200 = 网关已经知道结果：FILLED / CANCELED / REJECTED …
    IOC 没吃到对手价时交易所回的是 REJECTED + rejectionReason=IOC_CANCELED，
    这不是故障，只是「这个价没成交」。
    """
    body = data if isinstance(data, dict) else {}
    order_id = str(body.get("orderId") or "") or None
    cid = str(body.get("clientId") or client_id or "") or None
    status = str(body.get("status") or ("ACK" if http_status == 202 else "")).upper()
    reason = body.get("rejectionReason") or body.get("error")
    filled = _num(body.get("filledSize"))
    if http_status == 202:
        return ArcusReceipt(True, order_id, cid, status or "ACK", None, None)
    if http_status != 200:
        text = reason or body or f"HTTP {http_status}"
        return ArcusReceipt(False, order_id, cid, status or "ERROR", 0.0, f"Arcus 拒单：{text}")
    if status in ("REJECTED", "ERROR", "MARGIN_CANCELED"):
        if str(reason or "").upper() == "IOC_CANCELED":
            return ArcusReceipt(False, order_id, cid, status, 0.0, "Arcus IOC 未成交（没有吃到对手价）")
        return ArcusReceipt(False, order_id, cid, status, 0.0, f"Arcus 拒单：{reason or status}")
    if status == "CANCELED" and not (filled and filled > 0):
        return ArcusReceipt(False, order_id, cid, status, 0.0, "Arcus IOC 未成交（已撤销）")
    return ArcusReceipt(True, order_id, cid, status, filled, None)


def order_ref(order_id: str | None, client_id: str | None) -> str | None:
    """账本里的订单号：优先 orderId；没有就用 clientId（加前缀区分）。"""
    if order_id:
        return str(order_id)
    if client_id:
        return f"cid:{client_id}"
    return None


def match_fills(rows: list[dict[str, Any]] | None, ref: str | None):
    """按订单号（或 cid:clientId）把 /v1/fills 里属于这张单的成交加总。

    返回 (数量, 均价, 手续费, 已实现盈亏, 方向, 条数)；对不上返回 None。
    fee 为正 = 付出。返回的盈亏【不含手续费】（closedPnl 本身含，这里加回去）。
    """
    if not rows or not ref:
        return None
    by_client = ref.startswith("cid:")
    target = ref[4:] if by_client else ref
    quantity = notional = fees = pnl = 0.0
    matched = 0
    side: str | None = None
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("clientId") if by_client else row.get("orderId") or "")
        if key != target:
            continue
        trade = str(row.get("tradeId") or "")
        if trade:
            if trade in seen:
                continue
            seen.add(trade)
        price = abs(_num(row.get("price")) or 0.0)
        size = abs(_num(row.get("size")) or 0.0)
        if price <= 0 or size <= 0:
            continue
        matched += 1
        quantity += size
        notional += price * size
        fee = _num(row.get("fee")) or 0.0
        fees += fee
        # Arcus 的 closedPnl【已经扣过这笔成交的手续费】（2026-09-26 实盘核对：
        # 开仓成交 closedPnl = −fee；ETH 平仓 closedPnl −1.8858 = 价差 −1.4355 − 手续费 0.4503）。
        # 账本里的「价差损益」约定不含手续费（手续费单独一列，净损益 = 价差 − 手续费），
        # 所以这里把手续费加回去，否则手续费会被扣两遍。
        pnl += (_num(row.get("closedPnl")) or 0.0) + fee
        side = {"BUY": "buy", "SELL": "sell"}.get(str(row.get("side") or "").upper(), side)
    if quantity <= 0:
        return None
    return quantity, notional / quantity, fees, pnl, side, matched


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None
