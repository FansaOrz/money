"""A股规则策略前向模拟：就绪门槛、T+1、幂等和账本闭环。"""

import subprocess
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    IndexConstituent,
    DataSourceSLAState,
    StockDailyBar,
    StockFinancialIndicator,
    StockIndustry,
    StockMaster,
    StockPaperNavDaily,
    StockPaperRun,
    StockPaperSignal,
    StockPaperTrade,
    StockValuation,
    StrategyVersion,
)
from app.services import stock_paper, strategy_mandate
from app.services.stock_repository import (
    Fundamentals,
    StockBar,
    StockInfo,
    TradeCalendar,
)
from app.timezone import CN_TZ


class ForwardRepository:
    def __init__(self, infos: list[StockInfo], bars: dict[str, list[StockBar]]) -> None:
        self.infos = infos
        self.bars = bars

    def list_stocks(self, codes: list[str] | None = None) -> list[StockInfo]:
        wanted = set(codes) if codes else None
        return [item for item in self.infos if wanted is None or item.code in wanted]

    def daily_bars(
        self,
        codes: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[StockBar]:
        wanted = set(codes) if codes else set(self.bars)
        return [
            bar
            for code in wanted
            for bar in self.bars.get(code, [])
            if (start is None or bar.trade_date >= start)
            and (end is None or bar.trade_date <= end)
        ]

    def market_bars(self, codes, start=None, end=None):
        from app.services.stock_repository import MarketBars

        result = {}
        for code in codes:
            rows = tuple(
                bar
                for bar in self.bars.get(code, [])
                if (start is None or bar.trade_date >= start)
                and (end is None or bar.trade_date <= end)
            )
            result[code] = MarketBars(research_bars=rows, exec_bars=rows)
        return result

    def fundamentals(
        self, codes: list[str] | None = None, as_of: date | None = None
    ) -> list[Fundamentals]:
        wanted = set(codes) if codes else {item.code for item in self.infos}
        return [
            Fundamentals(
                code=code,
                available_at=date(2024, 12, 31),
                roe=0.12,
                gross_margin=0.30,
                ocf_to_profit=1.0,
                debt_ratio=0.45,
                ep=1 / 12,
                bp=1 / 1.5,
                market_cap=10_000_000_000.0,
                float_market_cap=8_000_000_000.0,
            )
            for code in wanted
        ]

    def trade_calendar(self, start: date | None, end: date | None) -> TradeCalendar:
        days = sorted(
            {
                bar.trade_date
                for rows in self.bars.values()
                for bar in rows
                if (start is None or bar.trade_date >= start)
                and (end is None or bar.trade_date <= end)
            }
        )
        return TradeCalendar(tuple(days))

    def index_bars(self, index_code: str, start=None, end=None):
        return []

    def name_histories(self, codes):
        return {}


def test_formal_validation_completion_separates_operational_and_investment_status() -> (
    None
):
    failed = stock_paper.formal_validation_completion_metadata(
        {
            "net_excess_return": -0.01,
            "alpha_evidence_status": "alpha_evidence_insufficient",
            "quintile_gate_status": "failed",
            "active_alpha_gate_status": "failed",
            "stability_gate_status": "failed",
            "robustness_gate_status": "failed",
        }
    )
    assert failed == {
        "formal_validation_status": "completed",
        "formal_validation_scope": ("operational_only_with_investment_diagnostics"),
        "operational_validation_status": "passed",
        "investment_validation_status": "evidence_failed",
        "investment_validation_failed_gates": [
            "net_excess_return",
            "composite_ic",
            "quintile",
            "active_alpha",
            "stability",
            "robustness",
        ],
    }

    passed = stock_paper.formal_validation_completion_metadata(
        {
            "net_excess_return": 0.01,
            "alpha_evidence_status": "alpha_evidence_sufficient",
            "quintile_gate_status": "passed",
            "active_alpha_gate_status": "passed",
            "stability_gate_status": "passed",
            "robustness_gate_status": "passed",
        }
    )
    assert passed["investment_validation_status"] == "evidence_passed"
    assert passed["investment_validation_failed_gates"] == []


def test_version11_research_record_uses_investment_mandate_without_readiness(
    db_session: Session,
) -> None:
    version = stock_paper.ensure_research_strategy_version(db_session)

    assert version.name == stock_paper.STRATEGY_NAME
    assert version.status == "research"
    assert version.params["model_version"] == "stock_rules_v7"
    assert version.params["formal_validation_status"] == "not_run"
    assert version.params["factor_weight_policy"] == {
        "prior": {
            "quality": 0.30,
            "value": 0.25,
            "momentum": 0.20,
            "trend": 0.15,
            "lowvol": 0.10,
        },
        "minimum_mature_periods": 12,
        "ic_prior": "zero_centered",
        "prior_strength": 12.0,
        "minimum_family_weight": 0.0,
        "maximum_family_weight": 0.50,
        "previous_weight_blend": 0.50,
        "negative_evidence_policy": "target_weight_zero",
        "fit_scope": "training_only_frozen_before_validation",
    }
    assert version.mandate["validation_scope"] == "investment_effectiveness"
    assert version.mandate["investment_approval_eligible"] is True
    assert stock_paper.ensure_research_strategy_version(db_session).id == version.id
    audit = db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "strategy_version_created",
            AuditLog.resource_id == str(version.id),
        )
    )
    assert audit is not None
    assert audit.detail["model_version"] == "stock_rules_v7"


