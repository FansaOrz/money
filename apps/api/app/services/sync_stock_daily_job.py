"""受调度器管理的 A 股日线子进程。

长耗时第三方行情抓取与主调度循环隔离；文件锁避免调度器重启后重复执行。
"""

from __future__ import annotations

import fcntl
from pathlib import Path

from app.config import get_settings
from app.db.session import SessionLocal
from app.main import create_tables
from app.services.research.stock_data import sync_stock_daily
from app.services.sync_status import track_sync_run


def main() -> None:
    settings = get_settings()
    lock_path = Path(settings.research_data_dir).expanduser().resolve() / ".stock_daily.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A股日线同步已有实例运行，本次跳过", flush=True)
            return

        create_tables()
        db = SessionLocal()
        try:
            with track_sync_run(db, "stock_daily") as record:
                result = sync_stock_daily(
                    db,
                    limit=settings.scheduled_stock_sync_batch_size,
                    fetch_qfq=True,
                )
                record(
                    total=int(result.get("total") or 0),
                    updated=int(result.get("updated") or 0),
                    failed=int(result.get("failed") or 0),
                )
            print(f"A股日线同步完成：{result}", flush=True)
        finally:
            db.close()


if __name__ == "__main__":
    main()
