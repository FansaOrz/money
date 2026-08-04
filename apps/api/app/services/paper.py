"""模拟交易（Paper Trading）核心服务。

每日 run_paper_cycle 流程（全部为模拟成交，不产生任何真实下单）：
1. 调用规则模型筛选器（quant_screener.run_screener）取全候选信号；
2. 到调仓日（每 rebalance_interval 个交易日一次，首次运行为建仓日）时
   固化信号快照（SignalSnapshot），并按 top10 target_weight 以当日净值
   虚拟成交：先按市值权重全卖出，再按目标权重买入，卖出可回收现金，
   不足现金按目标权重比例缩减；双边简化费用 0.1%（买卖各收一次）；
3. 按当日 FundNav（优先累计净值）估值，落 PaperNavDaily / PaperHoldingDaily；
4. 生成候选池等权基准：首个估值日起按共同交易日逐日等权收益累计；
5. 幂等：同一账户同一日期重复运行直接返回已有结果，不重复成交与估值。

仅使用标准库 + SQLAlchemy；数据源为 FundNav 与 quant_screener。
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    BacktestRun,
    FundNav,
    Instrument,
    PaperAccount,
    PaperHoldingDaily,
    PaperNavDaily,
    PaperPosition,
    PaperTrade,
    SignalSnapshot,
    StrategyVersion,
)
from app.models.paper import (
    DEFAULT_FEE_RATE,
    DEFAULT_INITIAL_CAPITAL,
    DEFAULT_REBALANCE_INTERVAL,
    DEFAULT_TOP_N,
)
from app.schemas.paper import (
    PaperHoldingDayItem,
    PaperHistoryResponse,
    PaperNavPoint,
    PaperPositionItem,
    PaperPositionsResponse,
    PaperRunResponse,
    PaperSignalSnapshotItem,
    PaperSignalsResponse,
    PaperStrategyInfo,
    PaperSummary,
    PaperTradeItem,
    PaperTradesResponse,
)
from app.schemas.quant import ScreenerItem, ScreenerRequest
from app.services import quant_screener as screener_service
from app.services.quant import (
    QuantError,
    _annual_return,
    _daily_returns,
    _max_drawdown,
    _parse_day,
    _sharpe,
    _win_rate,
)

LEGACY_STRATEGY_NAME = "规则模型V1-月调仓"
DEFAULT_STRATEGY_NAME = "规则模型V2-前向模拟"
DEFAULT_ACCOUNT_NAME = "默认模拟账户"
DEFAULT_STRATEGY_PARAMS = {
    "model_version": "v2",
    "purpose": "forward_paper_validation",
    "signal_engine": "quant_screener",
    "methodology": "使用修复净值与指数窗口后的当前量化规则，只从当时可见的数据向前验证",
    "historical_validation": "not_passed",
}

# 金额/份额落库精度
_CENT = Decimal("0.01")
_SHARE_Q = Decimal("0.000001")
_WEIGHT_Q = Decimal("0.000001")


class PaperError(ValueError):
    """模拟交易参数或数据不足错误，路由层转换为 400。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _q2(value: Decimal | float) -> Decimal:
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _q6(value: Decimal | float) -> Decimal:
    return Decimal(str(value)).quantize(_SHARE_Q, rounding=ROUND_HALF_UP)


def _qw(value: Decimal | float) -> Decimal:
    return Decimal(str(value)).quantize(_WEIGHT_Q, rounding=ROUND_HALF_UP)


