"""同步基金最新披露重仓股和行业配置。"""

from app.db.session import SessionLocal
from app.main import create_tables
from app.services.fund_holdings import sync_fund_holdings


def main() -> None:
    create_tables()
    db = SessionLocal()
    try:
        print(sync_fund_holdings(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
