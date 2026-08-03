"""as_of 日期快照：可用交易日、数据滞后与对齐面板装载。

量化验证要求可复现的"当时可见"数据视角：
- 国内基金净值通常 T 日披露 T 日净值（T+1 可见），QDII 披露滞后更久
  （T 日净值 T+2 可见）；因此对 as_of 当日做研究时，国内基金默认
  使用 as_of - 1 个交易日（lag1）及之前的净值，QDII 默认
  as_of - 2 个交易日（lag2）及之前的净值；
- 可用交易日 = 所选候选基金净值日期的并集（升序），as_of 快照接口
  返回该日历（及按 lag 折算后的有效数据日），便于调用方指定合法的
  as_of 参数复现历史任一交易日的研究视角；
- 对齐面板：各基金截取 ≤ 自身有效数据日的净值后，日历取净值日期
  并集（保留 QDII 等低频披露基金），当日缺测按前值填充（与量化模块
  既有口径一致，当日收益记 0），起点截断到所有基金均有前值之日，
  保证策略与基准逐日可比。

QDII 判定与 returns 模块一致：基金名称包含 "QDII"（大小写不敏感）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FundNav, Instrument
from app.services.quant import QuantError, _load_nav_series

# 数据滞后（单位：日历上的"可用交易日"个数）
DOMESTIC_LAG_DAYS = 1
QDII_LAG_DAYS = 2

NAV_LOAD_LIMIT = 5000


def is_qdii(fund_name: str) -> bool:
    """按基金名称识别 QDII（与 returns 模块口径一致）。"""
    return "QDII" in fund_name.upper()


def default_lag_days(fund_name: str) -> int:
    """默认数据滞后：QDII lag2，国内 lag1。"""
    return QDII_LAG_DAYS if is_qdii(fund_name) else DOMESTIC_LAG_DAYS


@dataclass(frozen=True)
class FundSnapshot:
    """一只基金在 as_of 视角下的数据可用性。"""

    code: str
    name: str
    is_qdii: bool
    lag_days: int
    latest_nav_date: str | None
    effective_date: str | None  # as_of 视角下实际使用的最后净值日


def available_trade_days(
    series_by_code: dict[str, list[tuple[date, float]]]
) -> list[date]:
    """可用交易日：全部基金净值日期的并集（升序）。"""
    days: set[date] = set()
    for series in series_by_code.values():
        days.update(d for d, _ in series)
    return sorted(days)


def effective_nav_date(
    nav_dates: list[date], as_of: date, lag_days: int
) -> date | None:
    """as_of 视角下该基金的可用净值截止日。

    净值披露滞后：国内基金 T 日净值 T+1 才可见（lag1），QDII T+2 才
    可见（lag2）。因此先剔除 as_of 当日（当日净值尚未披露），取
    "净值日期 < as_of"的序列为候选，再按滞后往前退 lag_days - 1 个
    净值日（lag1 = as_of 前最后一个净值日；lag2 = 再往前一个）。
    候选不足时返回 None。
    """
    candidates = [d for d in nav_dates if d < as_of]
    if len(candidates) < lag_days:
        return None
    return candidates[-lag_days]


def load_snapshot_panels(
    db: Session,
    instruments: list[Instrument],
    as_of: date | None,
    lag_overrides: dict[str, int] | None = None,
    min_samples: int = 2,
) -> tuple[list[date], dict[str, list[float]], list[FundSnapshot], list[str]]:
    """按 as_of 装载各基金净值并对齐为面板（不访问 as_of 之后的数据）。

    - as_of 为 None 时使用全部历史（不施加视角截断）；
    - 每只基金的有效数据日 = effective_nav_date(自身净值日, as_of, lag)，
      lag 缺省按 QDII lag2 / 国内 lag1，可用 lag_overrides 按代码覆盖；
    - 截取净值日期 ≤ 有效数据日的序列后，对齐到共同交易日交集，
      交集内当日缺测按前值填充；
    - 返回 (交易日历, {code: 等长净值序列}, 各基金快照, 警告)；
      有效基金不足 2 只或共同交易日不足时抛 QuantError。
    """
    warnings: list[str] = []
    lag_overrides = lag_overrides or {}

    # 各基金净值序列与 as_of 视角下的有效数据日
    raw_series: dict[str, list[tuple[date, float]]] = {}
    snapshots: list[FundSnapshot] = []
    for instrument in instruments:
        series = _load_nav_series(db, instrument.id, limit=NAV_LOAD_LIMIT)
        nav_dates = [d for d, _ in series]
        lag = lag_overrides.get(instrument.code, default_lag_days(instrument.name))
        lag = max(lag, 0)
        if as_of is None:
            effective = nav_dates[-1] if nav_dates else None
        else:
            effective = effective_nav_date(nav_dates, as_of, lag)
        snapshots.append(
            FundSnapshot(
                code=instrument.code,
                name=instrument.name,
                is_qdii=is_qdii(instrument.name),
                lag_days=lag,
                latest_nav_date=nav_dates[-1].isoformat() if nav_dates else None,
                effective_date=effective.isoformat() if effective else None,
            )
        )
        if effective is None:
            warnings.append(
                f"基金 {instrument.code}（{instrument.name}）在 as_of={as_of} 视角下"
                f"无满足 lag{lag} 要求的净值数据，已剔除"
            )
            continue
        trimmed = [(d, v) for d, v in series if d <= effective]
        if len(trimmed) < min_samples:
            warnings.append(
                f"基金 {instrument.code}（{instrument.name}）有效净值样本不足 "
                f"{min_samples} 条（{len(trimmed)} 条），已剔除"
            )
            continue
        raw_series[instrument.code] = trimmed

    if len(raw_series) < 2:
        raise QuantError(
            f"有效基金不足 2 只（{len(raw_series)} 只满足 as_of 数据要求），"
            "请扩大候选池、推迟 as_of 或减小滞后"
        )

    # 交易日历 = 各基金有效净值日期的并集（保留 QDII 等低频披露基金），
    # 当日缺测按前值填充（当日收益记 0，与量化模块既有口径一致）
    calendar = available_trade_days(raw_series)
    if len(calendar) < min_samples:
        raise QuantError(
            f"候选基金可用交易日仅 {len(calendar)} 天，不足 {min_samples} 天，"
            "请推迟 as_of 或核对净值区间"
        )

    # 统一从所有基金最晚的首个净值日起算，随后对并集日历前值填充。
    # 原实现遇到首日缺值就 break，导致起点更晚基金 filled=[]，最终把全部面板截成0天。
    common_start = max(series[0][0] for series in raw_series.values())
    calendar = [day for day in calendar if day >= common_start]
    panels: dict[str, list[float]] = {}
    for code, series in raw_series.items():
        values = dict(series)
        prior = [value for day, value in series if day <= common_start]
        last: float | None = prior[-1] if prior else None
        filled: list[float] = []
        for day in calendar:
            value = values.get(day)
            if value is not None and value > 0:
                last = value
            if last is None:
                raise QuantError(f"基金 {code} 在共同起点无可用净值")
            filled.append(last)
        panels[code] = filled
    if len(calendar) < min_samples:
        raise QuantError(
            f"对齐后共同交易日仅 {len(calendar)} 天，不足 {min_samples} 天，"
            "请推迟 as_of 或核对净值区间"
        )

    if any(len(series) != len(calendar) for series in raw_series.values()):
        warnings.append(
            f"各基金净值日期不完全一致，已对齐到 {len(calendar)} 个交易日，"
            "当日缺测按前值填充（当日收益记 0）"
        )
    return calendar, panels, snapshots, warnings


def list_available_days(
    db: Session, instruments: list[Instrument]
) -> tuple[list[date], dict[str, list[date]]]:
    """各基金净值日期与并集交易日历（as_of 快照接口用）。"""
    per_fund: dict[str, list[date]] = {}
    union: set[date] = set()
    for instrument in instruments:
        rows = db.execute(
            select(FundNav.nav_date)
            .where(FundNav.instrument_id == instrument.id)
            .order_by(FundNav.nav_date)
            .limit(NAV_LOAD_LIMIT)
        ).all()
        days = [row[0] for row in rows]
        per_fund[instrument.code] = days
        union.update(days)
    return sorted(union), per_fund
