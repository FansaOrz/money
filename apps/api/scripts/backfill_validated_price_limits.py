"""补齐 Tushare 空分区：公开名称证据 + daily.pre_close + 交易所规则。

本脚本不会覆盖已有 stk_limit Parquet。派生记录保留参考价、规则版本、
算法版本和外部名称证据，便于文件清单冻结及事后复核。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.historical_price_limits import (  # noqa: E402
    ALGORITHM_VERSION,
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
    return parser.parse_args()


def qualified_code(code: str) -> str:
    normalized = str(code).split(".")[0].zfill(6)
    exchange = "SH" if normalized.startswith(("5", "6", "9")) else "SZ"
    return f"{normalized}.{exchange}"


def historical_names(code: str) -> list[str]:
    import akshare as ak

    frame = ak.stock_info_change_name(symbol=code)
    if "name" not in frame:
        raise RuntimeError(f"{code} 公开名称接口缺少 name 字段")
    return [
        str(value).strip()
        for value in frame["name"].tolist()
        if str(value).strip()
    ]


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
        "name_evidence_source": "akshare.stock_info_change_name",
        "partitions": [],
    }
    for raw_code in args.codes.split(","):
        code = raw_code.strip().split(".")[0].zfill(6)
        if not code:
            continue
        if not code.startswith(("00", "001", "002", "003", "6")):
            raise RuntimeError(f"{code} 不是本脚本允许的沪深主板证券")
        names = historical_names(code)
        if not names_prove_non_st(names):
            raise RuntimeError(f"{code} 历史名称含 ST/退市标识，拒绝普通股派生")
        ts_code = qualified_code(code)
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
            derived = derive_price_limit(
                code,
                day,
                pre_close,
                st=False,
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
                    "name_evidence_source": "akshare.stock_info_change_name",
                    "algorithm_version": ALGORITHM_VERSION,
                    "generated_at": generated_at.isoformat(),
                }
            )
        if not rows:
            raise RuntimeError(f"{code} 指定区间没有可派生记录")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".parquet.tmp")
        pd.DataFrame(rows).to_parquet(temporary, index=False)
        os.replace(temporary, target)
        evidence["partitions"].append(
            {
                "code": code,
                "ts_code": ts_code,
                "rows": len(rows),
                "names": names,
                "output": str(target),
            }
        )
    args.evidence_output.parent.mkdir(parents=True, exist_ok=True)
    args.evidence_output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
