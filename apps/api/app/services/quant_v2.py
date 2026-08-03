"""稳健组合策略 V2：月频绝对动量轮动 + 层内 HRP + 波动率目标 + 冻结保护。

流程（全部为只读研究能力，不产生任何实盘下单行为）：
1. 候选池：显式 candidate_codes 或当前持仓基金；装载 FundNav 净值
   （优先累计净值，缺失回退单位净值），对齐到共同交易日交集；
2. 市场层分类：A股/沪深300/港股/恒生科技/美股标普/纳斯达克为权益层，
   黄金/债券/货币/其他海外为防御层（quant_risk.classify_market）；
3. 打分（每月最后一个交易日为信号日，仅使用该日及之前的数据，无未来数据）：
   - 绝对动量 12-1 > 0 过滤（t-21 收盘 / t-252 前一日收盘 - 1，跳过近 21 日）；
   - 同基金家族 A/C/D 份额去重（每家族保留动量最高的一只）；
   - 每个市场层内按动量取前 30%（向上取整，至少 1 只）；
4. 层内配置：近 120 日相关矩阵 + 逆方差递归二分 HRP；数据不足或数值
   异常时回退逆波动，再失败回退等权；各层内部配置后按层预算汇总；
5. 权重约束：单基金 ≤8%、同家族合计 ≤10%、QDII（海外层）合计 ≤30%，
   瀑布式再分配，截断部分保留为现金；
6. 波动率目标：组合 EWMA60（λ=0.94）年化波动超过目标 10% 的 10% 带宽
   上限时按 target/realized 降仓（只降仓、不升仓），降仓部分为现金；
7. 冻结保护：EWMA60 年化波动 ≥25% 且（近5日组合收益 ≥8% 或近10日 ≥12%）
   时，本期冻结调仓、沿用上一期持仓；
8. 成交假设（T+1/T+2）：信号日 T 收盘打分，目标中不含 QDII 基金时
   T+1 按当日净值成交；含 QDII 基金时统一 T+2 成交（成交日买入/卖出
   均按 T+2 净值）。费用模型接口预留（默认零费用）；
9. 基准：全部有效候选等权买入持有（B0），锚定首个成交日。

仅使用标准库 + SQLAlchemy；数据源为 FundNav。
"""

from __future__ import annotations

from datetime import date
from statistics import fmean

from sqlalchemy.orm import Session

from app.schemas.quant_v2 import (
    BacktestV2CurvePoint,
    BacktestV2Request,
    BacktestV2Result,
    BacktestV2Summary,
    FeeModelConfig,
    RebalanceV2Detail,
    SignalsV2Response,
    SignalV2Item,
    TradeV2,
)
from app.services import quant_hrp as hrp
from app.services import quant_risk as risk
from app.services.quant import (
    QuantError,
    _annual_return,
    _annual_volatility,
    _daily_returns,
    _load_nav_series,
    _max_drawdown,
    _parse_day,
    _sharpe,
    _win_rate,
)
from app.services.quant_screener import _load_candidates

NAV_LOAD_LIMIT = 5000  # 覆盖 253 动量样本 + 长回测区间
MAX_CURVE_POINTS = 260  # 曲线抽样上限（与 v1 一致）
MAX_TRADES = 500  # 成交记录上限（响应规模受控）
MIN_SAMPLES = risk.MIN_MOMENTUM_SAMPLES  # 入选所需最少净值样本（253）

