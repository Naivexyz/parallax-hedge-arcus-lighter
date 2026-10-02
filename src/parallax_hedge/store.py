"""任务、运行记录与成交账本的持久化（sqlite）。

只存必要的东西：任务配置、当前这轮的开仓时刻与走廊、每次动作的记录，
以及每一条真正发出去的腿（成交账本）。行情和费率不落库 —— 那些是
瞬时数据，重启后重新拉。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    asset               TEXT PRIMARY KEY,
    enabled             INTEGER NOT NULL DEFAULT 0,
    leverage            REAL    NOT NULL,
    rotation_hours      REAL    NOT NULL,
    notional_usdc       REAL,
    opened_at           REAL,
    corridor_at_open    REAL,
    last_closed_at      REAL,
    open_direction      TEXT,
    open_quantity       REAL,
    created_at          REAL    NOT NULL,
    updated_at          REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS cycles (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    asset     TEXT NOT NULL,
    plan      TEXT NOT NULL,
    reason    TEXT,
    urgent    INTEGER NOT NULL DEFAULT 0,
    dry_run   INTEGER NOT NULL DEFAULT 1,
    result    TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cycles_asset ON cycles(asset, id DESC);
CREATE TABLE IF NOT EXISTS fills (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     REAL    NOT NULL,
    asset          TEXT    NOT NULL,
    venue          TEXT    NOT NULL,     -- lighter | arcus
    action         TEXT    NOT NULL,     -- open | close | rescue | orphan | backout
    side           TEXT,                 -- buy | sell
    dry_run        INTEGER NOT NULL,
    market_id      INTEGER,              -- Lighter 市场号，对账时按市场查成交
    order_ref      TEXT,                 -- Lighter tx_hash / Arcus orderId
    requested_qty  REAL    NOT NULL DEFAULT 0,
    quantity       REAL    NOT NULL DEFAULT 0,   -- 目前最可信的成交量
    price          REAL,                         -- 目前最可信的成交均价
    fee            REAL,                         -- USDC；NULL = 还不知道
    realized_pnl   REAL,                         -- 交易所给的已实现盈亏（不含手续费）
    source         TEXT    NOT NULL,     -- exchange | receipt | position | simulated
    status         TEXT    NOT NULL,     -- pending | final | estimated
    attempts       INTEGER NOT NULL DEFAULT 0,
    cycle_id       INTEGER,
    note           TEXT,
    settled_at     REAL,
    confirmed_qty  REAL                          -- 下单时由仓位变化 / 回执证实的成交量
);
CREATE INDEX IF NOT EXISTS idx_fills_status ON fills(status, created_at);
CREATE INDEX IF NOT EXISTS idx_fills_asset ON fills(asset, dry_run);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_ref ON fills(venue, order_ref)
    WHERE order_ref IS NOT NULL;
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 老库升级：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，必须显式 ALTER。
# 只加列、不删不改 —— 线上库里有真实的持仓状态，不能动。
_TASK_COLUMNS = (
    ("rotation_hours_max", "REAL"),   # 随机轮换区间的上限；NULL = 固定周期
    ("hold_hours", "REAL"),           # 这一轮开仓时抽到的持有时长
    ("target_quantity", "REAL"),      # v1.3：这一轮的目标数量（挂单只开出一部分时，持仓期间继续挂单补到它）
)
_FILL_COLUMNS = (
    ("confirmed_qty", "REAL"),        # v0.2.1：对账时用来判断交易所记录是不是对全了
)

# 成交统计里算「平仓类」的动作 —— 已实现盈亏只出现在这些成交上
CLOSING_ACTIONS = frozenset({"close", "rescue", "orphan", "backout"})


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        for table, columns in (("tasks", _TASK_COLUMNS), ("fills", _FILL_COLUMNS)):
            existing = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def close(self) -> None:
        self.conn.close()

    # ── 任务 ────────────────────────────────────────
    def upsert_task(self, asset: str, **fields: Any) -> dict[str, Any]:
        now = time.time()
        existing = self.get_task(asset)
        if existing is None:
            row = {
                "asset": asset, "enabled": 0, "leverage": 1.0, "rotation_hours": 4.0,
                "rotation_hours_max": None, "hold_hours": None, "target_quantity": None,
                "notional_usdc": None, "opened_at": None, "corridor_at_open": None,
                "last_closed_at": None, "open_direction": None, "open_quantity": None,
                "created_at": now, "updated_at": now,
            }
            row.update(fields)
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            self.conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({marks})", list(row.values()))
        else:
            fields["updated_at"] = now
            sets = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(
                f"UPDATE tasks SET {sets} WHERE asset=?", [*fields.values(), asset]
            )
        self.conn.commit()
        return self.get_task(asset)  # type: ignore[return-value]

    def get_task(self, asset: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE asset=?", (asset,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM tasks ORDER BY asset")]

    def delete_task(self, asset: str) -> None:
        self.conn.execute("DELETE FROM tasks WHERE asset=?", (asset,))
        self.conn.commit()

    # 开平仓的状态转移，单独给出来，避免调用方漏字段
    def mark_opened(self, asset: str, *, direction: str, quantity: float,
                    corridor_pct: float | None, at: float | None = None,
                    hold_hours: float | None = None,
                    target_quantity: float | None = None) -> None:
        self.upsert_task(
            asset, opened_at=at or time.time(), open_direction=direction,
            open_quantity=quantity, corridor_at_open=corridor_pct,
            hold_hours=hold_hours, target_quantity=target_quantity,
        )

    def mark_closed(self, asset: str, at: float | None = None) -> None:
        self.upsert_task(
            asset, opened_at=None, open_direction=None, open_quantity=None,
            corridor_at_open=None, hold_hours=None, target_quantity=None,
            last_closed_at=at or time.time(),
        )

    # ── 运行记录 ────────────────────────────────────
    def log_cycle(self, asset: str, plan: str, reason: str | None, *,
                  urgent: bool = False, dry_run: bool = True,
                  result: dict[str, Any] | None = None) -> int:
        cursor = self.conn.execute(
            "INSERT INTO cycles (asset, plan, reason, urgent, dry_run, result, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (asset, plan, reason, int(urgent), int(dry_run),
             json.dumps(result, ensure_ascii=False) if result else None, time.time()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def recent_cycles(self, limit: int = 60) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            if item.get("result"):
                try:
                    item["result"] = json.loads(item["result"])
                except json.JSONDecodeError:
                    pass
            out.append(item)
        return out

    def live_cycles(self) -> list[dict[str, Any]]:
        """全部实盘记录（按时间正序），给账本回填用。"""
        rows = self.conn.execute(
            "SELECT * FROM cycles WHERE dry_run=0 ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 杂项 ────────────────────────────────────────
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ── 成交账本 ────────────────────────────────────
    def record_fill(
        self, *, asset: str, venue: str, action: str, side: str | None,
        dry_run: bool, market_id: int | None, order_ref: str | None,
        requested_qty: float, quantity: float, price: float | None,
        source: str, status: str, fee: float | None = None,
        realized_pnl: float | None = None, cycle_id: int | None = None,
        note: str | None = None, created_at: float | None = None,
        confirmed_qty: float | None = None,
    ) -> int | None:
        """记一条腿。同一个订单号只记一次（回填和实时记录重叠时靠这个去重）。"""
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO fills (created_at, asset, venue, action, side, dry_run,"
            " market_id, order_ref, requested_qty, quantity, price, fee, realized_pnl,"
            " source, status, cycle_id, note, confirmed_qty)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (created_at or time.time(), asset, venue, action, side, int(dry_run),
             market_id, order_ref, float(requested_qty or 0), float(quantity or 0),
             price, fee, realized_pnl, source, status, cycle_id, note, confirmed_qty),
        )
        self.conn.commit()
        return int(cursor.lastrowid) if cursor.rowcount else None

    def pending_fills(self, *, older_than: float, limit: int = 300) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM fills WHERE status='pending' AND created_at<=?"
            " ORDER BY created_at LIMIT ?",
            (older_than, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def settle_fill(
        self, fill_id: int, *, quantity: float, price: float | None,
        fee: float | None, realized_pnl: float | None, side: str | None = None,
    ) -> None:
        """交易所成交记录对上了：以交易所为准，覆盖掉所有估计值。"""
        self.conn.execute(
            "UPDATE fills SET quantity=?, price=?, fee=?, realized_pnl=?,"
            " side=COALESCE(?, side), source='exchange', status='final',"
            " settled_at=?, attempts=attempts+1 WHERE id=?",
            (float(quantity), price, fee, realized_pnl, side, time.time(), fill_id),
        )
        self.conn.commit()

    def give_up_fill(self, fill_id: int, note: str, *, price: float | None = None) -> None:
        """一直对不上：保留估计值定稿，并如实标注。

        price：交易所只对上了一部分成交时，那部分的成交均价仍然比盘口参考价准，
        可以拿来替换；数量、手续费、盈亏则不能用残缺的数据。
        """
        self.conn.execute(
            "UPDATE fills SET status='estimated', note=?, settled_at=?,"
            " price=COALESCE(?, price), attempts=attempts+1 WHERE id=?",
            (note, time.time(), price, fill_id),
        )
        self.conn.commit()

    def backfill_lighter_zero_fee(self) -> int:
        """Lighter 账户确认是 0 费率后，把「数量按仓位实测、手续费不计」的 Lighter 行手续费补成 0。

        手续费一栏只要有一行是「未知」，净损益和每万美元磨损就整列显示不出来。
        0 费率账户上手续费就是 0，没有理由让它一直「未知」。
        开仓 / 补仓成交本来就没有已实现盈亏，一并补 0；平仓类的盈亏仍然如实留空。
        """
        cur = self.conn.execute(
            "UPDATE fills SET fee=0,"
            " realized_pnl=CASE WHEN realized_pnl IS NULL AND action IN ('open','topup')"
            " THEN 0 ELSE realized_pnl END"
            " WHERE venue='lighter' AND dry_run=0 AND status='estimated'"
            " AND fee IS NULL AND quantity > 0"
        )
        self.conn.commit()
        return cur.rowcount

    def touch_fill(self, fill_id: int) -> None:
        self.conn.execute("UPDATE fills SET attempts=attempts+1 WHERE id=?", (fill_id,))
        self.conn.commit()

    def fill_stats(self, *, dry_run: bool) -> dict[str, Any]:
        """首页用的汇总：每个所、每个币种、合计的交易量 / 手续费 / 已实现盈亏。"""
        rows = [dict(r) for r in self.conn.execute(
            "SELECT asset, venue, action, quantity, price, fee, realized_pnl, status,"
            " created_at, cycle_id, id FROM fills WHERE dry_run=?",
            (int(dry_run),),
        )]
        return summarize_fills(rows)


def _empty_bucket() -> dict[str, Any]:
    return {
        "volume": 0.0, "fees": 0.0, "fee_unknown": 0, "realized_pnl": 0.0,
        "pnl_unknown": 0, "fills": 0, "pending": 0, "estimated": 0,
    }


def _add(bucket: dict[str, Any], row: dict[str, Any]) -> None:
    quantity = float(row.get("quantity") or 0.0)
    price = row.get("price")
    if row.get("status") == "pending":
        bucket["pending"] += 1
    elif row.get("status") == "estimated":
        bucket["estimated"] += 1
    if quantity <= 0:
        return
    bucket["fills"] += 1
    if price:
        bucket["volume"] += quantity * float(price)
    if row.get("fee") is None:
        bucket["fee_unknown"] += 1
    else:
        bucket["fees"] += float(row["fee"])
    if row.get("realized_pnl") is not None:
        bucket["realized_pnl"] += float(row["realized_pnl"])
    elif row.get("action") in CLOSING_ACTIONS:
        # 开仓成交的已实现盈亏天然是 0；只有平仓类成交缺了才是真的「不知道」
        bucket["pnl_unknown"] += 1


def _finish(bucket: dict[str, Any]) -> dict[str, Any]:
    """净损益与磨损率只在【两边的平仓盈亏都拿到了】时才给出。

    只拿到一边就去算，会得出荒谬的结论 —— 对冲的两条腿一赚一亏，
    只看 Arcus 那条腿，一夜下来可能显示「赚了 4.40」，而真实结果是亏的。
    """
    known = bucket["pnl_unknown"] == 0 and bucket["fee_unknown"] == 0
    net = bucket["realized_pnl"] - bucket["fees"] if known else None
    bucket["net_pnl"] = net
    bucket["wear_per_10k"] = (
        -net / bucket["volume"] * 10_000
        if net is not None and bucket["volume"] > 0 else None
    )
    return bucket


def summarize_fills(rows: list[dict[str, Any]]) -> dict[str, Any]:
    venues = {"lighter": _empty_bucket(), "arcus": _empty_bucket()}
    total = _empty_bucket()
    by_asset: dict[str, dict[str, Any]] = {}
    open_cycles: dict[str, set] = {}
    for row in rows:
        venue = row.get("venue")
        asset = row.get("asset") or "?"
        entry = by_asset.setdefault(asset, {
            "lighter": _empty_bucket(), "arcus": _empty_bucket(),
            "total": _empty_bucket(), "opens": 0,
        })
        if venue in venues:
            _add(venues[venue], row)
            _add(entry[venue], row)
        _add(total, row)
        _add(entry["total"], row)
        # 开仓次数按 Arcus 的开仓成交数：Arcus 腿只有在 Lighter 成交之后才会下，
        # 它成交了这一对才算真正开出来。按 Lighter 数会把「Lighter 成交、
        # Arcus 失败、被抢救平掉」的失败尝试也算成一次开仓。
        if (row.get("action") == "open" and venue == "arcus"
                and float(row.get("quantity") or 0) > 0):
            # 按「哪一轮」去重：挂单模式一次开仓可能是好几张 Arcus 挂单拼出来的
            key = row.get("cycle_id")
            open_cycles.setdefault(asset, set()).add(
                key if key is not None else f"row{row.get('id', id(row))}")
    for bucket in (*venues.values(), total):
        _finish(bucket)
    for asset, cycles in open_cycles.items():
        by_asset[asset]["opens"] = len(cycles)
    for entry in by_asset.values():
        for key in ("lighter", "arcus", "total"):
            _finish(entry[key])
    stamps = [float(r["created_at"]) for r in rows if r.get("created_at")]
    return {
        "since": min(stamps) if stamps else None,
        "venues": venues,
        "total": total,
        "by_asset": by_asset,
    }
