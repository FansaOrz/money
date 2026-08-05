"""银行、券商、保险独立特征字典与缺数排除测试。"""

from datetime import date
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.services.financial_sector_model import (
    FEATURE_DICTIONARIES,
    assess_financial_sector,
)
from app.services.quant_data_governance import register_financial_sector_metrics
from app.services.stock_repository import SqlStockRepository
from app.services.stock_repository import Fundamentals


def _valuation() -> Fundamentals:
    return Fundamentals(
        code="000001",
        available_at=date(2025, 4, 30),
        valuation_date=date(2025, 4, 30),
        ep=0.08,
        bp=0.9,
    )


def test_bank_model_requires_regulatory_risk_metrics() -> None:
    complete = Fundamentals(
        code="000001",
        available_at=date(2025, 4, 30),
        company_type="2",
        bank_net_interest_margin=0.018,
        bank_npl_ratio=0.012,
        bank_provision_coverage_ratio=2.8,
        bank_capital_adequacy_ratio=0.14,
    )
    result = assess_financial_sector(
        complete, industry="银行", valuation=_valuation()
    )
    assert result is not None and result.eligible
    assert "bank_npl_ratio" in result.used_features
    assert "bank_ep" in result.used_features
    missing = assess_financial_sector(
        Fundamentals(
            code="000001",
            available_at=date(2025, 4, 30),
            company_type="2",
            bank_net_interest_margin=0.018,
        ),
        industry="银行",
        valuation=_valuation(),
    )
    assert missing is not None and not missing.eligible
    assert "bank_npl_ratio" in missing.missing_required


def test_broker_and_insurance_use_distinct_feature_sets() -> None:
    broker = Fundamentals(
        code="600000",
        available_at=date(2025, 4, 30),
        company_type="4",
        broker_proprietary_risk_ratio=0.25,
        broker_leverage_ratio=3.0,
        broker_net_capital_ratio=0.22,
    )
    insurance = Fundamentals(
        code="601000",
        available_at=date(2025, 4, 30),
        company_type="3",
        insurance_solvency_ratio=2.0,
        insurance_combined_ratio=0.96,
        insurance_reserve_coverage_ratio=1.2,
    )
    broker_result = assess_financial_sector(
        broker, industry="证券", valuation=_valuation()
    )
    insurance_result = assess_financial_sector(
        insurance, industry="保险", valuation=_valuation()
    )
    assert broker_result is not None and broker_result.eligible
    assert insurance_result is not None and insurance_result.eligible
    assert set(broker_result.values).isdisjoint(insurance_result.values)
    assert set(FEATURE_DICTIONARIES) == {"bank", "broker", "insurance"}


def test_sector_metric_registry_is_point_in_time(
    db_session: Session,
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "regulatory.json"
    evidence.write_text("{}", encoding="utf-8")
    register_financial_sector_metrics(
        db_session,
        code="000001",
        report_period=date(2025, 3, 31),
        available_at=datetime(2025, 4, 20, tzinfo=UTC),
        metrics={
            "bank_net_interest_margin": 0.018,
            "bank_npl_ratio": 0.012,
        },
        source="exchange",
        source_file=evidence,
    )
    repository = SqlStockRepository(db_session)
    before = repository._financial_sector_metrics(
        ["000001"], date(2025, 4, 19)
    )
    after = repository._financial_sector_metrics(
        ["000001"], date(2025, 4, 20)
    )
    assert before == {}
    assert after[("000001", date(2025, 3, 31))]["bank_npl_ratio"] == 0.012
