"""冻结实际文件清单，并拒绝未登记、缺失或被替换的研究输入。"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DataFileAccessLog, DataFileManifestEntry


class FileManifestMismatch(RuntimeError):
    pass


def discover_research_files(root: Path, codes: list[str]) -> list[Path]:
    """枚举指定股票池可能被仓储消费的全部文件，供实验运行前冻结。"""
    wanted = set(codes)
    paths: set[Path] = set()
    for layer in ("raw", "qfq"):
        for code in wanted:
            path = root / "daily" / layer / f"{code}.parquet"
            if path.is_file():
                paths.add(path)
    stock_root = root / "tushare_snapshot" / "stocks"
    for dataset_dir in stock_root.iterdir() if stock_root.is_dir() else ():
        if not dataset_dir.is_dir():
            continue
        for code in wanted:
            paths.update(dataset_dir.glob(f"{code}.*.parquet"))
    for relative in (
        "tushare_snapshot/global/trade_cal/SSE.parquet",
        "tushare_snapshot/global/namechange/all.parquet",
    ):
        path = root / relative
        if path.is_file():
            paths.add(path)
    basic = root / "tushare_snapshot" / "global" / "stock_basic_full"
    if basic.is_dir():
        paths.update(basic.glob("*.parquet"))
    monthly = root / "tushare_snapshot" / "global" / "daily_basic_monthly"
    if monthly.is_dir():
        paths.update(monthly.glob("*.parquet"))
    return sorted(paths)


def file_observation(path: Path, root: Path) -> dict[str, object]:
    resolved = path.resolve()
    relative = str(resolved.relative_to(root.resolve()))
    payload = resolved.read_bytes()
    return {
        "relative_path": relative,
        "size_bytes": len(payload),
        "file_sha256": hashlib.sha256(payload).hexdigest(),
    }


def freeze_manifest(
    db: Session,
    *,
    root: Path,
    paths: list[Path],
) -> str:
    observations = [
        file_observation(path, root)
        for path in sorted(set(path.resolve() for path in paths))
    ]
    canonical = json.dumps(
        observations,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot = hashlib.sha256(canonical.encode()).hexdigest()
    if db.scalar(
        select(DataFileManifestEntry.id).where(
            DataFileManifestEntry.snapshot_sha256 == snapshot
        )
    ) is None:
        frozen_at = datetime.now(UTC)
        db.add_all(
            [
                DataFileManifestEntry(
                    snapshot_sha256=snapshot,
                    relative_path=str(item["relative_path"]),
                    size_bytes=int(item["size_bytes"]),
                    file_sha256=str(item["file_sha256"]),
                    frozen_at=frozen_at,
                )
                for item in observations
            ]
        )
        db.commit()
    return snapshot


def verify_accesses(
    db: Session,
    *,
    root: Path,
    snapshot_sha256: str,
    observations: list[dict[str, object]],
    strategy_version_id: int | None = None,
) -> dict[str, object]:
    expected = {
        row.relative_path: row
        for row in db.scalars(
            select(DataFileManifestEntry).where(
                DataFileManifestEntry.snapshot_sha256 == snapshot_sha256
            )
        ).all()
    }
    observed_paths: set[str] = set()
    failures: list[str] = []
    accessed_at = datetime.now(UTC)
    for observation in observations:
        relative = str(observation["relative_path"])
        observed_paths.add(relative)
        entry = expected.get(relative)
        current_path = root / relative
        current = (
            file_observation(current_path, root)
            if current_path.is_file()
            else None
        )
        status = "verified"
        detail = "运行时读取与冻结清单一致"
        if entry is None:
            status = "unregistered"
            detail = "实际读取文件未登记在冻结清单"
        elif current is None:
            status = "missing"
            detail = "冻结文件已缺失"
        elif (
            int(observation["size_bytes"]) != entry.size_bytes
            or str(observation["file_sha256"]) != entry.file_sha256
            or int(current["size_bytes"]) != entry.size_bytes
            or str(current["file_sha256"]) != entry.file_sha256
        ):
            status = "hash_mismatch"
            detail = "读取时或读取后文件大小/SHA-256 与冻结清单不一致"
        if status != "verified":
            failures.append(f"{relative}:{status}")
        db.add(
            DataFileAccessLog(
                snapshot_sha256=snapshot_sha256,
                strategy_version_id=strategy_version_id,
                relative_path=relative,
                observed_size_bytes=int(observation["size_bytes"]),
                observed_sha256=str(observation["file_sha256"]),
                status=status,
                detail=detail,
                accessed_at=accessed_at,
            )
        )
    missing_reads = sorted(set(expected) - observed_paths)
    db.commit()
    if failures:
        raise FileManifestMismatch("；".join(failures[:20]))
    return {
        "snapshot_sha256": snapshot_sha256,
        "accessed": len(observations),
        "verified": len(observations),
        "frozen_but_not_read": len(missing_reads),
    }
