"""统一研究组合服务：聚合基金/股票研究组合，供 GET /api/research/portfolios 使用。

设计要点（全部为只读研究能力，不产生任何实盘下单/订单行为）：
1. 基金组合：自动选取最新候选池（按创建时间倒序取第一个），调用既有
   quant_discovery.pool_signals_v2 获取稳健组合 V2 当期信号，再映射为前端
   兼容的 holdings 结构（code/name/weight/score/reason/reasons/market）；
   score 直接采用 V2 信号的 12-1 绝对动量，无可靠口径时保持 null，不伪造；
2. 降级语义：候选池模型不可用、尚未建池、池内研究就绪数据不足或信号
   引擎报错时均不抛错（不返回 404），返回空组合/空 holdings 并在顶层
   warnings 说明原因；
3. 股票组合：本轮不伪造数据、不调用不存在的接口；数据链路（多因子当期
   信号的可靠读取）尚不完整，固定给出一条 warning 说明，后续再接入。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.schemas.research_portfolios import (
    ResearchPortfolio,
    ResearchPortfolioHolding,
    ResearchPortfoliosResponse,
)
from app.services import quant_discovery as discovery_service
from app.services.quant import QuantError

# 基金组合默认入选只数上限（与 V2 回测默认 top_n 一致）
DEFAULT_FUND_TOP_N = 8

FUND_PORTFOLIO_DESCRIPTION = (
    "稳健组合 V2 当期目标信号：最新候选池成员经绝对动量过滤后层内配置，"
    "含单基金/家族/QDII 权重约束与波动率目标仓位控制。"
)

STOCK_DATA_WARNING = (
    "股票研究组合暂不可用：A股多因子当期信号的可靠读取链路尚未接入本接口，"
    "为避免伪造数据本轮不提供股票组合；请通过 /api/stocks/research/* 获取股票研究输出。"
)


def _latest_pool_id(db: Session) -> tuple[int | None, str | None]:
    """取最新候选池（按创建时间倒序的第一个）的 ID 与名称。

    候选池模型不可用或尚未建池时返回 (None, None)，由上层降级处理。
    """
    pool_model, member_model = discovery_service._load_pool_models()
    if pool_model is None or member_model is None:
        return None, None
    row = db.execute(
        select(pool_model.id, pool_model.name)
        .order_by(pool_model.created_at.desc(), pool_model.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None, None
    return int(row[0]), str(row[1]) if row[1] else None


def _build_fund_portfolio(db: Session, top_n: int) -> tuple[ResearchPortfolio | None, list[str]]:
    """构建基金研究组合；任何数据不足均降级为 None + warnings（不抛错）。"""
    warnings: list[str] = []
    pool_id, pool_name = _latest_pool_id(db)
    if pool_id is None:
        warnings.append(
            "基金研究组合暂不可用：尚无候选池（CandidatePool）数据，"
            "请先运行候选池构建任务后再访问本接口"
        )
        return None, warnings

    try:
        signals = discovery_service.pool_signals_v2(db, pool_id, None, top_n)
    except QuantError as exc:
        warnings.append(f"基金研究组合暂不可用（候选池 #{pool_id}）：{exc}")
        return None, warnings
    except Exception as exc:  # noqa: BLE001 - 统一降级：数据问题不向上抛 500
        warnings.append(
            f"基金研究组合暂不可用（候选池 #{pool_id}）：信号计算失败（{exc}）"
        )
        return None, warnings

    # 透传 V2 信号自身的数据提示（样本不足剔除、日期对齐等），保持可诊断性
    warnings.extend(signals.warnings)

    holdings = [
        ResearchPortfolioHolding(
            code=item.code,
            name=item.name,
            weight=item.weight,
            score=item.momentum_12_1,
            reason="；".join(item.reasons),
            reasons=list(item.reasons),
            market=item.market,
        )
        for item in signals.selected
    ]
    if not holdings:
        warnings.append(
            f"候选池 #{pool_id} 当期无入选基金（可能触发整体回避或冻结保护），"
            "基金组合 holdings 为空"
        )

    pool_label = pool_name or f"候选池 #{pool_id}"
    portfolio = ResearchPortfolio(
        id=f"fund-v2-pool-{pool_id}",
        name=f"基金研究组合（{pool_label}）",
        kind="fund",
        description=FUND_PORTFOLIO_DESCRIPTION,
        methodology=signals.methodology,
        as_of=signals.as_of,
        holdings=holdings,
    )
    return portfolio, warnings


def list_research_portfolios(
    db: Session, *, fund_top_n: int = DEFAULT_FUND_TOP_N
) -> ResearchPortfoliosResponse:
    """统一研究组合入口：基金组合（最新候选池 × V2 信号）+ 股票侧提示。

    只读聚合，不创建任何订单；数据不足时返回 200 + 空 portfolios + warnings。
    """
    fund_portfolio, warnings = _build_fund_portfolio(db, fund_top_n)
    # 股票侧：数据链路不完整，固定提示，不伪造组合
    warnings.append(STOCK_DATA_WARNING)

    portfolios = [fund_portfolio] if fund_portfolio is not None else []
    as_of = fund_portfolio.as_of if fund_portfolio is not None else None
    return ResearchPortfoliosResponse(portfolios=portfolios, as_of=as_of, warnings=warnings)
