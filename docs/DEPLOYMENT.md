# 统一 Docker 管理入口

适用于已经部署业务 API 和 `aigc-ollama` 的服务器，也支持补齐缺少的服务。入口为项目根目录的 `manage.sh`；运行需要 Docker、Python 3.10+，不要求宿主机安装 Python 依赖或 Docker Compose。API 部署会在有 Compose 插件时使用它，否则使用 Docker。

## 按提供方选择容器

| 容器 | 作用 | 模型/数据 |
|---|---|---|
| `aigc-checker-server` | Demo、鉴权、统计特征、评分融合、远程复核和任务记录 | `.env`、`data/` |
| `aigc-ollama` | 生成 Qwen 判断 JSON、降 AI 时的表达扰动 | 已有 `aigc-ollama-models` 卷；已有容器的挂载保持原样 |
| `aigc-cpu-scorer` | 独立测量原文 token 概率并汇总 NLL/PPL | 只读挂载 `models/Qwen3-4B-Q4_K_M.gguf` |

本地容器通过 `aigc-models` 网络通信。API 端口为 8027；新增模型容器的宿主机端口 11434/8091 仅绑定 127.0.0.1。API 在容器内通过服务名称访问模型，不使用宿主机的 127.0.0.1。

| `.env` 的检测提供方 | 评分开启 | 运行容器 |
|---|---|---|
| `DETECTION_PROVIDER=ollama` | 是 | 业务 + Ollama + 评分，三个 |
| `DETECTION_PROVIDER=ollama` | 否 | 业务 + Ollama，两个 |
| `DETECTION_PROVIDER=bailian` | 是 | 业务 + 评分，两个 |
| `DETECTION_PROVIDER=bailian` | 否 | 仅业务，一个 |

百炼模式下，分块判断 JSON 和降 AI 的表达扰动均使用百炼 Qwen，字段校验、短文本路由、失败统计回退和评分权重保持一致。切换后统一入口会在 API 验证通过后停止已有 Ollama 容器，保留模型卷。不会自动退回本地 Ollama。

概率测量独立于判断提供方：仍由 GGUF 评分服务提供，百炼生成的 JSON 不是原文 token 概率。PPL 暂不参与风险分，Binoculars 未实现。

## 百炼与 Azure 配置

在现有 `.env` 中设置，不要将密钥放到 `deploy.env`：

```dotenv
DETECTION_PROVIDER=bailian
BAILIAN_API_KEY=填写你的百炼API密钥
BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
BAILIAN_MODEL=qwen-plus
BAILIAN_REWRITE_MODEL=qwen-plus
BAILIAN_TIMEOUT=120
BAILIAN_MAX_CONCURRENCY=2
BAILIAN_JSON_MAX_TOKENS=512

# 继续使用已有 Azure key、endpoint、API version；这里填实际 deployment name。
REMOTE_REVIEW_ENABLED=true
AZURE_REVIEW_DEPLOYMENT=填写已有Azure部署名称
```

示例 BASE_URL 对应北京旧域名，仍可使用；API Key 必须与地域匹配。其他地域或业务空间专属地址请按[百炼兼容 API 文档](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)填写，地址截至 `/compatible-mode/v1`，不要包含 `/chat/completions`。选择支持 JSON 输出和非思考模式的 Qwen 模型。

复核固定为 Azure。`AZURE_REVIEW_DEPLOYMENT` 留空时沿用 `AZURE_DEPLOYMENT`；部署入口会将旧 `PRO_REVIEW_SOURCE=openrouter` 迁移为 Azure，并避免把旧 OpenRouter model ID 当作 Azure deployment。保留有效的已有 Azure 复核 deployment。

OpenRouter 仅保留在另一条链路：降 AI 的语义改写仍是 Azure 优先、OpenRouter 兜底，**不参与复核**。`REMOTE_REVIEW_ENABLED=false` 可关闭复核。

