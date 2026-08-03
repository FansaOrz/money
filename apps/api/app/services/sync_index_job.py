"""同步主要市场指数近期日线行情（上证/沪深300/恒生/恒生科技/标普500/纳指）。"""

from app.db.session import SessionLocal
from app.main import create_tables
from app.services.index_data import sync_index_history


def main() -> None:
    create_tables()
    db = SessionLocal()
    try:
        result = sync_index_history(db)
        print(f"指数行情同步完成：{result}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
