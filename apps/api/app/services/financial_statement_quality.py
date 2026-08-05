"""财务报表勾稽、期间、单位与审计元数据的正式因子门禁。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class FinancialQualityAssessment:
    formal_factor_usable: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    unit_policy: str
    flow_basis: str
    audit_opinion: str
    correction_status: str
    report_period_kind: str


UNIT_REGISTRY = {
    "income": {
        "monetary_unit": "CNY",
        "scale_to_cny": 1.0,
        "flow_basis": "year_to_date",
    },
    "balancesheet": {
        "monetary_unit": "CNY",
        "scale_to_cny": 1.0,
        "flow_basis": "point_in_time",
    },
    "cashflow": {
        "monetary_unit": "CNY",
        "scale_to_cny": 1.0,
        "flow_basis": "year_to_date",
    },
    "daily_basic": {
        "market_cap_unit": "10k_CNY",
        "market_cap_scale_to_cny": 10_000.0,
        "ratio_unit": "percent_or_multiple_by_field",
    },
    "fina_indicator": {
        "ratio_unit": "percentage_points",
        "per_share_unit": "CNY_per_share",
    },
}
UNIT_POLICY_VERSION = "TUSHARE_CN_FINANCIAL_UNITS_V1"


def _number(row: dict[str, object], field: str) -> float | None:
    try:
        value = row.get(field)
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _relative_error(left: float, right: float) -> float:
    return abs(left - right) / max(abs(left), abs(right), 1.0)


def _period_kind(period: date) -> str:
    if (period.month, period.day) == (3, 31):
        return "Q1_YTD"
    if (period.month, period.day) == (6, 30):
        return "H1_YTD"
    if (period.month, period.day) == (9, 30):
        return "Q3_YTD"
    if (period.month, period.day) == (12, 31):
        return "FY_YTD"
    return "NON_STANDARD"


def assess_statement_bundle(
    *,
    period: date,
    rows: dict[str, dict[str, object]],
    units: dict[str, dict[str, object]] | None = None,
) -> FinancialQualityAssessment:
    """评估一个报告期的三表；不确定单位或勾稽失败时禁止正式因子消费。"""
    errors: list[str] = []
    warnings: list[str] = []
    unit_definitions = units or UNIT_REGISTRY
    required_units = ("income", "balancesheet", "cashflow", "fina_indicator")
    missing_units = [name for name in required_units if name not in unit_definitions]
    if missing_units:
        errors.append("单位定义缺失：" + ",".join(missing_units))
    else:
        for dataset in ("income", "balancesheet", "cashflow"):
            scale = unit_definitions[dataset].get("scale_to_cny")
            if not isinstance(scale, (int, float)) or scale <= 0:
                errors.append(f"{dataset} 金额单位比例不确定")

    period_kind = _period_kind(period)
    if period_kind == "NON_STANDARD":
        errors.append(f"非标准报告期：{period.isoformat()}")

    balance = rows.get("balancesheet", {})
    assets = _number(balance, "total_assets")
    liab_equity = _number(balance, "total_liab_hldr_eqy")
    if liab_equity is None:
        liabilities = _number(balance, "total_liab")
        equity = _number(balance, "total_hldr_eqy_inc_min_int")
        if equity is None:
            equity = _number(balance, "total_hldr_eqy_exc_min_int")
        if liabilities is not None and equity is not None:
            liab_equity = liabilities + equity
    if assets is None or liab_equity is None:
        errors.append("资产负债表关键勾稽字段缺失")
    elif _relative_error(assets, liab_equity) > 0.01:
        errors.append(
            "资产 != 负债+权益："
            f"relative_error={_relative_error(assets, liab_equity):.6f}"
        )

    cashflow = rows.get("cashflow", {})
    begin_cash = _number(cashflow, "c_cash_equ_beg_period")
    cash_change = _number(cashflow, "n_incr_cash_cash_equ")
    end_cash = _number(cashflow, "c_cash_equ_end_period")
    if (
        begin_cash is not None
        and cash_change is not None
        and end_cash is not None
        and _relative_error(begin_cash + cash_change, end_cash) > 0.02
    ):
        errors.append(
            "期初现金+现金净增加额 != 期末现金："
            f"relative_error={_relative_error(begin_cash + cash_change, end_cash):.6f}"
        )
    elif begin_cash is None or cash_change is None or end_cash is None:
        warnings.append("现金流完整勾稽字段缺失")

    report_types = {
        str(row.get("report_type"))
        for row in rows.values()
        if row.get("report_type") not in (None, "")
    }
    if len(report_types) > 1:
        warnings.append("三表 report_type 不一致：" + ",".join(sorted(report_types)))
    correction = (
        "restated"
        if any(str(row.get("update_flag") or "") == "1" for row in rows.values())
        else "original"
    )
    audit_values = [
        str(
            row.get("audit_result")
            or row.get("audit_opinion")
            or ""
        ).strip()
        for row in rows.values()
    ]
    audit_opinion = next((value for value in audit_values if value), "unknown")
    if audit_opinion == "unknown":
        warnings.append("审计意见数据不可用")
    elif not any(
        marker in audit_opinion
        for marker in ("标准无保留", "无保留意见", "unqualified")
    ):
        warnings.append(f"非标准审计意见：{audit_opinion}")

    return FinancialQualityAssessment(
        formal_factor_usable=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        unit_policy=UNIT_POLICY_VERSION,
        flow_basis="year_to_date",
        audit_opinion=audit_opinion,
        correction_status=correction,
        report_period_kind=period_kind,
    )

def market_cap_to_cny(value: float) -> float:
    return value * float(
        UNIT_REGISTRY["daily_basic"]["market_cap_scale_to_cny"]
    )