改回本地模式时设置 `DETECTION_PROVIDER=ollama`，在 `deploy.env` 指定 Ollama 模型，然后执行完整 `up`。网页显示实际服务器提供方，不能从前端切换它；请求 `model` 仅在 `api_source` 与实际提供方一致时覆盖模型名。

## 第一次使用

将当前代码更新到服务器原项目目录，保留 `.env`、`data/`、`models/`。在原目录执行：

```bash
cd /home/html/aigc_checker_server
sudo bash manage.sh up
sudo bash manage.sh check --score
```

`up` 默认启用评分服务，依次执行：

1. 从 `deploy.env.example` 创建 `deploy.env`，首次沿用 `.env` 中已有的 `OLLAMA_MODEL`；检查 Docker 实际 CPU/内存配额。
2. 校验已下载 GGUF 的 SHA256；文件缺失时才下载。已运行 `prepare_cpu.py --download-only` 的用户无需再下载。
3. 构建评分和 API 镜像。构建失败时现有容器继续运行。
4. 本地模式复用 Ollama 容器和模型卷，设置资源配额；目标模型已存在则跳过拉取。百炼模式跳过此步。Ollama 与 GGUF 评分文件是两份独立存储，已有模型不会被删除。
5. 创建或更新评分容器，等待模型加载和健康检查。更新失败时尝试恢复旧评分容器；模型文件始终只读挂载。
6. 备份并更新 `.env` 的路由、模型、并发、Azure 复核与概率配置，重建 API，检查所需本地服务。百炼模式额外检查配置完整性。

首次构建要编译 llama.cpp，首次模型加载也需要时间。`up` 完成只代表服务就绪；`check --score` 才会实际测量一段短文本的 PPL。两个检查都不会请求付费云模型：百炼检查返回 `configured_not_called`，实际 key、额度、模型权限需通过 Demo 的真实检测确认。它们不代表检测准确率已验证。

## 配置放在哪里

- `.env`：`DETECTION_PROVIDER`、业务 Token、云端 API 密钥、套餐字数、复核设置和业务超时。
- `deploy.env`：部署模型、是否启用评分、容器资源和评分窗口。无须再手写 Docker 参数。

可先生成并查看不含密钥的部署配置：

```bash
sudo bash manage.sh config
```

4 核 / 16GiB 自动配额一般为 Ollama 2 核、评分服务 1 核，模型内存上限各 5～6GiB（取决于 Docker 报告的实际内存）。这为系统和现有业务保留资源，但不保证实时响应或其他业务可用内存；长文和并发需要实际测量。评分容器禁止额外 swap，避免交换造成更长延迟。大于 4B 的模型需要另行调整资源。

百炼模式不分配 Ollama 资源，评分服务的 auto 默认使用最多 2 核 / 6GiB。已手动填写的评分配额会保留。

例如在 `deploy.env` 调整：

```dotenv
OLLAMA_DEPLOY_MODEL=qwen3:4b
OLLAMA_DEPLOY_CPUS=2
OLLAMA_DEPLOY_MEMORY_GB=5
SCORER_ENABLED=true
SCORER_CPUS=1
SCORER_MEMORY_GB=5
SCORER_THREADS=1
SCORER_CONTEXT_TOKENS=1024
SCORER_STRIDE_TOKENS=512
SCORER_WAIT_SECONDS=600
MODEL_SOURCE=modelscope
```

本地模式下 `up` 会同步 `.env` 中的（百炼模式不会覆盖 Ollama 模型设置）：

```dotenv
OLLAMA_BASE_URL=http://aigc-ollama:11434
OLLAMA_DOCKER_NETWORK=aigc-models
OLLAMA_MODEL=qwen3:4b
OLLAMA_REWRITE_MODEL=qwen3:4b
OLLAMA_NUM_THREAD=2
OLLAMA_MAX_CONCURRENCY=1
SCAN_CHUNK_CONCURRENCY=1
PROBABILITY_ENABLED=true
PROBABILITY_BASE_URL=http://aigc-cpu-scorer:8091
```

