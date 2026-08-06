"""银行、券商、保险独立特征字典与缺数排除测试。"""

from datetime import date
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.services.financial_sector_model import (
    FEATURE_DICTIONARIES,
    assess_financial_sector,
)
from app.services.eastmoney_financial_sector import normalize_bank_indicator
from app.services import stock_factors
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
    result = assess_financial_sector(complete, industry="银行", valuation=_valuation())
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
    before = repository._financial_sector_metrics(["000001"], date(2025, 4, 19))
    after = repository._financial_sector_metrics(["000001"], date(2025, 4, 20))
    assert before == {}
    assert after[("000001", date(2025, 3, 31))]["bank_npl_ratio"] == 0.012


def test_eastmoney_bank_indicator_normalizes_percent_and_notice_date() -> None:
    result = normalize_bank_indicator(
        {
            "REPORT_DATE": "2021-06-30 00:00:00",
            "NOTICE_DATE": "2021-08-20 00:00:00",
            "NET_INTEREST_MARGIN": 2.83,
            "NONPERLOAN": 1.08,
            "BLDKBBL": 259.53,
            "NEWCAPITALADER": 12.58,
            "LTDRR": 1.002866,
        }
    )
    assert result is not None
    period, available_at, metrics = result
    assert period == date(2021, 6, 30)
    assert available_at == datetime(2021, 8, 20, tzinfo=UTC)
    assert metrics["bank_net_interest_margin"] == pytest.approx(0.0283)
    assert metrics["bank_npl_ratio"] == pytest.approx(0.0108)
    assert metrics["bank_provision_coverage_ratio"] == pytest.approx(2.5953)
    assert metrics["bank_capital_adequacy_ratio"] == pytest.approx(0.1258)
    assert metrics["bank_loan_deposit_ratio"] == pytest.approx(1.002866)


def test_financial_sector_snapshot_carries_each_metric_only_from_past() -> None:
    half_year = Fundamentals(
        code="000001",
        available_at=date(2021, 8, 20),
        period=date(2021, 6, 30),
        company_type="2",
        bank_net_interest_margin=0.0283,
        bank_npl_ratio=0.0108,
        bank_provision_coverage_ratio=2.5953,
        bank_capital_adequacy_ratio=0.1258,
        sector_metric_sources=("half-year",),
    )
    third_quarter = Fundamentals(
        code="000001",
        available_at=date(2021, 10, 21),
        period=date(2021, 9, 30),
        company_type="2",
        bank_net_interest_margin=0.0281,
        bank_npl_ratio=0.0105,
        bank_provision_coverage_ratio=2.6835,
        bank_capital_adequacy_ratio=None,
        sector_metric_sources=("third-quarter",),
    )
    fields = (
        "bank_net_interest_margin",
        "bank_npl_ratio",
        "bank_provision_coverage_ratio",
        "bank_capital_adequacy_ratio",
        "company_type",
    )
    before = stock_factors._latest_sector_snapshot(
        (half_year, third_quarter), date(2021, 10, 20), fields
    )
    after = stock_factors._latest_sector_snapshot(
        (half_year, third_quarter), date(2021, 10, 21), fields
    )
    assert before is not None and before.period == date(2021, 6, 30)
    assert after is not None and after.period == date(2021, 9, 30)
    assert after.bank_npl_ratio == 0.0105
    assert after.bank_capital_adequacy_ratio == 0.1258
    assert after.sector_metric_sources == ("half-year", "third-quarter")


def test_financial_sector_keeps_common_price_factor_families() -> None:
    result = stock_factors.FactorResult(
        code="000001",
        name="平安银行",
        industry="银行",
        model_structure={"sector": "bank"},
    )
    quality, _quality_required = stock_factors._family_policy(result, "quality")
    value, _value_required = stock_factors._family_policy(result, "value")
    momentum, momentum_required = stock_factors._family_policy(result, "momentum")
    trend, trend_required = stock_factors._family_policy(result, "trend")
    lowvol, lowvol_required = stock_factors._family_policy(result, "lowvol")
    assert "bank_net_interest_margin" in quality
    assert value == ["bank_ep", "bank_bp"]
    assert "momentum_12_1" in momentum
    assert momentum_required == {"momentum_12_1"}
    assert trend == ["trend"] and trend_required == {"trend"}
    assert "volatility_60" in lowvol
    assert lowvol_required == {"volatility_60"}
