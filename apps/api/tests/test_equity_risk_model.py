"""协方差风险、因子风险与跟踪误差。"""

import numpy as np
import pytest

from app.services.equity_risk_model import (
    portfolio_risk,
    psd_covariance,
    tracking_error,
)


def test_correlated_portfolio_is_riskier_than_low_correlation() -> None:
    covariance_high = np.array([[0.0001, 0.00009], [0.00009, 0.0001]])
    covariance_low = np.array([[0.0001, 0.0], [0.0, 0.0001]])
    high = portfolio_risk([0.5, 0.5], covariance_high)
    low = portfolio_risk([0.5, 0.5], covariance_low)
    assert high["total_variance"] > low["total_variance"]


def test_covariance_is_psd_and_exact_benchmark_copy_has_zero_te() -> None:
    generator = np.random.default_rng(20260805)
    result = psd_covariance(generator.normal(size=(100, 4)))
    assert np.linalg.eigvalsh(result["covariance"]).min() > 0
    te = tracking_error(
        [0.4, 0.6], [0.4, 0.6], np.eye(2) * 0.0001
    )
    assert te["predicted_tracking_error"] == pytest.approx(0.0)
