"""A股多因子策略回测引擎（纯函数面板回测 + 编排层）。

回测口径（全部为只读研究能力，不产生任何实盘下单行为）：
- T 日（调仓信号日，每月最后一个交易日）收盘后打分，T+1 交易日按
  当日开盘价成交（开盘价缺失时回退 raw 收盘价）；
- 涨跌停不可成交：涨跌幅按「成交日 bar 的前一根 bar」（严格早于成交日）
  计算；各板块幅度不同 —— 主板（60/00）±10%、创业板（30）/科创板（68）
  ±20%、北交所（4/8/92 开头）±30%、ST/*ST（沪深）±5%；涨停不可买、
  跌停不可卖，一字涨跌停（日内振幅≈0 且触板）买卖双向均不可成交；
  当日停牌或无行情同样不可成交；被阻塞的订单顺延至下一交易日重试，
  直至成交或遇下一次调仓被新目标覆盖（覆盖时显式 warning）；
- 费用：双边佣金（默认万 2.5，最低 5 元）、卖出印花税（默认 0.05%）、
  双边滑点（默认 0.1%，买价上浮、卖价下浮）；
- 估值：逐日份额 × raw 收盘价 + 现金；停牌日沿用最近可得收盘价
  （前收盘盯市）；
- 双口径行情：研究因子/打分/前瞻收益用研究口径（前复权 qfq 优先，
  缺失回退 raw），成交价/成交额流动性/停牌/涨跌停判定用执行口径 raw；
- 基准：universe 等权买入持有（B0），或数据源提供的指数收盘序列
  （benchmark_index 指定且仓储支持 index_bars 时）；
- 无未来数据：打分只用 ≤T 的行情与 available_at ≤T 的 PIT 财务，
  成交只用 T+1 及之后的行情。

walk-forward：按月调仓天然滚动 —— 第 k 期持仓仅由第 k 个信号日
（月末 T）及之前的数据决定，测试区间（T+1 起至下一信号日）为
样本外；validation 统计（Rank IC、五档单调性等）在 signals 与
逐期前瞻收益上计算，见 run_backtest 的 validation 输出。
"""

from __future__ import annotations

import logging
import math
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from statistics import fmean

from app.services import stock_factors as factors
from app.services import stock_strategy as strategy
from app.services import (
    cash_ledger,
    corporate_actions,
    fee_rules,
    order_lifecycle,
    position_lots,
    price_limit_rules,
    trading_rules,
)
from app.services.stock_repository import (
    BenchmarkSeries,
    CorporateAction,
    Fundamentals,
    MarketBars,
    NamePeriod,
    StockBar,
    StockInfo,
    StockRepository,
    TradeCalendar,
    UniverseMembership,
    load_repository,
    one_word_limit,
    st_status_as_of,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量与费用模型
# ---------------------------------------------------------------------------

MAX_CURVE_POINTS = 260  # 曲线抽样上限（与 quant 模块一致）
MAX_TRADE_RECORDS = 500  # 成交记录上限（响应规模受控）
BOARD_LOT = 100


class BacktestError(ValueError):
    """回测参数或数据不足错误，路由层转换为 400。"""


@dataclass(frozen=True)
class CostModel:
    """A股交易费用模型（小数口径）。"""

    commission_rate: float = 0.00025  # 双边佣金（万 2.5）
    min_commission: float = 5.0  # 单笔最低佣金（元）
    stamp_tax_rate: float = 0.0005  # 印花税（仅卖出，0.05%）
    slippage_rate: float = 0.001  # 双边滑点（0.1%）
    market_impact_coefficient: float = 0.002  # sqrt(成交量参与率) 冲击系数
    volatility_slippage_coefficient: float = 0.10  # 近20日波动对滑点的系数
    max_total_slippage: float = 0.03  # 极端情况下单边滑点上限


def cost_scenarios(base: CostModel) -> dict[str, CostModel]:
    """统一生成乐观、基准、保守和极端执行成本情景。"""
    return {
        "optimistic": replace(
            base,
            commission_rate=base.commission_rate * 0.8,
            slippage_rate=base.slippage_rate * 0.5,
            market_impact_coefficient=base.market_impact_coefficient * 0.5,
        ),
        "baseline": base,
        "conservative": replace(
            base,
            commission_rate=base.commission_rate * 1.5,
            slippage_rate=base.slippage_rate * 2.0,
            market_impact_coefficient=base.market_impact_coefficient * 2.0,
        ),
        "extreme": replace(
            base,
            commission_rate=base.commission_rate * 2.0,
            slippage_rate=base.slippage_rate * 4.0,
            market_impact_coefficient=base.market_impact_coefficient * 4.0,
            max_total_slippage=max(base.max_total_slippage, 0.08),
        ),
    }


@dataclass(frozen=True)
class BacktestConfig:
    """回测参数。"""

    start: date
    end: date
    initial_capital: float = 1_000_000.0
    top_n: int = strategy.DEFAULT_TOP_N
    max_stock_weight: float = strategy.DEFAULT_MAX_STOCK_WEIGHT
    max_industry_weight: float = strategy.DEFAULT_MAX_INDUSTRY_WEIGHT
    min_avg_amount: float = strategy.DEFAULT_MIN_AVG_AMOUNT
    price_limit: float = 0.098  # 触发容差：各板块法定幅度 × 该系数为触发线
    # （0.098 ≈ 主板 10% 的 98%，即「距板 2‰ 内视为触板」的近似阈值）
    candidate_codes: tuple[str, ...] | None = None  # None = 全市场动态 universe
    universe_indices: tuple[str, ...] = ("000300", "000905")
    initial_signal: bool = False  # True 时才允许在非月末区间起点主动建仓
    min_universe_data_coverage: float = 0.95
    max_volume_participation: float = 0.10
    minimum_trade_weight: float = 0.002
    minimum_holdings: int = 20
    max_annual_volatility: float = 0.25
    max_tracking_error: float = 0.15
    benchmark_index: str | None = None  # 正式验证使用 H00906 全收益
    benchmark_required: bool = False  # True 时缺失/断档必须失败，不允许回退
    benchmark_return_kind: str | None = None  # 如 gross_total_return
    min_limit_data_coverage: float = 0.0
    factor_weights: dict[str, float] | None = None  # 因子族权重覆盖（None = 缺省）
    adaptive_ic_weights: bool = False  # 正式模式按已成熟标签滚动收缩
    rebalance_day_offset: int = 0  # 稳健性测试可将月末信号前后平移
    signal_execution_delay_days: int = 1  # 信号后至少等待的交易日数
    order_policy: order_lifecycle.OrderLifecyclePolicy = field(
        default_factory=order_lifecycle.OrderLifecyclePolicy
    )
    cash_annual_rate: float = cash_ledger.ANNUAL_CASH_RATE
    cash_policy_version: str = cash_ledger.CASH_POLICY_VERSION
    cost: CostModel = field(default_factory=CostModel)
    strategy_name: str = "stock_rules_multifactor"
    strategy_version_id: int | None = None
    data_snapshot_sha256: str = ""
    file_manifest_snapshot_sha256: str = ""
    strict_file_manifest: bool = False


# ---------------------------------------------------------------------------
# 结果容器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fill:
    """一笔成交记录（研究展示用）。"""

    signal_date: date
    fill_date: date
    code: str
    action: str  # buy / sell
    price: float  # 含滑点的成交价
    shares: float
    amount: float  # 成交金额（不含费用）
    fee: float  # 佣金 + 印花税
    reason: str
    fee_rule_version: str | None = None
    fee_breakdown: dict[str, float] = field(default_factory=dict)
    arrival_price: float | None = None
    open_price: float | None = None
    decision_price: float | None = None
    market_vwap: float | None = None
    close_price: float | None = None
    participation_rate: float | None = None
    implementation_shortfall: float | None = None
    recent_volatility: float | None = None
    liquidity_adv: float | None = None
    execution_session: str = "open"
    slippage_model_version: str = "OPEN_ADV_SQRT_V1"
    cost_scenario: str = "baseline"


@dataclass
class RebalanceDetail:
    """一次调仓的明细。"""

    signal_date: date
    target: dict[str, float]
    fills: list[Fill] = field(default_factory=list)
    blocked_codes: list[str] = field(default_factory=list)
    turnover: float = 0.0
    cash_weight: float = 1.0
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, object] = field(default_factory=dict)
    order_events: list[dict[str, object]] = field(default_factory=list)


