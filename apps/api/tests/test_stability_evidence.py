"""年度、行情和分组稳定性硬判定。"""

from datetime import date, timedelta

from app.services.stability_evidence import stability_evidence


def test_best_year_dependency_is_rejected() -> None:
    start = date(2020, 1, 1)
    calendar = [start + timedelta(days=index) for index in range(757)]
    benchmark = [1.0]
    for _ in range(756):
        benchmark.append(benchmark[-1] * 1.0001)
    strategy_returns = [
        0.002 if calendar[index + 1].year == 2020 else -0.0005
        for index in range(756)
    ]
    result = stability_evidence(
        calendar,
        strategy_returns,
        benchmark,
        [],
        [],
        [],
    )
    assert result["passed"] is False
    assert result["excess_return_after_best_year_removed"] < 0
    assert result["regime_definition"]["kind"] == "ex_ante"