def _f(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


# ---------------------------------------------------------------------------
# 默认策略版本与账户
# ---------------------------------------------------------------------------


def ensure_default_account(db: Session) -> PaperAccount:
    """获取（或创建）V2 前向模拟账户，不复用已失效 V1 的持仓和净值。"""
    version = db.execute(
        select(StrategyVersion).where(StrategyVersion.name == DEFAULT_STRATEGY_NAME)
    ).scalars().first()
    if version is None:
        version = StrategyVersion(
            name=DEFAULT_STRATEGY_NAME,
            initial_capital=DEFAULT_INITIAL_CAPITAL,
            rebalance_interval=DEFAULT_REBALANCE_INTERVAL,
            fee_rate=DEFAULT_FEE_RATE,
            top_n=DEFAULT_TOP_N,
            params=dict(DEFAULT_STRATEGY_PARAMS),
            status="paper_testing",
        )
        db.add(version)
        db.flush()

    if version.status.startswith(("paused", "superseded", "invalid")):
        reason = (
            version.params.get("superseded_reason")
            or version.params.get("audit")
            or version.status
        )
        raise PaperError(f"模拟策略已暂停：{reason}")

    account = db.execute(
        select(PaperAccount).where(
            PaperAccount.strategy_version_id == version.id,
            PaperAccount.name == DEFAULT_ACCOUNT_NAME,
        )
    ).scalars().first()
    if account is None:
        account = PaperAccount(
            strategy_version_id=version.id,
            name=DEFAULT_ACCOUNT_NAME,
            initial_capital=DEFAULT_INITIAL_CAPITAL,
            cash=DEFAULT_INITIAL_CAPITAL,
        )
        db.add(account)
        db.flush()
    return account


def get_default_account(db: Session) -> PaperAccount:
    """读取默认模拟账户；不存在时报错（由路由转换为 404/400）。"""
    account = db.execute(
        select(PaperAccount)
        .join(StrategyVersion, StrategyVersion.id == PaperAccount.strategy_version_id)
        .where(
            StrategyVersion.name == DEFAULT_STRATEGY_NAME,
            PaperAccount.name == DEFAULT_ACCOUNT_NAME,
        )
    ).scalars().first()
    if account is None:
        raise PaperError("默认模拟账户不存在，请先调用 /paper/run 初始化并运行")
    return account


# ---------------------------------------------------------------------------
# 净值装载
# ---------------------------------------------------------------------------


def _load_nav_panels(
    db: Session, instrument_ids: list[int], up_to: date | None
) -> dict[int, dict[date, Decimal]]:
    """装载多只基金截至 up_to 的净值面板：{instrument_id: {日期: 净值}}。

    优先累计净值（含分红），缺失回退单位净值；非正净值剔除。
    """
    if not instrument_ids:
        return {}
    stmt = (
        select(
            FundNav.instrument_id,
            FundNav.nav_date,
            FundNav.accumulated_nav,
            FundNav.unit_nav,
        )
        .where(FundNav.instrument_id.in_(instrument_ids))
        .order_by(FundNav.nav_date)
    )
    panels: dict[int, dict[date, Decimal]] = {}
    for instrument_id, nav_date, accumulated_nav, unit_nav in db.execute(stmt).all():
        value = accumulated_nav if accumulated_nav is not None else unit_nav
        if value is None or Decimal(value) <= 0:
            continue
        if up_to is not None and nav_date > up_to:
            continue
        panels.setdefault(instrument_id, {})[nav_date] = Decimal(value)
    return panels


def _latest_nav(panel: dict[date, Decimal], on_or_before: date | None = None) -> tuple[date, Decimal] | None:
    """取面板中不晚于 on_or_before 的最新 (日期, 净值)。"""
    candidates = [
        (d, v) for d, v in panel.items() if on_or_before is None or d <= on_or_before
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])


# ---------------------------------------------------------------------------
# 候选池等权基准
# ---------------------------------------------------------------------------


def _benchmark_series(
    calendar: list[date],
    panels: dict[int, dict[date, Decimal]],
    start: date,
    up_to: date,
) -> list[tuple[date, Decimal]]:
    """候选池等权基准：从 start 起按共同交易日逐日等权收益累计（起点 1.0）。

    某天某基金无净值时，该基金当日收益按 0 处理（与其余候选取均值）。
    """
    days = [d for d in calendar if start <= d <= up_to]
    if not days or not panels:
        return []
    series: list[tuple[date, Decimal]] = [(days[0], Decimal("1"))]
    for prev_day, day in zip(days, days[1:]):
        returns: list[Decimal] = []
        for panel in panels.values():
            prev = panel.get(prev_day)
            cur = panel.get(day)
            if prev is not None and cur is not None and prev > 0:
                returns.append(cur / prev - 1)
        avg = sum(returns) / len(returns) if returns else Decimal("0")
        series.append((day, series[-1][1] * (1 + avg)))
    return series


def _common_calendar(panels: dict[int, dict[date, Decimal]]) -> list[date]:
    """候选基金共同交易日交集（升序）。"""
    common: set[date] | None = None
    for panel in panels.values():
        days = set(panel)
        common = days if common is None else (common & days)
    return sorted(common or [])


# ---------------------------------------------------------------------------
# 虚拟调仓
# ---------------------------------------------------------------------------