METHODOLOGY_V2 = (
    "稳健组合 V2：月频调仓。每月最后一个交易日打分（仅用当日及之前数据）："
    "绝对动量 12-1 > 0（t-21 收盘 / t-252 前一日收盘 - 1，跳过最近 21 个交易日）；"
    "按基金名称分市场层（A股/港股/美股/黄金/债券/货币/海外）；同基金家族 A/C/D "
    "份额去重（每家族保留动量最高者）；每层内按动量取前 30%（至少 1 只）；"
    "层内用近 120 日相关矩阵 + 逆方差递归二分 HRP 配置，失败回退逆波动/等权；"
    "权重约束：单基金 ≤8%、同家族 ≤10%、QDII（海外层）合计 ≤30%，截断留现金；"
    "组合 EWMA60（λ=0.94）年化波动目标 10%（只降仓，10% 带宽防抖）；"
    "高波动（≥25%）+ 急反弹（近5日 ≥8% 或近10日 ≥12%）冻结调仓、沿用持仓。"
    "信号日 T 收盘打分，T+1 按当日净值成交（目标含 QDII 基金时统一 T+2 成交）；"
    "费用模型接口预留（默认零费用）；不卖空、现金零收益。"
    "基准为全部有效候选等权买入持有（B0，锚定首个成交日净值 1.0，与策略同日起算）。"
    "年化按 252 个交易日折算，夏普比率采用 2% 无风险利率。"
    "幸存者偏差声明：回测基于当前候选池（当前持仓或当前指定成员），"
    "历史时点已清盘/退市/调出池的基金不在样本内，样本外表现可能系统性劣于回测。"
    "仅为研究回测，不构成投资建议，不产生任何自动交易。"
)


# ---------------------------------------------------------------------------
# 数据装载与对齐
# ---------------------------------------------------------------------------


def _load_aligned_panels(
    db: Session, candidate_codes: list[str] | None, start_date: str | None, end_date: str | None
) -> tuple[list[date], dict[str, list[float]], list[str], dict[str, str], list[str]]:
    """装载候选基金净值并对齐到共同交易日。

    返回 (共同交易日升序, {code: 等长净值序列}, 有效候选代码, {code: 名称}, 警告)。
    样本不足 253 条的基金被剔除并提示。
    """
    start = _parse_day(start_date)
    end = _parse_day(end_date)
    if start and end and start > end:
        raise QuantError("start_date 不能晚于 end_date")

    instruments, warnings = _load_candidates(db, candidate_codes)

    series_by_code: dict[str, list[tuple[date, float]]] = {}
    names: dict[str, str] = {}
    for instrument in instruments:
        series = _load_nav_series(db, instrument.id, start=start, end=end, limit=NAV_LOAD_LIMIT)
        if len(series) < MIN_SAMPLES:
            warnings.append(
                f"基金 {instrument.code}（{instrument.name}）净值样本不足 "
                f"{MIN_SAMPLES} 条（当前 {len(series)} 条），已从候选池剔除"
            )
            continue
        series_by_code[instrument.code] = series
        names[instrument.code] = instrument.name

    if len(series_by_code) < 2:
        raise QuantError(
            f"有效候选基金不足 2 只（{len(series_by_code)} 只满足样本要求），"
            "样本不足：请扩大候选池或核对净值区间（动量 12-1 需要至少 253 个净值点）"
        )

    common: set[date] | None = None
    for series in series_by_code.values():
        days = {d for d, _ in series}
        common = days if common is None else (common & days)
    calendar = sorted(common or set())
    if len(calendar) < MIN_SAMPLES + 22:
        raise QuantError(
            f"候选基金共同交易日仅 {len(calendar)} 天，不足动量样本 + 一个调仓周期"
            f"（{MIN_SAMPLES + 22} 天），请核对净值区间"
        )

    panels: dict[str, list[float]] = {}
    for code, series in series_by_code.items():
        values = dict(series)
        panels[code] = [values[d] for d in calendar]

    if any(len(s) != len(calendar) for s in series_by_code.values()):
        warnings.append(
            f"各基金净值日期不完全一致，已对齐到共同交易日交集（{len(calendar)} 天）"
        )

    codes = [instrument.code for instrument in instruments if instrument.code in panels]
    return calendar, panels, codes, names, warnings


# ---------------------------------------------------------------------------
# 费用模型钩子（接口预留：默认零费用）
# ---------------------------------------------------------------------------


def apply_fee(amount: float, action: str, fee_model: FeeModelConfig) -> float:
    """按费用模型计算单笔费用（默认零费用）。

    预留扩展点：后续接入真实申购/赎回分档费率时仅需修改本函数，
    回测主流程（现金扣减、份额计算）不变。amount 为成交金额（正数）。
    """
    if amount <= 0:
        return 0.0
    rate = fee_model.buy_fee_rate if action == "buy" else fee_model.sell_fee_rate
    fee = amount * (rate + fee_model.slippage_rate)
    if fee > 0:
        fee = max(fee, fee_model.min_fee)
    return fee


