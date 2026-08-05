"""小分母财务比率和 FCF yield 的经济含义治理。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

PROFIT_DENOMINATOR_ASSET_FLOOR = 0.005
PROFIT_DENOMINATOR_ABSOLUTE_FLOOR_CNY = 1_000_000.0
MAX_ABS_ASSET_SCALED_CASH_CONVERSION = 5.0
MAX_ABS_FCF_YIELD = 2.0


@dataclass(frozen=True)
class CashConversionAssessment:
    ocf_to_profit: float | None
    cash_conversion_assets: float | None
    profit_classification: str
    denominator_floor: float | None
    issues: tuple[str, ...]


@dataclass(frozen=True)
class FcfYieldAssessment:
    value: float | None
    status: str
    reason: str
    lineage: dict[str, object]


def assess_cash_conversion(
    *,
    net_income: float | None,
    operating_cash_flow: float | None,
    total_assets: float | None,
) -> CashConversionAssessment:
    """保留展示比率，但正式质量因子优先使用 (OCF-NI)/资产。"""
    if (
        net_income is None
        or operating_cash_flow is None
        or total_assets is None
        or total_assets <= 0
    ):
        return CashConversionAssessment(
            None, None, "missing", None, ("利润/经营现金流/总资产不完整",)
        )
    floor = max(
        PROFIT_DENOMINATOR_ABSOLUTE_FLOOR_CNY,
        abs(total_assets) * PROFIT_DENOMINATOR_ASSET_FLOOR,
    )
    issues: list[str] = []
    if net_income <= 0:
        classification = "loss"
        ratio = None
        issues.append("净利润非正，OCF/净利润不具备可比经济含义")
    elif net_income < floor:
        classification = "small_positive_profit"
        ratio = None
        issues.append(
            f"净利润低于分母下限{floor:.2f}，禁止构造不稳定 OCF/净利润"
        )
    else:
        classification = "normal_profit"
        ratio = operating_cash_flow / net_income
    stable = (operating_cash_flow - net_income) / total_assets
    if abs(stable) > MAX_ABS_ASSET_SCALED_CASH_CONVERSION:
        issues.append("资产缩放现金转化率超出经济范围，转入质量审查")
        stable = None
    return CashConversionAssessment(
        ocf_to_profit=ratio,
        cash_conversion_assets=stable,
        profit_classification=classification,
        denominator_floor=floor,
        issues=tuple(issues),
    )


def assess_fcf_yield(
    *,
    free_cash_flow: float | None,
    flow_basis: str | None,
    market_cap: float | None,
    market_cap_date: date | None,
    signal_date: date,
    is_financial_company: bool,
    unit_policy: str | None,
) -> FcfYieldAssessment:
    lineage: dict[str, object] = {
        "definition": "TTM经营现金流-TTM购建固定资产等现金支出",
        "capex_sign": "cash_paid_positive_then_subtracted",
        "currency": "CNY",
        "statement_unit": unit_policy or "CNY元",
        "market_cap_unit": "CNY元",
        "flow_basis": flow_basis,
        "market_cap_date": market_cap_date.isoformat() if market_cap_date else None,
        "signal_date": signal_date.isoformat(),
        "economic_range": [-MAX_ABS_FCF_YIELD, MAX_ABS_FCF_YIELD],
    }
    if is_financial_company:
        return FcfYieldAssessment(
            None,
            "not_applicable_financial",
            "银行、券商、保险和多元金融禁用工业企业 FCF yield",
            lineage,
        )
    if flow_basis != "TTM":
        return FcfYieldAssessment(
            None, "non_ttm", "自由现金流不是 PIT TTM 口径", lineage
        )
    if free_cash_flow is None:
        return FcfYieldAssessment(None, "missing_fcf", "TTM FCF 缺失", lineage)
    if market_cap is None or market_cap <= 0 or market_cap_date is None:
        return FcfYieldAssessment(
            None, "missing_market_cap", "同信号日 PIT 市值或日期缺失", lineage
        )
    if market_cap_date > signal_date:
        return FcfYieldAssessment(
            None, "future_market_cap", "市值日期晚于信号日", lineage
        )
    value = free_cash_flow / market_cap
    if abs(value) > MAX_ABS_FCF_YIELD:
        return FcfYieldAssessment(
            None,
            "economic_extreme",
            f"FCF yield={value:.6g} 超出经济范围",
            lineage,
        )
    return FcfYieldAssessment(
        value, "valid", "PIT TTM FCF/同日可得总市值", lineage
    )


def persist_factor_policy_issues(
    db: object,
    results: Iterable[object],
    *,
    signal_date: date,
) -> int:
    """把前向评分发现的小分母/FCF 极值写入质量问题账本。"""
    from sqlalchemy import select

    from app.models import DataQualityIssue
    from app.services.quant_data_governance import record_quality_issue

    created = 0
    for result in results:
        metadata = dict(getattr(result, "factor_metadata", {}) or {})
        candidates: list[tuple[str, str, str]] = []
        cash = dict(metadata.get("cash_conversion") or {})
        if cash.get("profit_classification") in {
            "loss",
            "small_positive_profit",
        }:
            for issue in cash.get("issues") or []:
                candidates.append(
                    (
                        "cash_conversion",
                        "unstable_financial_denominator",
                        str(issue),
                    )
                )
        fcf = dict(metadata.get("fcf_yield") or {})
        if fcf.get("status") == "economic_extreme":
            candidates.append(
                ("fcf_yield", "economic_extreme", str(fcf.get("reason")))
            )
        for field_name, rule, detail in candidates:
            code = str(getattr(result, "code", ""))
            full_detail = f"{signal_date.isoformat()} {detail}"
            exists = db.scalar(  # type: ignore[attr-defined]
                select(DataQualityIssue.id).where(
                    DataQualityIssue.dataset == "factor_input",
                    DataQualityIssue.code == code,
                    DataQualityIssue.field_name == field_name,
                    DataQualityIssue.rule == rule,
                    DataQualityIssue.detail == full_detail,
                    DataQualityIssue.status == "open",
                )
            )
            if exists is not None:
                continue
            record_quality_issue(
                db,
                dataset="factor_input",
                rule=rule,
                detail=full_detail,
                severity="error",
                code=code,
                field_name=field_name,
                source="formal_factor_engine",
            )
            created += 1
    if created:
        db.flush()  # type: ignore[attr-defined]
    return created
