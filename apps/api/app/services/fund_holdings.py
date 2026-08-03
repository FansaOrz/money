"""同步基金季度重仓股与行业配置。

优先使用 AKShare；未安装或接口失败时，重仓股回退到天天基金公开接口。
"""

from __future__ import annotations

import re
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO

import pandas as pd
import requests
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import FundHolding, FundIndustryAllocation, Instrument, Position

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://fundf10.eastmoney.com/"}


def _decimal(value) -> Decimal | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _normalized_code(value) -> str:
    text = str(value).split(".")[0].strip()
    return text.zfill(6) if text.isdigit() else text


def fetch_holdings_eastmoney(code: str, year: int) -> list[dict]:
    url = (
        "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
        f"?type=jjcc&code={code}&topline=10&year={year}&month=6"
    )
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    match = re.search(r'content:"(.*)",arryear:', response.text, re.S)
    if not match:
        return []
    html = match.group(1).replace('\\"', '"')
    tables = pd.read_html(StringIO(html))
    results: list[dict] = []
    for table in tables:
        label_match = re.search(rf"{year}年([124])季度", html)
        quarter = int(label_match.group(1)) if label_match else (2 if len(results) == 0 else 1)
        report_month = quarter * 3
        report_day = 30 if report_month in {6, 9} else 31
        report_date = date(year, report_month, report_day)
        columns = {str(col).replace(" ", ""): col for col in table.columns}
        for _, row in table.iterrows():
            stock_code = _normalized_code(row.get(columns.get("股票代码")))
            weight = _decimal(row.get(columns.get("占净值比例")))
            if not stock_code or weight is None:
                continue
            results.append(
                {
                    "report_date": report_date,
                    "rank": int(row.get(columns.get("序号"))) if not pd.isna(row.get(columns.get("序号"))) else None,
                    "stock_code": stock_code,
                    "stock_name": str(row.get(columns.get("股票名称"), "")).strip(),
                    "weight": weight,
                    "shares": _decimal(row.get(columns.get("持股数（万股）"))),
                    "market_value": _decimal(row.get(columns.get("持仓市值（万元）"))),
                }
            )
    return results


def fetch_holdings(code: str, year: int) -> list[dict]:
    try:
        import akshare as ak

        frame = ak.fund_portfolio_hold_em(symbol=code, date=str(year))
        results = []
        for _, row in frame.iterrows():
            report = str(row.get("季度", ""))
            quarter_match = re.search(r"([1-4])季度", report)
            quarter = int(quarter_match.group(1)) if quarter_match else 2
            month = quarter * 3
            results.append(
                {
                    "report_date": date(year, month, 30 if month in {6, 9} else 31),
                    "rank": int(row.get("序号")) if not pd.isna(row.get("序号")) else None,
                    "stock_code": _normalized_code(row.get("股票代码")),
                    "stock_name": str(row.get("股票名称", "")).strip(),
                    "weight": _decimal(row.get("占净值比例")) or Decimal("0"),
                    "shares": _decimal(row.get("持股数")),
                    "market_value": _decimal(row.get("持仓市值")),
                }
            )
        return results
    except Exception:
        return fetch_holdings_eastmoney(code, year)


def fetch_industries(code: str, year: int) -> list[dict]:
    try:
        import akshare as ak

        frame = ak.fund_portfolio_industry_allocation_em(symbol=code, date=str(year))
    except Exception:
        return []
    results = []
    for _, row in frame.iterrows():
        report_text = str(row.get("截止时间") or row.get("报告期") or f"{year}-06-30")[:10]
        try:
            report_date = date.fromisoformat(report_text)
        except ValueError:
            report_date = date(year, 6, 30)
        industry = str(row.get("行业类别") or row.get("行业名称") or "").strip()
        weight = _decimal(row.get("占净值比例"))
        if industry and weight is not None:
            results.append(
                {
                    "report_date": report_date,
                    "industry": industry,
                    "weight": weight,
                    "market_value": _decimal(row.get("市值")),
                }
            )
    return results


def sync_fund_holdings(db: Session, year: int | None = None, limit: int | None = None) -> dict:
    year = year or date.today().year
    instruments = db.scalars(
        select(Instrument).join(Position).distinct().order_by(Position.market_value.desc())
    ).all()
    if limit:
        instruments = instruments[:limit]
    succeeded = 0
    failed = 0
    holding_rows = 0
    industry_rows = 0
    for instrument in instruments:
        try:
            holdings = fetch_holdings(instrument.code, year)
            if holdings:
                report_dates = {item["report_date"] for item in holdings}
                db.execute(
                    delete(FundHolding).where(
                        FundHolding.instrument_id == instrument.id,
                        FundHolding.report_date.in_(report_dates),
                    )
                )
                for item in holdings:
                    db.add(FundHolding(instrument_id=instrument.id, **item))
                holding_rows += len(holdings)
                succeeded += 1
            else:
                failed += 1
            industries = fetch_industries(instrument.code, year)
            if industries:
                report_dates = {item["report_date"] for item in industries}
                db.execute(
                    delete(FundIndustryAllocation).where(
                        FundIndustryAllocation.instrument_id == instrument.id,
                        FundIndustryAllocation.report_date.in_(report_dates),
                    )
                )
                for item in industries:
                    db.add(FundIndustryAllocation(instrument_id=instrument.id, **item))
                industry_rows += len(industries)
            db.commit()
        except Exception:
            db.rollback()
            failed += 1
        time.sleep(0.15)
    return {
        "total": len(instruments),
        "succeeded": succeeded,
        "failed": failed,
        "holding_rows": holding_rows,
        "industry_rows": industry_rows,
    }
