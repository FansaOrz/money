"""基金发现量化服务：候选池成员的因子榜、双动量、V2 信号/回测与验证编排。

设计要点（全部为只读研究能力，不产生任何实盘下单行为）：
1. 候选池：CandidatePool / CandidatePoolMember（或 CandidateMember）模型
   通过动态导入解析（模型尚未落地时不报错、不阻塞应用启动），成员代码
   字段做防御式探测（fund_code / code / instrument_code / symbol）；
   模型不可用或成员为空时抛 QuantError，由路由层转换为 400；
2. 因子计算：完全复用既有模块，无重复实现 ——
   - 净值装载：quant._load_dual_nav_series（连续总收益口径，含分红，
     累计净值缺测区间按单位净值衔接比率拼接，杜绝单位混用）；
   - 收益/波动/回撤/夏普：quant 基础函数（252 交易日年化口径）；
   - CVaR95 / Calmar / Sortino：quant_stats 纯函数；
   - 12-1 绝对动量与市场层分类/基金家族：quant_risk（V2 同一套口径）；
   - 同类分位：quant_factors.quantile_ranks（同市场层内按动量）；
3. 因子榜：按指定因子降序/升序排列 + limit/offset 分页（total 为分页前
   有效候选数）；波动类因子（volatility / max_drawdown / cvar95）缺省
   升序即"风险最小优先"由调用方通过 order 控制，缺失值恒排末尾；
4. 双动量（Dual Momentum）：相对动量（候选内 12-1 前 top_n 等权）+
   绝对动量（前 top_n 全部 ≤ 0 时整体回避、权重归零）；
5. V2 信号 / V2 回测 / 量化验证：薄编排层 —— 解析候选池成员代码后
   直接委托 quant_v2.current_signals / quant_v2.run_backtest_v2 /
   quant_validation.run_validation，响应模型与 /api/quant/* 完全一致。
"""

from __future__ import annotations

import importlib
from datetime import date
from statistics import fmean
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CandidatePoolMember, FundNav, Instrument
from app.schemas.discovery_quant import (
    DualMomentumItem,
    DualMomentumQuery,
    DualMomentumResponse,
    FactorBoardItem,
    FactorBoardQuery,
    FactorBoardResponse,
)
from app.schemas.quant import ValidationRequest, ValidationResponse
from app.schemas.quant_v2 import BacktestV2Request, BacktestV2Result, SignalsV2Response
from app.services import quant_factors as factors
from app.services import quant_risk as risk
from app.services import quant_stats as stats
from app.services import quant_v2 as v2_service
from app.services import quant_validation as validation_service
from app.services.quant import (
    QuantError,
    _annual_volatility,
    _daily_returns,
    _load_dual_nav_series,
    _max_drawdown,
    _period_return,
    _sharpe,
)

# 读取净值的最大条数（覆盖 3 年收益 756 个交易日 + 冗余）
NAV_LOAD_LIMIT = 2000

# 因子榜展示用的收益窗口（交易日）：1 个月 / 3 个月 / 1 年 / 3 年
RETURN_WINDOWS: dict[str, int] = {
    "return_1m": 21,
    "return_3m": 63,
    "return_1y": 252,
    "return_3y": 756,
}

SORTABLE_FACTORS = frozenset(
    {
        "return_1m",
        "return_3m",
        "return_1y",
        "return_3y",
        "annual_volatility",
        "max_drawdown",
        "sharpe",
        "sortino",
        "calmar",
        "cvar95",
        "momentum_12_1",
        "quantile",
    }
)

METHODOLOGY_FACTORS = (
    "基金发现因子榜：候选池成员净值取连续总收益口径（优先累计净值，缺测区间"
    "按单位净值衔接比率拼接，含分红）。收益为区间收益：1m=21、3m=63、1y=252、"
    "3y=756 个交易日；波动/夏普/索提诺基于 window（默认 252）个交易日的日收益"
    "年化（252 口径，无风险利率 2%，索提诺以日无风险利率为最低可接受收益、"
    "按下行偏差折算）；最大回撤为 window 内峰值回撤（负数小数）；"
    "CVaR95 为 window 内最差 5% 日收益均值；Calmar = 年化收益 / |最大回撤|；"
    "12-1 绝对动量 = t-21 收盘 / t-252 前一日收盘 - 1（跳过最近 21 个交易日）；"
    "同类分位为同一市场层（A股/港股/美股/黄金/债券/货币/海外）内按 12-1 动量的"
    "分位数排名（并列取平均秩）。"
    "幸存者偏差声明：榜单基于当前候选池成员，历史时点已清盘/调出池的基金"
    "不在样本内，历史排序可能系统性偏好存活至今的基金。"
    "仅为研究排序，不构成投资建议，不产生任何自动交易。"
)

