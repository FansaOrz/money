"""下载东方财富银行历史监管指标并登记为不可变 PIT 研究数据。

数据通过 AkShare 的 ``stock_financial_analysis_indicator_em`` 公共接口获取；
原始响应逐股保存为 Parquet，规范记录保留报告期、公告日、源文件哈希和
逐字段血缘。脚本幂等，可在正式实验冻结文件清单前重复执行。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import akshare as ak
from sqlalchemy import select

from app.config import get_settings
from app.db.session import SessionLocal
from app.models import StockIndustry
from app.services.eastmoney_financial_sector import normalize_bank_indicator
from app.services.quant_data_governance import register_financial_sector_metrics


def _market_code(code: str) -> str:
    normalized = code.split(".")[0].zfill(6)
    suffix = "SH" if normalized.startswith(("5", "6", "9")) else "SZ"
    return f"{normalized}.{suffix}"


def _default_codes() -> list[str]:
    with SessionLocal() as db:
        return sorted(
            set(
                db.scalars(
                    select(StockIndustry.code).where(
                        StockIndustry.industry_name == "银行"
                    )
                ).all()
            )
        )


def sync(codes: list[str], output_dir: Path, retries: int = 3) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = registered = failed = 0
    with SessionLocal() as db:
        for raw_code in codes:
            symbol = _market_code(raw_code)
            frame = None
            for attempt in range(retries):
                try:
                    frame = ak.stock_financial_analysis_indicator_em(
                        symbol=symbol,
                        indicator="按报告期",
                    )
                    break
                except Exception:  # noqa: BLE001 - 公开源需有限重试
                    if attempt + 1 < retries:
                        time.sleep(1.5 * (attempt + 1))
            if frame is None or frame.empty:
                failed += 1
                continue
            source_file = output_dir / f"{symbol}.parquet"
            frame.to_parquet(source_file, index=False)
            downloaded += 1
            for row in frame.to_dict(orient="records"):
                normalized = normalize_bank_indicator(row)
                if normalized is None:
                    continue
                report_period, available_at, metrics = normalized
                register_financial_sector_metrics(
                    db,
                    code=symbol,
                    report_period=report_period,
                    available_at=available_at,
                    metrics=metrics,
                    source="eastmoney",
                    source_file=source_file,
                )
                registered += 1
    return {
        "requested": len(codes),
        "downloaded": downloaded,
        "registered_rows": registered,
        "failed": failed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()
    root = Path(get_settings().research_data_dir)
    codes = args.codes or _default_codes()
    result = sync(
        codes,
        root / "tushare_snapshot" / "stocks" / "financial_sector_metric",
        retries=max(args.retries, 1),
    )
    print(result)
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