# ---------------------------------------------------------------------------
# 打分与目标权重（纯函数，仅使用信号日及之前的数据）
# ---------------------------------------------------------------------------


def select_candidates(
    panels: dict[str, list[float]],
    names: dict[str, str],
    t: int,
) -> tuple[list[dict], list[str]]:
    """在信号日索引 t（含）处打分选基（无未来数据：只用 panels[code][:t+1]）。

    返回 (入选候选列表（含 market/family/momentum/rank）, 警告)。
    """
    warnings: list[str] = []
    candidates: list[dict] = []
    for code, values in panels.items():
        window = values[: t + 1]
        momentum = risk.absolute_momentum_12_1(window)
        if momentum is None or momentum <= 0:
            continue
        name = names.get(code, code)
        candidates.append(
            {
                "code": code,
                "name": name,
                "market": risk.classify_market(name),
                "family": risk.fund_family(name),
                "momentum": momentum,
            }
        )

    # 同家族 A/C/D 去重（保留动量最高者）
    deduped, dropped = risk.dedupe_share_classes(candidates)
    if dropped:
        warnings.append(f"同基金家族份额去重，剔除 {len(dropped)} 只：{', '.join(sorted(dropped))}")

    # 按市场层分组，层内取动量前 30%
    selected: list[dict] = []
    by_market: dict[str, list[dict]] = {}
    for item in deduped:
        by_market.setdefault(item["market"], []).append(item)
    for market, members in sorted(by_market.items()):
        for item in risk.select_top_in_market(members):
            item["market_candidates"] = len(members)
            selected.append(item)
    return selected, warnings


def compute_target_weights(
    selected: list[dict],
    panels: dict[str, list[float]],
    t: int,
    top_n: int,
    max_fund: float,
    max_family: float,
    max_qdii: float,
) -> tuple[dict[str, float], str, list[str]]:
    """由入选候选计算目标权重：层内 HRP → top_n → 权重约束。

    返回 ({code: 权重}, 配置方法, 警告)。权重合计 ≤ 1（截断为现金）。
    """
    warnings: list[str] = []
    if not selected:
        return {}, "equal_weight", warnings

    # 层内 HRP：各市场层独立配置；层预算按入选只数等比分配
    by_market: dict[str, list[dict]] = {}
    for item in selected:
        by_market.setdefault(item["market"], []).append(item)

    methods: list[str] = []
    raw_weights: dict[str, float] = {}
    total_members = len(selected)
    for market, members in sorted(by_market.items()):
        layer_budget = len(members) / total_members
        layer_panels = {item["code"]: panels[item["code"]][: t + 1] for item in members}
        layer_weights, method = hrp.allocate(layer_panels)
        methods.append(method)
        for code, w in layer_weights.items():
            raw_weights[code] = layer_budget * w

    # top_n：按动量保留前 top_n 只，权重重新归一
    if len(raw_weights) > top_n:
        momentum = {item["code"]: item["momentum"] for item in selected}
        keep = sorted(raw_weights, key=lambda c: momentum.get(c, 0.0), reverse=True)[:top_n]
        total = sum(raw_weights[c] for c in keep)
        raw_weights = {c: raw_weights[c] / total for c in keep} if total > 0 else {}
        warnings.append(f"入选数超过 top_n={top_n}，已按动量保留前 {top_n} 只")

    # 权重约束（单基金/家族/QDII）
    families = {item["code"]: item["family"] for item in selected}
    markets = {item["code"]: item["market"] for item in selected}
    weights = risk.apply_weight_caps(
        raw_weights, families, markets, max_fund, max_family, max_qdii
    )
    method = "+".join(sorted(set(methods))) if methods else "equal_weight"
    return weights, method, warnings


# ---------------------------------------------------------------------------
# 指标汇总与曲线抽样
# ---------------------------------------------------------------------------


