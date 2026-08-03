"""候选池基金季度重仓/行业披露回填任务（可恢复批处理）。

用法：
    python -m app.services.fund_holdings_backfill_job                       # 最新候选池，当年，20 只
    python -m app.services.fund_holdings_backfill_job --year 2025 --limit 50
    python -m app.services.fund_holdings_backfill_job --pool-id 3 --codes 110022,968092
    python -m app.services.fund_holdings_backfill_job --batch-size 20 --batch 1
    python -m app.services.fund_holdings_backfill_job --dry-run

说明：
- 断点续传：以 fund_holdings_sync_status 表按 (instrument_id, year) 记录状态，
  complete 自动跳过，failed / partial / 未跑过的下轮优先重试；
- --batch-size/--batch 用于分批执行（配合 cron 多次调用），批次内仍按状态跳过；
- 历史多年度回填：多次 --year 运行（如 2023/2024/2025 各跑一次）；
- 该任务为手动/低频触发，不进入每日调度。
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import create_tables
from app.models import CandidatePool, FundHoldingsSyncStatus
from app.services.fund_holdings_backfill import (
    DEFAULT_LIMIT,
    _upsert_status,
    backfill_one,
    latest_pool_id,
    select_pending,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="候选池基金季度重仓/行业披露回填（可恢复）")
    parser.add_argument("--year", type=int, default=date.today().year, help="披露年度，默认当年")
    parser.add_argument("--pool-id", type=int, default=None, help="候选池 ID，默认最新一期")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="本次最多处理基金数，默认 20")
    parser.add_argument("--batch-size", type=int, default=0, help="每批基金数量，0 表示不分批")
    parser.add_argument("--batch", type=int, default=0, help="批次序号（从 0 开始）")
    parser.add_argument("--codes", type=str, default="", help="逗号分隔的基金代码，仅回填这些基金")
    parser.add_argument("--dry-run", action="store_true", help="只打印将处理的基金与状态，不发起请求")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    create_tables()
    db = SessionLocal()
    try:
        pool_id = args.pool_id if args.pool_id is not None else latest_pool_id(db)
        if pool_id is None:
            raise SystemExit("尚无候选池，请先运行候选池构建（build_candidate_pool）")
        pool = db.get(CandidatePool, pool_id)
        if pool is None:
            raise SystemExit(f"候选池不存在：{pool_id}")

        codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
        # 先取出全部待处理（不套 limit），再按批次/数量截断，保证批次间不重不漏
        pending_all = select_pending(db, args.year, pool_id=pool_id, codes=codes)
        total_pending = len(pending_all)
        if args.batch_size > 0:
            start = args.batch * args.batch_size
            pending_all = pending_all[start : start + args.batch_size]
        instruments = pending_all[: args.limit] if args.limit > 0 else pending_all

        print(
            f"回填范围：候选池 #{pool_id}（{pool.name}），年度 {args.year}；"
            f"待处理 {total_pending}，本次 {len(instruments)} 只"
            + (f"（批次 {args.batch}）" if args.batch_size > 0 else "")
        )

        if args.dry_run:
            statuses = {
                (row.instrument_id, row.year): row
                for row in db.scalars(select(FundHoldingsSyncStatus)).all()
            }
            for instrument in instruments:
                record = statuses.get((instrument.id, args.year))
                state = record.status if record else "never"
                print(f"  [dry-run] {instrument.code} {instrument.name} 状态={state}")
            return

        results = []
        for index, instrument in enumerate(instruments, start=1):
            try:
                result = backfill_one(db, instrument, args.year)
            except Exception as exc:  # fetch 阶段异常：记录后继续
                db.rollback()
                _upsert_status(
                    db,
                    instrument.id,
                    args.year,
                    status="failed",
                    holding_rows=0,
                    industry_rows=0,
                    error=str(exc)[:500],
                )
                db.commit()
                result = {
                    "code": instrument.code,
                    "status": "failed",
                    "holding_rows": 0,
                    "industry_rows": 0,
                    "error": str(exc)[:500],
                }
            results.append(result)
            print(
                f"[{index}/{len(instruments)}] {result['code']} -> {result['status']} "
                f"重仓 {result['holding_rows']} 行业 {result['industry_rows']}"
                + (f" 错误：{result['error']}" if result["error"] else "")
            )
        summary = {
            "year": args.year,
            "pool_id": pool_id,
            "total_pending": total_pending,
            "processed": len(results),
            "complete": sum(1 for r in results if r["status"] == "complete"),
            "partial": sum(1 for r in results if r["status"] == "partial"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "holding_rows": sum(r["holding_rows"] for r in results),
            "industry_rows": sum(r["industry_rows"] for r in results),
            "failures": [r for r in results if r["status"] == "failed"],
        }
        print("回填完成：" + json.dumps(summary, ensure_ascii=False, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
