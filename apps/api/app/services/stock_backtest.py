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
from dataclasses import dataclass, field
from datetime import date
from statistics import fmean

from app.services import stock_factors as factors
from app.services import stock_strategy as strategy
from app.services.stock_repository import (
    Fundamentals,
    MarketBars,
    NamePeriod,
    StockBar,
    StockInfo,
    StockRepository,
    TradeCalendar,
    load_repository,
    one_word_limit,
    price_limit_for,
    st_status_as_of,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量与费用模型
# ---------------------------------------------------------------------------

MAX_CURVE_POINTS = 260  # 曲线抽样上限（与 quant 模块一致）
MAX_TRADE_RECORDS = 500  # 成交记录上限（响应规模受控）


class BacktestError(ValueError):
    """回测参数或数据不足错误，路由层转换为 400。"""


@dataclass(frozen=True)
class CostModel:
    """A股交易费用模型（小数口径）。"""

    commission_rate: float = 0.00025  # 双边佣金（万 2.5）
    min_commission: float = 5.0  # 单笔最低佣金（元）
    stamp_tax_rate: float = 0.0005  # 印花税（仅卖出，0.05%）
    slippage_rate: float = 0.001  # 双边滑点（0.1%）


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
    benchmark_index: str | None = None  # 指数代码；None 或缺失时回退等权基准
    factor_weights: dict[str, float] | None = None  # 因子族权重覆盖（None = 缺省）
    cost: CostModel = field(default_factory=CostModel)


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
    forward_returns: list[tuple[date, dict[str, float]]]  # 各信号日 → 前瞻收益（validation 用）
    scores_by_date: list[tuple[date, dict[str, float]]]  # 各信号日 → 复合分（validation 用）
    warnings: list[str] = field(default_factory=list)


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
    research_bars_by_code: dict[str, list[StockBar]] = field(default_factory=dict)
    name_histories: dict[str, list[NamePeriod]] = field(default_factory=dict)

    def research_series(self, code: str) -> list[StockBar]:
        """研究口径序列：qfq 存在时返回 qfq，否则回退执行口径 raw。"""
        return self.research_bars_by_code.get(code) or self.bars_by_code.get(code, [])


def build_panel(
    repository: StockRepository,
    codes: list[str],
    start: date,
    end: date,
    benchmark_index: str | None = None,
) -> MarketPanel:
    """装载行情面板；指数序列缺失时静默降级为空（基准回退等权）。

    仓储支持 market_bars（可选扩展协议）时构造研究/执行双口径面板；
    否则退化为执行口径 raw 单口径（research = exec，与旧行为一致）。
    历史名称（name_histories）同为可选扩展，缺失时 ST 按当前名称判定。
    """
    research_map: dict[str, list[StockBar]] = {}
    histories: dict[str, list[NamePeriod]] = {}
    exec_map: dict[str, list[StockBar]] = {}

    market_bars_fn = getattr(repository, "market_bars", None)
    dual: dict[str, MarketBars] | None = None
    if callable(market_bars_fn):
        try:
            dual = market_bars_fn(codes, start, end)
        except Exception:  # noqa: BLE001 - 可选扩展失败降级为单口径
            logger.warning("market_bars 装载失败，降级为 raw 单口径面板", exc_info=True)
            dual = None
    if dual is not None:
        for code in codes:
            entry = dual.get(code)
            if entry is None:
                exec_map[code] = []
                research_map[code] = []
                continue
            exec_map[code] = sorted(
                entry.exec_bars, key=lambda bar: bar.trade_date
            )
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
            logger.warning("name_histories 装载失败，历史 ST 判定降级为当前名称", exc_info=True)
            histories = {}

    lookup = {
        code: {bar.trade_date: bar for bar in series}
        for code, series in exec_map.items()
    }
    calendar = repository.trade_calendar(start, end)
    index_series: list[tuple[date, float]] = []
    if benchmark_index:
        try:
            index_series = repository.index_bars(benchmark_index, start, end)
        except Exception:  # noqa: BLE001 - 指数数据源缺失按无基准降级
            index_series = []
    return MarketPanel(
        calendar=calendar,
        bars_by_code=exec_map,
        bar_lookup=lookup,
        index_series=index_series,
        research_bars_by_code=research_map,
        name_histories=histories,
    )


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
) -> tuple[bool, str]:
    """T+1 成交日可否成交：停牌/无行情不可成交；涨停不可买、跌停不可卖；
    一字涨跌停（振幅≈0 且触板）买卖双向均不可成交。

    side ∈ {"buy", "sell"}；price_limit 为触发容差系数（默认 0.98 档），
    实际触发线 = 板块法定幅度（price_limit_for，主板 10%/创业科创 20%/
    北交所 30%/ST 5%）× 系数；prev_close 必须严格早于成交日。
    返回 (可否成交, 原因)。
    """
    if bar is None:
        return False, "成交日无行情"
    if bar.suspended:
        return False, "成交日停牌"
    price = bar.open if (bar.open is not None and bar.open > 0) else bar.close
    if price <= 0:
        return False, "成交日价格无效"
    board_limit = price_limit_for(code or bar.code, st)
    # 触发线 = 法定幅度 × 触发容差系数；coeff ≥ 1 时直接用法定幅度
    # （price_limit 字段同时承载「主板 10% 近似绝对阈值」的历史用法）。
    trigger = board_limit * price_limit if price_limit < 1.0 else board_limit
    move = daily_move(bar, prev_close)
    if move is not None:
        if one_word_limit(bar, move, board_limit):
            return False, f"一字涨跌停（{move:+.1%}，振幅≈0），买卖双向不可成交"
        if side == "buy" and move >= trigger:
            return False, f"涨停（{move:.1%} ≥ {trigger:.1%}）不可买入"
        if side == "sell" and move <= -trigger:
            return False, f"跌停（{move:.1%} ≤ -{trigger:.1%}）不可卖出"
    return True, ""


