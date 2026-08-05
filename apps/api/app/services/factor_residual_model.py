"""外部市场/严格不含自身代理的 Beta、残差波动与残差动量模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np


@dataclass(frozen=True)
class ResidualModelResult:
    alpha: float | None
    beta: float | None
    industry_beta: float | None
    residuals: dict[date, float]
    r_squared: float | None
    capm_r_squared: float | None
    model: str
    market_source: str
    window: int
    observations: int
    missing_dates: int


def price_series_returns(points: list[tuple[date, float]]) -> dict[date, float]:
    ordered = sorted(
        ((day, float(value)) for day, value in points if value is not None and value > 0),
        key=lambda item: item[0],
    )
    return {
        ordered[index][0]: ordered[index][1] / ordered[index - 1][1] - 1.0
        for index in range(1, len(ordered))
        if ordered[index - 1][1] > 0
    }


def leave_one_out_proxy(
    returns_by_code: dict[str, dict[date, float]],
    code: str,
    *,
    eligible_codes: set[str] | None = None,
    minimum_constituents: int = 5,
) -> dict[date, float]:
    """逐日严格排除目标股票，代理不会被该股票自身机械污染。"""
    allowed = eligible_codes or set(returns_by_code)
    by_date: dict[date, list[float]] = {}
    for other_code, series in returns_by_code.items():
        if other_code == code or other_code not in allowed:
            continue
        for day, value in series.items():
            by_date.setdefault(day, []).append(value)
    return {
        day: sum(values) / len(values)
        for day, values in by_date.items()
        if len(values) >= minimum_constituents
    }


def estimate_residual_model(
    stock_returns: dict[date, float],
    market_returns: dict[date, float],
    *,
    market_source: str,
    industry_returns: dict[date, float] | None = None,
    window: int = 252,
    minimum_observations: int = 60,
) -> ResidualModelResult:
    common = sorted(set(stock_returns) & set(market_returns))[-window:]
    if len(common) < minimum_observations:
        return ResidualModelResult(
            None,
            None,
            None,
            {},
            None,
            None,
            "insufficient",
            market_source,
            window,
            len(common),
            max(0, window - len(common)),
        )
    y = np.array([stock_returns[day] for day in common])
    market = np.array([market_returns[day] for day in common])
    capm_design = np.column_stack([np.ones(len(common)), market])
    capm_coef = np.linalg.pinv(capm_design) @ y
    capm_residual = y - capm_design @ capm_coef
    total = float(np.sum((y - float(np.mean(y))) ** 2))
    capm_r2 = 1.0 - float(np.sum(capm_residual**2)) / total if total > 0 else None
    design = capm_design
    model = "capm"
    industry_beta = None
    if industry_returns is not None:
        industry = np.array(
            [industry_returns.get(day, market_returns[day]) for day in common]
        )
        candidate = np.column_stack([np.ones(len(common)), market, industry])
        # 条件数过高时保持 CAPM，避免小行业/代理重合导致虚假系数。
        if np.linalg.matrix_rank(candidate) == candidate.shape[1]:
            design = candidate
            model = "market_plus_industry"
    coefficients = np.linalg.pinv(design) @ y
    residual = y - design @ coefficients
    r_squared = (
        1.0 - float(np.sum(residual**2)) / total if total > 0 else None
    )
    if design.shape[1] == 3:
        industry_beta = float(coefficients[2])
    return ResidualModelResult(
        alpha=float(coefficients[0]),
        beta=float(coefficients[1]),
        industry_beta=industry_beta,
        residuals={
            day: float(value) for day, value in zip(common, residual, strict=True)
        },
        r_squared=r_squared,
        capm_r_squared=capm_r2,
        model=model,
        market_source=market_source,
        window=window,
        observations=len(common),
        missing_dates=max(0, window - len(common)),
    )
