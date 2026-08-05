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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from statistics import fmean

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CorporateActionReviewCase,
    IndexConstituent,
    StockDailyBar,
    StockFinancialIndicator,
    StockIndustry,
    StockMaster,
    StockPaperAccount,
    StockPaperCashSettlement,
    StockPaperDividendTaxLiability,
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
    cash_ledger,
    corporate_actions,
    experiment_registry,
    order_lifecycle,
    position_lots,
    stock_backtest,
    stock_factors,
    stock_strategy,
    stock_validation,
    strategy_lifecycle,
    strategy_mandate,
    trading_rules,
)
from app.services.quant_data_governance import save_readiness
from app.services.execution_reference_sync import sla_health
from app.services.index_reference_sync import source_health as index_source_health
from app.services.source_reconciliation import reconciliation_gate
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
ORDER_POLICY = order_lifecycle.OrderLifecyclePolicy()
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
    "A股规则多因子两个月运行链路验证（operational_only）："
    "候选池在账户创建时冻结为沪深300+中证500"
    "全部当前成分，且启动前逐只校验日线、行业、财务和PE/PB估值覆盖；"
    "动态剔除ST/停牌/次新/低流动性；"
    "质量30%（含ROA/应计/稳定性）、价值25%（含SP/股息/FCF）、"
    "动量20%（12-1/6-1/反转/残差）、趋势15%、低风险10%，"
    "行业内缩尾和标准化；月频调仓，自由流通市值行业基准，约束单股5%、"
    "单行业20%、市值/Beta/流动性和ADV容量，持仓保留区与最小交易权重"
    "降低抖动；T日收盘生成信号，T+1开盘成交，含最低佣金、卖出印花税、"
    "波动与成交参与率动态滑点；部分成交、公司行为、停牌及涨跌停顺延。"
    "现金按CNY_CASH_FLAT_2PCT_ACT365_V1在每日开盘前计息，区分可用、冻结、"
    "股息应收与已结算可取资金，并逐日做现金守恒校验。"
    "只验收数据、调度、信号、模拟成交、账本、对账、告警和恢复；"
    "短期收益不构成Alpha证据，不得据此批准实盘，不构成投资建议，不产生真实订单。"
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
        select(
            StockDailyBar.last_trade_date, func.count(func.distinct(StockDailyBar.code))
        )
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
                StockFinancialIndicator.report_date >= latest - timedelta(days=550)
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
        valuation_ready = sum(1 for _code, count in valuation_rows if int(count) == 2)

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
        blockers.append(f"行业覆盖 {industry_ready}/{required}，必须全部就绪后才能启动")
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
    reference_health = sla_health(db)
    reference_health.update(index_source_health(db))
    source_health.update(reference_health)
    for dataset, health in reference_health.items():
        if not health["ready"]:
            blockers.append(
                f"{dataset} 未通过 SLA：{health['status']}，"
                f"安全动作={health['safe_action']}"
            )
        elif health["degraded"]:
            warnings.append(
                f"{dataset} 使用备用源 {health['active_source']}，"
                "本次运行标记为 degraded"
            )
    if latest is not None:
        reconciliation = reconciliation_gate(db, as_of=latest)
        source_health["cross_source_reconciliation"] = reconciliation
        if not reconciliation["ready"]:
            blockers.append(
                f"跨源复核存在 {reconciliation['blocking']} 个未解决阻断，"
                f"安全动作={reconciliation['safe_action']}"
            )
        if reconciliation["degraded"]:
            warnings.append(
                f"跨源复核有 {reconciliation['degraded']} 个字段使用备用源"
            )
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


def _persist_field_readiness(
    db: Session,
    signal_date: date,
    *,
    strategy_version_id: int,
    data_snapshot_sha256: str,
) -> dict[str, object]:
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
    valuation = {code for code, count in valuation_rows if int(count) == 2}
    reference_health = sla_health(db)
    reference_health.update(index_source_health(db))
    reconciliation = reconciliation_gate(db, as_of=signal_date)
    blocking_by_code = reconciliation["blocking_by_code"]
    rows = {
        code: {
            "daily_same_period": code in daily,
            "industry_sw2021": code in industry,
            "financial_recent": code in financial,
            "pe_pb_recent_non_null": code in valuation,
            **{
                f"{dataset}_sla": bool(health["ready"])
                for dataset, health in reference_health.items()
            },
            "cross_source_reconciled": code not in blocking_by_code,
            "reference_dataset_detail": reference_health,
            "cross_source_detail": {
                "blocking": blocking_by_code.get(code, []),
                "safe_action": reconciliation["safe_action"],
            },
        }
        for code in universe
    }
    return save_readiness(
        db,
        STRATEGY_NAME,
        signal_date,
        rows,
        strategy_version_id=strategy_version_id,
        data_snapshot_sha256=data_snapshot_sha256,
    )


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


