"""量化研究服务：指标计算与轻量回测。

设计约束：
- 仅使用标准库（math/statistics/datetime/decimal），不依赖 pandas/numpy；
- 数据源为本地 FundNav 表；装载时区分两个口径（见 _load_dual_nav_series）：
  连续总收益指数（含分红，用于全部指标与信号）与单位净值（成交价），
  禁止逐点在累计/单位净值之间切换造成单位混用与收益跳变；
- 信号驱动策略（ma_cross / macd）T 日收盘后生成信号，T+1 日按单位净值成交；
- 回测风险指标基于 TWR 日收益（剔除定投等现金流）计算；
- 全部为只读研究能力，不产生任何实盘下单行为；
- 响应规模受控：净值曲线按周抽样，信号上限 100 条。
"""

from __future__ import annotations

import importlib
import math
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import fmean

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    FundHolding,
    FundIndustryAllocation,
    FundNav,
    FundNewsImpact,
    Instrument,
    NewsEvent,
    Position,
)
from app.schemas.quant import (
    BacktestRequest,
    BacktestResult,
    EquityPoint,
    FundIndicators,
    HoldingMetrics,
    PortfolioMetricsSummary,
    ResearchSignal,
    ResearchSignalItem,
    SignalFilters,
    SignalListResponse,
    TradeSignal,
)
from app.services.fund_advice import build_fund_advice

TRADING_DAYS_PER_YEAR = 252
DEFAULT_RISK_FREE_RATE = 0.02
MAX_CURVE_POINTS = 260  # 约一年按周抽样
MAX_SIGNALS = 100
MIN_SAMPLES = 30  # 指标计算所需的最少净值样本
LOOKBACK_DAYS = 365  # 组合回溯窗口（自然日）

# MACD 柱口径：hist = MACD_HIST_FACTOR × (DIF - DEA)。
# 国内行情软件普遍展示 2×(DIF-DEA)，取 2 与之保持一致。
MACD_HIST_FACTOR = 2.0

# 网格触发的浮点容差：净值按 6 位小数存储，grid_step 边界（如恰好 ±10%）
# 可能因二进制浮点偏差而漏触发，用微小容差把「恰好触及网格线」视为触发。
_GRID_FLOAT_EPS = 1e-9


class QuantError(ValueError):
    """量化参数或数据不足错误，路由层转换为 400。"""


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise QuantError(f"日期格式错误：{value}，应为 YYYY-MM-DD") from exc


def _load_instrument(db: Session, code: str) -> Instrument:
    instrument = db.scalar(select(Instrument).where(Instrument.code == code))
    if instrument is None:
        raise QuantError(f"未找到基金 {code}，请先在持仓或净值同步中录入")
    return instrument


def _load_nav_series(
    db: Session,
    instrument_id: int,
    start: date | None = None,
    end: date | None = None,
    limit: int = 2000,
) -> list[tuple[date, float]]:
    """读取净值序列（升序）。优先累计净值以含分红，缺失时用单位净值。

    说明：这是给 screener / walkforward 等只关心「含分红价格序列」的调用方
    保留的兼容入口；单基金指标与回测请使用 _load_dual_nav_series，
    以免在累计净值缺测的日期发生单位混用。
    """
    return _load_dual_nav_series(db, instrument_id, start=start, end=end, limit=limit).total_series


class NavSeriesPair:
    """同一基金日历对齐的双净值序列。

    - total_series：分红再投资总收益指数。根据“累计净值－单位净值”的
      增量识别每份分红，再按除息日单位净值复投；不能直接用累计净值端点比，
      因为累计净值是历史分红的简单累加，并非复权价格；
    - unit_series：单位净值（交易成交价口径），缺失累计净值的日子回退为
      当日总收益指数值（该日单位/累计本就一致，单位不受缩放影响）。
    """

    def __init__(
        self,
        total_series: list[tuple[date, float]],
        unit_series: list[tuple[date, float]],
        unit_fallback: bool,
    ) -> None:
        self.total_series = total_series
        self.unit_series = unit_series
        self.unit_fallback = unit_fallback

    @property
    def dates(self) -> list[date]:
        return [d for d, _ in self.total_series]

    @property
    def total_values(self) -> list[float]:
        return [v for _, v in self.total_series]

    @property
    def unit_values(self) -> list[float]:
        return [v for _, v in self.unit_series]


def _load_dual_nav_series(
    db: Session,
    instrument_id: int,
    start: date | None = None,
    end: date | None = None,
    limit: int = 2000,
) -> NavSeriesPair:
    """装载双净值序列：连续总收益序列（指标用）+ 单位净值序列（成交价用）。

    装载顺序：先在 SQL 层按 start/end 过滤，再按日期 DESC 取最近 limit 条，
    最后反转为升序 —— limit 截断保留的是区间内最新的样本（窗口语义），
    而不是最早 limit 条（否则长历史 + 小窗口会错过最近行情）。

    总收益序列构造规则：
    - 相邻两日“累计净值－单位净值”的正增量视为当日每份现金分红；
    - 当日总收益因子 =（当日单位净值 + 当日分红）/ 前一日单位净值；
    - 累计净值缺失时只能按单位净值收益兜底，并标记 unit_fallback。
    """
    stmt = select(FundNav.nav_date, FundNav.accumulated_nav, FundNav.unit_nav).where(
        FundNav.instrument_id == instrument_id
    )
    if start is not None:
        stmt = stmt.where(FundNav.nav_date >= start)
    if end is not None:
        stmt = stmt.where(FundNav.nav_date <= end)
    # DESC LIMIT 保留最新 limit 条，反转为升序供窗口指标使用
    stmt = stmt.order_by(FundNav.nav_date.desc()).limit(limit)
    rows = db.execute(stmt).all()
    points: list[tuple[date, float, float | None]] = []
    for nav_date, accumulated_nav, unit_nav in reversed(rows):
        if unit_nav is None or float(unit_nav) <= 0:
            continue
        acc = float(accumulated_nav) if accumulated_nav is not None else None
        if acc is not None and acc <= 0:
            acc = None
        points.append((nav_date, float(unit_nav), acc))

    n = len(points)
    total_values: list[float] = [0.0] * n
    unit_fallback = any(acc is None for _day, _unit, acc in points)
    if n:
        # 起点与单位净值同尺度，后续逐日复权；这样回测按单位净值买入的份额
        # 可以直接乘总收益指数估值。
        total_values[0] = points[0][1]
    for i in range(1, n):
        _prev_day, prev_unit, prev_acc = points[i - 1]
        _day, unit, acc = points[i]
        distribution = 0.0
        if prev_acc is not None and acc is not None:
            previous_paid = prev_acc - prev_unit
            current_paid = acc - unit
            # 净值只保留 4～6 位小数，允许极小舍入误差；只有明显正增量才
            # 视为现金分红，防止舍入噪声被误复投。
            increase = current_paid - previous_paid
            if increase > 0.00005:
                distribution = increase
        total_values[i] = total_values[i - 1] * (unit + distribution) / prev_unit

    total_series = [(points[i][0], total_values[i]) for i in range(n)]
    # 成交价一律用单位净值；单位净值必然存在（见上方过滤）
    unit_series = [(day, unit) for day, unit, _acc in points]
    return NavSeriesPair(total_series, unit_series, unit_fallback)


# ---------------------------------------------------------------------------
# 基础指标（纯函数，便于单测）
# ---------------------------------------------------------------------------


def _daily_returns(values: list[float]) -> list[float]:
    return [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0]


def _period_return(values: list[float], window: int) -> float | None:
    """近 window 个区间的收益率，需要 window+1 个样本。"""
    if len(values) < window + 1:
        return None
    base = values[-window - 1]
    if base <= 0:
        return None
    return values[-1] / base - 1.0