def test_nested_v11_preflight_weights_are_used_for_forward_signal(
    db_session: Session,
) -> None:
    version = stock_paper.ensure_research_strategy_version(db_session)
    params = dict(version.params)
    params["training_preflight"] = {
        "frozen_factor_weights": {
            "quality": 0.0,
            "value": 0.28,
            "momentum": 0.50,
            "trend": 0.02,
            "lowvol": 0.20,
        }
    }
    version.params = params
    db_session.commit()

    assert stock_paper._frozen_forward_factor_weights(version) == {
        "quality": 0.0,
        "value": 0.28,
        "momentum": 0.50,
        "trend": 0.02,
        "lowvol": 0.20,
    }


def test_operational_shadow_is_preferred_as_active_forward_version(
    db_session: Session,
) -> None:
    research = stock_paper.ensure_research_strategy_version(db_session)
    mandate = strategy_mandate.operational_validation_mandate(
        strategy_name=stock_paper.OPERATIONAL_SHADOW_NAME,
        initial_capital=stock_paper.INITIAL_CAPITAL,
        rebalance_days=20,
        top_n=stock_paper.TOP_N,
    )
    shadow = StrategyVersion(
        name=stock_paper.OPERATIONAL_SHADOW_NAME,
        initial_capital=stock_paper.INITIAL_CAPITAL,
        rebalance_interval=20,
        fee_rate=research.fee_rate,
        top_n=stock_paper.TOP_N,
        params={"model_version": stock_paper.MODEL_VERSION},
        mandate=mandate,
        mandate_sha256=strategy_mandate.mandate_sha256(mandate),
        status="research",
    )
    db_session.add(shadow)
    db_session.commit()

    assert stock_paper._active_forward_version(db_session).id == shadow.id


