"""信号/持仓到因子、规范字段和原始文件的机器可读血缘。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    DataCorrection,
    DataFileAccessLog,
    DataQualityIssue,
    StockPaperAccount,
    StockPaperPosition,
    StockPaperRun,
    StockPaperSignal,
    StrategyVersion,
)
from app.services import pit_warehouse

FACTOR_FAMILIES = {
    "quality": (
        "roe",
        "roa",
        "gross_margin",
        "net_margin",
        "cash_conversion_assets",
        "debt_ratio",
        "accruals",
        "earnings_stability",
        "margin_change",
        "financial_roe",
        "financial_roa",
        "financial_earnings_stability",
        "bank_net_interest_margin",
        "bank_npl_ratio",
        "bank_provision_coverage_ratio",
        "bank_capital_adequacy_ratio",
        "bank_loan_deposit_ratio",
        "broker_proprietary_risk_ratio",
        "broker_leverage_ratio",
        "broker_net_capital_ratio",
        "insurance_solvency_ratio",
        "insurance_combined_ratio",
        "insurance_reserve_coverage_ratio",
    ),
    "value": (
        "ep",
        "bp",
        "sales_yield",
        "dividend_yield",
        "fcf_yield",
        "loss_profitability",
        "valuation_percentile",
        "bank_ep",
        "bank_bp",
        "broker_ep",
        "broker_bp",
        "insurance_ep",
        "insurance_bp",
    ),
    "momentum": (
        "momentum_12_1",
        "momentum_6_1",
        "short_reversal",
        "residual_momentum",
    ),
    "trend": ("trend",),
    "lowvol": (
        "volatility_60",
        "volatility_120",
        "max_drawdown_120",
        "residual_volatility",
    ),
}
DATASET_FACTORS = {
    "income": {
        "gross_margin",
        "net_margin",
        "sales_yield",
        "loss_profitability",
    },
    "balancesheet": {"debt_ratio", "accruals", "bp"},
    "cashflow": {"cash_conversion_assets", "accruals", "fcf_yield"},
    "fina_indicator": {
        "roe",
        "roa",
        "gross_margin",
        "net_margin",
        "earnings_stability",
        "margin_change",
        "financial_roe",
        "financial_roa",
        "financial_earnings_stability",
    },
    "daily_basic": {
        "ep",
        "bp",
        "sales_yield",
        "dividend_yield",
        "valuation_percentile",
    },
}


def _latest_pit_sources(
    *,
    code: str,
    signal_date,
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    root = Path(get_settings().research_data_dir)
    system_as_of = datetime.now(UTC)
    for dataset in DATASET_FACTORS:
        rows = pit_warehouse.query_as_of(
            root,
            dataset=dataset,
            code=code,
            economic_as_of=signal_date,
            system_as_of=system_as_of,
            limit=10_000,
        )
        if not rows:
            continue
        row = rows[-1]
        result[dataset] = {
            "effective_date": str(row.get("effective_date")),
            "available_date": str(row.get("available_date")),
            "system_valid_from": str(row.get("system_valid_from")),
            "source_file": row.get("source_file"),
            "source_hash": row.get("source_hash"),
            "pit_model_version": row.get("pit_model_version"),
        }
    return result


def export_signal_lineage(
    db: Session,
    *,
    signal_id: int,
    code: str,
) -> dict[str, object]:
    signal = db.get(StockPaperSignal, signal_id)
    if signal is None:
        raise ValueError("信号不存在")
    normalized_code = code.split(".")[0]
    run = db.get(StockPaperRun, signal.run_id) if signal.run_id else None
    account = db.get(StockPaperAccount, signal.account_id)
    if account is None:
        raise ValueError("信号账户不存在")
    version = db.get(StrategyVersion, account.strategy_version_id)
    if version is None:
        raise ValueError("信号策略版本不存在")
    snapshots = list((run.result if run else {}).get("factor_snapshot") or [])
    factor = next(
        (
            item
            for item in snapshots
            if str(item.get("code")) == normalized_code
        ),
        None,
    )
    if factor is None:
        raise ValueError("该证券不在信号的全量因子快照中")
    selected_item = next(
        (
            item
            for item in signal.items
            if str(item.get("code")) == normalized_code
        ),
        {},
    )
    raw = dict(factor.get("raw") or {})
    zscores = dict(factor.get("zscores") or {})
    factor_metadata = dict(factor.get("factor_metadata") or {})
    pit_sources = _latest_pit_sources(
        code=normalized_code,
        signal_date=signal.signal_date,
    )
    families: dict[str, object] = {}
    for family, names in FACTOR_FAMILIES.items():
        subfactors = []
        for name in names:
            datasets = [
                {
                    "dataset": dataset,
                    **pit_sources[dataset],
                }
                for dataset, factors in DATASET_FACTORS.items()
                if name in factors and dataset in pit_sources
            ]
            if not datasets:
                datasets = [
                    {
                        "dataset": "daily_price_history",
                        "source": "governed_research_parquet",
                    }
                ]
            subfactors.append(
                {
                    "name": name,
                    "raw_value": raw.get(name),
                    "normalized_value": zscores.get(name),
                    "calculation_metadata": factor_metadata.get(name),
                    "sources": datasets,
                }
            )
        families[family] = {
            "family_score": selected_item.get(family),
            "configured_weight": (
                version.params.get("factor_weights", {}).get(family)
            ),
            "subfactors": subfactors,
        }
    issues = db.scalars(
        select(DataQualityIssue).where(
            DataQualityIssue.code == normalized_code
        )
    ).all()
    corrections_by_issue = {
        row.issue_id: row
        for row in db.scalars(
            select(DataCorrection).where(
                DataCorrection.issue_id.in_([item.id for item in issues])
            )
        ).all()
    } if issues else {}
    file_accesses = db.scalars(
        select(DataFileAccessLog).where(
            DataFileAccessLog.strategy_version_id == version.id,
            DataFileAccessLog.relative_path.contains(normalized_code),
        )
    ).all()
    position = db.scalar(
        select(StockPaperPosition).where(
            StockPaperPosition.account_id == account.id,
            StockPaperPosition.stock_code == normalized_code,
        )
    )
    return {
        "schema_version": "SIGNAL_LINEAGE_V1",
        "signal": {
            "signal_id": signal.id,
            "signal_date": signal.signal_date.isoformat(),
            "code": normalized_code,
            "selected": bool(factor.get("selected")),
            "rank": factor.get("rank"),
            "target_weight": signal.target_weights.get(normalized_code, 0.0),
            "composite_score": factor.get("composite"),
            "filter_reasons": factor.get("filter_reasons", []),
        },
        "holding": {
            "shares": float(position.shares) if position else 0.0,
            "cost": float(position.cost) if position else 0.0,
            "status": position.status if position else "not_held",
        },
        "families": families,
        "calculation_metadata": factor_metadata,
        "model_structure": factor.get("model_structure", {}),
        "normalized_sources": pit_sources,
        "quality_and_corrections": [
            {
                "issue_id": issue.id,
                "dataset": issue.dataset,
                "field_name": issue.field_name,
                "rule": issue.rule,
                "status": issue.status,
                "original_value": issue.original_value,
                "correction": (
                    {
                        "corrected_value": correction.corrected_value,
                        "rule": correction.correction_rule,
                        "actor": correction.actor,
                        "evidence_sha256": correction.evidence_sha256,
                    }
                    if (correction := corrections_by_issue.get(issue.id))
                    else None
                ),
            }
            for issue in issues
        ],
        "runtime_file_accesses": [
            {
                "path": row.relative_path,
                "size_bytes": row.observed_size_bytes,
                "sha256": row.observed_sha256,
                "status": row.status,
            }
            for row in file_accesses
        ],
        "experiment": {
            "strategy_version_id": version.id,
            "strategy_name": version.name,
            "mandate_version": version.mandate.get("version"),
            "mandate_sha256": version.mandate_sha256,
            "parameters": version.params,
            "git_sha": version.params.get("git_sha"),
            "candidate_sha256": version.params.get("candidate_sha256"),
            "data_snapshot_sha256": version.params.get(
                "stocktoday_manifest_sha256"
            ),
        },
    }