METHODOLOGY_DUAL_MOMENTUM = (
    "双动量（Dual Momentum）研究信号：对候选池成员计算 12-1 绝对动量"
    "（t-21 收盘 / t-252 前一日收盘 - 1）。相对动量：按动量降序取前 top_n 只"
    "等权配置；绝对动量：仅动量 > 0 的候选可入选，若前 top_n 全部动量 ≤ 0 "
    "则整体回避（hold_offense=false，权重全部归零、现金权重 1）。"
    "幸存者偏差声明：信号基于当前候选池成员，历史时点已清盘/调出池的基金"
    "不在样本内。仅为研究信号，不构成投资建议，不产生任何自动交易。"
)


# ---------------------------------------------------------------------------
# 候选池解析（动态导入 / 防御式字段探测）
# ---------------------------------------------------------------------------


def _load_pool_models() -> tuple[type | None, type | None]:
    """动态导入候选池模型；不存在时优雅降级返回 (None, None)。

    候选池功能可能由独立分支开发，本模块不得因模型缺失而阻塞应用启动；
    仅在实际调用池接口且模型仍不可用时报错。
    """
    try:
        module = importlib.import_module("app.models")
    except Exception:  # noqa: BLE001 - 降级路径，任何导入失败都不影响主流程
        return None, None
    pool_model = getattr(module, "CandidatePool", None)
    member_model = getattr(module, "CandidatePoolMember", None)
    if member_model is None:
        member_model = getattr(module, "CandidateMember", None)
    return pool_model, member_model


def candidate_pool_available() -> bool:
    """候选池模型是否可用（供路由层在依赖未就绪时给出明确 400）。"""
    pool_model, member_model = _load_pool_models()
    return pool_model is not None and member_model is not None


def _member_attr(member: Any, *names: str) -> Any:
    for name in names:
        if hasattr(member, name):
            return getattr(member, name)
    return None


def resolve_pool_codes(db: Session, pool_id: int) -> list[str]:
    """解析候选池成员基金代码（active 成员、按 rank 升序、去重）。

    成员代码字段探测顺序：code / fund_code / instrument_code / symbol；
    若成员仅存 instrument_id，则回退按 Instrument 主键联查代码；
    status 字段存在时仅取 active 成员，排序优先 rank 字段（缺省按 id）。
    模型缺失、池不存在、成员为空或代码均无法识别时抛 QuantError（400）。
    """
    pool_model, member_model = _load_pool_models()
    if pool_model is None or member_model is None:
        raise QuantError(
            "候选池模型尚未就绪（CandidatePool/Member 未注册），"
            "请改用 codes 参数显式指定候选基金"
        )

    pool = db.get(pool_model, pool_id)
    if pool is None:
        raise QuantError(f"候选池 {pool_id} 不存在")

    try:
        stmt = select(member_model).where(member_model.pool_id == pool_id)
        if hasattr(member_model, "status"):
            stmt = stmt.where(member_model.status == "active")
        members = list(db.execute(stmt).scalars().all())
    except Exception as exc:  # noqa: BLE001 - 模型字段与预期不符时给出可诊断错误
        raise QuantError(f"候选池成员查询失败（模型字段可能尚未稳定）：{exc}") from exc
    if not members:
        raise QuantError(f"候选池 {pool_id} 暂无 active 成员基金")

    def _sort_key(member: Any) -> tuple[Any, Any]:
        rank = _member_attr(member, "rank")
        member_id = _member_attr(member, "id")
        return (
            rank if isinstance(rank, int) else 0,
            member_id if isinstance(member_id, int) else 0,
        )

    members.sort(key=_sort_key)

    codes: list[str] = []
    instrument_ids: list[int] = []
    for member in members:
        raw = _member_attr(member, "code", "fund_code", "instrument_code", "symbol")
        if raw is not None and str(raw).strip():
            code = str(raw).strip()
            if code not in codes:
                codes.append(code)
            continue
        instrument_id = _member_attr(member, "instrument_id")
        if instrument_id is not None:
            instrument_ids.append(int(instrument_id))

    if instrument_ids:
        rows = db.execute(
            select(Instrument.id, Instrument.code).where(Instrument.id.in_(instrument_ids))
        ).all()
        code_by_id = {row_id: code for row_id, code in rows}
        for instrument_id in instrument_ids:
            code = code_by_id.get(instrument_id)
            if code and code not in codes:
                codes.append(code)

    if not codes:
        raise QuantError(
            f"候选池 {pool_id} 的成员基金代码均无法识别（已探测 code/fund_code/"
            "instrument_code/symbol/instrument_id 字段）"
        )
    return codes


