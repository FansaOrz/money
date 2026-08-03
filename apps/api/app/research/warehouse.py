"""DuckDB 连接与 Parquet 分区目录管理。

目录布局（``data_dir`` 为根）::

    data_dir/
      research.duckdb                 # DuckDB 数据库文件（视图 + 元数据）
      warehouse.lock                  # 跨进程初始化互斥锁（best-effort）
      fund_nav/year=2024/part-*.parquet
      stock_daily/year=2024/part-*.parquet
      universe_membership/year=2024/part-*.parquet
      fundamentals/year=2023/part-*.parquet
      factor_panel/year=2024/part-*.parquet

- Parquet 文件按 ``year`` Hive 分区，写入幂等：写前按主键 + 涉及年份分区删除旧行，
  每次写入同时双写主表与磁盘 Parquet（研究脚本可无 DuckDB 直接读湖）。
- DuckDB 内为每张数据集建视图 ``<dataset>_all``：主表全量 UNION 磁盘中
  "主表没有的版本"（按 键 + available_at/ingested_at 反连接去重），
  因而全新库接管已有 lake、或同一批重试留下多份 Parquet 文件都不会重复计数。
- 初始化使用文件锁保证并发 create 安全；``init_schemas`` 可重复调用（幂等）。
"""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# 数据集定义（逻辑 schema，含快照元数据列，见 snapshots.py）
# ---------------------------------------------------------------------------

#: 数据集名 -> DuckDB 建表列定义（业务列；快照列统一附加）
DATASET_BUSINESS_COLUMNS: dict[str, str] = {
    # 基金净值：每个基金每个生效日期一条
    "fund_nav": (
        "fund_code VARCHAR NOT NULL, "
        "nav DOUBLE, "
        "accumulated_nav DOUBLE, "
        "daily_return DOUBLE"
    ),
    # 股票日线行情
    "stock_daily": (
        "symbol VARCHAR NOT NULL, "
        "open DOUBLE, "
        "high DOUBLE, "
        "low DOUBLE, "
        "close DOUBLE, "
        "volume BIGINT, "
        "amount DOUBLE, "
        "turnover DOUBLE, "
        "pct_change DOUBLE"
    ),
    # 宇宙（股票池/指数成分）成员关系：生效日期起属于某宇宙
    "universe_membership": (
        "universe VARCHAR NOT NULL, "
        "symbol VARCHAR NOT NULL, "
        "weight DOUBLE"
    ),
    # 财务数据：按报告期（effective_date = 报告期末）记录
    "fundamentals": (
        "symbol VARCHAR NOT NULL, "
        "report_period VARCHAR NOT NULL, "
        "metric VARCHAR NOT NULL, "
        "metric_value DOUBLE"
    ),
    # 因子面板：长表 (symbol, date, factor_name, factor_value)
    "factor_panel": (
        "symbol VARCHAR NOT NULL, "
        "factor_name VARCHAR NOT NULL, "
        "factor_value DOUBLE"
    ),
}

#: 快照元数据列（值由 snapshots.py 生成/校验）
SNAPSHOT_COLUMNS = (
    "effective_date DATE NOT NULL, "
    "available_at TIMESTAMP NOT NULL, "
    "ingested_at TIMESTAMP NOT NULL, "
    "source VARCHAR NOT NULL, "
    "row_hash VARCHAR NOT NULL"
)

#: 数据集名 -> 业务主键（幂等删除/质量检查用；快照列 effective_date 另行拼接）
DATASET_BUSINESS_KEYS: dict[str, list[str]] = {
    "fund_nav": ["fund_code"],
    "stock_daily": ["symbol"],
    "universe_membership": ["universe", "symbol"],
    "fundamentals": ["symbol", "report_period", "metric"],
    "factor_panel": ["symbol", "factor_name"],
}

ALL_DATASETS = tuple(DATASET_BUSINESS_COLUMNS)

_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_dataset(name: str) -> str:
    """校验数据集名合法（防 SQL 注入 / 路径穿越）。"""
    if name not in DATASET_BUSINESS_COLUMNS or not _SAFE_NAME.match(name):
        msg = f"未知数据集: {name!r}，可选: {sorted(DATASET_BUSINESS_COLUMNS)}"
        raise ValueError(msg)
    return name