def _summarize(values: list[float]) -> BacktestV2Summary:
    if len(values) < 2:
        return BacktestV2Summary()
    returns = _daily_returns(values)
    total_return = values[-1] - 1.0
    return BacktestV2Summary(
        total_return=total_return,
        annual_return=_annual_return(total_return, len(values) - 1),
        annual_volatility=_annual_volatility(returns),
        max_drawdown=_max_drawdown(values),
        sharpe=_sharpe(returns),
        win_rate=_win_rate(returns),
    )


def _sample_curve(
    calendar: list[date], strategy: list[float], benchmark: list[float]
) -> list[BacktestV2CurvePoint]:
    n = len(calendar)
    if n <= MAX_CURVE_POINTS:
        indices = range(n)
    else:
        step = n / MAX_CURVE_POINTS
        indices = sorted({int(i * step) for i in range(MAX_CURVE_POINTS)} | {n - 1})
    return [
        BacktestV2CurvePoint(
            date=calendar[i].isoformat(),
            strategy=round(strategy[i], 6),
            benchmark=round(benchmark[i], 6),
        )
        for i in indices
    ]


# ---------------------------------------------------------------------------
# 月频信号日（每月最后一个交易日）
# ---------------------------------------------------------------------------


def monthly_signal_indices(calendar: list[date], start_idx: int) -> list[int]:
    """从 start_idx 起，每月最后一个交易日的索引（严格递增）。

    最后一个自然月的最后交易日不产出信号（无后续成交日）。
    """
    result: list[int] = []
    for i in range(max(start_idx, 1), len(calendar)):
        if (calendar[i].year, calendar[i].month) != (calendar[i - 1].year, calendar[i - 1].month):
            result.append(i - 1)  # 上一月最后一个交易日
    return result


# ---------------------------------------------------------------------------
# 回测引擎（纯函数，不访问数据库）
# ---------------------------------------------------------------------------


def _turnover(
    target: dict[str, float], drift: dict[str, float]
) -> float:
    """换手率：Σ|目标-漂移|/2（现金作为一项纳入比较）。"""
    target_with_cash = dict(target)
    target_with_cash["__cash__"] = max(1.0 - sum(target.values()), 0.0)
    drift_with_cash = dict(drift)
    drift_with_cash["__cash__"] = max(1.0 - sum(drift.values()), 0.0)
    return 0.5 * sum(
        abs(target_with_cash.get(k, 0.0) - drift_with_cash.get(k, 0.0))
        for k in set(target_with_cash) | set(drift_with_cash)
    )