def trade_price(bar: StockBar, side: str, slippage_rate: float) -> float:
    """含滑点成交价：T+1 开盘价（缺失回退收盘），买入上浮、卖出下浮。"""
    base = bar.open if (bar.open is not None and bar.open > 0) else bar.close
    return base * (1.0 + slippage_rate) if side == "buy" else base * (1.0 - slippage_rate)


def trade_fee(side: str, amount: float, cost: CostModel) -> float:
    """单笔费用：佣金（双边，最低 5 元）+ 印花税（仅卖出）。"""
    commission = max(amount * cost.commission_rate, cost.min_commission)
    stamp = amount * cost.stamp_tax_rate if side == "sell" else 0.0
    return commission + stamp


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

    info_by_code = {info.code: info for info in infos}
    if config.candidate_codes is not None:
        wanted = set(config.candidate_codes)
        infos = [info for info in infos if info.code in wanted]
    if not infos:
        raise BacktestError("候选股票池为空，请检查 candidate_codes 或股票数据")

    signal_days = set(strategy.month_ends(calendar_days))
    # 首个交易日必须能建仓：若区间起点不是月末信号日，加入起点作为首期信号
    if calendar_days[0] not in signal_days:
        signal_days.add(calendar_days[0])

    cash = config.initial_capital
    shares: dict[str, float] = {}
    last_price: dict[str, float] = {}  # 逐股最近可得收盘（停牌盯市）
    pending_orders: dict[str, float] = {}  # 待执行目标权重（涨跌停/停牌顺延）
    pending_signal_date: date | None = None

    equity_curve: list[float] = []
    curve_days: list[date] = []
    rebalances: list[RebalanceDetail] = []
    total_fees = 0.0
    forward_returns: list[tuple[date, dict[str, float]]] = []
    scores_by_date: list[tuple[date, dict[str, float]]] = []
    warnings: list[str] = []
    last_signal_info: tuple[date, dict[str, float], dict[str, float]] | None = None

    for i, day in enumerate(calendar_days):
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
                        break
                pending_orders = {}
                pending_signal_date = None

            universe, filters = strategy.build_universe(
                infos,
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
            scored = factors.compute_cross_section(
                contexts, day, weights=config.factor_weights
            )
            plan = strategy.build_portfolio(
                scored,
                universe,
                day,
                top_n=config.top_n,
                max_stock_weight=config.max_stock_weight,
                max_industry_weight=config.max_industry_weight,
            )
            detail = RebalanceDetail(
                signal_date=day,
                target=dict(plan.target_weights),
                cash_weight=round(1.0 - plan.invested_weight, 6),
                warnings=list(plan.warnings),
            )
            rebalances.append(detail)
            pending_orders = dict(plan.target_weights)
            pending_signal_date = day
            scores_by_date.append((day, {item.code: item.composite for item in scored}))

            # 上一信号期的前瞻收益（validation 用）：信号日收盘 → 本信号日收盘
            if last_signal_info is not None:
                prev_day, prev_target, prev_scores = last_signal_info
                forwards: dict[str, float] = {}
                for code in prev_scores:
                    base = _last_price_before(panel.research_series(code), prev_day, None)
                    now = _last_price_before(panel.research_series(code), day, None)
                    if base and now and base > 0:
                        forwards[code] = now / base - 1.0
                if forwards:
                    forward_returns.append((prev_day, forwards))
            last_signal_info = (day, dict(plan.target_weights), dict(scores_by_date[-1][1]))
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
        elif pending_orders and pending_signal_date is not None:
            detail = rebalances[-1]
            total_value = cash + sum(
                amount
                * (_last_price_before(panel.bars_by_code.get(code, []), day, None) or 0.0)
                for code, amount in shares.items()
            )
            drifted = (
                {
                    code: amount
                    * (_last_price_before(panel.bars_by_code.get(code, []), day, None) or 0.0)
                    / total_value
                    for code, amount in shares.items()
                }
                if total_value > 0
                else {}
            )
            codes = set(drifted) | set(pending_orders)
            turnover_legs = 0.0
            all_filled = True
            for code in sorted(codes):
                target_w = pending_orders.get(code, 0.0)
                current_w = drifted.get(code, 0.0)
                diff = target_w - current_w
                if abs(diff) < 1e-4:
                    continue  # 权重基本一致，无需成交
                side = "buy" if diff > 0 else "sell"
                bar = panel.bar_lookup.get(code, {}).get(day)
                prev_bar = prev_bar_before(panel.bars_by_code.get(code, []), day)
                prev_close = prev_bar.close if prev_bar is not None else None
                info = info_by_code.get(code)
                st = st_status_as_of(
                    info.name if info else code,
                    panel.name_histories.get(code),
                    day,
                )
                ok, reason = can_trade(
                    bar, prev_close, side, config.price_limit, code=code, st=st
                )
                if not ok:
                    all_filled = False
                    if code not in detail.blocked_codes:
                        detail.blocked_codes.append(code)
                        detail.warnings.append(f"{day.isoformat()} {code} {reason}，顺延")
                    continue
                assert bar is not None  # can_trade 通过则 bar 必有行情
                price = trade_price(bar, side, config.cost.slippage_rate)
                if side == "buy":
                    amount = diff * total_value
                    affordable = cash * (1.0 - 1e-12)
                    spend = min(amount, affordable)
                    if spend <= 0:
                        continue
                    fee = trade_fee("buy", spend, config.cost)
                    spend = max(spend - fee, 0.0)
                    cash -= spend + fee
                    shares[code] = shares.get(code, 0.0) + spend / price
                    total_fees += fee
                    detail.fills.append(
                        Fill(pending_signal_date, day, code, "buy", price,
                             spend / price, spend, fee, "调仓买入")
                    )
                else:
                    sell_shares = min(-diff * total_value / price, shares.get(code, 0.0))
                    if sell_shares <= 0:
                        continue
                    amount = sell_shares * price
                    fee = trade_fee("sell", amount, config.cost)
                    shares[code] = shares.get(code, 0.0) - sell_shares
                    if shares[code] <= 1e-10:
                        shares.pop(code, None)
                    cash += amount - fee
                    total_fees += fee
                    detail.fills.append(
                        Fill(pending_signal_date, day, code, "sell", price,
                             sell_shares, amount, fee, "调仓卖出")
                    )
                turnover_legs += abs(diff)
            detail.turnover = round(detail.turnover + 0.5 * turnover_legs, 6)
            if all_filled:
                pending_orders = {}
                pending_signal_date = None

        # ---- 3) 逐日盯市（停牌沿用前收盘）----
        day_value = cash
        for code, amount in shares.items():
            price = _last_price_before(
                panel.bars_by_code.get(code, []), day, last_price.get(code)
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

    if len(equity_curve) < 2:
        raise BacktestError("回测区间内无任何交易日，无法构造净值曲线")

    daily_returns = [
        equity_curve[i] / equity_curve[i - 1] - 1.0
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] > 0
    ]

    # ---- 基准：指数优先，缺失回退 universe 等权买入持有（B0）----
    benchmark, benchmark_kind, bench_warnings = _build_benchmark(
        panel, infos, curve_days, config
    )
    warnings.extend(bench_warnings)

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
            fmean([detail.turnover for detail in rebalances])
            if rebalances
            else 0.0
        ),
        forward_returns=forward_returns,
        scores_by_date=scores_by_date,
        warnings=warnings,
    )
    return outcome


