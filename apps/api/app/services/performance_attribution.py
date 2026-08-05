"""几何链接、Brinson-Fachler、因子 P&L 与残差质量控制。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def geometric_link(
    period_contributions: Sequence[Mapping[str, float]],
) -> dict[str, object]:
    """Carino 几何链接；每期贡献必须与当期总收益同量纲。"""
    if not period_contributions:
        return {
            "linked_contributions": {},
            "total_return": 0.0,
            "bridge_error": 0.0,
            "method": "Carino geometric linking",
        }
    period_returns = [sum(row.values()) for row in period_contributions]
    total_return = math.prod(1.0 + value for value in period_returns) - 1.0

    def coefficient(value: float) -> float:
        return math.log1p(value) / value if abs(value) > 1e-12 else 1.0

    denominator = coefficient(total_return)
    linked: dict[str, float] = {}
    wealth_before = 1.0
    for period_return, contributions in zip(
        period_returns, period_contributions, strict=True
    ):
        adjustment = wealth_before * coefficient(period_return) / denominator
        for name, value in contributions.items():
            linked[name] = linked.get(name, 0.0) + float(value) * adjustment
        wealth_before *= 1.0 + period_return
    bridge_error = total_return - sum(linked.values())
    linked["residual_unexplained"] = linked.get(
        "residual_unexplained", 0.0
    ) + bridge_error
    return {
        "linked_contributions": linked,
        "total_return": total_return,
        "bridge_error": total_return - sum(linked.values()),
        "method": "Carino geometric linking",
    }


def brinson_fachler(
    *,
    portfolio_weights: Mapping[str, float],
    benchmark_weights: Mapping[str, float],
    portfolio_returns: Mapping[str, float],
    benchmark_returns: Mapping[str, float],
) -> dict[str, object]:
    """标准 Brinson-Fachler：配置、选择和交互项。键为行业。"""
    industries = sorted(set(portfolio_weights) | set(benchmark_weights))
    benchmark_total_return = sum(
        float(benchmark_weights.get(industry, 0.0))
        * float(benchmark_returns.get(industry, 0.0))
        for industry in industries
    )
    rows: dict[str, dict[str, float]] = {}
    for industry in industries:
        wp = float(portfolio_weights.get(industry, 0.0))
        wb = float(benchmark_weights.get(industry, 0.0))
        rp = float(portfolio_returns.get(industry, 0.0))
        rb = float(benchmark_returns.get(industry, 0.0))
        rows[industry] = {
            "allocation": (wp - wb) * (rb - benchmark_total_return),
            "selection": wb * (rp - rb),
            "interaction": (wp - wb) * (rp - rb),
        }
    totals = {
        name: sum(row[name] for row in rows.values())
        for name in ("allocation", "selection", "interaction")
    }
    return {
        "method": "Brinson-Fachler",
        "benchmark_total_return": benchmark_total_return,
        "industries": rows,
        "totals": totals,
    }


def factor_pnl_attribution(
    *,
    active_factor_exposures: Mapping[str, float],
    factor_returns: Mapping[str, float],
    active_return: float,
) -> dict[str, object]:
    factor_pnl = {
        factor: float(exposure) * float(factor_returns.get(factor, 0.0))
        for factor, exposure in active_factor_exposures.items()
    }
    specific = active_return - sum(factor_pnl.values())
    return {
        "factor_pnl": factor_pnl,
        "specific_return": specific,
        "active_return": active_return,
        "bridge_error": active_return - sum(factor_pnl.values()) - specific,
    }


def account_return_bridge(
    *,
    total_return: float,
    direct_selection: float,
    industry_allocation: float,
    style: float,
    market: float,
    cash_drag: float,
    fees: float,
    slippage: float,
    maximum_residual: float = 0.001,
) -> dict[str, float | bool]:
    explained = (
        direct_selection
        + industry_allocation
        + style
        + market
        + cash_drag
        + fees
        + slippage
    )
    residual = total_return - explained
    return {
        "selection": direct_selection,
        "industry_allocation": industry_allocation,
        "style": style,
        "market": market,
        "cash_drag": cash_drag,
        "fees": fees,
        "slippage": slippage,
        "residual_unexplained": residual,
        "total_return": total_return,
        "quality_warning": abs(residual) > maximum_residual,
    }
