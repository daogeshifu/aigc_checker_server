#!/usr/bin/env bash
# Deploy only the local model service. Never rebuild/restart the detection API.
set -Eeuo pipefail

MODEL_NAME="${OLLAMA_DEPLOY_MODEL:-qwen3:4b}"
CONTAINER_NAME="aigc-ollama"
MODEL_NETWORK="aigc-models"

docker info >/dev/null
if ! docker network inspect "$MODEL_NETWORK" >/dev/null 2>&1; then
    docker network create "$MODEL_NETWORK" >/dev/null
fi

if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "复用已有 aigc-ollama 容器，不修改其已有资源配置。"
    docker start "$CONTAINER_NAME" >/dev/null
    if ! docker network inspect --format '{{range .Containers}}{{println .Name}}{{end}}' "$MODEL_NETWORK" |
        grep -Fxq "$CONTAINER_NAME"; then
        docker network connect "$MODEL_NETWORK" "$CONTAINER_NAME"
    fi
else
    docker run --detach \
        --name "$CONTAINER_NAME" \
        --restart unless-stopped \
        --cpus 6 --memory 12g \
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

docker exec "$CONTAINER_NAME" ollama pull "$MODEL_NAME"
docker exec "$CONTAINER_NAME" ollama list
echo "Ollama 已准备好：本机 http://127.0.0.1:11434；同网容器 http://aigc-ollama:11434。"
echo "检测项目未重启。下一步按 docs/OLLAMA_DOCKER.md 修改原 .env 并正常更新项目。"
