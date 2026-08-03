"""数据质量检查。

对仓库中的数据集执行一组声明式检查，返回 ``QualityReport``：

- ``duplicate_keys``      业务键 + effective_date 重复行数（最新版本口径下应为 0）；
- ``null_key_rows``       键列/有效日期为空的行数；
- ``negative_prices``     行情/净值类数据集中价格 <= 0 的行数；
- ``date_gaps``           连续日期缺口（按 symbol 分组，缺口阈值可配）；
- ``future_effective``    effective_date 晚于 ingested_at 日期的行数（疑似时钟/口径错误）。

检查不抛异常，全部以 issue 形式汇报，调用方按 ``report.ok`` 决策。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from app.research.repository import DATASET_BUSINESS_COLUMNS
from app.research.warehouse import DATASET_BUSINESS_KEYS, validate_dataset

if TYPE_CHECKING:
    from app.research.warehouse import ResearchWarehouse

#: 价格类列（存在才检查非正价格）
_PRICE_COLUMNS: dict[str, list[str]] = {
    "fund_nav": ["nav"],
    "stock_daily": ["open", "high", "low", "close"],
}

#: 需要按 symbol 做交易日缺口检查的数据集及其分组列
_GAP_CHECK: dict[str, str] = {
    "fund_nav": "fund_code",
    "stock_daily": "symbol",
}


@dataclass(frozen=True)
class QualityIssue:
    """单条质量问题。"""

    check: str
    dataset: str
    detail: str
    severity: str = "error"  # error | warning


@dataclass
class QualityReport:
    """质量检查结果汇总。"""

    dataset: str
    row_count: int = 0
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """无 error 级问题视为通过。"""
        return not any(issue.severity == "error" for issue in self.issues)

    def add(self, check: str, detail: str, *, severity: str = "error") -> None:
        self.issues.append(
            QualityIssue(check=check, dataset=self.dataset, detail=detail, severity=severity)
        )


def _fetch_one(warehouse: ResearchWarehouse, sql: str, params: list | None = None) -> int:
    return int(warehouse.conn.execute(sql, params or []).fetchone()[0])


def check_dataset(
    warehouse: ResearchWarehouse,
    dataset: str,
    *,
    gap_max_days: int = 10,
    as_of: date | None = None,
) -> QualityReport:
    """对单个数据集执行全部质量检查。

    ``gap_max_days``：同一 symbol 相邻 effective_date 间隔超过该天数记 warning
    （自然含长假；研究用途下 >10 天通常意味着缺数）。
    """
    validate_dataset(dataset)
    report = QualityReport(dataset=dataset)
    conn = warehouse.conn

    # 去重后的最新版本快照作为检查基线（多版本修订不算重复）
    keys = DATASET_BUSINESS_KEYS[dataset]
    key_cols = [*keys, "effective_date"]
    key_list = ", ".join(key_cols)
    baseline = (
        f"SELECT * FROM {dataset}_all "
        f"QUALIFY row_number() OVER ("
        f"PARTITION BY {key_list} ORDER BY available_at DESC, ingested_at DESC) = 1"
    )

    report.row_count = _fetch_one(warehouse, f"SELECT count(*) FROM ({baseline})")
    if report.row_count == 0:
        report.add("empty", "数据集为空", severity="warning")
        return report

    # 1) 键列空值
    null_cond = " OR ".join(f"{c} IS NULL" for c in key_cols)
    null_rows = _fetch_one(warehouse, f"SELECT count(*) FROM ({baseline}) WHERE {null_cond}")
    if null_rows:
        report.add("null_key_rows", f"键列空值 {null_rows} 行")

    # 2) 完全重复键（去重后仍有重复说明 available_at 相同的多条冲突）
    dup = _fetch_one(
        warehouse,
        f"""
        SELECT count(*) FROM (
            SELECT {key_list}, count(*) AS n FROM ({baseline})
            GROUP BY {key_list} HAVING n > 1
        )
        """,
    )
    if dup:
        report.add("duplicate_keys", f"重复键分组 {dup} 个")

    # 3) 非正价格
    for col in _PRICE_COLUMNS.get(dataset, []):
        if col not in DATASET_BUSINESS_COLUMNS[dataset]:
            continue
        bad = _fetch_one(
            warehouse, f"SELECT count(*) FROM ({baseline}) WHERE {col} IS NOT NULL AND {col} <= 0"
        )
        if bad:
            report.add("negative_prices", f"列 {col} 非正值 {bad} 行")

    # 4) effective_date 晚于参考日期（默认今天）
    ref = as_of or date.today()
    future = _fetch_one(
        warehouse,
        f"SELECT count(*) FROM ({baseline}) WHERE effective_date > ?",
        [ref],
    )
    if future:
        report.add("future_effective", f"effective_date 晚于 {ref} 的行数 {future}")

    # 5) 日期缺口（按 symbol 分组，warning 级）
    group_col = _GAP_CHECK.get(dataset)
    if group_col:
        gaps = conn.execute(
            f"""
            SELECT {group_col}, prev_d, effective_date,
                   date_diff('day', prev_d, effective_date) AS gap_days
            FROM (
                SELECT {group_col}, effective_date,
                       lag(effective_date) OVER (
                           PARTITION BY {group_col} ORDER BY effective_date
                       ) AS prev_d
                FROM ({baseline})
            )
            WHERE prev_d IS NOT NULL
              AND date_diff('day', prev_d, effective_date) > ?
            ORDER BY gap_days DESC
            LIMIT 20
            """,
            [gap_max_days],
        ).fetchall()
        for group_val, prev_d, cur_d, gap_days in gaps:
            report.add(
                "date_gaps",
                f"{group_col}={group_val} 在 {prev_d} ~ {cur_d} 之间缺口 {gap_days} 天",
                severity="warning",
            )

    return report


def check_all(
    warehouse: ResearchWarehouse,
    *,
    gap_max_days: int = 10,
) -> dict[str, QualityReport]:
    """对全部数据集执行质量检查。"""
    from app.research.warehouse import ALL_DATASETS

    return {
        dataset: check_dataset(warehouse, dataset, gap_max_days=gap_max_days)
        for dataset in ALL_DATASETS
    }
