#!/usr/bin/env bash
# Deploy only the local model service. Never rebuild/restart the detection API.
set -Eeuo pipefail

MODEL_NAME="${OLLAMA_DEPLOY_MODEL:-qwen3:4b}"
CONTAINER_NAME="aigc-ollama"
MODEL_NETWORK="aigc-models"

# Query the daemon rather than the CLI host (which may use a remote Docker context).
read -r AVAILABLE_CPUS AVAILABLE_MEMORY_BYTES <<< "$(docker info --format '{{.NCPU}} {{.MemTotal}}')"
if [[ ! "$AVAILABLE_CPUS" =~ ^[1-9][0-9]*$ ]] ||
    [[ ! "$AVAILABLE_MEMORY_BYTES" =~ ^[1-9][0-9]*$ ]]; then
    echo "错误：无法读取 Docker 可用 CPU / 内存，请检查 docker info。"
    exit 1
fi
GIB=$((1024 * 1024 * 1024))
DEFAULT_CPUS=$((AVAILABLE_CPUS > 1 ? AVAILABLE_CPUS - 1 : 1))
DEFAULT_MEMORY_GB=$((AVAILABLE_MEMORY_BYTES / GIB / 2))
(( DEFAULT_CPUS <= 6 )) || DEFAULT_CPUS=6
(( DEFAULT_MEMORY_GB <= 12 )) || DEFAULT_MEMORY_GB=12
(( DEFAULT_MEMORY_GB >= 1 )) || DEFAULT_MEMORY_GB=1
DEPLOY_CPUS="${OLLAMA_DEPLOY_CPUS:-$DEFAULT_CPUS}"
DEPLOY_MEMORY_GB="${OLLAMA_DEPLOY_MEMORY_GB:-$DEFAULT_MEMORY_GB}"
if [[ ! "$DEPLOY_CPUS" =~ ^[1-9][0-9]*$ ]] ||
    [[ ! "$DEPLOY_MEMORY_GB" =~ ^[1-9][0-9]*$ ]]; then
    echo "错误：OLLAMA_DEPLOY_CPUS / OLLAMA_DEPLOY_MEMORY_GB 必须为正整数。"
    exit 1
fi
if (( DEPLOY_CPUS > AVAILABLE_CPUS || DEPLOY_MEMORY_GB * GIB > AVAILABLE_MEMORY_BYTES )); then
    echo "错误：请求 ${DEPLOY_CPUS} 核 / ${DEPLOY_MEMORY_GB}GiB 超过 Docker 可用资源。"
    exit 1
fi
echo "Docker 可用 ${AVAILABLE_CPUS} 核 / $((AVAILABLE_MEMORY_BYTES / GIB))GiB；Ollama 上限 ${DEPLOY_CPUS} 核 / ${DEPLOY_MEMORY_GB}GiB。"

if ! docker network inspect "$MODEL_NETWORK" >/dev/null 2>&1; then
    docker network create "$MODEL_NETWORK" >/dev/null
fi

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "复用已有 aigc-ollama 容器及模型卷，更新资源上限。"
    docker update --cpus "$DEPLOY_CPUS" --memory "${DEPLOY_MEMORY_GB}g" \
        --memory-swap "$((DEPLOY_MEMORY_GB * 2))g" "$CONTAINER_NAME" >/dev/null
    docker start "$CONTAINER_NAME" >/dev/null
    if ! docker network inspect --format '{{range .Containers}}{{println .Name}}{{end}}' "$MODEL_NETWORK" |
        grep -Fxq "$CONTAINER_NAME"; then
        docker network connect "$MODEL_NETWORK" "$CONTAINER_NAME"
    fi
else
    docker run --detach \
        --name "$CONTAINER_NAME" \
        --restart unless-stopped \
        --cpus "$DEPLOY_CPUS" --memory "${DEPLOY_MEMORY_GB}g" \
        --memory-swap "$((DEPLOY_MEMORY_GB * 2))g" \
        --network "$MODEL_NETWORK" \
        --publish 127.0.0.1:11434:11434 \
        --volume aigc-ollama-models:/root/.ollama \
        --env OLLAMA_HOST=0.0.0.0:11434 \
        --env OLLAMA_NUM_PARALLEL=1 \
        --env OLLAMA_MAX_LOADED_MODELS=1 \
        --env OLLAMA_MAX_QUEUE=8 \
        --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
        "${OLLAMA_IMAGE:-ollama/ollama:latest}"
fi

ready=false
for ((attempt=1; attempt<=30; attempt++)); do
    if docker exec "$CONTAINER_NAME" ollama list >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 2
done
if [ "$ready" != true ]; then
    docker logs --tail 60 "$CONTAINER_NAME"
    echo "Ollama 启动超时。"
    exit 1
fi

if [ "${OLLAMA_DEPLOY_SKIP_PULL_IF_PRESENT:-false}" = true ] &&
    docker exec "$CONTAINER_NAME" ollama show "$MODEL_NAME" >/dev/null 2>&1; then
    echo "复用已有模型 $MODEL_NAME，跳过下载。"
else
    docker exec "$CONTAINER_NAME" ollama pull "$MODEL_NAME"
fi
docker exec "$CONTAINER_NAME" ollama list
echo "Ollama 已准备好：本机 http://127.0.0.1:11434；同网容器 http://aigc-ollama:11434。"
echo "检测项目未重启。下一步按 docs/OLLAMA_DOCKER.md 修改原 .env 并正常更新项目。"