def _build_benchmark(
    panel: MarketPanel,
    infos: list[StockInfo],
    curve_days: list[date],
    config: BacktestConfig,
) -> tuple[list[float], str, list[str]]:
    """构造与曲线逐日对齐的基准净值（起点 1.0）。

    指数基准：index_bars 非空时按收盘点位买入持有（首日对齐 1.0，
    指数缺测的交易日沿用前值）；否则回退 universe 等权 B0
    （首日 universe 等权买入持有，逐日再平衡近似为每日等权平均收益）。
    """
    warnings: list[str] = []
    if panel.index_series:
        index_by_date = dict(panel.index_series)
        base = None
        last = None
        series: list[float] = []
        for day in curve_days:
            value = index_by_date.get(day, last)
            if value is not None and value > 0:
                if base is None:
                    base = value
                last = value
                series.append(value / base)
            else:
                series.append(series[-1] if series else 1.0)
        return series, f"index:{config.benchmark_index}", warnings

    # 等权基准：首日通过 universe 过滤的股票等权买入持有
    if config.benchmark_index:
        warnings.append(
            f"指数 {config.benchmark_index} 行情不可用，基准回退为 universe 等权买入持有"
        )
    universe, _filters = strategy.build_universe(
        infos, panel.bars_by_code, curve_days[0], config.min_avg_amount
    )
    codes = [info.code for info in universe]
    if not codes:
        warnings.append("universe 为空，基准退化为恒 1（零收益）")
        return [1.0] * len(curve_days), "equal_weight", warnings

    base_prices: dict[str, float] = {}
    for code in codes:
        price = _last_price_before(panel.bars_by_code.get(code, []), curve_days[0], None)
        if price is not None and price > 0:
            base_prices[code] = price
    if not base_prices:
        warnings.append("universe 股票首日无有效价格，基准退化为恒 1")
        return [1.0] * len(curve_days), "equal_weight", warnings

    last_seen = dict(base_prices)
    series = []
    for day in curve_days:
        total = 0.0
        for code, base in base_prices.items():
            price = _last_price_before(panel.bars_by_code.get(code, []), day, last_seen[code])
            if price is not None:
                last_seen[code] = price
            total += last_seen[code] / base
        series.append(total / len(base_prices))
    return series, "equal_weight", warnings


