"""本机初始化：导入 PDF 并同步基金净值。"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from app.db.session import SessionLocal
from app.main import create_tables
from app.config import get_settings
from app.services.fund_data import sync_fund_nav_history, sync_fund_navs
from app.services.importer import commit_import, create_preview

ROOT = Path("/root/Src/money")
PDF_DIR = ROOT / "tmp"


class LocalUpload:
    """把本地文件包装成 UploadFile 需要的最小接口。"""

    def __init__(self, path: Path):
        self.filename = path.name
        self.file = path.open("rb")


def bootstrap() -> None:
    if get_settings().auto_create_tables:
        create_tables()
    db = SessionLocal()
    try:
        for path in sorted(PDF_DIR.glob("*.pdf"), reverse=True):
            upload = LocalUpload(path)
            try:
                preview = create_preview(db, upload)  # type: ignore[arg-type]
            except HTTPException as exc:
                print(f"跳过 {path.name}：{exc.detail}")
                continue
            finally:
                upload.file.close()
            result = commit_import(db, int(preview.import_id))
            print(f"已导入 {path.name}：持仓 {result[0]} 条，交易 {result[1]} 条")

        history_result = sync_fund_nav_history(db, days=365)
        print(f"历史净值同步：{history_result}")
        result = sync_fund_navs(db)
        print(f"最新净值同步：{result}")
    finally:
        db.close()


if __name__ == "__main__":
    bootstrap()
