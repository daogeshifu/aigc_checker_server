# Ubuntu 8 核 / 32GB：四步部署原文概率评分

> 已有项目，或使用 4 核 / 16GB 机器时，优先使用 [统一 Docker 管理入口](DEPLOYMENT.md)。它复用原 `.env`、已有 Ollama 和下载的模型。本文保留为旧的 8 核 / 32GB 独立整套部署方案，请勿与新入口混合启动。

适用于现有 Ubuntu 22.04/24.04 x86_64 云服务器，不需要 GPU。先将本次更新的整个项目代码（包括新增文件）同步到服务器已有项目目录。本文的命令均在该目录执行。

部署使用 **Qwen3-4B-Q4_K_M** 单模型，固定文件 SHA256。Ollama 负责现有风格判别，独立的 llama-cpp-python 服务负责对原文做 teacher-forcing 评分。两者导入相同 GGUF 文件，分别限制 4 个 CPU 线程，模型服务各限制 10GB 内存；使用一个 API worker。模型下载约 2.5GB，Ollama 导入时还需要额外模型存储空间，建议部署前至少有 15GB 可用磁盘空间。

这次增加真实指标和部署能力，**尚未训练新的 AI 来源检测器或拟合 PPL → 风险分映射**。在有可信标注集之前保留现有评分算法；不把“PPL 越低”直接换算为 AI 百分比。切换判别模型本身可能改变分数，需要与旧部署做对照评测。

## 1. 检查机器并准备 Docker

```bash
lscpu
free -h
df -h .
docker version
docker compose version
```

若已经有可用的 Docker 和 Compose v2，直接进入第 2 步。若尚未安装，Ubuntu 可以安装以下发行版软件包；已有其他 Docker 安装时不要混装，使用原有安装来源的 Compose 插件。

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 python3 curl
sudo systemctl enable --now docker
```

以下 Docker 命令需要当前账号能访问 Docker daemon；无权限时以 `sudo` 执行 Docker/部署脚本。不要运行旧 `start.sh` 与本 Compose 占用同一个 8027 端口。

## 2. 生成独立配置并下载模型

```bash
python3 scripts/prepare_cpu.py --source modelscope
```

若魔搭下载不可用，可切换官方 Hugging Face 源：

```bash
python3 scripts/prepare_cpu.py --source huggingface
```

脚本生成权限为 `0600` 的 `.env.cpu`，原 `.env` 不变。已有非默认管理 Token 和 API 密钥会保留；公共默认 Token 会替换为随机值，不打印到终端。CPU 配置清空示例套餐 Token，默认关闭云端复核，并设置：

```dotenv
OLLAMA_MODEL=cpu-qwen4b
OLLAMA_NUM_THREAD=4
OLLAMA_MAX_CONCURRENCY=1
SCAN_CHUNK_CONCURRENCY=1
OLLAMA_TIMEOUT=600
PRO_MAX_CHARS=6000
PROBABILITY_ENABLED=true
PROBABILITY_TIMEOUT=600
SCORER_CONTEXT_TOKENS=1024
SCORER_STRIDE_TOKENS=512
REMOTE_REVIEW_ENABLED=false
```

重复执行会保留已有 `.env.cpu`；需要调整时编辑该文件。下载支持断点续传，两个下载源的文件都必须通过同一 SHA256 校验。校验失败时不会覆盖已有模型。默认模型的 SHA256：

```text
7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5
```

## 3. 构建并更新服务

```bash
bash scripts/deploy_cpu.sh
```

脚本先构建镜像，再启动 Ollama 并从 GGUF 导入 `cpu-qwen4b`，最后启动评分服务和主 API。首次需下载镜像并编译 CPU 推理库，耗时取决于网络与 CPU，不能把构建耗时当检测耗时。原有 `.env` 和 `data` 数据保留。

同一项目名下的现有 Compose 主容器会被更新。如果旧容器由其他方式创建，脚本会停止并重命名为带时间戳的备份容器，不删除它，以便回退。原生进程占用 8027 需要先由原部署方式停止。

访问入口：

- 主 API：`http://服务器地址:8027`，沿用 Bearer Token 鉴权。
- Ollama：仅 Compose 内网 `cpu-ollama:11434`，不占用宿主机已有 Ollama 端口。
- 原文评分：仅本机 `127.0.0.1:8091`，容器间用 Compose 内网访问。

状态和日志：

