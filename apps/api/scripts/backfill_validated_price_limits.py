"""补齐 Tushare 空分区：公开名称证据 + daily.pre_close + 交易所规则。

本脚本不会覆盖已有 stk_limit Parquet。派生记录保留参考价、规则版本、
算法版本和外部名称证据，便于文件清单冻结及事后复核。
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.historical_price_limits import (  # noqa: E402
    ALGORITHM_VERSION,
    dated_name_as_of,
    derive_price_limit,
    names_prove_non_st,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--codes", required=True, help="逗号分隔的六位股票代码")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument(
        "--public-name-cache",
        type=Path,
        help="AkShare 历史名称缓存；默认写在证据报告旁，成功单股会立即落盘",
    )
    parser.add_argument("--name-workers", type=int, default=4)
    parser.add_argument("--name-timeout-seconds", type=int, default=90)
    return parser.parse_args()


def qualified_code(code: str) -> str:
    normalized = str(code).split(".")[0].zfill(6)
    exchange = "SH" if normalized.startswith(("5", "6", "9")) else "SZ"
    return f"{normalized}.{exchange}"


def historical_names(code: str, *, timeout_seconds: int) -> list[str]:
    from app.services.execution_reference_sync import _akshare_calls

    rows = _akshare_calls(
        [("stock_info_change_name", {"symbol": code})],
        timeout_seconds=timeout_seconds,
    )
    return [
        str(row.get("name") or "").strip()
        for row in rows
        if str(row.get("name") or "").strip()
    ]


def read_public_name_cache(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = dict(payload.get("entries") or {})
    return {
        str(code): [
            str(name).strip()
            for name in dict(item).get("names", [])
            if str(name).strip()
        ]
        for code, item in entries.items()
    }


def write_public_name_cache(path: Path, names_by_code: dict[str, list[str]]) -> None:
    payload = {
        "source": "akshare.stock_info_change_name",
        "updated_at": datetime.now(UTC).isoformat(),
        "entries": {
            code: {"names": names} for code, names in sorted(names_by_code.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def fetch_required_public_names(
    codes: list[str],
    *,
    cache_path: Path,
    workers: int,
    timeout_seconds: int,
) -> dict[str, list[str]]:
    cached = read_public_name_cache(cache_path)
    missing = [code for code in codes if not cached.get(code)]
    if not missing:
        return {code: cached[code] for code in codes}
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(missing)))) as executor:
        future_codes = {
            executor.submit(
                historical_names,
                code,
                timeout_seconds=timeout_seconds,
            ): code
            for code in missing
        }
        for future in as_completed(future_codes):
            code = future_codes[future]
            try:
                names = future.result()
                if not names:
                    raise RuntimeError("公开历史名称响应为空")
                cached[code] = names
                # 单股成功即持久化，后续某只超时不会抹掉已经取得的证据。
                write_public_name_cache(cache_path, cached)
            except Exception as exc:  # noqa: BLE001 - 汇总所有外部接口失败
                failures[code] = str(exc)
    if failures:
        detail = "；".join(f"{code}: {message}" for code, message in failures.items())
        raise RuntimeError(f"AkShare 历史名称交叉验证失败：{detail}")
    return {code: cached[code] for code in codes}


def complete_leading_name_period(
    periods: list[tuple[date, date | None, str]],
    public_names: list[str],
    required_start: date,
) -> tuple[list[tuple[date, date | None, str]], str | None]:
    """用公开名称有序序列补齐首个 Tushare 变更记录之前的名称。"""
    if not periods or dated_name_as_of(periods, required_start) is not None:
        return periods, None
    earliest = min(periods, key=lambda item: item[0])
    if required_start >= earliest[0]:
        return periods, None
    matching_indices = [
        index for index, name in enumerate(public_names) if name == earliest[2]
    ]
    if not matching_indices or matching_indices[0] == 0:
        return periods, None
    predecessor = public_names[matching_indices[0] - 1]
    completed = [
        (
            required_start,
            earliest[0] - timedelta(days=1),
            predecessor,
        ),
        *periods,
    ]
    return completed, predecessor


def dated_name_periods(
    snapshot: Path,
    ts_code: str,
) -> tuple[list[tuple[date, date | None, str]], tuple[Path, ...], str]:
    path = snapshot / "stocks" / "namechange" / f"{ts_code}.parquet"
    if path.is_file():
        frame = pd.read_parquet(path, columns=["name", "start_date", "end_date"])
        periods: list[tuple[date, date | None, str]] = []
        for row in frame.to_dict("records"):
            start_text = str(row.get("start_date") or "")
            end_text = str(row.get("end_date") or "")
            name = str(row.get("name") or "").strip()
            if len(start_text) < 8 or not name:
                continue
            start = date.fromisoformat(
                f"{start_text[:4]}-{start_text[4:6]}-{start_text[6:8]}"
            )
            end = (
                date.fromisoformat(f"{end_text[:4]}-{end_text[4:6]}-{end_text[6:8]}")
                if len(end_text) >= 8
                else None
            )
            periods.append((start, end, name))
        if not periods:
            raise RuntimeError(f"{ts_code} 带日期名称证据为空")
        basic_path = snapshot / "global" / "stock_basic" / "L.parquet"
        if len(periods) == 1 and periods[0][1] is None and basic_path.is_file():
            basic = pd.read_parquet(basic_path)
            matched = basic[basic["ts_code"].astype(str) == ts_code]
            if len(matched) == 1:
                current_name = str(matched.iloc[0]["name"] or "").strip()
                list_text = str(matched.iloc[0]["list_date"] or "")
                list_date = (
                    date.fromisoformat(
                        f"{list_text[:4]}-{list_text[4:6]}-{list_text[6:8]}"
                    )
                    if len(list_text) >= 8
                    else None
                )
                if periods[0][0] == list_date and periods[0][2] == current_name:
                    return (
                        periods,
                        (path, basic_path),
                        "tushare.namechange.dated_single_from_listing"
                        "+stock_basic.current",
                    )
        return periods, (path,), "tushare.namechange.dated"

    # “没有改名记录”与“接口失败”不可混为一谈。只有保存的成功空响应、
    # 查询区间覆盖正式验证期，并且 Tushare 主表当前名称非 ST/退市时，
    # 才能证明该区间名称未变化并构造单一有效期。
    empty_path = snapshot / "stocks" / "namechange" / f"{ts_code}.empty.json"
    basic_path = snapshot / "global" / "stock_basic" / "L.parquet"
    if not empty_path.is_file() or not basic_path.is_file():
        raise RuntimeError(f"{ts_code} 缺少可验证的 namechange 空响应/证券主表")
    marker = json.loads(empty_path.read_text(encoding="utf-8"))
    params = dict(marker.get("params") or {})
    start_text = str(params.get("start_date") or "")
    if marker.get("status") != "empty" or len(start_text) < 8:
        raise RuntimeError(f"{ts_code} namechange 空响应证据无效")
    basic = pd.read_parquet(basic_path)
    matched = basic[basic["ts_code"].astype(str) == ts_code]
    if len(matched) != 1:
        raise RuntimeError(f"{ts_code} Tushare 当前证券主表记录不唯一")
    current_name = str(matched.iloc[0]["name"] or "").strip()
    if not names_prove_non_st([current_name]):
        raise RuntimeError(f"{ts_code} 当前名称含 ST/退市标识")
    covered_from = date.fromisoformat(
        f"{start_text[:4]}-{start_text[4:6]}-{start_text[6:8]}"
    )
    return (
        [(covered_from, None, current_name)],
        (empty_path, basic_path),
        "tushare.namechange.empty+stock_basic.current",
    )


def main() -> None:
    args = parse_args()
    snapshot = args.snapshot_dir.resolve()
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    generated_at = datetime.now(UTC)
    evidence: dict[str, object] = {
        "algorithm_version": ALGORITHM_VERSION,
        "generated_at": generated_at.isoformat(),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "reference_source": "tushare.daily.pre_close",
        "name_evidence_policy": (
            "有改名记录使用 tushare.namechange.dated 与 "
            "akshare.stock_info_change_name 交叉确认；成功空响应使用 "
            "tushare.namechange.empty 与 stock_basic.current 闭环"
        ),
        "partitions": [],
    }
    pending_outputs: list[tuple[Path, Path]] = []
    codes = [
        raw_code.strip().split(".")[0].zfill(6)
        for raw_code in args.codes.split(",")
        if raw_code.strip()
    ]
    name_materials: dict[
        str,
        tuple[list[tuple[date, date | None, str]], tuple[Path, ...], str],
    ] = {}
    for code in codes:
        if not code.startswith(("00", "001", "002", "003", "6")):
            raise RuntimeError(f"{code} 不是本脚本允许的沪深主板证券")
        ts_code = qualified_code(code)
        name_materials[code] = dated_name_periods(snapshot, ts_code)
    dated_codes = [
        code
        for code, (_periods, _sources, mode) in name_materials.items()
        if mode == "tushare.namechange.dated"
    ]
    cache_path = (
        args.public_name_cache.resolve()
        if args.public_name_cache
        else args.evidence_output.resolve().with_suffix(".public-names.json")
    )
    fetched_names = fetch_required_public_names(
        dated_codes,
        cache_path=cache_path,
        workers=args.name_workers,
        timeout_seconds=args.name_timeout_seconds,
    )
    for code in codes:
        periods, name_sources, dated_name_mode = name_materials[code]
        names = (
            fetched_names[code]
            if dated_name_mode == "tushare.namechange.dated"
            else [name for _period_start, _period_end, name in periods]
        )
        ts_code = qualified_code(code)
        periods, inferred_predecessor = complete_leading_name_period(
            periods,
            names,
            start,
        )
        dated_names = {
            name
            for period_start, period_end, name in periods
            if period_start <= end and (period_end is None or period_end >= start)
        }
        public_names = {name for name in names if name}
        if dated_name_mode == "tushare.namechange.dated" and not dated_names.issubset(
            public_names
        ):
            raise RuntimeError(
                f"{code} Tushare 带日期名称未被 AkShare 名称序列完整交叉确认"
            )
        if public_names and not names_prove_non_st(list(public_names)):
            # 带日期证据可以安全处理历史 ST；成功空响应模式却不能有另一源
            # 声称区间内存在 ST 名称。
            if dated_name_mode != "tushare.namechange.dated":
                raise RuntimeError(f"{code} AkShare 名称序列含 ST/退市标识")
        name_evidence_source = (
            "tushare.namechange.dated+akshare.stock_info_change_name.crosscheck"
            if dated_name_mode == "tushare.namechange.dated"
            else dated_name_mode
        )
        daily_path = snapshot / "stocks" / "daily" / f"{ts_code}.parquet"
        target = snapshot / "stocks" / "stk_limit" / f"{ts_code}.parquet"
        if target.exists():
            raise FileExistsError(f"{target} 已存在，拒绝覆盖原始限价分区")
        daily = pd.read_parquet(
            daily_path,
            columns=["trade_date", "pre_close"],
        )
        rows: list[dict[str, object]] = []
        for raw in daily.to_dict("records"):
            text = str(raw["trade_date"])
            day = date.fromisoformat(f"{text[:4]}-{text[4:6]}-{text[6:8]}")
            if not start <= day <= end:
                continue
            pre_close = float(raw["pre_close"])
            active_name = dated_name_as_of(periods, day)
            if active_name is None:
                raise RuntimeError(f"{code} {day} 缺少唯一有效的带日期名称证据")
            upper_name = active_name.upper()
            derived = derive_price_limit(
                code,
                day,
                pre_close,
                st="ST" in upper_name,
                delisting_period="退" in upper_name,
            )
            if derived.up_limit is None or derived.down_limit is None:
                continue
            rows.append(
                {
                    "ts_code": ts_code,
                    "trade_date": text,
                    "pre_close": pre_close,
                    "up_limit": derived.up_limit,
                    "down_limit": derived.down_limit,
                    "rule_version": derived.rule_version,
                    "source": "validated_derived",
                    "reference_source": "tushare.daily.pre_close",
                    "name_evidence_source": name_evidence_source,
                    "name_as_of": active_name,
                    "algorithm_version": ALGORITHM_VERSION,
                    "generated_at": generated_at.isoformat(),
                }
            )
        if not rows:
            raise RuntimeError(f"{code} 指定区间没有可派生记录")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".parquet.tmp")
        pd.DataFrame(rows).to_parquet(temporary, index=False)
        pending_outputs.append((temporary, target))
        evidence["partitions"].append(
            {
                "code": code,
                "ts_code": ts_code,
                "rows": len(rows),
                "names": names,
                "dated_name_periods": [
                    {
                        "start": period_start.isoformat(),
                        "end": period_end.isoformat() if period_end else None,
                        "name": name,
                    }
                    for period_start, period_end, name in periods
                ],
                "dated_name_mode": dated_name_mode,
                "name_evidence_source": name_evidence_source,
                "inferred_public_predecessor": inferred_predecessor,
                "dated_name_sources": [
                    {
                        "path": str(source_path),
                        "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                    }
                    for source_path in name_sources
                ],
                "public_name_cache": (
                    {
                        "path": str(cache_path),
                        "sha256": hashlib.sha256(cache_path.read_bytes()).hexdigest(),
                    }
                    if dated_name_mode == "tushare.namechange.dated"
                    else None
                ),
                "output": str(target),
            }
        )
    # 所有代码的数据、名称证据和规则推导都成功后才公开最终分区，避免
    # 多代码批次在后一个代码失败时留下“有数据文件、无证据报告”的半状态。
    for temporary, target in pending_outputs:
        os.replace(temporary, target)
    args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
    args.evidence_output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