def resolve_candidates(
    db: Session,
    pool_id: int | None,
    codes: list[str] | None,
) -> tuple[list[str] | None, list[str]]:
    """统一候选来源：显式 codes 优先，其次 pool_id，再次缺省（持仓基金）。

    返回 (候选代码或 None（缺省）, 警告)；pool_id 提供时附带来源说明警告。
    """
    if codes:
        return list(dict.fromkeys(codes)), []
    if pool_id is not None:
        resolved = resolve_pool_codes(db, pool_id)
        return resolved, [f"候选来自候选池 #{pool_id}（{len(resolved)} 只成员基金）"]
    return None, []


def resolve_research_ready_candidates(
    db: Session,
    pool_id: int | None,
    codes: list[str] | None,
    *,
    min_samples: int = risk.MIN_MOMENTUM_SAMPLES,
    limit: int | None = None,
) -> tuple[list[str] | None, list[str]]:
    """解析量化候选，并优先过滤到真实达到研究样本门槛的成员。

    候选池路径直接利用 member.nav_samples，显式 codes/缺省持仓则交给既有量化
    引擎逐只检查。limit 用于高成本验证，截断顺序保持池 rank 可复现。
    """
    resolved, warnings = resolve_candidates(db, pool_id, codes)
    if pool_id is not None and not codes:
        rows = db.execute(
            select(CandidatePoolMember.code, CandidatePoolMember.nav_samples)
            .where(
                CandidatePoolMember.pool_id == pool_id,
                CandidatePoolMember.status == "active",
            )
            .order_by(CandidatePoolMember.rank)
        ).all()
        ready = [code for code, samples in rows if int(samples or 0) >= min_samples]
        # 兼容早期/测试池尚未刷新 nav_samples 的情况：用真实 FundNav 计数兜底。
        if len(ready) < len(rows):
            pending_codes = [
                code for code, samples in rows if int(samples or 0) < min_samples
            ]
            counts = dict(
                db.execute(
                    select(Instrument.code, func.count(FundNav.id))
                    .join(FundNav, FundNav.instrument_id == Instrument.id)
                    .where(Instrument.code.in_(pending_codes))
                    .group_by(Instrument.code)
                ).all()
            )
            ready_set = set(ready)
            ready_set.update(
                code for code in pending_codes if int(counts.get(code, 0)) >= min_samples
            )
            ready = [code for code, _samples in rows if code in ready_set]
        excluded = len(rows) - len(ready)
        if excluded:
            warnings.append(
                f"候选池 #{pool_id} 有 {excluded} 只基金净值不足 {min_samples} 条，"
                f"本次仅使用 {len(ready)} 只研究就绪基金"
            )
        if not ready:
            raise QuantError(
                f"候选池 #{pool_id} 暂无达到 {min_samples} 个净值点的研究就绪基金，"
                "请先完成历史净值回填"
            )
        resolved = ready
    if resolved is not None and limit is not None and len(resolved) > limit:
        original = len(resolved)
        resolved = resolved[:limit]
        warnings.append(
            f"本次高成本验证最多使用 {limit} 只基金；已按候选池稳定顺序从 "
            f"{original} 只研究就绪基金中截取前 {limit} 只"
        )
    return resolved, warnings


