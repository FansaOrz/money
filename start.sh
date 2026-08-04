#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_DIR="$ROOT_DIR/apps/api"
WEB_DIR="$ROOT_DIR/apps/web"
WEB_STANDALONE_DIR="$WEB_DIR/.next/standalone"
DATA_DIR="$ROOT_DIR/data"
NODE_BIN="/usr/local/nvm/versions/node/v20.20.2/bin"

mkdir -p "$DATA_DIR"

if [[ -d "$NODE_BIN" ]]; then
  export PATH="$NODE_BIN:$PATH"
fi

start_process() {
  local name="$1"
  local pid_file="$2"
  local log_file="$3"
  shift 3

  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "$name 已在运行（PID $(cat "$pid_file")）"
    return
  fi

  nohup "$@" >"$log_file" 2>&1 &
  echo $! >"$pid_file"
  echo "$name 已启动（PID $(cat "$pid_file")）"
}

if [[ ! -f "$DATA_DIR/money.db" ]]; then
  echo "未发现数据库，正在初始化并导入 PDF..."
  (cd "$API_DIR" && python -m app.services.bootstrap)
fi

(
  cd "$API_DIR"
  start_process "后端 API" "$DATA_DIR/api.pid" "$DATA_DIR/api.log" \
    env MONEY_DATABASE_URL="sqlite:///$DATA_DIR/money.db" \
        MONEY_RESEARCH_DATA_DIR="$DATA_DIR/research" \
        MONEY_RESEARCH_DB="$DATA_DIR/research/research.duckdb" \
        MONEY_CORS_ORIGINS='["http://localhost:3000","http://127.0.0.1:3000"]' \
        uvicorn app.main:app --host 0.0.0.0 --port 8001
)

if [[ ! -d "$WEB_DIR/.next" ]]; then
  echo "正在构建前端..."
  # 浏览器统一请求同源 /api，再由 Next.js 服务端代理到 8001。
  # 不能把 127.0.0.1:8001 编进客户端，否则远程访问者会请求自己电脑。
  (cd "$WEB_DIR" && NEXT_PUBLIC_API_URL= API_PROXY_URL=http://127.0.0.1:8001 npm run build)
fi

if [[ -f "$WEB_STANDALONE_DIR/server.js" ]]; then
  # standalone 构建不会自动携带静态资源，需要在启动前补到产物目录。
  mkdir -p "$WEB_STANDALONE_DIR/.next"
  cp -a "$WEB_DIR/.next/static" "$WEB_STANDALONE_DIR/.next/"
  start_process "前端 Web" "$DATA_DIR/web.pid" "$DATA_DIR/web.log" \
    env API_PROXY_URL=http://127.0.0.1:8001 \
        HOSTNAME=0.0.0.0 \
        PORT=3000 \
        node "$WEB_STANDALONE_DIR/server.js"
else
  start_process "前端 Web" "$DATA_DIR/web.pid" "$DATA_DIR/web.log" \
    env API_PROXY_URL=http://127.0.0.1:8001 \
        npm --prefix "$WEB_DIR" run start -- -p 3000
fi

(
  cd "$API_DIR"
  start_process "每日调度器" "$DATA_DIR/scheduler.pid" "$DATA_DIR/scheduler.log" \
    env MONEY_DATABASE_URL="sqlite:///$DATA_DIR/money.db" \
        MONEY_RESEARCH_DATA_DIR="$DATA_DIR/research" \
        MONEY_RESEARCH_DB="$DATA_DIR/research/research.duckdb" \
        python -m app.services.scheduler
)

# 净值同步只保留常驻调度器一个触发源（20:30 由 scheduler 触发），
# 不再注册 crontab，避免 cron 与调度器双触发同一任务；
# 若历史上配置过 sync_navs.sh 的 cron 项，这里顺带清理。
if command -v crontab >/dev/null 2>&1; then
  if crontab -l 2>/dev/null | grep -q "$ROOT_DIR/sync_navs.sh"; then
    (crontab -l 2>/dev/null | grep -v "$ROOT_DIR/sync_navs.sh" || true) | crontab -
    echo "已移除 sync_navs.sh 的 crontab 项（净值同步统一由常驻调度器触发）"
  fi
fi
echo "常驻调度器已配置：19:30/20:30/22:00 持仓基金净值、22:00 净值同步后自动模拟交易、17:30 市场指数、07:30 美股指数、16:10 A股行业/财务补齐、17:05 A股日线、18:30 A股前向模拟"

echo
echo "Web:   http://localhost:3000"
echo "API:   http://localhost:8001"
echo "日志:  $DATA_DIR/api.log, $DATA_DIR/web.log"
