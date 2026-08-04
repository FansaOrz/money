"""A 股规则策略两个月前向模拟服务。

设计原则：
- 仅在沪深300 + 中证500当前成分的日线、行业、财务、PE/PB 均完整覆盖后冻结候选池；
- 首次运行只在 T 日收盘生成信号，下一真实交易日才按开盘价成交；
- 之后在跨月后的第一个交易日，用上月最后一个已记账交易日生成信号并成交；
- 停牌、涨跌停订单保留 pending，后续交易日继续尝试；
- 费用、滑点和涨跌停口径复用 stock_backtest；
- 每个真实行情日幂等，休市或数据未推进时不重复记账。

全部为本地模拟研究，不产生任何真实订单。
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import subprocess
import sys
from calendar import monthrange
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from statistics import fmean

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    IndexConstituent,
    StockDailyBar,
    StockFinancialIndicator,
    StockIndustry,
    StockMaster,
    StockPaperAccount,
    StockPaperNavDaily,
    StockPaperPosition,
    StockPaperReceivable,
    StockPaperRun,
    StockPaperSignal,
    StockPaperTrade,
    StockSyncState,
    StockValuation,
    StrategyVersion,
)
from app.schemas.stock_paper import (
    StockPaperHistoryPoint,
    StockPaperMetrics,
    StockPaperPositionOut,
    StockPaperReadiness,
    StockPaperRunResponse,
    StockPaperSignalOut,
    StockPaperStrategyInfo,
    StockPaperSummary,
)
from app.services import quant_stats as stats
from app.services import (
    stock_backtest,
    stock_factors,
    stock_strategy,
    stock_validation,
    strategy_lifecycle,
)
from app.services.quant_data_governance import save_readiness
from app.services.stock_repository import (
    StockBar,
    StockInfo,
    StockRepository,
    load_repository,
    st_status_as_of,
)
from app.timezone import now_cn

STRATEGY_NAME = "A股多因子规则V4-全链路治理版两个月前向验证"
ACCOUNT_NAME = "A股规则策略模拟账户"
INITIAL_CAPITAL = Decimal("1000000.00")
TRIAL_MONTHS = 2
INDEX_CODES = ("000300", "000905")
EXPECTED_UNIVERSE_COUNT = 800
TOP_N = 30
MAX_STOCK_WEIGHT = 0.05
MAX_INDUSTRY_WEIGHT = 0.20
MIN_AVG_AMOUNT = 5e7
PRICE_LIMIT_COEFFICIENT = 0.98
MAX_VOLUME_PARTICIPATION = 0.10
MINIMUM_TRADE_WEIGHT = 0.002
MINIMUM_HOLDINGS = 20
MAX_ANNUAL_VOLATILITY = 0.25
MAX_TRACKING_ERROR = 0.15
REQUIRE_PREVALIDATION = True
COST = stock_backtest.CostModel(
    commission_rate=0.00025,
    min_commission=5.0,
    stamp_tax_rate=0.0005,
    slippage_rate=0.001,
)

_CENT = Decimal("0.01")
_QTY = Decimal("0.000001")
_PRICE = Decimal("0.000001")
_WEIGHT = Decimal("0.00000001")

METHODOLOGY = (
    "A股规则多因子两个月前向验证：候选池在账户创建时冻结为沪深300+中证500"
    "全部当前成分，且启动前逐只校验日线、行业、财务和PE/PB估值覆盖；"
    "动态剔除ST/停牌/次新/低流动性；"
    "质量30%（含ROA/应计/稳定性）、价值25%（含SP/股息/FCF）、"
    "动量20%（12-1/6-1/反转/残差）、趋势15%、低风险10%，"
    "行业内缩尾和标准化；月频调仓，自由流通市值行业基准，约束单股5%、"
    "单行业20%、市值/Beta/流动性和ADV容量，持仓保留区与最小交易权重"
    "降低抖动；T日收盘生成信号，T+1开盘成交，含最低佣金、卖出印花税、"
    "波动与成交参与率动态滑点；部分成交、公司行为、停牌及涨跌停顺延。"
    "仅用于前向模拟研究，不构成投资建议，不产生真实订单。"
)


class StockPaperError(ValueError):
    """前向模拟数据不足或状态错误。"""


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _qty(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(_QTY, rounding=ROUND_HALF_UP)


def _price(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(_PRICE, rounding=ROUND_HALF_UP)


def _weight(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(_WEIGHT, rounding=ROUND_HALF_UP)


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _base_universe_codes(db: Session) -> list[str]:
    return list(
        db.scalars(
            select(IndexConstituent.stock_code)
            .where(IndexConstituent.index_code.in_(INDEX_CODES))
            .distinct()
            .order_by(IndexConstituent.stock_code)
        ).all()
    )


def _latest_data_date(db: Session, codes: list[str]) -> date | None:
    if not codes:
        return None
    return db.scalar(
        select(func.max(StockDailyBar.last_trade_date)).where(
            StockDailyBar.code.in_(codes)
        )
    )


def _quorum_data_date(db: Session, codes: list[str], ratio: float = 0.8) -> date | None:
    """取至少 ratio 候选已更新到的最近日期，防止小批同步提前推进模拟盘。"""
    if not codes:
        return None
    required = math.ceil(len(codes) * ratio)
    rows = db.execute(
        select(StockDailyBar.last_trade_date, func.count(func.distinct(StockDailyBar.code)))
        .where(
            StockDailyBar.code.in_(codes),
            StockDailyBar.last_trade_date.is_not(None),
        )
        .group_by(StockDailyBar.last_trade_date)
        .order_by(StockDailyBar.last_trade_date.desc())
    ).all()
    for day, count in rows:
        if day is not None and int(count) >= required:
            return day
    return _latest_data_date(db, codes)


def get_readiness(db: Session) -> StockPaperReadiness:
    """返回启动两个月观察所需的真实数据覆盖率与数据源状态。"""
    universe = _base_universe_codes(db)
    industry_codes = list(
        db.scalars(
            select(StockIndustry.code)
            .where(StockIndustry.code.in_(universe))
            .distinct()
        ).all()
    )
    latest = _quorum_data_date(db, industry_codes)
    stale_cutoff = latest - timedelta(days=7) if latest else None

    daily_ready = 0
    if stale_cutoff is not None:
        daily_ready = (
            db.scalar(
                select(func.count(func.distinct(StockDailyBar.code))).where(
                    StockDailyBar.code.in_(universe),
                    StockDailyBar.last_trade_date >= stale_cutoff,
                )
            )
            or 0
        )
    industry_ready = len(industry_codes)
    financial_ready = (
        db.scalar(
            select(func.count(func.distinct(StockFinancialIndicator.code))).where(
                StockFinancialIndicator.code.in_(universe),
                StockFinancialIndicator.report_date
                >= latest - timedelta(days=550)
                if latest is not None
                else True,
            )
        )
        or 0
    )
    valuation_ready = 0
    if latest is not None:
        valuation_rows = db.execute(
            select(
                StockValuation.code,
                func.count(func.distinct(StockValuation.indicator)),
            )
            .where(
                StockValuation.code.in_(universe),
                StockValuation.trade_date <= latest,
                StockValuation.trade_date >= latest - timedelta(days=10),
                StockValuation.indicator.in_(("pe_ttm", "pb")),
            )
            .group_by(StockValuation.code)
        ).all()
        valuation_ready = sum(
            1 for _code, count in valuation_rows if int(count) == 2
        )

    blockers: list[str] = []
    warnings: list[str] = []
    required = len(universe)
    if len(universe) < EXPECTED_UNIVERSE_COUNT:
        blockers.append(
            f"沪深300+中证500去重后仅 {len(universe)} 只，"
            f"完整性门槛为 {EXPECTED_UNIVERSE_COUNT} 只"
        )
    if daily_ready < required:
        blockers.append(
            f"近期日线覆盖 {daily_ready}/{required}，必须全部就绪后才能启动"
        )
    if industry_ready < required:
        blockers.append(
            f"行业覆盖 {industry_ready}/{required}，必须全部就绪后才能启动"
        )
    if financial_ready < required:
        blockers.append(
            f"财务数据覆盖 {financial_ready}/{required}，必须全部就绪后才能启动"
        )
    if valuation_ready < required:
        blockers.append(
            f"PE/PB 估值覆盖 {valuation_ready}/{required}，必须全部就绪后才能启动"
        )

    states = db.scalars(select(StockSyncState)).all()
    source_health = {
        row.task: {
            "status": row.status,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "updated": row.updated,
            "failed": row.failed,
            "detail": row.detail,
        }
        for row in states
    }
    return StockPaperReadiness(
        ready=not blockers,
        status="ready" if not blockers else "blocked",
        universe_count=len(universe),
        daily_ready_count=int(daily_ready),
        industry_ready_count=int(industry_ready),
        financial_ready_count=int(financial_ready),
        valuation_ready_count=int(valuation_ready),
        latest_data_date=latest.isoformat() if latest else None,
        source_health=source_health,
        blockers=blockers,
        warnings=warnings,
    )


def _persist_field_readiness(db: Session, signal_date: date) -> dict[str, object]:
    """生成逐股、逐必需字段的新鲜度/非空门禁报告。"""
    universe = _base_universe_codes(db)
    daily = set(
        db.scalars(
            select(StockDailyBar.code).where(
                StockDailyBar.code.in_(universe),
                StockDailyBar.last_trade_date >= signal_date - timedelta(days=7),
            )
        ).all()
    )
    industry = set(
        db.scalars(
            select(StockIndustry.code)
            .where(StockIndustry.code.in_(universe))
            .distinct()
        ).all()
    )
    financial = set(
        db.scalars(
            select(StockFinancialIndicator.code)
            .where(
                StockFinancialIndicator.code.in_(universe),
                StockFinancialIndicator.report_date
                >= signal_date - timedelta(days=550),
            )
            .distinct()
        ).all()
    )
    valuation_rows = db.execute(
        select(
            StockValuation.code,
            func.count(func.distinct(StockValuation.indicator)),
        )
        .where(
            StockValuation.code.in_(universe),
            StockValuation.trade_date.between(
                signal_date - timedelta(days=10), signal_date
            ),
            StockValuation.indicator.in_(("pe_ttm", "pb")),
            StockValuation.value.is_not(None),
        )
        .group_by(StockValuation.code)
    ).all()
    valuation = {
        code for code, count in valuation_rows if int(count) == 2
    }
    rows = {
        code: {
            "daily_same_period": code in daily,
            "industry_sw2021": code in industry,
            "financial_recent": code in financial,
            "pe_pb_recent_non_null": code in valuation,
        }
        for code in universe
    }
    return save_readiness(db, STRATEGY_NAME, signal_date, rows)


def _ready_candidate_codes(db: Session, latest: date) -> list[str]:
    universe = _base_universe_codes(db)
    if not universe:
        return []
    cutoff = latest - timedelta(days=7)
    daily = set(
        db.scalars(
            select(StockDailyBar.code).where(
                StockDailyBar.code.in_(universe),
                StockDailyBar.last_trade_date >= cutoff,
            )
        ).all()
    )
    industries = set(
        db.scalars(
            select(StockIndustry.code)
            .where(StockIndustry.code.in_(universe))
            .distinct()
        ).all()
    )
    return sorted(set(universe) & daily & industries)


def _ensure_account(db: Session, data_date: date) -> tuple[StockPaperAccount, StrategyVersion]:
    version = db.scalar(
        select(StrategyVersion)
        .where(StrategyVersion.name == STRATEGY_NAME)
        .order_by(StrategyVersion.id.desc())
        .limit(1)
    )
    if version is not None:
        account = db.scalar(
            select(StockPaperAccount).where(
                StockPaperAccount.strategy_version_id == version.id,
                StockPaperAccount.name == ACCOUNT_NAME,
            )
        )
        if account is not None:
            if version.status != "paper":
                raise StockPaperError(
                    f"策略版本状态为 {version.status}，不是可运行的 paper 状态"
                )
            return account, version

    readiness = get_readiness(db)
    if not readiness.ready:
        raise StockPaperError("；".join(readiness.blockers))
    candidates = _ready_candidate_codes(db, data_date)
    if len(candidates) != len(_base_universe_codes(db)):
        raise StockPaperError(
            f"完整覆盖校验失败：日线与行业同时就绪 {len(candidates)} 只，"
            "必须与沪深300+中证500完整候选池一致"
        )
    repository_root = Path(__file__).resolve().parents[4]
    git_sha: str | None = None
    git_status = ""
    try:
        git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        git_status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pass
    manifest = repository_root / "data/research/tushare_snapshot/manifest.jsonl"
    manifest_sha256 = (
        hashlib.sha256(manifest.read_bytes()).hexdigest()
        if manifest.exists()
        else None
    )
    candidate_sha256 = hashlib.sha256(
        json.dumps(
            candidates, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()
    params = {
        "asset": "cn_stock",
        "model_version": "stock_rules_v4",
        "purpose": "two_month_forward_paper_validation",
        "indices": list(INDEX_CODES),
        "candidate_count": len(candidates),
        "candidate_sha256": candidate_sha256,
        "git_sha": git_sha,
        "git_worktree_clean": not bool(git_status.strip()),
        "git_status_sha256": hashlib.sha256(git_status.encode()).hexdigest(),
        "stocktoday_manifest_sha256": manifest_sha256,
        "data_as_of": data_date.isoformat(),
        "factor_weights": dict(stock_factors.DEFAULT_FAMILY_WEIGHTS),
        "top_n": TOP_N,
        "max_stock_weight": MAX_STOCK_WEIGHT,
        "max_industry_weight": MAX_INDUSTRY_WEIGHT,
        "min_avg_amount": MIN_AVG_AMOUNT,
        "price_limit_coefficient": PRICE_LIMIT_COEFFICIENT,
        "max_volume_participation": MAX_VOLUME_PARTICIPATION,
        "minimum_trade_weight": MINIMUM_TRADE_WEIGHT,
        "minimum_holdings": MINIMUM_HOLDINGS,
        "max_annual_volatility": MAX_ANNUAL_VOLATILITY,
        "max_tracking_error": MAX_TRACKING_ERROR,
        "cost": {
            "commission_rate": COST.commission_rate,
            "min_commission": COST.min_commission,
            "stamp_tax_rate": COST.stamp_tax_rate,
            "slippage_rate": COST.slippage_rate,
            "market_impact_coefficient": COST.market_impact_coefficient,
            "volatility_slippage_coefficient": (
                COST.volatility_slippage_coefficient
            ),
            "max_total_slippage": COST.max_total_slippage,
        },
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
        },
        "methodology": METHODOLOGY,
    }
    if version is None:
        version = StrategyVersion(
            name=STRATEGY_NAME,
            initial_capital=INITIAL_CAPITAL,
            rebalance_interval=20,
            fee_rate=_weight(COST.commission_rate),
            top_n=TOP_N,
            params=params,
            status="research" if REQUIRE_PREVALIDATION else "paper",
        )
        db.add(version)
        db.commit()
        db.refresh(version)
    if version.status != "paper":
        raise StockPaperError(
            f"策略版本 {version.id} 尚处于 {version.status}；请先调用"
            " /api/stocks/paper/prepare 完成 purged walk-forward、完全留出集"
            "和实验快照门禁"
        )
    account = StockPaperAccount(
        strategy_version_id=version.id,
        name=ACCOUNT_NAME,
        initial_capital=INITIAL_CAPITAL,
        cash=INITIAL_CAPITAL,
        benchmark_nav=Decimal("1"),
        status="paper",
        trial_start=data_date,
        trial_end=_add_months(data_date, TRIAL_MONTHS),
        candidate_codes=candidates,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account, version


def prepare_forward_account(
    db: Session,
    *,
    start: date,
    end: date,
    top_n_grid: list[int],
    max_stock_weight_grid: list[float],
    embargo_days: int = 21,
) -> dict[str, object]:
    """用系统生成的证据晋级 research→validated→paper，并创建空账户。"""
    if start >= end:
        raise StockPaperError("验证开始日期必须早于结束日期")
    readiness = get_readiness(db)
    if not readiness.ready or readiness.latest_data_date is None:
        raise StockPaperError("；".join(readiness.blockers))
    data_date = date.fromisoformat(readiness.latest_data_date)
    if end > data_date:
        raise StockPaperError(
            f"验证结束日 {end.isoformat()} 晚于数据日 {data_date.isoformat()}"
        )

    version = db.scalar(
        select(StrategyVersion)
        .where(StrategyVersion.name == STRATEGY_NAME)
        .order_by(StrategyVersion.id.desc())
        .limit(1)
    )
    if version is None:
        try:
            _ensure_account(db, data_date)
        except StockPaperError:
            pass
        version = db.scalar(
            select(StrategyVersion)
            .where(StrategyVersion.name == STRATEGY_NAME)
            .order_by(StrategyVersion.id.desc())
            .limit(1)
        )
    if version is None:
        raise StockPaperError("研究策略版本创建失败")
    if version.status == "paper":
        account, _ = _ensure_account(db, data_date)
        validation = dict(version.params or {}).get("validation", {})
        return {
            "version_id": version.id,
            "status": version.status,
            "account_id": account.id,
            "data_date": data_date,
            "validation": validation,
        }
    if version.status != "research":
        raise StockPaperError(
            f"策略版本已处于 {version.status}，不能重复评估留出集；"
            "参数变化必须创建新策略名/版本"
        )
    repository_root = Path(__file__).resolve().parents[4]
    try:
        git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        git_status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise StockPaperError("无法读取 Git 实验快照，拒绝评估留出集") from exc
    if git_status.strip():
        changed = [
            line[3:] for line in git_status.splitlines() if len(line) > 3
        ]
        preview = "、".join(changed[:5])
        suffix = " 等" if len(changed) > 5 else ""
        raise StockPaperError(
            "工作区存在未提交改动，无法冻结可复现实验："
            f"{preview}{suffix}；请提交后重新执行"
        )
    params = dict(version.params or {})
    if params.get("validation_sha256"):
        if params.get("git_sha") == git_sha:
            raise StockPaperError(
                "该研究版本已经评估过完全留出集，禁止重复评估；"
                "参数或数据变化必须形成新 Git 提交和新策略版本"
            )
        try:
            strategy_lifecycle.transition(
                db,
                version.id,
                "retired",
                evidence={},
                actor="system:stock-paper-prepare",
                reason=(
                    "已冻结版本未通过晋级，检测到代码提交变化，"
                    "保留旧证据并创建全新研究版本"
                ),
            )
        except ValueError as exc:
            raise StockPaperError(str(exc)) from exc
        successor_params = {
            key: value
            for key, value in params.items()
            if key not in {"validation", "validation_sha256"}
        }
        successor_params["supersedes_version_id"] = version.id
        version = StrategyVersion(
            name=STRATEGY_NAME,
            initial_capital=INITIAL_CAPITAL,
            rebalance_interval=20,
            fee_rate=_weight(COST.commission_rate),
            top_n=TOP_N,
            params=successor_params,
            status="research",
        )
        db.add(version)
        db.commit()
        db.refresh(version)
        params = successor_params
    params.update(
        {
            "git_sha": git_sha,
            "git_worktree_clean": True,
            "git_status_sha256": hashlib.sha256(
                git_status.encode()
            ).hexdigest(),
        }
    )
    version.params = params
    db.commit()

    repository = _repo(db)
    base = stock_backtest.BacktestConfig(
        start=start,
        end=end,
        initial_capital=float(INITIAL_CAPITAL),
        top_n=TOP_N,
        max_stock_weight=MAX_STOCK_WEIGHT,
        max_industry_weight=MAX_INDUSTRY_WEIGHT,
        min_avg_amount=MIN_AVG_AMOUNT,
        price_limit=PRICE_LIMIT_COEFFICIENT,
        universe_indices=INDEX_CODES,
        min_universe_data_coverage=0.95,
        max_volume_participation=MAX_VOLUME_PARTICIPATION,
        minimum_trade_weight=MINIMUM_TRADE_WEIGHT,
        minimum_holdings=MINIMUM_HOLDINGS,
        max_annual_volatility=MAX_ANNUAL_VOLATILITY,
        max_tracking_error=MAX_TRACKING_ERROR,
        cost=COST,
    )
    try:
        validation = stock_validation.run_stock_walk_forward(
            repository,
            base,
            top_n_grid or [TOP_N],
            max_stock_weight_grid or [MAX_STOCK_WEIGHT],
            embargo_days,
        )
    except (
        stock_backtest.BacktestError,
        stock_strategy.IndustryCoverageError,
    ) as exc:
        raise StockPaperError(str(exc)) from exc
    frozen = json.dumps(
        validation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    params = dict(version.params or {})
    params["validation"] = validation
    params["validation_sha256"] = hashlib.sha256(frozen.encode()).hexdigest()
    version.params = params
    db.commit()

    holdout = dict(validation.get("holdout", {}))
    evidence = {
        "data_coverage": validation.get("minimum_data_coverage", 0.0),
        "holdout_evaluations": validation.get("holdout_evaluations"),
        "walkforward_folds": len(validation.get("folds", [])),
        "holdout_sharpe": holdout.get("sharpe"),
        "validation_sha256": params["validation_sha256"],
        "generated_by": "stock_validation.run_stock_walk_forward",
    }
    try:
        version = strategy_lifecycle.transition(
            db,
            version.id,
            "validated",
            evidence=evidence,
            actor="system:stock-paper-prepare",
            reason="系统生成的历史走步与完全留出验证通过",
        )
        version = strategy_lifecycle.transition(
            db,
            version.id,
            "paper",
            evidence={
                "experiment_snapshot_complete": all(
                    params.get(key)
                    for key in (
                        "git_sha",
                        "git_worktree_clean",
                        "candidate_sha256",
                        "data_as_of",
                        "validation_sha256",
                    )
                ),
                "validation_sha256": params["validation_sha256"],
            },
            actor="system:stock-paper-prepare",
            reason="参数、代码、候选池、数据与验证结果均已冻结",
        )
    except ValueError as exc:
        raise StockPaperError(str(exc)) from exc
    account, version = _ensure_account(db, data_date)
    return {
        "version_id": version.id,
        "status": version.status,
        "account_id": account.id,
        "data_date": data_date,
        "validation": validation,
    }


def _repo(db: Session) -> StockRepository:
    repository = load_repository(db)
    if repository is None:
        raise StockPaperError("股票研究仓储不可用")
    return repository


def _generate_signal(
    db: Session,
    repository: StockRepository,
    account: StockPaperAccount,
    run: StockPaperRun,
    signal_date: date,
    execute_on: date | None,
) -> StockPaperSignal:
    """复用生产因子/组合逻辑生成不可变信号快照。"""
    codes = list(account.candidate_codes)
    infos = repository.list_stocks(codes)
    industry_fn = getattr(repository, "industries_as_of", None)
    if callable(industry_fn):
        historical = industry_fn(codes, [signal_date]).get(signal_date, {})
        if not historical:
            raise StockPaperError(
                f"{signal_date.isoformat()} 缺少申万2021历史行业归属"
            )
        infos = [
            replace(info, industry=historical.get(info.code, "未知"))
            for info in infos
        ]
    panel = stock_backtest.build_panel(repository, codes, None, signal_date)
    fundamentals = stock_backtest.load_fundamentals_by_code(
        repository, codes, [signal_date]
    )
    universe, filters = stock_strategy.build_universe(
        infos,
        panel.bars_by_code,
        signal_date,
        MIN_AVG_AMOUNT,
        name_histories=panel.name_histories,
        research_bars_by_code=panel.research_bars_by_code or None,
    )
    contexts = [
        stock_factors.build_context(
            info,
            panel.research_series(info.code),
            fundamentals.get(info.code, []),
            signal_date,
        )
        for info in universe
    ]
    contexts = [
        item
        for item in contexts
        if stock_factors.history_depth(item) >= stock_factors.MIN_HISTORY_DAYS
    ]
    scored = stock_factors.compute_cross_section(contexts, signal_date)
    positions = _position_rows(db, account.id)
    _total, values = _portfolio_value(
        db, account, positions, panel.bars_by_code, signal_date
    )
    current_weights = (
        {code: value / _total for code, value in values.items()}
        if _total > 0
        else {}
    )
    try:
        plan = stock_strategy.build_portfolio(
            scored,
            universe,
            signal_date,
            top_n=TOP_N,
            max_stock_weight=MAX_STOCK_WEIGHT,
            max_industry_weight=MAX_INDUSTRY_WEIGHT,
            current_weights=current_weights,
            portfolio_value=_total,
            max_adv_participation=MAX_VOLUME_PARTICIPATION,
            minimum_holdings=MINIMUM_HOLDINGS,
            max_annual_volatility=MAX_ANNUAL_VOLATILITY,
            max_tracking_error=MAX_TRACKING_ERROR,
        )
    except stock_strategy.IndustryCoverageError as exc:
        raise StockPaperError(str(exc)) from exc
    ranked = sorted(scored, key=lambda item: item.composite, reverse=True)
    rank = {item.code: index + 1 for index, item in enumerate(ranked)}
    selected = {item.code: item for item in ranked if item.code in plan.target_weights}
    items = [
        {
            "code": code,
            "name": selected[code].name,
            "industry": selected[code].industry,
            "rank": rank[code],
            "composite": round(selected[code].composite, 6),
            "data_coverage": round(selected[code].data_coverage, 6),
            "data_warnings": list(selected[code].data_warnings),
            "weight": weight,
            "quality": selected[code].quality,
            "value": selected[code].value,
            "momentum": selected[code].momentum,
            "trend": selected[code].trend,
            "lowvol": selected[code].lowvol,
        }
        for code, weight in plan.target_weights.items()
    ]
    excluded = sum(1 for item in filters if not item.passed)
    warnings = list(plan.warnings)
    if excluded:
        warnings.append(f"动态股票池过滤剔除 {excluded} 只")
    filter_by_code = {item.code: item for item in filters}
    run.result = {
        **dict(run.result or {}),
        "factor_snapshot": [
            {
                "code": item.code,
                "name": item.name,
                "industry": item.industry,
                "rank": rank[item.code],
                "selected": item.code in plan.target_weights,
                "target_weight": plan.target_weights.get(item.code, 0.0),
                "composite": round(item.composite, 8),
                "data_coverage": round(item.data_coverage, 6),
                "eligible": item.eligible,
                "raw": dict(item.raw),
                "zscores": dict(item.zscores),
                "data_warnings": list(item.data_warnings),
                "filter_reasons": list(
                    filter_by_code[item.code].reasons
                    if item.code in filter_by_code
                    else []
                ),
            }
            for item in ranked
        ],
        "filter_snapshot": [
            {
                "code": item.code,
                "passed": item.passed,
                "reasons": list(item.reasons),
            }
            for item in filters
        ],
        "portfolio_diagnostics": dict(plan.diagnostics),
    }
    signal = StockPaperSignal(
        account_id=account.id,
        run_id=run.id,
        signal_date=signal_date,
        execute_on=execute_on,
        status="pending",
        universe_count=len(universe),
        selected_count=len(items),
        invested_weight=_weight(plan.invested_weight),
        target_weights=dict(plan.target_weights),
        order_state={
            code: {
                "target_weight": weight,
                "status": "pending",
                "attempts": 0,
                "filled_shares": 0.0,
                "events": [],
            }
            for code, weight in plan.target_weights.items()
        },
        items=items,
        methodology=METHODOLOGY,
        warnings=warnings,
    )
    db.add(signal)
    db.flush()
    return signal


def _bar_maps(
    repository: StockRepository, codes: list[str], day: date
) -> tuple[dict[str, StockBar], dict[str, list[StockBar]]]:
    bars = repository.daily_bars(codes, None, day)
    histories: dict[str, list[StockBar]] = {}
    for bar in bars:
        histories.setdefault(bar.code, []).append(bar)
    for values in histories.values():
        values.sort(key=lambda item: item.trade_date)
    current = {
        code: values[-1]
        for code, values in histories.items()
        if values and values[-1].trade_date == day
    }
    return current, histories


def _position_rows(db: Session, account_id: int) -> dict[str, StockPaperPosition]:
    return {
        row.stock_code: row
        for row in db.scalars(
            select(StockPaperPosition).where(
                StockPaperPosition.account_id == account_id
            )
        ).all()
    }


def _portfolio_value(
    db: Session,
    account: StockPaperAccount,
    positions: dict[str, StockPaperPosition],
    histories: dict[str, list[StockBar]],
    day: date,
) -> tuple[float, dict[str, float]]:
    values: dict[str, float] = {}
    for code, position in positions.items():
        price = stock_backtest._last_price_before(histories.get(code, []), day, None)
        if price is not None:
            values[code] = float(position.shares) * price
    receivables = (
        db.scalar(
            select(func.sum(StockPaperReceivable.amount)).where(
                StockPaperReceivable.account_id == account.id,
                StockPaperReceivable.status == "receivable",
            )
        )
        or Decimal("0")
    )
    return (
        float(account.cash) + float(receivables) + sum(values.values()),
        values,
    )


def _apply_corporate_actions(
    db: Session,
    repository: StockRepository,
    account: StockPaperAccount,
    run: StockPaperRun,
    day: date,
) -> None:
    """把当日公司行为记入模拟账本；run 的日期唯一约束保证幂等。"""
    positions = _position_rows(db, account.id)
    action_fn = getattr(repository, "corporate_actions", None)
    if not callable(action_fn):
        run.warnings = list(run.warnings) + ["仓储不支持公司行为，需人工核对"]
        return
    action_codes = sorted(set(positions) | set(account.candidate_codes))
    actions = list(action_fn(action_codes, day, day))
    applied: list[dict[str, object]] = []
    for action in actions:
        event_key = action.event_key or (
            f"{action.code}:{action.action_date.isoformat()}:{action.kind}"
        )
        if action.kind == "cash_payment":
            receivable = db.scalar(
                select(StockPaperReceivable).where(
                    StockPaperReceivable.account_id == account.id,
                    StockPaperReceivable.event_key == event_key,
                    StockPaperReceivable.status == "receivable",
                )
            )
            if receivable is not None:
                account.cash = _money(
                    float(account.cash) + float(receivable.amount)
                )
                receivable.status = "paid"
                receivable.paid_at = day
                applied.append(
                    {
                        "code": action.code,
                        "kind": "cash_payment",
                        "amount": float(receivable.amount),
                        "event_key": event_key,
                        "source": action.source,
                    }
                )
            continue
        position = positions.get(action.code)
        if position is None:
            continue
        held = float(position.shares)
        if action.kind == "terminal":
            price = action.terminal_price
            if price is None or price <= 0:
                bars = repository.daily_bars([action.code], None, day)
                price = stock_backtest._last_price_before(bars, day, None)
            if price is None or price <= 0:
                run.warnings = list(run.warnings) + [
                    f"{action.code} 终止上市但无清算价格，需人工处理"
                ]
                continue
            account.cash = _money(float(account.cash) + held * price)
            db.delete(position)
            applied.append(
                {
                    "code": action.code,
                    "kind": "terminal",
                    "shares": held,
                    "price": price,
                    "source": action.source,
                }
            )
            continue
        if action.kind == "cash_entitlement":
            cash_amount = held * action.cash_per_share
            if action.payment_date is None or action.payment_date <= day:
                account.cash = _money(float(account.cash) + cash_amount)
            else:
                existing = db.scalar(
                    select(StockPaperReceivable).where(
                        StockPaperReceivable.account_id == account.id,
                        StockPaperReceivable.event_key == event_key,
                    )
                )
                if existing is None:
                    db.add(
                        StockPaperReceivable(
                            account_id=account.id,
                            stock_code=action.code,
                            event_key=event_key,
                            entitlement_date=day,
                            payment_date=action.payment_date,
                            amount=_money(cash_amount),
                            status="receivable",
                            source=action.source,
                        )
                    )
            applied.append(
                {
                    "code": action.code,
                    "kind": "cash_entitlement",
                    "amount": cash_amount,
                    "payment_date": (
                        action.payment_date.isoformat()
                        if action.payment_date is not None
                        else None
                    ),
                    "event_key": event_key,
                    "source": action.source,
                }
            )
        elif action.kind == "distribution":
            cash_amount = held * action.cash_per_share
            account.cash = _money(float(account.cash) + cash_amount)
            new_shares = held * action.share_ratio
            position.shares = _qty(held + new_shares)
            applied.append(
                {
                    "code": action.code,
                    "kind": "distribution",
                    "cash": cash_amount,
                    "new_shares": new_shares,
                    "source": action.source,
                }
            )
        elif action.kind == "rights_issue":
            price = action.subscription_price
            requested = held * max(action.subscription_ratio, 0.0)
            if price is None or price <= 0 or requested <= 0:
                run.warnings = list(run.warnings) + [
                    f"{action.code} 配股事件字段不完整，需人工处理"
                ]
                continue
            subscribed = min(
                requested, max(float(account.cash) / price, 0.0)
            )
            account.cash = _money(float(account.cash) - subscribed * price)
            position.shares = _qty(held + subscribed)
            applied.append(
                {
                    "code": action.code,
                    "kind": "rights_issue",
                    "requested_shares": requested,
                    "subscribed_shares": subscribed,
                    "subscription_price": price,
                    "source": action.source,
                }
            )
            if subscribed + 1e-9 < requested:
                run.warnings = list(run.warnings) + [
                    f"{action.code} 配股因现金约束仅认购"
                    f" {subscribed:.3f}/{requested:.3f} 股"
                ]
        elif action.kind in {"merger", "code_change"}:
            successor = action.successor_code
            if not successor or action.share_ratio <= 0:
                run.warnings = list(run.warnings) + [
                    f"{action.code} 换股合并字段不完整，需人工处理"
                ]
                continue
            converted = held * action.share_ratio
            successor_position = positions.get(successor)
            if successor_position is None:
                successor_position = StockPaperPosition(
                    account_id=account.id,
                    stock_code=successor,
                    shares=_qty(converted),
                    cost=position.cost,
                )
                db.add(successor_position)
                positions[successor] = successor_position
            else:
                successor_position.shares = _qty(
                    float(successor_position.shares) + converted
                )
                successor_position.cost = _money(
                    float(successor_position.cost) + float(position.cost)
                )
            db.delete(position)
            positions.pop(action.code, None)
            applied.append(
                {
                    "code": action.code,
                    "kind": "merger",
                    "successor_code": successor,
                    "converted_shares": converted,
                    "share_ratio": action.share_ratio,
                    "source": action.source,
                }
            )
        else:
            new_shares = held * action.share_ratio
            position.shares = _qty(held + new_shares)
            applied.append(
                {
                    "code": action.code,
                    "kind": "share_distribution",
                    "new_shares": new_shares,
                    "source": action.source,
                }
            )
    if applied:
        run.result = {**dict(run.result or {}), "corporate_actions": applied}
        db.flush()


def _execute_pending(
    db: Session,
    repository: StockRepository,
    account: StockPaperAccount,
    run: StockPaperRun,
    day: date,
) -> tuple[int, float, list[str]]:
    """按 T+1 开盘执行最新 pending 信号；卖出优先，受阻订单后续重试。"""
    pending = db.scalar(
        select(StockPaperSignal)
        .where(
            StockPaperSignal.account_id == account.id,
            StockPaperSignal.status == "pending",
            StockPaperSignal.signal_date < day,
        )
        .order_by(StockPaperSignal.signal_date.desc())
        .limit(1)
    )
    if pending is None:
        return 0, 0.0, []
    if pending.execute_on is not None and day < pending.execute_on:
        return 0, 0.0, []

    older = db.scalars(
        select(StockPaperSignal).where(
            StockPaperSignal.account_id == account.id,
            StockPaperSignal.status == "pending",
            StockPaperSignal.id != pending.id,
        )
    ).all()
    for row in older:
        row.status = "superseded"
        state = {
            code: {**dict(item), "events": list(dict(item).get("events", []))}
            for code, item in dict(row.order_state or {}).items()
        }
        for item in state.values():
            if item.get("status") not in {"filled", "cancelled", "superseded"}:
                item["status"] = "superseded"
                item["last_reason"] = "被新一期目标权重覆盖"
                item["events"].append(
                    {
                        "date": day.isoformat(),
                        "status": "superseded",
                        "reason": "被新一期目标权重覆盖",
                    }
                )
        row.order_state = state

    positions = _position_rows(db, account.id)
    codes = sorted(set(positions) | set(pending.target_weights))
    infos = {item.code: item for item in repository.list_stocks(codes)}
    current, histories = _bar_maps(repository, codes, day)
    total_value, _values = _portfolio_value(
        db, account, positions, histories, day
    )
    if total_value <= 0:
        raise StockPaperError("模拟账户总资产无效，无法执行调仓")

    name_fn = getattr(repository, "name_histories", None)
    name_histories = {}
    if callable(name_fn):
        try:
            name_histories = dict(name_fn(codes))
        except Exception:  # noqa: BLE001
            name_histories = {}

    trade_count = 0
    fee_total = 0.0
    blocked: list[str] = []
    order_state = {
        code: {**dict(item), "events": list(dict(item).get("events", []))}
        for code, item in dict(pending.order_state or {}).items()
    }
    attempted: set[str] = set()

    def record_order(
        code: str,
        side: str,
        status_value: str,
        *,
        requested: float = 0.0,
        filled: float = 0.0,
        reason: str | None = None,
    ) -> None:
        item = order_state.setdefault(
            code,
            {
                "target_weight": float(pending.target_weights.get(code, 0.0)),
                "status": "pending",
                "attempts": 0,
                "filled_shares": 0.0,
                "events": [],
            },
        )
        if code not in attempted:
            item["attempts"] = int(item.get("attempts", 0)) + 1
            attempted.add(code)
        item["status"] = status_value
        item["side"] = side
        item["last_requested_shares"] = requested
        item["last_filled_shares"] = filled
        item["remaining_shares"] = max(requested - filled, 0.0)
        item["filled_shares"] = float(item.get("filled_shares", 0.0)) + filled
        item["last_reason"] = reason
        item.setdefault("events", []).append(
            {
                "date": day.isoformat(),
                "side": side,
                "status": status_value,
                "requested_shares": requested,
                "filled_shares": filled,
                "remaining_shares": max(requested - filled, 0.0),
                "reason": reason,
            }
        )

    def trade_allowed(code: str, side: str) -> tuple[bool, str, StockBar | None]:
        bar = current.get(code)
        prev = stock_backtest.prev_bar_before(histories.get(code, []), day)
        info = infos.get(code, StockInfo(code=code, name=code))
        st = st_status_as_of(
            info.name, name_histories.get(code), day
        )
        ok, reason = stock_backtest.can_trade(
            bar,
            prev.close if prev else None,
            side,
            PRICE_LIMIT_COEFFICIENT,
            code=code,
            st=st,
        )
        return ok, reason, bar

    # 先卖出释放现金。
    for code, position in list(positions.items()):
        bar = current.get(code)
        if bar is None:
            blocked.append(f"{code}: 成交日无行情")
            record_order(code, "sell", "blocked", reason="成交日无行情")
            continue
        px = stock_backtest.trade_price(bar, "sell", COST.slippage_rate)
        desired_value = float(pending.target_weights.get(code, 0.0)) * total_value
        desired_shares = math.floor(max(desired_value / px, 0.0) / 100.0) * 100.0
        held = float(position.shares)
        sell_shares = held - desired_shares
        if pending.target_weights.get(code, 0.0) <= 0:
            sell_shares = held
        else:
            sell_shares = math.floor(max(sell_shares, 0.0) / 100.0) * 100.0
        if sell_shares <= 1e-6:
            record_order(
                code,
                "sell",
                "filled",
                reason="已达到目标权重，无需卖出",
            )
            continue
        if sell_shares * px / total_value < MINIMUM_TRADE_WEIGHT:
            record_order(
                code,
                "sell",
                "filled",
                requested=sell_shares,
                reason="剩余偏差低于最小交易权重",
            )
            continue
        ok, reason, bar = trade_allowed(code, "sell")
        if not ok or bar is None:
            blocked.append(f"{code}: {reason}")
            record_order(
                code, "sell", "blocked", requested=sell_shares, reason=reason
            )
            continue
        requested_shares = sell_shares
        if bar.volume is not None:
            capacity = (
                math.floor(
                    max(bar.volume, 0.0)
                    * MAX_VOLUME_PARTICIPATION
                    / stock_backtest.BOARD_LOT
                )
                * stock_backtest.BOARD_LOT
            )
            if sell_shares > capacity:
                sell_shares = capacity
                blocked.append(f"{code}: 超过成交量参与率，卖出部分成交")
        if sell_shares <= 1e-6:
            record_order(
                code,
                "sell",
                "blocked",
                requested=requested_shares,
                reason="成交容量为零",
            )
            continue
        px = stock_backtest.trade_price(
            bar,
            "sell",
            COST.slippage_rate,
            shares=sell_shares,
            volatility=stock_backtest.recent_volatility(histories.get(code, []), day),
            market_impact_coefficient=COST.market_impact_coefficient,
            volatility_slippage_coefficient=COST.volatility_slippage_coefficient,
            max_total_slippage=COST.max_total_slippage,
        )
        amount = sell_shares * px
        fee = stock_backtest.trade_fee("sell", amount, COST)
        position.shares = _qty(held - sell_shares)
        if float(position.shares) <= 1e-6:
            db.delete(position)
            positions.pop(code, None)
        account.cash = _money(float(account.cash) + amount - fee)
        db.add(
            StockPaperTrade(
                account_id=account.id,
                run_id=run.id,
                signal_id=pending.id,
                stock_code=code,
                trade_date=day,
                side="sell",
                shares=_qty(sell_shares),
                price=_price(px),
                amount=_money(amount),
                fee=_money(fee),
                target_weight=_weight(pending.target_weights.get(code, 0.0)),
                reason="月度目标权重调仓卖出",
            )
        )
        trade_count += 1
        fee_total += fee
        record_order(
            code,
            "sell",
            (
                "filled"
                if sell_shares + 1e-9 >= requested_shares
                else "partially_filled"
            ),
            requested=requested_shares,
            filled=sell_shares,
            reason=(
                None
                if sell_shares + 1e-9 >= requested_shares
                else "成交量参与率限制"
            ),
        )

    db.flush()
    # 再按目标权重买入，A股按100股一手取整。
    positions = _position_rows(db, account.id)
    for code, target_weight in sorted(
        pending.target_weights.items(), key=lambda item: item[1], reverse=True
    ):
        bar = current.get(code)
        if bar is None:
            blocked.append(f"{code}: 成交日无行情")
            record_order(code, "buy", "blocked", reason="成交日无行情")
            continue
        px = stock_backtest.trade_price(bar, "buy", COST.slippage_rate)
        held = float(positions.get(code).shares) if code in positions else 0.0
        desired_shares = math.floor(
            max(target_weight * total_value / px, 0.0) / 100.0
        ) * 100.0
        buy_shares = desired_shares - held
        if buy_shares < 100.0:
            record_order(
                code,
                "buy",
                "filled",
                reason="整手取整后已达到目标",
            )
            continue
        if buy_shares * px / total_value < MINIMUM_TRADE_WEIGHT:
            record_order(
                code,
                "buy",
                "filled",
                requested=buy_shares,
                reason="剩余偏差低于最小交易权重",
            )
            continue
        ok, reason, bar = trade_allowed(code, "buy")
        if not ok or bar is None:
            blocked.append(f"{code}: {reason}")
            record_order(
                code, "buy", "blocked", requested=buy_shares, reason=reason
            )
            continue
        buy_shares = math.floor(buy_shares / 100.0) * 100.0
        requested_shares = buy_shares
        if bar.volume is not None:
            capacity = (
                math.floor(
                    max(bar.volume, 0.0)
                    * MAX_VOLUME_PARTICIPATION
                    / stock_backtest.BOARD_LOT
                )
                * stock_backtest.BOARD_LOT
            )
            if buy_shares > capacity:
                buy_shares = capacity
                blocked.append(f"{code}: 超过成交量参与率，买入部分成交")
        while buy_shares >= 100.0:
            amount = buy_shares * px
            fee = stock_backtest.trade_fee("buy", amount, COST)
            if amount + fee <= float(account.cash) + 1e-6:
                break
            buy_shares -= 100.0
        if buy_shares < 100.0:
            blocked.append(f"{code}: 现金不足以买入一手")
            record_order(
                code,
                "buy",
                "blocked",
                requested=requested_shares,
                reason="成交容量或现金不足一手",
            )
            continue
        px = stock_backtest.trade_price(
            bar,
            "buy",
            COST.slippage_rate,
            shares=buy_shares,
            volatility=stock_backtest.recent_volatility(histories.get(code, []), day),
            market_impact_coefficient=COST.market_impact_coefficient,
            volatility_slippage_coefficient=COST.volatility_slippage_coefficient,
            max_total_slippage=COST.max_total_slippage,
        )
        while buy_shares >= 100.0:
            amount = buy_shares * px
            fee = stock_backtest.trade_fee("buy", amount, COST)
            if amount + fee <= float(account.cash) + 1e-6:
                break
            buy_shares -= 100.0
        if buy_shares < 100.0:
            blocked.append(f"{code}: 动态冲击后现金不足以买入一手")
            record_order(
                code,
                "buy",
                "blocked",
                requested=requested_shares,
                reason="动态冲击后现金不足一手",
            )
            continue
        amount = buy_shares * px
        fee = stock_backtest.trade_fee("buy", amount, COST)
        position = positions.get(code)
        old_cost = float(position.cost) if position else 0.0
        if position is None:
            position = StockPaperPosition(
                account_id=account.id,
                stock_code=code,
                shares=_qty(0),
                cost=_money(0),
            )
            db.add(position)
            positions[code] = position
        position.shares = _qty(float(position.shares) + buy_shares)
        position.cost = _money(old_cost + amount + fee)
        account.cash = _money(float(account.cash) - amount - fee)
        db.add(
            StockPaperTrade(
                account_id=account.id,
                run_id=run.id,
                signal_id=pending.id,
                stock_code=code,
                trade_date=day,
                side="buy",
                shares=_qty(buy_shares),
                price=_price(px),
                amount=_money(amount),
                fee=_money(fee),
                target_weight=_weight(target_weight),
                reason="月度目标权重调仓买入",
            )
        )
        trade_count += 1
        fee_total += fee
        record_order(
            code,
            "buy",
            (
                "filled"
                if buy_shares + 1e-9 >= requested_shares
                else "partially_filled"
            ),
            requested=requested_shares,
            filled=buy_shares,
            reason=(
                None
                if buy_shares + 1e-9 >= requested_shares
                else "成交量参与率或现金约束"
            ),
        )

    db.flush()
    if not blocked:
        for code, item in order_state.items():
            if item.get("status") == "pending":
                item["status"] = "filled"
                item["last_reason"] = "无剩余可执行偏差"
                item.setdefault("events", []).append(
                    {
                        "date": day.isoformat(),
                        "status": "filled",
                        "reason": "无剩余可执行偏差",
                    }
                )
    pending.order_state = order_state
    pending.execute_on = pending.execute_on or day
    if blocked:
        pending.status = "pending"
        pending.warnings = list(pending.warnings) + [
            f"{day.isoformat()} 未完全成交，将在后续交易日重试："
            + "；".join(blocked[:10])
        ]
    else:
        pending.status = "executed"
        pending.executed_at = day
    return trade_count, fee_total, blocked


def _benchmark_nav(
    repository: StockRepository,
    codes: list[str],
    entry_date: date | None,
    day: date,
) -> float:
    """候选池等权买入持有基准，与策略在首个 execute_on 同时起算。"""
    if entry_date is None or day < entry_date:
        return 1.0
    bars = repository.daily_bars(codes, entry_date, day)
    by_code: dict[str, list[StockBar]] = {}
    for bar in bars:
        if not bar.suspended and bar.close > 0:
            by_code.setdefault(bar.code, []).append(bar)
    action_fn = getattr(repository, "corporate_actions", None)
    actions_by_code: dict[str, list[object]] = {}
    if callable(action_fn):
        for action in action_fn(codes, entry_date, day):
            actions_by_code.setdefault(action.code, []).append(action)
    relatives: list[float] = []
    for values in by_code.values():
        values.sort(key=lambda item: item.trade_date)
        entry = next(
            (item for item in values if item.trade_date == entry_date), None
        )
        base = (
            entry.open
            if entry is not None and entry.open is not None and entry.open > 0
            else entry.close if entry is not None else None
        )
        now = next(
            (item.close for item in reversed(values) if item.trade_date <= day), None
        )
        if base and now and base > 0:
            shares_count = 1.0 / base
            cash = 0.0
            code = values[0].code
            for action in actions_by_code.get(code, []):
                if action.kind == "terminal":
                    cash += shares_count * (action.terminal_price or now)
                    shares_count = 0.0
                elif action.kind in {"cash_entitlement", "distribution"}:
                    cash += shares_count * action.cash_per_share
                    if action.kind == "distribution":
                        shares_count *= 1.0 + action.share_ratio
                elif action.kind == "cash_payment":
                    continue
                elif action.kind == "rights_issue":
                    if (
                        action.subscription_price is not None
                        and action.subscription_price > 0
                    ):
                        subscribed = (
                            shares_count * action.subscription_ratio
                        )
                        cash -= subscribed * action.subscription_price
                        shares_count += subscribed
                elif action.kind in {"merger", "code_change"}:
                    successor = action.successor_code
                    if successor and action.share_ratio > 0:
                        successor_bars = repository.daily_bars(
                            [successor], entry_date, day
                        )
                        successor_price = next(
                            (
                                item.close
                                for item in reversed(successor_bars)
                                if item.trade_date <= day
                            ),
                            None,
                        )
                        if successor_price is not None:
                            cash += (
                                shares_count
                                * action.share_ratio
                                * successor_price
                            )
                            shares_count = 0.0
                else:
                    shares_count *= 1.0 + action.share_ratio
            relatives.append(cash + shares_count * now)
    return fmean(relatives) if relatives else 1.0


def _write_nav(
    db: Session,
    repository: StockRepository,
    account: StockPaperAccount,
    run: StockPaperRun,
    day: date,
    fee_total: float,
) -> StockPaperNavDaily:
    positions = _position_rows(db, account.id)
    names = {
        row.code: row
        for row in repository.list_stocks(list(positions))
    }
    current, histories = _bar_maps(repository, list(positions), day)
    total, values = _portfolio_value(db, account, positions, histories, day)
    market_value = sum(values.values())
    previous = db.scalar(
        select(StockPaperNavDaily)
        .where(StockPaperNavDaily.account_id == account.id)
        .order_by(StockPaperNavDaily.nav_date.desc())
        .limit(1)
    )
    daily_return = (
        total / float(previous.total_value) - 1.0
        if previous is not None and float(previous.total_value) > 0
        else None
    )
    first_signal = db.scalar(
        select(StockPaperSignal)
        .where(
            StockPaperSignal.account_id == account.id,
            StockPaperSignal.execute_on.is_not(None),
        )
        .order_by(StockPaperSignal.signal_date, StockPaperSignal.id)
        .limit(1)
    )
    benchmark_nav = _benchmark_nav(
        repository,
        list(account.candidate_codes),
        first_signal.execute_on if first_signal is not None else None,
        day,
    )
    previous_benchmark = float(previous.benchmark_nav) if previous else 1.0
    benchmark_return = (
        benchmark_nav / previous_benchmark - 1.0
        if previous_benchmark > 0
        else 0.0
    )
    account.benchmark_nav = _price(benchmark_nav)
    snapshot = []
    for code, position in sorted(positions.items()):
        value = values.get(code)
        price = current.get(code).close if code in current else None
        snapshot.append(
            {
                "code": code,
                "name": names.get(code).name if code in names else code,
                "industry": names.get(code).industry if code in names else "未知",
                "shares": float(position.shares),
                "cost": float(position.cost),
                "price": price,
                "market_value": value,
                "weight": value / total if value is not None and total > 0 else None,
            }
        )
    row = StockPaperNavDaily(
        account_id=account.id,
        run_id=run.id,
        nav_date=day,
        cash=_money(account.cash),
        market_value=_money(market_value),
        total_value=_money(total),
        nav=_price(total / float(account.initial_capital)),
        daily_return=_weight(daily_return) if daily_return is not None else None,
        cumulative_return=_weight(total / float(account.initial_capital) - 1.0),
        benchmark_nav=_price(account.benchmark_nav),
        benchmark_daily_return=_weight(benchmark_return),
        fee_total=_money(fee_total),
        rebalanced=run.rebalanced,
        positions=snapshot,
    )
    db.add(row)
    db.flush()
    return row


def _run_response(
    run: StockPaperRun, nav: StockPaperNavDaily, *, skipped: bool
) -> StockPaperRunResponse:
    return StockPaperRunResponse(
        account_id=run.account_id,
        run_date=run.run_date.isoformat(),
        skipped=skipped,
        status=run.status,
        signal_generated=run.signal_generated,
        rebalanced=run.rebalanced,
        trade_count=run.trade_count,
        total_value=float(nav.total_value),
        nav=float(nav.nav),
        benchmark_nav=float(nav.benchmark_nav),
        warnings=list(run.warnings),
    )


def run_cycle(db: Session) -> StockPaperRunResponse:
    """推进一个真实行情日；数据日不变时返回 skipped。"""
    readiness = get_readiness(db)
    if not readiness.ready or not readiness.latest_data_date:
        raise StockPaperError("；".join(readiness.blockers) or "股票数据尚未就绪")
    data_date = date.fromisoformat(readiness.latest_data_date)
    _persist_field_readiness(db, data_date)
    account, _version = _ensure_account(db, data_date)
    existing = db.scalar(
        select(StockPaperRun).where(
            StockPaperRun.account_id == account.id,
            StockPaperRun.run_date == data_date,
        )
    )
    if existing is not None:
        nav = db.scalar(
            select(StockPaperNavDaily).where(
                StockPaperNavDaily.account_id == account.id,
                StockPaperNavDaily.nav_date == data_date,
            )
        )
        if nav is None:
            raise StockPaperError("已有运行记录但缺少对应净值，账本不一致")
        return _run_response(existing, nav, skipped=True)

    previous = db.scalar(
        select(StockPaperNavDaily)
        .where(StockPaperNavDaily.account_id == account.id)
        .order_by(StockPaperNavDaily.nav_date.desc())
        .limit(1)
    )
    run_count = (
        db.scalar(
            select(func.count(StockPaperRun.id)).where(
                StockPaperRun.account_id == account.id
            )
        )
        or 0
    )
    run = StockPaperRun(
        account_id=account.id,
        run_date=data_date,
        trading_day_index=int(run_count) + 1,
        result={},
        warnings=list(readiness.warnings),
    )
    db.add(run)
    db.flush()
    repository = _repo(db)
    _apply_corporate_actions(db, repository, account, run, data_date)

    # 首日生成信号等待下一交易日；跨月首日用上月末已记账日生成并当日执行。
    previous_signal = (
        db.scalar(
            select(StockPaperSignal).where(
                StockPaperSignal.account_id == account.id,
                StockPaperSignal.signal_date == previous.nav_date,
            )
        )
        if previous is not None
        else None
    )
    crossed_month = previous is not None and (
        previous.nav_date.year,
        previous.nav_date.month,
    ) != (data_date.year, data_date.month)
    should_signal = previous is None or (crossed_month and previous_signal is None)
    if should_signal:
        signal_day = previous.nav_date if previous is not None else data_date
        if previous is not None:
            execute_on = data_date
        else:
            # 若首次账户是补录上一交易日数据（当前自然日已越过 signal_day），
            # 不能事后使用当前日开盘价；最早从下一自然日的真实交易日执行。
            created_day = now_cn().date()
            execute_on = (
                created_day + timedelta(days=1)
                if created_day > signal_day
                else None
            )
        _generate_signal(
            db, repository, account, run, signal_day, execute_on=execute_on
        )
        run.signal_generated = True

    trade_count, fee_total, blocked = _execute_pending(
        db, repository, account, run, data_date
    )
    run.trade_count = trade_count
    run.rebalanced = trade_count > 0
    if blocked:
        run.warnings = list(run.warnings) + [
            f"{len(blocked)} 个订单因停牌/涨跌停/现金约束等待后续交易日"
        ]
    nav = _write_nav(db, repository, account, run, data_date, fee_total)
    run_trades = db.scalars(
        select(StockPaperTrade).where(StockPaperTrade.run_id == run.id)
    ).all()
    traded_codes = sorted({trade.stock_code for trade in run_trades})
    execution_bars = {
        bar.code: bar
        for bar in repository.daily_bars(traded_codes, data_date, data_date)
    }
    slippage_cost = 0.0
    for trade in run_trades:
        bar = execution_bars.get(trade.stock_code)
        if bar is None:
            continue
        base = bar.open if bar.open is not None and bar.open > 0 else bar.close
        price = float(trade.price)
        shares_count = float(trade.shares)
        slippage_cost += (
            max(price - base, 0.0) * shares_count
            if trade.side == "buy"
            else max(base - price, 0.0) * shares_count
        )
    previous_positions = (
        {
            str(item.get("code")): dict(item)
            for item in list(previous.positions or [])
            if item.get("code")
        }
        if previous is not None
        else {}
    )
    prior_codes = sorted(previous_positions)
    prior_bars = {
        bar.code: bar
        for bar in (
            repository.daily_bars(prior_codes, data_date, data_date)
            if prior_codes
            else []
        )
    }
    stock_weights = {
        code: float(item.get("weight") or 0.0)
        for code, item in previous_positions.items()
    }
    stock_returns = {
        code: bar.close / float(previous_positions[code]["price"]) - 1.0
        for code, bar in prior_bars.items()
        if previous_positions[code].get("price")
        and float(previous_positions[code]["price"]) > 0
    }
    industries = {
        code: str(item.get("industry") or "未知")
        for code, item in previous_positions.items()
    }
    beta_deviation = 0.0
    active_signal = db.scalar(
        select(StockPaperSignal)
        .where(
            StockPaperSignal.account_id == account.id,
            StockPaperSignal.signal_date < data_date,
        )
        .order_by(StockPaperSignal.signal_date.desc())
        .limit(1)
    )
    if active_signal is not None and active_signal.run_id is not None:
        signal_run = db.get(StockPaperRun, active_signal.run_id)
        diagnostics = (
            dict(signal_run.result or {}).get("portfolio_diagnostics", {})
            if signal_run is not None
            else {}
        )
        deviations = (
            dict(diagnostics).get("exposure_deviations", {})
            if isinstance(diagnostics, dict)
            else {}
        )
        raw_beta = (
            dict(deviations).get("beta")
            if isinstance(deviations, dict)
            else None
        )
        if isinstance(raw_beta, (int, float)) and math.isfinite(raw_beta):
            beta_deviation = float(raw_beta)
    attribution = stock_backtest.account_period_attribution(
        float(previous.total_value) if previous is not None else float(nav.total_value),
        float(nav.total_value),
        fee_total,
        slippage_cost,
        benchmark_return=float(nav.benchmark_daily_return or 0.0),
        stock_weights=stock_weights,
        stock_returns=stock_returns,
        industries=industries,
        beta_deviation=beta_deviation,
        cash_weight=(
            float(previous.cash) / float(previous.total_value)
            if previous is not None and float(previous.total_value) > 0
            else 1.0
        ),
    )
    run.result = {
        **dict(run.result or {}),
        "total_value": float(nav.total_value),
        "nav": float(nav.nav),
        "benchmark_nav": float(nav.benchmark_nav),
        "fee_total": fee_total,
        "slippage_cost": slippage_cost,
        "attribution": attribution,
    }
    if data_date >= account.trial_end:
        account.status = "evaluation_due"
        expiring = db.scalars(
            select(StockPaperSignal).where(
                StockPaperSignal.account_id == account.id,
                StockPaperSignal.status == "pending",
            )
        ).all()
        for signal in expiring:
            signal.status = "expired"
            state = {
                code: {
                    **dict(item),
                    "events": list(dict(item).get("events", [])),
                }
                for code, item in dict(signal.order_state or {}).items()
            }
            for item in state.values():
                if item.get("status") not in {
                    "filled",
                    "cancelled",
                    "superseded",
                }:
                    item["status"] = "expired"
                    item["last_reason"] = "两个月前向观察期结束"
                    item["events"].append(
                        {
                            "date": data_date.isoformat(),
                            "status": "expired",
                            "reason": "两个月前向观察期结束",
                        }
                    )
            signal.order_state = state
    db.commit()
    return _run_response(run, nav, skipped=False)


def _metrics(rows: list[StockPaperNavDaily], db: Session, account_id: int) -> StockPaperMetrics:
    if not rows:
        return StockPaperMetrics()
    navs = [float(row.nav) for row in rows]
    bench = [float(row.benchmark_nav) for row in rows]
    returns = [
        navs[i] / navs[i - 1] - 1.0
        for i in range(1, len(navs))
        if navs[i - 1] > 0
    ]
    bench_returns = [
        bench[i] / bench[i - 1] - 1.0
        for i in range(1, len(bench))
        if bench[i - 1] > 0
    ]
    total_return = navs[-1] / navs[0] - 1.0 if navs[0] > 0 else None
    benchmark_return = bench[-1] / bench[0] - 1.0 if bench[0] > 0 else None
    max_drawdown = stats.max_drawdown(navs)
    annual_volatility = None
    if len(returns) >= 2:
        mean = fmean(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (
            len(returns) - 1
        )
        annual_volatility = math.sqrt(variance) * math.sqrt(252)
    trade_count = (
        db.scalar(
            select(func.count(StockPaperTrade.id)).where(
                StockPaperTrade.account_id == account_id
            )
        )
        or 0
    )
    rebalance_count = sum(1 for row in rows if row.rebalanced)
    fees = sum(float(row.fee_total) for row in rows)
    return StockPaperMetrics(
        total_return=total_return,
        benchmark_return=benchmark_return,
        excess_return=(
            total_return - benchmark_return
            if total_return is not None and benchmark_return is not None
            else None
        ),
        annual_return=stats.annualized_return(total_return, len(returns))
        if total_return is not None and returns
        else None,
        annual_volatility=annual_volatility,
        max_drawdown=max_drawdown,
        sharpe=stats.sharpe_ratio(returns),
        win_rate=(
            sum(1 for value in returns if value > 0) / len(returns)
            if returns
            else None
        ),
        information_ratio=stats.information_ratio(returns, bench_returns),
        trading_days=len(rows),
        rebalance_count=rebalance_count,
        trade_count=int(trade_count),
        total_fees=round(fees, 2),
    )


def _signal_out(row: StockPaperSignal | None) -> StockPaperSignalOut | None:
    if row is None:
        return None
    return StockPaperSignalOut(
        id=row.id,
        signal_date=row.signal_date.isoformat(),
        execute_on=row.execute_on.isoformat() if row.execute_on else None,
        status=row.status,
        universe_count=row.universe_count,
        selected_count=row.selected_count,
        invested_weight=float(row.invested_weight),
        items=list(row.items),
        order_state=dict(row.order_state or {}),
        warnings=list(row.warnings),
    )


def get_summary(db: Session) -> StockPaperSummary:
    readiness = get_readiness(db)
    account = db.scalar(
        select(StockPaperAccount).order_by(StockPaperAccount.id.desc()).limit(1)
    )
    if account is None:
        return StockPaperSummary(
            started=False,
            readiness=readiness,
            warnings=list(readiness.warnings) + list(readiness.blockers),
        )
    version = db.get(StrategyVersion, account.strategy_version_id)
    rows = list(
        db.scalars(
            select(StockPaperNavDaily)
            .where(StockPaperNavDaily.account_id == account.id)
            .order_by(StockPaperNavDaily.nav_date)
        ).all()
    )
    latest = rows[-1] if rows else None
    signal = db.scalar(
        select(StockPaperSignal)
        .where(StockPaperSignal.account_id == account.id)
        .order_by(StockPaperSignal.signal_date.desc())
        .limit(1)
    )
    today = latest.nav_date if latest else account.trial_start
    total_days = max((account.trial_end - account.trial_start).days, 1)
    elapsed = max(min((today - account.trial_start).days, total_days), 0)
    positions: list[StockPaperPositionOut] = []
    for item in (latest.positions if latest else []):
        market_value = item.get("market_value")
        cost = float(item.get("cost") or 0.0)
        positions.append(
            StockPaperPositionOut(
                code=item["code"],
                name=item.get("name") or item["code"],
                industry=item.get("industry") or "未知",
                shares=float(item.get("shares") or 0.0),
                cost=cost,
                price=item.get("price"),
                market_value=market_value,
                weight=item.get("weight"),
                pnl=(
                    float(market_value) - cost
                    if market_value is not None
                    else None
                ),
            )
        )
    warnings = list(readiness.warnings)
    if len(rows) < 20:
        warnings.append(
            f"目前只有 {len(rows)} 个前向交易日，至少观察两个月后再评价策略"
        )
    if account.status == "evaluation_due":
        warnings.append("两个月观察期已到，请结合超额收益、回撤和换手率做阶段评估")
    return StockPaperSummary(
        started=True,
        account_id=account.id,
        account_name=account.name,
        as_of=latest.nav_date.isoformat() if latest else None,
        initial_capital=float(account.initial_capital),
        cash=float(latest.cash) if latest else float(account.cash),
        market_value=float(latest.market_value) if latest else 0.0,
        total_value=float(latest.total_value)
        if latest
        else float(account.initial_capital),
        nav=float(latest.nav) if latest else 1.0,
        benchmark_nav=float(latest.benchmark_nav) if latest else 1.0,
        strategy=StockPaperStrategyInfo(
            version_id=version.id if version else 0,
            name=version.name if version else STRATEGY_NAME,
            status=account.status,
            trial_start=account.trial_start.isoformat(),
            trial_end=account.trial_end.isoformat(),
            calendar_days_elapsed=elapsed,
            calendar_days_remaining=max(total_days - elapsed, 0),
            observation_progress=round(elapsed / total_days, 6),
            candidate_count=len(account.candidate_codes),
            params=dict(version.params) if version else {},
        ),
        readiness=readiness,
        metrics=_metrics(rows, db, account.id),
        positions=positions,
        latest_signal=_signal_out(signal),
        history=[
            StockPaperHistoryPoint(
                date=row.nav_date.isoformat(),
                nav=float(row.nav),
                benchmark_nav=float(row.benchmark_nav),
                total_value=float(row.total_value),
                daily_return=float(row.daily_return)
                if row.daily_return is not None
                else None,
                benchmark_daily_return=float(row.benchmark_daily_return)
                if row.benchmark_daily_return is not None
                else None,
                rebalanced=row.rebalanced,
            )
            for row in rows
        ],
        warnings=warnings,
    )


def list_trades(db: Session, limit: int = 500) -> list[dict]:
    rows = db.execute(
        select(StockPaperTrade, StockPaperSignal, StockMaster.name)
        .join(StockPaperSignal, StockPaperSignal.id == StockPaperTrade.signal_id)
        .outerjoin(StockMaster, StockMaster.code == StockPaperTrade.stock_code)
        .order_by(StockPaperTrade.trade_date.desc(), StockPaperTrade.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": trade.id,
            "trade_date": trade.trade_date.isoformat(),
            "signal_date": signal.signal_date.isoformat(),
            "code": trade.stock_code,
            "name": name or trade.stock_code,
            "side": trade.side,
            "shares": float(trade.shares),
            "price": float(trade.price),
            "amount": float(trade.amount),
            "fee": float(trade.fee),
            "target_weight": float(trade.target_weight),
            "reason": trade.reason,
        }
        for trade, signal, name in rows
    ]
