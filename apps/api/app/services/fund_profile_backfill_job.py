"""主动管理型基金画像批量回填 CLI（断点可恢复）。

用法：
    python -m app.services.fund_profile_backfill_job                       # active 中从未抓取的 50 只
    python -m app.services.fund_profile_backfill_job --limit 20            # 自定义批量大小
    python -m app.services.fund_profile_backfill_job --codes 110022,000001 # 指定代码
    python -m app.services.fund_profile_backfill_job --force               # 忽略已有画像全部重抓
    python -m app.services.fund_profile_backfill_job --dry-run             # 只打印将处理的基金

说明：
- 选批范围：fund_catalog 中 active=True 的基金（--codes 时取交集）；
- 断点续传：默认只处理从未抓取（无 FundProfile 记录）或上次失败
  （last_error 非空）的基金；其余按 fetched_at 最旧优先补位，直到凑满 --limit；
  --force 忽略画像状态，按代码顺序重抓；
- 只调用 services.fund_profile.sync_profile（雪球失败自动回退东财），
  不触发持仓/行业等 composition 同步，也不进入每日调度；
- 对外源限速（--interval，默认 0.8 秒/只），单只失败记录后继续。
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.main import create_tables
from app.models import FundCatalogEntry, FundProfile
from app.services.fund_profile import sync_profile

DEFAULT_LIMIT = 50
DEFAULT_INTERVAL = 0.8


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="active 基金画像批量回填（断点可恢复）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="本批基金数量，默认 50")
    parser.add_argument("--codes", type=str, default="", help="逗号分隔的基金代码，仅处理这些基金")
    parser.add_argument("--force", action="store_true", help="忽略已有画像状态，全部重新抓取")
    parser.add_argument("--dry-run", action="store_true", help="只打印将处理的基金，不发起请求")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="每只间隔秒数（限速），默认 0.8")
    return parser.parse_args(argv)


def select_batch(
    db: Session,
    *,
    limit: int,
    codes: list[str] | None = None,
    force: bool = False,
) -> tuple[list[tuple[str, str, str]], int]:
    """从 active 目录选批，返回 (code, name, state) 列表与 active 总数。

    state 取值：never（从未抓取）/ error（上次失败）/ stale（最旧优先）/ force。
    """
    stmt = (
        select(FundCatalogEntry.code, FundCatalogEntry.name)
        .where(FundCatalogEntry.active.is_(True))
        .order_by(FundCatalogEntry.code)
    )
    if codes:
        stmt = stmt.where(FundCatalogEntry.code.in_(codes))
    entries = list(db.execute(stmt).all())
    total = len(entries)
    if limit <= 0:
        return [], total

    profiles = {
        row.code: row
        for row in db.scalars(
            select(FundProfile).where(FundProfile.code.in_([code for code, _ in entries]))
        ).all()
    } if entries else {}

    if force:
        return [(code, name, "force") for code, name in entries[:limit]], total

    never: list[tuple[str, str, str]] = []
    error: list[tuple[str, str, str]] = []
    stale: list[tuple[str, str, str, datetime]] = []
    for code, name in entries:
        record = profiles.get(code)
        if record is None or record.fetched_at is None:
            never.append((code, name, "never"))
        elif record.last_error:
            error.append((code, name, "error"))
        else:
            fetched = record.fetched_at.replace(tzinfo=None)
            stale.append((code, name, "stale", fetched))

    batch = never + error
    if len(batch) < limit:
        # 已成功画像的按最旧优先补位，凑满本批
        stale.sort(key=lambda item: item[3])
        batch.extend((code, name, state) for code, name, state, _ in stale)
    return batch[:limit], total


def run_batch(
    db: Session,
    batch: list[tuple[str, str, str]],
    *,
    interval: float = DEFAULT_INTERVAL,
    verbose: bool = True,
) -> dict[str, object]:
    """逐只同步画像，单只失败不中断，按 interval 限速。"""
    results: list[dict[str, object]] = []
    for index, (code, name, state) in enumerate(batch, start=1):
        if index > 1 and interval > 0:
            time.sleep(interval)
        try:
            profile, warnings = sync_profile(db, code)
            status = "failed" if profile.last_error else "ok"
            results.append(
                {
                    "code": code,
                    "name": name,
                    "state": state,
                    "status": status,
                    "source": profile.source,
                    "error": profile.last_error,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 单只失败继续
            db.rollback()
            status = "failed"
            results.append(
                {"code": code, "name": name, "state": state, "status": status, "source": None, "error": str(exc)}
            )
        if verbose:
            line = f"[{index}/{len(batch)}] {code} ({state}) -> {status}"
            if results[-1]["source"]:
                line += f" 来源={results[-1]['source']}"
            if results[-1]["error"]:
                line += f" 错误：{results[-1]['error']}"
            print(line)
    return {
        "total": len(results),
        "ok": sum(1 for item in results if item["status"] == "ok"),
        "failed": sum(1 for item in results if item["status"] == "failed"),
        "items": results,
    }


def main(argv: list[str] | None = None) -> dict[str, object]:
    args = parse_args(argv)
    codes = [code.strip() for code in args.codes.split(",") if code.strip()] or None
    create_tables()
    db = SessionLocal()
    try:
        batch, total = select_batch(db, limit=args.limit, codes=codes, force=args.force)
        scope = f"指定代码 {len(codes)} 个" if codes else "active 基金"
        print(
            f"画像回填范围：{scope}，active 总数 {total}，本批 {len(batch)} 只"
            f"（limit={args.limit}{'，force' if args.force else ''}）"
        )
        if args.dry_run:
            for code, name, state in batch:
                print(f"  [dry-run] {code} {name} 状态={state}")
            return {"dry_run": True, "active_total": total, "batch": len(batch)}

        summary = run_batch(db, batch, interval=args.interval)
        summary.update({"dry_run": False, "active_total": total})
        print("画像回填完成：" + json.dumps(summary, ensure_ascii=False, default=str))
        return summary
    finally:
        db.close()


if __name__ == "__main__":
    main()
