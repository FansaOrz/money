"""研究仓库迁移服务：编排各数据集迁移 + 提供 CLI 入口。

用法（CLI）::

    python -m app.services.research.migration \\
        --sqlite data/money.db --daily-root data/research/daily \\
        --dataset all --batch-size 5000 --dry-run

默认参数取自 ``settings``（``research_db`` / ``research_data_dir`` / ``database_url``）。
所有迁移均为幂等、只读源、不删除旧数据；``--dry-run`` 只扫描统计不写入。
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.research import migrate
from app.research.warehouse import ResearchWarehouse

logger = logging.getLogger(__name__)

ALL_MIGRATIONS = ("fund_nav", "stock_daily", "universe_membership")


@dataclass
class MigrationRunResult:
    """一次迁移运行的汇总（多数据集）。"""

    dry_run: bool = False
    reports: list[migrate.MigrationReport] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(report.ok for report in self.reports)

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "ok": self.ok,
            "reports": [report.to_dict() for report in self.reports],
        }


def _sqlite_path_from_url(database_url: str) -> Path:
    """从 SQLAlchemy 连接串提取 SQLite 文件路径。"""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        msg = f"仅支持 SQLite 业务库迁移，收到: {database_url!r}"
        raise ValueError(msg)
    return Path(database_url[len(prefix):])


def run_migrations(
    *,
    datasets: list[str] | None = None,
    sqlite_path: str | Path | None = None,
    daily_root: str | Path | None = None,
    db_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    batch_size: int = migrate.DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    warehouse: ResearchWarehouse | None = None,
) -> MigrationRunResult:
    """执行指定数据集迁移（默认全部），返回逐数据集报告。

    不传路径时从 ``settings`` 解析；``warehouse`` 可注入（测试/复用连接），
    否则按 ``db_path``/``data_dir`` 打开并初始化。
    """
    settings = get_settings()
    targets = list(datasets or ALL_MIGRATIONS)
    for name in targets:
        if name not in ALL_MIGRATIONS:
            msg = f"未知迁移数据集: {name!r}，可选: {list(ALL_MIGRATIONS)}"
            raise ValueError(msg)

    sqlite = Path(sqlite_path) if sqlite_path else _sqlite_path_from_url(settings.database_url)
    daily = Path(daily_root) if daily_root else Path(settings.research_data_dir) / "daily"

    owns = warehouse is None
    wh = warehouse or ResearchWarehouse(
        db_path or settings.research_db,
        data_dir or settings.research_data_dir,
    )
    result = MigrationRunResult(dry_run=dry_run)
    try:
        if not dry_run:
            wh.init_schemas()
        if "fund_nav" in targets:
            result.reports.append(
                migrate.migrate_fund_navs(
                    wh, sqlite, batch_size=batch_size, dry_run=dry_run
                )
            )
        if "stock_daily" in targets:
            result.reports.append(
                migrate.migrate_stock_daily(
                    wh, daily, batch_size=batch_size, dry_run=dry_run
                )
            )
        if "universe_membership" in targets:
            result.reports.append(
                migrate.migrate_universe_membership(wh, sqlite, dry_run=dry_run)
            )
    finally:
        if owns:
            wh.close()
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：``python -m app.services.research.migration``。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="现有数据层 → DuckDB 研究仓库迁移")
    parser.add_argument("--sqlite", default=None, help="业务库 SQLite 路径（默认取 settings.database_url）")
    parser.add_argument("--daily-root", default=None, help="daily raw/qfq 数据湖目录（默认 <research_data_dir>/daily）")
    parser.add_argument("--db", default=None, help="研究仓库 DuckDB 文件（默认 settings.research_db）")
    parser.add_argument("--data-dir", default=None, help="研究仓库 Parquet 根目录（默认 settings.research_data_dir）")
    parser.add_argument(
        "--dataset",
        action="append",
        choices=[*ALL_MIGRATIONS, "all"],
        default=None,
        help="迁移目标，可重复；默认 all",
    )
    parser.add_argument("--batch-size", type=int, default=migrate.DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true", help="只扫描统计，不写入")
    args = parser.parse_args(argv)

    requested = args.dataset or ["all"]
    datasets = list(ALL_MIGRATIONS) if "all" in requested else requested
    result = run_migrations(
        datasets=datasets,
        sqlite_path=args.sqlite,
        daily_root=args.daily_root,
        db_path=args.db,
        data_dir=args.data_dir,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
