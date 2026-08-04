"""量化平台治理、任务、备份、生命周期与模拟 OMS/RMS 验收。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient

from app.models import (
    DataFieldProvenance,
    PersistentJob,
    QuantDataRecord,
    QuantImportRun,
    StrategyVersion,
)
from app.config import get_settings
from app.db.session import get_db
from app.main import create_app, create_tables
from app.services import backup, job_queue, oms, strategy_lifecycle
from app.services import stock_validation
from app.services.stock_backtest import BacktestConfig, BacktestOutcome
from app.services.stock_repository import TradeCalendar
from app.services.quant_data_governance import (
    import_tushare_snapshot,
    register_corporate_action,
    resolve_field,
)


def _version(db_session) -> StrategyVersion:
    version = StrategyVersion(
        name="治理测试策略",
        initial_capital=Decimal("1000000"),
        rebalance_interval=20,
        fee_rate=Decimal("0.001"),
        top_n=30,
        params={},
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


def test_production_requires_api_key_and_blocks_readonly_mutation(
    db_session, monkeypatch
) -> None:
    monkeypatch.setenv("MONEY_ENVIRONMENT", "production")
    monkeypatch.setenv("MONEY_ADMIN_API_KEY", "admin-secret")
    monkeypatch.setenv("MONEY_READONLY_API_KEY", "readonly-secret")
    monkeypatch.setenv("MONEY_AUTO_CREATE_TABLES", "false")
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


def test_tushare_snapshot_import_is_pit_provenanced_and_idempotent(
    db_session, tmp_path
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = (
        tmp_path
        / "tushare_snapshot"
        / "stocks"
        / "adj_factor"
        / "600001.SH.parquet"
    )
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
    first = import_tushare_snapshot(
        db_session, tmp_path, ["adj_factor"]
    )
    second = import_tushare_snapshot(
        db_session, tmp_path, ["adj_factor"]
    )
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
        for row in db_session.query(DataFieldProvenance).filter_by(
            record_id=first.id
        )
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
    version = _version(db_session)
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
            "validated",
            evidence={"data_coverage": 0.90},
            actor="tester",
            reason="数据不足",
        )
    version = strategy_lifecycle.transition(
        db_session,
        version.id,
        "validated",
        evidence={
            "data_coverage": 0.99,
            "holdout_evaluations": 1,
            "walkforward_folds": 3,
            "holdout_sharpe": 0.5,
        },
        actor="tester",
        reason="门禁通过",
    )
    assert version.status == "validated"


def test_stock_strategy_validation_evidence_cannot_be_faked(db_session) -> None:
    version = _version(db_session)
    version.params = {
        "model_version": "stock_rules_v4",
        "validation_sha256": "frozen-hash",
    }
    db_session.commit()
    common = {
        "data_coverage": 0.99,
        "holdout_evaluations": 1,
        "walkforward_folds": 3,
        "holdout_sharpe": 0.5,
    }
    with pytest.raises(ValueError, match="证据哈希"):
        strategy_lifecycle.transition(
            db_session,
            version.id,
            "validated",
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
        "validated",
        evidence={
            **common,
            "validation_sha256": "frozen-hash",
            "generated_by": "stock_validation.run_stock_walk_forward",
        },
        actor="system",
        reason="系统验证",
    )
    assert version.status == "validated"


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
    oms.set_kill_switch(db_session, "SIM-A", True)
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


def test_sqlite_backup_verify_and_non_overwrite_restore(
    db_session, tmp_path
) -> None:
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
    restored = backup.restore_to_new_directory(
        destination, tmp_path / "restored"
    )
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
        curve_days = [
            day for day in days if config.start <= day <= config.end
        ]
        equity = [1_000_000.0 + index for index in range(len(curve_days))]
        returns = [
            equity[index] / equity[index - 1] - 1.0
            for index in range(1, len(equity))
        ]
        return BacktestOutcome(
            calendar=curve_days,
            equity=equity,
            daily_returns=returns,
            benchmark=[1.0] * len(curve_days),
            benchmark_kind="equal_weight",
            rebalances=[],
            final_value=equity[-1],
            total_fees=0.0,
            avg_turnover=0.0,
            forward_returns=[],
            scores_by_date=[],
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
    assert result["splits"]["train"][1] < result["splits"]["validation"][0]
    assert result["splits"]["validation"][1] < result["splits"]["holdout"][0]
    holdout_range = tuple(
        datetime.fromisoformat(value).date()
        for value in result["splits"]["holdout"]
    )
    assert sum(
        (start_day, end_day) == holdout_range
        for start_day, end_day, _initial in calls
    ) == 1
    assert not any(initial for _start, _end, initial in calls)
