"""本机常驻调度器：每日 20:30 更新净值，每小时更新资讯。

所有计划时间与日志时间戳均使用北京时间（Asia/Shanghai）。
fund_nav / indices / us_indices / news / holdings / paper / stock_daily 等任务的
每次执行都会写入 sync_runs 表，可通过 GET /api/sync/status 查询。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import create_tables
from app.models import CandidatePool, CandidatePoolMember, Instrument, PortfolioSnapshot
from app.services.fund_data import backfill_fund_nav_history, sync_fund_navs
from app.services.sync_status import track_sync_run
from app.timezone import CN_TZ, now_cn


class _BeijingFormatter(logging.Formatter):
    """日志时间戳使用北京时间的 Formatter。"""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=CN_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S") + f",{int(record.msecs):03d}"


_handler = logging.StreamHandler()
_handler.setFormatter(_BeijingFormatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 结果统计提取：各任务返回 dict 结构不同，这里统一折算成 total/updated/failed
# ---------------------------------------------------------------------------


def _extract_stats(result: Any) -> dict[str, Any]:
    """从任务返回结果中提取 total/updated/failed/data_date 统计字段。"""
    if isinstance(result, dict):
        total = (
            result.get("total")
            or result.get("total_funds")
            or result.get("total_indices")
            or result.get("fetched")
            or 0
        )
        updated = (
            result.get("updated")
            or result.get("updated_indices")
            or result.get("succeeded")
            or result.get("inserted")
            or 0
        )
        failed = result.get("failed") or len(result.get("errors") or [])
        data_date = None
        latest_nav_date = result.get("latest_nav_date")
        if latest_nav_date:
            try:
                data_date = datetime.strptime(str(latest_nav_date), "%Y-%m-%d").date()
            except ValueError:
                data_date = None
        return {"total": total, "updated": updated, "failed": failed, "data_date": data_date}
    # paper 任务返回 PaperRunResponse（pydantic 模型）
    trade_count = int(getattr(result, "trade_count", 0) or 0)
    skipped = bool(getattr(result, "skipped", False))
    run_date = getattr(result, "run_date", None)
    data_date = None
    if run_date:
        try:
            data_date = datetime.strptime(str(run_date), "%Y-%m-%d").date()
        except ValueError:
            data_date = None
    return {"total": 1, "updated": 0 if skipped else trade_count, "failed": 0, "data_date": data_date}


# ---------------------------------------------------------------------------
# 各任务包装：开独立 Session，写 sync_runs 记录
# ---------------------------------------------------------------------------


def _sync_navs() -> None:
    db = SessionLocal()
    try:
        with track_sync_run(db, "fund_nav") as record:
            result = sync_fund_navs(db)
            record(**_extract_stats(result))
        logger.info("基金净值同步完成：%s", result)
    except Exception:
        logger.exception("基金净值同步失败")
        db.rollback()
    finally:
        db.close()


def _sync_indices() -> None:
    try:
        from app.services.index_data import sync_index_history
    except ImportError:
        return
    db = SessionLocal()
    try:
        with track_sync_run(db, "indices") as record:
            result = sync_index_history(db)
            record(**_extract_stats(result))
        logger.info("主要市场指数同步完成：%s", result)
    except Exception:
        logger.exception("主要市场指数同步失败")
        db.rollback()
    finally:
        db.close()


def _sync_us_indices() -> None:
    """美股指数独立任务（独立 sync_runs 记录，与 A/港指数分开跟踪）。"""
    try:
        from app.services.index_data import sync_index_history
    except ImportError:
        return
    db = SessionLocal()
    try:
        with track_sync_run(db, "us_indices") as record:
            result = sync_index_history(db, markets=["us"])
            record(**_extract_stats(result))
        logger.info("美股指数同步完成：%s", result)
    except Exception:
        logger.exception("美股指数同步失败")
        db.rollback()
    finally:
        db.close()


def _sync_holdings() -> None:
    """季度披露数据每月检查一次即可。"""
    try:
        from app.services.fund_holdings import sync_fund_holdings
    except ImportError:
        return
    db = SessionLocal()
    try:
        with track_sync_run(db, "holdings") as record:
            result = sync_fund_holdings(db)
            record(**_extract_stats(result))
        logger.info("基金成分同步完成：%s", result)
    except Exception:
        logger.exception("基金成分同步失败")
        db.rollback()
    finally:
        db.close()


def _run_paper() -> None:
    try:
        from app.services.paper import run_paper_cycle
    except ImportError:
        return
    db = SessionLocal()
    try:
        with track_sync_run(db, "paper") as record:
            result = run_paper_cycle(db)
            record(**_extract_stats(result))
        logger.info("模拟交易周期完成：%s", result)
    except Exception:
        logger.exception("模拟交易周期失败")
        db.rollback()
    finally:
        db.close()


def _sync_fund_catalog() -> None:
    try:
        from app.services.fund_catalog import sync_fund_catalog
    except ImportError:
        return
    db = SessionLocal()
    try:
        with track_sync_run(db, "fund_catalog") as record:
            result = sync_fund_catalog(db, refresh_active=True, mark_inactive=True)
            record(**_extract_stats(result))
        logger.info("全市场基金目录同步完成：%s", result)
    except Exception:
        logger.exception("全市场基金目录同步失败")
        db.rollback()
    finally:
        db.close()


def _sync_candidate_pool_nav(batch_size: int = 25) -> None:
    """低优先级补齐最新候选池历史净值，每次只处理一小批未就绪成员。"""
    from app.services import candidate_pool as candidate_pool_service

    db = SessionLocal()
    try:
        pool = db.scalar(select(CandidatePool).order_by(CandidatePool.created_at.desc()).limit(1))
        if pool is None:
            return
        rows = db.execute(
            select(Instrument, CandidatePoolMember)
            .join(CandidatePoolMember, CandidatePoolMember.code == Instrument.code)
            .where(
                CandidatePoolMember.pool_id == pool.id,
                CandidatePoolMember.status == "active",
                CandidatePoolMember.nav_ready.is_(False),
            )
            .order_by(CandidatePoolMember.rank)
            .limit(batch_size)
        ).all()
        with track_sync_run(db, "candidate_pool_nav") as record:
            results = [
                backfill_fund_nav_history(db, instrument, years=5, resume=True, use_fallback=True)
                for instrument, _member in rows
            ]
            candidate_pool_service.refresh_member_nav_status(db, pool.id)
            failed = sum(1 for item in results if item["status"] == "failed")
            updated = sum(1 for item in results if item["status"] in {"complete", "partial", "skipped"})
            record(total=len(rows), updated=updated, failed=failed)
        logger.info("候选池 #%s 历史净值批量回填完成：%s", pool.id, results)
    except Exception:
        logger.exception("候选池历史净值批量回填失败")
        db.rollback()
    finally:
        db.close()


def _sync_stock_daily() -> None:
    try:
        from app.services.research.stock_data import sync_stock_daily
    except ImportError:
        return
    db = SessionLocal()
    try:
        with track_sync_run(db, "stock_daily") as record:
            # 断点续传：批次大小取 research_sync_batch_size，消费 stock_sync_state.last_code
            result = sync_stock_daily(db, fetch_qfq=True)
            record(**_extract_stats(result))
        logger.info("A股日线同步完成：%s", result)
    except Exception:
        logger.exception("A股日线同步失败")
        db.rollback()
    finally:
        db.close()


def _sync_news() -> None:
    try:
        from app.services.news import sync_news
        from app.services.fund_news_analysis import analyze_pending_news
    except ImportError:
        return
    db = SessionLocal()
    try:
        with track_sync_run(db, "news") as record:
            result = sync_news(db)
            record(**_extract_stats(result))
        logger.info("资讯同步完成：%s", result)
        analysis = analyze_pending_news(db)
        logger.info("资讯事件分析完成：%s", analysis)
    except Exception:
        logger.exception("资讯同步失败")
        db.rollback()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 下次计划时间计算（均为北京时间 aware datetime）
# ---------------------------------------------------------------------------


def _next_daily(now: datetime, hour: int, minute: int) -> datetime:
    """返回今天 hour:minute 的北京时间；若已过则返回明天。"""
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return nxt


def _next_hourly(now: datetime, minute: int = 17) -> datetime:
    """返回本小时 minute 分的北京时间；若已过则返回下一小时。"""
    nxt = now.replace(minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(hours=1)
    return nxt


def _next_monthly(now: datetime, day: int, hour: int, minute: int) -> datetime:
    """返回本月 day 日 hour:minute 的北京时间；若已过则返回下月。"""
    nxt = now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        month = nxt.month + 1
        year = nxt.year + (month - 1) // 12
        nxt = nxt.replace(year=year, month=(month - 1) % 12 + 1)
    return nxt


def _advance_month(nxt: datetime) -> datetime:
    """将月度计划时间推进一个月。"""
    month = nxt.month + 1
    year = nxt.year + (month - 1) // 12
    return nxt.replace(year=year, month=(month - 1) % 12 + 1)


def _next_weekly(now: datetime, weekday: int, hour: int, minute: int) -> datetime:
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    nxt += timedelta(days=(weekday - nxt.weekday()) % 7)
    if nxt <= now:
        nxt += timedelta(days=7)
    return nxt


def next_run_times(now: datetime | None = None) -> dict[str, datetime]:
    """返回各任务的下次计划运行时间（北京时间 aware datetime）。"""
    now = now or now_cn()
    return {
        # A股 15:00 收盘，17:30 先同步 A/港指数
        "indices": _next_daily(now, 17, 30),
        # 普通公募基金净值通常 18:00 后陆续公布，20:30 同步更稳妥
        "fund_nav": _next_daily(now, 20, 30),
        # 模拟交易跟随净值同步
        "paper": _next_daily(now, 20, 30),
        # 资讯每小时同步一次
        "news": _next_hourly(now, 17),
        # 美股指数收盘后（北京时间早晨）补一次
        "us_indices": _next_daily(now, 7, 30),
        # 季度披露数据每月 1 日 19:05 检查
        "holdings": _next_monthly(now, 1, 19, 5),
        # A股收盘后分批增量同步
        "stock_daily": _next_daily(now, 17, 5),
        # 全市场基金目录每周日凌晨同步
        "fund_catalog": _next_weekly(now, 6, 2, 30),
        # 候选池历史净值每日低优先级分批回填
        "candidate_pool_nav": _next_daily(now, 3, 23),
    }


def _needs_initial_sync() -> bool:
    db = SessionLocal()
    try:
        latest = db.scalar(
            select(PortfolioSnapshot.snapshot_date)
            .order_by(PortfolioSnapshot.snapshot_date.desc())
            .limit(1)
        )
        return latest is None or latest < now_cn().date()
    finally:
        db.close()


def main() -> None:
    create_tables()
    if _needs_initial_sync():
        _sync_navs()
    _sync_news()
    _sync_indices()

    schedule = next_run_times()
    next_news = schedule["news"]
    next_market_close = schedule["indices"]
    next_nav = schedule["fund_nav"]
    next_us_indices = schedule["us_indices"]
    next_holdings = schedule["holdings"]
    next_stock_daily = schedule["stock_daily"]
    next_fund_catalog = schedule["fund_catalog"]
    next_candidate_pool_nav = schedule["candidate_pool_nav"]

    logger.info("调度器已启动，下次净值同步（北京时间）：%s", next_nav.isoformat())
    while True:
        now = now_cn()
        if now >= next_news:
            _sync_news()
            next_news += timedelta(hours=1)
        if now >= next_market_close:
            _sync_indices()
            next_market_close += timedelta(days=1)
        if now >= next_nav:
            _sync_navs()
            _run_paper()
            next_nav += timedelta(days=1)
        if now >= next_us_indices:
            _sync_us_indices()
            next_us_indices += timedelta(days=1)
        if now >= next_holdings:
            _sync_holdings()
            next_holdings = _advance_month(next_holdings)
        if now >= next_stock_daily:
            _sync_stock_daily()
            next_stock_daily += timedelta(days=1)
        if now >= next_fund_catalog:
            _sync_fund_catalog()
            next_fund_catalog += timedelta(days=7)
        if now >= next_candidate_pool_nav:
            _sync_candidate_pool_nav()
            next_candidate_pool_nav += timedelta(days=1)
        time.sleep(30)


if __name__ == "__main__":
    main()
