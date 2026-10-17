# 已部署检测项目：独立 Docker 部署 Ollama

同时管理业务 API、Ollama 和原文概率评分时，使用 [统一 Docker 管理入口](DEPLOYMENT.md)：`sudo bash manage.sh up`。以下保留仅部署 Ollama 的说明。

你的检测项目沿用原来的部署流程。本指南只增加独立的 Ollama 容器，不运行 `deploy_cpu.sh`，不切换 `.env.cpu`，也不重建整套服务。

当前执行关系：

```text
已部署检测项目
  ├─ Ollama Qwen：分块风格判断 + 现有统计评分
  ├─ Azure / OpenRouter：按现有配置执行可选复核或改写
  └─ 可选独立评分服务：原文 token 概率 / PPL / NLL
```

代码支持通过 `.env` 的 `DETECTION_PROVIDER=ollama|bailian` 切换检测和表达扰动提供方。本文用于 `ollama` 模式；部署 Ollama 无需替换已有云端密钥。要启用真实 PPL，需另部署评分程序，单独启动 Ollama 并不会自动计算 PPL。远程复核仅使用 Azure，OpenRouter 仅保留语义改写兜底。

## 1. 部署 Ollama 容器

以下假设 Ubuntu 已安装 Docker，当前目录为更新后的项目根目录：

```bash
sudo bash scripts/deploy_ollama.sh
```

脚本只部署 `aigc-ollama` 容器、`aigc-models` 网络、持久化模型卷，并拉取 `qwen3:4b`。它读取 Docker 守护进程实际可用的 CPU / 内存：CPU 默认留出 1 核、最多使用 6 核；内存默认使用一半并向下取整到 GiB、最多 12GiB；生成并发为 1。参数超出机器资源时会在部署前退出。已有同名容器会复用并更新资源上限，模型卷保留；内存加交换空间总上限设为内存上限的两倍，实际交换空间取决于宿主机配置。脚本不会重启检测项目，也不会访问付费模型 API。若宿主机 11434 已有服务，先检查端口使用情况，再选择复用或调整新容器的主机端口；不要直接删除旧服务。

对于当前 **4 核 / 16GiB** 服务器，建议明确使用 3 核、8GiB：

```bash
sudo env OLLAMA_DEPLOY_CPUS=3 OLLAMA_DEPLOY_MEMORY_GB=8 \
  bash scripts/deploy_ollama.sh
```

这里的 `OLLAMA_DEPLOY_*` 是部署脚本环境变量；应用 `.env` 中的 `OLLAMA_NUM_THREAD` 控制推理线程，是另一项设置。实机报告内存略低于 16GiB 时，自动取一半可能得到 7GiB，显式指定 8GiB 仍可使用。

旧脚本若报 `range of CPUs ... only 4 CPUs available`，是此前固定的 `--cpus 6` 超过实际核数。可先在服务器项目目录临时修正旧脚本，再重新执行：

```bash
sed -i.bak 's/--cpus 6 --memory 12g/--cpus 3 --memory 8g/' scripts/deploy_ollama.sh
sudo bash scripts/deploy_ollama.sh
```

也可直接上传新版脚本后使用上述 `OLLAMA_DEPLOY_*` 参数。若之前已留下同名容器，新版脚本会先更新资源上限，再启动容器，无需删除模型数据。

针对 4 核 / 16GiB 机器，首次手工操作如下（选脚本或手工其中一种）：

```bash
sudo docker network create aigc-models
sudo docker run -d \
  --name aigc-ollama \
  --restart unless-stopped \
  --cpus 3 --memory 8g --memory-swap 16g \
  --network aigc-models \
  -p 127.0.0.1:11434:11434 \
  -v aigc-ollama-models:/root/.ollama \
  -e OLLAMA_HOST=0.0.0.0:11434 \
  -e OLLAMA_NUM_PARALLEL=1 \
  -e OLLAMA_MAX_LOADED_MODELS=1 \
  ollama/ollama:latest
sudo docker exec aigc-ollama ollama pull qwen3:4b
```

`qwen3:4b` 是 Ollama 官方库中约 2.5GB 的 Q4_K_M 量化版本。镜像下载大小、模型运行内存与模型文件大小不同，需以机器实测为准。

## 2. 验证模型确实能调用

```bash
curl --fail-with-body http://127.0.0.1:11434/api/tags
curl --fail-with-body --max-time 600 http://127.0.0.1:11434/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3:4b","prompt":"只回复：OK","think":false,"stream":false,"options":{"num_thread":3,"num_ctx":2048,"num_predict":32}}'
```

第一个响应的模型列表应包含 `qwen3:4b`，第二个应有非空 `response`。若失败，先看 Ollama 日志，此时还不需要修改检测项目。

```bash
sudo docker logs --tail 80 aigc-ollama
```

## 3. 接入原项目的 .env

检测 API 与 Ollama 都是 Docker 容器时，通信路径为 `aigc-checker-server → aigc-models 共享网络 → aigc-ollama:11434`。宿主机上 `curl 127.0.0.1:11434` 成功，只能证明宿主机能够访问 Ollama，不能证明 API 容器已经连通。`OLLAMA_DOCKER_NETWORK` 由更新后的 `disploy.sh` 读取；仅填写 `.env` 本身不会创建网络或连接容器。

可直接参考 [4 核 / 16GiB 完整配置模板](ubuntu-4c16g.env.example)，将其中全部 `REPLACE_WITH_*` 替换为有效 Token / 密钥后使用。