def run_backtest_panels(
    calendar: list[date],
    panels: dict[str, list[float]],
    names: dict[str, str],
    req: BacktestV2Request,
) -> tuple[
    list[float],
    list[float],
    list[RebalanceV2Detail],
    list[TradeV2],
    list[str],
]:
    """在对齐的净值面板上执行 V2 月频回测（不访问数据库）。

    返回 (策略净值, 基准净值, 调仓明细, 成交记录, 警告)。
    净值序列与 calendar[first_fill:] 对齐（首个成交日起），起点 1.0。
    """
    n = len(calendar)
    codes = list(panels)
    warnings: list[str] = []

    target_vol = req.target_vol if req.target_vol is not None else risk.DEFAULT_TARGET_VOL
    max_fund = (
        req.max_fund_weight if req.max_fund_weight is not None else risk.DEFAULT_MAX_FUND_WEIGHT
    )
    max_family = (
        req.max_family_weight
        if req.max_family_weight is not None
        else risk.DEFAULT_MAX_FAMILY_WEIGHT
    )
    max_qdii = (
        req.max_qdii_weight if req.max_qdii_weight is not None else risk.DEFAULT_MAX_QDII_WEIGHT
    )

    # 信号日：首个信号需在此前积累 253 个净值点（索引 252 起）
    first_signal = risk.MIN_MOMENTUM_SAMPLES - 1  # 索引 252
    signal_indices = monthly_signal_indices(calendar, first_signal)
    if not signal_indices:
        raise QuantError(
            f"共同交易日 {n} 天不足以构造月频调仓（需要至少 {risk.MIN_MOMENTUM_SAMPLES + 22} 天）"
        )
    if req.rebalance_interval_months > 1:
        signal_indices = signal_indices[:: req.rebalance_interval_months]

    markets_by_code = {code: risk.classify_market(names.get(code, code)) for code in codes}

    # ---- 预计算各信号日的决策（含成交日，无未来数据：打分只用信号日及之前）----
    # 组合日收益用模拟持仓权重逐日加权计算（EWMA 波动与冻结判定用）；
    # 决策在打分阶段全部固定，保证结果可复现。
    decisions: dict[int, dict] = {}  # fill_idx -> 决策
    sim_weights: dict[str, float] = {}  # 模拟持仓份额权重（每单位净值对应的份额×权重）
    sim_cash = 1.0
    portfolio_returns: list[float] = []  # 组合日收益（EWMA/冻结用）

    # 首次调仓前没有组合收益历史，使用候选池等权日收益作为风险估计代理；
    # 否则高波策略永远要到第二次调仓后才可能触发波动率目标。
    proxy_returns: list[float] = []
    for idx in range(1, min(first_signal + 1, n)):
        day_returns = [
            panels[code][idx] / panels[code][idx - 1] - 1.0
            for code in codes
            if panels[code][idx - 1] > 0
        ]
        if day_returns:
            proxy_returns.append(fmean(day_returns))
    portfolio_returns.extend(proxy_returns[-risk.EWMA_WINDOW :])

    prev_sig: int | None = None
    for sig_idx in signal_indices:
        # 逐日推进组合日收益（自上一信号日之后至本信号日，含权重漂移）；
        # 按漂移市值权重加权（现金零收益），与成交日模拟口径一致
        if prev_sig is not None and sim_weights:
            for idx in range(prev_sig + 1, sig_idx + 1):
                values = {
                    code: w * panels[code][idx] for code, w in sim_weights.items()
                }
                total = sum(values.values()) + sim_cash
                day_ret = 0.0
                if total > 0:
                    for code, value in values.items():
                        prev_nav = panels[code][idx - 1]
                        if prev_nav > 0:
                            day_ret += (value / total) * (panels[code][idx] / prev_nav - 1.0)
                portfolio_returns.append(day_ret)

        selected, sel_warnings = select_candidates(panels, names, sig_idx)
        target, method, tw_warnings = compute_target_weights(
            selected, panels, sig_idx, req.top_n, max_fund, max_family, max_qdii
        )
        has_qdii = any(risk.is_qdii(markets_by_code.get(code, "cn")) for code in target)
        fill_idx = sig_idx + (2 if has_qdii else 1)
        if fill_idx >= n:
            break  # 末尾信号无成交日，不再产出新决策

        realized_vol = risk.ewma_volatility(portfolio_returns)
        scalar = risk.vol_target_scalar(realized_vol, target_vol)
        if scalar < 1.0 and target:
            target = {code: round(w * scalar, 6) for code, w in target.items()}

        frozen, freeze_reason = risk.freeze_check(portfolio_returns, realized_vol)
        if frozen:
            decisions[fill_idx] = {
                "signal_idx": sig_idx,
                "target": None,  # 冻结：沿用当前持仓（成交日按漂移权重展示）
                "method": "frozen",
                "frozen": True,
                "reason": freeze_reason or "高波动+急反弹，冻结调仓",
                "realized_vol": realized_vol,
                "vol_scalar": 1.0,
                "warnings": sel_warnings + tw_warnings,
            }
        else:
            decisions[fill_idx] = {
                "signal_idx": sig_idx,
                "target": target,
                "method": method,
                "frozen": False,
                "reason": "QDII 持仓 T+2 成交" if has_qdii else "月频调仓 T+1 成交",
                "realized_vol": realized_vol,
                "vol_scalar": scalar,
                "warnings": sel_warnings + tw_warnings,
            }
            # 模拟持仓切换为新目标（按信号日净值折算份额权重，供下一期漂移）
            sim_weights = {
                code: w / panels[code][sig_idx]
                for code, w in target.items()
                if panels[code][sig_idx] > 0
            }
            sim_cash = max(1.0 - sum(target.values()), 0.0)
        prev_sig = sig_idx

    # ---- 日度模拟 ----
    first_fill = min(decisions) if decisions else None
    if first_fill is None:
        raise QuantError("无任何可执行的调仓信号（净值区间过短）")

    # 基准：全部候选等权买入持有（B0），锚定首个成交日（该日基准净值 = 1.0），
    # 与策略曲线起点一致，二者日期与长度逐一对齐、超额收益同日起算
    bench_base = {code: panels[code][first_fill] for code in codes}
    benchmark: list[float] = []
    for t in range(first_fill, n):
        benchmark.append(fmean(panels[code][t] / bench_base[code] for code in codes))

    strategy: list[float] = []
    rebalances: list[RebalanceV2Detail] = []
    trades: list[TradeV2] = []
    alloc: dict[str, float] = {}  # code -> 份额（按 1 元起点折算）
    cash = 1.0

    for t in range(first_fill, n):
        day = calendar[t]

        decision = decisions.get(t)
        if decision is not None:
            warnings.extend(decision["warnings"])
            total_value = sum(a * panels[c][t] for c, a in alloc.items()) + cash

            if decision["frozen"]:
                # 冻结：沿用当前持仓（展示漂移权重），无成交
                drift = (
                    {code: a * panels[code][t] / total_value for code, a in alloc.items()}
                    if total_value > 0
                    else {}
                )
                rebalances.append(
                    RebalanceV2Detail(
                        index=len(rebalances) + 1,
                        signal_date=calendar[decision["signal_idx"]].isoformat(),
                        fill_date=day.isoformat(),
                        holdings={code: round(w, 6) for code, w in drift.items()},
                        cash_weight=round(min(max(1.0 - sum(drift.values()), 0.0), 1.0), 6),
                        turnover=0.0,
                        frozen=True,
                        allocation_method="frozen",
                        realized_vol=decision["realized_vol"],
                        vol_scalar=1.0,
                        reason=decision["reason"],
                    )
                )
            else:
                target = decision["target"]
                # 漂移权重（按本日净值）
                drift = (
                    {code: a * panels[code][t] / total_value for code, a in alloc.items()}
                    if total_value > 0
                    else {}
                )
                turnover = _turnover(target, drift)
                settle_lag = t - decision["signal_idx"]

                new_alloc: dict[str, float] = {}
                for code, w in target.items():
                    price = panels[code][t]
                    if price > 0:
                        new_alloc[code] = (w * total_value) / price

                total_fees = 0.0
                for code in sorted(set(alloc) | set(new_alloc)):
                    old_value = alloc.get(code, 0.0) * panels[code][t]
                    new_value = new_alloc.get(code, 0.0) * panels[code][t]
                    diff = new_value - old_value
                    if abs(diff) < 1e-9:
                        continue
                    action = "buy" if diff > 0 else "sell"
                    amount = abs(diff)
                    fee = apply_fee(amount, action, req.fee_model)
                    total_fees += fee
                    if len(trades) < MAX_TRADES:
                        trades.append(
                            TradeV2(
                                signal_date=calendar[decision["signal_idx"]].isoformat(),
                                fill_date=day.isoformat(),
                                code=code,
                                name=names.get(code, code),
                                action=action,
                                weight_change=round(amount / total_value, 6)
                                if total_value > 0
                                else 0.0,
                                amount=round(amount, 2),
                                fee=round(fee, 6),
                                price=round(panels[code][t], 6),
                                settle_lag=settle_lag,
                                reason=decision["reason"],
                            )
                        )
                alloc = new_alloc
                # 非零费用模型下交易费用从现金中扣除；现金不允许为负
                # （不允许融资/卖空），费用超出现金部分视为损失在本日吸收
                cash = max(total_value * (1.0 - sum(target.values())) - total_fees, 0.0)
                rebalances.append(
                    RebalanceV2Detail(
                        index=len(rebalances) + 1,
                        signal_date=calendar[decision["signal_idx"]].isoformat(),
                        fill_date=day.isoformat(),
                        holdings=target,
                        cash_weight=round(min(max(1.0 - sum(target.values()), 0.0), 1.0), 6),
                        turnover=round(turnover, 6),
                        frozen=False,
                        allocation_method=decision["method"],
                        realized_vol=decision["realized_vol"],
                        vol_scalar=decision["vol_scalar"],
                        reason=decision["reason"],
                    )
                )

        # ---- 每日盯市 ----
        value = sum(a * panels[c][t] for c, a in alloc.items()) + cash
        strategy.append(value)

    return strategy, benchmark, rebalances, trades, warnings