class ResearchWarehouse:
    """研究数据仓库入口：持有 DuckDB 连接与 Parquet 根目录。"""

    def __init__(
        self,
        db_path: str | Path,
        data_dir: str | Path,
        *,
        read_only: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.data_dir = Path(data_dir).resolve()
        self.read_only = read_only
        if not read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self.db_path), read_only=read_only)

    # -- 连接 ---------------------------------------------------------------

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """底层 DuckDB 连接（同一进程内共享，调用方勿 close）。"""
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ResearchWarehouse:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- 目录布局 -------------------------------------------------------------

    def dataset_dir(self, dataset: str) -> Path:
        """数据集 Parquet 分区根目录。"""
        return self.data_dir / validate_dataset(dataset)

    def partition_dir(self, dataset: str, year: int) -> Path:
        """某年份的 Hive 分区目录（``year=YYYY``）。"""
        return self.dataset_dir(dataset) / f"year={year}"

    def new_partition_file(self, dataset: str, year: int) -> Path:
        """生成一个不冲突的分区内 Parquet 文件名。"""
        return self.partition_dir(dataset, year) / f"part-{uuid.uuid4().hex[:12]}.parquet"

    def parquet_glob(self, dataset: str) -> str:
        """数据集全部 Parquet 的 glob（含 Hive 分区）。"""
        return str(self.dataset_dir(dataset) / "year=*/part-*.parquet")

    # -- 初始化 ---------------------------------------------------------------

    def init_schemas(self) -> None:
        """幂等初始化：建表 + 合并视图 + 数据集目录。并发安全（文件锁）。"""
        with self._file_lock():
            for dataset in ALL_DATASETS:
                self.dataset_dir(dataset).mkdir(parents=True, exist_ok=True)
                self._conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {dataset} "
                    f"({DATASET_BUSINESS_COLUMNS[dataset]}, {SNAPSHOT_COLUMNS})"
                )
                self._refresh_view(dataset)

    def _refresh_view(self, dataset: str) -> None:
        """重建 ``<dataset>_all`` 视图：表内行 + 磁盘 Parquet 行（union_by_name）。

        磁盘 Parquet 的 Hive 分区列 ``year`` 仅用于目录组织，不进视图。
        """
        glob = self.parquet_glob(dataset)
        # DuckDB 对无匹配文件的 read_parquet 会直接报错。初始化空仓库时先创建
        # 纯表视图；首个 Parquet 写入后 refresh_views 再切换为联合视图。
        if not list(self.dataset_dir(dataset).glob("year=*/part-*.parquet")):
            self._conn.execute(
                f"CREATE OR REPLACE VIEW {dataset}_all AS SELECT * FROM {dataset}"
            )
            return
        # 磁盘 Parquet 与主表存在镜像重复（每次写入双写）；且同一批重试/重写
        # 会在磁盘留下多份文件。策略：主表全量 + 磁盘中"主表没有的版本"。
        # 注意不能按业务键整体反连接（否则全新库接管已有 lake 时磁盘行全被遮蔽）。
        key_cols = [*DATASET_BUSINESS_KEYS[dataset], "effective_date", "source"]
        ver_cols = ["available_at", "ingested_at"]
        join_cond = " AND ".join(
            [f"p.{c} IS NOT DISTINCT FROM t.{c}" for c in key_cols + ver_cols]
        )
        self._conn.execute(
            f"""
            CREATE OR REPLACE VIEW {dataset}_all AS
            SELECT * FROM {dataset}
            UNION ALL BY NAME
            SELECT p.* EXCLUDE (year) FROM read_parquet(
                '{glob}', hive_partitioning = true, union_by_name = true
            ) p
            ANTI JOIN {dataset} t ON {join_cond}
            """
        )

    def refresh_views(self) -> None:
        """外部写入 Parquet 后调用，刷新全部合并视图。"""
        for dataset in ALL_DATASETS:
            self._refresh_view(dataset)

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        """跨进程初始化互斥锁（best-effort；锁文件位于 data_dir）。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.data_dir / "warehouse.lock"
        fd = lock_path.open("a+")
        try:
            try:
                import fcntl

                fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - 非 POSIX 平台降级为无锁
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                pass
            fd.close()
