"""不可变投资任务书及策略有效性阈值。

任务书回答策略为谁、以什么基准、在多大资金和风险预算下运行。运行链路
验证任务书明确不具备投资批准资格；投资任务书则给出机器可执行的有效性门禁。
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any


INVESTMENT_MANDATE_VERSION = "cn-stock-long-only-v1"
OPERATIONAL_MANDATE_VERSION = "operational-paper-validation-v1"


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def mandate_sha256(mandate: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(mandate).encode()).hexdigest()


def operational_validation_mandate(
    *,
    strategy_name: str,
    initial_capital: float | Decimal,
    rebalance_days: int,
    top_n: int,
) -> dict[str, Any]:
    """只允许验证运行链路、永不直接批准实盘的任务书。"""

    return {
        "mandate_version": OPERATIONAL_MANDATE_VERSION,
        "name": f"{strategy_name}-运行链路验证任务书",
        "investor": "个人研究账户",
        "asset_class": "中国A股",
        "direction": "long_only",
        "validation_scope": "operational_only",
        "investment_approval_eligible": False,
        "initial_capital_cny": float(initial_capital),
        "rebalance_days": int(rebalance_days),
        "target_holdings": int(top_n),
        "purpose": (
            "仅验证数据、调度、信号、模拟成交、账本、对账、告警与恢复；"
            "收益不构成Alpha或实盘放行证据"
        ),
        "stop_conditions": [
            "关键数据集未通过readiness",
            "账本不守恒或对账存在未解决差异",
            "运行任务失败或证据哈希不一致",
        ],
    }


def cn_stock_investment_mandate(
    *,
    strategy_name: str,
    initial_capital: float | Decimal,
    rebalance_days: int,
    top_n: int,
) -> dict[str, Any]:
    """项目第一版可执行投资政策；阈值变化必须创建新任务书和策略版本。"""

    return {
        "mandate_version": INVESTMENT_MANDATE_VERSION,
        "name": f"{strategy_name}-A股长期选股任务书",
        "investor": "个人投资账户",
        "asset_class": "中国A股",
        "direction": "long_only",
        "validation_scope": "investment_effectiveness",
        "investment_approval_eligible": True,
        "initial_capital_cny": float(initial_capital),
        "capital_capacity_cny": float(initial_capital),
        "universe": {
            "indices": ["000300", "000905"],
            "membership": "point_in_time",
            "allowed_markets": ["sse_main", "szse_main", "chinext", "star"],
            "excluded": ["ST", "*ST", "上市不足253个交易日", "数据门禁不通过"],
        },
        "benchmark": {
            "primary": "CSI800_TOTAL_RETURN",
            "fallback_allowed": False,
            "comparators": ["UNIVERSE_EQUAL_WEIGHT_TOTAL_RETURN", "CASH_CNY"],
        },
        "horizon": {
            "rebalance_days": int(rebalance_days),
            "target_holdings": int(top_n),
            "minimum_historical_years": 6,
            "paper_operational_days": 42,
        },
        "portfolio_limits": {
            "max_stock_weight": 0.05,
            "max_industry_weight": 0.20,
            "max_annual_volatility": 0.20,
            "max_tracking_error": 0.12,
            "max_one_way_turnover": 0.50,
            "max_adv_participation": 0.10,
            "minimum_holdings": 20,
            "cash_min": 0.0,
            "cash_max": 1.0,
            "max_industry_active_weight": 0.03,
            "max_beta_deviation": 0.10,
            "max_size_z_deviation": 0.20,
            "max_liquidity_z_deviation": 0.20,
            "max_cvar95_loss": 0.08,
            "max_cdar95_drawdown": 0.20,
            "capacity_evidence_required": True,
        },
        "execution_policy": {
            "suspension": "no_trade_and_retry_until_ttl",
            "price_limit": "respect_point_in_time_exchange_limits",
            "delisting": "restricted_asset_until_official_consideration",
            "corporate_actions": "raw_price_plus_entitlement_ledger",
            "cash_interest": "explicit_curve_required",
            "unknown_event": "block_and_manual_review",
        },
        "validation_thresholds": {
            "min_data_coverage": 0.99,
            "holdout_evaluations": 1,
            "min_walkforward_folds": 3,
            "min_holdout_sharpe": 0.30,
            "min_net_excess_return": 0.0,
            "min_active_sharpe": 0.30,
            "min_active_return_ci_lower": 0.0,
            "min_regression_alpha_ci_lower": 0.0,
            "max_drawdown": -0.25,
            "min_rank_ic_mean": 0.02,
            "min_rank_icir": 0.30,
            "max_rank_ic_p_value": 0.10,
            "min_rank_ic_ci_lower": 0.0,
            "min_quintile_monotonicity": 0.60,
            "min_top_bottom_spread": 0.0,
            "min_deflated_sharpe_probability": 0.95,
            "min_probabilistic_sharpe_probability": 0.95,
            "max_probability_backtest_overfitting": 0.20,
            "max_multiple_testing_fdr": 0.10,
            "min_cost_2x_excess_return": 0.0,
            "max_single_period_alpha_contribution": 0.50,
            "min_worst_regime_excess_return": -0.05,
            "min_worst_year_excess_return": -0.05,
        },
        "stop_conditions": [
            "关键数据覆盖低于99%",
            "滚动IC失效或分布漂移达到停止阈值",
            "实现波动、跟踪误差或回撤突破投资任务书",
            "账本、券商或三方对账存在未解决差异",
            "持续数据、交易或监控链路不可用",
        ],
    }


def validate_mandate(mandate: dict[str, Any], expected_sha256: str) -> list[str]:
    failures: list[str] = []
    if not mandate:
        failures.append("缺少投资任务书")
        return failures
    required = {
        "mandate_version",
        "name",
        "validation_scope",
        "investment_approval_eligible",
        "purpose",
    }
    if mandate.get("validation_scope") == "investment_effectiveness":
        required = (required - {"purpose"}) | {
            "benchmark",
            "portfolio_limits",
            "execution_policy",
            "validation_thresholds",
            "stop_conditions",
        }
    missing = sorted(key for key in required if mandate.get(key) in (None, "", [], {}))
    if missing:
        failures.append("投资任务书字段不完整：" + "、".join(missing))
    actual = mandate_sha256(mandate)
    if not expected_sha256 or actual != expected_sha256:
        failures.append("投资任务书哈希不一致")
    return failures
