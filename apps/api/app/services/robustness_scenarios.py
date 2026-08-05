"""参数、成本、容量、时点与数据扰动的统一稳健性证据。"""

from __future__ import annotations

import math
from collections.abc import Iterable
from statistics import fmean


REQUIRED_DIMENSIONS = {
    "top_n",
    "max_stock_weight",
    "rebalance_day",
    "factor_window",
    "factor_weight",
    "filter_threshold",
    "cost_1x",
    "cost_2x",
    "cost_3x",
    "capacity_down",
    "signal_delay",
    "delete_period",
    "delete_industry",
    "winsorization",
    "data_late",
}


def scenario_catalog() -> list[dict[str, object]]:
    """返回必须执行的扰动目录，供研究运行器和报告使用。"""
    return [
        {"dimension": "top_n", "values": ["-20%", "+20%"]},
        {"dimension": "max_stock_weight", "values": ["-20%", "+20%"]},
        {"dimension": "rebalance_day", "values": ["T-1", "T+1"]},
        {"dimension": "factor_window", "values": ["0.8x", "1.2x"]},
        {"dimension": "factor_weight", "values": ["family±20%"]},
        {"dimension": "filter_threshold", "values": ["0.8x", "1.2x"]},
        {"dimension": "cost_1x", "values": ["baseline"]},
        {"dimension": "cost_2x", "values": ["all-in cost 2x"]},
        {"dimension": "cost_3x", "values": ["all-in cost 3x"]},
        {"dimension": "capacity_down", "values": ["ADV participation 50%"]},
        {"dimension": "signal_delay", "values": ["+1 trading day"]},
        {"dimension": "delete_period", "values": ["leave-one-period-out"]},
        {"dimension": "delete_industry", "values": ["leave-one-industry-out"]},
        {"dimension": "winsorization", "values": ["0.5/99.5", "2/98"]},
        {"dimension": "data_late", "values": ["lag/drop newest observation"]},
    ]


def evaluate_robustness(
    scenarios: Iterable[dict[str, object]],
    *,
    minimum_excess_return: float = 0.0,
    minimum_neighbor_pass_rate: float = 0.75,
) -> dict[str, object]:
    """汇总实际场景；目录缺项、邻域脆弱或保守成本失败均阻断晋级。"""
    rows = [dict(row) for row in scenarios]
    dimensions = {str(row.get("dimension")) for row in rows}
    missing = sorted(REQUIRED_DIMENSIONS - dimensions)
    for row in rows:
        value = row.get("net_excess_return")
        row["passed"] = bool(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) > minimum_excess_return
        )
    neighbors = [
        row
        for row in rows
        if row.get("dimension")
        in {
            "top_n",
            "max_stock_weight",
            "rebalance_day",
            "factor_window",
            "factor_weight",
            "filter_threshold",
            "delete_period",
            "delete_industry",
            "winsorization",
            "data_late",
        }
    ]
    neighbor_pass_rate = (
        sum(row["passed"] is True for row in neighbors) / len(neighbors)
        if neighbors
        else 0.0
    )
    conservative = [
        row
        for row in rows
        if row.get("dimension")
        in {"cost_2x", "cost_3x", "capacity_down", "signal_delay"}
    ]
    failures: list[str] = []
    if missing:
        failures.append("缺少扰动维度：" + ",".join(missing))
    if neighbor_pass_rate < minimum_neighbor_pass_rate:
        failures.append("邻域参数/数据扰动通过率不足")
    if not conservative or any(row["passed"] is not True for row in conservative):
        failures.append("保守成本、容量或信号延迟场景存在失败")
    finite_values = [
        float(row["net_excess_return"])
        for row in rows
        if isinstance(row.get("net_excess_return"), (int, float))
        and not isinstance(row.get("net_excess_return"), bool)
    ]
    return {
        "catalog": scenario_catalog(),
        "scenarios": rows,
        "missing_dimensions": missing,
        "neighbor_pass_rate": neighbor_pass_rate,
        "worst_net_excess_return": min(finite_values) if finite_values else None,
        "mean_net_excess_return": fmean(finite_values) if finite_values else None,
        "failures": failures,
        "passed": not failures,
        "status": "passed" if not failures else "failed",
    }
