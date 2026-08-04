#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$ROOT_DIR/data"

stop_process() {
  local name="$1"
  local pid_file="$2"

  if [[ ! -f "$pid_file" ]]; then
    echo "$name 没有 PID 文件"
    return
  fi

  local pid
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "$name 已停止（PID $pid）"
  else
    echo "$name 未运行"
  fi
  rm -f "$pid_file"
}

stop_process "后端 API" "$DATA_DIR/api.pid"
stop_process "前端 Web" "$DATA_DIR/web.pid"
stop_process "每日调度器" "$DATA_DIR/scheduler.pid"

# 调度器中的 A 股日线使用独立限时子进程；异常重启时一并清理，避免重复抓取。
pkill -f 'python -m app.services.sync_stock_daily_job' 2>/dev/null || true

# 清理脚本 PID 文件之外遗留的 Next.js 服务，避免 3000 端口被旧构建占用。
pkill -f 'next-server' 2>/dev/null || true
