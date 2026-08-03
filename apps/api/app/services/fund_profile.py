"""基金详情聚合：介绍缓存、按需披露和量化指标。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from io import StringIO
from typing import Any

import pandas as pd
import requests
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    FundCatalogEntry,
    FundHolding,
    FundIndustryAllocation,
    FundNav,
    FundProfile,
    Instrument,
    InstrumentType,
)
from app.schemas.discovery_quant import FactorBoardQuery
from app.schemas.funds import (
    FundDetailHolding,
    FundDetailIndustry,
    FundDetailResponse,
    FundProfileOut,
)
from app.services import fund_holdings, fund_news_analysis, quant, quant_discovery
from app.services.fund_data import backfill_fund_nav_history
from app.services.quant import QuantError

PROFILE_MAX_AGE = timedelta(days=30)
HEADERS = {"User-Agent": "Mozilla/5.0 money-personal-dashboard/0.1"}


def _clean(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return None if not text or text in {"<NA>", "nan", "None", "---"} else text


def _parse_day(value: Any) -> date | None:
    text = _clean(value)
    if not text:
        return None
    normalized = text[:10].replace("年", "-").replace("月", "-").replace("日", "")
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None


def _fetch_xq(code: str) -> dict[str, Any]:
    import akshare as ak

    frame = ak.fund_individual_basic_info_xq(symbol=code, timeout=15)
    if frame is None or frame.empty:
        raise RuntimeError("雪球基金详情返回空数据")
    values = {_clean(row.iloc[0]): _clean(row.iloc[1]) for _, row in frame.iterrows()}
    return {
        "short_name": values.get("基金名称"),
        "full_name": values.get("基金全称"),
        "inception_date": _parse_day(values.get("成立时间")),
        "latest_scale": values.get("最新规模"),
        "company": values.get("基金公司"),
        "manager": values.get("基金经理"),
        "custodian": values.get("托管银行"),
        "fund_type": values.get("基金类型"),
        "rating_agency": values.get("评级机构"),
        "rating": values.get("基金评级"),
        "investment_objective": values.get("投资目标"),
        "investment_strategy": values.get("投资策略"),
        "benchmark": values.get("业绩比较基准"),
        "source": "xueqiu",
    }


def _fetch_eastmoney(code: str) -> dict[str, Any]:
    response = requests.get(
        f"https://fundf10.eastmoney.com/jbgk_{code}.html", headers=HEADERS, timeout=20
    )
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))
    table = next((item for item in tables if item.shape[1] >= 4 and "基金全称" in item.astype(str).values), None)
    if table is None:
        raise RuntimeError("东方财富基金概况表不存在")
    values: dict[str, str | None] = {}
    for _, row in table.iterrows():
        for index in range(0, min(len(row), 4), 2):
            key = _clean(row.iloc[index])
            if key:
                values[key] = _clean(row.iloc[index + 1])
    inception_scale = values.get("成立日期/规模") or ""
    inception_text = inception_scale.split("/")[0].strip()
    return {
        "short_name": values.get("基金简称"),
        "full_name": values.get("基金全称"),
        "inception_date": _parse_day(inception_text),
        "latest_scale": values.get("净资产规模") or values.get("份额规模"),
        "company": values.get("基金管理人"),
        "manager": values.get("基金经理人"),
        "custodian": values.get("基金托管人"),
        "fund_type": values.get("基金类型"),
        "management_fee": values.get("管理费率"),
        "custody_fee": values.get("托管费率"),
        "source": "eastmoney",
    }


def sync_profile(db: Session, code: str) -> tuple[FundProfile, list[str]]:
    warnings: list[str] = []
    data: dict[str, Any] | None = None
    errors: list[str] = []
    for source in (_fetch_xq, _fetch_eastmoney):
        try:
            data = source(code)
            break
        except Exception as exc:  # noqa: BLE001 - 外部源统一降级
            errors.append(f"{source.__name__}: {exc}")
    record = db.get(FundProfile, code) or FundProfile(code=code)
    if data:
        for key, value in data.items():
            setattr(record, key, value)
        record.last_error = None
    else:
        record.last_error = "；".join(errors)
        warnings.append(f"基金介绍暂不可用：{record.last_error}")
    record.fetched_at = datetime.now()
    db.add(record)
    db.commit()
    db.refresh(record)
    return record, warnings


def get_profile(db: Session, code: str, *, refresh: bool = False) -> tuple[FundProfile, list[str]]:
    record = db.get(FundProfile, code)
    if not refresh and record is not None and record.fetched_at:
        fetched = record.fetched_at.replace(tzinfo=None)
        if datetime.now() - fetched <= PROFILE_MAX_AGE:
            return record, ([f"基金介绍最近同步失败：{record.last_error}"] if record.last_error else [])
    return sync_profile(db, code)


def _ensure_instrument(db: Session, catalog: FundCatalogEntry | None, code: str) -> Instrument | None:
    instrument = db.scalar(select(Instrument).where(Instrument.code == code))
    if instrument is None and catalog is not None:
        instrument = Instrument(
            code=catalog.code,
            name=catalog.name,
            type=InstrumentType.FUND,
            currency="CNY",
        )
        db.add(instrument)
        db.commit()
        db.refresh(instrument)
    return instrument


def _sync_composition(db: Session, instrument: Instrument) -> list[str]:
    warnings: list[str] = []
    year = date.today().year
    try:
        holdings = fund_holdings.fetch_holdings(instrument.code, year)
        if not holdings and year > 2000:
            holdings = fund_holdings.fetch_holdings(instrument.code, year - 1)
        if holdings:
            report_dates = {item["report_date"] for item in holdings}
            db.execute(
                delete(FundHolding).where(
                    FundHolding.instrument_id == instrument.id,
                    FundHolding.report_date.in_(report_dates),
                )
            )
            db.add_all(FundHolding(instrument_id=instrument.id, **item) for item in holdings)
        else:
            warnings.append("该基金暂无可用的季度重仓股披露")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"季度重仓股获取失败：{exc}")
        db.rollback()
    try:
        industries = fund_holdings.fetch_industries(instrument.code, year)
        if not industries and year > 2000:
            industries = fund_holdings.fetch_industries(instrument.code, year - 1)
        if industries:
            report_dates = {item["report_date"] for item in industries}
            db.execute(
                delete(FundIndustryAllocation).where(
                    FundIndustryAllocation.instrument_id == instrument.id,
                    FundIndustryAllocation.report_date.in_(report_dates),
                )
            )
            db.add_all(
                FundIndustryAllocation(instrument_id=instrument.id, **item) for item in industries
            )
        else:
            warnings.append("该基金暂无可用的行业配置披露")
        db.commit()
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"行业配置获取失败：{exc}")
        db.rollback()
    return warnings


def build_detail(db: Session, code: str, *, refresh: bool = False) -> FundDetailResponse | None:
    catalog = db.scalar(select(FundCatalogEntry).where(FundCatalogEntry.code == code))
    instrument = _ensure_instrument(db, catalog, code)
    if catalog is None and instrument is None:
        return None
    name = catalog.name if catalog is not None else instrument.name
    warnings: list[str] = []
    profile, profile_warnings = get_profile(db, code, refresh=refresh)
    warnings.extend(profile_warnings)

    if instrument is not None:
        latest_holding = db.scalar(
            select(func.max(FundHolding.report_date)).where(FundHolding.instrument_id == instrument.id)
        )
        if refresh or latest_holding is None:
            warnings.extend(_sync_composition(db, instrument))
        latest_holding = db.scalar(
            select(func.max(FundHolding.report_date)).where(FundHolding.instrument_id == instrument.id)
        )
        latest_industry = db.scalar(
            select(func.max(FundIndustryAllocation.report_date)).where(
                FundIndustryAllocation.instrument_id == instrument.id
            )
        )
        holding_rows = list(
            db.scalars(
                select(FundHolding)
                .where(
                    FundHolding.instrument_id == instrument.id,
                    FundHolding.report_date == latest_holding,
                )
                .order_by(FundHolding.rank, FundHolding.weight.desc())
            ).all()
        ) if latest_holding else []
        industry_rows = list(
            db.scalars(
                select(FundIndustryAllocation)
                .where(
                    FundIndustryAllocation.instrument_id == instrument.id,
                    FundIndustryAllocation.report_date == latest_industry,
                )
                .order_by(FundIndustryAllocation.weight.desc())
            ).all()
        ) if latest_industry else []
    else:
        latest_holding = latest_industry = None
        holding_rows = []
        industry_rows = []

    metrics = None
    metrics_as_of = None
    advice = None
    analysis = None
    if instrument is not None:
        nav_count = db.scalar(
            select(func.count(FundNav.id)).where(FundNav.instrument_id == instrument.id)
        ) or 0
        if nav_count < 2:
            result = backfill_fund_nav_history(
                db, instrument, years=5, resume=True, use_fallback=True
            )
            if result["status"] == "failed":
                warnings.append(f"历史净值按需回填失败：{result['error'] or '数据源未返回数据'}")
    try:
        response = quant_discovery.factor_leaderboard(
            db,
            FactorBoardQuery(codes=[code], min_samples=2, limit=1, window=252),
        )
        metrics = response.items[0].model_dump() if response.items else None
        metrics_as_of = response.as_of
    except QuantError as exc:
        warnings.append(f"量化指标暂不可用：{exc}")
    try:
        base_advice = quant.compute_fund_indicators(db, code).advice
        if base_advice is not None and instrument is not None:
            advice, analysis = fund_news_analysis.combine_fund_advice(
                db, instrument, base_advice
            )
        else:
            advice = base_advice
    except QuantError as exc:
        warnings.append(f"趋势建议暂不可用：{exc}")

    profile_out = FundProfileOut(
        code=profile.code,
        short_name=profile.short_name,
        full_name=profile.full_name,
        inception_date=profile.inception_date.isoformat() if profile.inception_date else None,
        latest_scale=profile.latest_scale,
        company=profile.company,
        manager=profile.manager,
        custodian=profile.custodian,
        fund_type=profile.fund_type,
        rating_agency=profile.rating_agency,
        rating=profile.rating,
        investment_objective=profile.investment_objective,
        investment_strategy=profile.investment_strategy,
        benchmark=profile.benchmark,
        management_fee=profile.management_fee,
        custody_fee=profile.custody_fee,
        source=profile.source,
        fetched_at=profile.fetched_at.isoformat() if profile.fetched_at else None,
    )
    report_dates = [value for value in (latest_holding, latest_industry) if value]
    return FundDetailResponse(
        code=code,
        name=name,
        fund_type=catalog.fund_type if catalog else profile.fund_type,
        market=catalog.market if catalog else None,
        family=catalog.family if catalog else None,
        share_class=catalog.share_class if catalog else None,
        active=catalog.active if catalog else None,
        profile=profile_out,
        metrics=metrics,
        metrics_as_of=metrics_as_of,
        metrics_basis="近1月/3月/1年/3年按自然日期计算，包含现金分红再投资收益",
        advice=advice,
        analysis=analysis,
        holdings=[
            FundDetailHolding(
                rank=row.rank,
                stock_code=row.stock_code,
                stock_name=row.stock_name,
                weight=row.weight,
                shares=row.shares,
                market_value=row.market_value,
                report_date=row.report_date.isoformat(),
            )
            for row in holding_rows
        ],
        industries=[
            FundDetailIndustry(
                industry=row.industry,
                weight=row.weight,
                market_value=row.market_value,
                report_date=row.report_date.isoformat(),
            )
            for row in industry_rows
        ],
        report_date=max(report_dates).isoformat() if report_dates else None,
        warnings=list(dict.fromkeys(warnings)),
    )
