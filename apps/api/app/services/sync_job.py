"""每日基金净值同步任务入口。"""

from app.db.session import SessionLocal
from app.main import create_tables
from app.services.fund_data import sync_fund_navs


def main() -> None:
    create_tables()
    db = SessionLocal()
    try:
        result = sync_fund_navs(db)
        print(f"同步完成：{result}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
