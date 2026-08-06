"""冻结文件清单与运行时读取一致性测试。"""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.models import DataFileAccessLog
from app.services import stock_backtest
from app.services.file_access_manifest import (
    FileManifestMismatch,
    discover_research_files,
    file_observation,
    freeze_manifest,
    verify_accesses,
)


def test_replaced_or_unregistered_file_cannot_silently_rerun(
    db_session: Session, tmp_path: Path
) -> None:
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    first.write_bytes(b"frozen-content")
    second.write_bytes(b"unregistered")
    snapshot = freeze_manifest(
        db_session,
        root=tmp_path,
        paths=[first],
    )
    observed = file_observation(first, tmp_path)
    verified = verify_accesses(
        db_session,
        root=tmp_path,
        snapshot_sha256=snapshot,
        observations=[observed],
    )
    assert verified["verified"] == 1
    first.write_bytes(b"replaced-content")
    with pytest.raises(FileManifestMismatch, match="hash_mismatch"):
        verify_accesses(
            db_session,
            root=tmp_path,
            snapshot_sha256=snapshot,
            observations=[observed],
        )
    with pytest.raises(FileManifestMismatch, match="unregistered"):
        verify_accesses(
            db_session,
            root=tmp_path,
            snapshot_sha256=snapshot,
            observations=[file_observation(second, tmp_path)],
        )
    statuses = {row.status for row in db_session.query(DataFileAccessLog).all()}
    assert statuses == {"verified", "hash_mismatch", "unregistered"}


def test_discovery_includes_global_index_industry_and_benchmark_files(
    tmp_path: Path,
) -> None:
    expected = [
        tmp_path / "daily/raw/600001.parquet",
        tmp_path / "tushare_snapshot/stocks/daily/600001.SH.parquet",
        tmp_path / "tushare_snapshot/global/trade_cal/SSE.parquet",
        tmp_path / "tushare_snapshot/indices/index_weight/000300.SH/2024.parquet",
        tmp_path / "tushare_snapshot/industries/sw2021/L1.parquet",
        tmp_path / "benchmarks/H00906.json",
        tmp_path / "indices/official_current/000300.xls",
    ]
    for path in expected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"frozen")

    discovered = discover_research_files(tmp_path, ["600001"])

    assert set(discovered) == set(expected)


def test_formal_backtest_uses_repository_governance_session_when_db_omitted() -> None:
    marker = object()

    class GovernedRepository:
        governance_db = marker

    repository = GovernedRepository()
    assert stock_backtest._resolve_governance_db(None, repository) is marker
    explicit = object()
    assert stock_backtest._resolve_governance_db(explicit, repository) is explicit