# ---------------------------------------------------------------------------
# validation 统计（Rank IC / 五档单调性，复用 quant_stats 纯函数）
# ---------------------------------------------------------------------------


def validation_stats(
    scores_by_date: list[tuple[date, dict[str, float]]],
    forward_returns: list[tuple[date, dict[str, float]]],
) -> dict[str, float | int | list[float | None] | bool | None]:
    """各调仓期复合分 vs 下一期前瞻收益的 Rank IC 均值与五档单调性。"""
    from app.services import quant_stats as stats

    forwards_by_date = dict(forward_returns)
    rank_ics: list[float] = []
    all_scores: list[float] = []
    all_forwards: list[float] = []
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
            all_scores.extend(score_list)
            all_forwards.extend(forward_list)

    quintile = stats.quintile_monotonicity(all_scores, all_forwards)
    return {
        "rank_ic_mean": fmean(rank_ics) if rank_ics else None,
        "rank_ic_count": len(rank_ics),
        "quintile_returns": list(quintile.quintile_returns) if quintile else [],
        "quintile_spread": quintile.spread if quintile else None,
        "quintile_kendall_tau": quintile.kendall_tau if quintile else None,
        "quintile_monotonic": quintile.monotonic if quintile else False,
    }


# ---------------------------------------------------------------------------
# 编排层：仓储装载 → 面板回测（供路由与测试注入仓储调用）
# ---------------------------------------------------------------------------


def load_fundamentals_by_code(
    repository: StockRepository, codes: list[str] | None
) -> dict[str, list[Fundamentals]]:
    """装载全部 PIT 财务快照并按 code 分组（不过滤 as_of，引擎逐日 PIT）。"""
    snapshots = repository.fundamentals(codes, None)
    grouped: dict[str, list[Fundamentals]] = {}
    for snapshot in snapshots:
        grouped.setdefault(snapshot.code, []).append(snapshot)
    for series in grouped.values():
        series.sort(key=lambda snap: snap.available_at)
    return grouped


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
    infos = repo.list_stocks(list(config.candidate_codes) if config.candidate_codes else None)
    if not infos:
        raise BacktestError("股票清单为空：数据仓储尚无股票元信息")
    codes = [info.code for info in infos]
    panel = build_panel(repo, codes, config.start, config.end, config.benchmark_index)
    fundamentals = load_fundamentals_by_code(repo, codes)
    return run_backtest_panel(panel, infos, fundamentals, config)


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