def _shift_months(day: date, months: int) -> date:
    """把日期向前移动 months 个月，月末日期自动收敛到目标月末。"""
    absolute_month = day.year * 12 + day.month - 1 - months
    year, month_zero = divmod(absolute_month, 12)
    month = month_zero + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _calendar_period_return(
    series: list[tuple[date, float]],
    *,
    months: int,
) -> float | None:
    """按自然月区间计算收益，起点取目标日期当日或之前最近一个净值日。"""
    if len(series) < 2:
        return None
    end_date, end_value = series[-1]
    target = _shift_months(end_date, months)
    start_value: float | None = None
    for nav_date, value in series:
        if nav_date > target:
            break
        if value > 0:
            start_value = value
    if start_value is None or start_value <= 0:
        return None
    return end_value / start_value - 1.0


def _annual_volatility(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = fmean(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR)


def _max_drawdown(values: list[float]) -> float | None:
    """最大回撤（负数小数），如 -0.15 表示从高点最多跌去 15%。"""
    if len(values) < 2:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = value / peak - 1.0
            if drawdown < worst:
                worst = drawdown
    return worst


def _annual_return(total_return: float, trading_days: int) -> float | None:
    if trading_days < 0 or total_return <= -1.0:
        return None
    if trading_days == 0:
        return total_return  # 单日序列无法年化，按总收益返回
    return (1.0 + total_return) ** (TRADING_DAYS_PER_YEAR / trading_days) - 1.0


def _sharpe(returns: list[float], risk_free_rate: float = DEFAULT_RISK_FREE_RATE) -> float | None:
    if len(returns) < 2:
        return None
    daily_rf = risk_free_rate / TRADING_DAYS_PER_YEAR
    excess = [r - daily_rf for r in returns]
    mean = fmean(excess)
    variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    return mean / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def _win_rate(returns: list[float]) -> float | None:
    """日收益胜率：正收益日占比（小数）。"""
    if not returns:
        return None
    wins = sum(1 for r in returns if r > 0)
    return wins / len(returns)


def _moving_average(values: list[float], window: int) -> list[float | None]:
    """简单移动平均序列，不足窗口的位置为 None。"""
    result: list[float | None] = []
    rolling = 0.0
    for i, value in enumerate(values):
        rolling += value
        if i >= window:
            rolling -= values[i - window]
        if i >= window - 1:
            result.append(rolling / window)
        else:
            result.append(None)
    return result


def _ema_series(values: list[float], span: int) -> list[float]:
    """指数移动平均（adjust 风格的近似：以首个值初始化，alpha=2/(span+1)）。"""
    if not values:
        return []
    alpha = 2.0 / (span + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _macd_series(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float], list[float], list[float]]:
    """返回 (dif, dea, hist) 三条等长序列。

    口径说明：hist = 2 × (DIF - DEA)，与国内行情软件的 MACD 红绿柱一致
    （MACD_HIST_FACTOR 控制；取 1.0 时退化为 DIF-DEA 差值口径）。
    """
    if not values:
        return [], [], []
    ema_fast = _ema_series(values, fast)
    ema_slow = _ema_series(values, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow, strict=True)]
    dea = _ema_series(dif, signal)
    hist = [MACD_HIST_FACTOR * (d - e) for d, e in zip(dif, dea, strict=True)]
    return dif, dea, hist


# ---------------------------------------------------------------------------
# 趋势信号（可解释）
# ---------------------------------------------------------------------------


def _trend_signal(
    values: list[float],
    ma20: list[float | None],
    ma60: list[float | None],
    dif: list[float],
    dea: list[float],
) -> tuple[str, list[str]]:
    """综合 MA 与 MACD 给出趋势信号与理由。"""
    if not values or ma20[-1] is None:
        return "neutral", ["样本不足 20 日，无法判断均线趋势"]

    score = 0
    reasons: list[str] = []
    price = values[-1]
    latest_ma20 = ma20[-1]
    latest_ma60 = ma60[-1] if ma60 else None

    if latest_ma20:
        if price >= latest_ma20:
            score += 1
            reasons.append(f"现价 {price:.4f} 位于 MA20 {latest_ma20:.4f} 之上")
        else:
            score -= 1
            reasons.append(f"现价 {price:.4f} 跌破 MA20 {latest_ma20:.4f}")
    if latest_ma20 and latest_ma60:
        if latest_ma20 >= latest_ma60:
            score += 1
            reasons.append(f"MA20({latest_ma20:.4f}) ≥ MA60({latest_ma60:.4f})，中期多头排列")
        else:
            score -= 1
            reasons.append(f"MA20({latest_ma20:.4f}) < MA60({latest_ma60:.4f})，中期空头排列")
    elif latest_ma60 is None:
        reasons.append("样本不足 60 日，缺少 MA60 参考")

    if dif and dea:
        if dif[-1] > dea[-1]:
            score += 1
            reasons.append(f"MACD DIF({dif[-1]:.4f}) 在 DEA({dea[-1]:.4f}) 上方，动能偏多")
        else:
            score -= 1
            reasons.append(f"MACD DIF({dif[-1]:.4f}) 在 DEA({dea[-1]:.4f}) 下方，动能偏空")

    if score >= 2:
        return "strong_up", reasons
    if score == 1:
        return "up", reasons
    if score == -1:
        return "down", reasons
    if score <= -2:
        return "strong_down", reasons
    return "neutral", reasons or ["多空因素抵消，趋势不明朗"]


# ---------------------------------------------------------------------------
# 单基金指标
# ---------------------------------------------------------------------------


def compute_fund_indicators(db: Session, code: str) -> FundIndicators:
    """计算单基金 20/60/250 日收益、年化波动、最大回撤、夏普、MA、MACD 与趋势信号。

    全部指标基于连续总收益指数（含分红）计算，避免累计净值缺测区间的
    单位净值拼接造成收益跳变。
    """
    instrument = _load_instrument(db, code)
    series = _load_dual_nav_series(db, instrument.id).total_series
    if len(series) < 2:
        raise QuantError(f"基金 {code} 净值样本不足（{len(series)} 条），请先同步历史净值")

    dates = [d for d, _ in series]
    values = [v for _, v in series]
    returns = _daily_returns(values)

    ma20 = _moving_average(values, 20)
    ma60 = _moving_average(values, 60)
    dif, dea, hist = _macd_series(values)
    trend, reasons = _trend_signal(values, ma20, ma60, dif, dea)
    return_20d = _period_return(values, 20)
    return_60d = _period_return(values, 60)
    annual_volatility = _annual_volatility(returns)
    max_drawdown = _max_drawdown(values)
    sharpe = _sharpe(returns)

    return FundIndicators(
        code=instrument.code,
        name=instrument.name,
        start_date=dates[0].isoformat(),
        end_date=dates[-1].isoformat(),
        sample_count=len(series),
        return_20d=return_20d,
        return_60d=return_60d,
        return_250d=_period_return(values, 250),
        return_1y=_calendar_period_return(series, months=12),
        annual_volatility=annual_volatility,
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        win_rate=_win_rate(returns),
        ma20=ma20[-1],
        ma60=ma60[-1],
        macd_dif=dif[-1] if dif else None,
        macd_dea=dea[-1] if dea else None,
        macd_hist=hist[-1] if hist else None,
        trend_signal=trend,
        trend_reasons=reasons,
        advice=build_fund_advice(
            sample_count=len(series),
            trend_signal=trend,
            return_20d=return_20d,
            return_60d=return_60d,
            annual_volatility=annual_volatility,
            max_drawdown=max_drawdown,
            sharpe=sharpe,
        ),
    )


# ---------------------------------------------------------------------------
# 回测引擎
# ---------------------------------------------------------------------------