模型名和线程数来自部署配置。现有 `OLLAMA_TIMEOUT`、`PROBABILITY_TIMEOUT`、JSON 上下文/输出长度保留，缺失时补默认值；CPU 请求建议在 `.env` 将两个超时设置为 `600`。API 密钥、套餐 Token、字数限制和远程复核开关均保留。每次配置变化前保存权限为 0600 的 `.env.backup.*`，不要上传这些备份。

## 日常操作

| 目的 | 命令 |
|---|---|
| 部署/更新全部服务 | `sudo bash manage.sh up` 或 `sudo bash manage.sh update` |
| 仅更新业务代码或 Token | `sudo bash manage.sh update api` |
| 仅重建评分服务 | `sudo bash manage.sh update scorer` |
| 准备 Ollama 模型、调整资源 | `sudo bash manage.sh update ollama` |
| 查看容器状态 | `sudo bash manage.sh status` |
| 查看 API 日志 | `sudo bash manage.sh logs api` |
| 跟随评分日志 | `sudo bash manage.sh logs scorer -f` |
| 查看 Ollama 日志 | `sudo bash manage.sh logs ollama` |
| 检查业务容器内的服务连通性 | `sudo bash manage.sh check` |
| 实际测量一段原文 PPL | `sudo bash manage.sh check --score` |
| 重启正在运行的服务 | `sudo bash manage.sh restart` |
| 停止三个服务，保留模型和数据 | `sudo bash manage.sh stop` |
| 停止后的完整恢复 | `sudo bash manage.sh up` |
| 仅下载/校验评分模型 | `bash manage.sh download` |

`restart` 不会重新读取容器外 `.env`，配置更新请使用 `update`。单独更新模型服务不重建 API；改变模型名或评分开关后，应执行完整 `up`。Ollama 更新复用已有容器，不自动替换已有 Ollama 引擎镜像。

管理入口部署当前目录代码，**不自动 git pull**。通过 Git 更新时先自行 `git pull --ff-only`；上传更新包时直接解压再执行 `up` 即可。

`tar -xzf 更新包.tar.gz` 只负责把压缩包中的代码解压到当前目录，不会启动或重启 Docker。使用 Git 更新源码时无需执行 tar。更新包不包含线上 `.env`、`deploy.env`、模型及业务数据。

任务响应保留 `judge_mode=qwen` 和历史 `execution_summary.local` 字段以兼容调用方；该字段现在表示主判别器，真实提供方看其中的 `provider` 和 `display_name`，百炼模式会明确显示百炼，避免误报本地调用。

## 暂时关闭概率评分

在 `deploy.env` 设置：

```dotenv
SCORER_ENABLED=false
```

然后执行 `sudo bash manage.sh up`。入口会先将 API 的 `PROBABILITY_ENABLED` 改为 false 并重建 API，再停止评分容器。数据和模型保留；重新改为 true 后执行 `up` 即可启用。

检测结果中 `probability.status=measured` 才是评分成功；`disabled` 为关闭；`unavailable` 表示调用失败。`used_in_risk_score=false` 是当前预期行为。

## 旧入口与验证范围

`disploy.sh` 仍可单独管理 API，`scripts/deploy_ollama.sh` 仍可单独准备 Ollama。新的统一入口复用它们，业务 Compose 只重建 API，不清除同项目中的其他服务。

`compose.cpu.yaml` / `scripts/deploy_cpu.sh` 是旧的 8 核 / 32GB 独立整套方案，使用 `.env.cpu` 和另一组容器。当前 4 核机器请使用本文入口，勿混合启动两套。

部署配置、模型复用、环境保留、资源校验、失败恢复及容器网络均有模拟测试。实际 Docker 镜像构建和模型推理需要在服务器完成，代码交付本身不表示线上已经部署或完成性能验证。
