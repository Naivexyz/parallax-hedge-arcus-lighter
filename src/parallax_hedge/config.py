"""配置：从 .env 读，全部可留空（没配私钥时只读面板照样能用）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values

LIGHTER_ROBINHOOD_URL = "https://api.rh.lighter.xyz"
ARCUS_URL = "https://api.arcus.xyz"
DEFAULT_PORT = 8004


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _num(value: str | None, fallback: float) -> float:
    try:
        return float(_clean(value))
    except (TypeError, ValueError):
        return fallback


def normalize_proxy(value: str | None) -> str:
    """把几种常见写法统一成 httpx 认识的代理 URL。

    规则：
      · 带协议的原样使用：http:// https:// socks5://
      · host:port:user:pass → socks5://user:pass@host:port
      · host:port           → http://host:port
    """
    s = _clean(value)
    if not s:
        return ""
    if re.match(r"^\w+://", s):
        return s
    parts = s.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"socks5://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{s}"
    return s


@dataclass
class Settings:
    env_path: Path
    data_dir: Path

    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT

    # ── Arcus ──
    arcus_api_url: str = ARCUS_URL
    arcus_address: str = ""                  # 主钱包公开地址（0x + 40 位）
    arcus_account_index: int = 0             # 子账户 0~9
    arcus_api_key: str = ""                  # Ed25519 公钥 hex；留空则由私钥推导
    arcus_api_private_key: str = ""          # Ed25519 私钥（seed hex / PKCS#8）
    arcus_api_private_key_file: str = ""     # 或者私钥文件路径

    # ── RHC Lighter ──
    lighter_base_url: str = LIGHTER_ROBINHOOD_URL
    lighter_account_index: int | None = None
    lighter_api_key_index: int | None = None
    lighter_api_private_key: str = ""

    # 独立 IP：每个所一个代理，留空则回落到 global_proxy，再空则直连。
    arcus_proxy: str = ""
    lighter_proxy: str = ""
    global_proxy: str = ""

    funding_refresh_seconds: float = 30.0
    funding_stale_max_seconds: float = 180.0
    funding_timeout_seconds: float = 30.0
    # 两条腿里更危险的那条，距强平价还剩多少百分比就算「危险」。
    # 到这个线的处置动作是【双腿同时平掉】，绝不单腿止损。
    liquidation_warn_distance_pct: float = 5.0
    # 开仓门槛：估算的强平走廊（离强平还有百分之几）低于它就不开
    min_corridor_pct: float = 3.0
    # 平仓线：持仓期间任一条腿离强平小于它，两边立刻一起平；面板的「危险」也是这条线。
    # （另外还有相对触发：剩余距离掉到开仓时的一半也会一起平。）
    close_corridor_pct: float = 1.5
    # 平仓后隔多久才允许重开 —— 防止刚平完就重开，连付两次往返成本
    reopen_cooldown_seconds: float = 60.0
    # 下单的滑点上限
    order_slippage_bps: float = 10.0
    # 挂单模式：Arcus 那条腿挂 ALO（0 手续费），成交后去 Lighter 吃单对冲
    arcus_maker: bool = False
    # 挂单最多等多久（秒）。开仓等不到就撤单下轮再挂；平仓等不到剩余部分改吃单
    maker_wait_seconds: float = 120.0
    # 只约束开仓和补仓：买价必须严格低于卖价，且绝对价差不超过这么多 bp。默认 1。
    # 计划内平仓不看这个数。某一个所可以一直更贵，不要等价差回到 0。
    max_spread_bps: float = 1.0
    # 两腿都成交之后，至少持有这么多秒，才允许平仓（包括价差有利的平仓）。
    min_hold_sec: float = 3.0
    # 从两腿都成交起超过这么多秒仍持仓，不再等价差，强制平仓。
    max_hold_sec: float = 300.0
    # 演练模式：只记录意图，不提交任何订单
    dry_run: bool = True
    # 引擎多久跑一个周期
    engine_cycle_seconds: float = 20.0

    _raw: dict[str, str] = field(default_factory=dict, repr=False)

    # ── 代理解析 ────────────────────────────────────────
    def _specific_proxy(self, exchange: str) -> str:
        return self.arcus_proxy if exchange == "arcus" else self.lighter_proxy

    def proxy_for(self, exchange: str) -> str | None:
        """返回该所要用的代理 URL；None = 直连。

        空字符串和纯空白都当作「没配」，不要传给 httpx —— httpx 收到 ""
        会当成一个非法代理地址直接抛错，而不是忽略它。
        """
        chosen = normalize_proxy(self._specific_proxy(exchange)) or normalize_proxy(
            self.global_proxy
        )
        return chosen or None

    def proxy_label(self, exchange: str) -> str:
        """面板上显示用：脱敏后的代理来源说明。"""
        specific = normalize_proxy(self._specific_proxy(exchange))
        if specific:
            return f"独立代理 {_mask_proxy(specific)}"
        shared = normalize_proxy(self.global_proxy)
        if shared:
            return f"共用代理 {_mask_proxy(shared)}"
        return "直连"

    @property
    def arcus_configured(self) -> bool:
        """读账户只需要公开地址。"""
        return bool(_ADDRESS_RE.match(self.arcus_address or ""))

    @classmethod
    def load(cls, env_path: Path, data_dir: Path) -> "Settings":
        raw = {k: v for k, v in dotenv_values(env_path).items() if v is not None}

        def s(key: str, fallback: str = "") -> str:
            return _clean(raw.get(key)) or fallback

        def i(key: str) -> int | None:
            try:
                return int(_clean(raw.get(key)))
            except (TypeError, ValueError):
                return None

        private_file = _resolve_private_key_file(env_path, s("ARCUS_API_PRIVATE_KEY_FILE"))

        return cls(
            env_path=env_path,
            data_dir=data_dir,
            host=s("HOST", "127.0.0.1"),
            port=int(_num(raw.get("PORT"), DEFAULT_PORT)),
            arcus_api_url=s("ARCUS_API_URL", ARCUS_URL).rstrip("/"),
            arcus_address=s("ARCUS_ADDRESS").lower(),
            arcus_account_index=i("ARCUS_ACCOUNT_INDEX") or 0,
            arcus_api_key=s("ARCUS_API_KEY").lower().removeprefix("0x"),
            arcus_api_private_key=s("ARCUS_API_PRIVATE_KEY"),
            arcus_api_private_key_file=private_file,
            lighter_base_url=s("LIGHTER_BASE_URL", LIGHTER_ROBINHOOD_URL).rstrip("/"),
            lighter_account_index=i("LIGHTER_ACCOUNT_INDEX"),
            lighter_api_key_index=i("LIGHTER_API_KEY_INDEX"),
            lighter_api_private_key=s("LIGHTER_API_PRIVATE_KEY"),
            arcus_proxy=s("ARCUS_PROXY"),
            lighter_proxy=s("LIGHTER_PROXY"),
            global_proxy=s("GLOBAL_PROXY"),
            funding_refresh_seconds=_num(raw.get("FUNDING_REFRESH_SECONDS"), 30.0),
            funding_stale_max_seconds=_num(raw.get("FUNDING_STALE_MAX_SECONDS"), 180.0),
            funding_timeout_seconds=_num(raw.get("FUNDING_TIMEOUT_SECONDS"), 30.0),
            liquidation_warn_distance_pct=_num(
                raw.get("LIQUIDATION_WARN_DISTANCE_PCT"), 5.0
            ),
            min_corridor_pct=_num(raw.get("MIN_CORRIDOR_PCT"), 3.0),
            close_corridor_pct=_num(raw.get("CLOSE_CORRIDOR_PCT"), 1.5),
            reopen_cooldown_seconds=_num(raw.get("REOPEN_COOLDOWN_SECONDS"), 60.0),
            order_slippage_bps=_num(raw.get("ORDER_SLIPPAGE_BPS"), 10.0),
            dry_run=_clean(raw.get("DRY_RUN", "true")).lower() not in ("0", "false", "no"),
            arcus_maker=_clean(raw.get("ARCUS_MAKER", "false")).lower() in ("1", "true", "yes"),
            maker_wait_seconds=_num(raw.get("MAKER_WAIT_SECONDS"), 120.0),
            max_spread_bps=_num(raw.get("MAX_SPREAD_BPS"), 1.0),
            min_hold_sec=_num(raw.get("MIN_HOLD_SEC"), 3.0),
            max_hold_sec=_num(raw.get("MAX_HOLD_SEC"), 300.0),
            engine_cycle_seconds=_num(raw.get("ENGINE_CYCLE_SECONDS"), 20.0),
            _raw=raw,
        )


_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")


def sync_env_file(env_path: Path, example_path: Path) -> list[str]:
    """把 .env.example 里有、.env 里没有的键补进 .env。

    为什么需要这个：.env 只在首次运行时从 .env.example 复制一次，
    之后每新增一个设置项，.env 就少一项 —— 而且完全看不出来，
    只会表现为「这个开关怎么不起作用」。2026-09-18 就是这样：
    DRY_RUN 等六个键全都不在 .env 里。

    只【追加】，绝不改写已有行 —— .env 里有私钥，不能碰。
    """
    if not env_path.exists() or not example_path.exists():
        return []
    existing = {
        line.split("=", 1)[0].strip()
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }
    added: list[tuple[str, str]] = []
    for line in example_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in existing:
            added.append((key, value.strip()))
    if not added:
        return []
    with env_path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n# ── 以下为程序自动补齐的新增设置（值为默认值）──\n")
        for key, value in added:
            handle.write(f"{key}={value}\n")
    return [key for key, _ in added]


def _mask_proxy(url: str) -> str:
    """把 user:pass@host:port 里的账密和大部分 IP 抹掉再显示。"""
    tail = url.split("@")[-1]
    host = tail.split("://")[-1]
    parts = host.split(":")
    addr = parts[0]
    octets = addr.split(".")
    if len(octets) == 4:
        addr = f"{octets[0]}.*.*.{octets[3]}"
    return addr

def format_env_number(value: float) -> str:
    """写成 .env 里的普通小数，不带多余的 0。"""
    text = format(float(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"



def _format_env_value(value: str) -> str:
    """含空格、# 或引号时加引号，避免 python-dotenv 把后半段当成注释。"""
    if any(ch.isspace() or ch in '#"\'\\' for ch in value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + escaped + '"'
    return value


def _resolve_private_key_file(env_path: Path, value: str | None) -> str:
    """相对路径按 .env 所在目录解析。双击启动时工作目录不一定是程序目录。"""
    private_file = _clean(value)
    if private_file and not Path(private_file).is_absolute():
        private_file = str((env_path.parent / private_file).resolve())
    return private_file


SECRET_DISPLAY = "****"
_IDENTIFIER_HEAD = 6
_IDENTIFIER_TAIL = 4


def mask_identifier(value: str, *, head: int = _IDENTIFIER_HEAD, tail: int = _IDENTIFIER_TAIL) -> str:
    """长标识只留头尾，例如 0xe24d…7777。空串保持空。"""
    text = _clean(value)
    if not text:
        return ""
    if len(text) <= head + tail:
        if len(text) <= 8:
            return text
        head = 2
        tail = 2
    return f"{text[:head]}…{text[-tail:]}"


@dataclass(frozen=True)
class AccountField:
    """面板「账户设置」要覆盖的字段。只含 Arcus / Lighter 已经在读的凭证和身份。"""

    env_key: str
    attr: str
    group: str
    label: str
    kind: str  # secret | identifier | index | text
    hint: str = ""
    # 短数字清空表示取消（Lighter 账户索引可以不填）。密钥和地址留空则保持原值。
    clear_on_blank: bool = False


ACCOUNT_FIELDS: tuple[AccountField, ...] = (
    AccountField(
        "ARCUS_API_URL", "arcus_api_url", "arcus", "API 地址", "text",
        "留空不修改",
    ),
    AccountField(
        "ARCUS_ADDRESS", "arcus_address", "arcus", "钱包地址", "identifier",
        "0x 开头的 40 位公开地址，不是私钥。留空不修改",
    ),
    AccountField(
        "ARCUS_ACCOUNT_INDEX", "arcus_account_index", "arcus", "子账户", "index",
        "0~9。保存后仍显示完整数字",
    ),
    AccountField(
        "ARCUS_API_KEY", "arcus_api_key", "arcus", "API 公钥", "secret",
        "Ed25519 公钥，64 位十六进制。可留空，由私钥推导。保存后只显示 ****",
    ),
    AccountField(
        "ARCUS_API_PRIVATE_KEY", "arcus_api_private_key", "arcus", "API 私钥", "secret",
        "留空表示不修改已保存的私钥。保存后只显示 ****",
    ),
    AccountField(
        "ARCUS_API_PRIVATE_KEY_FILE", "arcus_api_private_key_file", "arcus", "私钥文件", "identifier",
        "私钥文件路径。留空不修改",
    ),
    AccountField(
        "LIGHTER_BASE_URL", "lighter_base_url", "lighter", "API 地址", "text",
        "留空不修改",
    ),
    AccountField(
        "LIGHTER_ACCOUNT_INDEX", "lighter_account_index", "lighter", "账户索引", "index",
        "保存后仍显示完整数字。清空输入框会取消",
        clear_on_blank=True,
    ),
    AccountField(
        "LIGHTER_API_KEY_INDEX", "lighter_api_key_index", "lighter", "API key 编号", "index",
        "保存后仍显示完整数字。清空输入框会取消",
        clear_on_blank=True,
    ),
    AccountField(
        "LIGHTER_API_PRIVATE_KEY", "lighter_api_private_key", "lighter", "API 私钥", "secret",
        "留空表示不修改已保存的私钥。保存后只显示 ****",
    ),
    AccountField(
        "ARCUS_PROXY", "arcus_proxy", "proxy", "Arcus 代理", "secret",
        "可含账密。留空不修改，保存后只显示 ****",
    ),
    AccountField(
        "LIGHTER_PROXY", "lighter_proxy", "proxy", "Lighter 代理", "secret",
        "可含账密。留空不修改，保存后只显示 ****",
    ),
    AccountField(
        "GLOBAL_PROXY", "global_proxy", "proxy", "兜底代理", "secret",
        "两个所都没单独配时代理走这里。留空不修改，保存后只显示 ****",
    ),
)

_ACCOUNT_BY_KEY = {item.env_key: item for item in ACCOUNT_FIELDS}
_HEX_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def _looks_redacted(value: str) -> bool:
    text = value.strip()
    return text == SECRET_DISPLAY or "…" in text or "..." in text


def account_settings_view(settings: "Settings") -> dict:
    """给面板和 GET 用。密钥和完整地址都不出现在返回值里。"""
    groups = (
        ("arcus", "Arcus"),
        ("lighter", "Lighter"),
        ("proxy", "代理"),
    )
    return {
        "groups": [{"id": gid, "label": label} for gid, label in groups],
        "fields": [_account_field_view(settings, item) for item in ACCOUNT_FIELDS],
        "restart_required": False,
        "masking": {
            "secret": SECRET_DISPLAY,
            "identifier": "头尾保留，中间省略",
            "index": "原样显示",
            "blank_secret": "留空保持已保存的值",
        },
    }


def _account_field_view(settings: "Settings", item: AccountField) -> dict:
    raw = getattr(settings, item.attr)
    if item.kind == "index":
        if raw is None or raw == "":
            display, value, isset = "", "", False
        else:
            display = str(int(raw))
            value, isset = display, True
    elif item.kind == "text":
        display = _clean(None if raw is None else str(raw))
        value, isset = display, bool(display)
    elif item.kind == "secret":
        isset = bool(_clean(None if raw is None else str(raw)))
        display, value = (SECRET_DISPLAY if isset else ""), ""
    elif item.kind == "identifier":
        text = _clean(None if raw is None else str(raw))
        isset = bool(text)
        display, value = (mask_identifier(text) if isset else ""), ""
    else:
        raise ValueError(f"未知字段类型 {item.kind}")
    return {
        "key": item.env_key,
        "group": item.group,
        "label": item.label,
        "kind": item.kind,
        "hint": item.hint,
        "set": isset,
        "display": display,
        "value": value,
        "clear_on_blank": item.clear_on_blank,
    }


def account_settings_updates(settings: "Settings", payload: dict) -> dict[str, str]:
    """把面板提交收成要写入 .env 的键。

    密钥、地址、代理、URL 留空 = 不改已有值，这样改一个字段不用重填其它的。
    提交 **** 或带省略号的打码串也不会覆盖原值。
    未知键忽略，避免从这个接口改到 DRY_RUN 之类的非账户项。
    """
    if not isinstance(payload, dict):
        raise ValueError("请求必须是 JSON 对象")
    updates: dict[str, str] = {}
    for key, incoming in payload.items():
        item = _ACCOUNT_BY_KEY.get(str(key))
        if item is None:
            continue
        if incoming is None:
            text = ""
        elif isinstance(incoming, bool) or not isinstance(incoming, (str, int, float)):
            raise ValueError(f"{item.label} 不是有效文本")
        else:
            text = str(incoming).strip()
        if "\n" in text or "\r" in text or "\x00" in text:
            raise ValueError(f"{item.label} 不能包含换行")
        if len(text) > 8000:
            raise ValueError(f"{item.label} 过长")
        if item.kind in ("secret", "identifier", "text") and (text == "" or text == SECRET_DISPLAY):
            continue
        if item.kind == "identifier" and _looks_redacted(text):
            raise ValueError(f"{item.label} 请填写完整值，不要提交打码后的内容")
        if item.kind == "secret" and _looks_redacted(text):
            continue
        updates[item.env_key] = _normalize_account_value(item, text)
    return updates


def _normalize_account_value(item: AccountField, text: str) -> str:
    if item.kind == "text":
        if not re.match(r"^https?://", text, flags=re.IGNORECASE):
            raise ValueError(f"{item.label} 必须以 http:// 或 https:// 开头")
        return text.rstrip("/")
    if item.env_key == "ARCUS_ADDRESS":
        lowered = text.lower()
        if not _ADDRESS_RE.match(lowered):
            raise ValueError("钱包地址必须是 0x 开头的 40 位十六进制")
        return lowered
    if item.env_key == "ARCUS_API_KEY":
        hexkey = text.lower().removeprefix("0x")
        if not _HEX_KEY_RE.match(hexkey):
            raise ValueError("API 公钥必须是 64 位十六进制")
        return hexkey
    if item.kind == "index":
        if text == "":
            if item.clear_on_blank:
                return ""
            raise ValueError(f"{item.label} 不能为空")
        try:
            number = int(text)
        except ValueError:
            raise ValueError(f"{item.label} 必须是整数") from None
        if item.env_key == "ARCUS_ACCOUNT_INDEX" and not 0 <= number <= 9:
            raise ValueError("子账户必须是 0~9")
        if number < 0:
            raise ValueError(f"{item.label} 不能是负数")
        return str(number)
    return text


def apply_account_settings(settings: "Settings", updates: dict[str, str]) -> None:
    """写进正在运行的 Settings。行情和下一笔签名读的就是这个对象。"""
    for key, value in updates.items():
        item = _ACCOUNT_BY_KEY.get(key)
        if item is None:
            continue
        settings._raw[key] = value
        if item.kind == "text":
            cleaned = _clean(value).rstrip("/")
            if key == "ARCUS_API_URL":
                cleaned = cleaned or ARCUS_URL
            elif key == "LIGHTER_BASE_URL":
                cleaned = cleaned or LIGHTER_ROBINHOOD_URL
            setattr(settings, item.attr, cleaned)
        elif key == "ARCUS_ADDRESS":
            settings.arcus_address = _clean(value).lower()
        elif key == "ARCUS_API_PRIVATE_KEY_FILE":
            settings.arcus_api_private_key_file = _resolve_private_key_file(
                settings.env_path, value
            )
        elif key == "ARCUS_API_KEY":
            settings.arcus_api_key = _clean(value).lower().removeprefix("0x")
        elif item.kind == "secret":
            setattr(settings, item.attr, _clean(value))
        elif item.kind == "index":
            cleaned = _clean(value)
            if not cleaned:
                setattr(settings, item.attr, None if item.clear_on_blank else 0)
            else:
                setattr(settings, item.attr, int(cleaned))
        elif item.kind == "identifier":
            setattr(settings, item.attr, _clean(value))


def update_env_values(env_path: Path, updates: dict[str, str]) -> bool:
    """只改指定的键。文件不存在就不动；其它行（包括密钥）原样保留。

    返回 True 表示文件存在并已按 updates 处理（即使值和原来一样）。
    """
    if not env_path.is_file() or not updates:
        return False
    original = env_path.read_text(encoding="utf-8")
    lines = original.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={_format_env_value(updates[key])}")
                seen.add(key)
                continue
        out.append(line)
    missing = [key for key in updates if key not in seen]
    if missing:
        if out and out[-1] != "":
            out.append("")
        for key in missing:
            out.append(f"{key}={_format_env_value(updates[key])}")
    new = "\n".join(out)
    if original.endswith("\n") or original == "":
        new += "\n"
    if new != original:
        env_path.write_text(new, encoding="utf-8")
    return True
