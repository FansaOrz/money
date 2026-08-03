"""基金历史净值断点回填任务（默认 5 年）。

用法：
    python -m app.services.sync_backfill_job                     # 全部基金，5 年，断点续传
    python -m app.services.sync_backfill_job --years 5 --batch-size 20 --batch 0
    python -m app.services.sync_backfill_job --codes 110022,968092
    python -m app.services.sync_backfill_job --no-resume --no-fallback

说明：
- 断点续传：已完成（complete 且 earliest<=目标起点）的基金会自动跳过；
  partial 状态的基金从上轮记录的 next_end_date 继续向前回填；
- --batch-size/--batch 用于分批执行（配合 cron 多次调用），批次内仍按断点跳过；
- 该任务设计为手动或低频触发，不进入每日调度。
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import create_tables
from app.models import CandidatePool, CandidatePoolMember, Instrument, NavSyncStatus
from app.services import candidate_pool as candidate_pool_service
from app.services.fund_data import MAX_YEARS, backfill_fund_nav_history


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="基金历史净值断点回填（默认 5 年）")
    parser.add_argument("--years", type=int, default=MAX_YEARS, help="回填年限，上限 5 年")
    parser.add_argument("--batch-size", type=int, default=0, help="每批基金数量，0 表示不分批")
    parser.add_argument("--batch", type=int, default=0, help="批次序号（从 0 开始）")
    parser.add_argument("--codes", type=str, default="", help="逗号分隔的基金代码，仅回填这些基金")
    parser.add_argument("--pool-id", type=int, default=None, help="只回填指定候选池的 active 成员")
    parser.add_argument("--no-resume", action="store_true", help="忽略断点，从头回填")
    parser.add_argument("--no-fallback", action="store_true", help="禁用 AKShare 回退")
    parser.add_argument("--dry-run", action="store_true", help="只打印将处理的基金，不发起请求")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    years = min(max(args.years, 1), MAX_YEARS)
    create_tables()
    db = SessionLocal()
    try:
        stmt = select(Instrument).order_by(Instrument.code)
        wanted: list[str] | None = None
        if args.codes:
            wanted = [code.strip() for code in args.codes.split(",") if code.strip()]
        if args.pool_id is not None:
            pool = db.get(CandidatePool, args.pool_id)
            if pool is None:
                raise SystemExit(f"候选池不存在：{args.pool_id}")
            pool_codes = list(
                db.scalars(
                    select(CandidatePoolMember.code)
                    .where(
                        CandidatePoolMember.pool_id == args.pool_id,
                        CandidatePoolMember.status == "active",
                    )
                    .order_by(CandidatePoolMember.rank)
                ).all()
            )
            wanted = (
                [code for code in wanted if code in set(pool_codes)]
                if wanted is not None
                else pool_codes
            )
        if wanted is not None:
            stmt = stmt.where(Instrument.code.in_(wanted))
        instruments = db.scalars(stmt).all()
        total = len(instruments)
        if args.batch_size > 0:
            start = args.batch * args.batch_size
            instruments = instruments[start : start + args.batch_size]
        scope = f"候选池 #{args.pool_id}" if args.pool_id is not None else "基金标的"
        print(
            f"回填范围：{years} 年；{scope}总数 {total}，"
            f"本批 {len(instruments)} 只（批次 {args.batch}）"
        )

        if args.dry_run:
            statuses = {
                row.instrument_id: row
                for row in db.scalars(select(NavSyncStatus)).all()
            }
            for instrument in instruments:
                record = statuses.get(instrument.id)
                state = record.status if record else "never"
                print(f"  [dry-run] {instrument.code} {instrument.name} 状态={state}")
            return

        results = []
        for index, instrument in enumerate(instruments, start=1):
            result = backfill_fund_nav_history(
                db,
                instrument,
                years=years,
                resume=not args.no_resume,
                use_fallback=not args.no_fallback,
            )
            results.append(result)
            print(
                f"[{index}/{len(instruments)}] {result['code']} -> {result['status']} "
                f"新增 {result['inserted']} 更新 {result['updated']}"
                + (f" 错误：{result['error']}" if result["error"] else "")
            )
        refreshed = 0
        if args.pool_id is not None and not args.dry_run:
            refreshed = candidate_pool_service.refresh_member_nav_status(db, args.pool_id)
        summary = {
            "years": years,
            "pool_id": args.pool_id,
            "total": len(results),
            "complete": sum(1 for r in results if r["status"] == "complete"),
            "partial": sum(1 for r in results if r["status"] == "partial"),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "inserted": sum(r["inserted"] for r in results),
            "updated": sum(r["updated"] for r in results),
            "pool_members_refreshed": refreshed,
            "failures": [r for r in results if r["status"] == "failed"],
        }
        print("回填完成：" + json.dumps(summary, ensure_ascii=False, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
