"""基本面数据：财务指标、披露日程、估值、历史名称/ST。

共同约定：
- 全部幂等 upsert（唯一约束见 models/research.py）；
- 单只股票失败记 errors 继续，不中断批量任务；
- available_at 语义：
  - 财务指标/估值/名称：本地入库时间（真实披露时间不可得时的近似）；
  - 披露日程：实际披露日 15:00（北京时间）视为可用，无披露日则留空。
- 数据缺失就缺失，不补齐；coverage 由 /api/stocks/data/status 暴露。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime, time
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    StockFinancialIndicator,
    StockIndustry,
    StockNameHistory,
    StockMaster,
    StockReportDisclosure,
    StockValuation,
)
from app.services.research import ak_fetch
from app.services.research.stock_data import (
    _begin_task,
    _cninfo_market,
    _final_status,
    _finish_task,
    _progress_task,
    _to_date,
    _to_float,
)

logger = logging.getLogger(__name__)

# 北京时间收盘时刻：披露日 15:00 之后数据视为可用
_DISCLOSURE_AVAILABLE_TIME = time(15, 0)

# 百度估值支持的指标 -> 内部标识
VALUATION_INDICATORS: dict[str, str] = {
    "总市值": "total_mv",
    "市盈率(TTM)": "pe_ttm",
    "市净率": "pb",
    "市销率(TTM)": "ps_ttm",
    "股息率": "dividend_yield",
}


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# 财务指标
# ---------------------------------------------------------------------------

def sync_financial_indicators(db: Session, codes: list[str]) -> dict[str, Any]:
    """批量同步财务分析指标（新浪）。coverage 取决于数据源历史深度。"""
    state = _begin_task(db, "financial")
    updated = 0
    failed = 0
    errors: list[str] = []
    total_rows = 0
    total_codes = len(codes)
    for idx, code in enumerate(codes, start=1):
        frame = ak_fetch.fetch_financial_indicator(code)
        if frame is None:
            failed += 1
            errors.append(f"{code}: 数据源不可用")
        else:
            rows = _upsert_financial_frame(db, code, frame)
            total_rows += rows
            updated += 1
        db.commit()
        _progress_task(
            db, state, processed=idx, total=total_codes,
            updated=updated, failed=failed, last_code=code,
        )
    status = _final_status(updated, failed, processed=total_codes)
    _finish_task(
        db, state, total=total_codes, updated=updated, failed=failed,
        last_code=codes[-1] if codes else None,
        detail="; ".join(errors[:20]) or None, status=status,
    )
    return {
        "task": "financial",
        "status": status,
        "total": total_codes,
        "updated": updated,
        "failed": failed,
        "rows": total_rows,
        "errors": errors,
    }


def _json_safe(value: Any) -> Any:
    """把 DataFrame 单元格转成 JSON 可序列化值；NaN/NaT -> None。"""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):  # numpy 标量 -> python 标量
        try:
            return value.item()
        except (ValueError, AttributeError):
            pass
    return value


def _upsert_financial_frame(db: Session, code: str, frame: pd.DataFrame) -> int:
    """归一化并 upsert 财务指标。报告期列兼容 日期/report_date。"""
    count = 0
    available_at = _now()
    for _, record in frame.iterrows():
        report_date = _to_date(record.get("日期") or record.get("report_date"))
        if report_date is None:
            continue
        payload: dict[str, Any] = {
            str(key): _json_safe(value) for key, value in record.items()
        }
        row = db.scalar(
            select(StockFinancialIndicator).where(
                StockFinancialIndicator.code == code,
                StockFinancialIndicator.report_date == report_date,
            )
        )
        if row is None:
            row = StockFinancialIndicator(code=code, report_date=report_date)
            db.add(row)
        row.eps = _to_float(record.get("摊薄每股收益(元)") or record.get("eps"))
        row.roe = _to_float(record.get("净资产收益率(%)") or record.get("roe"))
        row.payload = json.dumps(payload, ensure_ascii=False, default=str)
        row.available_at = row.available_at or available_at
        count += 1
    return count


# ---------------------------------------------------------------------------
# 披露日程
# ---------------------------------------------------------------------------

def _normalize_period(period: str) -> tuple[str, date] | None:
    """把披露报告期参数归一化为 (巨潮接口 period 文案, 报告期日期)。

    兼容 "20231231" / "2023-12-31" / "2023年报" / "2023一季" / "2023半年报" / "2023三季"。
    仅支持 03-31/06-30/09-30/12-31 四个法定报告期。
    """
    text = str(period).strip()
    if not text:
        return None
    report_date = _to_date(text.replace("-", ""))
    if report_date is not None:
        month_day = (report_date.month, report_date.day)
        label_map = {
            (3, 31): "一季",
            (6, 30): "半年报",
            (9, 30): "三季",
            (12, 31): "年报",
        }
        label = label_map.get(month_day)
        if label is None:
            return None
        return f"{report_date.year}{label}", report_date
    match = re.fullmatch(r"(\d{4})\s*(一季|半年报|三季|年报)", text)
    if match is None:
        return None
    year = int(match.group(1))
    date_map = {
        "一季": date(year, 3, 31),
        "半年报": date(year, 6, 30),
        "三季": date(year, 9, 30),
        "年报": date(year, 12, 31),
    }
    return f"{year}{match.group(2)}", date_map[match.group(2)]


def _iter_disclosure_markets(db: Session) -> list[tuple[str, set[str] | None]]:
    """返回 (巨潮 market 分区, 该分区目标代码集合或 None=不过滤) 列表。

    stock_master 有数据时按分区归组（北交所独立分区，92 号段亦归入）；
    master 为空时退化为单次全市场快照（沪深京），按 master 后续可再精细分配。
    """
    rows = db.execute(select(StockMaster.code, StockMaster.exchange)).all()
    groups: dict[str, set[str]] = {}
    for code, exchange in rows:
        market = {"sh": "沪市", "sz": "深市", "bj": "北交所"}.get(exchange or "")
        if market is None:
            market = _cninfo_market(code)
        if market is not None:
            groups.setdefault(market, set()).add(code)
    if not groups:
        return [("沪深京", None)]
    return sorted(groups.items())


def sync_report_disclosure(
    db: Session, codes: list[str] | None = None, periods: list[str] | None = None
) -> dict[str, Any]:
    """同步财报披露日程（当前 akshare 全市场接口适配版）。

    ak.stock_report_disclosure 现为 (market, period) 全市场快照接口：每个
    (market 分区, period) 只需抓取一次，返回该分区全部股票的披露日程，
    再按 code 分配入库。因此本函数不再按个股循环抓全市场数据。

    - periods 缺省为当前年份的四个报告期；
    - codes 为 None 时同步 master 覆盖的全部市场分区（通常即全市场）；
    - 每个 (market, period) 抓取失败只影响该分区计数，不中断整体。
    """
    state = _begin_task(db, "disclosure")

    normalized: list[tuple[str, date]] = []
    errors: list[str] = []
    for raw in periods or []:
        parsed = _normalize_period(raw)
        if parsed is None:
            errors.append(f"{raw}: 无法识别的报告期（支持 20231231/2023年报 等）")
        else:
            normalized.append(parsed)
    if not normalized:
        year = datetime.now().year
        normalized = [
            (f"{year}一季", date(year, 3, 31)),
            (f"{year}半年报", date(year, 6, 30)),
            (f"{year}三季", date(year, 9, 30)),
            (f"{year}年报", date(year, 12, 31)),
        ]
    # 去重保序
    normalized = list(dict.fromkeys(normalized))

    wanted: set[str] | None = None
    if codes is not None:
        wanted = {str(code).strip().zfill(6) for code in codes if str(code).strip()}

    market_groups = _iter_disclosure_markets(db)
    if wanted is not None:
        market_groups = [
            (market, group & wanted if group is not None else wanted)
            for market, group in market_groups
        ]
        market_groups = [(m, g) for m, g in market_groups if g]
        if not market_groups:
            # 目标代码不在 master 分区中：按全市场快照过滤入库
            market_groups = [("沪深京", wanted)]

    updated = 0  # 成功落库的 (market, period) 组合数
    failed = 0
    total_rows = 0
    stocks_seen: set[str] = set()
    combos = [(market, group, period, report_date)
              for market, group in market_groups for period, report_date in normalized]
    for idx, (market, group, period_label, report_date) in enumerate(combos, start=1):
        frame = ak_fetch.fetch_report_disclosure(market, period_label)
        if frame is None:
            failed += 1
            errors.append(f"{market}/{period_label}: 数据源不可用")
        else:
            rows, matched = _upsert_disclosure_frame(db, report_date, frame, group)
            total_rows += rows
            stocks_seen |= matched
            updated += 1
            db.commit()
        _progress_task(
            db, state, processed=idx, total=len(combos),
            updated=updated, failed=failed,
        )
    status = _final_status(updated, failed, processed=len(combos))
    _finish_task(
        db, state, total=len(combos), updated=updated, failed=failed,
        detail="; ".join(errors[:20]) or None, status=status,
    )
    return {
        "task": "disclosure",
        "status": status,
        "total": len(combos),
        "updated": updated,
        "failed": failed,
        "rows": total_rows,
        "stocks": len(stocks_seen),
        "errors": errors,
    }


def _upsert_disclosure_frame(
    db: Session,
    report_date: date,
    frame: pd.DataFrame,
    wanted: set[str] | None = None,
) -> tuple[int, set[str]]:
    """把巨潮全市场披露快照按 code 分配 upsert，返回 (行数, 命中的股票代码集)。

    列名兼容 ak.stock_report_disclosure 当前中文表头
    （股票代码/首次预约/初次变更/二次变更/三次变更/实际披露）。
    """
    count = 0
    matched: set[str] = set()
    for _, record in frame.iterrows():
        raw_code = record.get("股票代码") or record.get("code")
        code = str(raw_code).strip().zfill(6) if raw_code is not None else ""
        if len(code) != 6 or not code.isdigit():
            continue
        if wanted is not None and code not in wanted:
            continue
        # 报告期以请求参数为准（快照按报告期整批抓取，同批一致）；
        # 接口若自带“报告期”列且可解析，则尊重数据本身
        row_report_date = _to_date(record.get("报告期")) or report_date
        disclosure_date = _to_date(
            record.get("实际披露") or record.get("实际披露时间") or record.get("披露时间")
        )
        # 预计披露日取首次预约；若发生过变更以最后一次变更为准
        estimate_date = (
            _to_date(record.get("三次变更"))
            or _to_date(record.get("二次变更"))
            or _to_date(record.get("初次变更"))
            or _to_date(record.get("首次预约") or record.get("预计披露时间"))
        )
        row = db.scalar(
            select(StockReportDisclosure).where(
                StockReportDisclosure.code == code,
                StockReportDisclosure.report_date == row_report_date,
            )
        )
        if row is None:
            row = StockReportDisclosure(code=code, report_date=row_report_date)
            db.add(row)
        row.disclosure_date = disclosure_date
        row.estimate_date = estimate_date
        row.available_at = (
            datetime.combine(disclosure_date, _DISCLOSURE_AVAILABLE_TIME)
            if disclosure_date
            else None
        )
        matched.add(code)
        count += 1
    return count, matched


# ---------------------------------------------------------------------------
# 估值
# ---------------------------------------------------------------------------

def sync_valuations(
    db: Session,
    codes: list[str],
    indicators: list[str] | None = None,
    period: str = "近一年",
) -> dict[str, Any]:
    """批量同步百度估值。indicators 缺省为 VALUATION_INDICATORS 全部中文名。"""
    state = _begin_task(db, "valuation")
    names = indicators or list(VALUATION_INDICATORS)
    updated = 0
    failed = 0
    errors: list[str] = []
    total_rows = 0
    total_codes = len(codes)
    for idx, code in enumerate(codes, start=1):
        code_failed = False
        rows_before = total_rows
        for name in names:
            frame = ak_fetch.fetch_valuation_baidu(code, name, period)
            if frame is None:
                code_failed = True
                continue
            total_rows += _upsert_valuation_frame(db, code, VALUATION_INDICATORS.get(name, name), frame)
        if code_failed and total_rows == rows_before:
            failed += 1
            errors.append(f"{code}: 全部估值指标抓取失败")
        elif code_failed:
            # 部分指标可用时保留数据并标为 partial，而不是把整只股票算作全败。
            updated += 1
            failed += 1
            errors.append(f"{code}: 部分估值指标抓取失败")
        else:
            updated += 1
        db.commit()
        _progress_task(
            db, state, processed=idx, total=total_codes,
            updated=updated, failed=failed, last_code=code,
        )
    status = _final_status(updated, failed, processed=total_codes)
    _finish_task(
        db, state, total=total_codes, updated=updated, failed=failed,
        last_code=codes[-1] if codes else None,
        detail="; ".join(errors[:20]) or None, status=status,
    )
    return {
        "task": "valuation",
        "status": status,
        "total": total_codes,
        "updated": updated,
        "failed": failed,
        "rows": total_rows,
        "errors": errors,
    }


def _upsert_valuation_frame(db: Session, code: str, indicator: str, frame: pd.DataFrame) -> int:
    """归一化并 upsert 估值序列（列名兼容 date/value 中文表头）。"""
    count = 0
    existing_dates = set(
        db.scalars(
            select(StockValuation.trade_date).where(
                StockValuation.code == code,
                StockValuation.indicator == indicator,
            )
        ).all()
    )
    for _, record in frame.iterrows():
        trade_date = _to_date(record.get("date") or record.get("日期"))
        value = _to_float(record.get("value") or record.get("值"))
        if trade_date is None:
            continue
        if trade_date in existing_dates:
            row = db.scalar(
                select(StockValuation).where(
                    StockValuation.code == code,
                    StockValuation.trade_date == trade_date,
                    StockValuation.indicator == indicator,
                )
            )
            if row is not None:
                row.value = value
                count += 1
            continue
        db.add(
            StockValuation(code=code, trade_date=trade_date, indicator=indicator, value=value)
        )
        existing_dates.add(trade_date)
        count += 1
    return count


# ---------------------------------------------------------------------------
# 历史名称 / ST
# ---------------------------------------------------------------------------

_ST_TOKENS = ("ST", "*ST")


def _is_st_name(name: str) -> bool:
    upper = name.upper()
    return any(token in upper for token in _ST_TOKENS)


def sync_name_history(db: Session, codes: list[str]) -> dict[str, Any]:
    """批量同步历史名称/ST 变更（ak.stock_info_change_name，含区间解析）。"""
    state = _begin_task(db, "name_history")
    updated = 0
    failed = 0
    errors: list[str] = []
    total_rows = 0
    total_codes = len(codes)
    for idx, code in enumerate(codes, start=1):
        frame = ak_fetch.fetch_name_change_hist(code)
        if frame is None:
            failed += 1
            errors.append(f"{code}: 数据源不可用或无变更记录")
        else:
            total_rows += _upsert_name_frame(db, code, frame)
            updated += 1
        db.commit()
        _progress_task(
            db, state, processed=idx, total=total_codes,
            updated=updated, failed=failed, last_code=code,
        )
    status = _final_status(updated, failed, processed=total_codes)
    _finish_task(
        db, state, total=total_codes, updated=updated, failed=failed,
        last_code=codes[-1] if codes else None,
        detail="; ".join(errors[:20]) or None, status=status,
    )
    return {
        "task": "name_history",
        "status": status,
        "total": total_codes,
        "updated": updated,
        "failed": failed,
        "rows": total_rows,
        "errors": errors,
    }


def _extract_name_segments(text: str) -> list[str]:
    """从曾用名文本中提取按时间排列的名称段（旧->新）。

    兼容 "A->B->C"（stock_info_change_name 常见格式）与 "A/B"、逗号分隔；
    剥离尾部括号注释（如 "中科健A(不含ST)"）。
    """
    parts = re.split(r"->|→|/|，|,", text)
    segments: list[str] = []
    for part in parts:
        name = re.sub(r"[(（].*$", "", part).strip()
        if name and (not segments or segments[-1] != name):
            segments.append(name)
    return segments


def _upsert_name_frame(db: Session, code: str, frame: pd.DataFrame) -> int:
    """归一化并 upsert 名称变更历史。

    兼容两类数据源：
    - ak.stock_info_change_name（当前）：行序即时间序（旧->新），每行 name
      字段可能含 "A->B->C" 区间标记，解析为连续名称区间；
    - 旧 ak.stock_name_change_hist：带 开始日期/结束日期/变更原因 列，
      直接按显式区间落库。
    接口不披露精确日期时 start_date 留空（不伪造），区间顺序以 sort_order 表达。
    """
    # 1) 解析为统一段结构：[(sort_order, name, start, end, reason)]
    segments: list[tuple[int, str, date | None, date | None, str | None]] = []
    order = 0
    has_explicit_dates = any(
        _to_date(record.get("开始日期") or record.get("start_date")) is not None
        for _, record in frame.iterrows()
    )
    for _, record in frame.iterrows():
        start = _to_date(record.get("开始日期") or record.get("start_date"))
        end = _to_date(record.get("结束日期") or record.get("end_date"))
        reason_raw = record.get("变更原因") or record.get("change_reason")
        reason = str(reason_raw).strip() if reason_raw else None
        name_text = str(record.get("名称") or record.get("name") or "").strip()
        if not name_text:
            continue
        for name in _extract_name_segments(name_text):
            segments.append((order, name, start, end, reason))
            order += 1

    if not segments:
        return 0

    # 2) 无显式日期时按行序推导区间：上一段的结束即下一段的开始（日期未知留空）
    if not has_explicit_dates:
        today = datetime.now().date()
        dated: list[tuple[int, str, date | None, date | None, str | None]] = []
        for idx, (sort_order, name, _start, _end, reason) in enumerate(segments):
            is_last = idx == len(segments) - 1
            end_date = None if is_last else today  # 区间边界日未知，用占位保证区间语义
            dated.append((sort_order, name, None, end_date, reason))
        segments = dated

    # 3) upsert：显式日期用 (code, start_date, name) 幂等键；无日期用名称顺序匹配
    existing_by_name: dict[str, list[StockNameHistory]] = {}
    if not has_explicit_dates:
        rows = db.scalars(
            select(StockNameHistory)
            .where(StockNameHistory.code == code)
            .order_by(StockNameHistory.sort_order, StockNameHistory.id)
        ).all()
        for row in rows:
            existing_by_name.setdefault(row.name, []).append(row)

    count = 0
    used_ids: set[int] = set()
    for sort_order, name, start, end, reason in segments:
        row: StockNameHistory | None = None
        if start is not None:
            row = db.scalar(
                select(StockNameHistory).where(
                    StockNameHistory.code == code,
                    StockNameHistory.start_date == start,
                    StockNameHistory.name == name,
                )
            )
        else:
            candidates = [
                item for item in existing_by_name.get(name, []) if item.id not in used_ids
            ]
            if candidates:
                row = candidates[0]
        if row is None:
            row = StockNameHistory(code=code, name=name, start_date=start)
            db.add(row)
            db.flush()
        used_ids.add(row.id)
        row.sort_order = sort_order
        row.end_date = end
        row.is_st = _is_st_name(name)
        row.change_reason = reason
        count += 1
    return count


# ---------------------------------------------------------------------------
# 行业归属
# ---------------------------------------------------------------------------

# 主源：东方财富行业板块成分（每板块一次全市场请求，离线可测）；
# 回退源：巨潮个股行业变动记录（按股票逐个查询，仅补缺）。
_INDUSTRY_SOURCE_EM = "em"
_INDUSTRY_SOURCE_CNINFO = "cninfo"


def _upsert_industry(db: Session, code: str, industry_name: str, source: str) -> None:
    row = db.scalar(
        select(StockIndustry).where(
            StockIndustry.code == code,
            StockIndustry.source == source,
        )
    )
    if row is None:
        row = StockIndustry(code=code, source=source, industry_name=industry_name)
        db.add(row)
    row.industry_name = industry_name


def sync_industries(db: Session, codes: list[str] | None = None) -> dict[str, Any]:
    """同步股票行业归属（主源东财板块成分，巨潮个股变动记录回退补缺）。

    - 主源：遍历东财行业板块 -> 每板块抓全量成分 -> 按 code 分配，批量成本低；
    - 回退：主源不可用或未覆盖的股票，逐个查巨潮行业变动记录取最新归属；
    - 单板块/单股失败不中断整体，失败摘要落 stock_sync_state.detail。
    """
    state = _begin_task(db, "industry")
    wanted: set[str] | None = None
    if codes is not None:
        wanted = {str(code).strip().zfill(6) for code in codes if str(code).strip()}

    updated = 0
    failed = 0
    errors: list[str] = []
    total_rows = 0
    covered: set[str] = set()

    boards = ak_fetch.fetch_industry_boards()
    if boards is None:
        errors.append("东财行业板块列表不可用")
    else:
        names = [
            str(record.get("板块名称") or record.get("name") or "").strip()
            for _, record in boards.iterrows()
        ]
        total_boards = len([n for n in names if n])
        processed = 0
        for board_name in names:
            if not board_name:
                continue
            processed += 1
            frame = ak_fetch.fetch_industry_cons(board_name)
            if frame is None:
                failed += 1
                errors.append(f"{board_name}: 成分抓取失败")
            else:
                board_rows = 0
                for _, record in frame.iterrows():
                    raw_code = record.get("代码") or record.get("code")
                    code = str(raw_code).strip().zfill(6) if raw_code is not None else ""
                    if len(code) != 6 or not code.isdigit():
                        continue
                    if wanted is not None and code not in wanted:
                        continue
                    _upsert_industry(db, code, board_name, _INDUSTRY_SOURCE_EM)
                    covered.add(code)
                    board_rows += 1
                total_rows += board_rows
                updated += 1
                db.commit()
            _progress_task(
                db, state, processed=processed, total=total_boards,
                updated=updated, failed=failed,
            )

    # 回退：主源缺失的目标股票逐个查巨潮行业变动
    fallback_targets: list[str] = []
    if wanted is not None:
        fallback_targets = sorted(wanted - covered)
    elif boards is None:
        # 主源整体不可用且无显式目标时，退化为 master 全表逐股回退
        fallback_targets = list(db.scalars(select(StockMaster.code).order_by(StockMaster.code)).all())
    for code in fallback_targets:
        frame = ak_fetch.fetch_industry_change_cninfo(code)
        if frame is None:
            failed += 1
            errors.append(f"{code}: 巨潮行业回退抓取失败")
            continue
        latest: str | None = None
        # 优先巨潮标准的行业大类；按变更日期升序取最新记录。
        if "变更日期" in frame.columns:
            frame = frame.sort_values("变更日期")
        for _, record in frame.iterrows():
            name = (
                record.get("行业中类")
                or record.get("行业大类")
                or record.get("行业门类")
                or record.get("行业名称")
                or record.get("所属行业")
                or record.get("industry_name")
                or record.get("变更后行业")
            )
            try:
                invalid = name is None or pd.isna(name)
            except (TypeError, ValueError):
                invalid = name is None
            if not invalid:
                text = str(name).strip()
                if text:
                    latest = text
        if not latest:
            failed += 1
            errors.append(f"{code}: 巨潮行业回退无记录")
            continue
        _upsert_industry(db, code, latest, _INDUSTRY_SOURCE_CNINFO)
        covered.add(code)
        total_rows += 1
        updated += 1
        db.commit()

    status = _final_status(updated, failed)
    _finish_task(
        db, state, total=updated + failed, updated=updated, failed=failed,
        detail="; ".join(errors[:20]) or None, status=status,
    )
    return {
        "task": "industry",
        "status": status,
        "total": updated + failed,
        "updated": updated,
        "failed": failed,
        "rows": total_rows,
        "stocks": len(covered),
        "errors": errors,
    }