# ---------------------------------------------------------------------------
# 单基金因子计算
# ---------------------------------------------------------------------------


def _downside_deviation(returns: list[float], mar: float = 0.0) -> float | None:
    """下行偏差：sqrt(mean(min(r - mar, 0)²))（全样本口径，含非负项按 0 计）。"""
    if len(returns) < 2:
        return None
    n = len(returns)
    downside = [min(r - mar, 0.0) for r in returns]
    variance = sum(d * d for d in downside) / n
    return variance**0.5


def _sortino(returns: list[float], risk_free_rate: float = stats.DEFAULT_RISK_FREE_RATE) -> float | None:
    """索提诺比率（年化）：(年化超额收益) / (年化下行偏差)。"""
    if len(returns) < 2:
        return None
    daily_mar = risk_free_rate / stats.TRADING_DAYS_PER_YEAR
    deviation = _downside_deviation(returns, daily_mar)
    if deviation is None or deviation == 0:
        return None
    excess_daily = fmean(returns) - daily_mar
    return excess_daily / deviation * (stats.TRADING_DAYS_PER_YEAR**0.5)


def _window_tail(values: list[float], window: int) -> list[float]:
    """取序列尾部 window+1 个净值点（window 个交易日收益的口径）。"""
    return values[-window - 1 :] if len(values) > window + 1 else values


def compute_member_factors(
    code: str,
    name: str,
    series: list[tuple[date, float]],
    window: int,
) -> FactorBoardItem:
    """由（已截取窗口的）净值序列计算单只基金的因子行（纯计算，不访问数据库）。"""
    values = [v for _, v in series]
    tail = _window_tail(values, window)
    returns = _daily_returns(tail)

    total = tail[-1] / tail[0] - 1.0 if len(tail) >= 2 and tail[0] > 0 else None
    max_dd = _max_drawdown(tail)
    market = risk.classify_market(name)
    return FactorBoardItem(
        rank=0,  # 由榜单编排层回填
        code=code,
        name=name,
        market=market,
        market_label=risk.market_label(market),
        family=risk.fund_family(name),
        sample_count=len(series),
        return_1m=_period_return(values, RETURN_WINDOWS["return_1m"]),
        return_3m=_period_return(values, RETURN_WINDOWS["return_3m"]),
        return_1y=_period_return(values, RETURN_WINDOWS["return_1y"]),
        return_3y=_period_return(values, RETURN_WINDOWS["return_3y"]),
        annual_volatility=_annual_volatility(returns),
        max_drawdown=max_dd,
        sharpe=_sharpe(returns),
        sortino=_sortino(returns),
        calmar=stats.calmar_ratio(total, len(tail) - 1, max_dd) if total is not None else None,
        cvar95=stats.cvar95(returns),
        momentum_12_1=risk.absolute_momentum_12_1(values),
    )


def _sort_value(item: FactorBoardItem, sort: str) -> float | None:
    return getattr(item, sort, None)


# ---------------------------------------------------------------------------
# 因子榜（分页）
# ---------------------------------------------------------------------------


