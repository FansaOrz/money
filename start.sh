#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$PROJECT_DIR/infra/docker-compose.yml"
ENV_FILE="$PROJECT_DIR/.env"

if ! command -v docker >/dev/null 2>&1; then
  echo "缺少 Docker；生产式启动必须由 Docker Compose 监督进程。" >&2
  exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "缺少 $ENV_FILE；请配置 PostgreSQL、API 身份凭据和备份密钥。" >&2
  exit 1
fi

echo "启动安全摘要："
echo "  绑定范围: 127.0.0.1（仅 loopback）"
echo "  环境: production"
echo "  认证: 强制 readonly/admin 身份；高风险操作另需 MFA 短期身份和双人审批"
echo "  传输: 对远程明文请求 fail closed；跨主机部署必须由 TLS/可信反代终止"

echo "正在由 Docker Compose 构建、迁移并启动受监督服务..."
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up \
  --build --detach --wait

echo "服务 readiness 已通过；状态如下："
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps
echo "Web: http://127.0.0.1:${MONEY_WEB_PORT:-3000}"
echo "API: https://127.0.0.1:${MONEY_API_TLS_PORT:-8443}"