def test_frozen_runtime_source_gate_accepts_clean_and_blocks_changes(
    monkeypatch,
) -> None:
    version = SimpleNamespace(id=15, params={"git_sha": "a" * 40})
    calls: list[list[str]] = []

    def clean_run(command, **_kwargs):  # noqa: ANN001
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(stock_paper.subprocess, "run", clean_run)
    stock_paper._assert_frozen_runtime_source(version)
    assert calls[0][-3:] == list(stock_paper.FROZEN_RUNTIME_PATHS)

    def changed_run(command, **_kwargs):  # noqa: ANN001
        return subprocess.CompletedProcess(
            command,
            1 if command[1] == "diff" else 0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(stock_paper.subprocess, "run", changed_run)
    with pytest.raises(stock_paper.StockPaperError, match="冻结提交"):
        stock_paper._assert_frozen_runtime_source(version)


def _seed_trial(db: Session) -> tuple[ForwardRepository, date]:
    first_day = date(2025, 1, 1)
    signal_day = first_day + timedelta(days=299)
    infos: list[StockInfo] = []
    bars: dict[str, list[StockBar]] = {}
    synced_at = datetime.now(UTC)
    for dataset in (
        "suspend_d",
        "stk_limit",
        "dividend",
        "namechange",
        "index_membership_weight",
        "industry_classification",
    ):
        db.add(
            DataSourceSLAState(
                dataset=dataset,
                required=True,
                primary_source="test",
                fallback_source="test-fallback",
                license_class="test",
                frequency_minutes=1440,
                max_latency_minutes=2160 if dataset != "stk_limit" else 240,
                owner="test",
                failure_mode="halt_new_orders",
                status="success",
                active_source="test",
                last_attempted_at=synced_at,
                last_success_at=synced_at,
                data_date=signal_day,
                row_count=50,
            )
        )
    for index in range(50):
        code = f"{600000 + index:06d}"
        industry = "制造" if index < 25 else "消费"
        infos.append(StockInfo(code=code, name=f"股票{index}", industry=industry))
        db.add(StockMaster(code=code, name=f"股票{index}", exchange="sh"))
        db.add(
            IndexConstituent(
                index_code="000300" if index < 25 else "000905",
                stock_code=code,
                stock_name=f"股票{index}",
            )
        )
        db.add(StockIndustry(code=code, source="test", industry_name=industry))
        db.add(
            StockFinancialIndicator(
                code=code,
                report_date=date(2024, 12, 31),
                roe=10,
                payload="{}",
                source="test",
            )
        )
        db.add(
            StockDailyBar(
                code=code,
                first_trade_date=first_day,
                last_trade_date=signal_day,
                rows=300,
                source="test",
            )
        )
        db.add(
            StockValuation(
                code=code,
                trade_date=signal_day,
                indicator="pe_ttm",
                value=12,
                source="test",
            )
        )
        db.add(
            StockValuation(
                code=code,
                trade_date=signal_day,
                indicator="pb",
                value=1.5,
                source="test",
            )
        )
        series = []
        for offset in range(300):
            day = first_day + timedelta(days=offset)
            close = 10.0 * (1.0 + (0.0002 + index * 0.00001)) ** offset
            series.append(
                StockBar(
                    code=code,
                    trade_date=day,
                    open=close,
                    high=close * 1.01,
                    low=close * 0.99,
                    close=close,
                    volume=1_000_000,
                    amount=100_000_000,
                )
            )
        bars[code] = series
    db.commit()
    return ForwardRepository(infos, bars), signal_day


def test_readiness_blocks_empty_database(db_session: Session) -> None:
    readiness = stock_paper.get_readiness(db_session)
    assert readiness.ready is False
    assert readiness.blockers


def test_forward_cycle_generates_then_executes_t_plus_one(
    db_session: Session, monkeypatch
) -> None:
    repository, signal_day = _seed_trial(db_session)
    monkeypatch.setattr(stock_paper, "EXPECTED_UNIVERSE_COUNT", 50)
    monkeypatch.setattr(stock_paper, "REQUIRE_PREVALIDATION", False)
    monkeypatch.setattr(stock_paper, "_assert_frozen_runtime_source", lambda _v: None)
    monkeypatch.setattr(stock_paper, "load_repository", lambda _db: repository)
    monkeypatch.setattr(
        stock_paper,
        "now_cn",
        lambda: datetime.combine(signal_day, time(16), tzinfo=CN_TZ),
    )

    first = stock_paper.run_cycle(db_session)
    assert first.run_date == signal_day.isoformat()
    assert first.signal_generated is True
    assert first.trade_count == 0
    signal = db_session.scalar(select(StockPaperSignal))
    assert signal is not None
    assert signal.status == "pending"
    assert signal.signal_date == signal_day

    # 同一行情日重跑幂等，不重复生成信号/净值。
    repeated = stock_paper.run_cycle(db_session)
    assert repeated.skipped is True
    assert db_session.query(StockPaperRun).count() == 1
    assert db_session.query(StockPaperNavDaily).count() == 1
    first_nav = db_session.scalar(
        select(StockPaperNavDaily).where(StockPaperNavDaily.nav_date == signal_day)
    )
    assert first_nav is not None
    # 基准不能在策略首个可执行日之前提前产生收益。
    assert float(first_nav.benchmark_nav) == 1.0

    next_day = signal_day + timedelta(days=1)
    for code, rows in repository.bars.items():
        previous = rows[-1]
        close = previous.close * 1.001
        rows.append(
            StockBar(
                code=code,
                trade_date=next_day,
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=1_000_000,
                amount=100_000_000,
            )
        )
    for row in db_session.scalars(select(StockDailyBar)).all():
        row.last_trade_date = next_day
        row.rows += 1
    db_session.commit()

    second = stock_paper.run_cycle(db_session)
    assert second.run_date == next_day.isoformat()
    assert second.trade_count > 0
    assert second.rebalanced is True
    db_session.refresh(signal)
    assert signal.status == "executed"
    assert signal.executed_at == next_day
    assert signal.order_state
    assert all(
        state["status"] in {"filled", "partial", "blocked"} and state["events"]
        for state in signal.order_state.values()
    )
    assert db_session.query(StockPaperTrade).count() == second.trade_count
    assert db_session.query(StockPaperNavDaily).count() == 2
    second_nav = db_session.scalar(
        select(StockPaperNavDaily).where(StockPaperNavDaily.nav_date == next_day)
    )
    assert second_nav is not None
    assert float(second_nav.benchmark_nav) == 1.0
    assert float(second_nav.cash_interest) > 0
    assert float(second_nav.frozen_cash) == 0.0
    assert float(second_nav.settled_cash) <= float(second_nav.cash)
    assert float(second_nav.cash_conservation_error) == 0.0
    assert second_nav.cash_ledger["policy_version"] == (
        stock_paper.cash_ledger.CASH_POLICY_VERSION
    )
    assert {event["event_type"] for event in second_nav.cash_ledger["events"]} >= {
        "cash_interest",
        "buy_order_frozen",
        "buy_order_settled",
    }

    summary = stock_paper.get_summary(db_session)
    assert summary.started is True
    assert summary.strategy is not None
    assert summary.strategy.candidate_count == 50
    assert summary.metrics.trading_days == 2
    assert summary.positions


def test_pending_signal_can_be_manually_cancelled_with_opportunity_cost(
    db_session: Session, monkeypatch
) -> None:
    repository, signal_day = _seed_trial(db_session)
    monkeypatch.setattr(stock_paper, "EXPECTED_UNIVERSE_COUNT", 50)
    monkeypatch.setattr(stock_paper, "REQUIRE_PREVALIDATION", False)
    monkeypatch.setattr(stock_paper, "load_repository", lambda _db: repository)
    monkeypatch.setattr(
        stock_paper,
        "now_cn",
        lambda: datetime.combine(signal_day, time(16), tzinfo=CN_TZ),
    )
    stock_paper.run_cycle(db_session)
    signal = db_session.scalar(select(StockPaperSignal))
    assert signal is not None

    result = stock_paper.cancel_pending_signal(
        db_session,
        signal.id,
        reason="人工风控复核取消",
    )
    assert result.status == "cancelled"
    db_session.refresh(signal)
    assert signal.status == "cancelled"
    assert all(
        item["status"] == "cancelled"
        and item["adverse_opportunity_cost"] == 0.0
        and item["events"][-1]["reason"] == "人工风控复核取消"
        and item["order_lifecycle_version"] == stock_paper.ORDER_POLICY.version
        for item in signal.order_state.values()
    )


def test_late_initialization_never_backfills_missed_open(
    db_session: Session, monkeypatch
) -> None:
    repository, signal_day = _seed_trial(db_session)
    monkeypatch.setattr(stock_paper, "EXPECTED_UNIVERSE_COUNT", 50)
    monkeypatch.setattr(stock_paper, "REQUIRE_PREVALIDATION", False)
    monkeypatch.setattr(stock_paper, "load_repository", lambda _db: repository)
    created_day = signal_day + timedelta(days=1)
    monkeypatch.setattr(
        stock_paper,
        "now_cn",
        lambda: datetime.combine(created_day, time(12), tzinfo=CN_TZ),
    )

    stock_paper.run_cycle(db_session)

    signal = db_session.scalar(select(StockPaperSignal))
    assert signal is not None
    assert signal.signal_date == signal_day
    assert signal.execute_on == created_day + timedelta(days=1)