def factor_leaderboard(db: Session, query: FactorBoardQuery) -> FactorBoardResponse:
    """因子榜入口：装载候选池/显式候选 → 逐只计算因子 → 同类分位 → 排序分页。"""
    warnings: list[str] = []
    codes, source_warnings = resolve_candidates(db, None, query.codes)
    warnings.extend(source_warnings)

    if codes is None:
        raise QuantError("因子榜需要显式 codes 或 pool_id 指定候选池（不支持缺省持仓）")
    if query.sort not in SORTABLE_FACTORS:
        raise QuantError(
            f"不支持的排序因子：{query.sort}（可选：{', '.join(sorted(SORTABLE_FACTORS))}）"
        )

    rows = db.execute(
        select(Instrument).where(Instrument.code.in_(codes))
    ).scalars().all()
    by_code = {instrument.code: instrument for instrument in rows}
    missing = [code for code in codes if code not in by_code]
    if missing:
        warnings.append(f"以下基金代码未找到，已跳过：{', '.join(missing)}")
    instruments = [by_code[code] for code in codes if code in by_code]
    if not instruments:
        raise QuantError("指定的候选基金均未找到，请检查代码")

    items: list[FactorBoardItem] = []
    excluded = 0
    insufficient_examples: list[str] = []
    for instrument in instruments:
        series = _load_dual_nav_series(db, instrument.id, limit=NAV_LOAD_LIMIT).total_series
        if len(series) < query.min_samples:
            excluded += 1
            if len(insufficient_examples) < 5:
                insufficient_examples.append(
                    f"{instrument.code} {instrument.name}（{len(series)} 条）"
                )
            continue
        items.append(
            compute_member_factors(instrument.code, instrument.name, series, query.window)
        )

    if excluded:
        examples = "；".join(insufficient_examples)
        warnings.append(
            f"{excluded} 只基金净值样本不足 {query.min_samples} 条，已从榜单剔除"
            + (f"；示例：{examples}" if examples else "")
        )

    if not items:
        raise QuantError(
            f"有效候选为空（{len(instruments)} 只候选均样本不足 {query.min_samples} 条），"
            "请先同步历史净值"
        )

    # 同类（同市场层）分位：按 12-1 动量在各市场层内取分位数
    for market in {item.market for item in items}:
        group = [item for item in items if item.market == market]
        ranks = factors.quantile_ranks({item.code: item.momentum_12_1 for item in group})
        for item in group:
            item.quantile = ranks.get(item.code)

    # 缺失值恒排末尾（升降序一致）：先按方向对非空值排序，再把 None 追加到尾部；
    # 不能用 reverse 作用于含 None 标记的元组键，否则 desc 时 None 会排到最前
    reverse = query.order == "desc"
    valued = [item for item in items if _sort_value(item, query.sort) is not None]
    missing = [item for item in items if _sort_value(item, query.sort) is None]
    valued.sort(key=lambda item: _sort_value(item, query.sort) or 0.0, reverse=reverse)
    items = valued + missing
    total = len(items)
    page = items[query.offset : query.offset + query.limit]
    for rank, item in enumerate(page, start=query.offset + 1):
        item.rank = rank

    as_of = _latest_nav_date(db, [instrument.id for instrument in instruments])

    return FactorBoardResponse(
        as_of=as_of.isoformat() if as_of else None,
        methodology=METHODOLOGY_FACTORS,
        total=total,
        limit=query.limit,
        offset=query.offset,
        sort=query.sort,
        order=query.order,
        window=query.window,
        pool_size=len(instruments),
        excluded_count=excluded,
        items=page,
        warnings=warnings,
    )


def _latest_nav_date(db: Session, instrument_ids: list[int]) -> date | None:
    """候选基金最新净值日期的最大值（单条聚合查询）。"""
    if not instrument_ids:
        return None
    latest = db.scalar(
        select(func.max(FundNav.nav_date)).where(FundNav.instrument_id.in_(instrument_ids))
    )
    return latest


# ---------------------------------------------------------------------------
# 双动量
# ---------------------------------------------------------------------------