# ---------------------------------------------------------------------------
# 入口：回测
# ---------------------------------------------------------------------------


def run_backtest_v2(db: Session, req: BacktestV2Request) -> BacktestV2Result:
    """稳健组合 V2 回测入口：装载对齐 → 月频回测 → 汇总输出。"""
    calendar, panels, codes, names, warnings = _load_aligned_panels(
        db, req.candidate_codes, req.start_date, req.end_date
    )

    strategy, benchmark, rebalances, trades, run_warnings = run_backtest_panels(
        calendar, panels, names, req
    )
    warnings.extend(run_warnings)

    strategy_summary = _summarize(strategy)
    benchmark_summary = _summarize(benchmark)
    excess = (
        strategy_summary.total_return - benchmark_summary.total_return
        if strategy_summary.total_return is not None
        and benchmark_summary.total_return is not None
        else None
    )

    curve_calendar = calendar[len(calendar) - len(strategy) :]
    total_fees = sum(t.fee for t in trades)
    turnover_values = [r.turnover for r in rebalances if not r.frozen]
    avg_turnover = fmean(turnover_values) if turnover_values else 0.0
    frozen_count = sum(1 for r in rebalances if r.frozen)

    params: dict = {
        "candidate_codes": codes,
        "top_n": req.top_n,
        "rebalance_interval_months": req.rebalance_interval_months,
        "target_vol": req.target_vol if req.target_vol is not None else risk.DEFAULT_TARGET_VOL,
        "max_fund_weight": req.max_fund_weight
        if req.max_fund_weight is not None
        else risk.DEFAULT_MAX_FUND_WEIGHT,
        "max_family_weight": req.max_family_weight
        if req.max_family_weight is not None
        else risk.DEFAULT_MAX_FAMILY_WEIGHT,
        "max_qdii_weight": req.max_qdii_weight
        if req.max_qdii_weight is not None
        else risk.DEFAULT_MAX_QDII_WEIGHT,
        "fee_model": req.fee_model.model_dump(),
    }

    return BacktestV2Result(
        params=params,
        start_date=curve_calendar[0].isoformat() if curve_calendar else "",
        end_date=curve_calendar[-1].isoformat() if curve_calendar else "",
        initial_capital=req.initial_capital,
        strategy=strategy_summary,
        benchmark=benchmark_summary,
        excess_return=excess,
        avg_turnover=round(avg_turnover, 6),
        rebalance_count=len([r for r in rebalances if not r.frozen]),
        frozen_count=frozen_count,
        total_fees=round(total_fees, 6),
        curve=_sample_curve(curve_calendar, strategy, benchmark),
        rebalances=rebalances,
        trades=trades,
        methodology=METHODOLOGY_V2,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 入口：当前信号（GET /api/quant/v2/signals）
# ---------------------------------------------------------------------------


def current_signals(db: Session, req: BacktestV2Request) -> SignalsV2Response:
    """基于最新净值计算当期目标信号。

    当前信号只需要每只基金自身满足 12-1 动量窗口，不应要求数百只基金拥有
    完全相同的 275 个交易日（新基金或 QDII 日期会把全体交集压缩）。各基金
    动量先按自身最新序列计算，配置所需相关性再使用可得尾部序列。
    """
    start = _parse_day(req.start_date)
    end = _parse_day(req.end_date)
    if start and end and start > end:
        raise QuantError("start_date 不能晚于 end_date")
    instruments, warnings = _load_candidates(db, req.candidate_codes)
    panels: dict[str, list[float]] = {}
    names: dict[str, str] = {}
    latest_dates: list[date] = []
    for instrument in instruments:
        series = _load_nav_series(db, instrument.id, start=start, end=end, limit=NAV_LOAD_LIMIT)
        if len(series) < MIN_SAMPLES:
            warnings.append(
                f"基金 {instrument.code}（{instrument.name}）净值样本不足 {MIN_SAMPLES} 条，已剔除"
            )
            continue
        panels[instrument.code] = [value for _day, value in series]
        names[instrument.code] = instrument.name
        latest_dates.append(series[-1][0])
    if len(panels) < 2:
        raise QuantError(
            f"有效候选基金不足 2 只（{len(panels)} 只满足 {MIN_SAMPLES} 个净值点），"
            "请先完成历史净值回填"
        )
    codes = [instrument.code for instrument in instruments if instrument.code in panels]
    t = min(len(values) for values in panels.values()) - 1
    # 尾部对齐只用于相关性/波动配置，动量计算仍覆盖至少 253 个最近样本。
    panels = {code: values[-(t + 1) :] for code, values in panels.items()}
    signal_date = max(latest_dates)

    selected, sel_warnings = select_candidates(panels, names, t)
    warnings.extend(sel_warnings)
    target, _method, tw_warnings = compute_target_weights(
        selected,
        panels,
        t,
        req.top_n,
        req.max_fund_weight if req.max_fund_weight is not None else risk.DEFAULT_MAX_FUND_WEIGHT,
        req.max_family_weight
        if req.max_family_weight is not None
        else risk.DEFAULT_MAX_FAMILY_WEIGHT,
        req.max_qdii_weight if req.max_qdii_weight is not None else risk.DEFAULT_MAX_QDII_WEIGHT,
    )
    warnings.extend(tw_warnings)

    # 组合 EWMA60：用全部候选等权日收益近似（无持仓历史时的组合口径）
    eq_returns: list[float] = []
    for i in range(1, t + 1):
        day_returns = [
            panels[code][i] / panels[code][i - 1] - 1.0
            for code in panels
            if panels[code][i - 1] > 0
        ]
        if day_returns:
            eq_returns.append(fmean(day_returns))
    realized_vol = risk.ewma_volatility(eq_returns)
    target_vol = req.target_vol if req.target_vol is not None else risk.DEFAULT_TARGET_VOL
    scalar = risk.vol_target_scalar(realized_vol, target_vol)
    if scalar < 1.0 and target:
        target = {code: round(w * scalar, 6) for code, w in target.items()}

    frozen, freeze_reason = risk.freeze_check(eq_returns, realized_vol)

    by_code = {item["code"]: item for item in selected}
    selected_items: list[SignalV2Item] = []
    for code, weight in sorted(target.items(), key=lambda kv: kv[1], reverse=True):
        item = by_code.get(code)
        if item is None:
            continue
        selected_items.append(
            SignalV2Item(
                code=code,
                name=item["name"],
                market=item["market"],
                family=item["family"],
                momentum_12_1=round(item["momentum"], 6),
                rank_in_market=item.get("rank", 0),
                market_candidates=item.get("market_candidates", 0),
                weight=weight,
                reasons=[
                    f"绝对动量 12-1 = {item['momentum']:.1%} > 0",
                    f"{risk.market_label(item['market'])}层内动量第 {item.get('rank', '-')} 名"
                    f"（{item.get('market_candidates', 0)} 只候选的前 30%）",
                    f"同家族「{item['family']}」份额中动量最高",
                ],
            )
        )

    # 预计成交日：当前历史序列通常止于信号日，未来净值日尚未入库；
    # 用日历日给出预计 T+1/T+2，实际成交任务仍以未来可得净值日为准。
    from datetime import timedelta

    has_qdii = any(
        risk.is_qdii(risk.classify_market(names.get(code, code))) for code in target
    )
    trade_date = (signal_date + timedelta(days=2 if has_qdii else 1)).isoformat()

    return SignalsV2Response(
        as_of=signal_date.isoformat(),
        trade_date=trade_date,
        methodology=METHODOLOGY_V2,
        candidate_count=len(codes),
        eligible_count=len(selected),
        selected=selected_items,
        cash_weight=round(1.0 - sum(target.values()), 6),
        realized_vol=realized_vol,
        vol_scalar=scalar,
        frozen=frozen,
        freeze_reason=freeze_reason,
        warnings=warnings,
    )
