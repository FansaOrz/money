"""现有数据层 → 研究数据仓库（DuckDB + Parquet）一次性/可重复迁移。

迁移对象（源一律只读，迁移不删除/不修改任何旧数据）：

1. ``fund_navs``（SQLite/ORM 业务库） → ``fund_nav`` 数据集；
2. ``daily/raw`` 每股 Parquet 数据湖 → ``stock_daily`` 数据集。
   **只迁 raw 层**：现 schema（``stock_daily`` 业务列）无法区分 raw/qfq 口径，
   qfq 前复权价随除权事件整体变化、与 raw 混写会污染同一 (symbol, effective_date)
   键空间，因此 qfq 目录明确跳过并记入报告 ``notes``；
3. ``index_constituents``（当前指数成分） → ``universe_membership`` 数据集。
   ``in_date`` 缺失时按 ``updated_at`` 的日期作为 effective_date，并在 notes 记录。

幂等性设计（关键）：
``repository.write`` 的版本保留语义依赖 ``row_hash`` + ``ingested_at``，而迁移
反复执行时每次 ``ingested_at`` 都不同，同一业务键会留下多份"伪修订版本"。
因此迁移不走 ``repository.write``，而是：

- 每个数据集使用**固定 source 标识 + 固定虚拟时间戳**
  （``available_at == ingested_at == MIGRATION_VIRTUAL_TS``），
  迁移版本永远排在任何实时写入（真实时间戳）之前，不遮蔽新数据；
- 每批先按 (业务键 + effective_date + source) **无条件删除**仓库中既有迁移行，
  再插入本批（目标由源单向决定，无需内容哈希）；
- 批次行以**确定性文件名**（``part-migrate-<source>-<序号>.parquet``）写分区，
  重复执行覆盖同名文件，不在磁盘累积多份；
- 主表为空但磁盘残留迁移文件时（如表被清空重迁），写前清理该 source 的旧文件。

支持分批（``batch_size`` / 按基金、按股票、按指数）与 ``dry_run``
（只扫描源、统计行数，不落任何数据）。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from app.research.warehouse import DATASET_BUSINESS_KEYS, validate_dataset

if TYPE_CHECKING:
    from app.research.warehouse import ResearchWarehouse

logger = logging.getLogger(__name__)

#: 迁移写入使用的固定虚拟时间戳：早于任何实时写入，as-of 查询中实时数据优先。
MIGRATION_VIRTUAL_TS = datetime(1970, 1, 1, 0, 0, 0)

#: 各数据集迁移来源标识（幂等删除/文件命名均按 source 隔离）
SOURCE_FUND_NAV = "migrate_fund_navs"
SOURCE_STOCK_DAILY_RAW = "migrate_daily_raw"
SOURCE_UNIVERSE = "migrate_index_constituents"

#: 迁移覆盖的数据集 -> （业务列, 排序键）
_DATASET_COLUMNS: dict[str, list[str]] = {
    "fund_nav": ["fund_code", "nav", "accumulated_nav", "daily_return"],
    "stock_daily": [
        "symbol", "open", "high", "low", "close",
        "volume", "amount", "turnover", "pct_change",
    ],
    "universe_membership": ["universe", "symbol", "weight"],
}

SNAPSHOT_COLUMNS = ["effective_date", "available_at", "ingested_at", "source", "row_hash"]

DEFAULT_BATCH_SIZE = 5000


@dataclass
class MigrationReport:
    """单数据集迁移结果。"""

    dataset: str
    source: str = ""
    dry_run: bool = False
    scanned: int = 0
    written: int = 0
    batches: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "source": self.source,
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "written": self.written,
            "batches": self.batches,
            "skipped": self.skipped,
            "ok": self.ok,
            "notes": list(self.notes),
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# 稳定快照列与幂等写入
# ---------------------------------------------------------------------------


def attach_stable_snapshot(df: pd.DataFrame, *, source: str) -> pd.DataFrame:
    """为迁移批次附加稳定快照列（固定时间戳 + 内容 SHA1）。

    ``row_hash`` 仅用于血缘/调试，幂等删除不依赖它（见 ``write_batch``）。
    """
    from app.research.snapshots import compute_row_hash

    out = df.copy()
    out["available_at"] = pd.Timestamp(MIGRATION_VIRTUAL_TS)
    out["ingested_at"] = pd.Timestamp(MIGRATION_VIRTUAL_TS)
    out["source"] = source
    out["row_hash"] = [
        compute_row_hash(list(row)) for row in out.itertuples(index=False)
    ]
    return out


def _migration_parquet_name(source: str, seq: int) -> str:
    """迁移分区文件名：按 source + 序号确定性命名，重跑覆盖而非累积。"""
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in source)
    return f"part-migrate-{safe}-{seq:04d}.parquet"


def _purge_migration_parquet(warehouse: ResearchWarehouse, dataset: str, source: str) -> int:
    """删除某数据集内某迁移 source 的全部历史分区文件，返回删除数。"""
    prefix = f"part-migrate-{source}-"
    removed = 0
    dataset_dir = warehouse.dataset_dir(dataset)
    if not dataset_dir.exists():
        return 0
    for path in dataset_dir.glob(f"year=*/{prefix}*.parquet"):
        path.unlink()
        removed += 1
    return removed


def write_batch(
    warehouse: ResearchWarehouse,
    dataset: str,
    frame: pd.DataFrame,
    *,
    source: str,
    file_seq: int,
) -> int:
    """幂等写入一个迁移批次：先删同 source 同键行，再插表 + 落分区文件。

    删除条件 = 业务键 + effective_date + source（年份条件利于分区裁剪），
    因此重复迁移/分批迁移任意交错执行都收敛到同一结果；实时写入的
    其他 source 行不受影响。
    """
    validate_dataset(dataset)
    if frame.empty:
        return 0
    business = _DATASET_COLUMNS[dataset]
    frame = attach_stable_snapshot(frame, source=source)
    frame = frame[[*business, *SNAPSHOT_COLUMNS]]

    conn = warehouse.conn
    keys = [*DATASET_BUSINESS_KEYS[dataset], "effective_date"]
    years = sorted({pd.Timestamp(d).year for d in frame["effective_date"]})
    year_in = ",".join(str(y) for y in years)
    join_cond = " AND ".join(f"t.{k} IS NOT DISTINCT FROM s.{k}" for k in keys)

    conn.register("_migrate_staging", frame)
    try:
        conn.execute(
            f"""
            DELETE FROM {dataset} t
            USING _migrate_staging s
            WHERE {join_cond} AND t.source = ? AND year(t.effective_date) IN ({year_in})
            """,
            [source],
        )
        conn.execute(f"INSERT INTO {dataset} SELECT * FROM _migrate_staging")
    finally:
        conn.unregister("_migrate_staging")

    year_counts = frame.groupby(frame["effective_date"].map(lambda d: d.year)).size()
    for year, count in year_counts.items():
        path = warehouse.partition_dir(dataset, int(year)) / _migration_parquet_name(source, file_seq)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame[frame["effective_date"].map(lambda d: d.year) == year].to_parquet(
            path, engine="pyarrow", compression="zstd", index=False
        )
        file_seq += 1
    return len(frame)


# ---------------------------------------------------------------------------
# 1) fund_navs (SQLite) -> fund_nav
# ---------------------------------------------------------------------------

_FUND_NAV_QUERY = """
SELECT i.code AS fund_code,
       f.nav_date AS effective_date,
       f.unit_nav AS nav,
       f.accumulated_nav AS accumulated_nav,
       f.daily_growth_rate AS daily_return