def dual_momentum(db: Session, query: DualMomentumQuery) -> DualMomentumResponse:
    """双动量信号入口：相对动量（前 top_n 等权）× 绝对动量（>0 过滤，整体回避）。"""
    warnings: list[str] = []
    codes, source_warnings = resolve_candidates(db, None, query.codes)
    warnings.extend(source_warnings)
    if codes is None:
        raise QuantError("双动量需要显式 codes 或 pool_id 指定候选池（不支持缺省持仓）")

    rows = db.execute(
        select(Instrument).where(Instrument.code.in_(codes))
    ).scalars().all()
    by_code = {instrument.code: instrument for instrument in rows}
    missing = [code for code in codes if code not in by_code]
    if missing:
        warnings.append(f"以下基金代码未找到，已跳过：{', '.join(missing)}")
    instruments = [by_code[code] for code in codes if code in by_code]
    if not instruments:
        raise QuantError("指定的候选基金均未找到，请检查代码")

    min_samples = risk.MIN_MOMENTUM_SAMPLES
    ranked: list[tuple[Instrument, float, list[tuple[date, float]]]] = []
    insufficient = 0
    insufficient_examples: list[str] = []
    for instrument in instruments:
        series = _load_dual_nav_series(db, instrument.id, limit=NAV_LOAD_LIMIT).total_series
        if len(series) < min_samples:
            insufficient += 1
            if len(insufficient_examples) < 5:
                insufficient_examples.append(
                    f"{instrument.code} {instrument.name}（{len(series)} 条）"
                )
            continue
        values = [v for _, v in series]
        momentum = risk.absolute_momentum_12_1(values)
        if momentum is None:
            warnings.append(f"基金 {instrument.code} 动量不可计算（净值非正），已跳过")
            continue
        ranked.append((instrument, momentum, series))

    if insufficient:
        examples = "；".join(insufficient_examples)
        warnings.append(
            f"{insufficient} 只基金净值样本不足 {min_samples} 条，无法计算 12-1 动量，已跳过"
            + (f"；示例：{examples}" if examples else "")
        )

    if not ranked:
        raise QuantError(
            f"有效候选为空：12-1 动量需要至少 {min_samples} 个净值点，请先同步历史净值"
        )

    ranked.sort(key=lambda entry: entry[1], reverse=True)
    top = ranked[: query.top_n]
    hold_offense = any(momentum > 0 for _instrument, momentum, _series in top)

    items: list[DualMomentumItem] = []
    for rank, (instrument, momentum, _series) in enumerate(ranked, start=1):
        selected = rank <= query.top_n and hold_offense and momentum > 0
        market = risk.classify_market(instrument.name)
        items.append(
            DualMomentumItem(
                rank=rank,
                code=instrument.code,
                name=instrument.name,
                market=market,
                market_label=risk.market_label(market),
                momentum_12_1=round(momentum, 6),
                selected=selected,
                weight=round(1.0 / query.top_n, 6) if selected else 0.0,
            )
        )

    if not hold_offense:
        warnings.append(
            f"前 {query.top_n} 名候选 12-1 动量均 ≤ 0，绝对动量过滤触发整体回避"
            "（hold_offense=false，全部权重归零）"
        )

    as_of = max(series[-1][0] for _instrument, _momentum, series in ranked)
    return DualMomentumResponse(
        as_of=as_of.isoformat(),
        methodology=METHODOLOGY_DUAL_MOMENTUM,
        top_n=query.top_n,
        candidate_count=len(ranked),
        hold_offense=hold_offense,
        cash_weight=1.0 if not hold_offense else round(1.0 - sum(i.weight for i in items), 6),
        items=items,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# V2 信号 / V2 回测 / 量化验证（薄编排：解析候选池后委托既有服务）
# ---------------------------------------------------------------------------


def pool_signals_v2(
    db: Session, pool_id: int | None, codes: list[str] | None, top_n: int
) -> SignalsV2Response:
    """候选池 V2 当期信号：仅装载研究就绪成员后委托 V2。"""
    resolved, warnings = resolve_research_ready_candidates(db, pool_id, codes)
    req = BacktestV2Request(candidate_codes=resolved, top_n=top_n)
    response = v2_service.current_signals(db, req)
    response.warnings = warnings + response.warnings
    return response


def pool_backtest_v2(
    db: Session,
    pool_id: int | None,
    codes: list[str] | None,
    req: BacktestV2Request,
) -> BacktestV2Result:
    """候选池 V2 回测：仅装载研究就绪成员并重新校验请求模型。"""
    resolved, warnings = resolve_research_ready_candidates(db, pool_id, codes)
    validated = req.model_copy(update={"candidate_codes": resolved})
    validated = BacktestV2Request.model_validate(validated.model_dump())
    result = v2_service.run_backtest_v2(db, validated)
    result.warnings = warnings + result.warnings
    return result


def pool_validation(
    db: Session,
    pool_id: int | None,
    codes: list[str] | None,
    req: ValidationRequest,
) -> ValidationResponse:
    """候选池量化验证：研究就绪优先，最多 250 只并重新校验模型。"""
    resolved, warnings = resolve_research_ready_candidates(
        db, pool_id, codes, limit=250
    )
    validated = req.model_copy(update={"candidate_codes": resolved})
    validated = ValidationRequest.model_validate(validated.model_dump())
    result = validation_service.run_validation(db, validated)
    result.warnings = warnings + result.warnings
    return result
