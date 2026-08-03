#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$ROOT_DIR/data"

cd "$ROOT_DIR/apps/api"

# 每日只增量同步最新净值。
# 历史 5 年回填为低频任务，请按需手动分批执行（断点续传，可反复跑）：
#   python -m app.services.sync_backfill_job --batch-size 20 --batch 0
MONEY_DATABASE_URL="sqlite:///$ROOT_DIR/data/money.db" \
python -m app.services.sync_job >> "$ROOT_DIR/data/sync.log" 2>&1
