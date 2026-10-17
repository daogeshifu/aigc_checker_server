#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."

docker info >/dev/null
docker compose version >/dev/null
test -f .env.cpu || { echo "请先运行 python3 scripts/prepare_cpu.py"; exit 1; }
python3 - <<'PY'
from scripts.prepare_cpu import ROOT, MODEL_FILE, MODEL_SHA256, checksum
path = ROOT / 'models' / MODEL_FILE
if not path.is_file() or checksum(path) != MODEL_SHA256:
    raise SystemExit('模型缺失或 SHA256 不匹配，请先运行 python3 scripts/prepare_cpu.py')
PY

COMPOSE=(docker compose --env-file .env.cpu --project-name aigc-checker-server -f compose.cpu.yaml)
"${COMPOSE[@]}" config --quiet
# Build all images before replacing the running API container.
"${COMPOSE[@]}" build --pull
"${COMPOSE[@]}" up -d --wait --wait-timeout 180 cpu-ollama
# Import the very same GGUF used for original-text scoring; no second model download.
printf 'FROM /models/Qwen3-4B-Q4_K_M.gguf\nPARAMETER num_thread 4\nPARAMETER num_ctx 2048\n' |
    "${COMPOSE[@]}" exec -T cpu-ollama sh -c 'cat > /tmp/cpu.Modelfile; ollama create cpu-qwen4b -f /tmp/cpu.Modelfile'

# Preserve an existing standalone container for rollback instead of deleting it.
if docker container inspect aigc-checker-server >/dev/null 2>&1; then
    project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' aigc-checker-server)"
    if [ "$project" != "aigc-checker-server" ]; then
        backup="aigc-checker-before-cpu-$(date +%Y%m%d%H%M%S)"
        docker stop aigc-checker-server
        docker rename aigc-checker-server "$backup"
        echo "旧容器已停止并保留为 $backup，可用于回退；未删除其数据。"
    fi
fi
"${COMPOSE[@]}" up -d --wait --wait-timeout 900
"${COMPOSE[@]}" ps
echo "CPU 部署完成。请运行：python3 scripts/check_cpu.py"