def _summarize_curve(
    curve: list[tuple[date, float]], initial_capital: float
) -> tuple[float | None, float | None, float | None, float | None]:
    """由资金曲线计算总收益、年化收益、最大回撤、夏普。"""
    if len(curve) < 2:
        return None, None, None, None
    values = [v for _, v in curve]
    total_return = (values[-1] / initial_capital - 1.0) if initial_capital > 0 else None
    annual = _annual_return(total_return, len(values) - 1) if total_return is not None else None
    return total_return, annual, _max_drawdown(values), _sharpe(_daily_returns(values))


def _sample_curve(curve: list[tuple[date, float]]) -> list[EquityPoint]:
    """控制响应规模：超过上限时均匀抽样。"""
    if len(curve) <= MAX_CURVE_POINTS:
        sampled = curve
    else:
        step = len(curve) / MAX_CURVE_POINTS
        indices = sorted({int(i * step) for i in range(MAX_CURVE_POINTS)} | {len(curve) - 1})
        sampled = [curve[i] for i in indices]
    return [EquityPoint(date=d.isoformat(), value=round(v, 2)) for d, v in sampled]


# 信号驱动策略最少样本：MA 窗口 + 2（窗口就绪并预留相邻两点判断交叉）
_STRATEGY_MIN_SAMPLES: dict[str, int] = {
    "buy_hold": 2,
    "ma_cross": 2,  # 运行时按 slow_window + 2 校验
    "macd": 35,  # slow(26) + signal(9) 经验最小值，提前明确报错
    "dca": 2,
    "grid": 2,
}


def _summarize_twr_returns(
    twr_returns: list[float], total_return: float | None
) -> tuple[float | None, float | None, float | None]:
    """由 TWR 日收益序列（已剔除现金流）计算年化收益、最大回撤、夏普。

    回撤在 TWR 财富指数（日收益连乘，现金流中性）上计算，
    避免定投注入资金被误判为「回撤修复」或「收益」。
    """
    if total_return is None or len(twr_returns) < 2:
        return None, None, None
    wealth: list[float] = [1.0]
    for r in twr_returns:
        wealth.append(wealth[-1] * (1.0 + r))
    return (
        _annual_return(total_return, len(twr_returns)),
        _max_drawdown(wealth),
        _sharpe(twr_returns),
    )


def _run_backtest(
    req: BacktestRequest, pair: NavSeriesPair
) -> tuple[list[tuple[date, float]], list[float], list[TradeSignal], dict[str, float | int | str]]:
    """按策略执行回测，返回资金曲线、TWR 日收益序列、信号与实际参数。

    口径约定（全策略统一）：
    - 成交价一律使用单位净值；指标/信号使用连续总收益指数（含分红）；
    - MA/MACD 信号于 T 日收盘后生成，T+1 日按单位净值成交（无未来函数）；
    - 资金曲线用于展示与总收益；风险指标基于 TWR 日收益（剔除定投等
      现金流的影响）计算；
    - 以净值成交，无手续费与滑点；不允许卖空，卖出不超过持仓；
    - 网格策略以最近一次真实成交价为基准，且只有真实成交才记录信号。
    """
    dates = pair.dates
    prices = pair.unit_values  # 成交价：单位净值
    indicators = pair.total_values  # 指标/信号：连续总收益指数
    # 估值口径：累计净值连续（含分红）时按总收益指数估值，使现金分红
    # 留存于组合价值；单位净值兜底区间两者一致，自动回退单位净值估值
    valuations = indicators if not pair.unit_fallback else prices
    cash = req.initial_capital
    shares = 0.0
    invested = req.initial_capital  # 定投策略的实际投入本金（含后续追加）
    prev_value = req.initial_capital
    curve: list[tuple[date, float]] = []
    twr_returns: list[float] = []
    signals: list[TradeSignal] = []
    params: dict[str, float | int | str] = {"strategy": req.strategy}

    def record_buy(day: date, price: float, amount: float, reason: str) -> bool:
        """按金额买入，返回是否真实成交。"""
        nonlocal cash, shares
        if amount <= 0 or price <= 0:
            return False
        spend = min(amount, cash)
        if spend <= 0:
            return False
        cash -= spend
        shares += spend / price
        if len(signals) < MAX_SIGNALS:
            signals.append(
                TradeSignal(
                    date=day.isoformat(), action="buy", price=round(price, 4),
                    shares=round(spend / price, 4), amount=round(spend, 2), reason=reason,
                )
            )
        return True

    def record_sell(day: date, price: float, amount: float, reason: str) -> bool:
        """按金额卖出，返回是否真实成交。"""
        nonlocal cash, shares
        if amount <= 0 or price <= 0 or shares <= 0:
            return False
        sell_shares = min(amount / price, shares)
        if sell_shares <= 0:
            return False
        proceeds = sell_shares * price
        shares -= sell_shares
        cash += proceeds
        if len(signals) < MAX_SIGNALS:
            signals.append(
                TradeSignal(
                    date=day.isoformat(), action="sell", price=round(price, 4),
                    shares=round(sell_shares, 4), amount=round(proceeds, 2), reason=reason,
                )
            )
        return True

    def mark_with_flow(i: int, flow: float) -> None:
        """记录第 i 天收盘后的总市值与 TWR 日收益（flow 为当日净流入）。

        组合日收益 = (当日总市值 - 当日净流入) / 昨日总市值 - 1，
        即修正 Dietz（期初加权）近似：现金流从分子剔除，使日收益只反映
        净值波动；TWR 指标（年化/回撤/夏普）在这些日收益的连乘财富指数上计算。
        """
        nonlocal prev_value
        value = cash + shares * valuations[i]
        curve.append((dates[i], value))
        if i > 0:
            twr_returns.append((value - flow) / prev_value - 1.0 if prev_value > 0 else 0.0)
        prev_value = value

    if req.strategy == "buy_hold":
        record_buy(dates[0], prices[0], cash, "买入并持有：建仓")
        for i in range(len(prices)):
            mark_with_flow(i, 0.0)

    elif req.strategy == "ma_cross":
        params.update({"fast_window": req.fast_window, "slow_window": req.slow_window})
        if req.fast_window >= req.slow_window:
            raise QuantError("ma_cross 策略要求 fast_window < slow_window")
        min_needed = req.slow_window + 2
        if len(prices) < min_needed:
            raise QuantError(
                f"ma_cross 策略需要至少 {min_needed} 条净值样本"
                f"（slow_window={req.slow_window} + 2），当前区间仅 {len(prices)} 条，"
                "请扩大回测区间或缩短均线窗口"
            )
        fast_ma = _moving_average(indicators, req.fast_window)
        slow_ma = _moving_average(indicators, req.slow_window)
        holding = False
        entered = False
        mark_with_flow(0, 0.0)
        for i in range(1, len(prices)):
            # T 日（i-1）收盘后生成信号，T+1 日（i）按单位净值成交
            j = i - 1
            f_prev, s_prev = fast_ma[j - 1] if j >= 1 else None, slow_ma[j - 1] if j >= 1 else None
            f_now, s_now = fast_ma[j], slow_ma[j]
            if f_now is not None and s_now is not None:
                if not holding:
                    # 金叉买入；若窗口就绪时已处于多头排列（回测起点），直接顺势建仓
                    if (f_prev is not None and s_prev is not None and f_prev <= s_prev and f_now > s_now) or (
                        not entered and f_now > s_now
                    ):
                        record_buy(
                            dates[i], prices[i], cash,
                            f"MA{req.fast_window} 上穿 MA{req.slow_window} 金叉买入（T 日信号，T+1 成交）",
                        )
                        holding = True
                        entered = True
                elif f_prev is not None and s_prev is not None and f_prev >= s_prev and f_now < s_now:
                    record_sell(
                        dates[i], prices[i], shares * prices[i],
                        f"MA{req.fast_window} 下穿 MA{req.slow_window} 死叉卖出（T 日信号，T+1 成交）",
                    )
                    holding = False
            mark_with_flow(i, 0.0)

    elif req.strategy == "macd":
        params.update({"macd_fast": req.macd_fast, "macd_slow": req.macd_slow, "macd_signal": req.macd_signal})
        if req.macd_fast >= req.macd_slow:
            raise QuantError("macd 策略要求 macd_fast < macd_slow")
        min_needed = req.macd_slow + req.macd_signal
        if len(prices) < min_needed:
            raise QuantError(
                f"macd 策略需要至少 {min_needed} 条净值样本"
                f"（slow={req.macd_slow} + signal={req.macd_signal}），当前区间仅 {len(prices)} 条，"
                "请扩大回测区间"
            )
        dif, dea, _ = _macd_series(indicators, req.macd_fast, req.macd_slow, req.macd_signal)
        holding = False
        entered = False
        mark_with_flow(0, 0.0)
        for i in range(1, len(prices)):
            # T 日（i-1）收盘后生成信号，T+1 日（i）按单位净值成交
            j = i - 1
            if not holding:
                # 金叉买入；若回测起点 DIF 已在 DEA 上方，直接顺势建仓
                if (dif[j - 1] <= dea[j - 1] and dif[j] > dea[j]) or (not entered and dif[j] > dea[j]):
                    record_buy(dates[i], prices[i], cash, "MACD DIF 上穿 DEA 金叉买入（T 日信号，T+1 成交）")
                    holding = True
                    entered = True
            elif dif[j - 1] >= dea[j - 1] and dif[j] < dea[j]:
                record_sell(dates[i], prices[i], shares * prices[i], "MACD DIF 下穿 DEA 死叉卖出（T 日信号，T+1 成交）")
                holding = False
            mark_with_flow(i, 0.0)

    elif req.strategy == "dca":
        params.update({"invest_interval": req.invest_interval, "invest_amount": req.invest_amount})
        # 首期用初始资金投入，之后每期追加 invest_amount（模拟外部现金流）
        record_buy(dates[0], prices[0], cash, "定投：首期建仓")
        mark_with_flow(0, 0.0)
        for i in range(1, len(prices)):
            flow = 0.0
            if i % req.invest_interval == 0:
                cash += req.invest_amount
                invested += req.invest_amount
                flow = req.invest_amount
                record_buy(dates[i], prices[i], req.invest_amount, f"定投：第 {i // req.invest_interval + 1} 期投入")
            mark_with_flow(i, flow)

    elif req.strategy == "grid":
        params.update({"grid_step": req.grid_step, "grid_amount": req.grid_amount})
        record_buy(dates[0], prices[0], cash / 2, "网格：首建半仓")
        # 基准锚定最近一次真实成交价：只有真实成交才移动锚点，
        # 未成交（资金/持仓不足）不记录信号也不移动锚点
        anchor = prices[0]
        mark_with_flow(0, 0.0)
        for i in range(1, len(prices)):
            price = prices[i]
            if anchor > 0 and price <= anchor * (1 - req.grid_step) * (1 + _GRID_FLOAT_EPS):
                if record_buy(dates[i], price, req.grid_amount, f"网格：价格较基准下跌 ≥{req.grid_step:.0%} 买入一格"):
                    anchor = price
            elif anchor > 0 and price >= anchor * (1 + req.grid_step) * (1 - _GRID_FLOAT_EPS):
                if shares * price >= req.grid_amount and record_sell(
                    dates[i], price, req.grid_amount, f"网格：价格较基准上涨 ≥{req.grid_step:.0%} 卖出一格"
                ):
                    anchor = price
            mark_with_flow(i, 0.0)

    # 计算收益率时的基准：定投按累计投入本金，其余按初始资金
    capital_base = invested if req.strategy == "dca" else req.initial_capital
    params["capital_base"] = round(capital_base, 2)
    return curve, twr_returns, signals, params


