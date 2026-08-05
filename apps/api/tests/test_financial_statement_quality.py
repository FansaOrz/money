"""财务勾稽、单位、报告期与正式因子隔离测试。"""

from datetime import date

from app.services.financial_statement_quality import (
    UNIT_REGISTRY,
    assess_statement_bundle,
    market_cap_to_cny,
)


def _rows() -> dict[str, dict[str, object]]:
    return {
        "income": {
            "report_type": "1",
            "update_flag": "0",
        },
        "balancesheet": {
            "report_type": "1",
            "total_assets": 1000.0,
            "total_liab": 600.0,
            "total_hldr_eqy_inc_min_int": 400.0,
            "total_liab_hldr_eqy": 1000.0,
            "update_flag": "0",
        },
        "cashflow": {
            "report_type": "1",
            "c_cash_equ_beg_period": 100.0,
            "n_incr_cash_cash_equ": 20.0,
            "c_cash_equ_end_period": 120.0,
            "update_flag": "0",
        },
        "fina_indicator": {},
    }


def test_valid_statement_bundle_and_unit_conversion() -> None:
    result = assess_statement_bundle(
        period=date(2025, 12, 31),
        rows=_rows(),
    )
    assert result.formal_factor_usable is True
    assert result.flow_basis == "year_to_date"
    assert result.audit_opinion == "unknown"
    assert market_cap_to_cny(123.0) == 1_230_000.0


def test_equation_failure_and_nonstandard_period_block_formal_factor() -> None:
    rows = _rows()
    rows["balancesheet"]["total_liab_hldr_eqy"] = 800.0
    result = assess_statement_bundle(
        period=date(2025, 11, 30),
        rows=rows,
    )
    assert result.formal_factor_usable is False
    assert any("资产" in reason for reason in result.errors)
    assert any("非标准报告期" in reason for reason in result.errors)


def test_uncertain_unit_blocks_formal_factor_and_restatement_is_tagged() -> None:
    rows = _rows()
    rows["income"]["update_flag"] = "1"
    units = dict(UNIT_REGISTRY)
    units.pop("cashflow")
    result = assess_statement_bundle(
        period=date(2025, 9, 30),
        rows=rows,
        units=units,
    )
    assert result.formal_factor_usable is False
    assert result.correction_status == "restated"
    assert any("单位定义缺失" in reason for reason in result.errors)