def _execute_rebalance(
    db: Session,
    account: PaperAccount,
    run: BacktestRun,
    version: StrategyVersion,
    targets: list[ScreenerItem],
    panels: dict[int, dict[date, Decimal]],
    run_date: date,
) -> tuple[Decimal, int, list[str]]:
    """按 top10 target_weight 以当日净值虚拟成交。

    先按当前市值权重全卖出（回收现金），再按目标权重买入；
    卖出可回收现金，不足现金按目标权重比例缩减；双边费用各收 fee_rate。
    返回 (费用合计, 成交笔数, 警告)。
    """
    warnings: list[str] = []
    fee_rate = Decimal(version.fee_rate)
    fee_total = Decimal("0")
    trade_count = 0

    positions = db.execute(
        select(PaperPosition).where(PaperPosition.account_id == account.id)
    ).scalars().all()
    pos_by_instrument = {p.instrument_id: p for p in positions}
    target_instruments = {
        i.code: i
        for i in db.execute(
            select(Instrument).where(Instrument.code.in_([t.code for t in targets]))
        ).scalars().all()
    }

    # ---- 估值当前持仓（当日净值，缺失回退最近可得）----
    holding_ids = [p.instrument_id for p in positions if p.shares > 0]
    code_by_instrument = {
        i.id: i.code
        for i in db.execute(
            select(Instrument).where(Instrument.id.in_(holding_ids))
        ).scalars().all()
    } if holding_ids else {}
    holdings_value: dict[int, Decimal] = {}
    for position in positions:
        if position.shares <= 0:
            continue
        panel = panels.get(position.instrument_id, {})
        latest = _latest_nav(panel, run_date)
        if latest is None:
            code = code_by_instrument.get(position.instrument_id, str(position.instrument_id))
            warnings.append(f"持仓基金 {code} 无可用净值，按成本估值")
            value = Decimal(position.cost)
        else:
            value = Decimal(position.shares) * latest[1]
        holdings_value[position.instrument_id] = value

    invested = sum(holdings_value.values(), Decimal("0"))
    total_equity = Decimal(account.cash) + invested

    # ---- 第一步：全卖出当前持仓（回收现金，卖出费用从回款中扣除）----
    cash = Decimal(account.cash)
    new_shares: dict[int, Decimal] = {}
    new_cost: dict[int, Decimal] = {}

    for instrument_id, position in pos_by_instrument.items():
        if position.shares <= 0:
            continue
        panel = panels.get(instrument_id, {})
        latest = _latest_nav(panel, run_date)
        price = latest[1] if latest else None
        gross = holdings_value.get(instrument_id, Decimal(position.cost))
        fee = _q2(gross * fee_rate)
        cash += gross - fee
        fee_total += fee
        trade_count += 1
        db.add(
            PaperTrade(
                account_id=account.id,
                run_id=run.id,
                instrument_id=instrument_id,
                trade_date=run_date,
                side="sell",
                shares=position.shares,
                price=price if price is not None else Decimal("0"),
                amount=_q2(gross),
                fee=fee,
                target_weight=Decimal("0"),
            )
        )

    # ---- 第二步：按目标权重买入（费用从现金扣除，不足按比例缩减）----
    weight_sum = sum(Decimal(str(t.target_weight)) for t in targets)
    if targets and weight_sum > 0:
        # 预估买入总额：现金需覆盖 买入额×(1+fee)，按目标权重占总权重的比例分配
        budget = cash / (1 + fee_rate) if fee_rate > 0 else cash
        for item in targets:
            instrument = target_instruments.get(item.code)
            if instrument is None:
                warnings.append(f"目标基金 {item.code} 未找到标的记录，已跳过")
                continue
            panel = panels.get(instrument.id, {})
            latest = _latest_nav(panel, run_date)
            if latest is None or latest[1] <= 0:
                warnings.append(f"目标基金 {item.code}（{item.name}）当日无可用净值，已跳过买入")
                continue
            price = latest[1]
            weight = Decimal(str(item.target_weight))
            target_amount = total_equity * weight
            # 受可用预算约束：理论上权重合计 ≤1，预算充足；现金不足时按比例缩减
            amount = min(target_amount, budget * (weight / weight_sum))
            if amount <= 0:
                continue
            amount = _q2(amount)
            fee = _q2(amount * fee_rate)
            if amount + fee > cash:
                amount = _q2(cash / (1 + fee_rate))
                fee = _q2(amount * fee_rate)
            if amount <= 0:
                continue
            shares = _q6(amount / price)
            if shares <= 0:
                continue
            cash -= amount + fee
            fee_total += fee
            trade_count += 1
            new_shares[instrument.id] = new_shares.get(instrument.id, Decimal("0")) + shares
            new_cost[instrument.id] = (
                new_cost.get(instrument.id, Decimal("0")) + amount + fee
            )
            db.add(
                PaperTrade(
                    account_id=account.id,
                    run_id=run.id,
                    instrument_id=instrument.id,
                    trade_date=run_date,
                    side="buy",
                    shares=shares,
                    price=price,
                    amount=amount,
                    fee=fee,
                    target_weight=_qw(weight),
                )
            )

    # ---- 更新持仓与现金 ----
    for instrument_id, position in pos_by_instrument.items():
        if instrument_id in new_shares:
            position.shares = new_shares[instrument_id]
            position.cost = _q2(new_cost[instrument_id])
        else:
            position.shares = Decimal("0")
            position.cost = Decimal("0")
    for instrument_id, shares in new_shares.items():
        if instrument_id not in pos_by_instrument:
            db.add(
                PaperPosition(
                    account_id=account.id,
                    instrument_id=instrument_id,
                    shares=shares,
                    cost=_q2(new_cost[instrument_id]),
                )
            )
    account.cash = _q2(cash)
    db.flush()
    return _q2(fee_total), trade_count, warnings


