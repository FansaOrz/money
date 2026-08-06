"""量化平台治理、任务、备份、生命周期与模拟 OMS/RMS 验收。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.models import (
    CorporateActionReviewCase,
    DataFieldProvenance,
    PersistentJob,
    QuantDataRecord,
    QuantImportRun,
    StrategyTransition,
    StrategyVersion,
)
from app.config import get_settings
from app.db.session import get_db
from app.main import create_app, create_tables
from app.services import (
    backup,
    benchmark_data,
    corporate_action_master,
    job_queue,
    oms,
    pit_warehouse,
    position_lots,
    stock_backtest,
    strategy_lifecycle,
    strategy_mandate,
    trading_rules,
)
from app.services import stock_validation
from app.services.stock_backtest import BacktestConfig, BacktestOutcome, MarketPanel
from app.services.stock_repository import SqlStockRepository, TradeCalendar
from app.services.quant_data_governance import (
    import_tushare_snapshot,
    register_corporate_action,
    resolve_field,
)


def _version(db_session, *, operational_only: bool = False) -> StrategyVersion:
    mandate = (
        strategy_mandate.operational_validation_mandate(
            strategy_name="治理测试策略",
            initial_capital=1_000_000,
            rebalance_days=20,
            top_n=30,
        )
        if operational_only
        else strategy_mandate.cn_stock_investment_mandate(
            strategy_name="治理测试策略",
            initial_capital=1_000_000,
            rebalance_days=20,
            top_n=30,
        )
    )
    version = StrategyVersion(
        name="治理测试策略",
        initial_capital=Decimal("1000000"),
        rebalance_interval=20,
        fee_rate=Decimal("0.001"),
        top_n=30,
        params={},
        mandate=mandate,
        mandate_sha256=strategy_mandate.mandate_sha256(mandate),
        status="research",
    )
    db_session.add(version)
    db_session.commit()
    db_session.refresh(version)
    return version


def test_field_source_priority_and_difference_warning() -> None:
    value, source, warnings = resolve_field(
        [("akshare", 10.5), ("tushare", 10.0)], "close"
    )
    assert value == 10.0
    assert source == "tushare"
    assert warnings and "差异" in warnings[0]


def test_unified_corporate_action_master_import_and_replay(
    db_session, tmp_path
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = tmp_path / "dividend"
    root.mkdir()
    source_file = root / "600001.SH.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "ts_code": "600001.SH",
                    "end_date": "20241231",
                    "ann_date": "20250301",
                    "imp_ann_date": "20250302",
                    "div_proc": "实施",
                    "stk_div": 0.1,
                    "cash_div": 0.5,
                    "cash_div_tax": 0.45,
                    "record_date": "20250309",
                    "ex_date": "20250310",
                    "pay_date": "20250312",
                    "div_listdate": "20250315",
                }
            ]
        ),
        source_file,
    )
    result = corporate_action_master.import_dividend_snapshot(
        db_session,
        root,
    )
    assert result["inserted"] == 3
    assert result["source_hashes"] == 1
    timeline = corporate_action_master.event_timeline(
        db_session,
        code="600001",
    )
    assert [item["payload"]["kind"] for item in timeline] == [
        "cash_entitlement",
        "share_distribution",
        "cash_payment",
    ]
    assert all(item["source_hash"] for item in timeline)
    assert all(
        item["payload"]["event_model_version"]
        == corporate_action_master.EVENT_MODEL_VERSION
        for item in timeline
    )
    repeated = corporate_action_master.import_dividend_snapshot(
        db_session,
        root,
    )
    assert repeated["inserted"] == 0
    assert repeated["skipped"] == 3
    review = CorporateActionReviewCase(
        code="600001",
        event_key="terminal-unknown",
        issue_type="terminal_consideration_unknown",
        status="open",
        reason="最终对价未知",
        conservative_value=0,
        evidence={"source_hash": timeline[0]["source_hash"]},
        resolution={},
        created_at=datetime.now(UTC),
    )
    db_session.add(review)
    db_session.commit()
    resolved = corporate_action_master.resolve_review_case(
        db_session,
        case_id=review.id,
        resolution={
            "terminal_type": "cash_liquidation",
            "official_price": 3.25,
            "official_document": "exchange-announcement.pdf",
        },
        operator="risk-reviewer",
    )
    assert resolved["status"] == "resolved"
    assert resolved["resolution"]["official_price"] == 3.25
    assert resolved["resolution"]["operator"] == "risk-reviewer"


def test_bitemporal_pit_reconstructs_pre_correction_system_view(
    db_session, tmp_path
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    research_root = tmp_path / "research"
    source_dir = research_root / "tushare_snapshot" / "stocks" / "daily_basic"
    source_dir.mkdir(parents=True)
    source_file = source_dir / "600001.SH.parquet"

    def write_value(pe_ttm: float) -> None:
        pq.write_table(
            pa.Table.from_pylist(
                [
                    {
                        "ts_code": "600001.SH",
                        "trade_date": "20250102",
                        "pe_ttm": pe_ttm,
                    }
                ]
            ),
            source_file,
        )

    write_value(10.0)
    first = pit_warehouse.build_pit_warehouse(
        db_session,
        research_root=research_root,
        datasets=["daily_basic"],
    )
    assert first["status"] == "success"
    before_correction = datetime.now(UTC)
    write_value(20.0)
    second = pit_warehouse.build_pit_warehouse(
        db_session,
        research_root=research_root,
        datasets=["daily_basic"],
    )
    assert second["status"] == "success"
    after_correction = datetime.now(UTC)
    historical = pit_warehouse.query_as_of(
        research_root,
        dataset="daily_basic",
        code="600001",
        economic_as_of=datetime(2025, 1, 2, tzinfo=UTC).date(),
        system_as_of=before_correction,
    )
    current = pit_warehouse.query_as_of(
        research_root,
        dataset="daily_basic",
        code="600001",
        economic_as_of=datetime(2025, 1, 2, tzinfo=UTC).date(),
        system_as_of=after_correction,
    )
    assert [row["pe_ttm"] for row in historical] == [10.0]
    assert [row["pe_ttm"] for row in current] == [20.0]
    assert historical[0]["source_hash"] != current[0]["source_hash"]


def test_versioned_exchange_quantity_rules_cover_star_and_boundaries() -> None:
    star = trading_rules.quantity_rule(
        "688001", datetime(2024, 1, 2, tzinfo=UTC).date()
    )
    assert star.buy_minimum == 200
    assert star.buy_increment == 1
    assert star.validate(side="buy", quantity=100) == ["买入最低申报 200 股"]
    assert not star.validate(side="buy", quantity=200)
    assert not star.validate(side="buy", quantity=201)
    main = trading_rules.quantity_rule(
        "600001", datetime(2024, 1, 2, tzinfo=UTC).date()
    )
    assert main.validate(side="buy", quantity=201) == ["买入必须按 100 股递增"]
    assert not main.validate(side="sell", quantity=51, held=51)
    assert main.validate(side="sell", quantity=1, held=51)
    assert (
        trading_rules.quantity_rule(
            "300001", datetime(2020, 8, 21, tzinfo=UTC).date()
        ).version
        != trading_rules.quantity_rule(
            "300001", datetime(2020, 8, 24, tzinfo=UTC).date()
        ).version
    )
    with pytest.raises(ValueError, match="北交所成立前"):
        trading_rules.quantity_rule("830001", datetime(2021, 11, 12, tzinfo=UTC).date())


def test_oms_uses_same_star_quantity_rule(db_session) -> None:
    rejected = oms.risk_check(
        db_session,
        oms.OrderRequest(
            client_order_id="STAR-100",
            account="SIM-STAR",
            code="688001",
            side="buy",
            quantity=100,
            reference_price=10,
        ),
        available_cash=100_000,
        available_position=0,
    )
    assert not rejected["passed"]
    assert "买入最低申报 200 股" in rejected["reasons"]
    accepted = oms.risk_check(
        db_session,
        oms.OrderRequest(
            client_order_id="STAR-201",
            account="SIM-STAR",
            code="688001",
            side="buy",
            quantity=201,
            reference_price=10,
        ),
        available_cash=100_000,
        available_position=0,
    )
    assert accepted["passed"]
    assert accepted["quantity_rule_version"] == "SSE_STAR_QTY_20190722_V1"


def test_t_plus_one_fifo_lot_ledger_blocks_same_day_sale() -> None:
    day = datetime(2026, 8, 5, tzinfo=UTC).date()
    next_day = day + timedelta(days=1)
    ledger = position_lots.LotLedger()
    ledger.buy(
        "600001",
        200,
        2_005,
        acquired_date=day,
        sellable_date=next_day,
        source="test-fill",
    )
    assert ledger.total("600001") == 200
    assert ledger.available("600001", day) == 0
    with pytest.raises(ValueError, match="T\\+1"):
        ledger.sell("600001", 100, trade_date=day)
    assert ledger.available("600001", next_day) == 200
    consumed = ledger.sell("600001", 100, trade_date=next_day)
    assert consumed[0]["acquired_date"] == day.isoformat()
    assert consumed[0]["cost"] == pytest.approx(1002.5)
    assert ledger.total("600001") == 100


def test_official_total_return_benchmark_is_hashed_and_governed(
    db_session, tmp_path
) -> None:
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {
                        "tradeDate": "20240102",
                        "indexCode": "H00906",
                        "indexNameCnAll": "中证800全收益指数",
                        "close": 5000.1,
                    },
                    {
                        "tradeDate": "20240103",
                        "indexCode": "H00906",
                        "indexNameCnAll": "中证800全收益指数",
                        "close": 5050.2,
                    },
                ]
            }

    detail = benchmark_data.sync_csindex_total_return(
        db_session,
        code="H00906",
        start=datetime(2024, 1, 1, tzinfo=UTC).date(),
        end=datetime(2024, 1, 3, tzinfo=UTC).date(),
        data_root=tmp_path,
        request_get=lambda *_args, **_kwargs: Response(),
    )
    assert detail["return_kind"] == "gross_total_return"
    source_file = tmp_path / str(detail["source_file"])
    assert source_file.is_file()
    assert hashlib.sha256(source_file.read_bytes()).hexdigest() == detail["source_hash"]
    series = SqlStockRepository(db_session, data_root=tmp_path).benchmark_series(
        "H00906"
    )
    assert series is not None
    assert series.name == "中证800全收益指数"
    assert series.return_kind == "gross_total_return"
    assert len(series.points) == 2
    source_file.write_text("tampered", encoding="utf-8")
    assert (
        SqlStockRepository(db_session, data_root=tmp_path).benchmark_series("H00906")
        is None
    )


def test_required_official_benchmark_rejects_missing_calendar_day() -> None:
    days = [datetime(2024, 1, day, tzinfo=UTC).date() for day in (2, 3, 4)]
    panel = MarketPanel(
        calendar=TradeCalendar(tuple(days)),
        bars_by_code={},
        bar_lookup={},
        index_series=[(days[0], 5000.0), (days[2], 5100.0)],
    )
    config = BacktestConfig(
        start=days[0],
        end=days[-1],
        benchmark_index="H00906",
        benchmark_required=True,
        benchmark_return_kind="gross_total_return",
    )
    with pytest.raises(stock_backtest.BacktestError, match="禁止前值填充"):
        stock_backtest._build_benchmark(
            panel,
            [],
            days,
            config,
            days[0],
            days[0],
        )
    complete = MarketPanel(
        calendar=panel.calendar,
        bars_by_code={},
        bar_lookup={},
        index_series=[
            (days[0], 5000.0),
            (days[1], 5050.0),
            (days[2], 5100.0),
        ],
    )
    curve, kind, warnings = stock_backtest._build_benchmark(
        complete, [], days, config, days[0], days[0]
    )
    assert kind == "CSI800_TOTAL_RETURN"
    assert curve == pytest.approx([1.0, 1.01, 1.02])
    assert not warnings


def test_production_requires_api_key_and_blocks_readonly_mutation(
    db_session, monkeypatch
) -> None:
    monkeypatch.setenv("MONEY_ENVIRONMENT", "production")
    monkeypatch.setenv("MONEY_ADMIN_API_KEY", "admin-secret")
    monkeypatch.setenv("MONEY_READONLY_API_KEY", "readonly-secret")
    monkeypatch.setenv("MONEY_AUTO_CREATE_TABLES", "false")
    monkeypatch.setenv(
        "MONEY_DATABASE_URL",
        "postgresql+psycopg://money:test@localhost:5432/money",
    )
    get_settings.cache_clear()
    app = create_app()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            assert client.get("/api/health").status_code == 401
            assert (
                client.get(
                    "/api/health",
                    headers={"X-API-Key": "readonly-secret"},
                ).status_code
                == 200
            )
            assert (
                client.post(
                    "/api/quant-governance/accounts/SIM/kill-switch",
                    params={"enabled": "true"},
                    headers={"X-API-Key": "readonly-secret"},
                ).status_code
                == 403
            )
        with pytest.raises(RuntimeError, match="production 禁止 create_all"):
            create_tables()
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_deep_health_and_structured_metrics_are_queryable(client) -> None:
    health = client.get("/api/health/deep")
    assert health.status_code == 200
    checks = health.json()["checks"]
    assert {
        "database",
        "research_repository",
        "stock_freshness",
        "scheduler",
        "stock_paper",
    } <= set(checks)
    metrics = client.get("/api/metrics")
    assert metrics.status_code == 200
    assert "persistent_jobs_queued" in metrics.json()
    assert "sync_runs_failed_24h" in metrics.json()
    assert "sync_runs_partial_24h" in metrics.json()


def test_tushare_snapshot_import_is_pit_provenanced_and_idempotent(
    db_session, tmp_path
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = tmp_path / "tushare_snapshot" / "stocks" / "adj_factor" / "600001.SH.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "ts_code": "600001.SH",
                    "trade_date": "20240102",
                    "adj_factor": 1.25,
                }
            ]
        ),
        path,
    )
    first = import_tushare_snapshot(db_session, tmp_path, ["adj_factor"])
    second = import_tushare_snapshot(db_session, tmp_path, ["adj_factor"])
    assert first["imported"] == 1
    assert second["skipped"] == 1
    record = db_session.query(QuantDataRecord).one()
    assert record.effective_date.isoformat() == "2024-01-02"
    assert record.source_hash and record.payload["adj_factor"] == 1.25
    provenance = db_session.query(DataFieldProvenance).all()
    assert {row.field_name for row in provenance} == {
        "ts_code",
        "trade_date",
        "adj_factor",
    }
    assert db_session.query(QuantImportRun).count() == 2


def test_official_corporate_action_registration_is_provenanced(
    db_session, tmp_path
) -> None:
    source = tmp_path / "announcement.pdf"
    source.write_bytes(b"%PDF-test")
    first = register_corporate_action(
        db_session,
        code="000527.SZ",
        effective_date=datetime(2013, 9, 18, tzinfo=UTC).date(),
        available_at=datetime(2013, 9, 12, tzinfo=UTC),
        payload={
            "kind": "merger",
            "successor_code": "000333",
            "share_ratio": 0.3447,
        },
        source="cninfo",
        source_file=source,
    )
    second = register_corporate_action(
        db_session,
        code="000527",
        effective_date=datetime(2013, 9, 18, tzinfo=UTC).date(),
        available_at=datetime(2013, 9, 12, tzinfo=UTC),
        payload={
            "kind": "merger",
            "successor_code": "000333",
            "share_ratio": 0.3447,
        },
        source="cninfo",
        source_file=source,
    )
    assert first.id == second.id
    assert first.source_hash
    assert {
        row.field_name
        for row in db_session.query(DataFieldProvenance).filter_by(record_id=first.id)
    } == {"kind", "successor_code", "share_ratio"}


def test_persistent_job_idempotency_retry_and_recovery(db_session) -> None:
    scheduled = datetime.now(UTC) - timedelta(minutes=1)
    first = job_queue.enqueue(db_session, "stock_daily", scheduled)
    same = job_queue.enqueue(db_session, "stock_daily", scheduled)
    assert first.id == same.id
    claimed = job_queue.claim(db_session, now=datetime.now(UTC), lease_seconds=1)
    assert claimed is not None and claimed.status == "running"
    claimed.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    assert job_queue.recover_expired(db_session, datetime.now(UTC)) == 1
    assert db_session.get(PersistentJob, claimed.id).status == "queued"


def test_strategy_lifecycle_blocks_skips_and_requires_gates(db_session) -> None:
    version = _version(db_session, operational_only=True)
    with pytest.raises(ValueError, match="不允许"):
        strategy_lifecycle.transition(
            db_session,
            version.id,
            "live",
            evidence={},
            actor="tester",
            reason="非法跳级",
        )
    with pytest.raises(ValueError, match="门禁未通过"):
        strategy_lifecycle.transition(
            db_session,
            version.id,
            "operational_validated",
            evidence={"data_coverage": 0.90},
            actor="tester",
            reason="数据不足",
        )
    version = strategy_lifecycle.transition(
        db_session,
        version.id,
        "operational_validated",
        evidence={
            "data_coverage": 0.99,
            "holdout_evaluations": 1,
            "walkforward_folds": 3,
            "holdout_sharpe": 0.5,
            "holdout_trade_count": 12,
            "holdout_turnover": 0.4,
            "validation_scope": "operational_only",
        },
        actor="tester",
        reason="门禁通过",
    )
    assert version.status == "operational_validated"
    version = strategy_lifecycle.transition(
        db_session,
        version.id,
        "paper_operational_validation",
        evidence={"experiment_snapshot_complete": True},
        actor="tester",
        reason="只运行验证",
    )
    assert version.status == "paper_operational_validation"
    with pytest.raises(ValueError, match="不允许"):
        strategy_lifecycle.transition(
            db_session,
            version.id,
            "approved",
            evidence={
                "paper_trading_days": 42,
                "reconciliation_clean": True,
                "operational_failures": 0,
                "investment_validation_passed": True,
            },
            actor="tester",
            reason="运行验证禁止转实盘批准",
        )


def test_operational_validation_rejects_cash_only_holdout(db_session) -> None:
    version = _version(db_session, operational_only=True)
    with pytest.raises(ValueError, match="没有任何真实模拟成交"):
        strategy_lifecycle.transition(
            db_session,
            version.id,
            "operational_validated",
            evidence={
                "data_coverage": 0.99,
                "holdout_evaluations": 1,
                "walkforward_folds": 3,
                "holdout_sharpe": 0.5,
                "holdout_trade_count": 0,
                "holdout_turnover": 0.0,
                "validation_scope": "operational_only",
            },
            actor="tester",
            reason="现金策略不能验证交易链路",
        )


def test_stock_strategy_validation_evidence_cannot_be_faked(db_session) -> None:
    version = _version(db_session, operational_only=True)
    common = {
        "data_coverage": 0.99,
        "holdout_evaluations": 1,
        "walkforward_folds": 3,
        "holdout_sharpe": 0.5,
        "holdout_trade_count": 12,
        "holdout_turnover": 0.4,
        "validation_scope": "operational_only",
        "validation_sha256": "frozen-hash",
        "generated_by": "stock_validation.run_stock_walk_forward",
        "benchmark_kind": "index_total_return:000906.SH",
        "benchmark_code": "000906.SH",
        "benchmark_curve_sha256": "benchmark-curve-hash",
        "benchmark_start_date": "2022-01-04",
        "benchmark_end_date": "2025-12-31",
        "benchmark_curve_points": 970,
        "strategy_curve_sha256": "strategy-curve-hash",
        "benchmark_return_kind": "gross_total_return",
        "benchmark_source_hashes": ["source-hash"],
        "benchmark_source_files": ["benchmarks/H00906.json"],
        "comparator_metrics": {"CSI800_TOTAL_RETURN": {"net_excess_return": 0.01}},
        "limit_data_coverage": 0.995,
    }
    version.params = {
        "model_version": "stock_rules_v4",
        "validation_sha256": "frozen-hash",
        "operational_validation_evidence": common,
    }
    db_session.commit()
    with pytest.raises(ValueError, match="证据哈希"):
        strategy_lifecycle.transition(
            db_session,
            version.id,
            "operational_validated",
            evidence={
                **common,
                "validation_sha256": "forged",
                "generated_by": "manual",
            },
            actor="tester",
            reason="伪造验证",
        )
    version = strategy_lifecycle.transition(
        db_session,
        version.id,
        "operational_validated",
        evidence=common,
        actor="system",
        reason="系统验证",
    )
    assert version.status == "operational_validated"


def test_stock_strategy_v5_cannot_bypass_frozen_evidence(db_session) -> None:
    version = _version(db_session, operational_only=True)
    version.params = {
        "model_version": "stock_rules_v5",
        "validation_sha256": "frozen-hash",
    }
    db_session.commit()

    with pytest.raises(ValueError, match="冻结字段"):
        strategy_lifecycle.transition(
            db_session,
            version.id,
            "operational_validated",
            evidence={
                "data_coverage": 0.99,
                "holdout_evaluations": 1,
                "walkforward_folds": 3,
                "holdout_sharpe": 0.5,
                "holdout_trade_count": 12,
                "holdout_turnover": 0.4,
                "validation_scope": "operational_only",
            },
            actor="attacker",
            reason="V5 不能绕过 V4 已有的冻结证据门禁",
        )


@pytest.mark.parametrize(
    ("field", "forged"),
    [
        ("benchmark_code", "000300.SH"),
        ("benchmark_curve_sha256", "forged-curve"),
        ("benchmark_start_date", "1990-01-01"),
    ],
)
def test_stock_strategy_frozen_benchmark_evidence_rejects_tampering(
    db_session, field, forged
) -> None:
    version = _version(db_session, operational_only=True)
    evidence = {
        "data_coverage": 0.99,
        "holdout_evaluations": 1,
        "walkforward_folds": 3,
        "holdout_sharpe": 0.5,
        "holdout_trade_count": 12,
        "holdout_turnover": 0.4,
        "validation_scope": "operational_only",
        "validation_sha256": "frozen-hash",
        "generated_by": "stock_validation.run_stock_walk_forward",
        "benchmark_kind": "index_total_return:000906.SH",
        "benchmark_code": "000906.SH",
        "benchmark_curve_sha256": "benchmark-curve-hash",
        "benchmark_start_date": "2022-01-04",
        "benchmark_end_date": "2025-12-31",
        "benchmark_curve_points": 970,
        "strategy_curve_sha256": "strategy-curve-hash",
        "benchmark_return_kind": "gross_total_return",
        "benchmark_source_hashes": ["source-hash"],
        "benchmark_source_files": ["benchmarks/H00906.json"],
        "comparator_metrics": {"CSI800_TOTAL_RETURN": {"net_excess_return": 0.01}},
        "limit_data_coverage": 0.995,
    }
    version.params = {
        "model_version": "stock_rules_v4",
        "validation_sha256": "frozen-hash",
        "operational_validation_evidence": evidence,
    }
    db_session.commit()
    with pytest.raises(ValueError, match=field):
        strategy_lifecycle.transition(
            db_session,
            version.id,
            "operational_validated",
            evidence={**evidence, field: forged},
            actor="attacker",
            reason="篡改正式基准证据",
        )


def _passing_investment_evidence() -> dict[str, object]:
    return {
        "data_coverage": 0.995,
        "holdout_evaluations": 1,
        "walkforward_folds": 5,
        "holdout_sharpe": 0.8,
        "net_excess_return": 0.08,
        "active_sharpe": 0.7,
        "active_return_ci_lower": 0.0001,
        "regression_alpha_ci_lower": 0.02,
        "max_drawdown": -0.15,
        "rank_ic_mean": 0.04,
        "rank_icir": 0.6,
        "rank_ic_p_value": 0.03,
        "rank_ic_ci_lower": 0.01,
        "quintile_monotonicity": 0.8,
        "top_bottom_spread": 0.03,
        "deflated_sharpe_probability": 0.97,
        "probabilistic_sharpe_probability": 0.98,
        "probability_backtest_overfitting": 0.10,
        "multiple_testing_fdr": 0.05,
        "cost_2x_excess_return": 0.03,
        "robustness_passed": True,
        "max_single_period_alpha_contribution": 0.40,
        "worst_regime_excess_return": -0.02,
        "worst_year_excess_return": -0.01,
        "benchmark_kind": "CSI800_TOTAL_RETURN",
    }


def test_investment_validation_rejects_negative_or_incomplete_alpha(db_session) -> None:
    version = _version(db_session)
    weak = _passing_investment_evidence()
    weak.update(
        {
            "holdout_sharpe": -1.0,
            "net_excess_return": -0.10,
            "active_sharpe": -1.2,
            "rank_ic_p_value": 0.40,
            "rank_ic_ci_lower": -0.02,
            "quintile_monotonicity": 0.20,
            "top_bottom_spread": -0.01,
        }
    )
    with pytest.raises(ValueError, match="晋级门禁未通过"):
        strategy_lifecycle.transition(
            db_session,
            version.id,
            "investment_validated",
            evidence=weak,
            actor="tester",
            reason="负Alpha不得通过",
        )
    transition = (
        db_session.query(StrategyTransition)
        .filter_by(strategy_version_id=version.id)
        .order_by(StrategyTransition.id.desc())
        .one()
    )
    assert not transition.approved
    assert not transition.gates["gate_results"]["net_excess_return"]["passed"]
    assert not transition.gates["gate_results"]["rank_ic_p_value"]["passed"]
    assert db_session.get(StrategyVersion, version.id).status == "research"


def test_investment_validation_records_thresholds_and_allows_strong_evidence(
    db_session,
) -> None:
    version = _version(db_session)
    version = strategy_lifecycle.transition(
        db_session,
        version.id,
        "investment_validated",
        evidence=_passing_investment_evidence(),
        actor="validator",
        reason="投资有效性全部门禁通过",
    )
    assert version.status == "investment_validated"
    transition = (
        db_session.query(StrategyTransition)
        .filter_by(
            strategy_version_id=version.id,
            approved=True,
        )
        .one()
    )
    assert transition.gates["mandate_sha256"] == version.mandate_sha256
    assert all(item["passed"] for item in transition.gates["gate_results"].values())


def test_strategy_mandate_is_immutable_and_hash_verified(db_session) -> None:
    version = _version(db_session)
    original = dict(version.mandate)
    version.mandate = {**original, "name": "被篡改"}
    with pytest.raises(ValueError, match="投资任务书不可原地修改"):
        db_session.commit()
    db_session.rollback()

    version = db_session.get(StrategyVersion, version.id)
    version.mandate_sha256 = "0" * 64
    with pytest.raises(ValueError, match="任务书哈希不可原地修改"):
        db_session.commit()
    db_session.rollback()


def test_strategy_governance_api_exposes_mandate_and_failed_gate(
    client, db_session
) -> None:
    version = _version(db_session)
    weak = _passing_investment_evidence()
    weak["net_excess_return"] = -0.01
    with pytest.raises(ValueError):
        strategy_lifecycle.transition(
            db_session,
            version.id,
            "investment_validated",
            evidence=weak,
            actor="tester",
            reason="记录失败门禁",
        )
    response = client.get(f"/api/quant-governance/strategies/{version.id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mandate_sha256"] == version.mandate_sha256
    assert payload["investment_approval_eligible"] is True
    assert payload["validation_scope"] == "investment_effectiveness"
    assert payload["transitions"][-1]["approved"] is False
    assert (
        payload["transitions"][-1]["gates"]["gate_results"]["net_excess_return"][
            "passed"
        ]
        is False
    )


def test_simulated_oms_risk_fill_cancel_and_reconciliation(
    db_session, monkeypatch
) -> None:
    monkeypatch.setenv("MONEY_BROKER_ADAPTER", "simulated")
    from app.config import get_settings

    get_settings.cache_clear()
    ledger = oms.initialize_simulated_account(db_session, "SIM-A", 100_000)
    assert float(ledger.cash) == 100_000
    request = oms.OrderRequest(
        client_order_id="order-1",
        account="SIM-A",
        code="600001",
        side="buy",
        quantity=100,
        reference_price=10.0,
    )
    order = oms.submit_order(
        db_session, request, available_cash=0, available_position=0
    )
    assert order.adapter == "simulated"
    fill = oms.simulate_fill(
        db_session,
        order.id,
        quantity=100,
        price=10.0,
        fee=5.0,
        external_fill_id="fill-1",
    )
    assert fill.order_id == order.id
    result = oms.reconcile(
        db_session,
        "SIM-A",
        broker_cash=98_995.0,
        broker_positions={"600001": 100},
    )
    assert result["clean"]
    oms.set_kill_switch(
        db_session,
        "SIM-A",
        True,
        actor="risk-operator",
        approver="risk-approver",
    )
    with pytest.raises(ValueError, match="紧急停止"):
        oms.submit_order(
            db_session,
            oms.OrderRequest(
                client_order_id="order-2",
                account="SIM-A",
                code="600001",
                side="sell",
                quantity=100,
                reference_price=10.0,
            ),
            available_cash=0,
            available_position=100,
        )
    get_settings.cache_clear()


def test_sqlite_backup_verify_and_non_overwrite_restore(db_session, tmp_path) -> None:
    database_url = str(db_session.get_bind().url)
    research = tmp_path / "research"
    research.mkdir()
    (research / "marker.txt").write_text("ok", encoding="utf-8")
    destination = tmp_path / "backup"
    manifest = backup.create_backup(
        db_session,
        database_url=database_url,
        research_data_dir=research,
        destination=destination,
    )
    assert "database.sqlite3" in manifest["artifacts"]
    assert backup.verify_backup(destination)["ok"]
    ledger = json.loads(
        (destination / "strategy_ledger.json").read_text(encoding="utf-8")
    )
    assert "stock_paper_receivables" in ledger
    restored = backup.restore_to_new_directory(destination, tmp_path / "restored")
    assert (restored / "database.sqlite3").exists()
    with pytest.raises(FileExistsError):
        backup.restore_to_new_directory(destination, restored)
    with pytest.raises(ValueError, match="不能位于研究数据目录内部"):
        backup.create_backup(
            db_session,
            database_url=database_url,
            research_data_dir=research,
            destination=research / "backups" / "bad",
        )


def test_stock_walk_forward_has_embargo_and_holdout_once(monkeypatch) -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC).date()
    days = tuple(start + timedelta(days=index) for index in range(1000))

    class Repository:
        def trade_calendar(self, _start, _end):
            return TradeCalendar(days)

    calls: list[tuple[datetime.date, datetime.date, bool]] = []

    def fake_run_backtest(*, config, repository):
        calls.append((config.start, config.end, config.initial_signal))
        curve_days = [day for day in days if config.start <= day <= config.end]
        equity = [1_000_000.0 + index for index in range(len(curve_days))]
        returns = [
            equity[index] / equity[index - 1] - 1.0 for index in range(1, len(equity))
        ]
        return BacktestOutcome(
            calendar=curve_days,
            equity=equity,
            daily_returns=returns,
            benchmark=[1.0] * len(curve_days),
            benchmark_kind="equal_weight",
            rebalances=[
                stock_backtest.RebalanceDetail(
                    signal_date=curve_days[0],
                    target={"600001": 1.0},
                    fills=[object()],
                    turnover=0.5,
                    cash_weight=0.0,
                )
            ],
            final_value=equity[-1],
            total_fees=0.0,
            avg_turnover=0.5,
            forward_returns=[],
            scores_by_date=[],
            benchmarks={
                "UNIVERSE_EQUAL_WEIGHT_TOTAL_RETURN": [1.0] * len(curve_days),
                "CASH_CNY": [1.0] * len(curve_days),
            },
        )

    monkeypatch.setattr(stock_validation, "run_backtest", fake_run_backtest)
    result = stock_validation.run_stock_walk_forward(
        Repository(),
        BacktestConfig(start=days[0], end=days[-1], candidate_codes=("600001",)),
        [20, 30],
        [0.05],
        embargo_days=21,
    )
    assert result["holdout_evaluations"] == 1
    assert result["holdout"]["benchmark_kind"] == "equal_weight"
    assert result["holdout"]["benchmark_return"] == 0.0
    assert result["holdout"]["net_excess_return"] == pytest.approx(
        result["holdout"]["total_return"]
    )
    assert result["holdout"]["tracking_error"] is not None
    assert result["holdout"]["active_sharpe"] is not None
    assert set(result["holdout"]["comparator_metrics"]) == {
        "UNIVERSE_EQUAL_WEIGHT_TOTAL_RETURN",
        "CASH_CNY",
    }
    assert result["holdout"]["benchmark_code"] == "equal_weight"
    assert result["holdout"]["benchmark_curve_points"] == len(
        [
            day
            for day in days
            if datetime.fromisoformat(result["splits"]["holdout"][0]).date()
            <= day
            <= datetime.fromisoformat(result["splits"]["holdout"][1]).date()
        ]
    )
    original_hash = stock_validation.validation_sha256(result)
    tampered = json.loads(json.dumps(result))
    tampered["holdout"]["benchmark_code"] = "000906.SH"
    assert stock_validation.validation_sha256(tampered) != original_hash
    tampered = json.loads(json.dumps(result))
    tampered["holdout"]["benchmark_curve_sha256"] = "forged"
    assert stock_validation.validation_sha256(tampered) != original_hash
    tampered = json.loads(json.dumps(result))
    tampered["holdout"]["benchmark_start_date"] = "1990-01-01"
    assert stock_validation.validation_sha256(tampered) != original_hash
    assert result["splits"]["train"][1] < result["splits"]["validation"][0]
    assert result["splits"]["validation"][1] < result["splits"]["holdout"][0]
    holdout_range = tuple(
        datetime.fromisoformat(value).date() for value in result["splits"]["holdout"]
    )
    assert (
        sum(
            (start_day, end_day) == holdout_range
            for start_day, end_day, _initial in calls
        )
        == 1
    )
    assert not any(initial for _start, _end, initial in calls)


def test_stock_walk_forward_rejects_cash_only_fold(monkeypatch) -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC).date()
    days = tuple(start + timedelta(days=index) for index in range(1000))

    class Repository:
        def trade_calendar(self, _start, _end):
            return TradeCalendar(days)

    def fake_cash_only(*, config, repository):
        del repository
        curve_days = [day for day in days if config.start <= day <= config.end]
        return BacktestOutcome(
            calendar=curve_days,
            equity=[1_000_000.0] * len(curve_days),
            daily_returns=[0.0] * max(len(curve_days) - 1, 0),
            benchmark=[1.0] * len(curve_days),
            benchmark_kind="equal_weight",
            rebalances=[
                stock_backtest.RebalanceDetail(
                    signal_date=curve_days[0],
                    target={},
                    warnings=["因子覆盖不足，本期持有现金"],
                    diagnostics={
                        "selection_funnel": {
                            "factor_eligible_count": 0,
                            "target_count": 0,
                        }
                    },
                )
            ],
            final_value=1_000_000.0,
            total_fees=0.0,
            avg_turnover=0.0,
            forward_returns=[],
            scores_by_date=[],
        )

    monkeypatch.setattr(stock_validation, "run_backtest", fake_cash_only)
    with pytest.raises(
        stock_backtest.BacktestError,
        match="未产生可验证的真实策略活动",
    ):
        stock_validation.run_stock_walk_forward(
            Repository(),
            BacktestConfig(
                start=days[0],
                end=days[-1],
                candidate_codes=("600001",),
            ),
            [30],
            [0.05],
            embargo_days=21,
        )