def run_backtest(db: Session, req: BacktestRequest) -> BacktestResult:
    """回测入口：装载双净值序列（成交价=单位净值，指标=连续总收益指数）、执行策略、汇总指标。"""
    instrument = _load_instrument(db, req.code)
    start = _parse_day(req.start_date)
    end = _parse_day(req.end_date)
    if start and end and start > end:
        raise QuantError("start_date 不能晚于 end_date")

    pair = _load_dual_nav_series(db, instrument.id, start=start, end=end)
    min_samples = _STRATEGY_MIN_SAMPLES.get(req.strategy, 2)
    if len(pair.total_series) < min_samples:
        raise QuantError(
            f"基金 {req.code} 在该区间净值样本不足（{len(pair.total_series)} 条），"
            f"{req.strategy} 策略至少需要 {min_samples} 条"
        )

    curve, twr_returns, signals, params = _run_backtest(req, pair)
    capital_base = float(params.pop("capital_base"))
    # 总收益按实际市值与累计本金计算；年化/回撤/夏普基于 TWR 日收益（剔除现金流）
    total_return, _, _, _ = _summarize_curve(curve, capital_base)
    annual, mdd, sharpe = _summarize_twr_returns(twr_returns, total_return)

    return BacktestResult(
        code=instrument.code,
        name=instrument.name,
        strategy=req.strategy,
        params=params,
        start_date=pair.total_series[0][0].isoformat(),
        end_date=pair.total_series[-1][0].isoformat(),
        initial_capital=capital_base,
        final_value=round(curve[-1][1], 2),
        total_return=total_return,
        annual_return=annual,
        max_drawdown=mdd,
        sharpe=sharpe,
        trade_count=len(signals),
        curve=_sample_curve(curve),
        signals=signals,
    )


# ---------------------------------------------------------------------------
# 组合指标摘要与研究信号
# ---------------------------------------------------------------------------

BACKTEST_METHODOLOGY = (
    "当前权重回溯组合：以当前持仓市值权重为固定权重（不做再平衡），"
    "取各基金 FundNav 净值（优先累计净值）在近一年的日收益，"
    "按日加权求和构造组合日收益序列；"
    "日期对齐采用共同日期优先、当日缺测基金以前值（零收益）填充；"
    "窗口内始终无净值的持仓权重在分子分母中同时剔除；"
    "年化按 252 个交易日折算，夏普比率采用 2% 无风险利率。"
    "幸存者偏差声明：回溯基于当前持仓基金（当前候选池），历史时点已清盘/"
    "调出池的基金不在样本内，回溯指标可能系统性偏好存活至今的基金。"
)


def _backtested_portfolio_returns(
    nav_series: dict[int, list[tuple[date, float]]],
    weights: dict[int, float],
    lookback_days: int = LOOKBACK_DAYS,
) -> tuple[list[tuple[date, float]], str | None]:
    """用当前权重对 FundNav 序列回溯构造组合日收益序列。

    对齐规则：以窗口内全部净值日期的并集为日历；当日无净值的基金
    按前值填充（即当日收益记 0）；当日所有持仓均无有效收益时跳过该日。

    返回 (日收益序列(升序), as_of 日期)；数据不足时返回 ([], None)。
    """
    series_by_id: dict[int, list[tuple[date, float]]] = {}
    for instrument_id, weight in weights.items():
        if weight <= 0:
            continue
        series = nav_series.get(instrument_id) or []
        if len(series) >= 2:
            series_by_id[instrument_id] = series
    if not series_by_id:
        return [], None

    as_of = max(series[-1][0] for series in series_by_id.values())
    window_start = as_of - timedelta(days=lookback_days)

    # 预计算每只基金窗口内的日收益；首日收益相对窗口前最后一个净值（前值对齐）
    returns_by_id: dict[int, dict[date, float]] = {}
    for instrument_id, series in series_by_id.items():
        daily: dict[date, float] = {}
        prev_value = series[0][1]
        for nav_date, value in series[1:]:
            if nav_date > window_start and prev_value > 0:
                daily[nav_date] = value / prev_value - 1.0
            prev_value = value
        if daily:
            returns_by_id[instrument_id] = daily
    if not returns_by_id:
        return [], None

    calendar = sorted({d for daily in returns_by_id.values() for d in daily})
    portfolio_returns: list[tuple[date, float]] = []
    for day in calendar:
        numerator = 0.0
        denominator = 0.0
        for instrument_id, weight in weights.items():
            daily = returns_by_id.get(instrument_id)
            if daily is None:
                continue  # 窗口内无净值：权重从分子分母同时剔除
            denominator += weight
            numerator += weight * daily.get(day, 0.0)  # 当日缺测按前值填充，收益记 0
        if denominator > 0:
            portfolio_returns.append((day, numerator / denominator))
    return portfolio_returns, as_of.isoformat()


