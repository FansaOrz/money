"""量化验证工具测试：纯函数解析对照 + 服务/路由集成。

重点验证：
1. quant_stats 纯函数与手工解析结果一致（CVaR95、Calmar、信息比率、
   Rank IC/Spearman、五档单调性、Deflated Sharpe、邻域稳定性、
   block bootstrap White Reality Check 的确定性）；
2. quant_costs 费用模型：买 0.15%、卖默认 0.5%/7 日内 1.5%、lot FIFO；
3. quant_snapshot：QDII lag2 / 国内 lag1 的有效数据日、前值填充对齐；
4. 服务集成：run_validation 端到端（含费用扣减与快照截断）；
5. 路由：POST /api/quant/validation、GET /api/quant/snapshot。

使用合成的确定性净值序列，不依赖外部行情。
"""

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Account, FundNav, Instrument, Position, Transaction, TransactionType
from app.schemas.quant import ValidationRequest
from app.services import quant_costs as costs
from app.services import quant_snapshot as snap
from app.services import quant_stats as stats
from app.services import quant_validation as validation


# ---------------------------------------------------------------------------
# 数据构造辅助
# ---------------------------------------------------------------------------


def _seed_navs(
    db: Session,
    code: str,
    name: str,
    days: int = 260,
    start_nav: float = 1.0,
    daily_growth: float = 0.001,
    start: date = date(2025, 1, 6),
    skip_weekdays: tuple[int, ...] | None = None,
) -> Instrument:
    """写入一只基金及叠加双频正弦噪声的趋势净值序列（daily_growth 可为负）。

    噪声 = 0.008×sin(2πi/13) + 0.004×sin(2πi/5 + code 相关相位)，使各基金
    路径彼此不同、含真实回撤、横截面动量有区分度（Rank IC 可计算）。
    skip_weekdays 指定时跳过对应星期（如 (5, 6) 跳过周末），用于构造
    稀疏披露（QDII）的净值序列。
    """
    instrument = Instrument(code=code, name=name)
    db.add(instrument)
    db.flush()
    phase = sum(ord(ch) for ch in code) % 7  # 按代码分散相位
    nav = start_nav
    written = 0
    day = start
    while written < days:
        if skip_weekdays is None or day.weekday() not in skip_weekdays:
            db.add(
                FundNav(
                    instrument_id=instrument.id,
                    nav_date=day,
                    unit_nav=Decimal(f"{nav:.6f}"),
                    accumulated_nav=Decimal(f"{nav:.6f}"),
                    source="test",
                )
            )
            noise = 0.008 * math.sin(2 * math.pi * written / 13.0) + 0.004 * math.sin(
                2 * math.pi * written / 5.0 + phase
            )
            nav *= 1 + daily_growth + noise
            written += 1
        day += timedelta(days=1)
    db.commit()
    return instrument


def _seed_position(db: Session, instrument: Instrument, market_value: str = "10000.00") -> Account:
    account = Account(name=f"账户-{instrument.code}")
    db.add(account)
    db.flush()
    db.add(
        Position(
            account_id=account.id,
            instrument_id=instrument.id,
            shares=Decimal("1000"),
            cost=Decimal(market_value),
            market_value=Decimal(market_value),
        )
    )
    db.commit()
    return account


# ---------------------------------------------------------------------------
# quant_stats：基础统计与风险指标（解析对照）
# ---------------------------------------------------------------------------


