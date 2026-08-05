"""银行、券商、保险独立因子契约；不以工业企业字段静默补位。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.services.stock_repository import Fundamentals


@dataclass(frozen=True)
class SectorFeature:
    field: str
    direction: int
    family: str
    required: bool = True


@dataclass(frozen=True)
class FinancialSectorAssessment:
    sector: str
    eligible: bool
    values: dict[str, float | None]
    missing_required: tuple[str, ...]
    used_features: tuple[str, ...]
    reason: str


FEATURE_DICTIONARIES: dict[str, tuple[SectorFeature, ...]] = {
    "bank": (
        SectorFeature("bank_net_interest_margin", 1, "quality"),
        SectorFeature("bank_npl_ratio", -1, "risk"),
        SectorFeature("bank_provision_coverage_ratio", 1, "risk"),
        SectorFeature("bank_capital_adequacy_ratio", 1, "risk"),
        SectorFeature("bank_loan_deposit_ratio", -1, "risk", required=False),
        SectorFeature("bank_ep", 1, "value"),
        SectorFeature("bank_bp", 1, "value"),
    ),
    "broker": (
        SectorFeature("broker_proprietary_risk_ratio", -1, "risk"),
        SectorFeature("broker_leverage_ratio", -1, "risk"),
        SectorFeature("broker_net_capital_ratio", 1, "risk"),
        SectorFeature("broker_ep", 1, "value"),
        SectorFeature("broker_bp", 1, "value"),
    ),
    "insurance": (
        SectorFeature("insurance_solvency_ratio", 1, "risk"),
        SectorFeature("insurance_combined_ratio", -1, "quality"),
        SectorFeature("insurance_reserve_coverage_ratio", 1, "risk"),
        SectorFeature("insurance_ep", 1, "value"),
        SectorFeature("insurance_bp", 1, "value"),
    ),
}

COMPANY_TYPE_TO_SECTOR = {"2": "bank", "3": "insurance", "4": "broker"}


def sector_from_snapshot(
    snapshot: Fundamentals | None,
    industry: str,
) -> str | None:
    if snapshot is not None and snapshot.company_type is not None:
        matched = COMPANY_TYPE_TO_SECTOR.get(str(snapshot.company_type))
        if matched:
            return matched
    # 只在已有专用字段时接受行业文本兜底，避免旧/mock 数据被误当作
    # 已满足监管指标的正式金融公司快照。
    if snapshot is not None:
        if "银行" in industry and snapshot.bank_npl_ratio is not None:
            return "bank"
        if "证券" in industry and snapshot.broker_net_capital_ratio is not None:
            return "broker"
        if "保险" in industry and snapshot.insurance_solvency_ratio is not None:
            return "insurance"
    return None


def assess_financial_sector(
    snapshot: Fundamentals,
    *,
    industry: str,
    valuation: Fundamentals | None = None,
) -> FinancialSectorAssessment | None:
    sector = sector_from_snapshot(snapshot, industry)
    if sector is None:
        return None
    values: dict[str, float | None] = {}
    missing: list[str] = []
    used: list[str] = []
    for feature in FEATURE_DICTIONARIES[sector]:
        if feature.field.endswith("_ep"):
            value = valuation.ep if valuation is not None else None
        elif feature.field.endswith("_bp"):
            value = valuation.bp if valuation is not None else None
        else:
            value = getattr(snapshot, feature.field)
        values[feature.field] = value
        if value is not None:
            used.append(feature.field)
        elif feature.required:
            missing.append(feature.field)
    eligible = not missing
    return FinancialSectorAssessment(
        sector=sector,
        eligible=eligible,
        values=values,
        missing_required=tuple(missing),
        used_features=tuple(used),
        reason=(
            f"{sector} 专用模型字段完整"
            if eligible
            else f"{sector} 专用模型缺少必需字段：{','.join(missing)}，显式排除"
        ),
    )


def factor_directions() -> Mapping[str, int]:
    return {
        feature.field: feature.direction
        for features in FEATURE_DICTIONARIES.values()
        for feature in features
    }


def factor_families() -> Mapping[str, str]:
    # 风险字段在多因子评分中并入质量族，但仍以 risk 标签保留在模型结构。
    return {
        feature.field: (
            "quality" if feature.family == "risk" else feature.family
        )
        for features in FEATURE_DICTIONARIES.values()
        for feature in features
    }