def _portfolio_metrics_from_returns(
    returns: list[tuple[date, float]],
) -> dict[str, float | None]:
    """由组合日收益序列计算累计净值并汇总各项指标。"""
    if len(returns) < 2:
        return {
            "total_return_rate": None,
            "annualized_return": None,
            "annualized_volatility": None,
            "max_drawdown": None,
            "sharpe_ratio": None,
            "win_rate": None,
        }
    values: list[float] = [1.0]
    for _, r in returns:
        values.append(values[-1] * (1.0 + r))
    daily = [r for _, r in returns]
    total_return = values[-1] - 1.0
    return {
        "total_return_rate": total_return,
        "annualized_return": _annual_return(total_return, len(daily)),
        "annualized_volatility": _annual_volatility(daily),
        "max_drawdown": _max_drawdown(values),
        "sharpe_ratio": _sharpe(daily),
        "win_rate": _win_rate(daily),
    }


def list_fund_indicators(db: Session) -> list[FundIndicators]:
    """计算当前持仓基金指标并合并持仓市值。

    无净值数据（或样本不足）的基金不再丢弃，而是返回 data_available=false
    的占位项（指标字段为 None，仍携带持仓市值），便于前端完整展示持仓。
    """
    from sqlalchemy import func

    rows = db.execute(
        select(Instrument, func.sum(Position.market_value), func.sum(Position.cost))
        .join(Position, Position.instrument_id == Instrument.id)
        .group_by(Instrument.id)
        .order_by(Instrument.code)
    ).all()

    results: list[FundIndicators] = []
    for instrument, market_value, cost in rows:
        value = market_value if market_value is not None else cost
        try:
            indicators = compute_fund_indicators(db, instrument.code)
        except QuantError:
            indicators = FundIndicators(
                code=instrument.code,
                name=instrument.name,
                start_date="",
                end_date="",
                sample_count=0,
                data_available=False,
                trend_signal="neutral",
                trend_reasons=["无净值数据或样本不足，指标不可用"],
            )
        indicators.market_value = value
        results.append(indicators)
    return results


def portfolio_metrics_summary(db: Session) -> PortfolioMetricsSummary:
    """组合指标摘要：集中度、各持仓趋势、可解释研究信号。"""
    from sqlalchemy import func

    rows = db.execute(
        select(
            Instrument,
            func.sum(Position.shares),
            func.sum(Position.cost),
            func.sum(Position.market_value),
        )
        .join(Instrument, Position.instrument_id == Instrument.id)
        .group_by(Instrument.id)
        .order_by(Instrument.id)
    ).all()

    holdings_raw: list[tuple[Instrument, Decimal]] = []
    for instrument, _shares, cost, market_value in rows:
        value = market_value if market_value is not None else (cost or Decimal("0"))
        holdings_raw.append((instrument, value))

    total_mv = sum((v for _, v in holdings_raw), Decimal("0"))
    total_mv_float = float(total_mv)

    weights: list[float] = []
    holdings: list[HoldingMetrics] = []
    signals: list[ResearchSignal] = []

    sorted_holdings = sorted(holdings_raw, key=lambda item: item[1], reverse=True)

    nav_series_by_id: dict[int, list[tuple[date, float]]] = {}
    weights_by_id: dict[int, float] = {}

    for instrument, value in sorted_holdings:
        weight = (float(value) / total_mv_float) if total_mv_float > 0 else None
        if weight is not None:
            weights.append(weight)
            weights_by_id[instrument.id] = weight

        trend_signal = None
        return_20d = return_60d = max_dd = None
        series = _load_nav_series(db, instrument.id)
        nav_series_by_id[instrument.id] = series
        if len(series) >= 2:
            values = [v for _, v in series]
            return_20d = _period_return(values, 20)
            return_60d = _period_return(values, 60)
            max_dd = _max_drawdown(values)
            if len(series) >= MIN_SAMPLES:
                try:
                    indicators = compute_fund_indicators(db, instrument.code)
                    trend_signal = indicators.trend_signal
                except QuantError:
                    trend_signal = None

        holdings.append(
            HoldingMetrics(
                code=instrument.code,
                name=instrument.name,
                market_value=value,
                weight=weight,
                trend_signal=trend_signal,
                return_20d=return_20d,
                return_60d=return_60d,
                max_drawdown=max_dd,
            )
        )

    # ---- 集中度信号 ----
    top1 = weights[0] if weights else None
    top3 = sum(weights[:3]) if weights else None
    hhi = sum(w * w for w in weights) if weights else None

    if top1 is not None and top1 >= 0.5:
        signals.append(ResearchSignal(
            category="concentration", level="risk",
            message=f"第一大持仓占比 {top1:.1%}，超过 50%，组合高度集中于 {sorted_holdings[0][0].name}",
        ))
    elif top1 is not None and top1 >= 0.3:
        signals.append(ResearchSignal(
            category="concentration", level="warning",
            message=f"第一大持仓占比 {top1:.1%}，集中度偏高，建议关注单一基金风险",
        ))
    if hhi is not None and hhi >= 0.25 and len(weights) > 1:
        signals.append(ResearchSignal(
            category="concentration", level="warning",
            message=f"组合 HHI 为 {hhi:.2f}（≥0.25 视为高集中），分散度不足",
        ))
    if weights and not signals:
        signals.append(ResearchSignal(
            category="concentration", level="info",
            message=f"组合共 {len(weights)} 只基金，第一大权重 {top1:.1%}，集中度处于温和区间",
        ))

    # ---- 趋势信号（按权重排序，最多展示前 5 只弱趋势持仓）----
    down_holdings = sorted(
        (h for h in holdings if h.trend_signal in ("strong_down", "down")),
        key=lambda h: h.weight or 0.0,
        reverse=True,
    )
    up_holdings = [h for h in holdings if h.trend_signal in ("strong_up", "up")]
    for holding in down_holdings[:5]:
        signals.append(ResearchSignal(
            category="trend", level="warning",
            message=f"{holding.name}（{holding.code}）趋势偏弱"
            + (f"，近20日收益 {holding.return_20d:.1%}" if holding.return_20d is not None else "")
            + "，可关注是否需减仓或观察",
        ))
    if len(down_holdings) > 5:
        signals.append(ResearchSignal(
            category="trend", level="info",
            message=f"另有 {len(down_holdings) - 5} 只持仓趋势偏弱，详见持仓列表",
        ))
    if up_holdings and not down_holdings:
        signals.append(ResearchSignal(
            category="trend", level="info",
            message=f"{len(up_holdings)} 只持仓趋势向上，整体动能偏多",
        ))

    # ---- 回撤信号（最多展示前 5 条）----
    drawdown_hits = [
        h for h in holdings
        if h.max_drawdown is not None and h.max_drawdown <= -0.2 and (h.weight or 0) >= 0.1
    ]
    for holding in drawdown_hits[:5]:
        signals.append(ResearchSignal(
            category="drawdown", level="risk",
            message=f"{holding.name}（{holding.code}）近一年最大回撤 {holding.max_drawdown:.1%}"
            f"且权重 {(holding.weight or 0):.1%}，对组合波动贡献较大",
        ))

    # ---- 当前权重回溯组合指标（近一年）----
    portfolio_returns, as_of = _backtested_portfolio_returns(nav_series_by_id, weights_by_id)
    portfolio_metrics = _portfolio_metrics_from_returns(portfolio_returns)

    # 风险级别在前，控制总体规模
    level_order = {"risk": 0, "warning": 1, "info": 2}
    signals.sort(key=lambda s: level_order[s.level])
    signals = signals[:20]

    return PortfolioMetricsSummary(
        total_market_value=total_mv,
        position_count=len(holdings),
        concentration_top1=top1,
        concentration_top3=top3,
        hhi=hhi,
        methodology=BACKTEST_METHODOLOGY,
        as_of=as_of,
        holdings=holdings,
        signals=signals,
        **portfolio_metrics,
    )


