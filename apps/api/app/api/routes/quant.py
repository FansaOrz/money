"""量化研究路由：单基金指标、轻量回测、组合指标摘要。

全部为只读研究能力，不涉及任何实盘下单。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.quant import (
    BacktestRequest,
    BacktestResult,
    FundIndicators,
    OptimizeRequest,
    OptimizeResult,
    PortfolioMetricsSummary,
    ScreenerRequest,
    ScreenerResponse,
    SignalFilters,
    SignalListResponse,
    SnapshotResponse,
    ValidationRequest,
    ValidationResponse,
    WalkForwardRequest,
    WalkForwardResult,
)
from app.services import quant as quant_service
from app.services import quant_optimizer as optimizer_service
from app.services import quant_screener as screener_service
from app.services import quant_validation as validation_service
from app.services import quant_walkforward as walkforward_service
from app.services.quant import QuantError

router = APIRouter(prefix="/quant", tags=["quant"])


@router.get("/indicators/{code}", response_model=FundIndicators)
def fund_indicators(code: str, db: Session = Depends(get_db)) -> FundIndicators:
    """单基金量化指标：20/60/250 日收益、年化波动、最大回撤、夏普、MA、MACD、趋势信号。"""
    try:
        return quant_service.compute_fund_indicators(db, code)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/backtest", response_model=BacktestResult)
def backtest(payload: BacktestRequest, db: Session = Depends(get_db)) -> BacktestResult:
    """轻量回测：买入持有 / MA 交叉 / MACD / 定投 / 网格。

    输出资金曲线（抽样）、交易信号（仅研究展示）与汇总指标。
    """
    try:
        return quant_service.run_backtest(db, payload)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/portfolio", response_model=PortfolioMetricsSummary)
@router.get("/portfolio-metrics", response_model=PortfolioMetricsSummary)
def portfolio_metrics(db: Session = Depends(get_db)) -> PortfolioMetricsSummary:
    """组合指标摘要：集中度、各持仓趋势与可解释研究信号（非投资建议）。"""
    return quant_service.portfolio_metrics_summary(db)


@router.get("/funds", response_model=list[FundIndicators])
def fund_metrics(db: Session = Depends(get_db)) -> list[FundIndicators]:
    """返回当前持仓基金的量化指标；样本不足的基金跳过。"""
    return quant_service.list_fund_indicators(db)


@router.get("/signals", response_model=SignalListResponse)
def research_signals(
    category: str | None = Query(default=None, description="按信号类别过滤，如 trend/drawdown/news"),
    level: str | None = Query(default=None, description="按风险级别过滤：info/warning/risk"),
    limit: int = Query(default=100, ge=1, le=1000, description="返回的最大条数"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: Session = Depends(get_db),
) -> SignalListResponse:
    """综合研究信号：融合趋势/动量/回撤、权重、股票穿透、行业、新闻与指数趋势。

    每条信号携带 evidence 证据、related_codes、as_of 与 source，不包含任何自动交易指令。
    """
    try:
        filters = SignalFilters(category=category, level=level, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return quant_service.comprehensive_research_signals(db, filters)


@router.get("/screener/signals", response_model=ScreenerResponse)
def screener_signals(db: Session = Depends(get_db)) -> ScreenerResponse:
    """规则模型五档信号（当前持仓为候选池）。

    动量/风险调整动量/趋势/回撤因子横截面打分，同类市场分位数落五档
    （+2 值得研究加仓 ~ −2 值得研究减仓）；全部样本满足的候选均返回，
    前 top_n 只进入目标组合分配目标权重（含 25%/50% 约束说明），其余仅分析。
    仅为研究信号，不构成投资建议。
    """
    try:
        return screener_service.run_screener(db, ScreenerRequest())
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/screener/run", response_model=ScreenerResponse)
def screener_run(payload: ScreenerRequest, db: Session = Depends(get_db)) -> ScreenerResponse:
    """规则模型筛选：可指定候选基金、目标组合配置上限 top_n 与样本门槛。

    默认候选池为当前持仓基金；全部样本满足的候选都参与五档分析并返回，
    综合分前 top_n 只进入目标组合，目标权重受单基金 25%、单一市场 50%
    约束，截断部分保留为现金；其余候选仅分析、目标权重为 0。
    不产生任何实盘下单行为。
    """
    try:
        return screener_service.run_screener(db, payload)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/walkforward", response_model=WalkForwardResult)
def walkforward(payload: WalkForwardRequest, db: Session = Depends(get_db)) -> WalkForwardResult:
    """Walk-Forward 滚动窗口组合回测（默认训练 120 / 测试 20 / 步进 20）。

    每段仅用训练窗口内数据打分（动量/风险调整动量/趋势/回撤横截面综合分），
    选 top_n 只按受约束目标权重（单基金 ≤25%、单一市场 ≤50%）建仓，
    样本外买入并持有至窗口结束；不卖空、不计手续费、现金零收益。
    基准为全部候选基金等权买入持有（B0）。
    输出净值曲线、各窗口 segments 明细、汇总指标 summary、
    方法说明 methodology 与数据提示 warnings。不产生任何实盘下单行为。
    """
    try:
        return walkforward_service.run_walkforward(db, payload)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/optimize", response_model=OptimizeResult)
def optimize(payload: OptimizeRequest, db: Session = Depends(get_db)) -> OptimizeResult:
    """规则参数优化：有限网格搜索 + 训练内 purged walk-forward + 留出测试一次。

    数据按时间先后 60%/20%/20% 切分为训练/验证/完全留出测试三段；
    在训练段内做 purged walk-forward（step = test_window，embargo = test_window）
    评估每组参数；综合评分 = 0.35×样本外夏普分位 + 0.30×回撤改善分位
    + 0.20×超额收益分位 + 0.15×低换手分位；训练评分前 ≤5 组在验证段
    比较选出最佳参数，最后在完全留出测试段仅评估一次并判定上线门槛。
    返回所有试验摘要、最佳参数、验证与留出测试评估、门槛判定。
    仅为研究用途，不产生任何实盘下单行为。
    """
    try:
        return optimizer_service.run_optimize(db, payload)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/validation", response_model=ValidationResponse)
def validation(payload: ValidationRequest, db: Session = Depends(get_db)) -> ValidationResponse:
    """量化验证：as_of 快照下的样本外验证与稳健性检验。

    as_of 指定时按 QDII lag2 / 国内 lag1 折算各基金可用净值截止日，
    仅用当时可见数据；walk-forward 样本外回测（可选费用：买 0.15%、
    卖默认 0.5%/7 日内 1.5%，基于 lot 持有期 FIFO 估算）。
    输出 CVaR95、Calmar、信息比率、Rank IC、五档收益单调性、
    Deflated Sharpe（记录 trial_count/skew/kurtosis）、block bootstrap
    White Reality Check 近似与参数邻域稳定性。
    仅为研究验证，不构成投资建议，不产生任何实盘下单行为。
    """
    try:
        return validation_service.run_validation(db, payload)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/snapshot", response_model=SnapshotResponse)
def snapshot(
    codes: str | None = Query(default=None, description="逗号分隔的基金代码；缺省为当前持仓基金"),
    as_of: str | None = Query(default=None, description="快照基准日 YYYY-MM-DD；缺省为数据最新交易日"),
    db: Session = Depends(get_db),
) -> SnapshotResponse:
    """as_of 可用日期快照：可用交易日与各基金按 lag 折算的有效数据日。

    QDII 默认 lag2（as_of - 2 个交易日及之前的净值可见）、国内默认 lag1；
    便于调用方选择合法的 as_of 复现历史任一交易日的研究视角。
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else None
    try:
        return validation_service.get_snapshot(db, code_list, as_of)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