class TestBasicStats:
    def test_skewness_symmetric_is_zero(self) -> None:
        # 对称分布偏度为 0
        assert stats.skewness([1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.0, abs=1e-12)

    def test_skewness_analytic(self) -> None:
        # 手工计算：[1,1,2]：mean=4/3，m2=(2×(1/3)²+(2/3)²)/3=(2/9+4/9)/3=2/9，
        # m3=(2×(-1/3)³+(2/3)³)/3=(-2/27+8/27)/3=2/27
        # γ3 = (2/27) / (2/9)^1.5 = (2/27) / (2√2/27) = 1/√2 ≈ 0.7071
        assert stats.skewness([1.0, 1.0, 2.0]) == pytest.approx(1.0 / math.sqrt(2.0), rel=1e-9)

    def test_kurtosis_normal_like(self) -> None:
        # [1,2,3,4]：mean=2.5，m2=1.25，m4=(2×2.25²... 计算：偏差 ±1.5,±0.5
        # m2=(2.25+0.25+0.25+2.25)/4=1.25；m4=(5.0625+0.0625+0.0625+5.0625)/4=2.5625
        # γ4 = 2.5625/1.5625 - 3 = 1.64 - 3 = -1.36
        assert stats.kurtosis([1.0, 2.0, 3.0, 4.0]) == pytest.approx(-1.36, rel=1e-9)

    def test_cvar95_tail_mean(self) -> None:
        # 100 个收益：最差 5%（5 个）均值 = (-0.05-0.04-0.03-0.02-0.01)/5 = -0.03
        returns = [-0.05, -0.04, -0.03, -0.02, -0.01] + [0.01] * 95
        assert stats.cvar95(returns) == pytest.approx(-0.03, rel=1e-9)

    def test_cvar95_small_sample_ceil(self) -> None:
        # 20 个样本：ceil(0.05×20)=1，取最小值
        returns = [0.01] * 19 + [-0.02]
        assert stats.cvar95(returns) == pytest.approx(-0.02, rel=1e-9)

    def test_cvar95_empty(self) -> None:
        assert stats.cvar95([]) is None

    def test_calmar(self) -> None:
        # 252 日总收益 0.21 → 年化 = 1.21-1 = 0.21；回撤 -0.10 → Calmar = 2.1
        calmar = stats.calmar_ratio(total_return=0.21, periods=252, max_dd=-0.10)
        assert calmar == pytest.approx(2.1, rel=1e-9)

    def test_calmar_zero_drawdown_is_infinite_for_positive_return(self) -> None:
        assert stats.calmar_ratio(total_return=0.1, periods=100, max_dd=0.0) == float("inf")

    def test_information_ratio_analytic(self) -> None:
        # 策略每日跑赢基准 0.001：主动收益恒为 0.001 → 跟踪误差 0 → None
        strategy = [0.011] * 50
        benchmark = [0.010] * 50
        assert stats.information_ratio(strategy, benchmark) is None
        # 主动收益 [0.002, 0.000, -0.001, 0.003]：mean=0.001，
        # 样本 std = sqrt(((0.001)²+(−0.001)²+(−0.002)²+(0.002)²)/3)
        #          = sqrt((1e-6+1e-6+4e-6+4e-6)/3) = sqrt(10e-6/3)
        strategy = [0.012, 0.010, 0.009, 0.013]
        benchmark = [0.010] * 4
        te = math.sqrt(10e-6 / 3)
        expected = 0.001 / te * math.sqrt(252)
        assert stats.information_ratio(strategy, benchmark) == pytest.approx(expected, rel=1e-9)


# ---------------------------------------------------------------------------
# quant_stats：Rank IC 与五档单调性（解析对照）
# ---------------------------------------------------------------------------


class TestRankIC:
    def test_perfect_positive(self) -> None:
        assert stats.rank_ic([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)

    def test_perfect_negative(self) -> None:
        assert stats.rank_ic([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_no_correlation(self) -> None:
        # 秩 (1,2,3) vs (2,1,3)：d = (1,1,0)... 用解析式：
        # ranks x=[1,2,3], y=[2,1,3]；mean=2；cov=((1-2)(2-2)+(2-2)(1-2)+(3-2)(3-2))/3=1/3
        # var=((1)+(0)+(1))/3=2/3；ρ=(1/3)/(2/3)=0.5
        assert stats.rank_ic([1, 2, 3], [2, 1, 3]) == pytest.approx(0.5, rel=1e-9)

    def test_ties_average_rank(self) -> None:
        # x=[1,1,2]：秩 [1.5,1.5,3]；y=[1,2,3]：秩 [1,2,3]
        # mean_x=2, mean_y=2
        # cov=((1.5-2)(1-2)+(1.5-2)(2-2)+(3-2)(3-2))/3=(0.5+0+1)/3=0.5
        # var_x=((0.25)+(0.25)+(1))/3=0.5；var_y=(1+0+1)/3=2/3
        # ρ=0.5/sqrt(0.5×2/3)=0.5/sqrt(1/3)≈0.8660254
        assert stats.rank_ic([1.0, 1.0, 2.0], [1.0, 2.0, 3.0]) == pytest.approx(
            0.8660254037844386, rel=1e-9
        )

    def test_constant_series_is_none(self) -> None:
        assert stats.rank_ic([1, 1, 1, 1], [1, 2, 3, 4]) is None

    def test_too_few_samples(self) -> None:
        assert stats.rank_ic([1, 2], [1, 2]) is None


class TestQuintileMonotonicity:
    def test_monotonic_quintiles(self) -> None:
        # 20 个样本，分数升序，前瞻收益严格递增 5 组
        scores = list(range(20))
        forward = [0.01] * 4 + [0.02] * 4 + [0.03] * 4 + [0.04] * 4 + [0.05] * 4
        result = stats.quintile_monotonicity(scores, forward)
        assert result is not None
        assert result.quintile_returns == pytest.approx([0.01, 0.02, 0.03, 0.04, 0.05])
        assert result.spread == pytest.approx(0.04)
        assert result.kendall_tau == pytest.approx(1.0)
        assert result.monotonic is True

    def test_inverted_quintiles(self) -> None:
        scores = list(range(20))
        forward = [0.05] * 4 + [0.04] * 4 + [0.03] * 4 + [0.02] * 4 + [0.01] * 4
        result = stats.quintile_monotonicity(scores, forward)
        assert result is not None
        assert result.kendall_tau == pytest.approx(-1.0)
        assert result.monotonic is False
        assert result.spread == pytest.approx(-0.04)

    def test_too_few_samples(self) -> None:
        assert stats.quintile_monotonicity([1, 2, 3], [1, 2, 3]) is None


# ---------------------------------------------------------------------------
# quant_stats：Deflated Sharpe（解析对照）
# ---------------------------------------------------------------------------


class TestDeflatedSharpe:
    def test_norm_ppf_standard(self) -> None:
        assert stats._norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
        assert stats._norm_ppf(0.975) == pytest.approx(1.959964, abs=1e-5)
        assert stats._norm_ppf(0.025) == pytest.approx(-1.959964, abs=1e-5)

    def test_expected_max_sharpe_single_trial(self) -> None:
        assert stats.expected_max_sharpe(1, 0.1) == 0.0

    def test_expected_max_sharpe_grows_with_trials(self) -> None:
        e10 = stats.expected_max_sharpe(10, 0.1)
        e100 = stats.expected_max_sharpe(100, 0.1)
        assert 0 < e10 < e100

    @staticmethod
    def _noisy_returns(days: int = 120) -> list[float]:
        # 确定性正弦噪声（对称、零偏度），漂移较小使年化 SR≈1.1，
        # 保证 SR 方差项 (1 - γ3·SR + (γ4-1)/4·SR²) 为正
        return [0.03 * math.sin(2 * math.pi * i / 7.0) + 0.002 for i in range(days)]

    def test_records_moments_and_trial_count(self) -> None:
        returns = self._noisy_returns()
        result = stats.deflated_sharpe(returns, trial_count=25)
        assert result is not None
        assert result.trial_count == 25
        assert result.sample_count == 120
        # 正弦噪声对称 → 偏度≈0、峰度为负（近正态/均匀混合）
        assert result.skew == pytest.approx(0.0, abs=1e-9)
        assert result.kurtosis is not None and result.kurtosis < 0
        assert result.sharpe is not None and result.sharpe > 0
        assert 0.0 <= result.dsr <= 1.0
        assert result.expected_max_sr > 0

    def test_dsr_decreases_with_more_trials(self) -> None:
        returns = self._noisy_returns()
        dsr1 = stats.deflated_sharpe(returns, trial_count=1)
        dsr100 = stats.deflated_sharpe(returns, trial_count=100)
        assert dsr1 is not None and dsr100 is not None
        # 试验数越多，期望最大夏普越高，DSR（超越运气上界的概率）越低
        assert dsr100.expected_max_sr > dsr1.expected_max_sr
        assert dsr100.dsr <= dsr1.dsr

    def test_zero_volatility_is_none(self) -> None:
        assert stats.deflated_sharpe([0.001] * 30, trial_count=10) is None

    def test_too_few_samples(self) -> None:
        assert stats.deflated_sharpe([0.01], trial_count=5) is None


# ---------------------------------------------------------------------------
# quant_stats：White Reality Check（确定性与解析性质）
# ---------------------------------------------------------------------------


class TestWhiteRealityCheck:
    def test_deterministic_with_seed(self) -> None:
        strategy = [0.002 * math.sin(i / 3.0) + 0.001 for i in range(60)]
        benchmark = [0.0005] * 60
        r1 = stats.white_reality_check(strategy, benchmark, resamples=100, seed=7)
        r2 = stats.white_reality_check(strategy, benchmark, resamples=100, seed=7)
        assert r1 is not None and r2 is not None
        assert r1 == r2

    def test_strong_outperformance_low_p(self) -> None:
        # 策略每天稳定跑赢基准 0.2%（叠加噪声使统计量可估）：主动收益
        # 显著为正，去均值重抽样的零假设分布以 0 为中心，p 值应很小
        strategy = [0.012 + 0.005 * math.sin(2 * math.pi * i / 9.0) for i in range(60)]
        benchmark = [0.010] * 60
        result = stats.white_reality_check(strategy, benchmark, resamples=200, seed=1)
        assert result is not None
        assert result.p_value <= 0.05
        assert result.observed_stat > 0
        # 零假设分布均值接近 0
        assert abs(result.null_mean) < abs(result.observed_stat)

    def test_no_skill_high_p(self) -> None:
        # 策略与基准几乎相同（主动收益围绕 0 小幅波动）：统计量接近 0，
        # 零假设分布以 0 为中心，p 值应较大（无法拒绝"无超额能力"）
        noise = [0.0005 * math.sin(2 * math.pi * i / 7.0) for i in range(60)]
        strategy = [0.001 * ((i % 3) - 1) + noise[i] for i in range(60)]
        benchmark = [0.001 * ((i % 3) - 1) for i in range(60)]
        result = stats.white_reality_check(strategy, benchmark, resamples=200, seed=5)
        assert result is not None
        # 主动收益均值 ≈ 0 → 统计量接近 0，p 值不显著
        assert result.p_value > 0.05

    def test_zero_active_return_stat(self) -> None:
        # 主动收益恒为 0（log 序列方差 0，均值 0）→ 统计量 None
        assert stats.mean_log_sharpe([0.0] * 30) is None
        # 恒定正主动收益（零波动但方向明确）→ 统计量取正向饱和值
        assert stats.mean_log_sharpe([0.001] * 30) == pytest.approx(8.0)
        # 恒定负主动收益 → 负向饱和值
        assert stats.mean_log_sharpe([-0.001] * 30) == pytest.approx(-8.0)

    def test_block_length_default_sqrt(self) -> None:
        strategy = [0.002 * math.sin(i / 5.0) for i in range(64)]
        benchmark = [0.0] * 64
        result = stats.white_reality_check(strategy, benchmark, resamples=50, seed=3)
        assert result is not None
        assert result.block_length == round(math.sqrt(64)) == 8

    def test_circular_block_bootstrap_preserves_length(self) -> None:
        import random

        rng = random.Random(0)
        sample = stats.circular_block_bootstrap(list(range(10)), size=25, block_length=4, rng=rng)
        assert len(sample) == 25
        # 所有值都来自原序列
        assert all(0 <= v < 10 for v in sample)


# ---------------------------------------------------------------------------
# quant_stats：邻域稳定性（解析对照）
# ---------------------------------------------------------------------------


class TestNeighborhoodStability:
    def test_center_best(self) -> None:
        # 中心为邻域最优：分位数 = (4 + 0)/5 = 0.8
        result = stats.neighborhood_stability(1.0, [0.5, 0.6, 0.7, 0.8])
        assert result is not None
        assert result.center_value == 1.0
        assert result.neighborhood_quantile == pytest.approx(0.8)
        assert result.neighbor_count == 5
        # 去掉 min/max 后的带：[0.6, 0.8]
        assert result.band_low == pytest.approx(0.6)
        assert result.band_high == pytest.approx(0.8)

    def test_center_worst(self) -> None:
        result = stats.neighborhood_stability(0.1, [0.5, 0.6, 0.7])
        assert result is not None
        assert result.neighborhood_quantile == pytest.approx(0.0)

    def test_ties_count_half(self) -> None:
        # 邻域 [0.5, 1.0, 0.6]：中心 1.0 有 1 个并列（除自身）→ (2 + 0.5)/4 = 0.625
        result = stats.neighborhood_stability(1.0, [0.5, 1.0, 0.6])
        assert result is not None
        assert result.neighborhood_quantile == pytest.approx(0.625)

    def test_empirical_quantile(self) -> None:
        assert stats.empirical_quantile([1, 2, 3, 4, 5], 0.5) == pytest.approx(3.0)
        assert stats.empirical_quantile([1, 2, 3, 4], 0.25) == pytest.approx(1.75)


# ---------------------------------------------------------------------------
# quant_costs：费用模型（解析对照）
# ---------------------------------------------------------------------------


class TestCostModel:
    def test_default_rates(self) -> None:
        assert costs.DEFAULT_BUY_FEE_RATE == pytest.approx(0.0015)
        assert costs.DEFAULT_SELL_FEE_RATE == pytest.approx(0.005)
        assert costs.SHORT_TERM_SELL_FEE_RATE == pytest.approx(0.015)
        assert costs.SHORT_TERM_HOLD_DAYS == 7

    def test_sell_fee_rate_short_term(self) -> None:
        # 持有 <7 个自然日 → 1.5%
        assert costs.sell_fee_rate(0) == pytest.approx(0.015)
        assert costs.sell_fee_rate(6) == pytest.approx(0.015)
        # ≥7 日 → 0.5%
        assert costs.sell_fee_rate(7) == pytest.approx(0.005)
        assert costs.sell_fee_rate(30) == pytest.approx(0.005)
        # 未知持有期 → 默认
        assert costs.sell_fee_rate(None) == pytest.approx(0.005)

    def test_sell_fee_rate_negative_hold_days_default(self) -> None:
        """负持有期（数据异常：卖出日早于 lot 买入日）按默认费率，不触发惩罚性短持费率。"""
        assert costs.sell_fee_rate(-1) == pytest.approx(0.005)
        assert costs.sell_fee_rate(-30) == pytest.approx(0.005)

    def test_estimate_sell_fee_future_lot_default_rate(self) -> None:
        """lot 买入日晚于卖出日（负持有期）时该 lot 按默认费率加权。"""
        lots = [costs.ShareLot(buy_date=date(2025, 7, 1), shares=100.0)]
        rate, covered = costs.estimate_sell_fee(lots, shares=50.0, sell_date=date(2025, 6, 1))
        assert rate == pytest.approx(0.005)
        assert covered == pytest.approx(50.0)

    def test_estimate_sell_fee_fifo(self) -> None:
        # 两个 lot：10 天前 60 份、3 天前 40 份；卖出 80 份
        # FIFO：先卖 60 份（10 天，0.5%），再卖 20 份（3 天，1.5%）
        # 加权费率 = (60×0.005 + 20×0.015) / 80 = (0.3+0.3)/80 = 0.0075
        lots = [
            costs.ShareLot(buy_date=date(2025, 6, 1), shares=60.0),
            costs.ShareLot(buy_date=date(2025, 6, 8), shares=40.0),
        ]
        rate, covered = costs.estimate_sell_fee(lots, shares=80.0, sell_date=date(2025, 6, 11))
        assert rate == pytest.approx(0.0075, rel=1e-9)
        assert covered == pytest.approx(80.0)

    def test_estimate_sell_fee_all_short_term(self) -> None:
        lots = [costs.ShareLot(buy_date=date(2025, 6, 10), shares=100.0)]
        rate, _ = costs.estimate_sell_fee(lots, shares=50.0, sell_date=date(2025, 6, 12))
        assert rate == pytest.approx(0.015)

    def test_estimate_sell_fee_no_lots_default(self) -> None:
        rate, covered = costs.estimate_sell_fee([], shares=100.0, sell_date=date(2025, 6, 12))
        assert rate == pytest.approx(0.005)
        assert covered == pytest.approx(100.0)

    def test_apply_costs_to_returns(self) -> None:
        # 第 2 天买 10% 卖 5%：fee = 0.1×0.0015 + 0.05×0.005 = 0.0004
        # r' = (1+0.01)×(1-0.0004)-1 = 0.0101×0.9996-1 ≈ 0.0095960
        returns = [0.01, 0.01, 0.01]
        adjusted = costs.apply_costs_to_returns(returns, [(1, 0.10, 0.05)])
        assert adjusted[0] == pytest.approx(0.01)
        assert adjusted[1] == pytest.approx(1.01 * 0.9996 - 1, rel=1e-9)
        assert adjusted[2] == pytest.approx(0.01)
        # 原序列不被修改
        assert returns == [0.01, 0.01, 0.01]


class TestLoadOpenLots:
    def test_fifo_reconstruction(self, db_session: Session) -> None:
        instrument = Instrument(code="F001", name="测试基金")
        db_session.add(instrument)
        db_session.flush()
        account = Account(name="测试账户")
        db_session.add(account)
        db_session.flush()

        def tx(tx_type: TransactionType, day: date, shares: str) -> None:
            db_session.add(
                Transaction(
                    account_id=account.id,
                    instrument_id=instrument.id,
                    type=tx_type,
                    trade_date=day,
                    shares=Decimal(shares),
                    amount=Decimal("1000.00"),
                )
            )

        tx(TransactionType.BUY, date(2025, 5, 1), "100")
        tx(TransactionType.BUY, date(2025, 6, 1), "50")
        tx(TransactionType.SELL, date(2025, 6, 10), "120")  # 吃掉 100 + 20
        tx(TransactionType.REINVEST, date(2025, 6, 15), "10")
        db_session.commit()

        lots = costs.load_open_lots(db_session, instrument.id)
        assert len(lots) == 2
        # 剩余：6/1 批次 30 份 + 6/15 再投 10 份
        assert lots[0].buy_date == date(2025, 6, 1)
        assert lots[0].shares == pytest.approx(30.0)
        assert lots[1].buy_date == date(2025, 6, 15)
        assert lots[1].shares == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# quant_snapshot：lag 与有效数据日（解析对照）
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_is_qdii(self) -> None:
        assert snap.is_qdii("华夏全球精选QDII")
        assert snap.is_qdii("qdii 基金")
        assert not snap.is_qdii("易方达消费行业")

    def test_default_lag(self) -> None:
        assert snap.default_lag_days("华夏全球QDII") == 2
        assert snap.default_lag_days("易方达消费行业") == 1

    def test_effective_nav_date_domestic(self) -> None:
        days = [date(2025, 6, 2) + timedelta(days=i) for i in range(10)]
        # lag1：as_of 6/11 → 用 6/10（前一日净值，当日净值尚未披露）
        assert snap.effective_nav_date(days, date(2025, 6, 11), 1) == date(2025, 6, 10)
        # as_of 恰好是净值日 → lag1 仍用前一天
        assert snap.effective_nav_date(days, date(2025, 6, 10), 1) == date(2025, 6, 9)

    def test_effective_nav_date_qdii(self) -> None:
        days = [date(2025, 6, 2) + timedelta(days=i) for i in range(10)]
        # lag2：as_of 6/11 → 用 6/9（前 2 个净值日）
        assert snap.effective_nav_date(days, date(2025, 6, 11), 2) == date(2025, 6, 9)
        # as_of 6/10 → 候选 <6/10 为 6/2..6/9 共 8 天，lag2 → 6/8
        assert snap.effective_nav_date(days, date(2025, 6, 10), 2) == date(2025, 6, 8)

    def test_effective_nav_date_insufficient(self) -> None:
        days = [date(2025, 6, 10)]
        assert snap.effective_nav_date(days, date(2025, 6, 11), 2) is None

    def test_available_trade_days_union(self) -> None:
        series = {
            "A": [(date(2025, 6, 1), 1.0), (date(2025, 6, 3), 1.1)],
            "B": [(date(2025, 6, 2), 2.0), (date(2025, 6, 3), 2.1)],
        }
        assert snap.available_trade_days(series) == [
            date(2025, 6, 1),
            date(2025, 6, 2),
            date(2025, 6, 3),
        ]

    def test_load_snapshot_panels_forward_fill(self, db_session: Session) -> None:
        # A 每天披露，B（QDII）每周一/三/五披露 → 并集日历 + 前值填充
        base = date(2025, 6, 2)  # 周一
        a = Instrument(code="A001", name="国内基金A")
        b = Instrument(code="B001", name="海外QDII基金B")
        db_session.add_all([a, b])
        db_session.flush()
        for i in range(10):
            db_session.add(
                FundNav(
                    instrument_id=a.id,
                    nav_date=base + timedelta(days=i),
                    unit_nav=Decimal(f"{1.0 + 0.01 * i:.6f}"),
                    accumulated_nav=Decimal(f"{1.0 + 0.01 * i:.6f}"),
                    source="test",
                )
            )
        for i in (0, 2, 4, 7, 9):  # B 稀疏披露
            db_session.add(
                FundNav(
                    instrument_id=b.id,
                    nav_date=base + timedelta(days=i),
                    unit_nav=Decimal(f"{2.0 + 0.02 * i:.6f}"),
                    accumulated_nav=Decimal(f"{2.0 + 0.02 * i:.6f}"),
                    source="test",
                )
            )
        db_session.commit()

        calendar, panels, snapshots, warnings = snap.load_snapshot_panels(
            db_session, [a, b], as_of=None, min_samples=2
        )
        assert len(calendar) == 10
        assert len(panels["A001"]) == 10
        assert len(panels["B001"]) == 10
        # B 在 6/3（base+1）缺测 → 前值 2.0（base+0 的值）
        assert panels["B001"][1] == pytest.approx(2.0)
        assert panels["B001"][2] == pytest.approx(2.04)
        # 快照标记 B 为 QDII
        snap_b = next(s for s in snapshots if s.code == "B001")
        assert snap_b.is_qdii is True
        assert snap_b.lag_days == 2

    def test_load_snapshot_panels_as_of_lag(self, db_session: Session) -> None:
        base = date(2025, 6, 2)
        a = Instrument(code="C001", name="国内基金C")
        db_session.add(a)
        db_session.flush()
        for i in range(10):
            db_session.add(
                FundNav(
                    instrument_id=a.id,
                    nav_date=base + timedelta(days=i),
                    unit_nav=Decimal("1.0"),
                    accumulated_nav=Decimal("1.0"),
                    source="test",
                )
            )
        db_session.commit()
        # as_of = base+5（6/7），lag1 → 有效净值日 base+4（6/6，当日净值未披露）
        effective = snap.effective_nav_date(
            [base + timedelta(days=i) for i in range(10)], base + timedelta(days=5), 1
        )
        assert effective == base + timedelta(days=4)


# ---------------------------------------------------------------------------
# 服务集成：run_validation
# ---------------------------------------------------------------------------


def _validation_payload(**overrides) -> ValidationRequest:
    defaults = dict(
        top_n=2,
        rebalance_interval=1,
        include_costs=False,
        trial_count=5,
        bootstrap_resamples=100,
        seed=7,
    )
    defaults.update(overrides)
    from app.schemas.quant import WalkForwardWindow

    return ValidationRequest(
        window=WalkForwardWindow(train_window=40, test_window=10, step=10),
        **defaults,
    )


class TestRunValidation:
    def _seed_candidates(self, db: Session) -> list[Instrument]:
        funds = [
            _seed_navs(db, "V001", "沪深300指数基金", daily_growth=0.0012),
            _seed_navs(db, "V002", "中证500指数基金", daily_growth=0.0008),
            _seed_navs(db, "V003", "纳斯达克QDII", daily_growth=0.0015),
        ]
        for instrument in funds:
            _seed_position(db, instrument)
        return funds

    def test_end_to_end(self, db_session: Session) -> None:
        self._seed_candidates(db_session)
        result = validation.run_validation(db_session, _validation_payload())

        assert len(result.candidate_codes) == 3
        assert result.oos_count > 0
        assert result.sample_count == 260
        # 风险指标存在
        assert result.strategy.sharpe is not None
        assert result.strategy.cvar95 is not None
        assert result.strategy.max_drawdown is not None
        assert result.strategy.calmar is not None
        # 信息比率与超额
        assert result.information_ratio is not None
        assert result.excess_return is not None
        # 预测有效性
        assert result.predictiveness.rank_ic_count > 0
        assert result.predictiveness.rank_ic_mean is not None
        assert len(result.predictiveness.quintile_returns) == 5
        # 稳健性
        assert result.robustness.trial_count == 5
        assert result.robustness.skew is not None
        assert result.robustness.kurtosis is not None
        assert result.robustness.deflated_sharpe is not None
        assert 0.0 <= result.robustness.deflated_sharpe <= 1.0
        assert result.robustness.reality_check_p is not None
        assert result.robustness.bootstrap_resamples == 100
        # 邻域
        assert result.neighborhood.center_sharpe is not None
        assert result.neighborhood.neighbor_count > 1
        # 快照：QDII 标记
        qdii_snap = next(s for s in result.fund_snapshots if s.code == "V003")
        assert qdii_snap.is_qdii is True
        assert qdii_snap.lag_days == 2
        domestic_snap = next(s for s in result.fund_snapshots if s.code == "V001")
        assert domestic_snap.lag_days == 1
        # 无费用时 basis 默认、零扣费
        assert result.costs.include_costs is False
        assert result.costs.total_fee_ratio == 0.0

    def test_with_costs_reduces_return(self, db_session: Session) -> None:
        self._seed_candidates(db_session)
        free = validation.run_validation(db_session, _validation_payload(include_costs=False))
        paid = validation.run_validation(db_session, _validation_payload(include_costs=True))

        assert paid.costs.include_costs is True
        assert paid.costs.total_fee_ratio > 0
        assert paid.costs.trade_days > 0
        # 无流水 → 默认费率
        assert paid.costs.sell_fee_basis == "default"
        # 扣费后总收益下降
        assert paid.strategy.total_return is not None
        assert free.strategy.total_return is not None
        assert paid.strategy.total_return < free.strategy.total_return

    def test_as_of_truncates_data(self, db_session: Session) -> None:
        self._seed_candidates(db_session)
        # 数据从 2025-01-06 起 260 个自然日（含跳过 None → 每天写入）
        full = validation.run_validation(db_session, _validation_payload())
        # as_of 提前 60 天：样本数应减少
        as_of = date(2025, 1, 6) + timedelta(days=259 - 60)
        trimmed = validation.run_validation(
            db_session, _validation_payload(as_of=as_of.isoformat())
        )
        assert trimmed.sample_count < full.sample_count
        assert trimmed.end_date <= as_of.isoformat()
        # QDII lag2：有效净值日 ≤ as_of - 2 个净值日
        qdii_snap = next(s for s in trimmed.fund_snapshots if s.code == "V003")
        assert qdii_snap.effective_date is not None
        assert qdii_snap.effective_date < trimmed.as_of

    def test_deterministic_bootstrap(self, db_session: Session) -> None:
        self._seed_candidates(db_session)
        r1 = validation.run_validation(db_session, _validation_payload(seed=11))
        r2 = validation.run_validation(db_session, _validation_payload(seed=11))
        assert r1.robustness.reality_check_p == r2.robustness.reality_check_p


# ---------------------------------------------------------------------------
# 路由：POST /api/quant/validation、GET /api/quant/snapshot
# ---------------------------------------------------------------------------


class TestRoutes:
    def test_validation_endpoint(self, client: TestClient, db_session: Session) -> None:
        funds = [
            _seed_navs(db_session, "R001", "沪深300指数", daily_growth=0.001),
            _seed_navs(db_session, "R002", "中证500指数", daily_growth=0.0008),
            _seed_navs(db_session, "R003", "纳斯达克QDII", daily_growth=0.0012),
        ]
        for instrument in funds:
            _seed_position(db_session, instrument)

        response = client.post(
            "/api/quant/validation",
            json={
                "window": {"train_window": 40, "test_window": 10, "step": 10},
                "top_n": 2,
                "include_costs": True,
                "trial_count": 10,
                "bootstrap_resamples": 100,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["sample_count"] == 260
        assert body["strategy"]["cvar95"] is not None
        assert body["strategy"]["calmar"] is not None
        assert body["information_ratio"] is not None
        assert body["predictiveness"]["rank_ic_mean"] is not None
        assert len(body["predictiveness"]["quintile_returns"]) == 5
        assert body["robustness"]["trial_count"] == 10
        assert body["robustness"]["skew"] is not None
        assert body["robustness"]["kurtosis"] is not None
        assert body["robustness"]["deflated_sharpe"] is not None
        assert body["robustness"]["reality_check_p"] is not None
        assert body["neighborhood"]["center_sharpe"] is not None
        assert body["costs"]["buy_fee_rate"] == pytest.approx(0.0015)
        assert body["costs"]["sell_fee_rate"] == pytest.approx(0.005)
        assert body["costs"]["short_term_sell_fee_rate"] == pytest.approx(0.015)
        assert body["methodology"]

    def test_validation_endpoint_insufficient_data(self, client: TestClient) -> None:
        response = client.post(
            "/api/quant/validation",
            json={"candidate_codes": ["NOPE1", "NOPE2"]},
        )
        assert response.status_code == 400

    def test_snapshot_endpoint(self, client: TestClient, db_session: Session) -> None:
        funds = [
            _seed_navs(db_session, "S001", "沪深300指数", daily_growth=0.001),
            _seed_navs(db_session, "S002", "纳斯达克QDII", daily_growth=0.0012),
        ]
        for instrument in funds:
            _seed_position(db_session, instrument)

        response = client.get("/api/quant/snapshot")
        assert response.status_code == 200
        body = response.json()
        assert body["trade_day_count"] == 260
        assert len(body["trade_days"]) == 260
        assert body["truncated"] is False
        assert body["as_of"] == body["trade_days"][-1]
        by_code = {f["code"]: f for f in body["funds"]}
        assert by_code["S001"]["lag_days"] == 1
        assert by_code["S001"]["is_qdii"] is False
        assert by_code["S002"]["lag_days"] == 2
        assert by_code["S002"]["is_qdii"] is True
        assert by_code["S001"]["nav_count"] == 260

    def test_snapshot_endpoint_with_as_of(self, client: TestClient, db_session: Session) -> None:
        funds = [
            _seed_navs(db_session, "T001", "沪深300指数", daily_growth=0.001),
            _seed_navs(db_session, "T002", "中证500指数", daily_growth=0.0008),
        ]
        for instrument in funds:
            _seed_position(db_session, instrument)

        # 净值从 2025-01-06 起每天写入；as_of = 第 100 天
        as_of = date(2025, 1, 6) + timedelta(days=99)
        response = client.get(f"/api/quant/snapshot?as_of={as_of.isoformat()}")
        assert response.status_code == 200
        body = response.json()
        assert body["as_of"] == as_of.isoformat()
        # lag1：有效净值日 = as_of 前一天
        expected_effective = (as_of - timedelta(days=1)).isoformat()
        assert body["funds"][0]["effective_date"] == expected_effective

    def test_snapshot_endpoint_codes_filter(self, client: TestClient, db_session: Session) -> None:
        funds = [
            _seed_navs(db_session, "U001", "沪深300指数", daily_growth=0.001),
            _seed_navs(db_session, "U002", "中证500指数", daily_growth=0.0008),
        ]
        for instrument in funds:
            _seed_position(db_session, instrument)

        response = client.get("/api/quant/snapshot?codes=U001")
        assert response.status_code == 200
        body = response.json()
        assert len(body["funds"]) == 1
        assert body["funds"][0]["code"] == "U001"