# ---------------------------------------------------------------------------
# 综合研究信号（独立入口，不改动上方 portfolio_metrics_summary 的行为）
# ---------------------------------------------------------------------------

NEWS_LOOKBACK_DAYS = 7  # 相关新闻统计窗口（自然日）

# 基金名称关键词 -> 粗略市场映射（用于选择对应市场指数）
_MARKET_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("us", ("纳斯达克", "标普", "美国", "美股", "海外", "全球", "道琼斯", "美元")),
    ("hk", ("恒生", "港股", "香港", "H股", "中概", "沪港深", "大中华")),
    ("cn", ("沪深", "中证", "上证", "创业板", "科创", "A股", "红利")),
]


def _signal_as_of(*candidates: date | datetime | None) -> str:
    """取各数据源截止日期的最大值作为信号基准日；无数据时回退当天。"""
    values: list[date] = []
    for candidate in candidates:
        if candidate is None:
            continue
        values.append(candidate.date() if isinstance(candidate, datetime) else candidate)
    if not values:
        return date.today().isoformat()
    return max(values).isoformat()


def _guess_market(fund_name: str) -> str:
    """按基金名称关键词粗略判断主要市场：us / hk / cn。"""
    for market, keywords in _MARKET_KEYWORDS:
        if any(keyword in fund_name for keyword in keywords):
            return market
    return "cn"


def _load_index_models() -> tuple[type | None, type | None]:
    """动态导入 MarketIndex / IndexQuote 模型；不存在时优雅降级返回 (None, None)。"""
    try:
        module = importlib.import_module("app.models")
        index_model = getattr(module, "MarketIndex", None)
        quote_model = getattr(module, "IndexQuote", None)
        return index_model, quote_model
    except Exception:  # noqa: BLE001 - 降级路径，任何导入失败都不影响主流程
        return None, None


def _index_quote_series(
    db: Session,
    index_model: type,
    quote_model: type,
    index_id: int,
    start: date | None = None,
    end: date | None = None,
    limit: int = 400,
) -> tuple[list[tuple[date, float]], date | None]:
    """读取指数行情序列（升序）。字段名做防御式探测，缺失时返回空序列。

    与基金净值装载同一口径：先按 start/end 过滤，再 DESC LIMIT 取最近
    limit 条后反转为升序，limit 截断保留的是最新行情而非最早样本。
    """
    date_field = None
    for candidate in ("quote_date", "trade_date", "nav_date", "date"):
        if hasattr(quote_model, candidate):
            date_field = candidate
            break
    value_field = None
    for candidate in ("close", "close_price", "price", "value"):
        if hasattr(quote_model, candidate):
            value_field = candidate
            break
    fk_field = None
    for candidate in ("index_id", "market_index_id", "instrument_id"):
        if hasattr(quote_model, candidate):
            fk_field = candidate
            break
    if date_field is None or value_field is None or fk_field is None:
        return [], None

    date_col = getattr(quote_model, date_field)
    value_col = getattr(quote_model, value_field)
    fk_col = getattr(quote_model, fk_field)
    stmt = select(date_col, value_col).where(fk_col == index_id)
    if start is not None:
        stmt = stmt.where(date_col >= start)
    if end is not None:
        stmt = stmt.where(date_col <= end)
    stmt = stmt.order_by(date_col.desc()).limit(limit)
    series: list[tuple[date, float]] = []
    last_day: date | None = None
    for quote_day, value in reversed(db.execute(stmt).all()):
        if value is None or float(value) <= 0:
            continue
        day = quote_day.date() if isinstance(quote_day, datetime) else quote_day
        series.append((day, float(value)))
        last_day = day
    return series, last_day


def _market_index_signals(
    db: Session,
    holdings: list[dict],
) -> tuple[list[ResearchSignalItem], str | None]:
    """基于 MarketIndex/IndexQuote 的 A/港/美指数趋势信号；模型缺失时静默降级。"""
    index_model, quote_model = _load_index_models()
    if index_model is None or quote_model is None:
        return [], None
    try:
        indexes = db.execute(select(index_model)).scalars().all()
    except Exception:  # noqa: BLE001 - 表结构不符预期时降级
        return [], None
    if not indexes:
        return [], None

    def _attr(obj: object, *names: str) -> object:
        for name in names:
            if hasattr(obj, name):
                return getattr(obj, name)
        return None

    # 组合权重在各市场的分布（按基金名称粗略映射）
    market_weight: dict[str, float] = {"cn": 0.0, "hk": 0.0, "us": 0.0}
    for holding in holdings:
        market_weight[_guess_market(holding["name"])] += holding.get("weight") or 0.0

    signals: list[ResearchSignalItem] = []
    latest_day: date | None = None
    for index in indexes:
        index_id = _attr(index, "id")
        if index_id is None:
            continue
        series, last_day = _index_quote_series(db, index_model, quote_model, int(index_id))
        if last_day is not None and (latest_day is None or last_day > latest_day):
            latest_day = last_day
        if len(series) < 21:
            continue
        values = [v for _, v in series]
        ret_20d = _period_return(values, 20)
        ret_60d = _period_return(values, 60)
        max_dd = _max_drawdown(values)
        name = str(_attr(index, "name", "index_name") or "未知指数")
        code = str(_attr(index, "code", "symbol") or "")
        market = _guess_market(name)
        exposure = market_weight.get(market, 0.0)
        evidence = {
            "index_name": name,
            "market": market,
            "return_20d": ret_20d,
            "return_60d": ret_60d,
            "max_drawdown": max_dd,
            "portfolio_market_exposure": round(exposure, 4),
            "sample_count": len(values),
        }
        if ret_20d is not None and ret_20d <= -0.05:
            level = "risk" if (ret_20d <= -0.10 and exposure >= 0.3) else "warning"
            signals.append(ResearchSignalItem(
                category="market", level=level, scope="market",
                message=(
                    f"{name} 近20日下跌 {ret_20d:.1%}，组合在{_market_label(market)}的"
                    f"权重约 {exposure:.0%}，需关注系统性回撤"
                ),
                related_codes=[code] if code else [],
                evidence=evidence,
                as_of=_signal_as_of(last_day),
                source="index_quotes",
            ))
        elif ret_20d is not None and ret_20d >= 0.05:
            signals.append(ResearchSignalItem(
                category="market", level="info", scope="market",
                message=f"{name} 近20日上涨 {ret_20d:.1%}，{_market_label(market)}环境偏多",
                related_codes=[code] if code else [],
                evidence=evidence,
                as_of=_signal_as_of(last_day),
                source="index_quotes",
            ))
    return signals, latest_day.isoformat() if latest_day else None


def _market_label(market: str) -> str:
    return {"cn": "A股", "hk": "港股", "us": "美股"}.get(market, "相关市场")


