"""数据库、Parquet 与策略账本的一致性备份和无覆盖恢复验证。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.parse import unquote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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
    encryption_key: str | None = None,
    offsite_directory: Path | None = None,
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
        parsed = urlparse(database_url.replace("postgresql+psycopg://", "postgresql://"))
        if not parsed.hostname or not parsed.username or not parsed.path.strip("/"):
            raise ValueError("PostgreSQL 备份连接配置不完整")
        password = unquote(parsed.password or "")
        with tempfile.TemporaryDirectory(prefix="money-pgpass-") as secret_dir:
            pgpass = Path(secret_dir) / ".pgpass"
            escaped = [
                str(value).replace("\\", "\\\\").replace(":", "\\:")
                for value in (
                    parsed.hostname,
                    parsed.port or 5432,
                    parsed.path.strip("/"),
                    unquote(parsed.username),
                    password,
                )
            ]
            pgpass.write_text(":".join(escaped) + "\n", encoding="utf-8")
            pgpass.chmod(0o600)
            environment = {**os.environ, "PGPASSFILE": str(pgpass)}
            command = [
                "pg_dump",
                "--format=custom",
                "--no-password",
                "--host",
                parsed.hostname,
                "--port",
                str(parsed.port or 5432),
                "--username",
                unquote(parsed.username),
                "--dbname",
                parsed.path.strip("/"),
                "--file",
                str(target),
            ]
            try:
                subprocess.run(
                    command,
                    check=True,
                    timeout=3600,
                    env=environment,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"pg_dump 失败（退出码 {exc.returncode}，敏感连接信息已脱敏）"
                ) from None
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

    encrypted = False
    if encryption_key is not None:
        if len(encryption_key) < 32:
            raise ValueError("备份加密密钥必须至少 32 字符")
        aes = AESGCM(hashlib.sha256(encryption_key.encode()).digest())
        encrypted_artifacts: list[Path] = []
        for artifact in artifacts:
            nonce = os.urandom(12)
            encrypted_path = artifact.with_suffix(artifact.suffix + ".aesgcm")
            encrypted_path.write_bytes(
                b"MONEY-BACKUP-V1\0"
                + nonce
                + aes.encrypt(nonce, artifact.read_bytes(), artifact.name.encode())
            )
            artifact.unlink()
            encrypted_artifacts.append(encrypted_path)
        artifacts = encrypted_artifacts
        encrypted = True
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "database_type": urlparse(database_url).scheme,
        "encrypted": encrypted,
        "encryption": "AES-256-GCM" if encrypted else None,
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
    for artifact in [*artifacts, manifest_path]:
        artifact.chmod(0o440)
    if offsite_directory is not None:
        offsite_directory = offsite_directory.resolve()
        if offsite_directory == destination or destination in offsite_directory.parents:
            raise ValueError("异地副本目录必须独立于本地备份目录")
        replica = offsite_directory / destination.name
        if replica.exists():
            raise FileExistsError(f"异地版本已存在：{replica}")
        shutil.copytree(destination, replica)
        for path in replica.rglob("*"):
            path.chmod(0o550 if path.is_dir() else 0o440)
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
    if manifest.get("encrypted"):
        return {"ok": True, "checked": checked, "encrypted": True}
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


def _materialize_encrypted(
    backup_dir: Path, work_dir: Path, *, encryption_key: str
) -> Path:
    if len(encryption_key) < 32:
        raise ValueError("恢复密钥无效")
    aes = AESGCM(hashlib.sha256(encryption_key.encode()).digest())
    artifacts: dict[str, dict[str, object]] = {}
    for source in backup_dir.glob("*.aesgcm"):
        payload = source.read_bytes()
        magic = b"MONEY-BACKUP-V1\0"
        if not payload.startswith(magic):
            raise ValueError(f"未知加密备份格式：{source.name}")
        nonce = payload[len(magic) : len(magic) + 12]
        original_name = source.name.removesuffix(".aesgcm")
        plaintext = aes.decrypt(
            nonce, payload[len(magic) + 12 :], original_name.encode()
        )
        target = work_dir / original_name
        target.write_bytes(plaintext)
        artifacts[original_name] = {
            "bytes": target.stat().st_size,
            "sha256": _sha256(target),
        }
    manifest = json.loads(
        (backup_dir / "manifest.json").read_text(encoding="utf-8")
    )
    manifest.update({"encrypted": False, "encryption": None, "artifacts": artifacts})
    (work_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return work_dir


def restore_to_new_directory(
    backup_dir: Path,
    target: Path,
    *,
    encryption_key: str | None = None,
) -> Path:
    """只恢复到不存在的新目录，禁止覆盖现有数据。"""
    verify_backup(backup_dir)
    if target.exists():
        raise FileExistsError(f"恢复目标已存在，拒绝覆盖：{target}")
    if json.loads((backup_dir / "manifest.json").read_text())["encrypted"]:
        if encryption_key is None:
            raise PermissionError("加密备份恢复必须显式提供独立密钥")
        with tempfile.TemporaryDirectory(prefix="money-restore-") as temporary:
            materialized = _materialize_encrypted(
                backup_dir, Path(temporary), encryption_key=encryption_key
            )
            return restore_to_new_directory(materialized, target)
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


def apply_retention(
    backup_root: Path,
    *,
    now: datetime | None = None,
    daily: int = 14,
    weekly: int = 8,
    monthly: int = 12,
) -> list[Path]:
    """保留每日、每周、每月版本；只删除不属于任何保留桶的完整目录。"""
    now = now or datetime.now(UTC)
    candidates: list[tuple[Path, datetime]] = []
    for manifest_path in backup_root.glob("*/manifest.json"):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        created = datetime.fromisoformat(payload["created_at"])
        candidates.append((manifest_path.parent, created))
    candidates.sort(key=lambda item: item[1], reverse=True)
    keep: set[Path] = {path for path, _ in candidates[:daily]}
    weekly_buckets: set[tuple[int, int]] = set()
    monthly_buckets: set[tuple[int, int]] = set()
    for path, created in candidates:
        week = created.isocalendar()[:2]
        month = (created.year, created.month)
        if len(weekly_buckets) < weekly and week not in weekly_buckets:
            weekly_buckets.add(week)
            keep.add(path)
        if len(monthly_buckets) < monthly and month not in monthly_buckets:
            monthly_buckets.add(month)
            keep.add(path)
    removed: list[Path] = []
    for path, _ in candidates:
        if path not in keep:
            shutil.rmtree(path)
            removed.append(path)
    return removed