@dataclass
class BacktestOutcome:
    """回测完整结果（服务层组装响应用）。"""

    calendar: list[date]  # 回测区间交易日（曲线日期，与 equity 对齐）
    equity: list[float]  # 组合总市值（含现金）
    daily_returns: list[float]
    benchmark: list[float]  # 基准净值（起点 1.0），与 calendar 对齐
    benchmark_kind: str  # equal_weight / index:<code>
    rebalances: list[RebalanceDetail]
    final_value: float
    total_fees: float
    avg_turnover: float
    forward_returns: list[
        tuple[date, dict[str, float]]
    ]  # 各信号日 → 前瞻收益（validation 用）
    scores_by_date: list[
        tuple[date, dict[str, float]]
    ]  # 各信号日 → 复合分（validation 用）
    warnings: list[str] = field(default_factory=list)
    attribution: dict[str, float] = field(default_factory=dict)
    groups_by_date: list[tuple[date, dict[str, tuple[str, str]]]] = field(
        default_factory=list
    )
    minimum_historical_coverage: float = 1.0
    benchmark_metadata: dict[str, object] = field(default_factory=dict)
    benchmarks: dict[str, list[float]] = field(default_factory=dict)
    benchmark_metadata_by_kind: dict[str, dict[str, object]] = field(
        default_factory=dict
    )
    execution_limit_data_coverage: float = 1.0
    total_cash_interest: float = 0.0
    cash_ledgers: list[dict[str, object]] = field(default_factory=list)
    total_dividend_tax: float = 0.0
    factor_values_by_date: list[
        tuple[date, dict[str, dict[str, float | None]]]
    ] = field(default_factory=list)
    factor_weight_history: list[dict[str, object]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 行情面板（纯数据准备）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketPanel:
    """回测用行情面板：日历 + 双口径 bar 序列 + 指数序列。

    - bars_by_code / bar_lookup：执行口径 raw（成交、流动性、停牌、
      涨跌停判定、盯市估值）；
    - research_bars_by_code：研究口径（qfq 优先，缺失回退 raw），
      仅用于研究因子/打分/前瞻收益，绝不用于成交价；
    - name_histories：历史名称/ST 区间（历史 ST as_of 判定用）。
    """

    calendar: TradeCalendar
    bars_by_code: dict[str, list[StockBar]]  # 执行口径 raw，升序
    bar_lookup: dict[str, dict[date, StockBar]]  # code → date → bar（执行口径）
    index_series: list[tuple[date, float]]  # 指数收盘（可能为空）
    benchmark_metadata: dict[str, object] = field(default_factory=dict)
    research_bars_by_code: dict[str, list[StockBar]] = field(default_factory=dict)
    name_histories: dict[str, list[NamePeriod]] = field(default_factory=dict)
    universe_by_date: dict[date, frozenset[str]] = field(default_factory=dict)
    universe_snapshot_dates: dict[date, dict[str, date]] = field(default_factory=dict)
    industry_by_date: dict[date, dict[str, str]] = field(default_factory=dict)
    corporate_actions_by_date: dict[date, tuple[CorporateAction, ...]] = field(
        default_factory=dict
    )
    data_warnings: tuple[str, ...] = ()

    def research_series(self, code: str) -> list[StockBar]:
        """研究口径序列：qfq 存在时返回 qfq，否则回退执行口径 raw。"""
        return self.research_bars_by_code.get(code) or self.bars_by_code.get(code, [])


def build_panel(
    repository: StockRepository,
    codes: list[str],
    start: date | None,
    end: date | None,
    benchmark_index: str | None = None,
    benchmark_required: bool = False,
    benchmark_return_kind: str | None = None,
) -> MarketPanel:
    """装载行情面板；所有数据降级都写入结果 warning。

    仓储支持 market_bars（可选扩展协议）时构造研究/执行双口径面板；
    否则退化为执行口径 raw 单口径（research = exec，与旧行为一致）。
    历史名称（name_histories）同为可选扩展，缺失时 ST 按当前名称判定。
    """
    research_map: dict[str, list[StockBar]] = {}
    histories: dict[str, list[NamePeriod]] = {}
    exec_map: dict[str, list[StockBar]] = {}
    actions_by_date: dict[date, list[CorporateAction]] = {}
    data_warnings: list[str] = []

    market_bars_fn = getattr(repository, "market_bars", None)
    dual: dict[str, MarketBars] | None = None
    if callable(market_bars_fn):
        try:
            dual = market_bars_fn(codes, start, end)
        except Exception:  # noqa: BLE001 - 可选扩展失败降级为单口径
            logger.warning("market_bars 装载失败，降级为 raw 单口径面板", exc_info=True)
            data_warnings.append("前复权研究行情装载失败，因子行情显式回退 raw 单口径")
            dual = None
    if dual is not None:
        for code in codes:
            entry = dual.get(code)
            if entry is None:
                exec_map[code] = []
                research_map[code] = []
                continue
            exec_map[code] = sorted(entry.exec_bars, key=lambda bar: bar.trade_date)
            research_map[code] = sorted(
                entry.research_bars, key=lambda bar: bar.trade_date
            )
    else:
        raw_bars = repository.daily_bars(codes or None, start, end)
        for bar in raw_bars:
            exec_map.setdefault(bar.code, []).append(bar)
        for series in exec_map.values():
            series.sort(key=lambda bar: bar.trade_date)
        for code in codes:
            exec_map.setdefault(code, [])
            research_map[code] = exec_map[code]

    name_fn = getattr(repository, "name_histories", None)
    if callable(name_fn):
        try:
            histories = dict(name_fn(codes))
        except Exception:  # noqa: BLE001 - 可选扩展失败降级（ST 按当前名称）
            logger.warning(
                "name_histories 装载失败，历史 ST 判定降级为当前名称", exc_info=True
            )
            data_warnings.append("历史名称/ST 数据装载失败，ST 判定显式回退当前名称")
            histories = {}

    action_fn = getattr(repository, "corporate_actions", None)
    if callable(action_fn):
        try:
            for action in action_fn(codes, start, end):
                actions_by_date.setdefault(action.action_date, []).append(action)
        except Exception:  # noqa: BLE001 - 公司行为不可静默伪造
            logger.warning("公司行为装载失败，原始价持仓可能不完整", exc_info=True)
            data_warnings.append("公司行为装载失败，原始价持仓结果不完整，仅供排障")

    lookup = {
        code: {bar.trade_date: bar for bar in series}
        for code, series in exec_map.items()
    }
    # 日历保留 end 之后的已知交易日，用于判断 end 是否真正处于月末；
    # 行情和净值仍在 run_backtest_panel 中严格按 config.end 截断。
    calendar = repository.trade_calendar(start, None)
    index_series: list[tuple[date, float]] = []
    benchmark_metadata: dict[str, object] = {}
    if benchmark_index:
        governed_fn = getattr(repository, "benchmark_series", None)
        governed: BenchmarkSeries | None = None
        if callable(governed_fn):
            governed = governed_fn(benchmark_index, start, end)
        if governed is not None:
            if (
                benchmark_return_kind is not None
                and governed.return_kind != benchmark_return_kind
            ):
                raise BacktestError(
                    f"基准收益口径为 {governed.return_kind}，"
                    f"正式要求 {benchmark_return_kind}"
                )
            index_series = list(governed.points)
            benchmark_metadata = {
                "code": governed.code,
                "name": governed.name,
                "return_kind": governed.return_kind,
                "source": governed.source,
                "source_files": list(governed.source_files),
                "source_hashes": list(governed.source_hashes),
                "source_rows": len(governed.points),
                "source_first_date": (
                    governed.points[0][0].isoformat() if governed.points else None
                ),
                "source_last_date": (
                    governed.points[-1][0].isoformat() if governed.points else None
                ),
            }
        elif not benchmark_required:
            try:
                index_series = repository.index_bars(benchmark_index, start, end)
            except Exception:  # noqa: BLE001 - 交互研究允许显式降级
                data_warnings.append(
                    f"指数 {benchmark_index} 装载失败，基准将显式回退等权"
                )
                index_series = []
        if benchmark_required and not index_series:
            raise BacktestError(f"正式基准 {benchmark_index} 不可用，禁止回退等权基准")
    return MarketPanel(
        calendar=calendar,
        bars_by_code=exec_map,
        bar_lookup=lookup,
        index_series=index_series,
        benchmark_metadata=benchmark_metadata,
        research_bars_by_code=research_map,
        name_histories=histories,
        corporate_actions_by_date={
            day: tuple(actions) for day, actions in actions_by_date.items()
        },
        data_warnings=tuple(data_warnings),
    )


def signal_dates(
    calendar_days: list[date] | tuple[date, ...],
    start: date,
    end: date,
    initial_signal: bool = False,
    day_offset: int = 0,
) -> list[date]:
    """返回区间内真实月末信号日，可选显式起点建仓。

    calendar_days 应尽量包含 end 之后的交易日，这样区间中途截止不会被
    误判为月末。若仓储只提供区间日历，则保留其最后可见日的旧兼容语义。
    """
    all_days = sorted(set(calendar_days))
    in_range = [day for day in all_days if start <= day <= end]
    if not in_range:
        return []
    month_end_set = set(strategy.month_ends(all_days))
    result = [day for day in in_range if day in month_end_set]
    if day_offset:
        index_by_day = {day: index for index, day in enumerate(in_range)}
        shifted: list[date] = []
        for day in result:
            target = index_by_day[day] + day_offset
            if 0 <= target < len(in_range):
                shifted.append(in_range[target])
        result = sorted(set(shifted))
    if initial_signal and in_range[0] not in result:
        result.insert(0, in_range[0])
    return result


# ---------------------------------------------------------------------------
# 成交可行性：涨跌停 / 停牌
# ---------------------------------------------------------------------------


def daily_move(bar: StockBar, prev_close: float | None) -> float | None:
    """当日涨跌幅（小数）：优先数据源 raw_return，否则按前收盘计算。

    prev_close 必须为严格早于 bar.trade_date 的收盘价（由
    prev_bar_before 提供）；prev_close 与 bar 同日会稀释涨跌幅、
    使涨跌停判定失效，属于数据错误而非合法输入。
    """
    if bar.raw_return is not None:
        return bar.raw_return
    if prev_close is not None and prev_close > 0:
        return bar.close / prev_close - 1.0
    return None


def prev_bar_before(bars: list[StockBar], day: date) -> StockBar | None:
    """严格早于 day 的最后一根 bar（bars 升序）；用于涨跌幅的前收盘口径。

    严格早于成交日：停牌期间 close 被填充为前收盘的数据源，若用
    「≤ 成交日」的前收盘会把当日 close 当作前收盘（涨跌幅恒 0，
    涨跌停判定失效）。一律取严格早于成交日的 bar。
    """
    result: StockBar | None = None
    for bar in bars:
        if bar.trade_date >= day:
            break
        result = bar
    return result


def can_trade(
    bar: StockBar | None,
    prev_close: float | None,
    side: str,
    price_limit: float,
    code: str | None = None,
    st: bool = False,
    listing_session: int | None = None,
    delisting_period: bool = False,
) -> tuple[bool, str]:
    """T+1 成交日可否成交：停牌/无行情不可成交；涨停不可买、跌停不可卖；
    一字涨跌停（振幅≈0 且触板）买卖双向均不可成交。

    side ∈ {"buy", "sell"}；price_limit 为触发容差系数（默认 0.98 档），
    实际触发线由成交日对应的版本化规则给出；prev_close 必须严格早于成交日。
    返回 (可否成交, 原因)。
    """
    if bar is None:
        return False, "成交日无行情"
    if bar.suspended:
        return False, "成交日停牌"
    if bar.open is None or bar.open <= 0:
        return False, "开盘价缺失，开盘执行订单顺延"
    price = bar.open
    if bar.up_limit is not None or bar.down_limit is not None:
        at_up = (
            bar.up_limit is not None
            and bar.up_limit > 0
            and price >= bar.up_limit * (1.0 - 1e-4)
        )
        at_down = (
            bar.down_limit is not None
            and bar.down_limit > 0
            and price <= bar.down_limit * (1.0 + 1e-4)
        )
        one_word = (
            bar.high is not None
            and bar.low is not None
            and abs(bar.high - bar.low) <= 1e-9
            and (at_up or at_down)
        )
        if one_word:
            return False, "真实涨跌停价确认一字板，买卖双向不可成交"
        if side == "buy" and at_up:
            return False, f"开盘价触及真实涨停价 {bar.up_limit:.3f}，不可买入"
        if side == "sell" and at_down:
            return False, f"开盘价触及真实跌停价 {bar.down_limit:.3f}，不可卖出"
        # 有权威涨跌停价且开盘未触板，开盘订单可成交；不再用收盘涨跌幅
        # 反推并错误阻塞早已在开盘成交的订单。
        return True, ""
    rule = price_limit_rules.price_limit_rule(
        code or bar.code,
        bar.trade_date,
        st=st,
        listing_session=listing_session,
        delisting_period=delisting_period,
    )
    if rule.upper_limit is None or rule.lower_limit is None:
        return True, ""
    upper_trigger = (
        rule.upper_limit * price_limit if price_limit < 1.0 else rule.upper_limit
    )
    lower_trigger = (
        rule.lower_limit * price_limit if price_limit < 1.0 else rule.lower_limit
    )
    move = daily_move(bar, prev_close)
    if move is not None:
        applicable_limit = rule.upper_limit if move >= 0 else rule.lower_limit
        if one_word_limit(bar, move, applicable_limit):
            return False, f"一字涨跌停（{move:+.1%}，振幅≈0），买卖双向不可成交"
        if side == "buy" and move >= upper_trigger:
            return False, f"涨停（{move:.1%} ≥ {upper_trigger:.1%}）不可买入"
        if side == "sell" and move <= -lower_trigger:
            return False, f"跌停（{move:.1%} ≤ -{lower_trigger:.1%}）不可卖出"
    return True, ""


def trade_price(
    bar: StockBar,
    side: str,
    slippage_rate: float,
    *,
    shares: float = 0.0,
    available_volume: float | None = None,
    volatility: float = 0.0,
    market_impact_coefficient: float = 0.0,
    volatility_slippage_coefficient: float = 0.0,
    max_total_slippage: float = 0.03,
) -> float:
    """开盘价加动态滑点。

    冲击使用 ``coefficient * sqrt(成交量参与率)``，另叠加近期日波动。
    默认附加系数为零，保留旧调用的固定滑点兼容性。
    """
    if bar.open is None or bar.open <= 0:
        raise BacktestError("开盘执行模型缺少有效开盘价，禁止回退当日收盘价")
    base = bar.open
    participation = (
        max(shares, 0.0) / available_volume
        if available_volume is not None and available_volume > 0
        else 0.0
    )
    impact = market_impact_coefficient * math.sqrt(participation)
    volatility_drag = volatility_slippage_coefficient * max(volatility, 0.0)
    total_slippage = min(
        max(slippage_rate + impact + volatility_drag, 0.0),
        max_total_slippage,
    )
    return (
        base * (1.0 + total_slippage)
        if side == "buy"
        else base * (1.0 - total_slippage)
    )


def recent_volatility(bars: list[StockBar], day: date, window: int = 20) -> float:
    """严格使用 day 之前收盘计算的日收益标准差。"""
    closes = [bar.close for bar in bars if bar.trade_date < day and bar.close > 0]
    closes = closes[-(window + 1) :]
    if len(closes) < 3:
        return 0.0
    returns = [
        closes[index] / closes[index - 1] - 1.0
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]
    if len(returns) < 2:
        return 0.0
    mean = fmean(returns)
    return math.sqrt(sum((value - mean) ** 2 for value in returns) / (len(returns) - 1))


def prior_adv_volume(
    bars: list[StockBar],
    day: date,
    *,
    window: int = 20,
    minimum_observations: int = 5,
) -> float:
    """开盘前可知的历史日均成交量；绝不读取成交日当日全日成交量。"""
    values = [
        float(bar.volume)
        for bar in bars
        if bar.trade_date < day
        and not bar.suspended
        and bar.volume is not None
        and bar.volume > 0
    ][-window:]
    if len(values) < minimum_observations:
        return 0.0
    return fmean(values)


def execution_tca_fields(
    *,
    side: str,
    fill_price: float,
    bar: StockBar,
    decision_price: float | None,
    shares: float,
    available_volume: float,
    recent_volatility_value: float | None = None,
) -> dict[str, object]:
    """生成 arrival/decision/VWAP、参与率和方向调整后的实现损失。"""
    market_vwap = (
        float(bar.amount) / float(bar.volume)
        if bar.amount is not None
        and bar.volume is not None
        and bar.amount > 0
        and bar.volume > 0
        else None
    )
    shortfall = None
    if decision_price is not None and decision_price > 0:
        shortfall = (
            fill_price / decision_price - 1.0
            if side == "buy"
            else 1.0 - fill_price / decision_price
        )
    return {
        "arrival_price": bar.open,
        "open_price": bar.open,
        "decision_price": decision_price,
        "market_vwap": market_vwap,
        "close_price": bar.close,
        "participation_rate": (
            shares / available_volume if available_volume > 0 else None
        ),
        "implementation_shortfall": shortfall,
        "recent_volatility": recent_volatility_value,
        "liquidity_adv": available_volume,
        "execution_session": "open",
    }


def trade_fee_breakdown(
    side: str,
    amount: float,
    cost: CostModel,
    *,
    code: str | None = None,
    trade_date: date | None = None,
    shares: float = 0.0,
) -> fee_rules.FeeBreakdown:
    """正式路径按成交日政策计算；缺少代码/日期仅保留旧交互调用兼容。"""
    if code is not None and trade_date is not None:
        return fee_rules.calculate_fee(
            code=code,
            trade_date=trade_date,
            side=side,
            amount=amount,
            shares=shares,
            commission_rate=cost.commission_rate,
            minimum_commission=cost.min_commission,
        )
    commission = max(amount * cost.commission_rate, cost.min_commission)
    stamp = amount * cost.stamp_tax_rate if side == "sell" else 0.0
    return fee_rules.FeeBreakdown(
        rule_version="LEGACY_UNDATED_COST_MODEL",
        commission=commission,
        stamp_tax=stamp,
        transfer_fee=0.0,
        total=commission + stamp,
    )


def trade_fee(
    side: str,
    amount: float,
    cost: CostModel,
    *,
    code: str | None = None,
    trade_date: date | None = None,
    shares: float = 0.0,
) -> float:
    return trade_fee_breakdown(
        side,
        amount,
        cost,
        code=code,
        trade_date=trade_date,
        shares=shares,
    ).total


# ---------------------------------------------------------------------------
# 主引擎（纯函数，不访问数据库/仓储）
# ---------------------------------------------------------------------------


def _last_price_before(
    bars: list[StockBar], day: date, fallback: float | None
) -> float | None:
    """day 当天或之前最近一个非停牌正收盘价（停牌日前收盘盯市用）。"""
    result = fallback
    for bar in bars:
        if bar.trade_date > day:
            break
        if not bar.suspended and bar.close > 0:
            result = bar.close
    return result


def run_backtest_panel(
    panel: MarketPanel,
    infos: list[StockInfo],
    fundamentals_by_code: dict[str, list[Fundamentals]],
    config: BacktestConfig,
) -> BacktestOutcome:
    """在行情面板上执行月调仓多因子回测（纯函数）。

    流程：每个调仓信号日（月内最后一个交易日 T）——
    1. 动态 universe：ST/停牌/次新/流动性/样本过滤；
    2. 行业内 winsorize+z-score 复合分（PIT 财务、≤T 行情）；
    3. 行业中性 + 单股/行业上限的目标组合；
    4. T+1 起逐日执行调仓（涨跌停/停牌顺延重试），费用按 CostModel；
    5. 调仓之外的交易日逐日前收盘盯市。
    """
    calendar_days = [
        day for day in panel.calendar.days if config.start <= day <= config.end
    ]
    if len(calendar_days) < 2:
        raise BacktestError(
            f"回测区间交易日不足（{len(calendar_days)} 天），请扩大 start/end 区间"
        )
    calendar_index = {trade_day: index for index, trade_day in enumerate(calendar_days)}

    info_by_code = {info.code: info for info in infos}
    if config.candidate_codes is not None:
        wanted = set(config.candidate_codes)
        infos = [info for info in infos if info.code in wanted]
    if not infos:
        raise BacktestError("候选股票池为空，请检查 candidate_codes 或股票数据")

    signal_days = set(
        signal_dates(
            panel.calendar.days,
            config.start,
            config.end,
            config.initial_signal,
            config.rebalance_day_offset,
        )
    )

    cash = config.initial_capital
    settled_cash = config.initial_capital
    sale_cash_settlements: list[tuple[date, float, str]] = []
    shares: dict[str, float] = {}
    lot_ledger = position_lots.LotLedger()
    dividend_receivables: dict[str, float] = {}
    dividend_tax_claims: list[corporate_actions.DividendTaxClaim] = []
    restricted_assets: dict[str, tuple[float, str]] = {}
    restricted_until: dict[str, date] = {}
    last_price: dict[str, float] = {}  # 逐股最近可得收盘（停牌盯市）
    pending_orders: dict[str, float] = {}  # 待执行目标权重（涨跌停/停牌顺延）
    pending_order_state: dict[str, dict[str, object]] = {}
    pending_signal_date: date | None = None

    equity_curve: list[float] = []
    curve_days: list[date] = []
    rebalances: list[RebalanceDetail] = []
    total_fees = 0.0
    total_dividend_tax = 0.0
    total_slippage_cost = 0.0
    total_cash_interest = 0.0
    cash_ledgers: list[dict[str, object]] = []
    forward_returns: list[tuple[date, dict[str, float]]] = []
    scores_by_date: list[tuple[date, dict[str, float]]] = []
    factor_values_by_date: list[
        tuple[date, dict[str, dict[str, float | None]]]
    ] = []
    factor_weight_history: list[dict[str, object]] = []
    ic_training_observations: list[object] = []
    last_family_signal: tuple[
        date, dict[str, dict[str, float | None]]
    ] | None = None
    groups_by_date: list[tuple[date, dict[str, tuple[str, str]]]] = []
    warnings: list[str] = list(panel.data_warnings)
    last_signal_info: tuple[date, dict[str, float], dict[str, float]] | None = None
    limit_checks = 0
    authoritative_limit_checks = 0

    for i, day in enumerate(calendar_days):
        for code, release_day in list(restricted_until.items()):
            if release_day <= day:
                restricted_until.pop(code, None)
                restricted_assets.pop(code, None)
        day_cash_ledger = cash_ledger.CashLedger(
            available=cash,
            settled=settled_cash,
            frozen=0.0,
            receivable=sum(dividend_receivables.values()),
        )
        calendar_day_count = (day - calendar_days[i - 1]).days if i > 0 else 0
        total_cash_interest += day_cash_ledger.accrue_interest(
            day,
            calendar_days=calendar_day_count,
            annual_rate=config.cash_annual_rate,
            reference=config.cash_policy_version,
        )
        remaining_settlements: list[tuple[date, float, str]] = []
        for settle_date, settlement_amount, reference in sale_cash_settlements:
            if settle_date <= day:
                day_cash_ledger.settle_sale_proceeds(
                    day,
                    settlement_amount,
                    reference,
                )
            else:
                remaining_settlements.append(
                    (settle_date, settlement_amount, reference)
                )
        sale_cash_settlements = remaining_settlements
        cash = day_cash_ledger.available
        settled_cash = day_cash_ledger.settled
        # ---- 0) 原始价持仓公司行为：除权权益与终止上市先于当日交易 ----
        for action in panel.corporate_actions_by_date.get(day, ()):
            event_key = action.event_key or (
                f"{action.code}:{action.action_date.isoformat()}:{action.kind}"
            )
            if action.kind == "cash_payment":
                paid = dividend_receivables.pop(event_key, 0.0)
                day_cash_ledger.settle_receivable(
                    day,
                    paid,
                    f"dividend:{event_key}",
                )
                cash = day_cash_ledger.available
                settled_cash = day_cash_ledger.settled
                if paid > 0:
                    warnings.append(
                        f"{day.isoformat()} {action.code} 现金股利应收"
                        f" {paid:.2f} 到账（{action.source}）"
                    )
                continue
            held = shares.get(action.code, 0.0)
            if held <= 0:
                continue
            if action.kind == "terminal":
                resolution = corporate_actions.resolve_terminal(
                    terminal_type=action.terminal_type,
                    terminal_price=action.terminal_price,
                    consideration_status=action.consideration_status,
                    restricted_valuation_per_share=(
                        action.restricted_valuation_per_share
                    ),
                )
                if resolution.action != "cash_settlement":
                    restricted_assets[action.code] = (
                        resolution.restricted_value_per_share,
                        resolution.reason,
                    )
                    pending_orders.pop(action.code, None)
                    pending_order_state.pop(action.code, None)
                    warnings.append(
                        f"{day.isoformat()} {action.code} 退市持仓转为受限资产："
                        f"{resolution.reason}；保守估值"
                        f" {resolution.restricted_value_per_share:.4f}/股，"
                        "等待人工核对和后续更正"
                    )
                    continue
                terminal_price = resolution.cash_per_share
                day_cash_ledger.receive_cash(
                    day,
                    held * terminal_price,
                    f"terminal:{event_key}",
                    event_type="terminal_cash_received",
                )
                cash = day_cash_ledger.available
                settled_cash = day_cash_ledger.settled
                shares.pop(action.code, None)
                lot_ledger.remove(action.code)
                pending_orders.pop(action.code, None)
                pending_order_state.pop(action.code, None)
                warnings.append(
                    f"{day.isoformat()} {action.code} 终止持仓按"
                    f" {terminal_price:.3f} 清算（{action.source}）"
                )
                continue
            if action.kind == "cash_entitlement":
                amount = held * action.cash_per_share
                dividend_tax_claims.extend(
                    corporate_actions.create_dividend_tax_claims(
                        code=action.code,
                        event_key=event_key,
                        entitlement_date=day,
                        gross_cash_per_share=action.cash_per_share,
                        lots=list(lot_ledger.lots(action.code)),
                    )
                )
                if action.payment_date is None or action.payment_date <= day:
                    day_cash_ledger.receive_cash(
                        day,
                        amount,
                        f"dividend:{event_key}",
                        event_type="dividend_received",
                    )
                    cash = day_cash_ledger.available
                    settled_cash = day_cash_ledger.settled
                else:
                    dividend_receivables[event_key] = (
                        dividend_receivables.get(event_key, 0.0) + amount
                    )
                    day_cash_ledger.recognize_receivable(
                        day,
                        amount,
                        f"dividend:{event_key}",
                    )
                warnings.append(
                    f"{day.isoformat()} {action.code} 确认现金股利"
                    f" {amount:.2f}"
                    + (
                        f"，{action.payment_date.isoformat()} 入账"
                        if action.payment_date is not None and action.payment_date > day
                        else "，当日入账"
                    )
                    + f"（{action.source}）"
                )
            elif action.kind == "distribution":
                distribution_cash = held * action.cash_per_share
                day_cash_ledger.receive_cash(
                    day,
                    distribution_cash,
                    f"distribution:{event_key}",
                    event_type="distribution_cash_received",
                )
                cash = day_cash_ledger.available
                settled_cash = day_cash_ledger.settled
                lot_ledger.distribute_shares(
                    action.code,
                    action.share_ratio,
                    action_date=day,
                    source=action.source,
                )
                conversion = corporate_actions.convert_registered_shares(
                    raw_shares=lot_ledger.total(action.code),
                    cash_compensation_per_fraction=(
                        action.cash_compensation_per_fraction
                    ),
                )
                lot_ledger.scale_total(
                    action.code,
                    conversion.registered_shares,
                )
                if conversion.cash_compensation > 0:
                    day_cash_ledger.receive_cash(
                        day,
                        conversion.cash_compensation,
                        f"fractional_compensation:{event_key}",
                        event_type="fractional_cash_compensation",
                    )
                    cash = day_cash_ledger.available
                    settled_cash = day_cash_ledger.settled
                shares[action.code] = lot_ledger.total(action.code)
                warnings.append(
                    f"{day.isoformat()} {action.code} 公司行为：每股现金"
                    f" {action.cash_per_share:.4f}、新增股份"
                    f" {action.share_ratio:.6f}；登记"
                    f" {conversion.registered_shares:.3f} 股、零碎"
                    f" {conversion.fractional_shares:.6f}、补偿"
                    f" {conversion.cash_compensation:.2f}"
                    f"（{conversion.rule_version}；{action.source}）"
                )
            elif action.kind == "rights_issue":
                price = action.subscription_price
                if price is None or price <= 0:
                    warnings.append(
                        f"{day.isoformat()} {action.code} 配股事件字段不完整，"
                        "权利失效并标记人工核对"
                    )
                    continue
                rights_portfolio_value = cash + sum(
                    quantity
                    * (
                        _last_price_before(
                            panel.bars_by_code.get(code, []),
                            day,
                            last_price.get(code),
                        )
                        or 0.0
                    )
                    for code, quantity in shares.items()
                )
                decision = corporate_actions.decide_rights_issue(
                    held_shares=held,
                    subscription_ratio=action.subscription_ratio,
                    subscription_price=price,
                    available_cash=cash,
                    portfolio_value=rights_portfolio_value,
                    rights_tradable=action.rights_tradable,
                    right_market_price=action.right_market_price,
                )
                conversion = corporate_actions.convert_registered_shares(
                    raw_shares=decision.subscribed_shares,
                    cash_compensation_per_fraction=None,
                )
                subscribed = conversion.registered_shares
                subscription_cash = subscribed * price
                if subscription_cash > 0:
                    day_cash_ledger.debit_cash(
                        day,
                        subscription_cash,
                        f"rights_issue:{event_key}",
                        event_type="rights_subscription",
                    )
                if decision.rights_sale_cash > 0:
                    day_cash_ledger.receive_cash(
                        day,
                        decision.rights_sale_cash,
                        f"rights_sale:{event_key}",
                        event_type="rights_sale",
                    )
                cash = day_cash_ledger.available
                settled_cash = day_cash_ledger.settled
                if subscribed > 0:
                    lot_ledger.buy(
                        action.code,
                        subscribed,
                        subscribed * price,
                        acquired_date=day,
                        sellable_date=(
                            panel.calendar.next_trade_day(day)
                            or position_lots.next_calendar_settlement_day(day)
                        ),
                        source=f"rights_issue:{action.source}",
                    )
                shares[action.code] = lot_ledger.total(action.code)
                warnings.append(
                    f"{day.isoformat()} {action.code} 配股决策"
                    f" {decision.policy_version}：申请"
                    f" {decision.requested_shares:.3f}、认购 {subscribed:.3f}、"
                    f"出售权利 {decision.sold_rights:.3f}、失效"
                    f" {decision.lapsed_rights + conversion.fractional_shares:.3f}；"
                    f"{decision.reason}"
                )
                if subscribed + 1e-9 < decision.requested_shares:
                    warnings.append(
                        f"{day.isoformat()} {action.code} 配股因现金约束仅认购"
                        f" {subscribed:.3f}/{decision.requested_shares:.3f} 股"
                    )
            elif action.kind in {"merger", "code_change"}:
                successor = action.successor_code
                if not successor or action.share_ratio <= 0:
                    warnings.append(
                        f"{day.isoformat()} {action.code} 换股合并字段不完整，"
                        "持仓保留并标记人工核对"
                    )
                    continue
                raw_converted = held * action.share_ratio
                successor_listing_date = action.successor_listing_date
                if (
                    successor_listing_date is None
                    and panel.bar_lookup.get(successor, {}).get(day) is not None
                ):
                    successor_listing_date = day
                conversion = corporate_actions.convert_registered_shares(
                    raw_shares=raw_converted,
                    cash_compensation_per_fraction=(
                        action.cash_compensation_per_fraction
                    ),
                )
                converted = conversion.registered_shares
                shares[successor] = shares.get(successor, 0.0) + converted
                shares.pop(action.code, None)
                old_lots = lot_ledger.remove(action.code)
                successor_lots = list(lot_ledger.lots(successor))
                raw_total = sum(lot.shares * action.share_ratio for lot in old_lots)
                successor_lots.extend(
                    position_lots.PositionLot(
                        lot_id=lot.lot_id,
                        acquired_date=lot.acquired_date,
                        sellable_date=max(
                            lot.sellable_date,
                            successor_listing_date or date.max,
                        ),
                        shares=(
                            converted * lot.shares * action.share_ratio / raw_total
                            if raw_total > 0
                            else 0.0
                        ),
                        total_cost=lot.total_cost,
                        source=f"{lot.source}|merger:{action.source}",
                    )
                    for lot in old_lots
                )
                lot_ledger.replace_lots(successor, successor_lots)
                for claim in dividend_tax_claims:
                    if claim.code == action.code:
                        claim.code = successor
                if conversion.cash_compensation > 0:
                    day_cash_ledger.receive_cash(
                        day,
                        conversion.cash_compensation,
                        f"fractional_compensation:{event_key}",
                        event_type="fractional_cash_compensation",
                    )
                    cash = day_cash_ledger.available
                    settled_cash = day_cash_ledger.settled
                if successor_listing_date is None:
                    restricted_assets[successor] = (
                        0.0,
                        "换股后新证券上市日期未知，全部股份暂时受限",
                    )
                elif successor_listing_date > day:
                    restricted_assets[successor] = (
                        0.0,
                        f"换股新证券上市前受限至 {successor_listing_date.isoformat()}",
                    )
                    restricted_until[successor] = successor_listing_date
                pending_orders.pop(action.code, None)
                pending_order_state.pop(action.code, None)
                warnings.append(
                    f"{day.isoformat()} {action.code} 按"
                    f" 1:{action.share_ratio:.6f} 换为 {successor}"
                    f" {converted:.3f} 股；零碎"
                    f" {conversion.fractional_shares:.6f} 股，现金补偿"
                    f" {conversion.cash_compensation:.2f}，未补偿受限零碎"
                    f" {conversion.restricted_fractional_value:.6f}"
                    f"（{conversion.rule_version}；{action.source}）"
                )
            else:
                raw_total = held * (1.0 + action.share_ratio)
                lot_ledger.distribute_shares(
                    action.code,
                    action.share_ratio,
                    action_date=day,
                    source=action.source,
                )
                conversion = corporate_actions.convert_registered_shares(
                    raw_shares=raw_total,
                    cash_compensation_per_fraction=(
                        action.cash_compensation_per_fraction
                    ),
                )
                lot_ledger.scale_total(
                    action.code,
                    conversion.registered_shares,
                )
                if conversion.cash_compensation > 0:
                    day_cash_ledger.receive_cash(
                        day,
                        conversion.cash_compensation,
                        f"fractional_compensation:{event_key}",
                        event_type="fractional_cash_compensation",
                    )
                    cash = day_cash_ledger.available
                    settled_cash = day_cash_ledger.settled
                shares[action.code] = lot_ledger.total(action.code)
                warnings.append(
                    f"{day.isoformat()} {action.code} 送转/拆并新增股份"
                    f" {held * action.share_ratio:.6f}；实际登记"
                    f" {conversion.registered_shares:.3f}、零碎"
                    f" {conversion.fractional_shares:.6f}、补偿"
                    f" {conversion.cash_compensation:.2f}"
                    f"（{conversion.rule_version}；{action.source}）"
                )

        # ---- 1) 信号日收盘后打分（T），目标权重自 T+1 起执行 ----
        if day in signal_days:
            # 上一期未执行完的订单被新信号覆盖：显式 warning（含首期顺延被覆盖）
            if pending_orders and pending_signal_date is not None:
                override_msg = (
                    f"{pending_signal_date.isoformat()} 期有 {len(pending_orders)} 只"
                    f"股票的未成交订单被新信号覆盖放弃："
                    f"{'、'.join(sorted(pending_orders))}"
                )
                warnings.append(override_msg)
                # 记到被覆盖的那一期（按 signal_date 定位，而非默认最后一期）
                for past in reversed(rebalances):
                    if past.signal_date == pending_signal_date:
                        past.warnings.append(override_msg)
                        for code in sorted(pending_orders):
                            state = pending_order_state.get(code, {})
                            current_price = _last_price_before(
                                panel.bars_by_code.get(code, []), day, None
                            )
                            cost = order_lifecycle.opportunity_cost(
                                side=str(state.get("side", "buy")),
                                unfilled_shares=float(
                                    state.get("remaining_shares", 0.0)
                                ),
                                decision_price=(
                                    float(state["decision_price"])
                                    if state.get("decision_price") is not None
                                    else None
                                ),
                                current_price=current_price,
                            )
                            past.order_events.append(
                                {
                                    "date": day.isoformat(),
                                    "code": code,
                                    "status": "superseded",
                                    "reason": "被新一期目标权重覆盖",
                                    "order_lifecycle_version": (
                                        config.order_policy.version
                                    ),
                                    **cost,
                                }
                            )
                        break
                pending_orders = {}
                pending_order_state = {}
                pending_signal_date = None

            signal_infos = [
                info for info in infos if info.code not in restricted_assets
            ]
            if panel.universe_by_date:
                members = panel.universe_by_date.get(day)
                if members is None:
                    raise BacktestError(
                        f"{day.isoformat()} 缺少历史指数成分快照，拒绝静默使用当前成分"
                    )
                signal_infos = [info for info in infos if info.code in members]
            if panel.industry_by_date:
                historical_industries = panel.industry_by_date.get(day)
                if historical_industries is None:
                    raise BacktestError(
                        f"{day.isoformat()} 缺少历史申万行业快照，拒绝使用当前行业"
                    )
                signal_infos = [
                    replace(
                        info,
                        industry=historical_industries.get(info.code, "未知"),
                    )
                    for info in signal_infos
                ]
            universe, filters = strategy.build_universe(
                signal_infos,
                panel.bars_by_code,
                day,
                config.min_avg_amount,
                name_histories=panel.name_histories,
                research_bars_by_code=panel.research_bars_by_code or None,
            )
            contexts = [
                factors.build_context(
                    info,
                    panel.research_series(info.code),
                    fundamentals_by_code.get(info.code, []),
                    day,
                )
                for info in universe
            ]
            contexts = [
                ctx
                for ctx in contexts
                if factors.history_depth(ctx) >= factors.MIN_HISTORY_DAYS
            ]
            from app.services.factor_residual_model import price_series_returns
            from app.services.ic_weighting import (
                IcTrainingObservation,
                estimate_ic_weights,
            )

            if last_family_signal is not None:
                previous_day, previous_family_scores = last_family_signal
                matured_returns: dict[str, float] = {}
                previous_codes = set().union(
                    *(set(values) for values in previous_family_scores.values())
                )
                for code in previous_codes:
                    base_price = _last_price_before(
                        panel.research_series(code), previous_day, None
                    )
                    current_price = _last_price_before(
                        panel.research_series(code), day, None
                    )
                    if base_price and current_price and base_price > 0:
                        matured_returns[code] = current_price / base_price - 1.0
                ic_training_observations.append(
                    IcTrainingObservation(
                        signal_date=previous_day,
                        label_available_at=day,
                        family_scores=previous_family_scores,
                        forward_returns=matured_returns,
                    )
                )
            effective_factor_weights = config.factor_weights
            if (
                effective_factor_weights is None
                and (config.adaptive_ic_weights or config.benchmark_required)
            ):
                weight_estimate = estimate_ic_weights(
                    ic_training_observations,  # type: ignore[arg-type]
                    as_of=day,
                )
                effective_factor_weights = weight_estimate.weights
                factor_weight_history.append(
                    {
                        "as_of": day.isoformat(),
                        "weights": dict(weight_estimate.weights),
                        "raw_ic": dict(weight_estimate.raw_ic),
                        "shrunk_ic": dict(weight_estimate.shrunk_ic),
                        "observation_counts": dict(
                            weight_estimate.observation_counts
                        ),
                        "training_start": (
                            weight_estimate.training_start.isoformat()
                            if weight_estimate.training_start
                            else None
                        ),
                        "training_end": (
                            weight_estimate.training_end.isoformat()
                            if weight_estimate.training_end
                            else None
                        ),
                        "status": weight_estimate.status,
                        "half_life_periods": weight_estimate.half_life_periods,
                        "prior_strength": weight_estimate.prior_strength,
                        "maximum_weight": weight_estimate.maximum_weight,
                    }
                )

            scored = factors.compute_cross_section(
                contexts,
                day,
                weights=effective_factor_weights,
                official_market_returns=price_series_returns(
                    [
                        (market_day, value)
                        for market_day, value in panel.index_series
                        if market_day <= day
                    ]
                ),
            )
            signal_value = cash + sum(dividend_receivables.values())
            current_weights: dict[str, float] = {}
            position_values: dict[str, float] = {}
            for code, held in shares.items():
                price = (
                    restricted_assets[code][0]
                    if code in restricted_assets
                    else _last_price_before(
                        panel.bars_by_code.get(code, []),
                        day,
                        last_price.get(code),
                    )
                )
                if price is not None:
                    position_values[code] = held * price
                    signal_value += held * price
            if signal_value > 0:
                current_weights = {
                    code: value / signal_value
                    for code, value in position_values.items()
                }
            plan = strategy.build_portfolio(
                scored,
                universe,
                day,
                top_n=config.top_n,
                max_stock_weight=config.max_stock_weight,
                max_industry_weight=config.max_industry_weight,
                current_weights=current_weights,
                portfolio_value=signal_value,
                max_adv_participation=config.max_volume_participation,
                minimum_holdings=config.minimum_holdings,
                max_annual_volatility=config.max_annual_volatility,
                max_tracking_error=config.max_tracking_error,
                use_convex_optimizer=config.benchmark_required,
            )
            from app.services.portfolio_risk_controls import (
                discretize_portfolio,
            )

            decision_prices = {
                code: price
                for code in plan.target_weights
                if (
                    price := _last_price_before(
                        panel.bars_by_code.get(code, []), day, None
                    )
                )
                is not None
                and price > 0
            }
            executable_target = dict(plan.target_weights)
            if (
                signal_value > 0
                and decision_prices
                and len(decision_prices) == len(plan.target_weights)
            ):
                discrete = discretize_portfolio(
                    target_weights=plan.target_weights,
                    prices=decision_prices,
                    portfolio_value=signal_value,
                    lot_sizes={
                        code: trading_rules.quantity_rule(
                            code, day
                        ).buy_increment
                        for code in plan.target_weights
                    },
                    max_stock_weight=config.max_stock_weight,
                )
                plan.diagnostics["discrete_feasibility"] = discrete
                if discrete["passed"] is True:
                    executable_target = dict(discrete["actual_weights"])
                else:
                    plan.warnings.append(
                        "整手离散后违反硬约束，本期拒绝生成下单目标"
                    )
                    executable_target = {}
            detail = RebalanceDetail(
                signal_date=day,
                target=dict(plan.target_weights),
                cash_weight=round(1.0 - plan.invested_weight, 6),
                warnings=list(plan.warnings),
                diagnostics=dict(plan.diagnostics),
            )
            rebalances.append(detail)
            pending_orders = {
                code: executable_target.get(code, 0.0)
                for code in set(shares) | set(executable_target)
                if code not in restricted_assets
            }
            pending_order_state = {
                code: {
                    "attempts": 0,
                    "side": (
                        "buy"
                        if executable_target.get(code, 0.0)
                        >= current_weights.get(code, 0.0)
                        else "sell"
                    ),
                    "decision_price": _last_price_before(
                        panel.bars_by_code.get(code, []), day, None
                    ),
                    "remaining_shares": 0.0,
                }
                for code in set(shares) | set(plan.target_weights)
                if code not in restricted_assets
            }
            pending_signal_date = day
            scores_by_date.append((day, {item.code: item.composite for item in scored}))

            factor_values_by_date.append(
                (
                    day,
                    {
                        factor_name: {
                            item.code: (
                                getattr(item, factor_name)
                                if factor_name
                                in {
                                    "quality",
                                    "value",
                                    "momentum",
                                    "trend",
                                    "lowvol",
                                }
                                else item.zscores.get(factor_name)
                            )
                            for item in scored
                        }
                        for factor_name in (
                            *tuple(factors._FACTOR_DIRECTION),
                            "quality",
                            "value",
                            "momentum",
                            "trend",
                            "lowvol",
                            "composite",
                        )
                    },
                )
            )
            last_family_signal = (
                day,
                {
                    family: {
                        item.code: getattr(item, family)
                        for item in scored
                    }
                    for family in (
                        "quality",
                        "value",
                        "momentum",
                        "trend",
                                    "lowvol",
                                    "composite",
                    )
                },
            )
            caps = sorted(
                item.market_cap
                for item in scored
                if item.market_cap is not None and item.market_cap > 0
            )
            lower_cap = caps[len(caps) // 3] if caps else None
            upper_cap = caps[(2 * len(caps)) // 3] if caps else None
            groups_by_date.append(
                (
                    day,
                    {
                        item.code: (
                            item.industry,
                            "unknown"
                            if item.market_cap is None
                            else "small"
                            if lower_cap is not None and item.market_cap <= lower_cap
                            else "large"
                            if upper_cap is not None and item.market_cap >= upper_cap
                            else "mid",
                        )
                        for item in scored
                    },
                )
            )

            # 上一信号期的前瞻收益（validation 用）：信号日收盘 → 本信号日收盘
            if last_signal_info is not None:
                prev_day, prev_target, prev_scores = last_signal_info
                forwards: dict[str, float] = {}
                for code in prev_scores:
                    base = _last_price_before(
                        panel.research_series(code), prev_day, None
                    )
                    now = _last_price_before(panel.research_series(code), day, None)
                    if base and now and base > 0:
                        forwards[code] = now / base - 1.0
                if forwards:
                    forward_returns.append((prev_day, forwards))
            last_signal_info = (
                day,
                dict(plan.target_weights),
                dict(scores_by_date[-1][1]),
            )
            # 本期筛选剔除原因汇总（可解释）
            failed = [f for f in filters if not f.passed]
            if failed:
                detail.warnings.append(
                    f"universe 过滤剔除 {len(failed)} 只："
                    + "；".join(
                        f"{f.code}({'+'.join(f.reasons[:1])})" for f in failed[:5]
                    )
                    + ("…" if len(failed) > 5 else "")
                )

        # ---- 2) 信号日次日（T+1）起执行待成交订单 ----
        elif (
            pending_orders
            and pending_signal_date is not None
            and calendar_index[day] - calendar_index[pending_signal_date]
            >= config.signal_execution_delay_days
        ):
            detail = rebalances[-1]
            total_value = (
                cash
                + sum(dividend_receivables.values())
                + sum(
                    amount
                    * (
                        _last_price_before(panel.bars_by_code.get(code, []), day, None)
                        or 0.0
                    )
                    for code, amount in shares.items()
                )
            )
            drifted = (
                {
                    code: amount
                    * (
                        _last_price_before(panel.bars_by_code.get(code, []), day, None)
                        or 0.0
                    )
                    / total_value
                    for code, amount in shares.items()
                }
                if total_value > 0
                else {}
            )
            codes = set(pending_orders)
            turnover_legs = 0.0
            all_filled = True
            for code in sorted(codes):
                current_w = drifted.get(code, 0.0)
                state = pending_order_state.setdefault(
                    code,
                    {
                        "attempts": 0,
                        "side": (
                            "buy"
                            if pending_orders.get(code, 0.0) >= current_w
                            else "sell"
                        ),
                        "decision_price": _last_price_before(
                            panel.bars_by_code.get(code, []),
                            pending_signal_date,
                            None,
                        ),
                        "remaining_shares": 0.0,
                    },
                )
                attempts = int(state.get("attempts", 0))
                elapsed = calendar_index[day] - calendar_index[pending_signal_date]
                if order_lifecycle.is_expired(
                    attempts=attempts,
                    trading_days_elapsed=elapsed,
                    policy=config.order_policy,
                ):
                    current_price = _last_price_before(
                        panel.bars_by_code.get(code, []), day, None
                    )
                    cost = order_lifecycle.opportunity_cost(
                        side=str(state.get("side", "buy")),
                        unfilled_shares=float(state.get("remaining_shares", 0.0)),
                        decision_price=(
                            float(state["decision_price"])
                            if state.get("decision_price") is not None
                            else None
                        ),
                        current_price=current_price,
                    )
                    detail.order_events.append(
                        {
                            "date": day.isoformat(),
                            "code": code,
                            "status": "expired",
                            "reason": (
                                f"超过订单TTL {config.order_policy.ttl_trading_days}"
                                f" 个交易日或最大重试 {config.order_policy.max_attempts} 次"
                            ),
                            "attempts": attempts,
                            "trading_days_elapsed": elapsed,
                            "order_lifecycle_version": config.order_policy.version,
                            **cost,
                        }
                    )
                    pending_orders.pop(code, None)
                    pending_order_state.pop(code, None)
                    continue
                target_w, signal_strength = order_lifecycle.decayed_target_weight(
                    current_weight=current_w,
                    original_target_weight=pending_orders.get(code, 0.0),
                    attempts_before_execution=attempts,
                    policy=config.order_policy,
                )
                diff = target_w - current_w
                if abs(diff) < config.minimum_trade_weight:
                    pending_orders.pop(code, None)
                    pending_order_state.pop(code, None)
                    continue  # 权重基本一致，无需成交
                side = "buy" if diff > 0 else "sell"
                state["side"] = side
                bar = panel.bar_lookup.get(code, {}).get(day)
                reference_price = (
                    bar.open
                    if bar is not None and bar.open is not None and bar.open > 0
                    else (
                        float(state["decision_price"])
                        if state.get("decision_price") is not None
                        else None
                    )
                )
                if reference_price is not None and reference_price > 0:
                    state["remaining_shares"] = (
                        abs(diff) * total_value / reference_price
                    )
                state["attempts"] = attempts + 1
                cancel_for_deviation, deviation = (
                    order_lifecycle.should_cancel_for_price_deviation(
                        (
                            float(state["decision_price"])
                            if state.get("decision_price") is not None
                            else None
                        ),
                        bar.open if bar is not None else None,
                        config.order_policy,
                    )
                )
                if cancel_for_deviation:
                    cost = order_lifecycle.opportunity_cost(
                        side=side,
                        unfilled_shares=float(state.get("remaining_shares", 0.0)),
                        decision_price=(
                            float(state["decision_price"])
                            if state.get("decision_price") is not None
                            else None
                        ),
                        current_price=bar.open if bar is not None else None,
                    )
                    detail.order_events.append(
                        {
                            "date": day.isoformat(),
                            "code": code,
                            "side": side,
                            "status": "price_deviation_cancelled",
                            "reason": (
                                f"开盘价相对决策价偏离 {deviation:.2%}，"
                                f"超过 {config.order_policy.max_price_deviation:.2%}"
                            ),
                            "attempts": attempts + 1,
                            "signal_strength": signal_strength,
                            "order_lifecycle_version": config.order_policy.version,
                            **cost,
                        }
                    )
                    pending_orders.pop(code, None)
                    pending_order_state.pop(code, None)
                    continue
                prev_bar = prev_bar_before(panel.bars_by_code.get(code, []), day)
                prev_close = prev_bar.close if prev_bar is not None else None
                info = info_by_code.get(code)
                st = st_status_as_of(
                    info.name if info else code,
                    panel.name_histories.get(code),
                    day,
                )
                listing_session = None
                if info is not None and info.list_date is not None:
                    listing_session = sum(
                        info.list_date <= known_day <= day
                        for known_day in panel.calendar.days
                    )
                delisting_period = bool(info is not None and "退" in info.name)
                if bar is not None:
                    rule = price_limit_rules.price_limit_rule(
                        code,
                        day,
                        st=st,
                        listing_session=listing_session,
                        delisting_period=delisting_period,
                    )
                    if rule.upper_limit is not None and rule.lower_limit is not None:
                        limit_checks += 1
                        if bar.up_limit is not None and bar.down_limit is not None:
                            authoritative_limit_checks += 1
                ok, reason = can_trade(
                    bar,
                    prev_close,
                    side,
                    config.price_limit,
                    code=code,
                    st=st,
                    listing_session=listing_session,
                    delisting_period=delisting_period,
                )
                if not ok:
                    all_filled = False
                    detail.order_events.append(
                        {
                            "date": day.isoformat(),
                            "code": code,
                            "side": side,
                            "status": "blocked",
                            "reason": reason,
                            "attempts": attempts + 1,
                            "signal_strength": signal_strength,
                            "order_lifecycle_version": config.order_policy.version,
                        }
                    )
                    if code not in detail.blocked_codes:
                        detail.blocked_codes.append(code)
                        detail.warnings.append(
                            f"{day.isoformat()} {code} {reason}，顺延"
                        )
                    continue
                assert bar is not None  # can_trade 通过则 bar 必有行情
                price = trade_price(bar, side, config.cost.slippage_rate)
                opening_adv = prior_adv_volume(panel.bars_by_code.get(code, []), day)
                quantity_rule = trading_rules.quantity_rule(code, day)
                raw_capacity = opening_adv * config.max_volume_participation
                if side == "buy":
                    held = shares.get(code, 0.0)
                    capacity_shares = quantity_rule.normalize_buy(raw_capacity)
                    desired_shares = quantity_rule.normalize_buy(
                        max(target_w * total_value / price, 0.0)
                    )
                    buy_shares = quantity_rule.normalize_buy(
                        max(desired_shares - held, 0.0)
                    )
                    requested_shares = buy_shares
                    buy_shares = min(buy_shares, capacity_shares)
                    if buy_shares + 1e-9 < requested_shares:
                        all_filled = False
                        detail.warnings.append(
                            f"{day.isoformat()} {code} 买入受成交量参与率"
                            f" {config.max_volume_participation:.0%} 限制，部分成交"
                        )
                    while buy_shares >= quantity_rule.buy_minimum:
                        amount = buy_shares * price
                        fee = trade_fee(
                            "buy",
                            amount,
                            config.cost,
                            code=code,
                            trade_date=day,
                            shares=buy_shares,
                        )
                        if amount + fee <= cash + 1e-9:
                            break
                        buy_shares -= quantity_rule.buy_increment
                    if buy_shares < quantity_rule.buy_minimum:
                        all_filled = False
                        state["remaining_shares"] = requested_shares
                        detail.order_events.append(
                            {
                                "date": day.isoformat(),
                                "code": code,
                                "side": "buy",
                                "status": "blocked",
                                "requested_shares": requested_shares,
                                "filled_shares": 0.0,
                                "remaining_shares": requested_shares,
                                "reason": "成交容量或现金不足一手",
                                "quantity_rule_version": quantity_rule.version,
                                "attempts": attempts + 1,
                                "signal_strength": signal_strength,
                                "order_lifecycle_version": (
                                    config.order_policy.version
                                ),
                            }
                        )
                        continue
                    price = trade_price(
                        bar,
                        side,
                        config.cost.slippage_rate,
                        shares=buy_shares,
                        available_volume=opening_adv,
                        volatility=recent_volatility(
                            panel.bars_by_code.get(code, []), day
                        ),
                        market_impact_coefficient=(
                            config.cost.market_impact_coefficient
                        ),
                        volatility_slippage_coefficient=(
                            config.cost.volatility_slippage_coefficient
                        ),
                        max_total_slippage=config.cost.max_total_slippage,
                    )
                    # 动态冲击抬高买价后再次执行现金约束。
                    while buy_shares >= quantity_rule.buy_minimum:
                        amount = buy_shares * price
                        fee = trade_fee(
                            "buy",
                            amount,
                            config.cost,
                            code=code,
                            trade_date=day,
                            shares=buy_shares,
                        )
                        if amount + fee <= cash + 1e-9:
                            break
                        buy_shares -= quantity_rule.buy_increment
                    if buy_shares < quantity_rule.buy_minimum:
                        all_filled = False
                        state["remaining_shares"] = requested_shares
                        detail.order_events.append(
                            {
                                "date": day.isoformat(),
                                "code": code,
                                "side": "buy",
                                "status": "blocked",
                                "requested_shares": requested_shares,
                                "filled_shares": 0.0,
                                "remaining_shares": requested_shares,
                                "reason": "动态冲击后现金不足一手",
                                "quantity_rule_version": quantity_rule.version,
                                "attempts": attempts + 1,
                                "signal_strength": signal_strength,
                                "order_lifecycle_version": (
                                    config.order_policy.version
                                ),
                            }
                        )
                        continue
                    amount = buy_shares * price
                    base_price = (
                        bar.open if bar.open is not None and bar.open > 0 else bar.close
                    )
                    total_slippage_cost += max(price - base_price, 0.0) * buy_shares
                    fee_detail = trade_fee_breakdown(
                        "buy",
                        amount,
                        config.cost,
                        code=code,
                        trade_date=day,
                        shares=buy_shares,
                    )
                    fee = fee_detail.total
                    decision_bar = panel.bar_lookup.get(code, {}).get(
                        pending_signal_date
                    )
                    tca = execution_tca_fields(
                        side="buy",
                        fill_price=price,
                        bar=bar,
                        decision_price=(
                            decision_bar.close if decision_bar is not None else None
                        ),
                        shares=buy_shares,
                        available_volume=opening_adv,
                        recent_volatility_value=recent_volatility(
                            panel.bars_by_code.get(code, []), day
                        ),
                    )
                    cash_required = amount + fee
                    cash_reference = f"backtest_buy:{pending_signal_date}:{day}:{code}"
                    day_cash_ledger.reserve(
                        day,
                        cash_required,
                        cash_reference,
                    )
                    day_cash_ledger.consume_reservation(
                        day,
                        cash_required,
                        cash_reference,
                        fee=fee,
                    )
                    cash = day_cash_ledger.available
                    settled_cash = day_cash_ledger.settled
                    lot_ledger.buy(
                        code,
                        buy_shares,
                        amount + fee,
                        acquired_date=day,
                        sellable_date=(
                            panel.calendar.next_trade_day(day)
                            or position_lots.next_calendar_settlement_day(day)
                        ),
                        source=f"backtest_fill:{pending_signal_date}",
                    )
                    shares[code] = lot_ledger.total(code)
                    total_fees += fee
                    detail.fills.append(
                        Fill(
                            pending_signal_date,
                            day,
                            code,
                            "buy",
                            price,
                            buy_shares,
                            amount,
                            fee,
                            "调仓买入",
                            fee_detail.rule_version,
                            {
                                "commission": fee_detail.commission,
                                "stamp_tax": fee_detail.stamp_tax,
                                "transfer_fee": fee_detail.transfer_fee,
                            },
                            **tca,
                        )
                    )
                    remaining_shares = max(requested_shares - buy_shares, 0.0)
                    state["remaining_shares"] = remaining_shares
                    opportunity = order_lifecycle.opportunity_cost(
                        side="buy",
                        unfilled_shares=remaining_shares,
                        decision_price=(
                            float(state["decision_price"])
                            if state.get("decision_price") is not None
                            else None
                        ),
                        current_price=bar.open,
                    )
                    detail.order_events.append(
                        {
                            "date": day.isoformat(),
                            "code": code,
                            "side": "buy",
                            "status": (
                                "filled"
                                if buy_shares + 1e-9 >= requested_shares
                                else "partially_filled"
                            ),
                            "requested_shares": requested_shares,
                            "filled_shares": buy_shares,
                            "remaining_shares": remaining_shares,
                            "quantity_rule_version": quantity_rule.version,
                            "attempts": attempts + 1,
                            "signal_strength": signal_strength,
                            "order_lifecycle_version": (config.order_policy.version),
                            "reason": (
                                None
                                if buy_shares + 1e-9 >= requested_shares
                                else "成交容量或现金约束"
                            ),
                            **opportunity,
                        }
                    )
                    if remaining_shares <= 1e-9:
                        pending_orders.pop(code, None)
                        pending_order_state.pop(code, None)
                else:
                    held = shares.get(code, 0.0)
                    capacity_shares = quantity_rule.normalize_sell(raw_capacity, held)
                    if target_w <= 0:
                        sell_shares = held
                    else:
                        desired_shares = quantity_rule.normalize_buy(
                            max(target_w * total_value / price, 0.0)
                        )
                        sell_shares = quantity_rule.normalize_sell(
                            max(held - desired_shares, 0.0), held
                        )
                    requested_shares = sell_shares
                    settled_shares = lot_ledger.available(code, day)
                    sell_shares = min(sell_shares, settled_shares)
                    if sell_shares + 1e-9 < requested_shares:
                        all_filled = False
                        detail.warnings.append(
                            f"{day.isoformat()} {code} 受 T+1 可卖批次限制，"
                            f"仅可卖 {settled_shares:.3f} 股"
                        )
                    sell_shares = quantity_rule.normalize_sell(
                        min(sell_shares, capacity_shares), held
                    )
                    if sell_shares + 1e-9 < requested_shares:
                        all_filled = False
                        detail.warnings.append(
                            f"{day.isoformat()} {code} 卖出受成交量参与率"
                            f" {config.max_volume_participation:.0%} 限制，部分成交"
                        )
                    if sell_shares <= 0:
                        all_filled = False
                        state["remaining_shares"] = requested_shares
                        detail.order_events.append(
                            {
                                "date": day.isoformat(),
                                "code": code,
                                "side": "sell",
                                "status": "blocked",
                                "requested_shares": requested_shares,
                                "filled_shares": 0.0,
                                "remaining_shares": requested_shares,
                                "reason": "成交容量为零",
                                "quantity_rule_version": quantity_rule.version,
                                "attempts": attempts + 1,
                                "signal_strength": signal_strength,
                                "order_lifecycle_version": (
                                    config.order_policy.version
                                ),
                            }
                        )
                        continue
                    price = trade_price(
                        bar,
                        side,
                        config.cost.slippage_rate,
                        shares=sell_shares,
                        available_volume=opening_adv,
                        volatility=recent_volatility(
                            panel.bars_by_code.get(code, []), day
                        ),
                        market_impact_coefficient=(
                            config.cost.market_impact_coefficient
                        ),
                        volatility_slippage_coefficient=(
                            config.cost.volatility_slippage_coefficient
                        ),
                        max_total_slippage=config.cost.max_total_slippage,
                    )
                    amount = sell_shares * price
                    base_price = (
                        bar.open if bar.open is not None and bar.open > 0 else bar.close
                    )
                    total_slippage_cost += max(base_price - price, 0.0) * sell_shares
                    fee_detail = trade_fee_breakdown(
                        "sell",
                        amount,
                        config.cost,
                        code=code,
                        trade_date=day,
                        shares=sell_shares,
                    )
                    fee = fee_detail.total
                    decision_bar = panel.bar_lookup.get(code, {}).get(
                        pending_signal_date
                    )
                    tca = execution_tca_fields(
                        side="sell",
                        fill_price=price,
                        bar=bar,
                        decision_price=(
                            decision_bar.close if decision_bar is not None else None
                        ),
                        shares=sell_shares,
                        available_volume=opening_adv,
                        recent_volatility_value=recent_volatility(
                            panel.bars_by_code.get(code, []), day
                        ),
                    )
                    consumed_lots = lot_ledger.sell(code, sell_shares, trade_date=day)
                    dividend_tax, dividend_tax_details = (
                        corporate_actions.realize_dividend_tax(
                            claims=[
                                claim
                                for claim in dividend_tax_claims
                                if claim.code == code and claim.remaining_shares > 1e-9
                            ],
                            consumed_lots=consumed_lots,
                            sale_date=day,
                        )
                    )
                    shares[code] = lot_ledger.total(code)
                    if shares[code] <= 1e-10:
                        shares.pop(code, None)
                    net_proceeds = amount - fee
                    cash_reference = f"backtest_sell:{pending_signal_date}:{day}:{code}"
                    day_cash_ledger.receive_cash(
                        day,
                        net_proceeds,
                        cash_reference,
                        settled=False,
                        event_type="stock_sale_proceeds",
                        fee=fee,
                    )
                    if dividend_tax > 0:
                        day_cash_ledger.debit_cash(
                            day,
                            dividend_tax,
                            f"dividend_tax:{cash_reference}",
                            event_type="dividend_tax_clawback",
                            fee=dividend_tax,
                        )
                        total_dividend_tax += dividend_tax
                    settle_date = panel.calendar.next_trade_day(
                        day
                    ) or position_lots.next_calendar_settlement_day(day)
                    sale_cash_settlements.append(
                        (
                            settle_date,
                            net_proceeds - dividend_tax,
                            cash_reference,
                        )
                    )
                    cash = day_cash_ledger.available
                    settled_cash = day_cash_ledger.settled
                    total_fees += fee
                    detail.fills.append(
                        Fill(
                            pending_signal_date,
                            day,
                            code,
                            "sell",
                            price,
                            sell_shares,
                            amount,
                            fee,
                            "调仓卖出",
                            fee_detail.rule_version,
                            {
                                "commission": fee_detail.commission,
                                "stamp_tax": fee_detail.stamp_tax,
                                "transfer_fee": fee_detail.transfer_fee,
                                "dividend_tax": dividend_tax,
                                "dividend_tax_details": dividend_tax_details,
                            },
                            **tca,
                        )
                    )
                    remaining_shares = max(requested_shares - sell_shares, 0.0)
                    state["remaining_shares"] = remaining_shares
                    opportunity = order_lifecycle.opportunity_cost(
                        side="sell",
                        unfilled_shares=remaining_shares,
                        decision_price=(
                            float(state["decision_price"])
                            if state.get("decision_price") is not None
                            else None
                        ),
                        current_price=bar.open,
                    )
                    detail.order_events.append(
                        {
                            "date": day.isoformat(),
                            "code": code,
                            "side": "sell",
                            "status": (
                                "filled"
                                if sell_shares + 1e-9 >= requested_shares
                                else "partially_filled"
                            ),
                            "requested_shares": requested_shares,
                            "filled_shares": sell_shares,
                            "remaining_shares": remaining_shares,
                            "quantity_rule_version": quantity_rule.version,
                            "attempts": attempts + 1,
                            "signal_strength": signal_strength,
                            "order_lifecycle_version": (config.order_policy.version),
                            "consumed_lots": consumed_lots,
                            "reason": (
                                None
                                if sell_shares + 1e-9 >= requested_shares
                                else "成交容量限制"
                            ),
                            **opportunity,
                        }
                    )
                    if remaining_shares <= 1e-9:
                        pending_orders.pop(code, None)
                        pending_order_state.pop(code, None)
                turnover_legs += amount / total_value if total_value > 0 else 0.0
            detail.turnover = round(detail.turnover + 0.5 * turnover_legs, 6)
            if all_filled or not pending_orders:
                pending_orders = {}
                pending_order_state = {}
                pending_signal_date = None

        # ---- 3) 现金守恒与逐日盯市（停牌沿用前收盘）----
        if abs(day_cash_ledger.receivable - sum(dividend_receivables.values())) > 1e-6:
            raise BacktestError(f"{day.isoformat()} 现金应收账本与明细不一致")
        day_cash_ledger.assert_conserved()
        cash_audit = day_cash_ledger.conservation()
        cash_audit["date"] = day.isoformat()
        cash_ledgers.append(cash_audit)
        cash = day_cash_ledger.available
        settled_cash = day_cash_ledger.settled
        day_value = cash + sum(dividend_receivables.values())
        for code, amount in shares.items():
            price = (
                restricted_assets[code][0]
                if code in restricted_assets
                else _last_price_before(
                    panel.bars_by_code.get(code, []),
                    day,
                    last_price.get(code),
                )
            )
            if price is not None:
                last_price[code] = price
                day_value += amount * price
        equity_curve.append(day_value)
        curve_days.append(day)

    # 区间结束时仍有未成交订单：显式 warning（不静默丢弃）
    if pending_orders and pending_signal_date is not None:
        residual_msg = (
            f"回测区间结束时，{pending_signal_date.isoformat()} 期仍有 "
            f"{len(pending_orders)} 只股票的订单未成交（停牌/涨跌停顺延至区间外）："
            f"{'、'.join(sorted(pending_orders))}"
        )
        warnings.append(residual_msg)
        if rebalances:
            rebalances[-1].warnings.append(residual_msg)
            for code in sorted(pending_orders):
                state = pending_order_state.get(code, {})
                current_price = _last_price_before(
                    panel.bars_by_code.get(code, []), config.end, None
                )
                cost = order_lifecycle.opportunity_cost(
                    side=str(state.get("side", "buy")),
                    unfilled_shares=float(state.get("remaining_shares", 0.0)),
                    decision_price=(
                        float(state["decision_price"])
                        if state.get("decision_price") is not None
                        else None
                    ),
                    current_price=current_price,
                )
                rebalances[-1].order_events.append(
                    {
                        "date": config.end.isoformat(),
                        "code": code,
                        "status": "expired",
                        "reason": "回测区间结束，订单生命周期终止",
                        "attempts": int(state.get("attempts", 0)),
                        "order_lifecycle_version": config.order_policy.version,
                        **cost,
                    }
                )

    if len(equity_curve) < 2:
        raise BacktestError("回测区间内无任何交易日，无法构造净值曲线")
    limit_coverage = authoritative_limit_checks / limit_checks if limit_checks else 1.0
    if limit_coverage + 1e-12 < config.min_limit_data_coverage:
        raise BacktestError(
            f"权威涨跌停数据覆盖率 {limit_coverage:.2%} 低于正式门槛 "
            f"{config.min_limit_data_coverage:.2%}，禁止大量使用规则回退"
        )

    daily_returns = [
        equity_curve[i] / equity_curve[i - 1] - 1.0
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] > 0
    ]

    # ---- 基准：指数优先，缺失回退 universe 等权买入持有（B0）----
    benchmark_entry: date | None = None
    first_signal: date | None = rebalances[0].signal_date if rebalances else None
    if first_signal is not None:
        benchmark_entry = next((day for day in curve_days if day > first_signal), None)
    benchmark, benchmark_kind, bench_warnings = _build_benchmark(
        panel, infos, curve_days, config, benchmark_entry, first_signal
    )
    warnings.extend(bench_warnings)
    equal_weight, _equal_kind, equal_warnings = _build_benchmark(
        replace(panel, index_series=[], benchmark_metadata={}),
        infos,
        curve_days,
        replace(
            config,
            benchmark_index=None,
            benchmark_required=False,
            benchmark_return_kind=None,
        ),
        benchmark_entry,
        first_signal,
    )
    warnings.extend(equal_warnings)
    cash_curve = [1.0]
    for index in range(1, len(curve_days)):
        elapsed_days = (curve_days[index] - curve_days[index - 1]).days
        cash_curve.append(
            cash_curve[-1] * (1.0 + config.cash_annual_rate * elapsed_days / 365.0)
        )
    benchmarks = {
        "UNIVERSE_EQUAL_WEIGHT_TOTAL_RETURN": equal_weight,
        "CASH_CNY": cash_curve,
    }
    if benchmark_kind == "CSI800_TOTAL_RETURN":
        benchmarks["CSI800_TOTAL_RETURN"] = benchmark
    benchmark_metadata_by_kind = {
        "CSI800_TOTAL_RETURN": dict(panel.benchmark_metadata),
        "UNIVERSE_EQUAL_WEIGHT_TOTAL_RETURN": {
            "return_kind": "gross_total_return",
            "source": "backtest_reconstructed_from_raw_bars_and_corporate_actions",
        },
        "CASH_CNY": {
            "return_kind": "interest_bearing_cash_act365",
            "annual_rate": config.cash_annual_rate,
            "policy_version": config.cash_policy_version,
            "source": "versioned_cash_policy_curve",
        },
    }
    attribution = _return_attribution(
        rebalances,
        forward_returns,
        info_by_code,
        config.initial_capital,
        total_fees,
        total_slippage_cost,
    )

    outcome = BacktestOutcome(
        calendar=curve_days,
        equity=equity_curve,
        daily_returns=daily_returns,
        benchmark=benchmark,
        benchmark_kind=benchmark_kind,
        rebalances=rebalances,
        final_value=round(equity_curve[-1], 2),
        total_fees=round(total_fees, 2),
        avg_turnover=(
            fmean([detail.turnover for detail in rebalances]) if rebalances else 0.0
        ),
        forward_returns=forward_returns,
        scores_by_date=scores_by_date,
        warnings=warnings,
        attribution=attribution,
        groups_by_date=groups_by_date,
        benchmark_metadata=dict(panel.benchmark_metadata),
        benchmarks=benchmarks,
        benchmark_metadata_by_kind=benchmark_metadata_by_kind,
        execution_limit_data_coverage=limit_coverage,
        total_cash_interest=round(total_cash_interest, 2),
        cash_ledgers=cash_ledgers,
        total_dividend_tax=round(total_dividend_tax, 2),
        factor_values_by_date=factor_values_by_date,
        factor_weight_history=factor_weight_history,
    )
    return outcome


def _return_attribution(
    rebalances: list[RebalanceDetail],
    forward_returns: list[tuple[date, dict[str, float]]],
    info_by_code: dict[str, StockInfo],
    initial_capital: float,
    total_fees: float,
    total_slippage_cost: float,
) -> dict[str, float]:
    """逐期同量纲直接贡献，再用 Carino 方法几何链接。"""
    forwards = dict(forward_returns)
    period_rows: list[dict[str, float]] = []
    for detail in rebalances:
        returns = forwards.get(detail.signal_date)
        if not returns or not detail.target:
            continue
        universe_return = fmean(returns.values())
        industry_returns: dict[str, list[float]] = {}
        for code, value in returns.items():
            industry = info_by_code.get(code, StockInfo(code, code)).industry
            industry_returns.setdefault(industry, []).append(value)
        industry_mean = {
            industry: fmean(values) for industry, values in industry_returns.items()
        }
        row = {
            "selection": 0.0,
            "industry_allocation": 0.0,
            "style": 0.0,
            "market": 0.0,
            "cash_drag": 0.0,
            "fees": 0.0,
            "slippage": 0.0,
            "residual_unexplained": 0.0,
        }
        for code, weight in detail.target.items():
            if code not in returns:
                continue
            industry = info_by_code.get(code, StockInfo(code, code)).industry
            peer = industry_mean.get(industry, universe_return)
            row["selection"] += weight * (returns[code] - peer)
            row["industry_allocation"] += weight * (peer - universe_return)
            row["market"] += weight * universe_return
        deviations = detail.diagnostics.get("exposure_deviations", {})
        if isinstance(deviations, dict):
            beta_deviation = deviations.get("beta")
            if isinstance(beta_deviation, (int, float)):
                row["style"] += float(beta_deviation) * universe_return
        row["cash_drag"] -= (
            max(1.0 - sum(detail.target.values()), 0.0) * universe_return
        )
        actual_period_return = sum(
            weight * returns.get(code, 0.0)
            for code, weight in detail.target.items()
        )
        row["residual_unexplained"] = actual_period_return - sum(
            row.values()
        )
        period_rows.append(row)
    if period_rows and initial_capital > 0:
        for row in period_rows:
            row["fees"] = -total_fees / initial_capital / len(period_rows)
            row["slippage"] = (
                -total_slippage_cost / initial_capital / len(period_rows)
            )
    from app.services.performance_attribution import geometric_link

    linked = geometric_link(period_rows)
    return {
        key: float(value)
        for key, value in dict(linked["linked_contributions"]).items()
    }


def account_period_attribution(
    previous_value: float,
    current_value: float,
    fees: float,
    slippage: float,
    *,
    benchmark_return: float = 0.0,
    stock_weights: dict[str, float] | None = None,
    stock_returns: dict[str, float] | None = None,
    industries: dict[str, str] | None = None,
    beta_deviation: float = 0.0,
    cash_weight: float = 0.0,
) -> dict[str, float]:
    """回测/前向账本共用单期桥接；账本差异单列 residual。"""
    if previous_value <= 0:
        return {
            "selection": 0.0,
            "industry_allocation": 0.0,
            "style": 0.0,
            "market": 0.0,
            "cash_drag": 0.0,
            "fees": 0.0,
            "slippage": 0.0,
            "residual_unexplained": 0.0,
        }
    total_return = current_value / previous_value - 1.0
    fee_contribution = -fees / previous_value
    slippage_contribution = -slippage / previous_value
    weights = stock_weights or {}
    returns = stock_returns or {}
    groups = industries or {}
    industry_values: dict[str, list[float]] = {}
    for code, value in returns.items():
        industry_values.setdefault(groups.get(code, "未知"), []).append(value)
    industry_returns = {
        industry: fmean(values)
        for industry, values in industry_values.items()
        if values
    }
    industry_allocation = sum(
        weight
        * (
            industry_returns.get(groups.get(code, "未知"), benchmark_return)
            - benchmark_return
        )
        for code, weight in weights.items()
        if code in returns
    )
    style = beta_deviation * benchmark_return
    cash_drag = -max(cash_weight, 0.0) * benchmark_return
    market = benchmark_return
    selection = sum(
        weight
        * (
            returns[code]
            - industry_returns.get(groups.get(code, "未知"), benchmark_return)
        )
        for code, weight in weights.items()
        if code in returns
    )
    from app.services.performance_attribution import account_return_bridge

    return {
        key: float(value) if not isinstance(value, bool) else value
        for key, value in account_return_bridge(
            total_return=total_return,
            direct_selection=selection,
            industry_allocation=industry_allocation,
            style=style,
            market=market,
            cash_drag=cash_drag,
            fees=fee_contribution,
            slippage=slippage_contribution,
        ).items()
    }


def _build_benchmark(
    panel: MarketPanel,
    infos: list[StockInfo],
    curve_days: list[date],
    config: BacktestConfig,
    entry_date: date | None,
    signal_date: date | None,
) -> tuple[list[float], str, list[str]]:
    """构造与曲线逐日对齐的基准净值（起点 1.0）。

    指数基准：index_bars 非空时按收盘点位买入持有（首日对齐 1.0，
    指数缺测的交易日沿用前值）；否则回退 universe 等权 B0
    （首日 universe 等权买入持有，逐日再平衡近似为每日等权平均收益）。
    """
    warnings: list[str] = []
    if entry_date is None:
        warnings.append("区间内没有可执行的首期信号，基准保持为 1")
        kind = (
            "CSI800_TOTAL_RETURN"
            if config.benchmark_index == "H00906"
            and config.benchmark_return_kind == "gross_total_return"
            else f"index:{config.benchmark_index}"
            if config.benchmark_index
            else "equal_weight"
        )
        return [1.0] * len(curve_days), kind, warnings
    if panel.index_series:
        index_by_date = dict(panel.index_series)
        required_days = [day for day in curve_days if day >= entry_date]
        missing_days = [day for day in required_days if day not in index_by_date]
        if config.benchmark_required and missing_days:
            preview = "、".join(day.isoformat() for day in missing_days[:5])
            raise BacktestError(
                f"正式基准 {config.benchmark_index} 缺失 "
                f"{len(missing_days)} 个策略交易日（{preview}），禁止前值填充"
            )
        base = None
        last = None
        series: list[float] = []
        for day in curve_days:
            if day < entry_date:
                series.append(1.0)
                continue
            value = index_by_date.get(day, last)
            if value is not None and value > 0:
                if base is None:
                    base = value
                last = value
                series.append(value / base)
            else:
                series.append(series[-1] if series else 1.0)
        kind = (
            "CSI800_TOTAL_RETURN"
            if config.benchmark_index == "H00906"
            and config.benchmark_return_kind == "gross_total_return"
            else f"index:{config.benchmark_index}"
        )
        return series, kind, warnings

    # 等权基准：与策略同一 T+1 日按开盘价等权买入持有。
    if config.benchmark_index:
        warnings.append(
            f"指数 {config.benchmark_index} 行情不可用，基准回退为 universe 等权买入持有"
        )
    codes = [info.code for info in infos]
    if signal_date is not None and panel.universe_by_date:
        members = panel.universe_by_date.get(signal_date, frozenset())
        codes = [code for code in codes if code in members]
    if not codes:
        warnings.append("universe 为空，基准退化为恒 1（零收益）")
        return [1.0] * len(curve_days), "equal_weight", warnings

    base_prices: dict[str, float] = {}
    for code in codes:
        entry_bar = panel.bar_lookup.get(code, {}).get(entry_date)
        price = (
            entry_bar.open
            if entry_bar is not None and entry_bar.open is not None
            else entry_bar.close
            if entry_bar is not None
            else None
        )
        if price is not None and price > 0:
            base_prices[code] = price
    if not base_prices:
        warnings.append("universe 股票首日无有效价格，基准退化为恒 1")
        return [1.0] * len(curve_days), "equal_weight", warnings

    last_seen = dict(base_prices)
    benchmark_shares = {code: 1.0 / price for code, price in base_prices.items()}
    benchmark_cash = {code: 0.0 for code in base_prices}
    benchmark_restricted_value: dict[str, float] = {}
    series = []
    for day in curve_days:
        if day < entry_date:
            series.append(1.0)
            continue
        for action in panel.corporate_actions_by_date.get(day, ()):
            if action.code not in benchmark_shares:
                continue
            held = benchmark_shares[action.code]
            if action.kind == "terminal":
                resolution = corporate_actions.resolve_terminal(
                    terminal_type=action.terminal_type,
                    terminal_price=action.terminal_price,
                    consideration_status=action.consideration_status,
                    restricted_valuation_per_share=(
                        action.restricted_valuation_per_share
                    ),
                )
                if resolution.action == "cash_settlement":
                    benchmark_cash[action.code] += held * resolution.cash_per_share
                    benchmark_shares[action.code] = 0.0
                else:
                    benchmark_restricted_value[action.code] = (
                        held * resolution.restricted_value_per_share
                    )
                    benchmark_shares[action.code] = 0.0
            elif action.kind in {"cash_entitlement", "distribution"}:
                benchmark_cash[action.code] += held * action.cash_per_share
                if action.kind == "distribution":
                    benchmark_shares[action.code] = held * (1.0 + action.share_ratio)
            elif action.kind == "cash_payment":
                continue
            elif action.kind == "rights_issue":
                if (
                    action.subscription_price is not None
                    and action.subscription_price > 0
                ):
                    decision = corporate_actions.decide_rights_issue(
                        held_shares=held,
                        subscription_ratio=action.subscription_ratio,
                        subscription_price=action.subscription_price,
                        available_cash=max(benchmark_cash[action.code], 0.0),
                        portfolio_value=1.0,
                        rights_tradable=action.rights_tradable,
                        right_market_price=action.right_market_price,
                    )
                    subscribed = decision.subscribed_shares
                    benchmark_cash[action.code] += (
                        decision.rights_sale_cash
                        - subscribed * action.subscription_price
                    )
                    benchmark_shares[action.code] += subscribed
            elif action.kind in {"merger", "code_change"}:
                successor = action.successor_code
                if successor and action.share_ratio > 0:
                    conversion = corporate_actions.convert_registered_shares(
                        raw_shares=held * action.share_ratio,
                        cash_compensation_per_fraction=(
                            action.cash_compensation_per_fraction
                        ),
                        registration_increment=1e-9,
                    )
                    benchmark_shares[successor] = (
                        benchmark_shares.get(successor, 0.0)
                        + conversion.registered_shares
                    )
                    benchmark_cash[action.code] += conversion.cash_compensation
                    benchmark_cash.setdefault(successor, 0.0)
                    successor_price = _last_price_before(
                        panel.bars_by_code.get(successor, []),
                        day,
                        last_seen.get(successor),
                    )
                    if successor_price is not None:
                        last_seen[successor] = successor_price
                    benchmark_shares[action.code] = 0.0
            else:
                benchmark_shares[action.code] = held * (1.0 + action.share_ratio)
        total = 0.0
        for code, held in benchmark_shares.items():
            price = _last_price_before(
                panel.bars_by_code.get(code, []), day, last_seen.get(code)
            )
            if price is not None:
                last_seen[code] = price
            total += (
                held * last_seen.get(code, 0.0)
                + benchmark_cash.get(code, 0.0)
                + benchmark_restricted_value.get(code, 0.0)
            )
        series.append(total / len(base_prices))
    return series, "equal_weight", warnings


# ---------------------------------------------------------------------------
# validation 统计（Rank IC / 五档单调性，复用 quant_stats 纯函数）
# ---------------------------------------------------------------------------


def validation_stats(
    scores_by_date: list[tuple[date, dict[str, float]]],
    forward_returns: list[tuple[date, dict[str, float]]],
) -> dict[str, float | int | list[float | None] | bool | None]:
    """按期计算 Rank IC 和五档收益，再做时间序列汇总。"""
    import math
    import random
    from app.services import quant_stats as stats

    forwards_by_date = dict(forward_returns)
    rank_ics: list[float] = []
    period_quintiles: list[tuple[float | None, ...]] = []
    for signal_day, scores in scores_by_date[:-1]:  # 最后一期无前瞻收益
        forwards = forwards_by_date.get(signal_day)
        if not forwards:
            continue
        codes = [code for code in scores if code in forwards]
        if len(codes) < 3:
            continue
        score_list = [scores[code] for code in codes]
        forward_list = [forwards[code] for code in codes]
        ic = stats.rank_ic(score_list, forward_list)
        if ic is not None:
            rank_ics.append(ic)
        quintile = stats.quintile_monotonicity(score_list, forward_list)
        if quintile is not None:
            period_quintiles.append(quintile.quintile_returns)

    quintile_returns: list[float | None] = []
    if period_quintiles:
        for bucket in range(5):
            values = [
                row[bucket] for row in period_quintiles if row[bucket] is not None
            ]
            quintile_returns.append(fmean(values) if values else None)
    valid_quintiles = [value for value in quintile_returns if value is not None]
    quintile_spread = (
        valid_quintiles[-1] - valid_quintiles[0] if len(valid_quintiles) >= 2 else None
    )
    monotonic = (
        len(quintile_returns) == 5
        and all(value is not None for value in quintile_returns)
        and all(
            quintile_returns[index] > quintile_returns[index - 1]  # type: ignore[operator]
            for index in range(1, 5)
        )
    )
    concordant = discordant = 0
    for left in range(len(valid_quintiles) - 1):
        for right in range(left + 1, len(valid_quintiles)):
            if valid_quintiles[right] > valid_quintiles[left]:
                concordant += 1
            elif valid_quintiles[right] < valid_quintiles[left]:
                discordant += 1
    pairs = len(valid_quintiles) * (len(valid_quintiles) - 1) / 2
    kendall_tau = (concordant - discordant) / pairs if pairs > 0 else None
    ic_mean = fmean(rank_ics) if rank_ics else None
    ic_std = None
    if len(rank_ics) >= 2:
        ic_std = math.sqrt(
            sum((value - ic_mean) ** 2 for value in rank_ics) / (len(rank_ics) - 1)
        )
    newey_west_t = None
    newey_west_se = None
    if ic_mean is not None and len(rank_ics) >= 3:
        centered = [value - ic_mean for value in rank_ics]
        lag = min(int(len(rank_ics) ** 0.25), len(rank_ics) - 1)
        long_run_variance = sum(value * value for value in centered) / len(centered)
        for offset in range(1, lag + 1):
            covariance = sum(
                centered[index] * centered[index - offset]
                for index in range(offset, len(centered))
            ) / len(centered)
            long_run_variance += 2.0 * (1.0 - offset / (lag + 1)) * covariance
        newey_west_se = math.sqrt(max(long_run_variance, 0.0) / len(rank_ics))
        if newey_west_se > 0:
            newey_west_t = ic_mean / newey_west_se
    ic_decay: list[float | None] = []
    for lag in range(1, 4):
        if len(rank_ics) <= lag + 1:
            ic_decay.append(None)
            continue
        left = rank_ics[:-lag]
        right = rank_ics[lag:]
        left_mean = fmean(left)
        right_mean = fmean(right)
        numerator = sum(
            (a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True)
        )
        denominator = math.sqrt(
            sum((a - left_mean) ** 2 for a in left)
            * sum((b - right_mean) ** 2 for b in right)
        )
        ic_decay.append(numerator / denominator if denominator > 0 else None)
    rank_turnovers: list[float] = []
    for index in range(1, len(scores_by_date)):
        previous = scores_by_date[index - 1][1]
        current = scores_by_date[index][1]
        common = set(previous) & set(current)
        if len(common) < 2:
            continue
        previous_rank = {
            code: rank
            for rank, code in enumerate(
                sorted(common, key=lambda code: previous[code]), start=1
            )
        }
        current_rank = {
            code: rank
            for rank, code in enumerate(
                sorted(common, key=lambda code: current[code]), start=1
            )
        }
        maximum = max(len(common) - 1, 1)
        rank_turnovers.append(
            fmean(
                abs(previous_rank[code] - current_rank[code]) / maximum
                for code in common
            )
        )
    p_value = (
        math.erfc(abs(newey_west_t) / math.sqrt(2.0))
        if newey_west_t is not None
        else None
    )
    bootstrap_ci: list[float] = []
    if len(rank_ics) >= 3:
        generator = random.Random(0)
        block_length = max(1, int(math.sqrt(len(rank_ics))))
        means: list[float] = []
        for _ in range(1000):
            sample: list[float] = []
            while len(sample) < len(rank_ics):
                start = generator.randrange(len(rank_ics))
                sample.extend(
                    rank_ics[(start + offset) % len(rank_ics)]
                    for offset in range(block_length)
                )
            means.append(fmean(sample[: len(rank_ics)]))
        means.sort()
        bootstrap_ci = [
            means[int(0.025 * (len(means) - 1))],
            means[int(0.975 * (len(means) - 1))],
        ]
    return {
        "rank_ic_mean": ic_mean,
        "rank_ic_count": len(rank_ics),
        "rank_ic_std": ic_std,
        "rank_ic_ir": (
            ic_mean / ic_std
            if ic_mean is not None and ic_std is not None and ic_std > 0
            else None
        ),
        "rank_ic_t_stat": (
            ic_mean / (ic_std / math.sqrt(len(rank_ics)))
            if ic_mean is not None and ic_std is not None and ic_std > 0
            else None
        ),
        "rank_ic_hit_rate": (
            sum(value > 0 for value in rank_ics) / len(rank_ics) if rank_ics else None
        ),
        "rank_ic_newey_west_se": newey_west_se,
        "rank_ic_newey_west_t": newey_west_t,
        "rank_ic_p_value": p_value,
        "rank_ic_95_ci": (
            [
                ic_mean - 1.96 * newey_west_se,
                ic_mean + 1.96 * newey_west_se,
            ]
            if ic_mean is not None and newey_west_se is not None
            else []
        ),
        "rank_ic_bootstrap_95_ci": bootstrap_ci,
        "ic_decay_autocorrelation": ic_decay,
        "mean_rank_turnover": (fmean(rank_turnovers) if rank_turnovers else None),
        "multiple_testing_warning": (
            "同时检验多个因子/参数时应使用 Holm/FDR 校正；"
            "当前复合分原始 p 值不得单独作为上线依据"
        ),
        "quintile_period_count": len(period_quintiles),
        "quintile_returns": quintile_returns,
        "quintile_spread": quintile_spread,
        "quintile_kendall_tau": kendall_tau,
        "quintile_monotonic": monotonic,
    }


# ---------------------------------------------------------------------------
# 编排层：仓储装载 → 面板回测（供路由与测试注入仓储调用）
# ---------------------------------------------------------------------------


def load_fundamentals_by_code(
    repository: StockRepository,
    codes: list[str] | None,
    valuation_dates: list[date] | tuple[date, ...] | None = None,
) -> dict[str, list[Fundamentals]]:
    """装载 PIT 财务与指定信号日估值，并按 code 分组。

    财务报告保留真实披露日；估值通过仓储可选扩展按信号日独立取数，
    避免数据库最终估值被回填到历史报告。旧/mock 仓储没有该扩展时，
    继续使用其 Fundamentals 中已有的 EP/BP，保持协议向后兼容。
    """
    snapshots = repository.fundamentals(codes, None)
    valuation_fn = getattr(repository, "valuation_snapshots", None)
    if callable(valuation_fn) and valuation_dates:
        from calendar import monthrange

        signal_days = tuple(sorted(set(valuation_dates)))
        first = signal_days[0]
        # 至少加载五年逐月 PIT 估值；这既给 24 期最低门槛留有停牌/缺数
        # 余量，也避免前向单点调用退化成伪造的 0.5。
        history_days: set[date] = set(signal_days)
        year, month = first.year, first.month
        for offset in range(1, 61):
            absolute_month = year * 12 + month - 1 - offset
            history_year, zero_month = divmod(absolute_month, 12)
            history_month = zero_month + 1
            history_days.add(
                date(
                    history_year,
                    history_month,
                    monthrange(history_year, history_month)[1],
                )
            )
        snapshots.extend(
            valuation_fn(list(codes or []), tuple(sorted(history_days)))
        )
    grouped: dict[str, list[Fundamentals]] = {}
    for snapshot in snapshots:
        grouped.setdefault(snapshot.code, []).append(snapshot)
    for series in grouped.values():
        series.sort(key=lambda snap: snap.available_at)
    return grouped


def validate_historical_universe_coverage(
    memberships: dict[date, UniverseMembership],
    infos: list[StockInfo],
    panel: MarketPanel,
    fundamentals_by_code: dict[str, list[Fundamentals]],
    minimum: float,
) -> tuple[list[str], float]:
    """检查每个动态股票池信号日的核心数据覆盖，低于门槛即拒绝回测。"""
    if not memberships:
        return [], 1.0
    info_by_code = {info.code: info for info in infos}
    summaries: list[str] = []
    minimum_observed = 1.0
    for day, selection in sorted(memberships.items()):
        members = set(selection.members)
        if not members:
            raise BacktestError(f"{day.isoformat()} 历史指数股票池为空")
        recent_cutoff = day - timedelta(days=10)
        observed_daily = {
            code
            for code in members
            if any(bar.trade_date <= day for bar in panel.bars_by_code.get(code, []))
        }
        recently_traded = {
            code
            for code in observed_daily
            if any(
                recent_cutoff <= bar.trade_date <= day
                for bar in panel.bars_by_code.get(code, [])
            )
        }
        historical_industries = panel.industry_by_date.get(day, {})
        industry = {
            code
            for code in members
            if (
                historical_industries.get(code)
                if panel.industry_by_date
                else (info_by_code[code].industry if code in info_by_code else None)
            )
            not in (None, "", "未知")
        }
        financial: set[str] = set()
        valuation: set[str] = set()
        fresh_valuation: set[str] = set()
        for code in members:
            for snapshot in fundamentals_by_code.get(code, []):
                if snapshot.available_at > day:
                    break
                if any(
                    value is not None
                    for value in (
                        snapshot.roe,
                        snapshot.gross_margin,
                        snapshot.ocf_to_profit,
                        snapshot.debt_ratio,
                    )
                ):
                    financial.add(code)
                if snapshot.ep is not None or snapshot.bp is not None:
                    valuation.add(code)
                    if (
                        snapshot.valuation_date is None
                        or snapshot.valuation_date >= recent_cutoff
                    ):
                        fresh_valuation.add(code)
        # 停牌股在信号日前可能超过十天没有成交，也不会产生新的日估值。
        # 这属于“已知但不可交易”，不是数据缺失：完整性门禁检查是否存在
        # 历史行情/估值，实际选股与成交层仍按 recently_traded 排除停牌标的。
        complete = observed_daily & industry & financial & valuation
        ratio = len(complete) / len(members)
        minimum_observed = min(minimum_observed, ratio)
        summaries.append(
            f"{day.isoformat()} 完整 {len(complete)}/{len(members)}"
            f"（历史日线{len(observed_daily)}、近10日成交{len(recently_traded)}、"
            f"行业{len(industry)}、财务{len(financial)}、估值{len(valuation)}、"
            f"近10日估值{len(fresh_valuation)}）"
        )
        if ratio + 1e-12 < minimum:
            raise BacktestError(
                f"历史股票池核心数据覆盖 {ratio:.1%} 低于门槛 {minimum:.1%}："
                + summaries[-1]
            )
    return summaries, minimum_observed


def persist_historical_readiness(
    db: object,
    *,
    config: BacktestConfig,
    signal_days: list[date],
    memberships: dict[date, UniverseMembership],
    infos: list[StockInfo],
    panel: MarketPanel,
    fundamentals_by_code: dict[str, list[Fundamentals]],
) -> dict[str, object]:
    """为每个历史信号日保存逐股逐字段、来源和阻断原因。"""
    from sqlalchemy.orm import Session

    from app.services.quant_data_governance import save_readiness

    if not isinstance(db, Session):
        return {"signal_days": 0, "reports": 0, "skipped": "non_sql_session"}
    info_by_code = {info.code: info for info in infos}
    total = ready = 0
    snapshot_hash = config.data_snapshot_sha256
    if not snapshot_hash:
        snapshot_basis = {
            day.isoformat(): {
                code: snapshot.isoformat()
                for code, snapshot in sorted(
                    (
                        memberships[day].snapshot_dates
                        if day in memberships
                        else {}
                    ).items()
                )
            }
            for day in signal_days
        }
        snapshot_hash = hashlib.sha256(
            json.dumps(
                snapshot_basis,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    for day in signal_days:
        selection = memberships.get(day)
        members = (
            set(selection.members)
            if selection is not None
            else set(config.candidate_codes or info_by_code)
        )
        recent_cutoff = day - timedelta(days=10)
        industry_map = panel.industry_by_date.get(day, {})
        rows: dict[str, dict[str, object]] = {}
        for code in sorted(members):
            recent_bars = [
                bar
                for bar in panel.bars_by_code.get(code, [])
                if recent_cutoff <= bar.trade_date <= day
            ]
            latest_bar = max(
                recent_bars, key=lambda bar: bar.trade_date, default=None
            )
            snapshots = [
                item
                for item in fundamentals_by_code.get(code, [])
                if item.available_at <= day
            ]
            latest_fundamental = max(
                snapshots, key=lambda item: item.available_at, default=None
            )
            financial_ready = (
                latest_fundamental is not None
                and any(
                    value is not None
                    for value in (
                        latest_fundamental.roe,
                        latest_fundamental.gross_margin,
                        latest_fundamental.ocf_to_profit,
                        latest_fundamental.debt_ratio,
                    )
                )
            )
            valuation_ready = (
                latest_fundamental is not None
                and (
                    latest_fundamental.ep is not None
                    or latest_fundamental.bp is not None
                )
                and (
                    latest_fundamental.valuation_date is None
                    or latest_fundamental.valuation_date >= recent_cutoff
                )
            )
            industry = (
                industry_map.get(code)
                if panel.industry_by_date
                else (
                    info_by_code[code].industry
                    if code in info_by_code
                    else None
                )
            )
            gates = {
                "daily_same_period": latest_bar is not None,
                "industry_pit": industry not in (None, "", "未知"),
                "financial_pit": financial_ready,
                "valuation_pit": valuation_ready,
            }
            blocked = [
                label
                for label, passed in gates.items()
                if not passed
            ]
            rows[code] = {
                **gates,
                "source_status": {
                    "membership_snapshot": (
                        {
                            index: snapshot.isoformat()
                            for index, snapshot in selection.snapshot_dates.items()
                        }
                        if selection is not None
                        else {"mode": "fixed_candidate_snapshot"}
                    ),
                    "membership_source_hashes": {},
                    "daily_data_date": (
                        latest_bar.trade_date.isoformat()
                        if latest_bar is not None
                        else None
                    ),
                    "daily_source": "governed_research_parquet",
                    "financial_available_date": (
                        latest_fundamental.available_at.isoformat()
                        if latest_fundamental is not None
                        else None
                    ),
                    "financial_period": (
                        latest_fundamental.period.isoformat()
                        if latest_fundamental is not None
                        and latest_fundamental.period is not None
                        else None
                    ),
                    "valuation_date": (
                        latest_fundamental.valuation_date.isoformat()
                        if latest_fundamental is not None
                        and latest_fundamental.valuation_date is not None
                        else None
                    ),
                    "industry": industry,
                    "industry_basis": (
                        "SW2021_effective_interval"
                        if panel.industry_by_date
                        else "fixed_snapshot"
                    ),
                    "execution_reference": {
                        "authoritative_limit": bool(
                            latest_bar is not None
                            and latest_bar.up_limit is not None
                            and latest_bar.down_limit is not None
                        ),
                        "suspended": (
                            latest_bar.suspended
                            if latest_bar is not None
                            else None
                        ),
                    },
                    "degraded": False,
                    "blocked_reasons": blocked,
                },
            }
        result = save_readiness(
            db,
            config.strategy_name,
            day,
            rows,
            strategy_version_id=config.strategy_version_id,
            data_snapshot_sha256=snapshot_hash,
        )
        total += int(result["total"])
        ready += int(result["ready"])
    return {
        "signal_days": len(signal_days),
        "reports": total,
        "ready": ready,
        "data_snapshot_sha256": snapshot_hash,
    }


def run_backtest(
    db: object = None,
    config: BacktestConfig | None = None,
    repository: StockRepository | None = None,
) -> BacktestOutcome:
    """回测入口：动态装载仓储（显式注入 > 工厂 > 未来模块 > ORM 探测）。"""
    if config is None:
        raise BacktestError("缺少回测配置 BacktestConfig")
    repo = load_repository(db, repository)
    if repo is None:
        raise BacktestError(
            "股票数据仓储不可用：请注入 repository，或等待 stock data 模块落地"
            "（app.services.stock_repository.get_repository / ORM 模型探测）"
        )
    calendar = repo.trade_calendar(config.start, None)
    valuation_dates = signal_dates(
        calendar.days, config.start, config.end, config.initial_signal
    )
    memberships: dict[date, UniverseMembership] = {}
    data_warnings: list[str] = []

    if config.candidate_codes:
        requested_codes: list[str] | None = list(config.candidate_codes)
    elif config.universe_indices:
        membership_fn = getattr(repo, "universe_members_as_of", None)
        if not callable(membership_fn):
            raise BacktestError(
                "仓储不支持历史指数成分，无法执行动态股票池回测；"
                "请显式传 candidate_codes 运行固定股票池研究"
            )
        memberships = dict(membership_fn(config.universe_indices, valuation_dates))
        missing = {
            day: selection.missing_indices
            for day, selection in memberships.items()
            if selection.missing_indices
        }
        if missing:
            sample_day = min(missing)
            raise BacktestError(
                f"{sample_day.isoformat()} 缺少指数"
                f" {','.join(missing[sample_day])} 的历史成分快照，拒绝回退当前成分"
            )
        requested_codes = sorted(
            {code for selection in memberships.values() for code in selection.members}
        )
        if not requested_codes and valuation_dates:
            raise BacktestError("历史指数股票池为空，请先导入成分快照")
        if memberships:
            first_day = min(memberships)
            last_day = max(memberships)
            first_basis = ",".join(
                f"{index}@{snapshot.isoformat()}"
                for index, snapshot in sorted(
                    memberships[first_day].snapshot_dates.items()
                )
            )
            last_basis = ",".join(
                f"{index}@{snapshot.isoformat()}"
                for index, snapshot in sorted(
                    memberships[last_day].snapshot_dates.items()
                )
            )
            data_warnings.append(
                f"历史动态股票池已启用：首期 {first_basis}；末期 {last_basis}"
            )
    else:
        requested_codes = None

    infos = repo.list_stocks(requested_codes)
    if not infos:
        raise BacktestError("股票清单为空：数据仓储尚无股票元信息")
    codes = [info.code for info in infos]
    if memberships:
        known = set(codes)
        missing_counts = {
            day: len(selection.members - known)
            for day, selection in memberships.items()
        }
        max_missing = max(missing_counts.values(), default=0)
        if max_missing:
            affected = sum(count > 0 for count in missing_counts.values())
            data_warnings.append(
                f"历史成分主数据不完整：最多单期缺 {max_missing} 只，"
                f"影响 {affected}/{len(missing_counts)} 个信号日"
            )
    # 因子至少需要 253 个交易日；行情装载必须早于回测净值起点，
    # 否则每次回测的前约一年会因“没有历史”而错误持币。
    history_start = config.start - timedelta(days=550)
    panel = build_panel(
        repo,
        codes,
        history_start,
        config.end,
        config.benchmark_index,
        config.benchmark_required,
        config.benchmark_return_kind,
    )
    if memberships:
        panel = replace(
            panel,
            universe_by_date={
                day: selection.members for day, selection in memberships.items()
            },
            universe_snapshot_dates={
                day: dict(selection.snapshot_dates)
                for day, selection in memberships.items()
            },
            data_warnings=panel.data_warnings + tuple(data_warnings),
        )
    fundamentals = load_fundamentals_by_code(repo, codes, valuation_dates)
    industry_fn = getattr(repo, "industries_as_of", None)
    if callable(industry_fn) and valuation_dates:
        industry_history = dict(industry_fn(codes, valuation_dates))
        panel = replace(panel, industry_by_date=industry_history)
        if any(not industry_history.get(day) for day in valuation_dates):
            missing_day = next(
                day for day in valuation_dates if not industry_history.get(day)
            )
            raise BacktestError(
                f"{missing_day.isoformat()} 无申万2021历史行业归属，"
                "拒绝使用当前行业造成前视偏差"
            )
    coverage, minimum_observed_coverage = validate_historical_universe_coverage(
        memberships,
        infos,
        panel,
        fundamentals,
        config.min_universe_data_coverage,
    )
    if coverage and panel.data_warnings:
        panel = replace(
            panel,
            data_warnings=panel.data_warnings
            + (f"历史股票池数据门禁通过：{coverage[0]}；{coverage[-1]}",),
        )
    if config.strict_file_manifest:
        access_fn = getattr(repo, "accessed_file_records", None)
        if not config.file_manifest_snapshot_sha256:
            raise BacktestError("正式实验缺少冻结文件清单 SHA-256")
        if not callable(access_fn):
            raise BacktestError("正式实验仓储不支持实际文件访问审计")
        from app.config import get_settings
        from app.services.file_access_manifest import (
            FileManifestMismatch,
            verify_accesses,
        )

        try:
            verify_accesses(
                db,
                root=Path(get_settings().research_data_dir),
                snapshot_sha256=config.file_manifest_snapshot_sha256,
                observations=list(access_fn()),
                strategy_version_id=config.strategy_version_id,
            )
        except FileManifestMismatch as exc:
            raise BacktestError(f"实际读取文件与冻结清单不一致：{exc}") from exc
    persist_historical_readiness(
        db,
        config=config,
        signal_days=list(valuation_dates),
        memberships=memberships,
        infos=infos,
        panel=panel,
        fundamentals_by_code=fundamentals,
    )
    outcome = run_backtest_panel(panel, infos, fundamentals, config)
    outcome.minimum_historical_coverage = minimum_observed_coverage
    return outcome


def sample_curve(
    days: list[date], equity: list[float], benchmark: list[float]
) -> list[tuple[str, float, float]]:
    """控制响应规模：超过上限时均匀抽样（策略/基准同日期对齐）。"""
    n = len(days)
    if n <= MAX_CURVE_POINTS:
        indices = range(n)
    else:
        step = n / MAX_CURVE_POINTS
        indices = sorted({int(i * step) for i in range(MAX_CURVE_POINTS)} | {n - 1})
    return [
        (days[i].isoformat(), round(equity[i], 2), round(benchmark[i], 6))
        for i in indices
    ]


__all__ = [
    "BacktestConfig",
    "BacktestError",
    "BacktestOutcome",
    "CostModel",
    "Fill",
    "MarketPanel",
    "RebalanceDetail",
    "build_panel",
    "can_trade",
    "daily_move",
    "load_fundamentals_by_code",
    "run_backtest",
    "run_backtest_panel",
    "sample_curve",
    "trade_fee",
    "trade_price",
    "validation_stats",
]