def _ensure_account(
    db: Session, data_date: date
) -> tuple[StockPaperAccount, StrategyVersion]:
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
            if version.status not in {
                "paper_operational_validation",
                "paper",
            }:
                raise StockPaperError(
                    f"策略版本状态为 {version.status}，不是可运行的前向模拟状态"
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
        hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.exists() else None
    )
    candidate_sha256 = hashlib.sha256(
        json.dumps(candidates, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    params = {
        "asset": "cn_stock",
        "model_version": "stock_rules_v4",
        "purpose": "two_month_forward_paper_validation",
        "validation_scope": "operational_only",
        "investment_approval_eligible": False,
        "approval_blocker": (
            "该版本仅验证运行链路；必须创建绑定投资任务书的新版本并通过"
            "净超额、主动风险、IC显著性、DSR/PBO和成本压力门禁"
        ),
        "indices": list(INDEX_CODES),
        "universe_mode": "frozen_at_trial_start",
        "production_universe_mode": "dynamic_as_of_signal_date",
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
            "volatility_slippage_coefficient": (COST.volatility_slippage_coefficient),
            "max_total_slippage": COST.max_total_slippage,
        },
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
        },
        "methodology": METHODOLOGY,
    }
    if version is None:
        mandate = strategy_mandate.operational_validation_mandate(
            strategy_name=STRATEGY_NAME,
            initial_capital=INITIAL_CAPITAL,
            rebalance_days=20,
            top_n=TOP_N,
        )
        version = StrategyVersion(
            name=STRATEGY_NAME,
            initial_capital=INITIAL_CAPITAL,
            rebalance_interval=20,
            fee_rate=_weight(COST.commission_rate),
            top_n=TOP_N,
            params=params,
            mandate=mandate,
            mandate_sha256=strategy_mandate.mandate_sha256(mandate),
            status=(
                "research" if REQUIRE_PREVALIDATION else "paper_operational_validation"
            ),
        )
        db.add(version)
        db.commit()
        db.refresh(version)
    if version.status not in {"paper_operational_validation", "paper"}:
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
        frozen_cash=Decimal("0"),
        settled_cash=INITIAL_CAPITAL,
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
    """用系统证据晋级到运行链路模拟；不授予投资有效性或实盘资格。"""
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
    if version.status in {"paper_operational_validation", "paper"}:
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
        changed = [line[3:] for line in git_status.splitlines() if len(line) > 3]
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
        mandate = strategy_mandate.operational_validation_mandate(
            strategy_name=STRATEGY_NAME,
            initial_capital=INITIAL_CAPITAL,
            rebalance_days=20,
            top_n=TOP_N,
        )
        version = StrategyVersion(
            name=STRATEGY_NAME,
            initial_capital=INITIAL_CAPITAL,
            rebalance_interval=20,
            fee_rate=_weight(COST.commission_rate),
            top_n=TOP_N,
            params=successor_params,
            mandate=mandate,
            mandate_sha256=strategy_mandate.mandate_sha256(mandate),
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
            "git_status_sha256": hashlib.sha256(git_status.encode()).hexdigest(),
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
        benchmark_index="H00906",
        benchmark_required=True,
        benchmark_return_kind="gross_total_return",
        min_limit_data_coverage=0.99,
        cost=COST,
    )
    experiment = experiment_registry.preregister_experiment(
        db,
        experiment_key=(
            f"paper-prevalidation-v{version.id}-"
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
        ),
        hypothesis="规则多因子参数在严格走步样本外满足运行与风险门禁",
        parameter_space={
            "top_n": top_n_grid or [TOP_N],
            "max_stock_weight": max_stock_weight_grid or [MAX_STOCK_WEIGHT],
            "embargo_days": embargo_days,
        },
        target_metrics=[
            "net_excess_return",
            "active_sharpe",
            "rank_ic",
            "tracking_error",
        ],
        data_scope={"start": start.isoformat(), "end": end.isoformat()},
        actor="system:stock-paper-prepare",
    )
    experiment_registry.start_experiment(db, experiment.id)
    db.commit()
    from app.services import holdout_registry

    holdout_start, holdout_end = stock_validation.planned_holdout_interval(
        repository, start, end
    )
    try:
        holdout_registry.assert_pristine(db, holdout_start, holdout_end)
    except ValueError as exc:
        experiment_registry.finalize_experiment(
            db,
            experiment.id,
            status="abandoned",
            summary={"error": str(exc), "holdout_contaminated": True},
        )
        db.commit()
        raise StockPaperError(str(exc)) from exc
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
        experiment_registry.record_trial(
            db,
            experiment_id=experiment.id,
            trial_key="validation-run-failed",
            factor_spec={"model": "rules_multifactor"},
            parameters={
                "top_n": top_n_grid or [TOP_N],
                "max_stock_weight": max_stock_weight_grid
                or [MAX_STOCK_WEIGHT],
            },
            status="failed",
            error=str(exc),
        )
        experiment_registry.finalize_experiment(
            db,
            experiment.id,
            status="failed",
            summary={"error": str(exc)},
        )
        db.commit()
        raise StockPaperError(str(exc)) from exc
    for index, trial in enumerate(validation.get("trials", [])):
        experiment_registry.record_trial(
            db,
            experiment_id=experiment.id,
            trial_key=f"grid-{index:04d}",
            factor_spec={"model": "rules_multifactor"},
            parameters=dict(trial.get("params") or {}),
            status="completed",
            metrics={"score": trial.get("score")},
            score_series=[
                float(item["sharpe"])
                for item in trial.get("folds", [])
                if item.get("sharpe") is not None
            ],
        )
    experiment_registry.finalize_experiment(
        db,
        experiment.id,
        status="completed",
        summary={"best_params": validation.get("best_params")},
    )
    params = dict(version.params or {})
    params["research_experiment_id"] = experiment.id
    params["validation"] = validation
    params["validation_sha256"] = stock_validation.validation_sha256(validation)
    consumption = holdout_registry.consume(
        db,
        experiment_id=experiment.id,
        strategy_version_id=version.id,
        interval_start=holdout_start,
        interval_end=holdout_end,
        purpose="formal_holdout_evaluation",
        result_sha256=params["validation_sha256"],
        actor="system:stock-paper-prepare",
    )
    params["holdout_consumption_id"] = consumption.id
    params["holdout_consumption_status"] = consumption.status
    params["frozen_adaptive_factor_weights"] = validation.get(
        "frozen_adaptive_factor_weights"
    )
    version.params = params
    db.commit()

    holdout = dict(validation.get("holdout", {}))
    evidence = {
        "data_coverage": validation.get("minimum_data_coverage", 0.0),
        "limit_data_coverage": min(
            float(holdout.get("execution_limit_data_coverage") or 0.0),
            float(
                dict(validation.get("validation", {})).get(
                    "execution_limit_data_coverage"
                )
                or 0.0
            ),
        ),
        "holdout_evaluations": validation.get("holdout_evaluations"),
        "walkforward_folds": len(validation.get("folds", [])),
        "holdout_sharpe": holdout.get("sharpe"),
        "benchmark_kind": holdout.get("benchmark_kind"),
        "benchmark_code": holdout.get("benchmark_code"),
        "benchmark_name": holdout.get("benchmark_name"),
        "benchmark_return_kind": holdout.get("benchmark_return_kind"),
        "benchmark_source": holdout.get("benchmark_source"),
        "benchmark_source_files": holdout.get("benchmark_source_files"),
        "benchmark_source_hashes": holdout.get("benchmark_source_hashes"),
        "benchmark_source_rows": holdout.get("benchmark_source_rows"),
        "benchmark_source_first_date": holdout.get("benchmark_source_first_date"),
        "benchmark_source_last_date": holdout.get("benchmark_source_last_date"),
        "benchmark_curve_sha256": holdout.get("benchmark_curve_sha256"),
        "benchmark_start_date": holdout.get("benchmark_start_date"),
        "benchmark_end_date": holdout.get("benchmark_end_date"),
        "benchmark_curve_points": holdout.get("benchmark_curve_points"),
        "strategy_curve_sha256": holdout.get("strategy_curve_sha256"),
        "comparator_metrics": holdout.get("comparator_metrics"),
        "benchmark_return": holdout.get("benchmark_return"),
        "net_excess_return": holdout.get("net_excess_return"),
        "active_sharpe": holdout.get("active_sharpe"),
        "tracking_error": holdout.get("tracking_error"),
        "annualized_alpha": holdout.get("annualized_alpha"),
        "beta": holdout.get("beta"),
        "up_capture": holdout.get("up_capture"),
        "down_capture": holdout.get("down_capture"),
        "probability_backtest_overfitting": validation.get(
            "probability_backtest_overfitting"
        ),
        "cscv_pbo": validation.get("cscv_pbo"),
        "effective_trial_count": holdout.get("effective_trial_count"),
        "return_skewness": holdout.get("return_skewness"),
        "return_excess_kurtosis": holdout.get(
            "return_excess_kurtosis"
        ),
        "probabilistic_sharpe_probability": holdout.get(
            "probabilistic_sharpe_probability"
        ),
        "deflated_sharpe_probability": holdout.get(
            "deflated_sharpe_probability"
        ),
        "minimum_track_record_length": holdout.get(
            "minimum_track_record_length"
        ),
        "rank_ic_mean": holdout.get("rank_ic_mean"),
        "rank_icir": holdout.get("rank_icir"),
        "rank_ic_p_value": holdout.get("rank_ic_p_value"),
        "rank_ic_ci_lower": holdout.get("rank_ic_ci_lower"),
        "rank_ic_effective_observations": holdout.get(
            "rank_ic_effective_observations"
        ),
        "multiple_testing_fdr": holdout.get("multiple_testing_fdr"),
        "alpha_evidence_status": holdout.get("alpha_evidence_status"),
        "quintile_monotonicity": holdout.get(
            "quintile_monotonicity"
        ),
        "top_bottom_spread": holdout.get("top_bottom_spread"),
        "top_bottom_ci_lower": holdout.get("top_bottom_ci_lower"),
        "top_bottom_hit_rate": holdout.get("top_bottom_hit_rate"),
        "quintile_gate_status": holdout.get("quintile_gate_status"),
        "active_return_newey_west_t": holdout.get(
            "active_return_newey_west_t"
        ),
        "active_return_ci_lower": holdout.get("active_return_ci_lower"),
        "regression_alpha_ci_lower": holdout.get(
            "regression_alpha_ci_lower"
        ),
        "active_alpha_gate_status": holdout.get(
            "active_alpha_gate_status"
        ),
        "worst_year_excess_return": holdout.get(
            "worst_year_excess_return"
        ),
        "worst_regime_excess_return": holdout.get(
            "worst_regime_excess_return"
        ),
        "max_single_period_alpha_contribution": holdout.get(
            "max_single_period_alpha_contribution"
        ),
        "best_year_removed_excess_return": holdout.get(
            "best_year_removed_excess_return"
        ),
        "stability_gate_status": holdout.get("stability_gate_status"),
        "robustness_passed": holdout.get("robustness_passed"),
        "robustness_neighbor_pass_rate": holdout.get(
            "robustness_neighbor_pass_rate"
        ),
        "cost_2x_excess_return": holdout.get(
            "cost_2x_excess_return"
        ),
        "robustness_gate_status": holdout.get(
            "robustness_gate_status"
        ),
        "validation_scope": "operational_only",
        "validation_sha256": params["validation_sha256"],
        "generated_by": "stock_validation.run_stock_walk_forward",
    }
    params["operational_validation_evidence"] = evidence
    version.params = params
    db.commit()
    try:
        version = strategy_lifecycle.transition(
            db,
            version.id,
            "operational_validated",
            evidence=evidence,
            actor="system:stock-paper-prepare",
            reason=(
                "系统生成的历史走步与完全留出运行链路验证通过；"
                "该结果不代表投资Alpha有效"
            ),
        )
        version = strategy_lifecycle.transition(
            db,
            version.id,
            "paper_operational_validation",
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
            reason=(
                "参数、代码、候选池、数据与运行验证结果均已冻结；"
                "仅启动两个月运行链路模拟"
            ),
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
    restricted_codes = {
        row.stock_code
        for row in db.scalars(
            select(StockPaperPosition).where(
                StockPaperPosition.account_id == account.id,
                StockPaperPosition.status == "restricted",
            )
        ).all()
    }
    infos = [
        info
        for info in repository.list_stocks(codes)
        if info.code not in restricted_codes
    ]
    industry_fn = getattr(repository, "industries_as_of", None)
    if callable(industry_fn):
        historical = industry_fn(codes, [signal_date]).get(signal_date, {})
        if not historical:
            raise StockPaperError(f"{signal_date.isoformat()} 缺少申万2021历史行业归属")
        infos = [
            replace(info, industry=historical.get(info.code, "未知")) for info in infos
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
    version = db.get(StrategyVersion, account.strategy_version_id)
    frozen_weights = (
        dict((version.params or {}).get("frozen_adaptive_factor_weights") or {})
        if version is not None
        else {}
    )
    scored = stock_factors.compute_cross_section(
        contexts,
        signal_date,
        weights=frozen_weights or None,
    )
    from app.services.factor_health import persist_factor_health_reports

    factor_health_reports = persist_factor_health_reports(
        db,
        scored,
        signal_date=signal_date,
        strategy_version_id=account.strategy_version_id,
        direction_map=dict(stock_factors._FACTOR_DIRECTION),
    )
    blocked_factor_health = [
        report for report in factor_health_reports if report.blocked
    ]
    if REQUIRE_PREVALIDATION and blocked_factor_health:
        sample = "；".join(
            f"{item.factor}:{'/'.join(item.reasons)}"
            for item in blocked_factor_health[:5]
        )
        raise StockPaperError(f"因子分布健康门禁失败：{sample}")
    positions = _position_rows(db, account.id)
    _total, values = _portfolio_value(
        db, account, positions, panel.bars_by_code, signal_date
    )
    current_weights = (
        {code: value / _total for code, value in values.items()} if _total > 0 else {}
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
            use_convex_optimizer=REQUIRE_PREVALIDATION,
        )
    except stock_strategy.IndustryCoverageError as exc:
        raise StockPaperError(str(exc)) from exc
    ranked = sorted(scored, key=lambda item: item.composite, reverse=True)
    from app.services.financial_ratio_policy import persist_factor_policy_issues

    persist_factor_policy_issues(db, ranked, signal_date=signal_date)
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
                "factor_metadata": dict(item.factor_metadata),
                "model_structure": dict(item.model_structure),
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
                "target_weight": plan.target_weights.get(code, 0.0),
                "status": "pending",
                "attempts": 0,
                "filled_shares": 0.0,
                "remaining_shares": (
                    abs(
                        plan.target_weights.get(code, 0.0)
                        - current_weights.get(code, 0.0)
                    )
                    * _total
                    / decision_price
                    if (
                        decision_price := stock_backtest._last_price_before(
                            panel.bars_by_code.get(code, []),
                            signal_date,
                            None,
                        )
                    )
                    else 0.0
                ),
                "side": (
                    "buy"
                    if plan.target_weights.get(code, 0.0)
                    >= current_weights.get(code, 0.0)
                    else "sell"
                ),
                "decision_price": decision_price,
                "last_market_price": decision_price,
                "order_lifecycle_version": ORDER_POLICY.version,
                "events": [],
            }
            for code in set(current_weights) | set(plan.target_weights)
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


def _paper_lot_ledger(position: StockPaperPosition) -> position_lots.LotLedger:
    rows = list(position.lots or [])
    if not rows and float(position.shares) > 0:
        rows = [
            {
                "acquired_date": "1970-01-01",
                "sellable_date": "1970-01-02",
                "shares": float(position.shares),
                "total_cost": float(position.cost),
                "source": "legacy_aggregate_position",
            }
        ]
    return position_lots.LotLedger.from_payload({position.stock_code: rows})


def _portfolio_value(
    db: Session,
    account: StockPaperAccount,
    positions: dict[str, StockPaperPosition],
    histories: dict[str, list[StockBar]],
    day: date,
) -> tuple[float, dict[str, float]]:
    values: dict[str, float] = {}
    for code, position in positions.items():
        if position.status == "restricted":
            values[code] = float(position.restricted_value)
            continue
        price = stock_backtest._last_price_before(histories.get(code, []), day, None)
        if price is not None:
            values[code] = float(position.shares) * price
    receivables = db.scalar(
        select(func.sum(StockPaperReceivable.amount)).where(
            StockPaperReceivable.account_id == account.id,
            StockPaperReceivable.status == "receivable",
        )
    ) or Decimal("0")
    return (
        float(account.cash) + float(receivables) + sum(values.values()),
        values,
    )


def _sync_cash_account(
    account: StockPaperAccount,
    ledger: cash_ledger.CashLedger,
) -> None:
    account.cash = _money(ledger.available)
    account.frozen_cash = _money(ledger.frozen)
    account.settled_cash = _money(ledger.settled)


def _open_corporate_action_review(
    db: Session,
    *,
    code: str,
    event_key: str,
    issue_type: str,
    reason: str,
    conservative_value: float,
    evidence: dict[str, object],
) -> CorporateActionReviewCase:
    existing = db.scalar(
        select(CorporateActionReviewCase).where(
            CorporateActionReviewCase.code == code,
            CorporateActionReviewCase.event_key == event_key,
            CorporateActionReviewCase.issue_type == issue_type,
        )
    )
    if existing is not None:
        return existing
    row = CorporateActionReviewCase(
        code=code,
        event_key=event_key,
        issue_type=issue_type,
        status="open",
        reason=reason,
        conservative_value=conservative_value,
        evidence=evidence,
        resolution={},
        created_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row


def _apply_corporate_actions(
    db: Session,
    repository: StockRepository,
    account: StockPaperAccount,
    run: StockPaperRun,
    day: date,
    ledger: cash_ledger.CashLedger,
) -> None:
    """把当日公司行为记入模拟账本；run 的日期唯一约束保证幂等。"""
    positions = _position_rows(db, account.id)
    released = []
    for position in positions.values():
        if (
            position.status == "restricted"
            and position.sellable_after is not None
            and position.sellable_after <= day
        ):
            position.status = "tradable"
            position.restricted_shares = _qty(0)
            position.restricted_value = _money(0)
            position.restriction_reason = None
            released.append(position.stock_code)
    action_fn = getattr(repository, "corporate_actions", None)
    if not callable(action_fn):
        run.warnings = list(run.warnings) + ["仓储不支持公司行为，需人工核对"]
        return
    action_codes = sorted(set(positions) | set(account.candidate_codes))
    actions = list(action_fn(action_codes, day, day))
    applied: list[dict[str, object]] = [
        {
            "code": code,
            "kind": "restriction_released",
            "date": day.isoformat(),
        }
        for code in released
    ]
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
                ledger.settle_receivable(
                    day,
                    float(_money(receivable.amount)),
                    f"dividend:{event_key}",
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
        lot_ledger = _paper_lot_ledger(position)
        if action.kind == "terminal":
            resolution = corporate_actions.resolve_terminal(
                terminal_type=action.terminal_type,
                terminal_price=action.terminal_price,
                consideration_status=action.consideration_status,
                restricted_valuation_per_share=(action.restricted_valuation_per_share),
            )
            if resolution.action != "cash_settlement":
                position.status = "restricted"
                position.restricted_shares = _qty(held)
                position.restricted_value = _money(
                    held * resolution.restricted_value_per_share
                )
                position.restriction_reason = resolution.reason
                position.sellable_after = None
                _open_corporate_action_review(
                    db,
                    code=action.code,
                    event_key=event_key,
                    issue_type="terminal_consideration_unknown",
                    reason=resolution.reason,
                    conservative_value=float(position.restricted_value),
                    evidence={
                        "terminal_type": action.terminal_type,
                        "consideration_status": action.consideration_status,
                        "source": action.source,
                        "source_hash": action.source_hash,
                    },
                )
                run.warnings = list(run.warnings) + [
                    f"{action.code} 退市持仓已转为受限资产并按"
                    f" {resolution.restricted_value_per_share:.4f}/股保守估值："
                    f"{resolution.reason}"
                ]
                applied.append(
                    {
                        "code": action.code,
                        "kind": "terminal_restricted",
                        "terminal_type": action.terminal_type,
                        "shares": held,
                        "restricted_value": float(position.restricted_value),
                        "reason": resolution.reason,
                        "rule_version": resolution.rule_version,
                        "source": action.source,
                    }
                )
                continue
            price = resolution.cash_per_share
            ledger.receive_cash(
                day,
                float(_money(held * price)),
                f"terminal:{event_key}",
                event_type="terminal_cash_received",
            )
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
            cash_amount = float(_money(held * action.cash_per_share))
            tax_claims = corporate_actions.create_dividend_tax_claims(
                code=action.code,
                event_key=event_key,
                entitlement_date=day,
                gross_cash_per_share=action.cash_per_share,
                lots=list(lot_ledger.lots(action.code)),
            )
            for claim in tax_claims:
                existing_claim = db.scalar(
                    select(StockPaperDividendTaxLiability).where(
                        StockPaperDividendTaxLiability.account_id == account.id,
                        StockPaperDividendTaxLiability.event_key == event_key,
                        StockPaperDividendTaxLiability.lot_id == claim.lot_id,
                    )
                )
                if existing_claim is None:
                    db.add(
                        StockPaperDividendTaxLiability(
                            account_id=account.id,
                            stock_code=action.code,
                            event_key=event_key,
                            lot_id=claim.lot_id,
                            acquired_date=claim.acquired_date,
                            entitlement_date=claim.entitlement_date,
                            remaining_shares=_qty(claim.remaining_shares),
                            gross_cash_per_share=_price(claim.gross_cash_per_share),
                            withheld_at_payment=_money(0),
                            tax_paid=_money(0),
                            rule_version=claim.rule_version,
                            status="open",
                        )
                    )
            if action.payment_date is None or action.payment_date <= day:
                ledger.receive_cash(
                    day,
                    cash_amount,
                    f"dividend:{event_key}",
                    event_type="dividend_received",
                )
            else:
                existing = db.scalar(
                    select(StockPaperReceivable).where(
                        StockPaperReceivable.account_id == account.id,
                        StockPaperReceivable.event_key == event_key,
                    )
                )
                if existing is None:
                    ledger.recognize_receivable(
                        day,
                        cash_amount,
                        f"dividend:{event_key}",
                    )
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
            cash_amount = float(_money(held * action.cash_per_share))
            ledger.receive_cash(
                day,
                cash_amount,
                f"distribution:{event_key}",
                event_type="distribution_cash_received",
            )
            new_shares = held * action.share_ratio
            lot_ledger.distribute_shares(
                action.code,
                action.share_ratio,
                action_date=day,
                source=action.source,
            )
            conversion = corporate_actions.convert_registered_shares(
                raw_shares=lot_ledger.total(action.code),
                cash_compensation_per_fraction=(action.cash_compensation_per_fraction),
            )
            lot_ledger.scale_total(
                action.code,
                conversion.registered_shares,
            )
            if conversion.cash_compensation > 0:
                ledger.receive_cash(
                    day,
                    float(_money(conversion.cash_compensation)),
                    f"fractional_compensation:{event_key}",
                    event_type="fractional_cash_compensation",
                )
            if conversion.restricted_fractional_value > 0:
                position.restricted_shares = _qty(
                    float(position.restricted_shares)
                    + conversion.restricted_fractional_value
                )
                position.status = "tradable_with_restricted_fraction"
                position.restriction_reason = "送转零碎股缺少官方现金补偿"
                _open_corporate_action_review(
                    db,
                    code=action.code,
                    event_key=event_key,
                    issue_type="fractional_share_compensation",
                    reason=position.restriction_reason,
                    conservative_value=0.0,
                    evidence={
                        "fractional_shares": conversion.fractional_shares,
                        "source": action.source,
                    },
                )
            position.shares = _qty(lot_ledger.total(action.code))
            position.lots = lot_ledger.to_payload().get(action.code, [])
            applied.append(
                {
                    "code": action.code,
                    "kind": "distribution",
                    "cash": cash_amount,
                    "new_shares": new_shares,
                    "registered_shares": conversion.registered_shares,
                    "fractional_shares": conversion.fractional_shares,
                    "cash_compensation": conversion.cash_compensation,
                    "rule_version": conversion.rule_version,
                    "source": action.source,
                }
            )
        elif action.kind == "rights_issue":
            price = action.subscription_price
            if price is None or price <= 0:
                run.warnings = list(run.warnings) + [
                    f"{action.code} 配股事件字段不完整，需人工处理"
                ]
                continue
            _current, rights_histories = _bar_maps(
                repository,
                list(positions),
                day,
            )
            rights_portfolio_value, _values = _portfolio_value(
                db,
                account,
                positions,
                rights_histories,
                day,
            )
            decision = corporate_actions.decide_rights_issue(
                held_shares=held,
                subscription_ratio=action.subscription_ratio,
                subscription_price=price,
                available_cash=ledger.available,
                portfolio_value=rights_portfolio_value,
                rights_tradable=action.rights_tradable,
                right_market_price=action.right_market_price,
            )
            conversion = corporate_actions.convert_registered_shares(
                raw_shares=decision.subscribed_shares,
                cash_compensation_per_fraction=None,
            )
            subscribed = conversion.registered_shares
            subscription_cash = float(_money(subscribed * price))
            if subscription_cash > 0:
                ledger.debit_cash(
                    day,
                    subscription_cash,
                    f"rights_issue:{event_key}",
                    event_type="rights_subscription",
                )
            if decision.rights_sale_cash > 0:
                ledger.receive_cash(
                    day,
                    float(_money(decision.rights_sale_cash)),
                    f"rights_sale:{event_key}",
                    event_type="rights_sale",
                )
            if subscribed > 0:
                lot_ledger.buy(
                    action.code,
                    subscribed,
                    subscribed * price,
                    acquired_date=day,
                    sellable_date=(
                        repository.trade_calendar(day, None).next_trade_day(day)
                        or position_lots.next_calendar_settlement_day(day)
                    ),
                    source=f"rights_issue:{action.source}",
                )
            position.shares = _qty(lot_ledger.total(action.code))
            position.cost = _money(float(position.cost) + subscribed * price)
            position.lots = lot_ledger.to_payload().get(action.code, [])
            applied.append(
                {
                    "code": action.code,
                    "kind": "rights_issue",
                    "requested_shares": decision.requested_shares,
                    "subscribed_shares": subscribed,
                    "sold_rights": decision.sold_rights,
                    "lapsed_rights": (
                        decision.lapsed_rights + conversion.fractional_shares
                    ),
                    "rights_sale_cash": decision.rights_sale_cash,
                    "subscription_price": price,
                    "policy_version": decision.policy_version,
                    "reason": decision.reason,
                    "source": action.source,
                }
            )
            if subscribed + decision.sold_rights + 1e-9 < decision.requested_shares:
                run.warnings = list(run.warnings) + [
                    f"{action.code} 配股按 {decision.policy_version} 仅认购"
                    f" {subscribed:.3f}/{decision.requested_shares:.3f} 股；"
                    f"{decision.reason}"
                ]
        elif action.kind in {"merger", "code_change"}:
            successor = action.successor_code
            if not successor or action.share_ratio <= 0:
                run.warnings = list(run.warnings) + [
                    f"{action.code} 换股合并字段不完整，需人工处理"
                ]
                continue
            raw_converted = held * action.share_ratio
            successor_listing_date = action.successor_listing_date
            if successor_listing_date is None:
                successor_day_bars = repository.daily_bars(
                    [successor],
                    day,
                    day,
                )
                if successor_day_bars:
                    successor_listing_date = day
            conversion = corporate_actions.convert_registered_shares(
                raw_shares=raw_converted,
                cash_compensation_per_fraction=(action.cash_compensation_per_fraction),
            )
            converted = conversion.registered_shares
            successor_position = positions.get(successor)
            old_lots = list(lot_ledger.remove(action.code))
            raw_total = sum(lot.shares * action.share_ratio for lot in old_lots)
            converted_lots = [
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
            ]
            if successor_position is None:
                successor_position = StockPaperPosition(
                    account_id=account.id,
                    stock_code=successor,
                    shares=_qty(0),
                    cost=_money(0),
                    lots=[],
                    status="tradable",
                    restricted_shares=_qty(0),
                    restricted_value=_money(0),
                )
                db.add(successor_position)
                positions[successor] = successor_position
            successor_position.cost = _money(
                float(successor_position.cost) + float(position.cost)
            )
            successor_ledger = _paper_lot_ledger(successor_position)
            successor_ledger.replace_lots(
                successor,
                list(successor_ledger.lots(successor)) + converted_lots,
            )
            successor_position.shares = _qty(successor_ledger.total(successor))
            successor_position.lots = successor_ledger.to_payload().get(successor, [])
            if successor_listing_date is None:
                successor_position.status = "restricted"
                successor_position.restricted_shares = _qty(converted)
                successor_position.restricted_value = _money(0)
                successor_position.restriction_reason = (
                    "换股新证券上市日期未知，等待人工核对"
                )
                _open_corporate_action_review(
                    db,
                    code=successor,
                    event_key=event_key,
                    issue_type="successor_listing_unknown",
                    reason=successor_position.restriction_reason,
                    conservative_value=0.0,
                    evidence={
                        "predecessor": action.code,
                        "share_ratio": action.share_ratio,
                        "source": action.source,
                    },
                )
            elif successor_listing_date > day:
                successor_position.status = "restricted"
                successor_position.restricted_shares = _qty(converted)
                successor_position.restricted_value = _money(0)
                successor_position.restriction_reason = "换股新证券上市前受限"
                successor_position.sellable_after = successor_listing_date
            if conversion.cash_compensation > 0:
                ledger.receive_cash(
                    day,
                    float(_money(conversion.cash_compensation)),
                    f"fractional_compensation:{event_key}",
                    event_type="fractional_cash_compensation",
                )
            if conversion.restricted_fractional_value > 0:
                successor_position.restricted_shares = _qty(
                    float(successor_position.restricted_shares)
                    + conversion.restricted_fractional_value
                )
                _open_corporate_action_review(
                    db,
                    code=successor,
                    event_key=event_key,
                    issue_type="fractional_share_compensation",
                    reason="换股零碎股缺少官方现金补偿",
                    conservative_value=0.0,
                    evidence={
                        "fractional_shares": conversion.fractional_shares,
                        "source": action.source,
                    },
                )
            for tax_row in db.scalars(
                select(StockPaperDividendTaxLiability).where(
                    StockPaperDividendTaxLiability.account_id == account.id,
                    StockPaperDividendTaxLiability.stock_code == action.code,
                    StockPaperDividendTaxLiability.status == "open",
                )
            ).all():
                tax_row.stock_code = successor
            db.delete(position)
            positions.pop(action.code, None)
            applied.append(
                {
                    "code": action.code,
                    "kind": "merger",
                    "successor_code": successor,
                    "converted_shares": converted,
                    "fractional_shares": conversion.fractional_shares,
                    "cash_compensation": conversion.cash_compensation,
                    "restricted_fractional_shares": (
                        conversion.restricted_fractional_value
                    ),
                    "successor_listing_date": (
                        successor_listing_date.isoformat()
                        if successor_listing_date
                        else None
                    ),
                    "rule_version": conversion.rule_version,
                    "share_ratio": action.share_ratio,
                    "source": action.source,
                }
            )
        else:
            new_shares = held * action.share_ratio
            lot_ledger.distribute_shares(
                action.code,
                action.share_ratio,
                action_date=day,
                source=action.source,
            )
            conversion = corporate_actions.convert_registered_shares(
                raw_shares=lot_ledger.total(action.code),
                cash_compensation_per_fraction=(action.cash_compensation_per_fraction),
            )
            lot_ledger.scale_total(
                action.code,
                conversion.registered_shares,
            )
            if conversion.cash_compensation > 0:
                ledger.receive_cash(
                    day,
                    float(_money(conversion.cash_compensation)),
                    f"fractional_compensation:{event_key}",
                    event_type="fractional_cash_compensation",
                )
            if conversion.restricted_fractional_value > 0:
                position.restricted_shares = _qty(
                    float(position.restricted_shares)
                    + conversion.restricted_fractional_value
                )
                position.status = "tradable_with_restricted_fraction"
                position.restriction_reason = "送转零碎股缺少官方现金补偿"
                _open_corporate_action_review(
                    db,
                    code=action.code,
                    event_key=event_key,
                    issue_type="fractional_share_compensation",
                    reason=position.restriction_reason,
                    conservative_value=0.0,
                    evidence={
                        "fractional_shares": conversion.fractional_shares,
                        "source": action.source,
                    },
                )
            position.shares = _qty(lot_ledger.total(action.code))
            position.lots = lot_ledger.to_payload().get(action.code, [])
            applied.append(
                {
                    "code": action.code,
                    "kind": "share_distribution",
                    "new_shares": new_shares,
                    "registered_shares": conversion.registered_shares,
                    "fractional_shares": conversion.fractional_shares,
                    "cash_compensation": conversion.cash_compensation,
                    "rule_version": conversion.rule_version,
                    "source": action.source,
                }
            )
    _sync_cash_account(account, ledger)
    if applied:
        run.result = {**dict(run.result or {}), "corporate_actions": applied}
        db.flush()


def _execute_pending(
    db: Session,
    repository: StockRepository,
    account: StockPaperAccount,
    run: StockPaperRun,
    day: date,
    ledger: cash_ledger.CashLedger,
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
        for code, item in state.items():
            if item.get("status") not in {"filled", "cancelled", "superseded"}:
                cost = order_lifecycle.opportunity_cost(
                    side=str(item.get("side", "buy")),
                    unfilled_shares=float(item.get("remaining_shares", 0.0)),
                    decision_price=(
                        float(item["decision_price"])
                        if item.get("decision_price") is not None
                        else None
                    ),
                    current_price=(
                        float(item["last_market_price"])
                        if item.get("last_market_price") is not None
                        else None
                    ),
                )
                item["status"] = "superseded"
                item["last_reason"] = "被新一期目标权重覆盖"
                item.update(cost)
                item["events"].append(
                    {
                        "date": day.isoformat(),
                        "code": code,
                        "status": "superseded",
                        "reason": "被新一期目标权重覆盖",
                        "order_lifecycle_version": ORDER_POLICY.version,
                        **cost,
                    }
                )
        row.order_state = state

    positions = _position_rows(db, account.id)
    codes = sorted(set(positions) | set(pending.target_weights))
    infos = {item.code: item for item in repository.list_stocks(codes)}
    current, histories = _bar_maps(repository, codes, day)
    total_value, position_values = _portfolio_value(
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
    lifecycle_days = max(
        len(repository.trade_calendar(pending.signal_date, day).days) - 1,
        0,
    )
    effective_targets: dict[str, float] = {}
    active_codes: set[str] = set()

    for code in codes:
        item = order_state.setdefault(
            code,
            {
                "target_weight": float(pending.target_weights.get(code, 0.0)),
                "status": "pending",
                "attempts": 0,
                "filled_shares": 0.0,
                "order_lifecycle_version": ORDER_POLICY.version,
                "events": [],
            },
        )
        if item.get("status") in order_lifecycle.TERMINAL_STATUSES:
            continue
        existing_position = positions.get(code)
        if existing_position is not None and existing_position.status == "restricted":
            item["status"] = "cancelled"
            item["last_reason"] = (
                existing_position.restriction_reason or "证券处于受限状态"
            )
            item.setdefault("events", []).append(
                {
                    "date": day.isoformat(),
                    "status": "cancelled",
                    "reason": item["last_reason"],
                    "order_lifecycle_version": ORDER_POLICY.version,
                }
            )
            continue
        decision_bar = next(
            (
                known
                for known in histories.get(code, [])
                if known.trade_date == pending.signal_date
            ),
            None,
        )
        decision_price = (
            float(item["decision_price"])
            if item.get("decision_price") is not None
            else (
                decision_bar.close
                if decision_bar is not None and decision_bar.close > 0
                else None
            )
        )
        market_bar = current.get(code)
        market_price = (
            market_bar.open
            if market_bar is not None
            and market_bar.open is not None
            and market_bar.open > 0
            else None
        )
        item["decision_price"] = decision_price
        item["last_market_price"] = market_price
        attempts = int(item.get("attempts", 0))
        current_weight = (
            float(position_values.get(code, 0.0)) / total_value
            if total_value > 0
            else 0.0
        )
        original_target = float(pending.target_weights.get(code, 0.0))
        effective_target, strength = order_lifecycle.decayed_target_weight(
            current_weight=current_weight,
            original_target_weight=original_target,
            attempts_before_execution=attempts,
            policy=ORDER_POLICY,
        )
        side = "buy" if effective_target >= current_weight else "sell"
        item["side"] = side
        item["signal_strength"] = strength
        item["effective_target_weight"] = effective_target
        if order_lifecycle.is_expired(
            attempts=attempts,
            trading_days_elapsed=lifecycle_days,
            policy=ORDER_POLICY,
        ):
            cost = order_lifecycle.opportunity_cost(
                side=side,
                unfilled_shares=float(item.get("remaining_shares", 0.0)),
                decision_price=decision_price,
                current_price=market_price,
            )
            item.update(
                {
                    "status": "expired",
                    "last_reason": (
                        f"超过订单TTL {ORDER_POLICY.ttl_trading_days} 个交易日"
                        f"或最大重试 {ORDER_POLICY.max_attempts} 次"
                    ),
                    **cost,
                }
            )
            item.setdefault("events", []).append(
                {
                    "date": day.isoformat(),
                    "side": side,
                    "status": "expired",
                    "attempts": attempts,
                    "trading_days_elapsed": lifecycle_days,
                    "reason": item["last_reason"],
                    "order_lifecycle_version": ORDER_POLICY.version,
                    **cost,
                }
            )
            continue
        cancel, deviation = order_lifecycle.should_cancel_for_price_deviation(
            decision_price,
            market_price,
            ORDER_POLICY,
        )
        if cancel:
            cost = order_lifecycle.opportunity_cost(
                side=side,
                unfilled_shares=float(item.get("remaining_shares", 0.0)),
                decision_price=decision_price,
                current_price=market_price,
            )
            item.update(
                {
                    "status": "price_deviation_cancelled",
                    "last_reason": (
                        f"开盘价相对决策价偏离 {deviation:.2%}，"
                        f"超过 {ORDER_POLICY.max_price_deviation:.2%}"
                    ),
                    **cost,
                }
            )
            item.setdefault("events", []).append(
                {
                    "date": day.isoformat(),
                    "side": side,
                    "status": "price_deviation_cancelled",
                    "attempts": attempts + 1,
                    "reason": item["last_reason"],
                    "order_lifecycle_version": ORDER_POLICY.version,
                    **cost,
                }
            )
            continue
        effective_targets[code] = effective_target
        active_codes.add(code)

    def record_order(
        code: str,
        side: str,
        status_value: str,
        *,
        requested: float = 0.0,
        filled: float = 0.0,
        reason: str | None = None,
        quantity_rule_version: str | None = None,
    ) -> None:
        item = order_state.setdefault(
            code,
            {
                "target_weight": float(pending.target_weights.get(code, 0.0)),
                "status": "pending",
                "attempts": 0,
                "filled_shares": 0.0,
                "order_lifecycle_version": ORDER_POLICY.version,
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
        item["quantity_rule_version"] = quantity_rule_version
        market_bar = current.get(code)
        market_price = (
            market_bar.open
            if market_bar is not None and market_bar.open is not None
            else None
        )
        item["last_market_price"] = market_price
        cost = order_lifecycle.opportunity_cost(
            side=side,
            unfilled_shares=max(requested - filled, 0.0),
            decision_price=(
                float(item["decision_price"])
                if item.get("decision_price") is not None
                else None
            ),
            current_price=market_price,
        )
        item.update(cost)
        item.setdefault("events", []).append(
            {
                "date": day.isoformat(),
                "side": side,
                "status": status_value,
                "requested_shares": requested,
                "filled_shares": filled,
                "remaining_shares": max(requested - filled, 0.0),
                "reason": reason,
                "quantity_rule_version": quantity_rule_version,
                "attempts": int(item.get("attempts", 0)),
                "signal_strength": item.get("signal_strength"),
                "order_lifecycle_version": ORDER_POLICY.version,
                **cost,
            }
        )

    def trade_allowed(code: str, side: str) -> tuple[bool, str, StockBar | None]:
        bar = current.get(code)
        prev = stock_backtest.prev_bar_before(histories.get(code, []), day)
        info = infos.get(code, StockInfo(code=code, name=code))
        st = st_status_as_of(info.name, name_histories.get(code), day)
        listing_session = None
        if info.list_date is not None:
            listing_session = sum(
                info.list_date <= item.trade_date <= day
                for item in histories.get(code, [])
            )
        ok, reason = stock_backtest.can_trade(
            bar,
            prev.close if prev else None,
            side,
            PRICE_LIMIT_COEFFICIENT,
            code=code,
            st=st,
            listing_session=listing_session,
            delisting_period="退" in info.name,
        )
        return ok, reason, bar

    # 先卖出释放现金。
    for code, position in list(positions.items()):
        if code not in active_codes:
            continue
        bar = current.get(code)
        if bar is None:
            blocked.append(f"{code}: 成交日无行情")
            record_order(code, "sell", "blocked", reason="成交日无行情")
            continue
        ok, reason, bar = trade_allowed(code, "sell")
        if not ok or bar is None:
            blocked.append(f"{code}: {reason}")
            record_order(code, "sell", "blocked", requested=0.0, reason=reason)
            continue
        quantity_rule = trading_rules.quantity_rule(code, day)
        px = stock_backtest.trade_price(bar, "sell", COST.slippage_rate)
        effective_target = effective_targets.get(code, 0.0)
        desired_value = effective_target * total_value
        desired_shares = quantity_rule.normalize_buy(max(desired_value / px, 0.0))
        held = float(position.shares)
        lot_ledger = _paper_lot_ledger(position)
        sell_shares = held - desired_shares
        if effective_target <= 0:
            sell_shares = held
        else:
            sell_shares = quantity_rule.normalize_sell(max(sell_shares, 0.0), held)
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
        requested_shares = sell_shares
        sellable_shares = lot_ledger.available(code, day)
        if sell_shares > sellable_shares:
            sell_shares = sellable_shares
            blocked.append(f"{code}: 受 T+1 可卖批次限制，卖出部分成交")
        opening_adv = stock_backtest.prior_adv_volume(histories.get(code, []), day)
        capacity = quantity_rule.normalize_sell(
            opening_adv * MAX_VOLUME_PARTICIPATION, held
        )
        if sell_shares > capacity:
            sell_shares = capacity
            blocked.append(f"{code}: 超过开盘前历史ADV参与率，卖出部分成交")
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
            available_volume=opening_adv,
            volatility=stock_backtest.recent_volatility(histories.get(code, []), day),
            market_impact_coefficient=COST.market_impact_coefficient,
            volatility_slippage_coefficient=COST.volatility_slippage_coefficient,
            max_total_slippage=COST.max_total_slippage,
        )
        amount = sell_shares * px
        fee_detail = stock_backtest.trade_fee_breakdown(
            "sell",
            amount,
            COST,
            code=code,
            trade_date=day,
            shares=sell_shares,
        )
        fee = fee_detail.total
        decision_bar = next(
            (
                item
                for item in histories.get(code, [])
                if item.trade_date == pending.signal_date
            ),
            None,
        )
        tca = stock_backtest.execution_tca_fields(
            side="sell",
            fill_price=px,
            bar=bar,
            decision_price=(decision_bar.close if decision_bar is not None else None),
            shares=sell_shares,
            available_volume=opening_adv,
            recent_volatility_value=stock_backtest.recent_volatility(
                histories.get(code, []), day
            ),
        )
        consumed_lots = lot_ledger.sell(code, sell_shares, trade_date=day)
        tax_rows = db.scalars(
            select(StockPaperDividendTaxLiability).where(
                StockPaperDividendTaxLiability.account_id == account.id,
                StockPaperDividendTaxLiability.stock_code == code,
                StockPaperDividendTaxLiability.status == "open",
            )
        ).all()
        tax_claims = [
            corporate_actions.DividendTaxClaim(
                event_key=row.event_key,
                code=row.stock_code,
                lot_id=row.lot_id,
                acquired_date=row.acquired_date,
                entitlement_date=row.entitlement_date,
                remaining_shares=float(row.remaining_shares),
                gross_cash_per_share=float(row.gross_cash_per_share),
                rule_version=row.rule_version,
                withheld_at_payment=float(row.withheld_at_payment),
            )
            for row in tax_rows
        ]
        dividend_tax, dividend_tax_details = corporate_actions.realize_dividend_tax(
            claims=tax_claims,
            consumed_lots=consumed_lots,
            sale_date=day,
        )
        dividend_tax = float(_money(dividend_tax))
        for row, claim in zip(tax_rows, tax_claims, strict=True):
            row.remaining_shares = _qty(claim.remaining_shares)
            paid_for_claim = sum(
                float(item["tax_due"])
                for item in dividend_tax_details
                if item["event_key"] == row.event_key and item["lot_id"] == row.lot_id
            )
            row.tax_paid = _money(float(row.tax_paid) + paid_for_claim)
            if claim.remaining_shares <= 1e-9:
                row.status = "settled"
        consumed_cost = sum(float(item["cost"]) for item in consumed_lots)
        position.shares = _qty(lot_ledger.total(code))
        position.cost = _money(max(float(position.cost) - consumed_cost, 0.0))
        position.lots = lot_ledger.to_payload().get(code, [])
        if float(position.shares) <= 1e-6:
            db.delete(position)
            positions.pop(code, None)
        cash_reference = f"paper_sell:{run.id}:{code}:{trade_count + 1}"
        net_proceeds = float(_money(amount - fee))
        ledger.receive_cash(
            day,
            net_proceeds,
            cash_reference,
            settled=False,
            event_type="stock_sale_proceeds",
            fee=fee,
        )
        if dividend_tax > 0:
            ledger.debit_cash(
                day,
                dividend_tax,
                f"dividend_tax:{cash_reference}",
                event_type="dividend_tax_clawback",
                fee=dividend_tax,
            )
        settle_date = repository.trade_calendar(day, None).next_trade_day(
            day
        ) or position_lots.next_calendar_settlement_day(day)
        db.add(
            StockPaperCashSettlement(
                account_id=account.id,
                trade_date=day,
                settle_date=settle_date,
                amount=_money(net_proceeds - dividend_tax),
                reference=cash_reference,
                status="pending",
            )
        )
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
                fee_rule_version=fee_detail.rule_version,
                fee_breakdown={
                    "commission": fee_detail.commission,
                    "stamp_tax": fee_detail.stamp_tax,
                    "transfer_fee": fee_detail.transfer_fee,
                    "dividend_tax": dividend_tax,
                    "dividend_tax_details": dividend_tax_details,
                },
                lot_consumption=consumed_lots,
                **tca,
                slippage_model_version="OPEN_ADV_SQRT_V1",
                cost_scenario="baseline",
                target_weight=_weight(effective_target),
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
                None if sell_shares + 1e-9 >= requested_shares else "成交量参与率限制"
            ),
            quantity_rule_version=quantity_rule.version,
        )

    db.flush()
    # 再按成交日对应交易所申报规则买入。
    positions = _position_rows(db, account.id)
    for code, target_weight in sorted(
        pending.target_weights.items(), key=lambda item: item[1], reverse=True
    ):
        if code not in active_codes:
            continue
        target_weight = effective_targets[code]
        bar = current.get(code)
        if bar is None:
            blocked.append(f"{code}: 成交日无行情")
            record_order(code, "buy", "blocked", reason="成交日无行情")
            continue
        ok, reason, bar = trade_allowed(code, "buy")
        if not ok or bar is None:
            blocked.append(f"{code}: {reason}")
            record_order(code, "buy", "blocked", reason=reason)
            continue
        quantity_rule = trading_rules.quantity_rule(code, day)
        px = stock_backtest.trade_price(bar, "buy", COST.slippage_rate)
        held = float(positions.get(code).shares) if code in positions else 0.0
        desired_shares = quantity_rule.normalize_buy(
            max(target_weight * total_value / px, 0.0)
        )
        buy_shares = quantity_rule.normalize_buy(desired_shares - held)
        if buy_shares < quantity_rule.buy_minimum:
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
        buy_shares = quantity_rule.normalize_buy(buy_shares)
        requested_shares = buy_shares
        opening_adv = stock_backtest.prior_adv_volume(histories.get(code, []), day)
        capacity = quantity_rule.normalize_buy(opening_adv * MAX_VOLUME_PARTICIPATION)
        if buy_shares > capacity:
            buy_shares = capacity
            blocked.append(f"{code}: 超过开盘前历史ADV参与率，买入部分成交")
        while buy_shares >= quantity_rule.buy_minimum:
            amount = buy_shares * px
            fee = stock_backtest.trade_fee(
                "buy",
                amount,
                COST,
                code=code,
                trade_date=day,
                shares=buy_shares,
            )
            if amount + fee <= ledger.available + 1e-6:
                break
            buy_shares -= quantity_rule.buy_increment
        if buy_shares < quantity_rule.buy_minimum:
            blocked.append(f"{code}: 现金不足以买入一手")
            record_order(
                code,
                "buy",
                "blocked",
                requested=requested_shares,
                reason="成交容量或现金不足一手",
                quantity_rule_version=quantity_rule.version,
            )
            continue
        px = stock_backtest.trade_price(
            bar,
            "buy",
            COST.slippage_rate,
            shares=buy_shares,
            available_volume=opening_adv,
            volatility=stock_backtest.recent_volatility(histories.get(code, []), day),
            market_impact_coefficient=COST.market_impact_coefficient,
            volatility_slippage_coefficient=COST.volatility_slippage_coefficient,
            max_total_slippage=COST.max_total_slippage,
        )
        while buy_shares >= quantity_rule.buy_minimum:
            amount = buy_shares * px
            fee = stock_backtest.trade_fee(
                "buy",
                amount,
                COST,
                code=code,
                trade_date=day,
                shares=buy_shares,
            )
            if amount + fee <= ledger.available + 1e-6:
                break
            buy_shares -= quantity_rule.buy_increment
        if buy_shares < quantity_rule.buy_minimum:
            blocked.append(f"{code}: 动态冲击后现金不足以买入一手")
            record_order(
                code,
                "buy",
                "blocked",
                requested=requested_shares,
                reason="动态冲击后现金不足一手",
                quantity_rule_version=quantity_rule.version,
            )
            continue
        amount = buy_shares * px
        fee_detail = stock_backtest.trade_fee_breakdown(
            "buy",
            amount,
            COST,
            code=code,
            trade_date=day,
            shares=buy_shares,
        )
        fee = fee_detail.total
        decision_bar = next(
            (
                item
                for item in histories.get(code, [])
                if item.trade_date == pending.signal_date
            ),
            None,
        )
        tca = stock_backtest.execution_tca_fields(
            side="buy",
            fill_price=px,
            bar=bar,
            decision_price=(decision_bar.close if decision_bar is not None else None),
            shares=buy_shares,
            available_volume=opening_adv,
            recent_volatility_value=stock_backtest.recent_volatility(
                histories.get(code, []), day
            ),
        )
        position = positions.get(code)
        old_cost = float(position.cost) if position else 0.0
        if position is None:
            position = StockPaperPosition(
                account_id=account.id,
                stock_code=code,
                shares=_qty(0),
                cost=_money(0),
                lots=[],
            )
            db.add(position)
            positions[code] = position
        lot_ledger = _paper_lot_ledger(position)
        lot_ledger.buy(
            code,
            buy_shares,
            amount + fee,
            acquired_date=day,
            sellable_date=(
                repository.trade_calendar(day, None).next_trade_day(day)
                or position_lots.next_calendar_settlement_day(day)
            ),
            source=f"stock_paper_fill:{run.id}",
        )
        position.shares = _qty(lot_ledger.total(code))
        position.cost = _money(old_cost + amount + fee)
        position.lots = lot_ledger.to_payload().get(code, [])
        cash_reference = f"paper_buy:{run.id}:{code}:{trade_count + 1}"
        cash_required = float(_money(amount + fee))
        ledger.reserve(day, cash_required, cash_reference)
        ledger.consume_reservation(
            day,
            cash_required,
            cash_reference,
            fee=fee,
        )
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
                fee_rule_version=fee_detail.rule_version,
                fee_breakdown={
                    "commission": fee_detail.commission,
                    "stamp_tax": fee_detail.stamp_tax,
                    "transfer_fee": fee_detail.transfer_fee,
                },
                **tca,
                slippage_model_version="OPEN_ADV_SQRT_V1",
                cost_scenario="baseline",
                target_weight=_weight(target_weight),
                reason="月度目标权重调仓买入",
            )
        )
        trade_count += 1
        fee_total += fee
        record_order(
            code,
            "buy",
            ("filled" if buy_shares + 1e-9 >= requested_shares else "partially_filled"),
            requested=requested_shares,
            filled=buy_shares,
            reason=(
                None
                if buy_shares + 1e-9 >= requested_shares
                else "成交量参与率或现金约束"
            ),
            quantity_rule_version=quantity_rule.version,
        )

    _sync_cash_account(account, ledger)
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
        statuses = {str(item.get("status", "pending")) for item in order_state.values()}
        if trade_count == 0 and "expired" in statuses:
            pending.status = "expired"
        elif trade_count == 0 and "price_deviation_cancelled" in statuses:
            pending.status = "cancelled"
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
        entry = next((item for item in values if item.trade_date == entry_date), None)
        base = (
            entry.open
            if entry is not None and entry.open is not None and entry.open > 0
            else entry.close
            if entry is not None
            else None
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
                    resolution = corporate_actions.resolve_terminal(
                        terminal_type=action.terminal_type,
                        terminal_price=action.terminal_price,
                        consideration_status=action.consideration_status,
                        restricted_valuation_per_share=(
                            action.restricted_valuation_per_share
                        ),
                    )
                    cash += shares_count * (
                        resolution.cash_per_share
                        if resolution.action == "cash_settlement"
                        else resolution.restricted_value_per_share
                    )
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
                        decision = corporate_actions.decide_rights_issue(
                            held_shares=shares_count,
                            subscription_ratio=action.subscription_ratio,
                            subscription_price=action.subscription_price,
                            available_cash=max(cash, 0.0),
                            portfolio_value=1.0,
                            rights_tradable=action.rights_tradable,
                            right_market_price=action.right_market_price,
                        )
                        subscribed = decision.subscribed_shares
                        cash += (
                            decision.rights_sale_cash
                            - subscribed * action.subscription_price
                        )
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
                            cash += shares_count * action.share_ratio * successor_price
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
    ledger: cash_ledger.CashLedger,
) -> StockPaperNavDaily:
    _sync_cash_account(account, ledger)
    ledger.assert_conserved()
    ledger_audit = ledger.conservation()
    positions = _position_rows(db, account.id)
    names = {row.code: row for row in repository.list_stocks(list(positions))}
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
        benchmark_nav / previous_benchmark - 1.0 if previous_benchmark > 0 else 0.0
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
        frozen_cash=_money(ledger.frozen),
        receivable_cash=_money(ledger.receivable),
        settled_cash=_money(ledger.settled),
        cash_interest=_money(sum(item.interest for item in ledger.events)),
        cash_ledger=ledger_audit,
        cash_conservation_error=Decimal(str(ledger_audit["conservation_error"])),
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
    run.result = {
        **dict(run.result or {}),
        "cash_ledger": ledger_audit,
    }
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
    account, version = _ensure_account(db, data_date)
    snapshot_sha256 = str(
        version.params.get("stocktoday_manifest_sha256")
        or version.params.get("candidate_sha256")
        or ""
    )
    _persist_field_readiness(
        db,
        data_date,
        strategy_version_id=version.id,
        data_snapshot_sha256=snapshot_sha256,
    )
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
    receivable_balance = db.scalar(
        select(func.sum(StockPaperReceivable.amount)).where(
            StockPaperReceivable.account_id == account.id,
            StockPaperReceivable.status == "receivable",
        )
    ) or Decimal("0")
    ledger = cash_ledger.CashLedger(
        available=float(account.cash),
        frozen=float(account.frozen_cash),
        receivable=float(receivable_balance),
        settled=float(account.settled_cash),
    )
    elapsed_calendar_days = (
        (data_date - previous.nav_date).days if previous is not None else 0
    )
    ledger.accrue_interest(
        data_date,
        calendar_days=elapsed_calendar_days,
    )
    settling_rows = db.scalars(
        select(StockPaperCashSettlement).where(
            StockPaperCashSettlement.account_id == account.id,
            StockPaperCashSettlement.status == "pending",
            StockPaperCashSettlement.settle_date <= data_date,
        )
    ).all()
    for settlement in settling_rows:
        ledger.settle_sale_proceeds(
            data_date,
            float(settlement.amount),
            settlement.reference,
        )
        settlement.status = "settled"
        settlement.settled_at = data_date
    _sync_cash_account(account, ledger)
    _apply_corporate_actions(
        db,
        repository,
        account,
        run,
        data_date,
        ledger,
    )

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
                created_day + timedelta(days=1) if created_day > signal_day else None
            )
        _generate_signal(
            db, repository, account, run, signal_day, execute_on=execute_on
        )
        run.signal_generated = True

    trade_count, fee_total, blocked = _execute_pending(
        db, repository, account, run, data_date, ledger
    )
    run.trade_count = trade_count
    run.rebalanced = trade_count > 0
    if blocked:
        run.warnings = list(run.warnings) + [
            f"{len(blocked)} 个订单因停牌/涨跌停/现金约束等待后续交易日"
        ]
    nav = _write_nav(
        db,
        repository,
        account,
        run,
        data_date,
        fee_total,
        ledger,
    )
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
            dict(deviations).get("beta") if isinstance(deviations, dict) else None
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
    from app.services.transaction_cost_analysis import (
        aggregate_tca,
        order_tca,
    )

    tca_rows: list[dict[str, object]] = []
    for trade in run_trades:
        if any(
            value is None
            for value in (
                trade.decision_price,
                trade.arrival_price,
                trade.market_vwap,
                trade.close_price,
            )
        ):
            continue
        decomposition = order_tca(
            side=trade.side,
            shares=float(trade.shares),
            decision_price=float(trade.decision_price),
            arrival_price=float(trade.arrival_price),
            market_vwap=float(trade.market_vwap),
            close_price=float(trade.close_price),
            fill_price=float(trade.price),
            fee=float(trade.fee),
        )
        amount = float(trade.amount)
        tca_rows.append(
            {
                **decomposition,
                "code": trade.stock_code,
                "size_bucket": (
                    "small"
                    if amount < 100_000
                    else "medium"
                    if amount < 1_000_000
                    else "large"
                ),
                "session": trade.execution_session,
                "execution_algorithm": trade.slippage_model_version,
            }
        )
    tca_report = aggregate_tca(tca_rows)
    run.result = {
        **dict(run.result or {}),
        "total_value": float(nav.total_value),
        "nav": float(nav.nav),
        "benchmark_nav": float(nav.benchmark_nav),
        "fee_total": fee_total,
        "slippage_cost": slippage_cost,
        "attribution": attribution,
        "transaction_cost_analysis": tca_report,
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


def _metrics(
    rows: list[StockPaperNavDaily], db: Session, account_id: int
) -> StockPaperMetrics:
    if not rows:
        return StockPaperMetrics()
    navs = [float(row.nav) for row in rows]
    bench = [float(row.benchmark_nav) for row in rows]
    returns = [
        navs[i] / navs[i - 1] - 1.0 for i in range(1, len(navs)) if navs[i - 1] > 0
    ]
    bench_returns = [
        bench[i] / bench[i - 1] - 1.0 for i in range(1, len(bench)) if bench[i - 1] > 0
    ]
    total_return = navs[-1] / navs[0] - 1.0 if navs[0] > 0 else None
    benchmark_return = bench[-1] / bench[0] - 1.0 if bench[0] > 0 else None
    max_drawdown = stats.max_drawdown(navs)
    annual_volatility = None
    if len(returns) >= 2:
        mean = fmean(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
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
            sum(1 for value in returns if value > 0) / len(returns) if returns else None
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


def cancel_pending_signal(
    db: Session,
    signal_id: int,
    *,
    reason: str,
) -> StockPaperSignalOut:
    """人工撤销未完成信号，并固化逐订单机会成本与审计原因。"""
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise StockPaperError("人工取消必须填写原因")
    signal = db.get(StockPaperSignal, signal_id)
    if signal is None:
        raise StockPaperError(f"信号 {signal_id} 不存在")
    if signal.status != "pending":
        raise StockPaperError(
            f"信号 {signal_id} 当前状态为 {signal.status}，不能重复取消"
        )
    cancelled_on = now_cn().date()
    state = {
        code: {**dict(item), "events": list(dict(item).get("events", []))}
        for code, item in dict(signal.order_state or {}).items()
    }
    for code, item in state.items():
        if item.get("status") in order_lifecycle.TERMINAL_STATUSES:
            continue
        cost = order_lifecycle.opportunity_cost(
            side=str(item.get("side", "buy")),
            unfilled_shares=float(item.get("remaining_shares", 0.0)),
            decision_price=(
                float(item["decision_price"])
                if item.get("decision_price") is not None
                else None
            ),
            current_price=(
                float(item["last_market_price"])
                if item.get("last_market_price") is not None
                else None
            ),
        )
        item.update(
            {
                "status": "cancelled",
                "last_reason": normalized_reason,
                "order_lifecycle_version": ORDER_POLICY.version,
                **cost,
            }
        )
        item["events"].append(
            {
                "date": cancelled_on.isoformat(),
                "code": code,
                "status": "cancelled",
                "reason": normalized_reason,
                "order_lifecycle_version": ORDER_POLICY.version,
                **cost,
            }
        )
    signal.order_state = state
    signal.status = "cancelled"
    signal.executed_at = cancelled_on
    signal.warnings = list(signal.warnings) + [
        f"{cancelled_on.isoformat()} 人工取消：{normalized_reason}"
    ]
    db.commit()
    return _signal_out(signal)  # type: ignore[return-value]


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
    for item in latest.positions if latest else []:
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
                pnl=(float(market_value) - cost if market_value is not None else None),
            )
        )
    warnings = list(readiness.warnings)
    mandate = dict(version.mandate or {}) if version else {}
    validation_scope = str(
        mandate.get("validation_scope")
        or dict(version.params or {}).get("validation_scope")
        if version
        else "operational_only"
    )
    approval_eligible = bool(mandate.get("investment_approval_eligible", False))
    if validation_scope == "operational_only":
        warnings.insert(
            0,
            "本账户仅验收运行链路；任何短期盈利、Sharpe或超额收益都不能证明"
            "策略具有Alpha，也不能用于approved/live放行",
        )
    if len(rows) < 20:
        warnings.append(
            f"目前只有 {len(rows)} 个前向交易日；两个月结束后也只评价运行可靠性，"
            "不评价投资有效性"
        )
    if account.status == "evaluation_due":
        warnings.append(
            "两个月观察期已到：只能评估数据、调度、订单、账本、对账、告警和恢复，"
            "不得据此批准实盘"
        )
    return StockPaperSummary(
        started=True,
        account_id=account.id,
        account_name=account.name,
        as_of=latest.nav_date.isoformat() if latest else None,
        initial_capital=float(account.initial_capital),
        cash=float(latest.cash) if latest else float(account.cash),
        frozen_cash=(
            float(latest.frozen_cash) if latest else float(account.frozen_cash)
        ),
        receivable_cash=float(latest.receivable_cash) if latest else 0.0,
        settled_cash=(
            float(latest.settled_cash) if latest else float(account.settled_cash)
        ),
        cash_interest=float(latest.cash_interest) if latest else 0.0,
        cash_ledger=dict(latest.cash_ledger or {}) if latest else {},
        market_value=float(latest.market_value) if latest else 0.0,
        total_value=float(latest.total_value)
        if latest
        else float(account.initial_capital),
        nav=float(latest.nav) if latest else 1.0,
        benchmark_nav=float(latest.benchmark_nav) if latest else 1.0,
        strategy=StockPaperStrategyInfo(
            version_id=version.id if version else 0,
            name=version.name if version else STRATEGY_NAME,
            status=version.status if version else account.status,
            trial_start=account.trial_start.isoformat(),
            trial_end=account.trial_end.isoformat(),
            calendar_days_elapsed=elapsed,
            calendar_days_remaining=max(total_days - elapsed, 0),
            observation_progress=round(elapsed / total_days, 6),
            candidate_count=len(account.candidate_codes),
            validation_scope=validation_scope,
            investment_approval_eligible=approval_eligible,
            mandate_version=str(mandate.get("mandate_version") or "missing"),
            mandate_sha256=version.mandate_sha256 if version else "",
            result_interpretation=(
                "operational_only：只验证数据、调度、信号、模拟成交、账本、"
                "对账、告警和恢复；收益不构成Alpha证据"
                if validation_scope == "operational_only"
                else "investment_effectiveness：仍须以冻结任务书和门禁结果解释"
            ),
            approval_blocker=(
                str(dict(version.params or {}).get("approval_blocker"))
                if version and dict(version.params or {}).get("approval_blocker")
                else None
            ),
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
                available_cash=float(row.cash),
                frozen_cash=float(row.frozen_cash),
                receivable_cash=float(row.receivable_cash),
                settled_cash=float(row.settled_cash),
                cash_interest=float(row.cash_interest),
                cash_conservation_error=float(row.cash_conservation_error),
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
