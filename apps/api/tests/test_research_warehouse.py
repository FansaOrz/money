"""研究数据仓库（DuckDB + Parquet 分区）测试。

覆盖：
- 初始化幂等（重复 init_schemas / 并发目录结构）；
- 五类数据集基本读写（基金净值/股票行情/宇宙/财务/因子面板）；
- 写入幂等（同键重写不产生重复，row_hash 稳定）；
- as_of 过滤（point-in-time：修订后旧视角仍可见旧版本）；
- Parquet 分区落盘 + 视图去重（表与磁盘不双计）；
- 质量检查（空库、非正价格、重复键、日期缺口、未来日期）；
- CompositeRepository 读回退。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from app.research.quality import check_all, check_dataset
from app.research.repository import CompositeRepository, DuckDBRepository
from app.research.snapshots import compute_row_hash, normalize_frame
from app.research.warehouse import ALL_DATASETS, ResearchWarehouse


@pytest.fixture()
def warehouse(tmp_path: Path):
    wh = ResearchWarehouse(tmp_path / "research.duckdb", tmp_path / "lake")
    wh.init_schemas()
    yield wh
    wh.close()


@pytest.fixture()
def repo(warehouse: ResearchWarehouse) -> DuckDBRepository:
    return DuckDBRepository(warehouse)


def _nav_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "fund_code": ["000001", "000001", "000002"],
            "effective_date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 2)],
            "nav": [1.0, 1.01, 2.0],
            "accumulated_nav": [1.0, 1.01, 2.5],
            "daily_return": [0.0, 0.01, 0.0],
        }
    )


def _stock_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["600519", "600519"],
            "effective_date": [date(2024, 3, 1), date(2024, 3, 4)],
            "open": [1700.0, 1710.0],
            "high": [1720.0, 1725.0],
            "low": [1690.0, 1700.0],
            "close": [1710.0, 1720.0],
            "volume": [10000, 12000],
            "amount": [1.71e7, 2.05e7],
            "turnover": [0.1, 0.12],
            "pct_change": [0.5, 0.58],
        }
    )


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------


def test_init_schemas_idempotent(tmp_path: Path) -> None:
    wh = ResearchWarehouse(tmp_path / "r.duckdb", tmp_path / "lake")
    wh.init_schemas()
    wh.init_schemas()  # 重复调用不报错
    for dataset in ALL_DATASETS:
        assert (tmp_path / "lake" / dataset).is_dir()
    tables = {r[0] for r in wh.conn.execute("SHOW TABLES").fetchall()}
    for dataset in ALL_DATASETS:
        assert dataset in tables
        assert f"{dataset}_all" in tables
    wh.close()


def test_unknown_dataset_rejected(repo: DuckDBRepository) -> None:
    with pytest.raises(ValueError, match="未知数据集"):
        repo.read("evil; DROP TABLE fund_nav")
    with pytest.raises(ValueError, match="未知数据集"):
        repo.write("../escape", pd.DataFrame({"a": [1]}), source="x")


# ---------------------------------------------------------------------------
# 基本读写
# ---------------------------------------------------------------------------


def test_fund_nav_roundtrip(repo: DuckDBRepository) -> None:
    n = repo.write_fund_nav(_nav_rows(), source="eastmoney")
    assert n == 3
    df = repo.read_fund_nav("000001")
    assert len(df) == 2
    assert list(df["effective_date"]) == [date(2024, 1, 2), date(2024, 1, 3)]
    assert set(df["source"]) == {"eastmoney"}
    assert {"available_at", "ingested_at", "row_hash"} <= set(df.columns)


def test_stock_daily_roundtrip_and_range(repo: DuckDBRepository) -> None:
    repo.write_stock_daily(_stock_rows(), source="akshare")
    df = repo.read_stock_daily("600519", start=date(2024, 3, 2), end=date(2024, 3, 31))
    assert len(df) == 1
    assert df.iloc[0]["close"] == 1720.0


def test_universe_membership(repo: DuckDBRepository) -> None:
    rows = pd.DataFrame(
        {
            "universe": ["csi300"] * 2,
            "symbol": ["600519", "000001"],
            "weight": [5.0, 1.0],
            "effective_date": [date(2024, 6, 14)] * 2,
        }
    )
    repo.write_universe_membership(rows, source="csindex")
    df = repo.read_universe("csi300")
    assert sorted(df["symbol"]) == ["000001", "600519"]


def test_fundamentals(repo: DuckDBRepository) -> None:
    rows = pd.DataFrame(
        {
            "symbol": ["600519"] * 2,
            "report_period": ["2023Q4", "2024Q1"],
            "metric": ["roe", "roe"],
            "metric_value": [30.0, 31.0],
            "effective_date": [date(2023, 12, 31), date(2024, 3, 31)],
        }
    )
    repo.write_fundamentals(rows, source="sina")
    df = repo.read_fundamentals("600519", metrics=["roe"])
    assert len(df) == 2
    assert df["metric_value"].max() == 31.0


def test_factor_panel(repo: DuckDBRepository) -> None:
    rows = pd.DataFrame(
        {
            "symbol": ["600519", "000001"],
            "factor_name": ["momentum_20d"] * 2,
            "factor_value": [0.12, -0.03],
            "effective_date": [date(2024, 5, 31)] * 2,
        }
    )
    repo.write_factor_panel(rows, source="research")
    df = repo.read_factor_panel(factors=["momentum_20d"])
    assert len(df) == 2
    df_one = repo.read_factor_panel("600519")
    assert df_one.iloc[0]["factor_value"] == pytest.approx(0.12)


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


def test_write_idempotent_same_batch(repo: DuckDBRepository, warehouse: ResearchWarehouse) -> None:
    repo.write_fund_nav(_nav_rows(), source="eastmoney")
    repo.write_fund_nav(_nav_rows(), source="eastmoney")  # 全量重写
    df = repo.read_fund_nav()
    assert len(df) == 3  # 无重复
    # 表内也只有 3 行（视图 ANTI JOIN 只是双保险）
    (n,) = warehouse.conn.execute("SELECT count(*) FROM fund_nav").fetchone()
    assert n == 3


def test_write_partial_overwrite(repo: DuckDBRepository) -> None:
    repo.write_fund_nav(_nav_rows(), source="eastmoney")
    update = pd.DataFrame(
        {
            "fund_code": ["000001"],
            "effective_date": [date(2024, 1, 2)],
            "nav": [1.5],
        }
    )
    repo.write_fund_nav(update, source="eastmoney")
    # 无 as_of 时保留历史修订；指定当前时间时返回最新版本。
    history = repo.read_fund_nav("000001")
    assert len(history) == 3
    df = repo.read_fund_nav("000001", as_of=datetime.now())
    assert len(df) == 2
    corrected = df[df["effective_date"] == date(2024, 1, 2)].iloc[0]
    assert corrected["nav"] == 1.5
    assert repo.read_fund_nav("000002").iloc[0]["nav"] == 2.0  # 未受影响


def test_row_hash_stable() -> None:
    h1 = compute_row_hash(["000001", date(2024, 1, 2), 1.0, None])
    h2 = compute_row_hash(["000001", date(2024, 1, 2), 1.0, None])
    h3 = compute_row_hash(["000001", date(2024, 1, 2), 1.1, None])
    assert h1 == h2 and h1 != h3


# ---------------------------------------------------------------------------
# as_of 过滤（point-in-time）
# ---------------------------------------------------------------------------


def test_as_of_filters_unpublished_rows(repo: DuckDBRepository) -> None:
    rows = _nav_rows().copy()
    rows["available_at"] = [
        datetime(2024, 1, 2, 20, 0),
        datetime(2024, 1, 3, 20, 0),
        datetime(2024, 1, 2, 20, 0),
    ]
    repo.write_fund_nav(rows, source="eastmoney")
    df = repo.read_fund_nav("000001", as_of=datetime(2024, 1, 2, 23, 59))
    assert len(df) == 1
    assert df.iloc[0]["effective_date"] == date(2024, 1, 2)


def test_as_of_picks_latest_known_version(repo: DuckDBRepository) -> None:
    v1 = pd.DataFrame(
        {
            "fund_code": ["000001"],
            "effective_date": [date(2024, 1, 2)],
            "nav": [1.0],
            "available_at": [datetime(2024, 1, 2, 18, 0)],
        }
    )
    v2 = v1.copy()
    v2["nav"] = [1.05]  # 修订
    v2["available_at"] = [datetime(2024, 1, 5, 9, 0)]
    repo.write_fund_nav(v1, source="eastmoney")
    repo.write_fund_nav(v2, source="eastmoney")

    # 修订发布前：看到旧值
    old = repo.read_fund_nav("000001", as_of=datetime(2024, 1, 3))
    assert old.iloc[0]["nav"] == 1.0
    # 修订发布后：看到新值
    new = repo.read_fund_nav("000001", as_of=datetime(2024, 1, 6))
    assert new.iloc[0]["nav"] == 1.05
    # 不加 as_of：两个版本都在（血缘可追溯）
    assert len(repo.read_fund_nav("000001")) == 2


def test_universe_as_of(repo: DuckDBRepository) -> None:
    rows = pd.DataFrame(
        {
            "universe": ["pool"],
            "symbol": ["AAA"],
            "effective_date": [date(2024, 6, 14)],
            "available_at": [datetime(2024, 6, 10)],
        }
    )
    repo.write_universe_membership(rows, source="csv")
    assert repo.read_universe("pool", as_of=datetime(2024, 6, 9)).empty
    assert len(repo.read_universe("pool", as_of=datetime(2024, 6, 11))) == 1


# ---------------------------------------------------------------------------
# Parquet 分区
# ---------------------------------------------------------------------------


def test_parquet_partitions_written(tmp_path: Path, repo: DuckDBRepository) -> None:
    repo.write_fund_nav(_nav_rows(), source="eastmoney")
    parts = list((tmp_path / "lake" / "fund_nav" / "year=2024").glob("part-*.parquet"))
    assert len(parts) == 1
    on_disk = pd.read_parquet(parts[0])
    assert len(on_disk) == 3


def test_no_double_count_between_table_and_parquet(repo: DuckDBRepository) -> None:
    # 分两批跨年写入，再整体重写第一批：视图行数始终等于去重后业务行数
    repo.write_fund_nav(_nav_rows(), source="eastmoney")
    jan2025 = pd.DataFrame(
        {
            "fund_code": ["000001"],
            "effective_date": [date(2025, 1, 2)],
            "nav": [1.2],
        }
    )
    repo.write_fund_nav(jan2025, source="eastmoney")
    repo.write_fund_nav(_nav_rows(), source="eastmoney")  # 重写 2024 批
    df = repo.read_fund_nav()
    assert len(df) == 4
    assert len(df.drop_duplicates(["fund_code", "effective_date"])) == 4


def test_read_without_duckdb_table(tmp_path: Path) -> None:
    """纯 Parquet 也能被第二个（全新）仓库连接读到。"""
    wh1 = ResearchWarehouse(tmp_path / "a.duckdb", tmp_path / "lake")
    DuckDBRepository(wh1).write_fund_nav(_nav_rows(), source="eastmoney")
    wh1.close()

    wh2 = ResearchWarehouse(tmp_path / "b.duckdb", tmp_path / "lake")
    wh2.init_schemas()
    df = DuckDBRepository(wh2, auto_init=False).read_fund_nav("000002")
    assert len(df) == 1
    wh2.close()


# ---------------------------------------------------------------------------
# snapshots.normalize_frame
# ---------------------------------------------------------------------------


def test_normalize_frame_defaults() -> None:
    df = pd.DataFrame({"fund_code": ["000001"], "effective_date": ["2024-01-02"], "nav": [1.0]})
    out = normalize_frame(df, business_columns=["fund_code", "nav"], source="unit-test")
    assert out.iloc[0]["effective_date"] == date(2024, 1, 2)
    assert out.iloc[0]["source"] == "unit-test"
    assert out.iloc[0]["available_at"] <= pd.Timestamp.now()
    assert len(out.iloc[0]["row_hash"]) == 40


def test_normalize_frame_requires_effective_date() -> None:
    with pytest.raises(ValueError, match="effective_date"):
        normalize_frame(pd.DataFrame({"a": [1]}), business_columns=["a"], source="x")


# ---------------------------------------------------------------------------
# 质量检查
# ---------------------------------------------------------------------------


def test_quality_empty_dataset(warehouse: ResearchWarehouse) -> None:
    report = check_dataset(warehouse, "fund_nav")
    assert report.row_count == 0
    assert report.ok  # 空只是 warning
    assert report.issues[0].check == "empty"


def test_quality_clean_data(repo: DuckDBRepository, warehouse: ResearchWarehouse) -> None:
    repo.write_fund_nav(_nav_rows(), source="eastmoney")
    report = check_dataset(warehouse, "fund_nav")
    assert report.ok
    assert report.row_count == 3


def test_quality_detects_negative_price(repo: DuckDBRepository, warehouse: ResearchWarehouse) -> None:
    bad = _nav_rows().copy()
    bad.loc[0, "nav"] = -1.0
    repo.write_fund_nav(bad, source="eastmoney")
    report = check_dataset(warehouse, "fund_nav")
    assert not report.ok
    assert any(i.check == "negative_prices" for i in report.issues)


def test_quality_detects_future_dates(repo: DuckDBRepository, warehouse: ResearchWarehouse) -> None:
    future = pd.DataFrame(
        {
            "fund_code": ["000001"],
            "effective_date": [date(2999, 1, 1)],
            "nav": [1.0],
        }
    )
    repo.write_fund_nav(future, source="eastmoney")
    report = check_dataset(warehouse, "fund_nav")
    assert not report.ok
    assert any(i.check == "future_effective" for i in report.issues)


def test_quality_detects_date_gap(repo: DuckDBRepository, warehouse: ResearchWarehouse) -> None:
    gappy = pd.DataFrame(
        {
            "fund_code": ["000001", "000001"],
            "effective_date": [date(2024, 1, 2), date(2024, 3, 15)],
            "nav": [1.0, 1.1],
        }
    )
    repo.write_fund_nav(gappy, source="eastmoney")
    report = check_dataset(warehouse, "fund_nav", gap_max_days=10)
    gap_issues = [i for i in report.issues if i.check == "date_gaps"]
    assert gap_issues and gap_issues[0].severity == "warning"
    assert report.ok  # warning 不影响 ok


def test_quality_check_all(repo: DuckDBRepository, warehouse: ResearchWarehouse) -> None:
    repo.write_stock_daily(_stock_rows(), source="akshare")
    reports = check_all(warehouse)
    assert set(reports) == set(ALL_DATASETS)
    assert reports["stock_daily"].row_count == 2
    assert reports["stock_daily"].ok


# ---------------------------------------------------------------------------
# CompositeRepository
# ---------------------------------------------------------------------------


def test_composite_read_fallback(tmp_path: Path) -> None:
    wh_a = ResearchWarehouse(tmp_path / "a.duckdb", tmp_path / "lake_a")
    wh_b = ResearchWarehouse(tmp_path / "b.duckdb", tmp_path / "lake_b")
    repo_a = DuckDBRepository(wh_a)
    repo_b = DuckDBRepository(wh_b)
    repo_b.write_fund_nav(_nav_rows(), source="eastmoney")

    composite = CompositeRepository(repo_a, [repo_b])
    df = composite.read_fund_nav("000001")
    assert len(df) == 2  # 主仓为空时回退到 repo_b

    composite.write_fund_nav(_nav_rows(), source="eastmoney")  # 写主仓
    assert len(repo_a.read_fund_nav()) == 3
    wh_a.close()
    wh_b.close()
