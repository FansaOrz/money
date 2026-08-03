"""研究数据仓库读写层。

- ``MarketDataRepository``  抽象接口：五类数据集的读写 + as-of 查询；
- ``DuckDBRepository``      基于 ``ResearchWarehouse``（DuckDB + Parquet）的默认实现；
- ``CompositeRepository``   多源组合（读：按优先级回退；写：主仓或扇出）。

写入语义（幂等）：
1. 业务键 + effective_date + 涉及年份分区 先 DELETE 再 INSERT（重写同键数据安全）；
2. 同步将本批数据追加写入对应 ``year=YYYY`` 分区 Parquet，供无 DuckDB 的研究脚本直读；
3. 同一 (业务键, effective_date) 多版本（数据修订）通过 available_at 区分，
   as-of 查询自动取 ``available_at <= as_of`` 的最新版本。
"""

from __future__ import annotations

import abc
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from app.research.snapshots import as_of_latest_sql, normalize_frame
from app.research.warehouse import (
    DATASET_BUSINESS_KEYS,
    ResearchWarehouse,
    validate_dataset,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: 数据集名 -> 业务列（顺序与 warehouse 建表一致）
DATASET_BUSINESS_COLUMNS: dict[str, list[str]] = {
    "fund_nav": ["fund_code", "nav", "accumulated_nav", "daily_return"],
    "stock_daily": [
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "turnover",
        "pct_change",
    ],
    "universe_membership": ["universe", "symbol", "weight"],
    "fundamentals": ["symbol", "report_period", "metric", "metric_value"],
    "factor_panel": ["symbol", "factor_name", "factor_value"],
}


def _key_columns(dataset: str) -> list[str]:
    """幂等键：业务键 + 生效日。修订版本由 available_at 查询最近值。"""
    return [*DATASET_BUSINESS_KEYS[dataset], "effective_date"]


def _normalize_codes(codes: str | Sequence[str] | None) -> list[str] | None:
    if codes is None:
        return None
    if isinstance(codes, str):
        return [codes]
    return list(codes)


class MarketDataRepository(abc.ABC):
    """研究数据仓库抽象接口。"""

    # -- 通用读写 -------------------------------------------------------------

    @abc.abstractmethod
    def write(
        self,
        dataset: str,
        df: pd.DataFrame,
        *,
        source: str,
        available_at: datetime | pd.Timestamp | None = None,
        ingest_date: datetime | pd.Timestamp | None = None,
    ) -> int:
        """写入数据集（幂等），返回行数。"""

    @abc.abstractmethod
    def read(
        self,
        dataset: str,
        *,
        columns: Sequence[str] | None = None,
        where: str | None = None,
        params: Sequence[Any] | None = None,
        start: date | None = None,
        end: date | None = None,
        as_of: datetime | pd.Timestamp | None = None,
        dedupe_latest: bool = True,
    ) -> pd.DataFrame:
        """读取数据集。

        - ``as_of``      仅保留 ``available_at <= as_of`` 的版本（防前视）；
        - ``dedupe_latest``  同一键多版本时只保留最新版本。
        """

    # -- 基金净值 ---------------------------------------------------------------

    def write_fund_nav(self, df: pd.DataFrame, *, source: str, **kw: Any) -> int:
        return self.write("fund_nav", df, source=source, **kw)

    def read_fund_nav(
        self,
        codes: str | Sequence[str] | None = None,
        *,
        start: date | None = None,
        end: date | None = None,
        as_of: datetime | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        codes_list = _normalize_codes(codes)
        where, params = "1=1", []
        if codes_list:
            where = f"fund_code IN ({','.join('?' * len(codes_list))})"
            params.extend(codes_list)
        return self.read(
            "fund_nav", where=where, params=params, start=start, end=end, as_of=as_of,
            dedupe_latest=as_of is not None,
        )

    # -- 股票行情 ---------------------------------------------------------------

    def write_stock_daily(self, df: pd.DataFrame, *, source: str, **kw: Any) -> int:
        return self.write("stock_daily", df, source=source, **kw)

    def read_stock_daily(
        self,
        symbols: str | Sequence[str] | None = None,
        *,
        start: date | None = None,
        end: date | None = None,
        as_of: datetime | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        symbols_list = _normalize_codes(symbols)
        where, params = "1=1", []
        if symbols_list:
            where = f"symbol IN ({','.join('?' * len(symbols_list))})"
            params.extend(symbols_list)
        return self.read(
            "stock_daily", where=where, params=params, start=start, end=end, as_of=as_of
        )

    # -- 宇宙（股票池）成员 -------------------------------------------------------

    def write_universe_membership(self, df: pd.DataFrame, *, source: str, **kw: Any) -> int:
        return self.write("universe_membership", df, source=source, **kw)

    def read_universe(
        self,
        universe: str,
        *,
        as_of: datetime | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """某宇宙在 as_of 时点已知的最新成员列表。"""
        return self.read(
            "universe_membership",
            where="universe = ?",
            params=[universe],
            as_of=as_of,
        )

    # -- 财务数据 ---------------------------------------------------------------

    def write_fundamentals(self, df: pd.DataFrame, *, source: str, **kw: Any) -> int:
        return self.write("fundamentals", df, source=source, **kw)

    def read_fundamentals(
        self,
        symbols: str | Sequence[str] | None = None,
        *,
        metrics: Sequence[str] | None = None,
        as_of: datetime | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        symbols_list = _normalize_codes(symbols)
        clauses, params = ["1=1"], []
        if symbols_list:
            clauses.append(f"symbol IN ({','.join('?' * len(symbols_list))})")
            params.extend(symbols_list)
        if metrics:
            clauses.append(f"metric IN ({','.join('?' * len(metrics))})")
            params.extend(metrics)
        return self.read("fundamentals", where=" AND ".join(clauses), params=params, as_of=as_of)

    # -- 因子面板 ---------------------------------------------------------------

    def write_factor_panel(self, df: pd.DataFrame, *, source: str, **kw: Any) -> int:
        return self.write("factor_panel", df, source=source, **kw)

    def read_factor_panel(
        self,
        symbols: str | Sequence[str] | None = None,
        *,
        factors: Sequence[str] | None = None,
        start: date | None = None,
        end: date | None = None,
        as_of: datetime | pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        symbols_list = _normalize_codes(symbols)
        clauses, params = ["1=1"], []
        if symbols_list:
            clauses.append(f"symbol IN ({','.join('?' * len(symbols_list))})")
            params.extend(symbols_list)
        if factors:
            clauses.append(f"factor_name IN ({','.join('?' * len(factors))})")
            params.extend(factors)
        return self.read(
            "factor_panel",
            where=" AND ".join(clauses),
            params=params,
            start=start,
            end=end,
            as_of=as_of,
        )


class DuckDBRepository(MarketDataRepository):
    """DuckDB + Parquet 分区目录实现。"""

    def __init__(self, warehouse: ResearchWarehouse, *, auto_init: bool = True) -> None:
        self.warehouse = warehouse
        if auto_init:
            warehouse.init_schemas()

    # -- 写 -------------------------------------------------------------------

    def write(
        self,
        dataset: str,
        df: pd.DataFrame,
        *,
        source: str,
        available_at: datetime | pd.Timestamp | None = None,
        ingest_date: datetime | pd.Timestamp | None = None,
    ) -> int:
        validate_dataset(dataset)
        business_columns = DATASET_BUSINESS_COLUMNS[dataset]
        missing = [c for c in business_columns[: len(DATASET_BUSINESS_KEYS[dataset])] if c not in df.columns]
        if missing:
            msg = f"{dataset} 缺少键列: {missing}"
            raise ValueError(msg)
        for col in business_columns:
            if col not in df.columns:
                df = df.assign(**{col: None})

        frame = normalize_frame(
            df,
            business_columns=business_columns,
            source=source,
            available_at=available_at,
            ingested_at=ingest_date,
        )
        if frame.empty:
            return 0

        conn = self.warehouse.conn
        keys = _key_columns(dataset)
        conn.register("_staging", frame)
        try:
            # 1) 幂等删除：本批涉及的键 + 本批涉及的年份分区（年份条件利于分区裁剪）
            years = sorted({pd.Timestamp(d).year for d in frame["effective_date"]})
            year_in = ",".join(str(y) for y in years)
            join_cond = " AND ".join(f"t.{k} = s.{k}" for k in keys)
            # 同一业务键+内容哈希的重试幂等；内容变化则作为修订版本保留。
            version_join = join_cond + " AND t.row_hash = s.row_hash"
            conn.execute(
                f"""
                DELETE FROM {dataset} t
                USING _staging s
                WHERE {version_join} AND year(t.effective_date) IN ({year_in})
                """
            )
            # 2) 插入 DuckDB 表
            conn.execute(f"INSERT INTO {dataset} SELECT * FROM _staging")
        finally:
            conn.unregister("_staging")

        # 3) 追加写 Parquet 分区（按年分组，一年一个文件）
        self._write_parquet_partitions(dataset, frame)
        return len(frame)

    def _write_parquet_partitions(self, dataset: str, frame: pd.DataFrame) -> None:
        years = frame["effective_date"].map(lambda value: value.year)
        for year, group in frame.groupby(years):
            path = self.warehouse.new_partition_file(dataset, int(year))
            path.parent.mkdir(parents=True, exist_ok=True)
            group.to_parquet(path, engine="pyarrow", compression="zstd", index=False)

    # -- 读 -------------------------------------------------------------------

    def read(
        self,
        dataset: str,
        *,
        columns: Sequence[str] | None = None,
        where: str | None = None,
        params: Sequence[Any] | None = None,
        start: date | None = None,
        end: date | None = None,
        as_of: datetime | pd.Timestamp | None = None,
        dedupe_latest: bool = True,
    ) -> pd.DataFrame:
        validate_dataset(dataset)
        select_cols = ", ".join(columns) if columns else "*"
        clauses: list[str] = []
        bind: list[Any] = list(params or [])

        if where:
            clauses.append(f"({where})")
        if start is not None:
            clauses.append("effective_date >= ?")
            bind.append(start)
        if end is not None:
            clauses.append("effective_date <= ?")
            bind.append(end)
        if as_of is not None:
            clauses.append("available_at <= ?")
            bind.append(pd.Timestamp(as_of).to_pydatetime())

        sql = f"SELECT {select_cols} FROM {dataset}_all"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if dedupe_latest:
            # 无论是否指定 as_of，默认每个业务日期只返回当时可见的最新修订。
            sql += " " + as_of_latest_sql(dataset, _key_columns(dataset))
        sql += " ORDER BY effective_date"

        frame = self.warehouse.conn.execute(sql, bind).fetchdf()
        if "effective_date" in frame.columns:
            frame["effective_date"] = frame["effective_date"].map(
                lambda value: value.date() if hasattr(value, "date") else value
            )
        return frame

    # -- 便捷 -----------------------------------------------------------------

    def list_sources(self, dataset: str) -> list[str]:
        """数据集内出现过的来源标识。"""
        validate_dataset(dataset)
        rows = self.warehouse.conn.execute(
            f"SELECT DISTINCT source FROM {dataset}_all ORDER BY 1"
        ).fetchall()
        return [r[0] for r in rows]


class CompositeRepository(MarketDataRepository):
    """多仓组合：读按优先级回退，写主仓（可选扇出）。

    典型场景：``primary`` 为本地 DuckDB 仓，``fallbacks`` 为只读镜像/其他来源；
    读取时若主仓结果为空则依次回退；写入默认只写主仓，``fanout_write=True``
    时广播到所有仓（单个失败不影响主仓写入结果）。
    """

    def __init__(
        self,
        primary: MarketDataRepository,
        fallbacks: Iterable[MarketDataRepository] = (),
        *,
        fanout_write: bool = False,
    ) -> None:
        self.primary = primary
        self.fallbacks = list(fallbacks)
        self.fanout_write = fanout_write

    def write(
        self,
        dataset: str,
        df: pd.DataFrame,
        *,
        source: str,
        available_at: datetime | pd.Timestamp | None = None,
        ingest_date: datetime | pd.Timestamp | None = None,
    ) -> int:
        written = self.primary.write(
            dataset, df, source=source, available_at=available_at, ingest_date=ingest_date
        )
        if self.fanout_write:
            for repo in self.fallbacks:
                try:
                    repo.write(
                        dataset,
                        df,
                        source=source,
                        available_at=available_at,
                        ingest_date=ingest_date,
                    )
                except Exception:  # noqa: BLE001 - 扇出写入尽力而为
                    continue
        return written

    def read(
        self,
        dataset: str,
        *,
        columns: Sequence[str] | None = None,
        where: str | None = None,
        params: Sequence[Any] | None = None,
        start: date | None = None,
        end: date | None = None,
        as_of: datetime | pd.Timestamp | None = None,
        dedupe_latest: bool = True,
    ) -> pd.DataFrame:
        for repo in [self.primary, *self.fallbacks]:
            result = repo.read(
                dataset,
                columns=columns,
                where=where,
                params=params,
                start=start,
                end=end,
                as_of=as_of,
                dedupe_latest=dedupe_latest,
            )
            if not result.empty:
                return result
        return pd.DataFrame()
