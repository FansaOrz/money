"""PSR/DSR 的非正态、短样本和多次尝试惩罚测试。"""

from app.services.quant_stats import deflated_sharpe, probabilistic_sharpe


def test_short_sample_reports_minimum_track_record() -> None:
    returns = [0.001, -0.0005, 0.0012, -0.0004, 0.0008] * 4
    result = probabilistic_sharpe(returns)
    assert result is not None
    assert result.sample_count == 20
    assert result.minimum_track_record_length is not None
    assert result.skew is not None
    assert result.kurtosis is not None


def test_many_trials_deflate_same_observed_sharpe() -> None:
    returns = [0.01, -0.01, 0.01, -0.01, 0.0002] * 50
    one = deflated_sharpe(returns, 1)
    many = deflated_sharpe(returns, 100)
    assert one is not None and many is not None
    assert many.dsr < one.dsr
    assert many.expected_max_sr > one.expected_max_sr
