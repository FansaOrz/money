"""本机常驻调度器：每日 20:30 更新净值，每小时更新资讯。

所有计划时间与日志时间戳均使用北京时间（Asia/Shanghai）。
fund_nav / indices / us_indices / news / holdings / paper / stock_daily 等任务的
每次执行都会写入 sync_runs 表，可通过 GET /api/sync/status 查询。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db.session import SessionLocal
from app.main import create_tables
from app.models import (
    CandidatePool,
    CandidatePoolMember,
    Instrument,
    PortfolioSnapshot,
    StockSyncState,
    SyncRun,
)
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
_stock_daily_thread: threading.Thread | None = None
_SCHEDULED_JOB_NAMES = {
    "candidate_pool_nav",
    "fund_catalog",
    "fund_nav",
    "fund_nav_early",
    "fund_nav_late",
    "fund_nav_startup",
    "holdings",
    "indices",
    "news",
    "paper",
    "stock_daily",
    "stock_market_close",
    "stock_paper",
    "stock_reference",
    "us_indices",
}


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
        latest_nav_date = result.get("latest_nav_date") or result.get("data_date")
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


def _sync_navs(*, job_name: str = "fund_nav") -> None:
    """优先同步用户实际持仓，避免全市场目录拖慢收益刷新。"""
    db = SessionLocal()
    try:
        with track_sync_run(db, job_name) as record:
            result = sync_fund_navs(db, held_only=True)
            record(**_extract_stats(result))
        logger.info("基金持仓净值同步完成（%s）：%s", job_name, result)
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


def _run_stock_paper() -> None:
    """A股数据同步完成后推进规则策略前向模拟；数据日不变时幂等跳过。"""
    try:
        from app.services.stock_paper import run_cycle
    except ImportError:
        return
    db = SessionLocal()
    try:
        with track_sync_run(db, "stock_paper") as record:
            result = run_cycle(db)
            record(**_extract_stats(result))
        logger.info("A股规则策略前向模拟完成：%s", result)
    except Exception:
        logger.exception("A股规则策略前向模拟失败")
        db.rollback()
    finally:
        db.close()


def _sync_stock_reference() -> None:
    """分批补齐当前指数成分的行业与财务数据，新成分加入后自动收敛。"""
    try:
        from app.services.research.stock_fundamentals import sync_reference_coverage
    except ImportError:
        return
    db = SessionLocal()
    try:
        with track_sync_run(db, "stock_reference") as record:
            result = sync_reference_coverage(
                db,
                batch_size=get_settings().scheduled_stock_reference_batch_size,
            )
            record(**_extract_stats(result))
        logger.info("A股行业/财务覆盖补齐完成：%s", result)
    except Exception:
        logger.exception("A股行业/财务覆盖补齐失败")
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
    """同步一小批 A 股日线；主要供子进程入口与单元测试直接调用。"""
    try:
        from app.services.research.stock_data import sync_stock_daily
    except ImportError:
        return
    db = SessionLocal()
    try:
        with track_sync_run(db, "stock_daily") as record:
            result = sync_stock_daily(
                db,
                limit=get_settings().scheduled_stock_sync_batch_size,
                fetch_qfq=True,
            )
            record(**_extract_stats(result))
        logger.info("A股日线同步完成：%s", result)
    except Exception:
        logger.exception("A股日线同步失败")
        db.rollback()
    finally:
        db.close()


def _sync_stock_market_close() -> bool:
    """同步收盘行情与 PE/PB；两者完整后模拟盘才可推进数据日。"""
    try:
        from app.services.research.stock_data import sync_stock_market_close
        from app.services.research.stock_fundamentals import (
            sync_market_valuations,
        )
    except ImportError:
        return False
    db = SessionLocal()
    try:
        with track_sync_run(db, "stock_market_close") as record:
            result = sync_stock_market_close(db)
            record(**_extract_stats(result))
        if result.get("status") in {"success", "partial"} and result.get("data_date"):
            valuation_result = sync_market_valuations(
                db, date.fromisoformat(str(result["data_date"]))
            )
            logger.info("A股全市场收盘估值完成：%s", valuation_result)
        logger.info("A股全市场收盘快照完成：%s", result)
        return result.get("status") in {"success", "partial"} and result.get("updated", 0) > 0
    except Exception:
        logger.exception("A股全市场收盘快照失败")
        db.rollback()
        return False
    finally:
        db.close()


def _mark_stock_daily_timeout(timeout_minutes: int) -> None:
    """子进程被超时终止后，把悬空的运行状态收口为失败/部分完成。"""
    db = SessionLocal()
    try:
        run = db.scalar(
            select(SyncRun)
            .where(SyncRun.job_name == "stock_daily", SyncRun.status == "running")
            .order_by(SyncRun.started_at.desc())
            .limit(1)
        )
        if run is not None:
            run.status = "failed"
            run.finished_at = now_cn()
            run.error = f"超过 {timeout_minutes} 分钟，已终止；下次从断点继续"
        state = db.get(StockSyncState, "daily")
        if state is not None and state.status == "running":
            state.status = "partial"
            state.finished_at = datetime.now(UTC)
            timeout_note = f"超过 {timeout_minutes} 分钟，已终止；下次从 {state.last_code or '断点'} 继续"
            state.detail = f"{state.detail}; {timeout_note}" if state.detail else timeout_note
        db.commit()
    except Exception:
        logger.exception("收口 A 股日线超时状态失败")
        db.rollback()
    finally:
        db.close()


def _recover_stale_stock_daily_run() -> None:
    """调度器重启时收口超过时限的旧 running 记录。"""
    timeout_minutes = max(1, get_settings().scheduled_stock_sync_timeout_minutes)
    db = SessionLocal()
    try:
        run = db.scalar(
            select(SyncRun)
            .where(SyncRun.job_name == "stock_daily", SyncRun.status == "running")
            .order_by(SyncRun.started_at.desc())
            .limit(1)
        )
        if run is None:
            return
        started_at = run.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=CN_TZ)
        if now_cn() - started_at < timedelta(minutes=timeout_minutes):
            return
    finally:
        db.close()
    _mark_stock_daily_timeout(timeout_minutes)


def _recover_interrupted_runs() -> None:
    """新调度器启动前关闭上一个进程遗留的 running 记录。"""
    db = SessionLocal()
    try:
        runs = db.scalars(
            select(SyncRun).where(
                SyncRun.job_name.in_(_SCHEDULED_JOB_NAMES),
                SyncRun.status == "running",
            )
        ).all()
        if not runs:
            return
        finished_at = now_cn()
        for run in runs:
            run.status = "failed"
            run.finished_at = finished_at
            run.error = run.error or "调度器重启，上一轮任务已中断"
        if any(run.job_name == "stock_daily" for run in runs):
            state = db.get(StockSyncState, "daily")
            if state is not None and state.status == "running":
                state.status = "partial"
                state.finished_at = datetime.now(UTC)
                note = "调度器重启，保留当前断点供下次续跑"
                state.detail = f"{state.detail}; {note}" if state.detail else note
        db.commit()
        logger.warning("已收口 %s 条上次调度器遗留的运行记录", len(runs))
    except Exception:
        logger.exception("收口调度器遗留状态失败")
        db.rollback()
    finally:
        db.close()


def _run_stock_daily_subprocess() -> None:
    """在线程中看护独立子进程，主调度循环不会被第三方接口阻塞。"""
    timeout_minutes = max(1, get_settings().scheduled_stock_sync_timeout_minutes)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "app.services.sync_stock_daily_job"],
            check=False,
            timeout=timeout_minutes * 60,
        )
        if completed.returncode != 0:
            logger.error("A股日线子进程异常退出，返回码：%s", completed.returncode)
    except subprocess.TimeoutExpired:
        logger.error("A股日线同步超过 %s 分钟，已终止，不再阻塞基金任务", timeout_minutes)
        _mark_stock_daily_timeout(timeout_minutes)
    except Exception:
        logger.exception("启动 A 股日线子进程失败")


def _launch_stock_daily() -> bool:
    """启动受限时保护的后台 A 股任务；已有任务时不重复启动。"""
    global _stock_daily_thread
    if _stock_daily_thread is not None and _stock_daily_thread.is_alive():
        logger.warning("A股日线后台任务仍在运行，本轮跳过")
        return False
    _stock_daily_thread = threading.Thread(
        target=_run_stock_daily_subprocess,
        name="stock-daily-supervisor",
        daemon=True,
    )
    _stock_daily_thread.start()
    logger.info("A股日线后台任务已启动，不阻塞基金净值调度")
    return True


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
        # 持仓基金分三轮补抓：先尽早拿已披露净值，再覆盖主披露时段和晚披露基金。
        "fund_nav_early": _next_daily(now, 19, 30),
        "fund_nav": _next_daily(now, 20, 30),
        "fund_nav_late": _next_daily(now, 22, 0),
        # 模拟交易在最后一轮持仓净值同步完成后运行，尽量覆盖晚披露基金。
        "paper": _next_daily(now, 22, 0),
        # 资讯每小时同步一次
        "news": _next_hourly(now, 17),
        # 美股指数收盘后（北京时间早晨）补一次
        "us_indices": _next_daily(now, 7, 30),
        # 季度披露数据每月 1 日 19:05 检查
        "holdings": _next_monthly(now, 1, 19, 5),
        # A股收盘后分批增量同步
        "stock_daily": _next_daily(now, 17, 5),
        # 收盘前维护当前指数成分的行业和财务覆盖，新成分会自动补齐。
        "stock_reference": _next_daily(now, 16, 10),
        # 等待 A 股同步子进程（最长 60 分钟）结束后再推进前向模拟。
        "stock_paper": _next_daily(now, 18, 30),
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
    _recover_interrupted_runs()
    _recover_stale_stock_daily_run()
    if _needs_initial_sync():
        _sync_navs(job_name="fund_nav_startup")
    _sync_news()
    _sync_indices()

    schedule = next_run_times()
    next_news = schedule["news"]
    next_market_close = schedule["indices"]
    next_nav_early = schedule["fund_nav_early"]
    next_nav = schedule["fund_nav"]
    next_nav_late = schedule["fund_nav_late"]
    next_us_indices = schedule["us_indices"]
    next_holdings = schedule["holdings"]
    next_stock_daily = schedule["stock_daily"]
    next_stock_reference = schedule["stock_reference"]
    next_stock_paper = schedule["stock_paper"]
    next_fund_catalog = schedule["fund_catalog"]
    next_candidate_pool_nav = schedule["candidate_pool_nav"]

    logger.info(
        "调度器已启动，持仓净值三轮同步（北京时间）：%s / %s / %s",
        next_nav_early.isoformat(),
        next_nav.isoformat(),
        next_nav_late.isoformat(),
    )
    while True:
        now = now_cn()
        if now >= next_news:
            _sync_news()
            next_news += timedelta(hours=1)
        if now >= next_market_close:
            _sync_indices()
            next_market_close += timedelta(days=1)
        if now >= next_nav_early:
            _sync_navs(job_name="fund_nav_early")
            next_nav_early += timedelta(days=1)
        if now >= next_nav:
            _sync_navs(job_name="fund_nav")
            next_nav += timedelta(days=1)
        if now >= next_nav_late:
            _sync_navs(job_name="fund_nav_late")
            _run_paper()
            next_nav_late += timedelta(days=1)
        if now >= next_us_indices:
            _sync_us_indices()
            next_us_indices += timedelta(days=1)
        if now >= next_holdings:
            _sync_holdings()
            next_holdings = _advance_month(next_holdings)
        if now >= next_stock_daily:
            _sync_stock_market_close()
            _launch_stock_daily()
            next_stock_daily += timedelta(days=1)
        if now >= next_stock_reference:
            _sync_stock_reference()
            next_stock_reference += timedelta(days=1)
        if now >= next_stock_paper:
            _run_stock_paper()
            next_stock_paper += timedelta(days=1)
        if now >= next_fund_catalog:
            _sync_fund_catalog()
            next_fund_catalog += timedelta(days=7)
        if now >= next_candidate_pool_nav:
            _sync_candidate_pool_nav()
            next_candidate_pool_nav += timedelta(days=1)
        time.sleep(30)


if __name__ == "__main__":
    main()
