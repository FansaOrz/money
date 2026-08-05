"""估值历史分位的最低样本、去重与前向历史装载测试。"""

from datetime import date

from app.services.stock_backtest import load_fundamentals_by_code
from app.services.stock_factors import (
    _fundamental_value_series,
    historical_percentile,
)
from app.services.factor_health import inspect_factor
from app.services.stock_repository import Fundamentals


def test_single_point_does_not_return_fake_half_percentile() -> None:
    assert historical_percentile([0.1], 0.1) is None
    history = [float(index) for index in range(24)]
    assert historical_percentile(history, 23.0) == (23.0 + 0.5) / 24.0


def test_valuation_series_deduplicates_same_market_observation() -> None:
    snapshots = (
        Fundamentals(
            code="000001",
            available_at=date(2025, 1, 31),
            valuation_date=date(2025, 1, 30),
            ep=0.1,
            bp=0.2,
        ),
        Fundamentals(
            code="000001",
            available_at=date(2025, 2, 1),
            valuation_date=date(2025, 1, 30),
            ep=0.2,
            bp=0.2,
        ),
    )
    assert _fundamental_value_series(snapshots, date(2025, 2, 1)) == [0.2]


class _HistoryRepository:
    def __init__(self) -> None:
        self.requested: tuple[date, ...] = ()

    def fundamentals(self, codes, as_of):  # noqa: ANN001
        return []

    def valuation_snapshots(self, codes, dates):  # noqa: ANN001
        self.requested = tuple(dates)
        return []


def test_forward_single_signal_loads_five_year_monthly_history() -> None:
    repository = _HistoryRepository()
    signal = date(2025, 12, 15)
    load_fundamentals_by_code(repository, ["000001"], [signal])  # type: ignore[arg-type]
    assert signal in repository.requested
    assert len(repository.requested) == 61
    assert min(repository.requested) < date(2021, 1, 1)


def test_constant_valuation_percentile_is_blocked() -> None:
    health = inspect_factor("valuation_percentile", [0.5] * 100)
    assert health.blocked is True
    assert health.unique_values == 1
    assert any("唯一值" in reason for reason in health.reasons)


def test_unit_scale_and_sign_mutation_are_blocked() -> None:
    scale = inspect_factor(
        "bp",
        [float(index) * 10_000 for index in range(1, 101)],
        historical_median=50.5,
    )
    assert scale.blocked
    assert any("单位突变" in reason for reason in scale.reasons)
    sign = inspect_factor(
        "bp",
        [-float(index) for index in range(1, 101)],
        historical_median=50.5,
    )
    assert sign.blocked
    assert any("符号" in reason for reason in sign.reasons)
