"""同步资讯：抓取 RSS/可选 AKShare 数据源并去重入库。"""

from app.db.session import SessionLocal
from app.main import create_tables
from app.services.news import sync_news


def main() -> None:
    create_tables()
    db = SessionLocal()
    try:
        result = sync_news(db)
        print(f"资讯同步完成：{result}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
