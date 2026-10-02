"""资金费率的归一化与方向选择。

这是整个程序最容易出错、也最致命的一块：
两个所的费率如果周期不同，直接相减会差出整数倍，而方向选错的代价
（按 SNDK 实测：本该 +5 bps 变成 -15 bps）比不开仓大得多。

所以这里的规矩是：
  1. 每个适配器必须自报 interval_sec，比较一律在【年化】维度上做；
     不硬编码「两个所都是每小时」—— 今天碰巧是，改规则那天就错了。
  2. 单期费率超过 SANE_RATE_PER_INTERVAL 一律判为单位解析错误，
     标 suspect 并拒绝开仓，而不是偷偷缩放。宁可不开，不能按错的方向开。
  3. 开仓看的是「下一期会收/付多少」，有预测值就优先用预测值。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Literal

SECONDS_PER_YEAR = 365 * 24 * 3600
SECONDS_PER_HOUR = 3600

# 单期费率超过 5% 几乎必然是单位解析错了，不是真行情。
# （Lighter 协议硬顶 4%/8h，Hyperliquid 每小时也远达不到 5%。）
SANE_RATE_PER_INTERVAL = 0.05

Venue = Literal["arcus", "lighter"]

# 方向命名以 Lighter 那条腿为准，和 Parallax 的 *_sell 习惯保持一致
Direction = Literal["long_lighter_short_arcus", "short_lighter_long_arcus"]

DIRECTION_LABELS: dict[str, str] = {
    "long_lighter_short_arcus": "Lighter 多 / Arcus 空",
    "short_lighter_long_arcus": "Lighter 空 / Arcus 多",
}


@dataclass(frozen=True)
class FundingRow:
    """某个所、某个币种的一条费率记录。"""

    venue: Venue
    symbol: str
    rate: float | None          # 当期费率（每 interval_sec 一期，小数）
    interval_sec: int
    next_rate: float | None = None   # 下期预测，拿得到就用它
    raw_symbol: str = ""
    fetched_at: float = 0.0

    @property
    def effective_rate(self) -> float | None:
        return self.next_rate if self.next_rate is not None else self.rate

    @property
    def is_predicted(self) -> bool:
        return self.next_rate is not None

    @property
    def suspect(self) -> bool:
        r = self.effective_rate
        return r is not None and abs(r) > SANE_RATE_PER_INTERVAL

    @property
    def suspect_reason(self) -> str | None:
        if not self.suspect:
            return None
        r = self.effective_rate or 0.0
        return (
            f"单期费率 {r * 100:.3f}% 超出合理区间，疑似单位解析有误，已拒绝参与开仓"
        )

    @property
    def apr(self) -> float | None:
        return annualize(self.effective_rate, self.interval_sec)

    @property
    def bps_per_hour(self) -> float | None:
        """归一化到 bps/小时 —— 面板和成本模型统一用这个单位。"""
        r = self.effective_rate
        if r is None or self.interval_sec <= 0:
            return None
        return r * (SECONDS_PER_HOUR / self.interval_sec) * 10_000

    @property
    def periods_per_day(self) -> float:
        return 86400 / self.interval_sec if self.interval_sec > 0 else 0.0


def annualize(rate_per_interval: float | None, interval_sec: int) -> float | None:
    if rate_per_interval is None or interval_sec <= 0:
        return None
    return rate_per_interval * (SECONDS_PER_YEAR / interval_sec)


@dataclass(frozen=True)
class DirectionChoice:
    """一个币种上，两个方向里更优的那个。"""

    symbol: str
    direction: Direction
    net_bps_per_hour: float      # 该方向每小时净收（正=收，负=付）
    lighter_bps_per_hour: float
    arcus_bps_per_hour: float
    tradable: bool
    reason: str | None = None

    @property
    def direction_label(self) -> str:
        return DIRECTION_LABELS[self.direction]

    def net_bps_over(self, hours: float) -> float:
        return self.net_bps_per_hour * hours


def choose_direction(
    lighter: FundingRow | None,
    arcus: FundingRow | None,
) -> DirectionChoice | None:
    """在两个方向里挑净资金费更高的那个。

    永续的约定：费率为正时多头付给空头。所以
        方向 A（Lighter 多 / Arcus 空）：付 lighter，收 arcus
        方向 B（Lighter 空 / Arcus 多）：收 lighter，付 arcus
    两者恰好互为相反数，因此【差值总能在某一个方向上收到】——
    净资金费本身不会两个方向都为负。会为负的是「净资金费 − 价差成本」，
    那个判断放在成本模型里做，不在这里。
    """
    if lighter is None or arcus is None:
        return None
    lb = lighter.bps_per_hour
    eb = arcus.bps_per_hour
    if lb is None or eb is None:
        return None

    net_a = eb - lb           # 收 arcus、付 lighter
    if net_a >= 0:
        direction: Direction = "long_lighter_short_arcus"
        net = net_a
    else:
        direction = "short_lighter_long_arcus"
        net = -net_a

    blockers = [r.suspect_reason for r in (lighter, arcus) if r.suspect]
    return DirectionChoice(
        symbol=lighter.symbol,
        direction=direction,
        net_bps_per_hour=net,
        lighter_bps_per_hour=lb,
        arcus_bps_per_hour=eb,
        tradable=not blockers,
        reason="；".join(b for b in blockers if b) or None,
    )


# ── 周期自检：地板价年化 ────────────────────────────────
#
# 2026-09-18 的教训：把 Lighter 的 8 小时费率当成每小时，差了 8 倍，
# ANTH 的开仓方向被判反。SANE_RATE_PER_INTERVAL 那道闸门没拦住 ——
# 它只防数量级错误，8 倍误差仍落在"合理区间"内。
#
# 这里加一道专门针对【周期假设】的检查，依据是一个经验事实：
# 一个所的大多数市场在溢价接近零时会卡在同一个基准利率（地板价）上。
# 地板价换算出的年化必然是温和的（几个百分点）。如果算出几十个百分点，
# 那不是行情，是周期用错了。

# 地板价至少要占这么多市场，才认为它确实是"地板"而不是巧合
FLOOR_SHARE_THRESHOLD = 0.30
# 地板价隐含年化超过这个数就报警（真实基准利率通常在 10% 以内）
FLOOR_APR_WARN_PCT = 15.0


def detect_period_anomaly(rows: list[FundingRow]) -> str | None:
    """用地板价的隐含年化反查周期假设对不对。返回告警文案，没问题则 None。"""
    usable = [r for r in rows if r.effective_rate is not None]
    if len(usable) < 10:
        return None
    counts: dict[float, int] = {}
    for r in usable:
        counts[r.effective_rate] = counts.get(r.effective_rate, 0) + 1
    floor_rate, hits = max(counts.items(), key=lambda kv: kv[1])
    if hits / len(usable) < FLOOR_SHARE_THRESHOLD:
        return None
    if floor_rate == 0:
        return None
    sample = next(r for r in usable if r.effective_rate == floor_rate)
    apr_pct = abs((annualize(floor_rate, sample.interval_sec) or 0.0) * 100)
    if apr_pct <= FLOOR_APR_WARN_PCT:
        return None
    hours = sample.interval_sec / 3600
    return (
        f"{sample.venue}：{hits}/{len(usable)} 个市场卡在同一个费率 {floor_rate:g} 上"
        f"（疑似基准利率），按 {hours:g} 小时周期换算隐含年化 {apr_pct:.1f}%，"
        f"高得不合常理 —— 多半是结算周期假设错了，请核对该所的费率周期"
    )


# ── 上一份好数据 ────────────────────────────────────────
#
# 走住宅代理时某个所偶尔一轮拉不到是常态。一失败就当这个所不存在，
# 面板会忽有忽无，根本没法用。所以失败时沿用上一份快照并如实标注年龄，
# 超过 stale_max_seconds 才真正丢弃 —— 陈旧的费率比没有费率更危险。
class LastGoodCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[list[FundingRow], float]] = {}

    def put(self, venue: str, rows: list[FundingRow]) -> None:
        if rows:
            self._store[venue] = (rows, time.time())

    def get(self, venue: str, stale_max_seconds: float) -> tuple[list[FundingRow], float] | None:
        entry = self._store.get(venue)
        if not entry:
            return None
        rows, at = entry
        age = time.time() - at
        # 约定：stale_max_seconds <= 0 表示【完全不沿用】，不是「不设上限」。
        # 写反的话代理一抖动就会无限期使用陈旧费率，而陈旧费率比没有费率更危险。
        if stale_max_seconds <= 0 or age > stale_max_seconds:
            return None
        return rows, age

    def clear(self) -> None:
        self._store.clear()
