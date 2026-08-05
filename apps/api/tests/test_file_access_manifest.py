"""冻结文件清单与运行时读取一致性测试。"""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.models import DataFileAccessLog
from app.services.file_access_manifest import (
    FileManifestMismatch,
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
    statuses = {
        row.status for row in db_session.query(DataFileAccessLog).all()
    }
    assert statuses == {"verified", "hash_mismatch", "unregistered"}
