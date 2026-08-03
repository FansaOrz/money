"""研究仓库迁移（app.research.migrate）测试。

覆盖：
- fund_navs SQLite → fund_nav 幂等迁移（分批、重复执行不重复、不删旧数据）；
- daily/raw per-code Parquet → stock_daily 幂等迁移（qfq 明确拒绝/跳过）；
- index_constituents → universe_membership 迁移（in_date 缺失回退 updated_at）；
- dry-run 只统计不写入；
- 迁移版本不遮蔽实时写入（虚拟时间戳 < 真实 ingested_at）；
- 稳定快照：重复迁移不产生伪修订版本（as-of 口径行数不变）。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from app.research import migrate
from app.research.repository import DuckDBRepository
from app.research.warehouse import ResearchWarehouse
from app.services.research.migration import run_migrations


@pytest.fixture()
def warehouse(tmp_path: Path):
    wh = ResearchWarehouse(tmp_path / "research.duckdb", tmp_path / "lake")
    wh.init_schemas()
    yield wh
    wh.close()


@pytest.fixture()
def repo(warehouse: ResearchWarehouse) -> DuckDBRepository:
    return DuckDBRepository(warehouse, auto_init=False)


@pytest.fixture()
def biz_sqlite(tmp_path: Path) -> Path:
    """临时业务库：instruments + fund_navs + index_constituents。"""
    path = tmp_path / "biz.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE instruments (
            id INTEGER PRIMARY KEY, code VARCHAR(32) UNIQUE NOT NULL,
            name VARCHAR(200) NOT NULL, type VARCHAR(20) NOT NULL DEFAULT 'fund',
            currency VARCHAR(3) NOT NULL DEFAULT 'CNY',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE fund_navs (
            id INTEGER PRIMARY KEY, instrument_id INTEGER NOT NULL,
            nav_date DATE NOT NULL, unit_nav NUMERIC(18,6) NOT NULL,
            accumulated_nav NUMERIC(18,6), daily_growth_rate NUMERIC(10,4),
            source VARCHAR NOT NULL DEFAULT 'eastmoney',
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE index_constituents (
            id INTEGER PRIMARY KEY, index_code VARCHAR(10) NOT NULL,
            index_name VARCHAR(50), stock_code VARCHAR(10) NOT NULL,
            stock_name VARCHAR(50), in_date DATE,
            source VARCHAR(30) NOT NULL DEFAULT 'csindex',
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.execute("INSERT INTO instruments VALUES (1, '110022', '基金A', 'fund', 'CNY', '2024-01-01')")
    conn.execute("INSERT INTO instruments VALUES (2, '000300X', '基金B', 'fund', 'CNY', '2024-01-01')")
    navs = [
        (1, 1, "2024-01-02", 1.0, 1.5, 0.0, "eastmoney", "2024-01-03 01:00:00"),
        (2, 1, "2024-01-03", 1.01, 1.51, 1.0, "eastmoney", "2024-01-04 01:00:00"),
        (3, 2, "2024-01-02", 2.0, 2.5, 0.5, "eastmoney_fast", "2024-01-03 01:00:00"),
    ]
    conn.executemany("INSERT INTO fund_navs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", navs)
    cons = [
        (1, "000300", "沪深300", "600519", "贵州茅台", "2024-06-14", "csindex", "2024-06-15 02:00:00"),
        (2, "000300", "沪深300", "000001", "平安银行", None, "csindex", "2024-06-15 02:00:00"),
        (3, "000905", "中证500", "000002", "万科A", None, "csindex", "2024-06-15 02:00:00"),
    ]
    conn.executemany("INSERT INTO index_constituents VALUES (?, ?, ?, ?, ?, ?, ?, ?)", cons)
    conn.commit()
    conn.close()
    return path


def _write_raw_daily(root: Path, code: str, rows: list[tuple]) -> None:
    """构造 daily/raw/<code>.parquet（列与真实数据湖一致）。"""
    raw_dir = root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        rows,
        columns=["code", "trade_date", "open", "high", "low", "close",
                 "volume", "amount", "outstanding_share", "turnover"],
    )
    frame.to_parquet(raw_dir / f"{code}.parquet", index=False)


@pytest.fixture()
def daily_root(tmp_path: Path) -> Path:
    root = tmp_path / "daily"
    _write_raw_daily(
        root,
        "000001",
        [
            ("000001", "2024-03-01", 10.0, 10.5, 9.9, 10.2, 1000, 10200.0, 1e8, 0.1),
            ("000001", "2024-03-04", 10.2, 10.6, 10.1, 10.5, 1200, 12600.0, 1e8, 0.12),
        ],
    )
    _write_raw_daily(
        root,
        "600519",
        [
            ("600519", "2024-03-01", 1700.0, 1720.0, 1690.0, 1710.0, 800, 1.4e6, 1e8, 0.05),
        ],
    )
    # qfq 目录存在但不应被迁移
    qfq = root / "qfq"
    qfq.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [("000001", "2024-03-01", 9.0, 9.5, 8.9, 9.2, 1000, 9200.0)],
        columns=["code", "trade_date", "open", "high", "low", "close", "volume", "amount"],
    ).to_parquet(qfq / "000001.parquet", index=False)
    return root


# ---------------------------------------------------------------------------
# fund_navs -> fund_nav
# ---------------------------------------------------------------------------


def test_migrate_fund_navs_basic(
    warehouse: ResearchWarehouse, repo: DuckDBRepository, biz_sqlite: Path
) -> None:
    report = migrate.migrate_fund_navs(warehouse, biz_sqlite, batch_size=2)
    assert report.ok and report.scanned == 3 and report.written == 3
    assert report.batches >= 2  # batch_size=2 分了两批

    df = repo.read_fund_nav("110022")
    assert len(df) == 2
    assert list(df["effective_date"]) == [date(2024, 1, 2), date(2024, 1, 3)]
    assert df.iloc[1]["nav"] == pytest.approx(1.01)
    assert set(df["source"]) == {migrate.SOURCE_FUND_NAV}
    assert set(df["available_at"]) == {pd.Timestamp(migrate.MIGRATION_VIRTUAL_TS)}


def test_migrate_fund_navs_idempotent(
    warehouse: ResearchWarehouse, biz_sqlite: Path
) -> None:
    migrate.migrate_fund_navs(warehouse, biz_sqlite, batch_size=2)
    again = migrate.migrate_fund_navs(warehouse, biz_sqlite, batch_size=1)  # 不同分批重跑
    assert again.ok
    (n,) = warehouse.conn.execute("SELECT count(*) FROM fund_nav").fetchone()
    assert n == 3  # 无重复行、无伪修订版本
    (distinct_hashes,) = warehouse.conn.execute(
        "SELECT count(DISTINCT row_hash) FROM fund_nav"
    ).fetchone()
    assert distinct_hashes == 3
    # 磁盘分区文件不累积：重跑前后数量一致（确定性文件名覆盖写）
    files = sorted(warehouse.dataset_dir("fund_nav").glob("year=*/part-migrate-*.parquet"))
    assert len(files) == 3  # batch_size=1 -> 3 批 -> 3 个文件（均 2024 分区）
    third = migrate.migrate_fund_navs(warehouse, biz_sqlite, batch_size=1)
    assert third.ok
    files_after = sorted(warehouse.dataset_dir("fund_nav").glob("year=*/part-migrate-*.parquet"))
    assert [f.name for f in files_after] == [f.name for f in files]


def test_migrate_fund_navs_dry_run(
    warehouse: ResearchWarehouse, biz_sqlite: Path
) -> None:
    report = migrate.migrate_fund_navs(warehouse, biz_sqlite, dry_run=True)
    assert report.ok and report.scanned == 3 and report.written == 0
    (n,) = warehouse.conn.execute("SELECT count(*) FROM fund_nav").fetchone()
    assert n == 0


def test_migrate_fund_navs_missing_sqlite(warehouse: ResearchWarehouse, tmp_path: Path) -> None:
    report = migrate.migrate_fund_navs(warehouse, tmp_path / "nope.db")
    assert not report.ok and report.errors


# ---------------------------------------------------------------------------
# daily/raw -> stock_daily
# ---------------------------------------------------------------------------


def test_migrate_stock_daily_basic(
    warehouse: ResearchWarehouse, repo: DuckDBRepository, daily_root: Path
) -> None:
    report = migrate.migrate_stock_daily(warehouse, daily_root, batch_size=2)
    assert report.ok and report.scanned == 3 and report.written == 3
    assert any("qfq" in note for note in report.notes)  # qfq 跳过有记录

    df = repo.read_stock_daily("000001")
    assert len(df) == 2
    assert df.iloc[0]["close"] == pytest.approx(10.2)
    # raw 无 pct_change 列时按收盘价推算
    assert df.iloc[1]["pct_change"] == pytest.approx((10.5 / 10.2 - 1) * 100, rel=1e-6)


def test_migrate_stock_daily_idempotent(
    warehouse: ResearchWarehouse, repo: DuckDBRepository, daily_root: Path
) -> None:
    migrate.migrate_stock_daily(warehouse, daily_root, batch_size=2)
    again = migrate.migrate_stock_daily(warehouse, daily_root, batch_size=100)
    assert again.ok
    df = repo.read_stock_daily()
    assert len(df) == 3
    (n,) = warehouse.conn.execute("SELECT count(*) FROM stock_daily").fetchone()
    assert n == 3


def test_migrate_stock_daily_rejects_qfq(
    warehouse: ResearchWarehouse, daily_root: Path
) -> None:
    report = migrate.migrate_stock_daily(warehouse, daily_root, layer="qfq")
    assert not report.ok
    assert any("raw" in err for err in report.errors)
    (n,) = warehouse.conn.execute("SELECT count(*) FROM stock_daily").fetchone()
    assert n == 0


def test_migrate_stock_daily_dry_run(
    warehouse: ResearchWarehouse, daily_root: Path
) -> None:
    report = migrate.migrate_stock_daily(warehouse, daily_root, dry_run=True)
    assert report.ok and report.scanned == 3 and report.written == 0
    (n,) = warehouse.conn.execute("SELECT count(*) FROM stock_daily").fetchone()
    assert n == 0


def test_migrate_stock_daily_skips_corrupt(
    warehouse: ResearchWarehouse, daily_root: Path
) -> None:
    bad = daily_root / "raw" / "999999.parquet"
    bad.write_bytes(b"not a parquet")
    report = migrate.migrate_stock_daily(warehouse, daily_root)
    assert report.ok and report.scanned == 3 and report.skipped == 1


# ---------------------------------------------------------------------------
# index_constituents -> universe_membership
# ---------------------------------------------------------------------------


def test_migrate_universe_basic(
    warehouse: ResearchWarehouse, repo: DuckDBRepository, biz_sqlite: Path
) -> None:
    report = migrate.migrate_universe_membership(warehouse, biz_sqlite)
    assert report.ok and report.scanned == 3 and report.written == 3
    assert any("in_date" in note for note in report.notes)  # 2 行缺失有记录

    csi300 = repo.read_universe("index:000300")
    assert sorted(csi300["symbol"]) == ["000001", "600519"]
    by_symbol = dict(zip(csi300["symbol"], csi300["effective_date"], strict=True))
    assert by_symbol["600519"] == date(2024, 6, 14)  # in_date
    assert by_symbol["000001"] == date(2024, 6, 15)  # 回退 updated_at 日期


def test_migrate_universe_idempotent(
    warehouse: ResearchWarehouse, biz_sqlite: Path
) -> None:
    migrate.migrate_universe_membership(warehouse, biz_sqlite)
    again = migrate.migrate_universe_membership(warehouse, biz_sqlite)
    assert again.ok
    (n,) = warehouse.conn.execute("SELECT count(*) FROM universe_membership").fetchone()
    assert n == 3


# ---------------------------------------------------------------------------
# 与实时写入共存：迁移不遮蔽、不删除其他 source 的数据
# ---------------------------------------------------------------------------


def test_migration_does_not_shadow_live_writes(
    warehouse: ResearchWarehouse, repo: DuckDBRepository, biz_sqlite: Path
) -> None:
    live = pd.DataFrame(
        {
            "fund_code": ["110022"],
            "effective_date": [date(2024, 1, 2)],
            "nav": [9.99],
        }
    )
    repo.write_fund_nav(live, source="eastmoney")
    migrate.migrate_fund_navs(warehouse, biz_sqlite)

    current = repo.read_fund_nav("110022", as_of=datetime.now())
    row = current[current["effective_date"] == date(2024, 1, 2)].iloc[0]
    assert row["nav"] == pytest.approx(9.99)  # 实时版本优先于迁移版本
    assert row["source"] == "eastmoney"

    migrate.migrate_fund_navs(warehouse, biz_sqlite)  # 重跑不动实时行
    current2 = repo.read_fund_nav("110022", as_of=datetime.now())
    row2 = current2[current2["effective_date"] == date(2024, 1, 2)].iloc[0]
    assert row2["nav"] == pytest.approx(9.99)
    (live_rows,) = warehouse.conn.execute(
        "SELECT count(*) FROM fund_nav WHERE source = 'eastmoney'"
    ).fetchone()
    assert live_rows == 1


def test_run_migrations_end_to_end(tmp_path: Path, biz_sqlite: Path, daily_root: Path) -> None:
    result = run_migrations(
        sqlite_path=biz_sqlite,
        daily_root=daily_root,
        db_path=tmp_path / "r.duckdb",
        data_dir=tmp_path / "lake2",
        batch_size=2,
    )
    assert result.ok
    counts = {r.dataset: r.written for r in result.reports}
    assert counts == {"fund_nav": 3, "stock_daily": 3, "universe_membership": 3}

    # dry-run 于同一目标：只统计，行数不变
    dry = run_migrations(
        sqlite_path=biz_sqlite,
        daily_root=daily_root,
        db_path=tmp_path / "r.duckdb",
        data_dir=tmp_path / "lake2",
        dry_run=True,
    )
    assert all(r.written == 0 for r in dry.reports)

    # 再次实跑保持幂等
    rerun = run_migrations(
        sqlite_path=biz_sqlite,
        daily_root=daily_root,
        db_path=tmp_path / "r.duckdb",
        data_dir=tmp_path / "lake2",
    )
    assert rerun.ok
    wh = ResearchWarehouse(tmp_path / "r.duckdb", tmp_path / "lake2", read_only=True)
    try:
        for dataset, expected in counts.items():
            (n,) = wh.conn.execute(f"SELECT count(*) FROM {dataset}").fetchone()
            assert n == expected
    finally:
        wh.close()


def test_run_migrations_rejects_unknown_dataset(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="未知迁移数据集"):
        run_migrations(datasets=["factor_panel"], sqlite_path=tmp_path / "x.db")


def test_migration_cli(tmp_path: Path, biz_sqlite: Path, daily_root: Path) -> None:
    from app.services.research.migration import main

    rc = main(
        [
            "--sqlite", str(biz_sqlite),
            "--daily-root", str(daily_root),
            "--db", str(tmp_path / "cli.duckdb"),
            "--data-dir", str(tmp_path / "cli_lake"),
            "--dataset", "fund_nav",
            "--batch-size", "2",
        ]
    )
    assert rc == 0
