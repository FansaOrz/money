"""Import normalized StockToday snapshot datasets into the application database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许直接执行 ``python scripts/import_stocktoday_snapshot.py``。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal
from app.services.research.stock_fundamentals import (
    import_stocktoday_financial_indicators,
    import_stocktoday_industries,
    import_stocktoday_name_history,
    import_stocktoday_valuations,
)
from app.services.research.stock_universe import import_stocktoday_index_weights


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=Path("../../data/research/tushare_snapshot"),
    )
    parser.add_argument(
        "--dataset",
        choices=(
            "all",
            "index-weight",
            "valuations",
            "financials",
            "name-history",
            "industries",
        ),
        default="all",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with SessionLocal() as db:
        if args.dataset == "index-weight":
            result = import_stocktoday_index_weights(
                db, args.snapshot_dir.resolve()
            )
        elif args.dataset == "valuations":
            result = import_stocktoday_valuations(
                db, args.snapshot_dir.resolve()
            )
        elif args.dataset == "financials":
            result = import_stocktoday_financial_indicators(
                db, args.snapshot_dir.resolve()
            )
        elif args.dataset == "name-history":
            result = import_stocktoday_name_history(
                db, args.snapshot_dir.resolve()
            )
        elif args.dataset == "industries":
            result = import_stocktoday_industries(
                db, args.snapshot_dir.resolve()
            )
        else:
            datasets = {
                "index_weight": import_stocktoday_index_weights(
                    db, args.snapshot_dir.resolve()
                ),
                "valuations": import_stocktoday_valuations(
                    db, args.snapshot_dir.resolve()
                ),
                "financials": import_stocktoday_financial_indicators(
                    db, args.snapshot_dir.resolve()
                ),
                "name_history": import_stocktoday_name_history(
                    db, args.snapshot_dir.resolve()
                ),
                "industries": import_stocktoday_industries(
                    db, args.snapshot_dir.resolve()
                ),
            }
            statuses = {item["status"] for item in datasets.values()}
            result = {
                "status": (
                    "failed"
                    if statuses == {"failed"}
                    else "partial"
                    if "failed" in statuses or "partial" in statuses
                    else "success"
                ),
                "datasets": datasets,
            }
    print(json.dumps(result, ensure_ascii=False, default=str))
    if result["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