FROM fund_navs f
JOIN instruments i ON i.id = f.instrument_id
ORDER BY i.code, f.nav_date
"""


def _fund_nav_frame(rows: list[tuple]) -> pd.DataFrame:
    frame = pd.DataFrame(
        rows,
        columns=["fund_code", "effective_date", "nav", "accumulated_nav", "daily_return"],
    )
    frame["fund_code"] = frame["fund_code"].astype(str)
    frame["effective_date"] = frame["effective_date"].map(
        lambda v: v if isinstance(v, date) else date.fromisoformat(str(v)[:10])
    )
    for col in ("nav", "accumulated_nav", "daily_return"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def migrate_fund_navs(
    warehouse: ResearchWarehouse,
    sqlite_path: str | Path,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    codes: list[str] | None = None,
) -> MigrationReport:
    """SQLite ``fund_navs`` → ``fund_nav`` 数据集（按基金分批，幂等）。"""
    report = MigrationReport(dataset="fund_nav", source=SOURCE_FUND_NAV, dry_run=dry_run)
    sqlite_path = Path(sqlite_path)
    if not sqlite_path.exists():
        report.errors.append(f"SQLite 不存在: {sqlite_path}")
        return report
    if batch_size <= 0:
        batch_size = DEFAULT_BATCH_SIZE

    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        query = _FUND_NAV_QUERY
        if codes:
            placeholders = ",".join("?" * len(codes))
            query = query.replace(
                "ORDER BY", f"WHERE i.code IN ({placeholders}) ORDER BY"
            )
        total = conn.execute(
            f"SELECT count(*) FROM ({query.rstrip().rstrip(';')})",
            codes or [],
        ).fetchone()[0]
        report.scanned = int(total)
        if dry_run:
            report.notes.append("dry-run：仅统计，未写入")
            return report

        _purge_migration_parquet(warehouse, "fund_nav", SOURCE_FUND_NAV)
        file_seq = 0
        cursor = conn.execute(query, codes or [])
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            frame = _fund_nav_frame(rows)
            report.written += write_batch(
                warehouse, "fund_nav", frame, source=SOURCE_FUND_NAV, file_seq=file_seq
            )
            file_seq += max(1, frame["effective_date"].map(lambda d: d.year).nunique())
            report.batches += 1
        warehouse.refresh_views()
    except sqlite3.Error as exc:
        report.errors.append(f"读取 SQLite 失败: {exc}")
    finally:
        conn.close()
    return report


# ---------------------------------------------------------------------------
# 2) daily/raw per-code Parquet -> stock_daily
# ---------------------------------------------------------------------------


def _raw_daily_frame(path: Path) -> pd.DataFrame | None:
    """读取单只 raw 日线 Parquet 并映射到 stock_daily 业务列。"""
    try:
        frame = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 - 损坏文件跳过并记录
        logger.warning("跳过损坏文件 %s: %s", path, exc)
        return None
    if frame.empty or "trade_date" not in frame.columns:
        return None
    out = pd.DataFrame()
    out["symbol"] = frame["code"].astype(str) if "code" in frame.columns else path.stem
    out["effective_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    for src, dst in (
        ("open", "open"), ("high", "high"), ("low", "low"), ("close", "close"),
        ("volume", "volume"), ("amount", "amount"), ("turnover", "turnover"),
    ):
        out[dst] = pd.to_numeric(frame[src], errors="coerce") if src in frame.columns else None
    if "pct_change" in frame.columns:
        out["pct_change"] = pd.to_numeric(frame["pct_change"], errors="coerce")
    else:
        close = out["close"]
        out["pct_change"] = close.pct_change() * 100.0
    out["volume"] = out["volume"].astype("Int64")
    out = out.dropna(subset=["effective_date"])
    return out.reset_index(drop=True)


def migrate_stock_daily(
    warehouse: ResearchWarehouse,
    daily_root: str | Path,
    *,
    layer: str = "raw",
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    codes: list[str] | None = None,
) -> MigrationReport:
    """``daily/raw`` 数据湖 → ``stock_daily`` 数据集（按股票分批，幂等）。

    现 schema 无法区分复权口径，**仅支持 raw**；传入 qfq 直接拒绝并记录，
    避免与 raw 混写同一 (symbol, effective_date) 键空间。
    """
    report = MigrationReport(
        dataset="stock_daily", source=SOURCE_STOCK_DAILY_RAW, dry_run=dry_run
    )
    if layer != "raw":
        report.errors.append(
            f"仅支持迁移 raw 层（现 schema 无法区分复权口径），拒绝 layer={layer!r}"
        )
        return report
    raw_dir = Path(daily_root) / "raw"
    if not raw_dir.exists():
        report.errors.append(f"raw 目录不存在: {raw_dir}")
        return report
    if (Path(daily_root) / "qfq").exists():
        report.notes.append("qfq 目录存在但已跳过：schema 无法区分 raw/qfq 口径，不混写")
    if batch_size <= 0:
        batch_size = DEFAULT_BATCH_SIZE

    paths = sorted(raw_dir.glob("*.parquet"))
    if codes:
        wanted = set(codes)
        paths = [p for p in paths if p.stem in wanted]
    report.skipped = 0
    if dry_run:
        for path in paths:
            frame = _raw_daily_frame(path)
            if frame is None:
                report.skipped += 1
            else:
                report.scanned += len(frame)
        report.notes.append("dry-run：仅统计，未写入")
        return report

    _purge_migration_parquet(warehouse, "stock_daily", SOURCE_STOCK_DAILY_RAW)
    buffer: list[pd.DataFrame] = []
    buffered = 0
    file_seq = 0
    for path in paths:
        frame = _raw_daily_frame(path)
        if frame is None:
            report.skipped += 1
            continue
        report.scanned += len(frame)
        buffer.append(frame)
        buffered += len(frame)
        if buffered >= batch_size:
            merged = pd.concat(buffer, ignore_index=True)
            report.written += write_batch(
                warehouse, "stock_daily", merged,
                source=SOURCE_STOCK_DAILY_RAW, file_seq=file_seq,
            )
            file_seq += max(1, merged["effective_date"].map(lambda d: d.year).nunique())
            report.batches += 1
            buffer, buffered = [], 0
    if buffer:
        merged = pd.concat(buffer, ignore_index=True)
        report.written += write_batch(
            warehouse, "stock_daily", merged,
            source=SOURCE_STOCK_DAILY_RAW, file_seq=file_seq,
        )
        report.batches += 1
    warehouse.refresh_views()
    return report


# ---------------------------------------------------------------------------
# 3) index_constituents -> universe_membership
# ---------------------------------------------------------------------------


def migrate_universe_membership(
    warehouse: ResearchWarehouse,
    sqlite_path: str | Path,
    *,
    dry_run: bool = False,
    index_codes: list[str] | None = None,
) -> MigrationReport:
    """当前指数成分（``index_constituents``）→ ``universe_membership``。

    effective_date 取 ``in_date``（中证公布纳入日）；缺失时回退该行的
    ``updated_at`` 日期并在 notes 记录条数。weight 数据源没有，置 NULL。
    每个指数代码一个批次。
    """
    report = MigrationReport(
        dataset="universe_membership", source=SOURCE_UNIVERSE, dry_run=dry_run
    )
    sqlite_path = Path(sqlite_path)
    if not sqlite_path.exists():
        report.errors.append(f"SQLite 不存在: {sqlite_path}")
        return report

    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        where, params = "", []
        if index_codes:
            placeholders = ",".join("?" * len(index_codes))
            where = f"WHERE index_code IN ({placeholders})"
            params = list(index_codes)
        rows = conn.execute(
            f"""
            SELECT index_code, stock_code, in_date, updated_at
            FROM index_constituents {where}
            ORDER BY index_code, stock_code
            """,
            params,
        ).fetchall()
        report.scanned = len(rows)
        fallback = sum(1 for r in rows if not r[2])
        if fallback:
            report.notes.append(
                f"{fallback} 行 in_date 缺失，effective_date 取 updated_at 的日期"
            )
        if dry_run:
            report.notes.append("dry-run：仅统计，未写入")
            return report

        _purge_migration_parquet(warehouse, "universe_membership", SOURCE_UNIVERSE)
        by_index: dict[str, list[tuple]] = {}
        for row in rows:
            by_index.setdefault(row[0], []).append(row)
        file_seq = 0
        for index_code, group in sorted(by_index.items()):
            frame = pd.DataFrame(
                {
                    "universe": [f"index:{index_code}"] * len(group),
                    "symbol": [str(r[1]) for r in group],
                    "weight": [None] * len(group),
                    "effective_date": [
                        date.fromisoformat(str(r[2])[:10])
                        if r[2]
                        else date.fromisoformat(str(r[3])[:10])
                        for r in group
                    ],
                }
            )
            report.written += write_batch(
                warehouse, "universe_membership", frame,
                source=SOURCE_UNIVERSE, file_seq=file_seq,
            )
            file_seq += max(1, frame["effective_date"].map(lambda d: d.year).nunique())
            report.batches += 1
        warehouse.refresh_views()
    except sqlite3.Error as exc:
        report.errors.append(f"读取 SQLite 失败: {exc}")
    finally:
        conn.close()
    return report
