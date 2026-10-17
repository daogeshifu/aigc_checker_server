#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

CONTAINER_NAME="aigc-checker-server"
IMAGE_NAME="aigc-checker-server:latest"
export COMPOSE_PROJECT_NAME="aigc-checker-server"

if ! command -v docker >/dev/null 2>&1; then
    echo "错误：未安装 Docker。"
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "错误：无法连接 Docker，请启动 Docker 服务并检查当前用户权限。"
    exit 1
fi

if [ ! -f .env ]; then
    echo "错误：缺少 ${SCRIPT_DIR}/.env。"
    echo "请先执行：cp .env.example .env，并填写 Token、Ollama 地址及 API 密钥。"
    exit 1
fi

# 使用 Compose 插件；旧版 docker-compose v1 走纯 Docker 兼容模式。
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose --project-name "${COMPOSE_PROJECT_NAME}" -f "${SCRIPT_DIR}/compose.yaml")
    DEPLOY_MODE="compose"
    echo ">>> 使用 Docker Compose 插件"
else
    DEPLOY_MODE="docker"
    echo ">>> 未检测到 Docker Compose 插件，使用纯 Docker 兼容模式"
fi

echo ">>> 1/5 拉取最新代码"
if [ "${SKIP_GIT_PULL:-0}" = "1" ]; then
    echo "SKIP_GIT_PULL=1，使用当前目录代码。"
elif command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git pull --ff-only
else
    echo "未安装 Git 或当前目录不是 Git 仓库，跳过 git pull。"
fi

echo ">>> 2/5 校验配置并准备数据目录"
# Optional local model network; remote Ollama deployments keep their old behavior.
# Read just this literal setting, without executing .env as shell code or printing keys.
MODEL_NETWORK="${OLLAMA_DOCKER_NETWORK:-}"
if [ -z "$MODEL_NETWORK" ]; then
    MODEL_NETWORK="$(python3 - <<'PY'
from pathlib import Path
import shlex
value = ''
for line in Path('.env').read_text(encoding='utf-8').splitlines():
    key, separator, raw = line.partition('=')
    if separator and key.strip() == 'OLLAMA_DOCKER_NETWORK':
        parts = shlex.split(raw, comments=True)
        if len(parts) > 1:
            raise SystemExit('OLLAMA_DOCKER_NETWORK 必须为单个网络名称')
        value = parts[0] if parts else ''
print(value)
PY
)"
fi
if [ -n "$MODEL_NETWORK" ]; then
    if [[ ! "$MODEL_NETWORK" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
        echo "错误：OLLAMA_DOCKER_NETWORK 格式不合法。"
        exit 1
    fi
    if ! docker network inspect "$MODEL_NETWORK" >/dev/null 2>&1; then
        echo "错误：模型网络 $MODEL_NETWORK 尚不存在，请先运行 bash scripts/deploy_ollama.sh。"
        exit 1
    fi
fi
if [ "${DEPLOY_MODE}" = "compose" ]; then
    "${COMPOSE[@]}" config --quiet
fi
mkdir -p "${SCRIPT_DIR}/data"

# 先构建，确保构建失败时现有容器继续运行。
echo ">>> 3/5 构建镜像"
if [ "${DEPLOY_MODE}" = "compose" ]; then
    "${COMPOSE[@]}" build --pull
else
    docker build --pull --tag "${IMAGE_NAME}" .
fi

echo ">>> 4/5 重建并启动容器"
if docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
    EXISTING_PROJECT="$(
        docker container inspect \
            --format '{{ index .Config.Labels "com.docker.compose.project" }}' \
            "${CONTAINER_NAME}"
    )"
    if [ "${DEPLOY_MODE}" = "docker" ] || [ "${EXISTING_PROJECT}" != "${COMPOSE_PROJECT_NAME}" ]; then
        docker container rm --force "${CONTAINER_NAME}"
    fi
fi

if [ "${DEPLOY_MODE}" = "compose" ]; then
    "${COMPOSE[@]}" up --detach --force-recreate --remove-orphans
else
    docker run \
        --detach \
        --name "${CONTAINER_NAME}" \
        --publish 8027:8027 \
        --env-file .env \
        --env TASK_LOG_DB_PATH=/app/data/task_logs.db \
        --volume "${SCRIPT_DIR}/data:/app/data" \
        --add-host host.docker.internal:host-gateway \
        --restart unless-stopped \
        --log-driver json-file \
        --log-opt max-size=10m \
        --log-opt max-file=3 \
        "${IMAGE_NAME}"
fi

if [ -n "$MODEL_NETWORK" ]; then
    if ! docker network inspect --format '{{range .Containers}}{{println .Name}}{{end}}' "$MODEL_NETWORK" |
        grep -Fxq "$CONTAINER_NAME"; then
        docker network connect "$MODEL_NETWORK" "$CONTAINER_NAME"
    fi
fi

show_logs() {
    docker container logs --tail 100 "${CONTAINER_NAME}" || true
}

echo ">>> 5/5 等待健康检查"
for ((attempt = 1; attempt <= 36; attempt++)); do
    STATE="$(
        docker container inspect \
            --format '{{.State.Status}}' "${CONTAINER_NAME}" 2>/dev/null || true
    )"
    HEALTH="$(
        docker container inspect \
            --format '{{ if .State.Health }}{{ .State.Health.Status }}{{ else }}unknown{{ end }}' \
            "${CONTAINER_NAME}" 2>/dev/null || true
    )"
    if [ "${HEALTH}" = "healthy" ] && [ "${STATE}" = "running" ]; then
        echo "部署完成：http://127.0.0.1:8027"
        echo "API 文档：http://127.0.0.1:8027/docs"
        docker container ps --filter "name=^/${CONTAINER_NAME}$"
        exit 0
    fi
    if [ "${HEALTH}" = "unhealthy" ] || [[ "${STATE}" =~ ^(exited|dead|restarting)$ ]]; then
        echo "容器启动或健康检查失败，最近日志如下："
        show_logs
        exit 1
    fi
    printf '等待容器启动（%s/36，状态：%s，健康：%s）...\n' \
        "${attempt}" "${STATE:-unknown}" "${HEALTH:-unknown}"
    sleep 5
done

echo "等待健康检查超时，最近日志如下："
show_logs
exit 1
