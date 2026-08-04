"""数据库、Parquet 与策略账本的一致性备份和无覆盖恢复验证。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.orm import Session


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("不是 SQLite URL")
    return Path(database_url[len(prefix) :]).resolve()


def create_backup(
    db: Session,
    *,
    database_url: str,
    research_data_dir: Path,
    destination: Path,
) -> dict[str, object]:
    research_data_dir = research_data_dir.resolve()
    destination = destination.resolve()
    if destination == research_data_dir or research_data_dir in destination.parents:
        raise ValueError("备份目录不能位于研究数据目录内部，避免归档递归包含自身")
    destination.mkdir(parents=True, exist_ok=True)
    artifacts: list[Path] = []
    if database_url.startswith("sqlite:///"):
        source = _sqlite_path(database_url)
        target = destination / "database.sqlite3"
        with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
            source_db.backup(target_db)
        artifacts.append(target)
    elif database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        target = destination / "database.dump"
        subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(target), database_url],
            check=True,
            timeout=3600,
        )
        artifacts.append(target)
    else:
        raise ValueError("仅支持 SQLite/PostgreSQL 备份")

    lake_archive = destination / "research_data.tar.gz"
    with tarfile.open(lake_archive, "w:gz") as archive:
        if research_data_dir.exists():
            archive.add(research_data_dir, arcname="research_data", recursive=True)
    artifacts.append(lake_archive)

    ledger = destination / "strategy_ledger.json"
    ledger_tables = (
        "stock_paper_accounts",
        "stock_paper_positions",
        "stock_paper_receivables",
        "stock_paper_runs",
        "stock_paper_signals",
        "stock_paper_trades",
        "stock_paper_nav_daily",
        "broker_orders",
        "broker_fills",
    )
    payload: dict[str, list[dict[str, object]]] = {}
    bind = db.get_bind()
    available_tables = set(inspect(bind).get_table_names())
    metadata = MetaData()
    for table_name in ledger_tables:
        if table_name not in available_tables:
            payload[table_name] = []
            continue
        table = Table(table_name, metadata, autoload_with=bind)
        rows = db.execute(select(table)).mappings().all()
        payload[table_name] = [
            {
                key: (
                    value.isoformat()
                    if hasattr(value, "isoformat")
                    else float(value)
                    if value.__class__.__name__ == "Decimal"
                    else value
                )
                for key, value in row.items()
                if value is not None
            }
            for row in rows
        ]
    ledger.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    artifacts.append(ledger)

    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "database_type": urlparse(database_url).scheme,
        "artifacts": {
            artifact.name: {
                "bytes": artifact.stat().st_size,
                "sha256": _sha256(artifact),
            }
            for artifact in artifacts
        },
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def verify_backup(directory: Path) -> dict[str, object]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    checked: list[str] = []
    for name, metadata in manifest["artifacts"].items():
        path = directory / name
        if not path.is_file():
            raise ValueError(f"备份缺少 {name}")
        if _sha256(path) != metadata["sha256"]:
            raise ValueError(f"备份校验和不匹配：{name}")
        checked.append(name)
    database = directory / "database.sqlite3"
    if database.exists():
        with sqlite3.connect(database) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise ValueError(f"SQLite 完整性检查失败：{result}")
    archive = directory / "research_data.tar.gz"
    if archive.exists():
        with tarfile.open(archive, "r:gz") as handle:
            # 只检查可完整读取，不直接释放到用户目录。
            for member in handle:
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise ValueError(f"归档含不安全路径：{member.name}")
    json.loads((directory / "strategy_ledger.json").read_text(encoding="utf-8"))
    return {"ok": True, "checked": checked}


def restore_to_new_directory(backup_dir: Path, target: Path) -> Path:
    """只恢复到不存在的新目录，禁止覆盖现有数据。"""
    verify_backup(backup_dir)
    if target.exists():
        raise FileExistsError(f"恢复目标已存在，拒绝覆盖：{target}")
    target.mkdir(parents=True)
    for name in ("database.sqlite3", "database.dump", "strategy_ledger.json"):
        source = backup_dir / name
        if source.exists():
            shutil.copy2(source, target / name)
    archive = backup_dir / "research_data.tar.gz"
    if archive.exists():
        with tempfile.TemporaryDirectory(dir=target) as temporary:
            temporary_path = Path(temporary)
            with tarfile.open(archive, "r:gz") as handle:
                handle.extractall(temporary_path, filter="data")
            restored = temporary_path / "research_data"
            if restored.exists():
                shutil.move(str(restored), target / "research_data")
    return target