# ---------------------------------------------------------------------------
# 每日循环入口
# ---------------------------------------------------------------------------


def run_paper_cycle(
    db: Session, run_date: date | None = None
) -> PaperRunResponse:
    """每日模拟交易循环：信号 → 调仓（到期时）→ 估值 → 基准，幂等。

    run_date 缺省时，以筛选器实际使用的数据日期作为记账日。这样在周末、休市
    或基金净值尚未更新时不会把同一份数据重复记成新的“交易日”。显式传入
    run_date 主要供可重复的研究测试使用。
    """
    today = run_date or date.today()
    account = ensure_default_account(db)
    version = db.get(StrategyVersion, account.strategy_version_id)
    if version is None:  # pragma: no cover - 外键保证存在
        raise PaperError("账户绑定的策略版本不存在")

    warnings: list[str] = []
    screener = None

    # 显式研究日期可先命中幂等；日常前向运行需先知道筛选器真实的数据日期。
    if run_date is not None:
        existing = db.execute(
            select(BacktestRun).where(
                BacktestRun.account_id == account.id, BacktestRun.run_date == today
            )
        ).scalars().first()
        if existing is not None:
            nav_row = db.execute(
                select(PaperNavDaily).where(
                    PaperNavDaily.account_id == account.id,
                    PaperNavDaily.nav_date == today,
                )
            ).scalars().first()
            db.commit()
            return _run_response(account, existing, nav_row, skipped=True, warnings=[])

    # ---- 1. 全候选信号（screener：默认当前真实持仓为候选池）----
    try:
        screener = screener_service.run_screener(db, ScreenerRequest())
    except QuantError as exc:
        db.rollback()
        raise PaperError(f"筛选器无法生成候选信号：{exc}") from exc
    warnings.extend(screener.warnings)

    if run_date is None:
        try:
            data_date = _parse_day(screener.as_of)
        except QuantError as exc:
            db.rollback()
            raise PaperError(f"筛选器未返回有效的数据日期：{exc}") from exc
        if data_date is None:
            db.rollback()
            raise PaperError("筛选器未返回数据日期，无法安全地推进前向模拟")
        today = data_date

        existing = db.execute(
            select(BacktestRun).where(
                BacktestRun.account_id == account.id, BacktestRun.run_date == today
            )
        ).scalars().first()
        if existing is not None:
            nav_row = db.execute(
                select(PaperNavDaily).where(
                    PaperNavDaily.account_id == account.id,
                    PaperNavDaily.nav_date == today,
                )
            ).scalars().first()
            stale_warning = (
                f"基金净值最新仍是 {today.isoformat()}，本次未重复记账，"
                "模拟交易日和调仓倒计时均未增加"
            )
            db.commit()
            return _run_response(
                account,
                existing,
                nav_row,
                skipped=True,
                warnings=[stale_warning],
            )

    # ---- 2. 运行序号与是否调仓日 ----
    run_count = db.execute(
        select(func.count(BacktestRun.id)).where(BacktestRun.account_id == account.id)
    ).scalar_one()
    trading_day_index = int(run_count) + 1
    is_rebalance_day = (trading_day_index - 1) % version.rebalance_interval == 0

    # ---- 3. 估值用净值面板（持仓 + 全部候选）----
    positions = db.execute(
        select(PaperPosition).where(PaperPosition.account_id == account.id)
    ).scalars().all()
    holding_ids = [p.instrument_id for p in positions if p.shares > 0]

    candidate_codes = [item.code for item in screener.items]
    candidate_instruments = {
        i.code: i
        for i in db.execute(
            select(Instrument).where(Instrument.code.in_(candidate_codes))
        ).scalars().all()
    } if candidate_codes else {}
    candidate_ids = [i.id for i in candidate_instruments.values()]

    panels = _load_nav_panels(db, sorted(set(holding_ids) | set(candidate_ids)), today)
    all_ids = sorted(set(holding_ids) | set(candidate_ids))
    code_by_instrument = {
        i.id: i.code
        for i in db.execute(select(Instrument).where(Instrument.id.in_(all_ids))).scalars().all()
    } if all_ids else {}

    # ---- 4. 创建运行记录 ----
    run = BacktestRun(
        account_id=account.id,
        run_date=today,
        trading_day_index=trading_day_index,
        rebalanced=False,
        trade_count=0,
    )
    db.add(run)
    db.flush()

    # ---- 5. 调仓日：固化信号快照 + 虚拟成交 ----
    fee_total = Decimal("0")
    trade_count = 0
    if is_rebalance_day:
        db.add(
            SignalSnapshot(
                account_id=account.id,
                run_id=run.id,
                signal_date=today,
                as_of=screener.as_of,
                methodology=screener.methodology,
                candidate_count=screener.candidate_count,
                excluded_count=screener.excluded_count,
                observe_count=screener.observe_count,
                selected_count=screener.selected_count,
                items=[item.model_dump(mode="json") for item in screener.items],
                warnings=list(screener.warnings),
            )
        )
        targets = [
            item
            for item in screener.items
            if item.target_weight and item.target_weight > 0
        ][: version.top_n]
        if targets:
            fee_total, trade_count, rebalance_warnings = _execute_rebalance(
                db, account, run, version, targets, panels, today
            )
            warnings.extend(rebalance_warnings)
        else:
            warnings.append("调仓日无有效入选基金，本期持有现金")
        run.rebalanced = True
        run.trade_count = trade_count
        db.flush()

    # ---- 6. 当日估值 ----
    positions = db.execute(
        select(PaperPosition).where(PaperPosition.account_id == account.id)
    ).scalars().all()
    holding_rows: list[tuple[PaperPosition, Decimal, Decimal]] = []
    market_value = Decimal("0")
    for position in positions:
        if position.shares <= 0:
            continue
        panel = panels.get(position.instrument_id, {})
        latest = _latest_nav(panel, today)
        if latest is None:
            code = code_by_instrument.get(position.instrument_id, str(position.instrument_id))
            warnings.append(f"持仓基金 {code} 无可用净值，当日按成本估值")
            nav_value = (
                Decimal(position.cost) / Decimal(position.shares)
                if position.shares > 0
                else Decimal("0")
            )
            value = Decimal(position.cost)
        else:
            nav_value = latest[1]
            value = Decimal(position.shares) * nav_value
        market_value += value
        holding_rows.append((position, nav_value, _q2(value)))

    market_value = _q2(market_value)
    total_value = _q2(Decimal(account.cash) + market_value)
    initial = Decimal(account.initial_capital)
    nav = total_value / initial if initial > 0 else Decimal("1")

    # 权重需要总市值，先算总量再落持仓快照
    for position, nav_value, value in holding_rows:
        weight = (value / total_value) if total_value > 0 else Decimal("0")
        db.add(
            PaperHoldingDaily(
                account_id=account.id,
                instrument_id=position.instrument_id,
                holding_date=today,
                shares=position.shares,
                nav=_q6(nav_value),
                market_value=value,
                weight=_qw(weight),
            )
        )

    # 日收益：相对上一个估值日
    prev_nav_row = db.execute(
        select(PaperNavDaily)
        .where(
            PaperNavDaily.account_id == account.id,
            PaperNavDaily.nav_date < today,
        )
        .order_by(PaperNavDaily.nav_date.desc())
        .limit(1)
    ).scalars().first()
    daily_return: Decimal | None = None
    if prev_nav_row is not None and Decimal(prev_nav_row.nav) > 0:
        daily_return = nav / Decimal(prev_nav_row.nav) - 1

    # ---- 7. 候选池等权基准 ----
    candidate_panels = {
        instrument_id: panel
        for instrument_id, panel in panels.items()
        if instrument_id in set(candidate_ids)
    }
    calendar = _common_calendar(candidate_panels)
    benchmark_nav: Decimal | None = None
    benchmark_daily: Decimal | None = None
    if calendar:
        if prev_nav_row is not None:
            anchor = prev_nav_row.nav_date
            prev_benchmark = prev_nav_row.benchmark_nav
            benchmark_base = Decimal(prev_benchmark) if prev_benchmark is not None else None
        else:
            # 首个估值日：从当日或之前最近的共同交易日起算（起点 1.0）
            anchor = max((d for d in calendar if d <= today), default=None)
            benchmark_base = None
        if anchor is not None:
            series = _benchmark_series(calendar, candidate_panels, anchor, today)
            if series:
                benchmark_nav = series[-1][1].quantize(_SHARE_Q, rounding=ROUND_HALF_UP)
                if benchmark_base is not None:
                    benchmark_nav = (benchmark_base * series[-1][1]).quantize(
                        _SHARE_Q, rounding=ROUND_HALF_UP
                    )
        if benchmark_nav is not None and benchmark_base is not None and benchmark_base > 0:
            benchmark_daily = benchmark_nav / benchmark_base - 1
        if benchmark_nav is not None:
            prev_benchmark = db.execute(
                select(PaperNavDaily.benchmark_nav)
                .where(
                    PaperNavDaily.account_id == account.id,
                    PaperNavDaily.nav_date < today,
                    PaperNavDaily.benchmark_nav.is_not(None),
                )
                .order_by(PaperNavDaily.nav_date.desc())
                .limit(1)
            ).scalars().first()
            if prev_benchmark is not None and Decimal(prev_benchmark) > 0:
                benchmark_daily = benchmark_nav / Decimal(prev_benchmark) - 1

    db.add(
        PaperNavDaily(
            account_id=account.id,
            run_id=run.id,
            nav_date=today,
            cash=account.cash,
            market_value=market_value,
            total_value=total_value,
            nav=_q6(nav),
            daily_return=_qw(daily_return) if daily_return is not None else None,
            cumulative_return=_qw(nav - 1),
            benchmark_nav=benchmark_nav,
            benchmark_daily_return=(
                _qw(benchmark_daily) if benchmark_daily is not None else None
            ),
            fee_total=_q2(fee_total),
            rebalanced=is_rebalance_day,
        )
    )

    db.commit()
    nav_row = db.execute(
        select(PaperNavDaily).where(
            PaperNavDaily.account_id == account.id,
            PaperNavDaily.nav_date == today,
        )
    ).scalars().first()
    return _run_response(account, run, nav_row, skipped=False, warnings=warnings)


