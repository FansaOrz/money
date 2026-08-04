"""A 股研究数据层路由。

- GET  /api/stocks/data/status       各数据域 coverage（真实统计）
- POST /api/stocks/sync/master       同步 A 股代码/名称主表
- POST /api/stocks/sync/daily        批量同步日线（raw+qfq，断点续传）
- POST /api/stocks/sync/universe     同步沪深300/中证500 当前成分
- POST /api/stocks/sync/fundamentals 同步财务/披露/估值/名称历史/行业归属
- POST /api/stocks/universe/events   导入成分调整事件 CSV
- POST /api/stocks/universe/snapshot 物化某日成分快照
- GET  /api/stocks/universe          查询指数成分（当前/历史）
- GET  /api/stocks/list              A 股 master 列表
- GET  /api/stocks/industries        股票行业归属列表
- GET  /api/stocks/{code}/daily      单股票日线（Parquet 数据湖）
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import StockIndustry, StockMaster
from app.schemas.research import (
    DailyBarOut,
    FundamentalsQueryResult,
    IndustryStatus,
    MasterStatus,
    DailyStatus,
    FundamentalsStatus,
    MembershipImportResult,
    StockDailyResponse,
    StockTechnicalResponse,
    StockDataStatusResponse,
    StockIndustryOut,
    StockIndustryListResponse,
    StockMasterListResponse,
    StockMasterOut,
    StockSyncResult,
    SyncTaskState,
    UniverseResponse,
    UniverseStatus,
)
from app.services.research import stock_data, stock_fundamentals, stock_universe
from app.services.research.parquet_store import DAILY_QFQ, DAILY_RAW
from app.services.stock_technical import analyze_technical

router = APIRouter(prefix="/stocks", tags=["stocks"])


def _wrap_status(raw: dict) -> StockDataStatusResponse:
    """把 service 返回的 dict 装配为响应模型（容错缺省字段）。"""
    def task(d: dict) -> SyncTaskState:
        return SyncTaskState(**{k: v for k, v in d.items() if k in SyncTaskState.model_fields})

    return StockDataStatusResponse(
        generated_at=raw["generated_at"],
        master=MasterStatus(stocks=raw["master"]["stocks"], sync=task(raw["master"]["sync"])),
        daily=DailyStatus(
            stocks_tracked=raw["daily"]["stocks_tracked"],
            stocks_with_parquet=raw["daily"]["stocks_with_parquet"],
            stocks_with_error=raw["daily"]["stocks_with_error"],
            first_trade_date=raw["daily"]["first_trade_date"],
            last_trade_date=raw["daily"]["last_trade_date"],
            sync=task(raw["daily"]["sync"]),
        ),
        universe=UniverseStatus(
            constituents=raw["universe"]["constituents"],
            membership_events=raw["universe"]["membership_events"],
            snapshots=raw["universe"]["snapshots"],
            sync=task(raw["universe"]["sync"]),
        ),
        fundamentals=FundamentalsStatus(
            financial_indicator_rows=raw["fundamentals"]["financial_indicator_rows"],
            financial_indicator_stocks=raw["fundamentals"]["financial_indicator_stocks"],
            disclosure_rows=raw["fundamentals"]["disclosure_rows"],
            disclosure_stocks=raw["fundamentals"]["disclosure_stocks"],
            valuation_rows=raw["fundamentals"]["valuation_rows"],
            valuation_stocks=raw["fundamentals"]["valuation_stocks"],
            name_history_rows=raw["fundamentals"]["name_history_rows"],
            name_history_stocks=raw["fundamentals"]["name_history_stocks"],
            sync_financial=task(raw["fundamentals"]["sync_financial"]),
            sync_disclosure=task(raw["fundamentals"]["sync_disclosure"]),
            sync_valuation=task(raw["fundamentals"]["sync_valuation"]),
            sync_name_history=task(raw["fundamentals"]["sync_name_history"]),
        ),
        industry=IndustryStatus(
            stocks=raw["industry"]["stocks"],
            sources=raw["industry"]["sources"],
            sync=task(raw["industry"]["sync"]),
        ),
    )


@router.get("/data/status", response_model=StockDataStatusResponse)
def get_data_status(db: Session = Depends(get_db)) -> StockDataStatusResponse:
    """研究数据层 coverage 汇总。"""
    return _wrap_status(stock_data.get_data_status(db))


@router.post("/sync/master", response_model=StockSyncResult)
def sync_master(db: Session = Depends(get_db)) -> StockSyncResult:
    """同步 A 股代码/名称主表（ak.stock_info_a_code_name）。"""
    return StockSyncResult(**stock_data.sync_stock_master(db))


@router.post("/sync/daily", response_model=StockSyncResult)
def sync_daily(
    codes: str | None = Query(default=None, description="逗号分隔的 6 位代码；缺省为自动断点选批"),
    limit: int | None = Query(default=None, ge=1, description="本次最多处理多少只（缺省取 research_sync_batch_size）"),
    start_date: date | None = Query(default=None, description="仅保留该日期之后的行情（增量）"),
    with_qfq: bool = Query(default=True, description="是否同步前复权数据"),
    resume: bool = Query(default=True, description="消费上次失败位点 last_code 继续"),
    db: Session = Depends(get_db),
) -> StockSyncResult:
    """批量同步日线到 Parquet 数据湖（raw 必抓，qfq 可选，断点续传）。

    不传 codes 时按“未同步 -> 有错误 -> 最久未更新”自动选取下一批，
    批次大小缺省取 settings.research_sync_batch_size；上次带错结束时会
    从 stock_sync_state.last_code 游标继续，不会每次从头抓头部 N 只。
    """
    code_list = [c.strip().zfill(6) for c in codes.split(",") if c.strip()] if codes else None
    result = stock_data.sync_stock_daily(
        db, code_list, limit=limit, start_date=start_date, fetch_qfq=with_qfq, resume=resume
    )
    return StockSyncResult(**result)


@router.post("/sync/market-close", response_model=StockSyncResult)
def sync_market_close(
    trade_date: date | None = Query(
        default=None, description="研究重放用；日常缺省为北京时间今天"
    ),
    db: Session = Depends(get_db),
) -> StockSyncResult:
    """一次拉取全市场收盘快照，快速推进股票前向模拟的数据日。"""
    return StockSyncResult(**stock_data.sync_stock_market_close(db, trade_date=trade_date))


@router.post("/sync/universe", response_model=StockSyncResult)
def sync_universe(
    index_codes: str | None = Query(default=None, description="逗号分隔指数代码，缺省 000300,000905"),
    db: Session = Depends(get_db),
) -> StockSyncResult:
    """同步指数当前成分（ak.index_stock_cons_csindex）。"""
    codes = [c.strip() for c in index_codes.split(",") if c.strip()] if index_codes else None
    return StockSyncResult(**stock_universe.sync_index_cons(db, codes))


@router.post("/sync/fundamentals", response_model=FundamentalsQueryResult)
def sync_fundamentals(
    codes: str | None = Query(
        default=None,
        description="逗号分隔的 6 位代码；disclosure/industry 缺省时为全市场（按分区快照分配）",
    ),
    kinds: str = Query(
        default="financial,disclosure,valuation,name_history",
        description="逗号分隔：financial/disclosure/valuation/name_history/industry",
    ),
    periods: str | None = Query(
        default=None,
        description="披露日程报告期，如 20231231,20240331 或 2023年报；缺省为今年四个报告期",
    ),
    db: Session = Depends(get_db),
) -> FundamentalsQueryResult:
    """批量同步基本面数据（财务指标/披露日程/估值/历史名称/行业归属）。

    披露日程使用当前 akshare 的 market+period 全市场快照接口：每个
    (市场分区, 报告期) 只抓一次，按 code 分配入库，不再按个股重复抓全市场。
    """
    code_list = (
        [c.strip().zfill(6) for c in codes.split(",") if c.strip()] if codes else None
    )
    kind_set = {k.strip() for k in kinds.split(",") if k.strip()}
    per_code_kinds = kind_set & {"financial", "valuation", "name_history"}
    if per_code_kinds and not code_list:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{sorted(per_code_kinds)} 为按股同步，必须提供 codes",
        )
    rows: list[dict] = []
    if "financial" in kind_set:
        rows.append(stock_fundamentals.sync_financial_indicators(db, code_list or []))
    if "disclosure" in kind_set:
        period_list = (
            [p.strip() for p in periods.split(",") if p.strip()] if periods else None
        )
        rows.append(stock_fundamentals.sync_report_disclosure(db, code_list, period_list))
    if "valuation" in kind_set:
        rows.append(stock_fundamentals.sync_valuations(db, code_list or []))
    if "name_history" in kind_set:
        rows.append(stock_fundamentals.sync_name_history(db, code_list or []))
    if "industry" in kind_set:
        rows.append(stock_fundamentals.sync_industries(db, code_list))
    return FundamentalsQueryResult(
        code=",".join(code_list) if code_list else "*", rows=rows, total=len(rows)
    )


@router.post("/universe/events", response_model=MembershipImportResult)
async def import_membership_events(
    file: UploadFile,
    db: Session = Depends(get_db),
) -> MembershipImportResult:
    """导入指数成分调整事件 CSV（add/remove）。"""
    content = await file.read()
    result = stock_universe.import_membership_events_csv(
        db, content, source=f"csv:{file.filename or 'upload'}"
    )
    return MembershipImportResult(**result)


@router.post("/universe/snapshot", response_model=dict)
def materialize_snapshot(
    index_code: str = Query(...),
    as_of: date = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    """按事件流物化某日成分快照。"""
    return stock_universe.materialize_snapshot(db, index_code, as_of)


@router.get("/universe", response_model=UniverseResponse)
def get_universe(
    index_code: str = Query(default="000300"),
    universe: str | None = Query(default=None, description="hs300 | zz500 兼容别名"),
    as_of: date | None = Query(default=None, description="缺省/未来日期 -> 当前成分"),
    db: Session = Depends(get_db),
) -> UniverseResponse:
    """查询指数成分（当前/历史快照/事件回放）。"""
    aliases = {
        "hs300": "000300",
        "csi300": "000300",
        "zz500": "000905",
        "csi500": "000905",
    }
    if universe:
        index_code = aliases.get(universe.strip().lower(), universe.strip())
    result = stock_universe.get_universe(db, index_code, as_of)
    return UniverseResponse(**result, total=len(result["members"]))


@router.get("/list", response_model=StockMasterListResponse)
def list_stocks(
    keyword: str | None = Query(default=None, description="按代码或名称过滤"),
    limit: int = Query(default=100, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> StockMasterListResponse:
    """A 股 master 列表（同步自 ak.stock_info_a_code_name）。"""
    stmt = select(StockMaster).order_by(StockMaster.code)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(StockMaster.code.like(like) | StockMaster.name.like(like))
    rows = db.scalars(stmt.offset(offset).limit(limit)).all()
    return StockMasterListResponse(
        items=[StockMasterOut(code=r.code, name=r.name, exchange=r.exchange) for r in rows],
        total=len(rows),
    )


@router.get("/master")
def list_stocks_compat(
    industry: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> dict:
    """股票筛选页兼容入口，返回带行业的主数据。"""
    industry_rows = db.execute(
        select(StockIndustry.code, StockIndustry.industry_name)
        .order_by(StockIndustry.code, StockIndustry.source)
    ).all()
    industry_map: dict[str, str] = {}
    for code, name in industry_rows:
        industry_map.setdefault(code, name)
    stmt = select(StockMaster).order_by(StockMaster.code)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(StockMaster.code.like(like) | StockMaster.name.like(like))
    rows = db.scalars(stmt).all()
    items = [
        {
            "code": row.code,
            "name": row.name,
            "exchange": row.exchange,
            "industry": industry_map.get(row.code, "未知"),
        }
        for row in rows
        if not industry or industry_map.get(row.code) == industry
    ][:limit]
    return {
        "items": items,
        "total": len(items),
        "industries": sorted(set(industry_map.values())),
    }


@router.get("/industries", response_model=StockIndustryListResponse)
def list_stock_industries(
    keyword: str | None = Query(default=None, description="按代码/名称/行业过滤"),
    limit: int = Query(default=100, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> StockIndustryListResponse:
    """股票行业归属列表（东财主源 + 巨潮回退源，按 (code, source) 存储）。"""
    stmt = select(StockIndustry).order_by(StockIndustry.code, StockIndustry.source)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            StockIndustry.code.like(like)
            | StockIndustry.name.like(like)
            | StockIndustry.industry_name.like(like)
        )
    rows = db.scalars(stmt.offset(offset).limit(limit)).all()
    return StockIndustryListResponse(
        items=[
            StockIndustryOut(
                code=r.code, name=r.name, source=r.source, industry_name=r.industry_name
            )
            for r in rows
        ],
        total=len(rows),
    )


@router.get("/{code}/daily", response_model=StockDailyResponse)
def get_stock_daily(
    code: str,
    layer: str = Query(default=DAILY_RAW, description="raw | qfq"),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
) -> StockDailyResponse:
    """单只股票日线（从 Parquet 数据湖读取；无数据返回空列表）。"""
    if layer not in {DAILY_RAW, DAILY_QFQ}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="layer 仅支持 raw/qfq"
        )
    rows = stock_data.get_daily_bars(
        db, code.zfill(6), layer=layer, start_date=start_date, end_date=end_date
    )
    return StockDailyResponse(
        code=code.zfill(6),
        layer=layer,
        items=[DailyBarOut(**row) for row in rows],
        total=len(rows),
    )


@router.get("/{code}/technical", response_model=StockTechnicalResponse)
def get_stock_technical(
    code: str,
    db: Session = Depends(get_db),
) -> StockTechnicalResponse:
    """基于前复权日线计算单股技术面摘要和可解释风险提示。"""
    normalized = code.zfill(6)
    rows = stock_data.get_daily_bars(db, normalized, layer=DAILY_QFQ)
    return StockTechnicalResponse(**analyze_technical(normalized, rows))
