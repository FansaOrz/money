"""因子健康门禁必须按模型适用范围统计，而不是统一使用全市场分母。"""

from datetime import date

from sqlalchemy import select

from app.models import FactorHealthReport
from app.services.factor_health import persist_factor_health_reports
from app.services.stock_factors import FactorResult


def _result(
    index: int,
    *,
    industry: str,
    sector: str,
    raw: dict[str, float | None],
    model_eligible: bool = True,
    reason: str | None = None,
) -> FactorResult:
    return FactorResult(
        code=f"{index:06d}",
        name=f"股票{index}",
        industry=industry,
        raw=raw,
        model_eligible=model_eligible,
        model_structure={"sector": sector, "reason": reason},
    )


def test_health_scope_uses_family_policy_and_optional_coverage_does_not_block(
    db_session,
) -> None:
    industrial = [
        _result(
            index,
            industry="工业",
            sector="industrial_or_legacy",
            raw={
                "roe": 0.10 + index / 10_000,
                "margin_change": None,
                "financial_roe": None,
                "bank_net_interest_margin": None,
            },
        )
        for index in range(100)
    ]
    banks = [
        _result(
            index + 100,
            industry="银行",
            sector="bank",
            raw={
                "roe": None,
                "margin_change": None,
                "financial_roe": None,
                "bank_net_interest_margin": 0.02 + index / 10_000,
            },
        )
        for index in range(30)
    ]
    excluded_brokers = [
        _result(
            index + 130,
            industry="证券",
            sector="broker",
            raw={"broker_net_capital_ratio": None},
            model_eligible=False,
            reason="broker 专用模型缺少必需字段：broker_net_capital_ratio，显式排除",
        )
        for index in range(25)
    ]

    reports = persist_factor_health_reports(
        db_session,
        [*industrial, *banks, *excluded_brokers],
        signal_date=date(2026, 8, 6),
        strategy_version_id=None,
        direction_map={
            "roe": 1,
            "margin_change": 1,
            "financial_roe": 1,
            "bank_net_interest_margin": 1,
            "broker_net_capital_ratio": 1,
        },
    )
    by_factor = {report.factor: report for report in reports}

    assert by_factor["roe"].total == 100
    assert by_factor["roe"].coverage == 1.0
    assert by_factor["roe"].blocked is False
    assert by_factor["margin_change"].total == 100
    assert by_factor["margin_change"].coverage == 0.0
    assert by_factor["margin_change"].blocked is False
    assert by_factor["financial_roe"].total == 0
    assert by_factor["financial_roe"].blocked is False
    assert by_factor["bank_net_interest_margin"].total == 30
    assert by_factor["bank_net_interest_margin"].coverage == 1.0
    assert by_factor["bank_net_interest_margin"].blocked is False
    assert by_factor["broker_net_capital_ratio"].total == 0
    assert by_factor["broker_net_capital_ratio"].blocked is False

    persisted = {
        row.factor_name: row
        for row in db_session.scalars(select(FactorHealthReport)).all()
    }
    assert persisted["roe"].statistics["universe_total"] == 155
    assert persisted["roe"].statistics["applicable_total"] == 100
    assert persisted["roe"].statistics["required"] is True
    assert persisted["margin_change"].statistics["required"] is False
    assert persisted["financial_roe"].statistics["applicable_total"] == 0
    assert persisted["bank_net_interest_margin"].statistics["applicable_total"] == 30
    broker = persisted["broker_net_capital_ratio"].statistics
    assert broker["applicable_total"] == 0
    assert broker["structural_applicable_total"] == 25
    assert broker["model_ineligible_total"] == 25
    assert broker["required"] is True
    assert sum(broker["model_ineligible_reasons"].values()) == 25