def _run_response(
    account: PaperAccount,
    run: BacktestRun,
    nav_row: PaperNavDaily | None,
    skipped: bool,
    warnings: list[str],
) -> PaperRunResponse:
    """组装 run 响应（nav_row 可能因历史数据缺失为空）。"""
    total_value = Decimal(nav_row.total_value) if nav_row else Decimal(account.cash)
    initial = Decimal(account.initial_capital)
    nav = float(nav_row.nav) if nav_row else float(total_value / initial if initial > 0 else 1)
    return PaperRunResponse(
        account_id=account.id,
        run_date=run.run_date.isoformat(),
        trading_day_index=run.trading_day_index,
        rebalanced=bool(run.rebalanced),
        trade_count=run.trade_count,
        fee_total=Decimal(nav_row.fee_total) if nav_row else Decimal("0"),
        total_value=total_value,
        nav=nav,
        daily_return=_f(nav_row.daily_return) if nav_row else None,
        cumulative_return=_f(nav_row.cumulative_return) if nav_row else None,
        benchmark_nav=_f(nav_row.benchmark_nav) if nav_row else None,
        skipped=skipped,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 查询：摘要 / 历史 / 持仓 / 成交 / 信号
# ---------------------------------------------------------------------------


def get_summary(db: Session) -> PaperSummary:
    """模拟账户摘要：最新估值 + 全区间汇总指标。"""
    account = get_default_account(db)
    version = db.get(StrategyVersion, account.strategy_version_id)

    nav_rows = db.execute(
        select(PaperNavDaily)
        .where(PaperNavDaily.account_id == account.id)
        .order_by(PaperNavDaily.nav_date)
    ).scalars().all()

    latest = nav_rows[-1] if nav_rows else None
    nav_values = [float(row.nav) for row in nav_rows]
    summary_fields: dict[str, float | None] = {
        "total_return": None,
        "annual_return": None,
        "max_drawdown": None,
        "sharpe": None,
        "win_rate": None,
    }
    if len(nav_values) >= 2:
        returns = _daily_returns(nav_values)
        total_return = nav_values[-1] - 1.0
        summary_fields = {
            "total_return": total_return,
            "annual_return": _annual_return(total_return, len(nav_values) - 1),
            "max_drawdown": _max_drawdown(nav_values),
            "sharpe": _sharpe(returns),
            "win_rate": _win_rate(returns),
        }
    elif nav_values:
        summary_fields["total_return"] = nav_values[-1] - 1.0

    position_count = db.execute(
        select(func.count(PaperPosition.id)).where(
            PaperPosition.account_id == account.id, PaperPosition.shares > 0
        )
    ).scalar_one()
    trade_count = db.execute(
        select(func.count(PaperTrade.id)).where(PaperTrade.account_id == account.id)
    ).scalar_one()
    rebalance_count = db.execute(
        select(func.count(BacktestRun.id)).where(
            BacktestRun.account_id == account.id, BacktestRun.rebalanced.is_(True)
        )
    ).scalar_one()
    total_fees = db.execute(
        select(func.coalesce(func.sum(PaperTrade.fee), 0)).where(
            PaperTrade.account_id == account.id
        )
    ).scalar_one()

    cumulative_return = summary_fields["total_return"]
    benchmark_nav = _f(latest.benchmark_nav) if latest else None
    benchmark_return = (benchmark_nav - 1.0) if benchmark_nav is not None else None
    excess = (
        cumulative_return - benchmark_return
        if cumulative_return is not None and benchmark_return is not None
        else None
    )

    next_rebalance_in: int | None = None
    if nav_rows and version is not None:
        last_index = db.execute(
            select(func.max(BacktestRun.trading_day_index)).where(
                BacktestRun.account_id == account.id
            )
        ).scalar_one()
        if last_index is not None:
            interval = version.rebalance_interval
            next_rebalance_in = (interval - (int(last_index) - 1) % interval) % interval
            if next_rebalance_in == 0:
                next_rebalance_in = interval

    cash = Decimal(account.cash)
    market_value = Decimal(latest.market_value) if latest else Decimal("0")
    total_value = Decimal(latest.total_value) if latest else cash
    warnings: list[str] = []
    if version is not None and version.status == "paper_testing":
        warnings.append(
            "这套规则正在做前向模拟验证，历史样本外验证没有通过；"
            "当前结果只能用来观察规则是否有效，不能当成已经验证成功的策略。"
        )
    if len(nav_rows) < 20:
        warnings.append(
            f"目前只有 {len(nav_rows)} 个有效净值交易日，样本太少，"
            "暂时不要根据收益高低判断规则好坏。"
        )

    return PaperSummary(
        account_id=account.id,
        account_name=account.name,
        strategy=PaperStrategyInfo(
            version_id=version.id if version else 0,
            name=version.name if version else "",
            status=version.status if version else "unknown",
            initial_capital=Decimal(account.initial_capital),
            rebalance_interval=version.rebalance_interval if version else 0,
            fee_rate=float(version.fee_rate) if version else 0.0,
            top_n=version.top_n if version else 0,
        ),
        currency=account.currency,
        cash=cash,
        market_value=market_value,
        total_value=total_value,
        nav=float(latest.nav) if latest else None,
        cumulative_return=cumulative_return,
        total_return=summary_fields["total_return"],
        annual_return=summary_fields["annual_return"],
        max_drawdown=summary_fields["max_drawdown"],
        sharpe=summary_fields["sharpe"],
        win_rate=summary_fields["win_rate"],
        benchmark_nav=benchmark_nav,
        benchmark_return=benchmark_return,
        excess_return=excess,
        position_count=int(position_count),
        trade_count=int(trade_count),
        rebalance_count=int(rebalance_count),
        total_fees=Decimal(total_fees),
        start_date=nav_rows[0].nav_date.isoformat() if nav_rows else None,
        last_run_date=latest.nav_date.isoformat() if latest else None,
        next_rebalance_in=next_rebalance_in,
        warnings=warnings,
    )


def get_history(
    db: Session,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 500,
) -> PaperHistoryResponse:
    """净值历史（日期升序，可选区间过滤）。"""
    account = get_default_account(db)
    start = _parse_day(start_date)
    end = _parse_day(end_date)
    if start and end and start > end:
        raise PaperError("start_date 不能晚于 end_date")

    stmt = (
        select(PaperNavDaily)
        .where(PaperNavDaily.account_id == account.id)
        .order_by(PaperNavDaily.nav_date)
    )
    rows_ = db.execute(stmt).scalars().all()
    items = [
        PaperNavPoint(
            date=row.nav_date.isoformat(),
            cash=Decimal(row.cash),
            market_value=Decimal(row.market_value),
            total_value=Decimal(row.total_value),
            nav=float(row.nav),
            daily_return=_f(row.daily_return),
            cumulative_return=_f(row.cumulative_return),
            benchmark_nav=_f(row.benchmark_nav),
            benchmark_daily_return=_f(row.benchmark_daily_return),
            fee_total=Decimal(row.fee_total),
            rebalanced=bool(row.rebalanced),
        )
        for row in rows_
        if (start is None or row.nav_date >= start) and (end is None or row.nav_date <= end)
    ]
    if len(items) > limit:
        items = items[-limit:]
    return PaperHistoryResponse(
        account_id=account.id,
        start_date=items[0].date if items else None,
        end_date=items[-1].date if items else None,
        count=len(items),
        items=items,
    )


def get_positions(db: Session) -> PaperPositionsResponse:
    """当前持仓：按最新估值日的持仓快照（含成本与浮动盈亏）。"""
    account = get_default_account(db)
    positions = db.execute(
        select(PaperPosition)
        .where(PaperPosition.account_id == account.id, PaperPosition.shares > 0)
        .order_by(PaperPosition.instrument_id)
    ).scalars().all()

    latest_nav_row = db.execute(
        select(PaperNavDaily)
        .where(PaperNavDaily.account_id == account.id)
        .order_by(PaperNavDaily.nav_date.desc())
        .limit(1)
    ).scalars().first()
    as_of = latest_nav_row.nav_date if latest_nav_row else None
    total_value = (
        Decimal(latest_nav_row.total_value) if latest_nav_row else Decimal(account.cash)
    )

    snapshot: dict[int, PaperHoldingDaily] = {}
    if as_of is not None:
        holding_rows = db.execute(
            select(PaperHoldingDaily).where(
                PaperHoldingDaily.account_id == account.id,
                PaperHoldingDaily.holding_date == as_of,
            )
        ).scalars().all()
        snapshot = {row.instrument_id: row for row in holding_rows}

    instruments = {
        i.id: i
        for i in db.execute(
            select(Instrument).where(
                Instrument.id.in_([p.instrument_id for p in positions])
            )
        ).scalars().all()
    } if positions else {}

    items: list[PaperPositionItem] = []
    for position in positions:
        instrument = instruments.get(position.instrument_id)
        snap = snapshot.get(position.instrument_id)
        market_value = Decimal(snap.market_value) if snap else None
        cost = Decimal(position.cost)
        profit = (market_value - cost) if market_value is not None else None
        items.append(
            PaperPositionItem(
                code=instrument.code if instrument else str(position.instrument_id),
                name=instrument.name if instrument else "",
                shares=Decimal(position.shares),
                cost=cost,
                nav=float(snap.nav) if snap else None,
                nav_date=as_of.isoformat() if (snap and as_of) else None,
                market_value=market_value,
                weight=float(snap.weight) if snap else None,
                profit=profit,
                profit_pct=(
                    float(profit / cost) if profit is not None and cost > 0 else None
                ),
            )
        )
    return PaperPositionsResponse(
        account_id=account.id,
        as_of=as_of.isoformat() if as_of else None,
        cash=Decimal(account.cash),
        total_value=total_value,
        count=len(items),
        items=items,
    )


def get_holding_history(
    db: Session, start_date: str | None = None, end_date: str | None = None, limit: int = 2000
) -> list[PaperHoldingDayItem]:
    """每日持仓估值快照（供历史查询，日期升序）。"""
    account = get_default_account(db)
    start = _parse_day(start_date)
    end = _parse_day(end_date)
    if start and end and start > end:
        raise PaperError("start_date 不能晚于 end_date")
    stmt = (
        select(PaperHoldingDaily, Instrument)
        .join(Instrument, Instrument.id == PaperHoldingDaily.instrument_id)
        .where(PaperHoldingDaily.account_id == account.id)
        .order_by(PaperHoldingDaily.holding_date, Instrument.code)
        .limit(limit)
    )
    return [
        PaperHoldingDayItem(
            date=row.holding_date.isoformat(),
            code=instrument.code,
            name=instrument.name,
            shares=Decimal(row.shares),
            nav=float(row.nav),
            market_value=Decimal(row.market_value),
            weight=float(row.weight),
        )
        for row, instrument in db.execute(stmt).all()
        if (start is None or row.holding_date >= start)
        and (end is None or row.holding_date <= end)
    ]


def get_trades(db: Session, limit: int = 200, offset: int = 0) -> PaperTradesResponse:
    """虚拟成交记录（日期倒序）。"""
    account = get_default_account(db)
    total = db.execute(
        select(func.count(PaperTrade.id)).where(PaperTrade.account_id == account.id)
    ).scalar_one()
    stmt = (
        select(PaperTrade, Instrument)
        .join(Instrument, Instrument.id == PaperTrade.instrument_id)
        .where(PaperTrade.account_id == account.id)
        .order_by(PaperTrade.trade_date.desc(), PaperTrade.id.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [
        PaperTradeItem(
            id=trade.id,
            date=trade.trade_date.isoformat(),
            code=instrument.code,
            name=instrument.name,
            side=trade.side,  # type: ignore[arg-type]
            shares=Decimal(trade.shares),
            price=Decimal(trade.price),
            amount=Decimal(trade.amount),
            fee=Decimal(trade.fee),
            target_weight=_f(trade.target_weight),
        )
        for trade, instrument in db.execute(stmt).all()
    ]
    return PaperTradesResponse(account_id=account.id, count=int(total), items=items)


def get_signals(db: Session, limit: int = 60, offset: int = 0) -> PaperSignalsResponse:
    """调仓日信号快照（日期倒序）。"""
    account = get_default_account(db)
    total = db.execute(
        select(func.count(SignalSnapshot.id)).where(
            SignalSnapshot.account_id == account.id
        )
    ).scalar_one()
    rows_ = db.execute(
        select(SignalSnapshot)
        .where(SignalSnapshot.account_id == account.id)
        .order_by(SignalSnapshot.signal_date.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()
    items = [
        PaperSignalSnapshotItem(
            id=row.id,
            signal_date=row.signal_date.isoformat(),
            as_of=row.as_of,
            methodology=row.methodology,
            candidate_count=row.candidate_count,
            excluded_count=row.excluded_count,
            observe_count=row.observe_count,
            selected_count=row.selected_count,
            items=list(row.items or []),
            warnings=list(row.warnings or []),
        )
        for row in rows_
    ]
    return PaperSignalsResponse(account_id=account.id, count=int(total), items=items)
