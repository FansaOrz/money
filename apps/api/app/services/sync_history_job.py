"""同步全部基金历史净值（默认近 5 年，断点续传）。

注意：该任务为手动/低频补数任务，不进入每日调度。
日常每日只跑 sync_job 增量最新净值；历史缺口用
``python -m app.services.sync_backfill_job --batch-size N --batch K`` 分批补齐。
"""

from app.db.session import SessionLocal
from app.main import create_tables
from app.services.fund_data import sync_fund_nav_history


def main() -> None:
    create_tables()
    db = SessionLocal()
    try:
        result = sync_fund_nav_history(db, years=5)
        # 摘要中去掉 details，避免日志过长；失败明细保留
        summary = {key: value for key, value in result.items() if key != "details"}
        print(f"历史净值同步完成：{summary}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
