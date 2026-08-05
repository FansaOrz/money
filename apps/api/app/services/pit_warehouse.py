"""用 DuckDB 外部视图规范化大规模 PIT 数据，避免复制千万行 JSON 到 SQLite。"""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
from pathlib import Path
import shutil

from sqlalchemy.orm import Session

from app.models import QuantImportRun
from app.services.quant_data_governance import DATASET_DATE_FIELDS


PIT_MODEL_VERSION = "PIT_EXTERNAL_PARQUET_V1"
CONSUMPTION_MAP = {
    "income": "StockRepository.fundamentals -> immutable parquet; PIT view for audit",
    "balancesheet": "StockRepository.fundamentals -> immutable parquet; PIT view for audit",
    "cashflow": "StockRepository.fundamentals -> immutable parquet; PIT view for audit",
    "fina_indicator": "StockRepository.fundamentals -> immutable parquet; PIT view for audit",
    "daily_basic": "StockRepository.fundamentals/valuation -> immutable parquet",
    "adj_factor": "StockRepository.market_bars research adjustment -> immutable parquet",
    "dividend": "StockRepository.corporate_actions -> normalized corporate_action master",
    "suspend_d": "StockRepository.execution_rules -> immutable parquet",
    "stk_limit": "StockRepository.execution_rules -> immutable parquet",
}


def _quote(value: str) -> str:
    return value.replace("'", "''")


def _date_expression(field: str) -> str:
    return (
        "CAST(try_strptime(substr(CAST(p."
        f"\"{field}\" AS VARCHAR), 1, 8), '%Y%m%d') AS DATE)"
    )