def comprehensive_research_signals(
    db: Session, filters: SignalFilters | None = None
) -> SignalListResponse:
    """综合研究信号：融合趋势/动量/回撤、持仓权重、股票穿透、行业暴露、新闻与指数趋势。

    设计要点：
    - 独立于 portfolio_metrics_summary，不影响组合指标接口的行为；
    - 每条信号携带 evidence 结构化证据、related_codes、as_of 与 source；
    - 不设"前5/总20"硬限制，由调用方通过 filters 的 limit/offset 分页；
    - MarketIndex/IndexQuote 模型不存在时自动跳过市场信号（优雅降级）。
    """
    filters = filters or SignalFilters()
    signals: list[ResearchSignalItem] = []

    # ---- 持仓与权重 ----
    rows = db.execute(
        select(
            Instrument,
            func.sum(Position.cost),
            func.sum(Position.market_value),
        )
        .join(Instrument, Position.instrument_id == Instrument.id)
        .group_by(Instrument.id)
        .order_by(Instrument.id)
    ).all()

    holdings: list[dict] = []
    for instrument, cost, market_value in rows:
        value = market_value if market_value is not None else (cost or Decimal("0"))
        holdings.append({
            "id": instrument.id,
            "code": instrument.code,
            "name": instrument.name,
            "value": float(value),
        })

    total_mv = sum(h["value"] for h in holdings)
    for holding in holdings:
        holding["weight"] = (holding["value"] / total_mv) if total_mv > 0 else 0.0
    holdings.sort(key=lambda h: h["value"], reverse=True)
    weight_by_code = {h["code"]: h["weight"] for h in holdings}

    as_of_candidates: list[date | datetime | None] = []

    # ---- 基金净值：趋势 / 动量 / 回撤 ----
    for holding in holdings:
        series = _load_nav_series(db, holding["id"])
        if len(series) < 2:
            continue
        as_of_candidates.append(series[-1][0])
        values = [v for _, v in series]
        weight = holding["weight"]
        ret_20d = _period_return(values, 20)
        ret_60d = _period_return(values, 60)
        max_dd = _max_drawdown(values)
        nav_as_of = _signal_as_of(series[-1][0])

        trend = None
        if len(series) >= 20:
            ma20 = _moving_average(values, 20)
            ma60 = _moving_average(values, 60)
            dif, dea, _ = _macd_series(values)
            trend, _reasons = _trend_signal(values, ma20, ma60, dif, dea)

        evidence = {
            "fund_name": holding["name"],
            "portfolio_weight": round(weight, 4),
            "return_20d": ret_20d,
            "return_60d": ret_60d,
            "max_drawdown": max_dd,
            "trend_signal": trend,
            "sample_count": len(values),
        }

        # 趋势信号
        if trend in ("strong_down", "down"):
            signals.append(ResearchSignalItem(
                category="trend",
                level="risk" if (trend == "strong_down" and weight >= 0.1) else "warning",
                scope="fund",
                message=(
                    f"{holding['name']}（{holding['code']}）趋势偏弱"
                    + (f"，近20日收益 {ret_20d:.1%}" if ret_20d is not None else "")
                    + f"，组合权重 {weight:.1%}，可关注是否需减仓或观察"
                ),
                related_codes=[holding["code"]],
                evidence=evidence,
                as_of=nav_as_of,
                source="fund_nav",
            ))
        elif trend in ("strong_up", "up") and weight >= 0.05:
            signals.append(ResearchSignalItem(
                category="trend", level="info", scope="fund",
                message=(
                    f"{holding['name']}（{holding['code']}）趋势向上"
                    + (f"，近20日收益 {ret_20d:.1%}" if ret_20d is not None else "")
                    + f"，组合权重 {weight:.1%}"
                ),
                related_codes=[holding["code"]],
                evidence=evidence,
                as_of=nav_as_of,
                source="fund_nav",
            ))

        # 动量信号（近 60 日动量与权重交叉）
        if ret_60d is not None and ret_60d <= -0.10 and weight >= 0.05:
            signals.append(ResearchSignalItem(
                category="momentum",
                level="risk" if weight >= 0.2 else "warning",
                scope="fund",
                message=(
                    f"{holding['name']}（{holding['code']}）近60日动量 {ret_60d:.1%}"
                    f"且权重 {weight:.1%}，拖累组合动能"
                ),
                related_codes=[holding["code"]],
                evidence=evidence,
                as_of=nav_as_of,
                source="fund_nav",
            ))
        elif ret_60d is not None and ret_60d >= 0.15 and weight >= 0.05:
            signals.append(ResearchSignalItem(
                category="momentum", level="info", scope="fund",
                message=(
                    f"{holding['name']}（{holding['code']}）近60日动量 {ret_60d:.1%}，"
                    "为组合主要正贡献来源"
                ),
                related_codes=[holding["code"]],
                evidence=evidence,
                as_of=nav_as_of,
                source="fund_nav",
            ))

        # 回撤信号
        if max_dd is not None and max_dd <= -0.2 and weight >= 0.05:
            signals.append(ResearchSignalItem(
                category="drawdown",
                level="risk" if weight >= 0.1 else "warning",
                scope="fund",
                message=(
                    f"{holding['name']}（{holding['code']}）区间最大回撤 {max_dd:.1%}"
                    f"且权重 {weight:.1%}，对组合波动贡献较大"
                ),
                related_codes=[holding["code"]],
                evidence=evidence,
                as_of=nav_as_of,
                source="fund_nav",
            ))

    # ---- 集中度信号（组合级）----
    weights = [h["weight"] for h in holdings if h["weight"] > 0]
    if weights:
        top1 = weights[0]
        hhi = sum(w * w for w in weights)
        concentration_as_of = _signal_as_of(*as_of_candidates)
        top_holding = holdings[0]
        if top1 >= 0.3:
            signals.append(ResearchSignalItem(
                category="concentration",
                level="risk" if top1 >= 0.5 else "warning",
                scope="portfolio",
                message=(
                    f"第一大持仓 {top_holding['name']}（{top_holding['code']}）占比 {top1:.1%}，"
                    + ("超过 50%，组合高度集中" if top1 >= 0.5 else "集中度偏高，建议关注单一基金风险")
                ),
                related_codes=[top_holding["code"]],
                evidence={"top1": round(top1, 4), "hhi": round(hhi, 4), "position_count": len(weights)},
                as_of=concentration_as_of,
                source="positions",
            ))
        if hhi >= 0.25 and len(weights) > 1:
            signals.append(ResearchSignalItem(
                category="concentration", level="warning", scope="portfolio",
                message=f"组合 HHI 为 {hhi:.2f}（≥0.25 视为高集中），分散度不足",
                related_codes=[h["code"] for h in holdings[:3]],
                evidence={"hhi": round(hhi, 4), "position_count": len(weights)},
                as_of=concentration_as_of,
                source="positions",
            ))

    # ---- FundHolding 股票穿透暴露与重复持有 ----
    holdings_ids = [h["id"] for h in holdings]
    if holdings_ids:
        latest_report = db.scalar(
            select(func.max(FundHolding.report_date)).where(
                FundHolding.instrument_id.in_(holdings_ids)
            )
        )
        if latest_report is not None:
            as_of_candidates.append(latest_report)
            holding_rows = db.execute(
                select(
                    FundHolding.instrument_id,
                    FundHolding.stock_code,
                    FundHolding.stock_name,
                    FundHolding.weight,
                ).where(
                    FundHolding.instrument_id.in_(holdings_ids),
                    FundHolding.report_date == latest_report,
                )
            ).all()

            # FundHolding.weight 为百分数口径（5.0 = 5%，源自“占净值比例”列），
            # 这里除以 100 换算为小数后计算组合级穿透暴露
            fund_weight_by_id = {h["id"]: h["weight"] for h in holdings}
            stock_exposure: dict[str, dict] = {}
            for instrument_id, stock_code, stock_name, stock_weight in holding_rows:
                fund_weight = fund_weight_by_id.get(instrument_id, 0.0)
                contribution = fund_weight * float(stock_weight) / 100.0
                entry = stock_exposure.setdefault(
                    stock_code,
                    {"name": stock_name, "exposure": 0.0, "funds": [], "fund_count": 0},
                )
                entry["exposure"] += contribution
                entry["funds"].append(instrument_id)
                entry["fund_count"] += 1

            instrument_code_by_id = {h["id"]: h["code"] for h in holdings}
            exposure_as_of = _signal_as_of(latest_report)

            # 单只股票组合级穿透暴露 ≥5%
            for stock_code, entry in sorted(
                stock_exposure.items(), key=lambda kv: kv[1]["exposure"], reverse=True
            ):
                if entry["exposure"] < 0.05:
                    break
                signals.append(ResearchSignalItem(
                    category="stock_exposure",
                    level="risk" if entry["exposure"] >= 0.10 else "warning",
                    scope="portfolio",
                    message=(
                        f"通过基金间接持有 {entry['name']}（{stock_code}）约 {entry['exposure']:.1%}，"
                        f"分散在 {entry['fund_count']} 只基金中，实际股票敞口偏高"
                    ),
                    related_codes=sorted(
                        {instrument_code_by_id[i] for i in entry["funds"] if i in instrument_code_by_id}
                    ) + [stock_code],
                    evidence={
                        "stock_name": entry["name"],
                        "look_through_exposure": round(entry["exposure"], 4),
                        "fund_count": entry["fund_count"],
                        "report_date": latest_report.isoformat(),
                    },
                    as_of=exposure_as_of,
                    source="fund_holdings",
                ))

            # 同一股票被多只持仓基金重复持有
            for stock_code, entry in stock_exposure.items():
                if entry["fund_count"] < 2 or entry["exposure"] < 0.02:
                    continue
                signals.append(ResearchSignalItem(
                    category="overlap", level="warning", scope="portfolio",
                    message=(
                        f"{entry['name']}（{stock_code}）同时出现在 {entry['fund_count']} 只持仓基金的重仓中，"
                        f"合计穿透暴露 {entry['exposure']:.1%}，存在重复持有"
                    ),
                    related_codes=sorted(
                        {instrument_code_by_id[i] for i in entry["funds"] if i in instrument_code_by_id}
                    ) + [stock_code],
                    evidence={
                        "stock_name": entry["name"],
                        "fund_count": entry["fund_count"],
                        "look_through_exposure": round(entry["exposure"], 4),
                        "report_date": latest_report.isoformat(),
                    },
                    as_of=exposure_as_of,
                    source="fund_holdings",
                ))

    # ---- FundIndustryAllocation 行业暴露 ----
    if holdings_ids:
        latest_industry_report = db.scalar(
            select(func.max(FundIndustryAllocation.report_date)).where(
                FundIndustryAllocation.instrument_id.in_(holdings_ids)
            )
        )
        if latest_industry_report is not None:
            as_of_candidates.append(latest_industry_report)
            industry_rows = db.execute(
                select(
                    FundIndustryAllocation.instrument_id,
                    FundIndustryAllocation.industry,
                    FundIndustryAllocation.weight,
                ).where(
                    FundIndustryAllocation.instrument_id.in_(holdings_ids),
                    FundIndustryAllocation.report_date == latest_industry_report,
                )
            ).all()
            fund_weight_by_id = {h["id"]: h["weight"] for h in holdings}
            industry_exposure: dict[str, float] = {}
            for instrument_id, industry, industry_weight in industry_rows:
                fund_weight = fund_weight_by_id.get(instrument_id, 0.0)
                industry_exposure[industry] = (
                    industry_exposure.get(industry, 0.0) + fund_weight * float(industry_weight) / 100.0
                )
            industry_as_of = _signal_as_of(latest_industry_report)
            for industry, exposure in sorted(
                industry_exposure.items(), key=lambda kv: kv[1], reverse=True
            ):
                if exposure < 0.2:
                    break
                signals.append(ResearchSignalItem(
                    category="industry",
                    level="risk" if exposure >= 0.35 else "warning",
                    scope="portfolio",
                    message=(
                        f"组合在「{industry}」行业的穿透暴露约 {exposure:.1%}，行业集中度偏高"
                    ),
                    related_codes=[h["code"] for h in holdings],
                    evidence={
                        "industry": industry,
                        "look_through_exposure": round(exposure, 4),
                        "report_date": latest_industry_report.isoformat(),
                    },
                    as_of=industry_as_of,
                    source="fund_industry_allocations",
                ))

    # ---- 已去重、已分析的近期新闻事件 ----
    if holdings:
        news_since = datetime.now() - timedelta(days=NEWS_LOOKBACK_DAYS)
        holding_ids = [holding["id"] for holding in holdings]
        recent_news = db.execute(
            select(
                FundNewsImpact.instrument_id,
                FundNewsImpact.signed_score,
                NewsEvent.latest_published_at,
            )
            .join(NewsEvent, NewsEvent.id == FundNewsImpact.event_id)
            .where(
                FundNewsImpact.instrument_id.in_(holding_ids),
                NewsEvent.latest_published_at.is_not(None),
                NewsEvent.latest_published_at >= news_since,
                NewsEvent.expires_at.is_not(None),
                NewsEvent.expires_at >= datetime.now(),
            )
        ).all()
        news_stats: dict[int, dict[str, float | int]] = {}
        latest_news_at: datetime | None = None
        for instrument_id, signed_score, published_at in recent_news:
            if published_at is not None and (latest_news_at is None or published_at > latest_news_at):
                latest_news_at = published_at
            stat = news_stats.setdefault(instrument_id, {"count": 0, "score": 0.0})
            stat["count"] = int(stat["count"]) + 1
            stat["score"] = float(stat["score"]) + float(signed_score)
        holding_by_id = {holding["id"]: holding for holding in holdings}
        news_as_of = _signal_as_of(latest_news_at)
        for instrument_id, stat in sorted(
            news_stats.items(), key=lambda item: abs(float(item[1]["score"])), reverse=True
        ):
            holding = holding_by_id.get(instrument_id)
            if holding is None:
                continue
            count, score = int(stat["count"]), float(stat["score"])
            if count < 2 and abs(score) < 5:
                continue
            direction_text = "偏利好" if score >= 5 else "偏利空" if score <= -5 else "影响中性"
            level = "risk" if score <= -25 else "warning" if score <= -8 else "info"
            signals.append(ResearchSignalItem(
                category="news",
                level=level,
                scope="fund",
                message=(
                    f"{holding['name']}（{holding['code']}）近{NEWS_LOOKBACK_DAYS}天有 "
                    f"{count} 条去重后的有效事件，综合{direction_text}"
                ),
                related_codes=[holding["code"]],
                evidence={
                    "event_count": count,
                    "signed_impact_score": round(score, 4),
                    "lookback_days": NEWS_LOOKBACK_DAYS,
                    "portfolio_weight": round(weight_by_code[holding["code"]], 4),
                },
                as_of=news_as_of,
                source="fund_news_impacts",
            ))
        if latest_news_at is not None:
            as_of_candidates.append(latest_news_at)

    # ---- 指数趋势（模型存在时）----
    market_signals, index_as_of = _market_index_signals(db, holdings)
    signals.extend(market_signals)
    if index_as_of is not None:
        as_of_candidates.append(_parse_day(index_as_of))

    # ---- 排序、过滤、分页 ----
    level_order = {"risk": 0, "warning": 1, "info": 2}
    signals.sort(key=lambda s: (level_order[s.level], s.category, s.message))

    if filters.category is not None:
        signals = [s for s in signals if s.category == filters.category]
    if filters.level is not None:
        signals = [s for s in signals if s.level == filters.level]

    total = len(signals)
    page = signals[filters.offset : filters.offset + filters.limit]

    return SignalListResponse(
        total=total,
        limit=filters.limit,
        offset=filters.offset,
        as_of=_signal_as_of(*as_of_candidates),
        signals=page,
    )