```bash
docker compose --env-file .env.cpu -p aigc-checker-server -f compose.cpu.yaml ps
docker compose --env-file .env.cpu -p aigc-checker-server -f compose.cpu.yaml logs --tail 80 cpu-scorer aigc-checker-server
```

## 4. 验证真实计算与延迟

```bash
python3 scripts/check_cpu.py
docker stats --no-stream
```

检查脚本从 `.env.cpu` 读取 Token，不打印 Token；提交固定中文测试文本，要求 Ollama 实际判别成功、原文评分成功且未使用云端复核。输出 `judge_modes`、总耗时、PPL、NLL、模型 SHA256、告警。

也可以单独检查原文评分：

```bash
curl --fail-with-body http://127.0.0.1:8091/health
curl --fail-with-body http://127.0.0.1:8091/score \
  -H 'Content-Type: application/json' \
  -d '{"text":"今天出门时忘了带伞，下午下雨，只好在便利店门口等了一会儿。","include_token_logprobs":true}'
```

主检测 `/api/scan` 的新增字段：

- `probability.status == "measured"`：评分成功。
- `probability.model_sha256/runtime/context_tokens/stride_tokens`：实验所需的模型和评分版本信息。
- `probability.nll/perplexity/nll_std/scored_tokens/elapsed_seconds`：原文实测指标。
- `probability.used_in_risk_score == false`：未用未经校准的公式更改风险分。
- `metrics.perplexity`、`metrics.nll`：文档级实测指标；没有按块 PPL 做算术平均。
- `metrics.binoculars`、旧分块代理字段依旧是 `null`。

评分对象是清洗后的整篇文本，无聊天提示词，也不生成回复。采用因果下一 token 对齐和滑动窗口，每个目标 token 只计一次；分词器按其配置处理 BOS，然后跳过序列位置 0。超出评分上限直接失败，**不截断后冒充全文结果**。忙碌时评分服务返回 429；主检测会返回 `probability.status == "unavailable"`、`probability_unavailable` 告警和 `null` PPL，现有判别仍可完成。

API 仍是同步请求。多篇请求可能排队或遇到评分忙碌；生产流量需要按实测限制并发，并相应设置客户端、网关和反向代理超时。单篇速度取决于 CPU 型号、共享资源竞争和文本 token 数，本方案没有预设吞吐承诺。

## 后续：用标注数据验证准确率

先保存真实 `/api/scan` 预测与人工/AI 来源标签，格式见主 README 的“离线评测”。同一原文、所有分块和改写版必须共用 `group_id`，且不能跨训练、校准、测试分组。先测真人误报率、AI 检出率与证据不足比例，再决定更换模型或拟合新权重。

```bash
python3 scripts/evaluate_detection.py data/labeled_predictions.jsonl \
  --split test --threshold 70 --output data/evaluation.json
```

上述命令要求先准备真实标注数据；默认 `70` 是现有风险分的比较基线，不是已通过验证的新阈值。没有标注数据时，完成的是部署、数值正确性和延迟验证，不能宣称准确率提高。

## 回退

临时只停用 PPL：将 `.env.cpu` 中 `PROBABILITY_ENABLED` 改为 `false`，再执行：

```bash
docker compose --env-file .env.cpu -p aigc-checker-server -f compose.cpu.yaml up -d --no-deps aigc-checker-server
docker compose --env-file .env.cpu -p aigc-checker-server -f compose.cpu.yaml stop cpu-scorer
```

重新启用时先启动 `cpu-scorer` 并检查健康状态，再重建 API。要完全返回原部署，停用 CPU 专属服务后使用原 `.env` 和 `compose.yaml` 构建并启动；不要删除 `data` 或执行 `down -v`。

## 验证范围与参考

数学对齐、滑窗覆盖、输入上限、忙碌保护、失败回退、旧风险分保持不变都有单元/接口测试。要运行全部 CPU 单元测试，仅需额外安装 NumPy，无需下载 GGUF：

```bash
python3 -m venv .venv-test
.venv-test/bin/pip install -r requirements.txt 'numpy>=1.26,<3'
.venv-test/bin/python -m unittest discover -s tests -v
```

- [Qwen 官方 GGUF 模型](https://huggingface.co/Qwen/Qwen3-4B-GGUF)
- [llama-cpp-python 原文 eval 与 logits 接口](https://llama-cpp-python.readthedocs.io/en/latest/api-reference/)
- [OpenBLAS CPU 编译选项](https://llama-cpp-python.readthedocs.io/en/latest/)
- [Ollama CPU 容器](https://docs.ollama.com/docker)