def build_pit_warehouse(
    db: Session,
    *,
    research_root: Path,
    datasets: list[str] | None = None,
) -> dict[str, object]:
    import duckdb
    import pyarrow.parquet as pq

    wanted = datasets or list(DATASET_DATE_FIELDS)
    warehouse_path = research_root / "research.duckdb"
    snapshot_root = research_root / "tushare_snapshot" / "stocks"
    started = datetime.now(UTC)
    run = QuantImportRun(
        dataset="pit_warehouse:" + ",".join(wanted),
        status="running",
        source_root=str(snapshot_root),
        imported=0,
        skipped=0,
        invalid=0,
        detail={"model_version": PIT_MODEL_VERSION},
        started_at=started,
    )
    db.add(run)
    db.commit()
    connection = duckdb.connect(str(warehouse_path))
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pit_file_manifest (
            dataset VARCHAR NOT NULL,
            source_file VARCHAR NOT NULL,
            source_hash VARCHAR NOT NULL,
            row_count BIGINT NOT NULL,
            file_size BIGINT NOT NULL,
            mtime_ns BIGINT NOT NULL,
            archive_file VARCHAR,
            ingested_at TIMESTAMPTZ NOT NULL,
            model_version VARCHAR NOT NULL,
            PRIMARY KEY(dataset, source_file)
        )
        """
    )
    connection.execute(
        "ALTER TABLE pit_file_manifest ADD COLUMN IF NOT EXISTS archive_file VARCHAR"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pit_file_manifest_history (
            dataset VARCHAR NOT NULL,
            source_file VARCHAR NOT NULL,
            source_hash VARCHAR NOT NULL,
            archive_file VARCHAR NOT NULL,
            row_count BIGINT NOT NULL,
            system_valid_from TIMESTAMPTZ NOT NULL,
            system_valid_to TIMESTAMPTZ,
            model_version VARCHAR NOT NULL,
            PRIMARY KEY(dataset, source_file, source_hash)
        )
        """
    )
    result: dict[str, dict[str, object]] = {}
    total_files = total_rows = 0
    try:
        for dataset in wanted:
            date_fields = DATASET_DATE_FIELDS.get(dataset)
            if date_fields is None:
                result[dataset] = {"status": "unsupported"}
                continue
            directory = snapshot_root / dataset
            paths = sorted(directory.glob("*.parquet"))
            source_rows = 0
            for path in paths:
                absolute = str(path.resolve())
                row_count = pq.read_metadata(path).num_rows
                stat = path.stat()
                source_rows += row_count
                current = connection.execute(
                    """
                    SELECT source_hash, row_count, file_size, mtime_ns, archive_file
                    FROM pit_file_manifest
                    WHERE dataset = ? AND source_file = ?
                    """,
                    [dataset, absolute],
                ).fetchone()
                archive_dir = research_root / "pit_archive" / dataset
                archive_dir.mkdir(parents=True, exist_ok=True)
                if (
                    current is not None
                    and int(current[1]) == row_count
                    and int(current[2]) == stat.st_size
                    and int(current[3]) == stat.st_mtime_ns
                ):
                    checksum = str(current[0])
                else:
                    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                archive_path = (archive_dir / f"{checksum}.parquet").resolve()
                if not archive_path.exists():
                    shutil.copy2(path, archive_path)
                now = datetime.now(UTC)
                if current is not None and str(current[0]) != checksum:
                    connection.execute(
                        """
                        UPDATE pit_file_manifest_history
                        SET system_valid_to = ?
                        WHERE dataset = ?
                          AND source_file = ?
                          AND system_valid_to IS NULL
                        """,
                        [now, dataset, absolute],
                    )
                connection.execute(
                    """
                    INSERT INTO pit_file_manifest_history
                    VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(dataset, source_file, source_hash) DO NOTHING
                    """,
                    [
                        dataset,
                        absolute,
                        checksum,
                        str(archive_path),
                        row_count,
                        now,
                        PIT_MODEL_VERSION,
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO pit_file_manifest (
                        dataset,
                        source_file,
                        source_hash,
                        row_count,
                        file_size,
                        mtime_ns,
                        archive_file,
                        ingested_at,
                        model_version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dataset, source_file) DO UPDATE SET
                        source_hash = excluded.source_hash,
                        row_count = excluded.row_count,
                        file_size = excluded.file_size,
                        mtime_ns = excluded.mtime_ns,
                        archive_file = excluded.archive_file,
                        ingested_at = excluded.ingested_at,
                        model_version = excluded.model_version
                    """,
                    [
                        dataset,
                        absolute,
                        checksum,
                        row_count,
                        stat.st_size,
                        stat.st_mtime_ns,
                        str(archive_path),
                        now,
                        PIT_MODEL_VERSION,
                    ],
                )
            effective_field, available_field, fallback_field = date_fields
            archive_glob = _quote(
                str((research_root / "pit_archive" / dataset / "*.parquet").resolve())
            )
            effective = (
                "COALESCE("
                + ", ".join(
                    (
                        _date_expression("ex_date"),
                        _date_expression("record_date"),
                        _date_expression("end_date"),
                    )
                )
                + ")"
                if dataset == "dividend"
                else _date_expression(effective_field)
            )
            available_primary = _date_expression(available_field)
            available_fallback = _date_expression(fallback_field)
            connection.execute(f'DROP VIEW IF EXISTS "pit_{dataset}"')
            connection.execute(
                f"""
                CREATE VIEW "pit_{dataset}" AS
                SELECT
                    p.* EXCLUDE(filename),
                    m.source_file,
                    {effective} AS effective_date,
                    COALESCE(
                        {available_primary},
                        {available_fallback},
                        CAST(m.ingested_at AS DATE)
                    )
                        AS available_date,
                    (
                        {available_primary} IS NULL
                        AND {available_fallback} IS NULL
                    ) AS availability_fallback_to_ingested_at,
                    m.ingested_at,
                    m.source_hash,
                    m.model_version AS pit_model_version
                FROM read_parquet(
                    '{archive_glob}',
                    filename = true,
                    union_by_name = true
                ) AS p
                JOIN pit_file_manifest AS m
                  ON m.dataset = '{_quote(dataset)}'
                 AND m.archive_file = p.filename
                """
            )
            connection.execute(f'DROP VIEW IF EXISTS "pit_history_{dataset}"')
            connection.execute(
                f"""
                CREATE VIEW "pit_history_{dataset}" AS
                SELECT
                    p.* EXCLUDE(filename),
                    h.source_file,
                    {effective} AS effective_date,
                    COALESCE(
                        {available_primary},
                        {available_fallback},
                        CAST(h.system_valid_from AS DATE)
                    ) AS available_date,
                    h.system_valid_from AS ingested_at,
                    h.system_valid_from,
                    h.system_valid_to,
                    h.source_hash,
                    h.model_version AS pit_model_version
                FROM read_parquet(
                    '{archive_glob}',
                    filename = true,
                    union_by_name = true
                ) AS p
                JOIN pit_file_manifest_history AS h
                  ON h.dataset = '{_quote(dataset)}'
                 AND h.archive_file = p.filename
                """
            )
            normalized_rows = int(
                connection.execute(f'SELECT count(*) FROM "pit_{dataset}"').fetchone()[
                    0
                ]
            )
            invalid_dates = int(
                connection.execute(
                    f"""
                    SELECT count(*) FROM "pit_{dataset}"
                    WHERE effective_date IS NULL OR available_date IS NULL
                    """
                ).fetchone()[0]
            )
            result[dataset] = {
                "status": (
                    "ready"
                    if normalized_rows == source_rows and invalid_dates == 0
                    else "degraded"
                ),
                "files": len(paths),
                "source_rows": source_rows,
                "normalized_rows": normalized_rows,
                "invalid_dates": invalid_dates,
                "view": f"pit_{dataset}",
                "consumer": CONSUMPTION_MAP[dataset],
            }
            total_files += len(paths)
            total_rows += normalized_rows
        run.imported = total_rows
        run.status = (
            "success"
            if all(item.get("status") == "ready" for item in result.values())
            else "partial"
        )
        run.detail = {
            "model_version": PIT_MODEL_VERSION,
            "warehouse": str(warehouse_path),
            "datasets": result,
            "total_files": total_files,
        }
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception as exc:
        run.status = "failed"
        run.detail = {
            **dict(run.detail or {}),
            "error": str(exc),
            "datasets": result,
        }
        run.finished_at = datetime.now(UTC)
        db.commit()
        raise
    finally:
        connection.close()
    return {
        "status": run.status,
        "model_version": PIT_MODEL_VERSION,
        "warehouse": str(warehouse_path),
        "total_files": total_files,
        "total_rows": total_rows,
        "datasets": result,
    }


def pit_status(research_root: Path) -> dict[str, object]:
    import duckdb

    warehouse_path = research_root / "research.duckdb"
    if not warehouse_path.exists():
        return {"status": "missing", "datasets": {}}
    connection = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        views = {
            row[0]
            for row in connection.execute(
                """
                SELECT table_name
                FROM information_schema.views
                WHERE table_name LIKE 'pit_%'
                """
            ).fetchall()
        }
        datasets = {}
        for dataset in DATASET_DATE_FIELDS:
            view = f"pit_{dataset}"
            if view not in views:
                datasets[dataset] = {"status": "missing"}
                continue
            rows, invalid = connection.execute(
                f"""
                SELECT
                    count(*),
                    count(*) FILTER (
                        WHERE effective_date IS NULL OR available_date IS NULL
                    )
                FROM "{view}"
                """
            ).fetchone()
            datasets[dataset] = {
                "status": "ready" if invalid == 0 else "degraded",
                "rows": int(rows),
                "invalid_dates": int(invalid),
                "consumer": CONSUMPTION_MAP[dataset],
            }
        return {
            "status": (
                "ready"
                if all(item["status"] == "ready" for item in datasets.values())
                else "degraded"
            ),
            "model_version": PIT_MODEL_VERSION,
            "warehouse": str(warehouse_path),
            "datasets": datasets,
        }
    finally:
        connection.close()


def query_as_of(
    research_root: Path,
    *,
    dataset: str,
    code: str,
    economic_as_of: date,
    system_as_of: datetime,
    limit: int = 1000,
) -> list[dict[str, object]]:
    """按经济公开时点和系统历史时点同时重建当时可见版本。"""
    import duckdb

    if dataset not in DATASET_DATE_FIELDS:
        raise ValueError(f"不支持的 PIT 数据集：{dataset}")
    connection = duckdb.connect(
        str(research_root / "research.duckdb"),
        read_only=True,
    )
    try:
        cursor = connection.execute(
            f"""
            SELECT *
            FROM "pit_history_{dataset}"
            WHERE split_part(CAST(ts_code AS VARCHAR), '.', 1) = ?
              AND effective_date <= ?
              AND available_date <= ?
              AND system_valid_from <= ?
              AND (
                    system_valid_to IS NULL
                    OR system_valid_to > ?
              )
            ORDER BY effective_date, available_date, system_valid_from
            LIMIT ?
            """,
            [
                code.split(".")[0],
                economic_as_of,
                economic_as_of,
                system_as_of,
                system_as_of,
                limit,
            ],
        )
        columns = [item[0] for item in cursor.description]
        return [
            {
                key: (
                    value.isoformat() if isinstance(value, (date, datetime)) else value
                )
                for key, value in zip(columns, row, strict=True)
            }
            for row in cursor.fetchall()
        ]
    finally:
        connection.close()