如果检测项目也运行在 Docker 中，编辑原来的 `.env`，修改或添加以下项。原有 Azure / OpenRouter / Token 配置保留：

```dotenv
OLLAMA_BASE_URL=http://aigc-ollama:11434
OLLAMA_DOCKER_NETWORK=aigc-models
OLLAMA_MODEL=qwen3:4b
OLLAMA_REWRITE_MODEL=qwen3:4b
OLLAMA_NUM_THREAD=3
OLLAMA_TIMEOUT=600
OLLAMA_MAX_CONCURRENCY=1
SCAN_CHUNK_CONCURRENCY=1
OLLAMA_JSON_NUM_CTX=2048
OLLAMA_JSON_NUM_PREDICT=256
OLLAMA_DISABLE_THINKING=true
PROBABILITY_ENABLED=false
```

上面的 `OLLAMA_REWRITE_MODEL` 同时指向本次拉取的模型。若要保留原来的其他改写模型，需先在新 Ollama 中另外拉取它，再恢复该配置并验证改写效果。

如果检测项目是 Ubuntu 上直接运行的 Python 进程，则使用 `OLLAMA_BASE_URL=http://127.0.0.1:11434`，并将 `OLLAMA_DOCKER_NETWORK` 留空。容器里的 `127.0.0.1` 指向容器自身，不能用它连接另一个容器。

本步骤暂不启用独立 PPL 服务，因此 `PROBABILITY_ENABLED=false`。之前已正确部署评分服务的用户可保留 `true` 和实际可访问的评分地址。已有云端 API 配置与远程复核开关由你原来的 `.env` 决定，本指南不替换它们。

## 4. 按原方式更新检测项目

更新包代码已同步到服务器后，使用项目原来的脚本即可：

```bash
sudo env SKIP_GIT_PULL=1 bash disploy.sh
```

`SKIP_GIT_PULL=1` 适用于直接上传本次更新包的情况；通过 Git 分发更新时可照常运行 `bash disploy.sh`。更新后的脚本读取 `OLLAMA_DOCKER_NETWORK`，自动将重建后的 API 容器连接到模型网络。后续正常更新也会恢复此连接，避免模型地址更新后失联。

从检测容器内部验证连通性：

```bash
sudo docker exec aigc-checker-server python -c \
  'import os,json,urllib.request; base=os.environ["OLLAMA_BASE_URL"].rstrip("/"); print("实际模型地址:",base); data=json.load(urllib.request.urlopen(base+"/api/tags",timeout=10)); print([m["name"] for m in data["models"]])'
```

应先输出 `http://aigc-ollama:11434`，再输出含 `qwen3:4b` 的模型列表。若仍输出原远端地址，说明运行中的 API 容器还没有重新加载更新后的环境变量；单独 `docker restart` 不会刷新通过 `env_file` 注入的配置，需要按上述流程重建 API 容器。

随后调用原来的 `/api/scan` 或测试页面，确认实际结果的 `engine.qwen_model` 是 `qwen3:4b`，正常判别分块的 `judge_mode` 是 `qwen`。短文本会按现有规则跳过模型，验证时使用超过 200 字且有多句的自然文本。`heuristic_fallback` 说明模型调用或输出格式失败；继续检查日志，不要把回退当作模型成功。

如果检测项目原来直接用 Python 进程运行，则按原来的进程管理方式重启，毋需执行 Docker 项目更新脚本。

## 真实 PPL 的后续接入

Ollama 这一步解决现有模型判别调用。原文概率需要独立的 `cpu_scoring` 服务；其接入已做成可选项，默认关闭，失败时显式告警。没有标注数据时，PPL 不参与未经验证的 AI 风险分换算，Binoculars 尚未实现。

当前 4 核 / 16GiB 机器先保持 `PROBABILITY_ENABLED=false`，验证 Ollama 的耗时和内存占用；下面两套模型服务同时常驻的示例按此前 8 核 / 32GiB 配置编写，不能直接照搬到当前服务器。

要在原项目旁边单独增加 PPL 服务，可执行以下命令；它们不会重建检测项目，也不生成或切换 `.env.cpu`：

```bash
python3 scripts/prepare_cpu.py --download-only --source modelscope
sudo docker build -f cpu_scoring/Dockerfile -t aigc-cpu-scorer:0.1 .
sudo docker run -d \
  --name aigc-cpu-scorer \
  --restart unless-stopped \
  --cpus 4 --memory 10g \
  --network aigc-models \
  -p 127.0.0.1:8091:8091 \
  -v "$PWD/models:/models:ro" \
  -e SCORER_MODEL_PATH=/models/Qwen3-4B-Q4_K_M.gguf \
  -e SCORER_THREADS=4 \
  aigc-cpu-scorer:0.1
```

先检查评分服务已成功加载模型：

```bash
curl --fail-with-body http://127.0.0.1:8091/health
```

返回 `status=ok` 后，修改原 `.env` 中 `PROBABILITY_ENABLED=true`、`PROBABILITY_BASE_URL=http://aigc-cpu-scorer:8091`，并将 `OLLAMA_NUM_THREAD=4`，然后按第 4 步正常更新检测项目。原生 Python 部署使用评分地址 `http://127.0.0.1:8091`。调用 `/api/scan`，确认 `probability.status=measured` 与非空 `metrics.perplexity`；失败则查看评分容器日志。

## 参考

- [Ollama 官方 CPU Docker 部署](https://docs.ollama.com/docker)
- [qwen3:4b 官方模型](https://ollama.com/library/qwen3:4b)
