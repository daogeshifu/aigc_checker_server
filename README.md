# AIGC Checker Server

## 启动与更新：先看这里

**服务器统一使用 `manage.sh` 管理。首次部署用 `up`，只改配置用 `reload api`，只改业务代码用 `update api`。** 需要 Docker 和 Python 3.10+，以下命令均在服务器项目目录执行。

### 1. 首次启动

上传或拉取最新代码后，准备配置；已有配置会保留：

```bash
cd /home/html/aigc_checker_server
test -f .env || cp .env.example .env
test -f deploy.env || cp deploy.env.example deploy.env
```

先编辑 `.env`，填写访问 Token、模型提供方和对应密钥：`DETECTION_PROVIDER=bailian` 使用百炼，`ollama` 使用本地 Ollama。Azure 复核使用 `.env` 中的 Azure 配置；是否启用独立 PPL 服务由 `deploy.env` 的 `SCORER_ENABLED=true/false` 决定。

配置完成后启动：

```bash
sudo bash manage.sh up
sudo bash manage.sh status
sudo bash manage.sh check
```

启用评分服务时，可再执行 `sudo bash manage.sh check --score` 验证实际 PPL 测量。首次需要构建镜像，缺少评分模型时还会下载模型；已有模型会校验并复用。启动后访问：`http://服务器IP:8027/demo`，接口文档：`http://服务器IP:8027/docs`。

### 2. 日常更新：按修改内容选择

| 修改内容或操作 | 命令 | 执行范围 |
|---|---|---|
| 修改 `.env` 中的 Token、密钥、远程模型部署名等 | `sudo bash manage.sh reload api` | 不构建镜像，只重建业务容器加载配置 |
| 修改 Python / HTML / JS 业务代码 | `sudo bash manage.sh update api` | 只构建并更新业务镜像，复用依赖缓存；不重启评分模型 |
| 业务容器需要重启，配置和代码均未变化 | `sudo bash manage.sh restart api` | 仅重启现有容器，不重新读取 `.env` |
| 首次部署、切换 Ollama/百炼、修改模型容器资源或恢复整套服务 | `sudo bash manage.sh up` | 完整部署所需服务，会执行镜像构建并加载评分模型 |
| 更新评分服务代码或编译依赖 | `sudo bash manage.sh update scorer` | 只构建并更新评分容器 |

**日常配置或业务代码更新不必每次执行完整 `up`。** `reload api` 需要已有业务镜像和所需模型服务正常运行，也不能部署源码变化。模型服务尚未启动或刚开启评分开关时，先执行 `up`。

管理入口不会自动拉取代码：Git 用户先 `git pull --ff-only`，上传更新包的用户先解压，再选择上表中的更新命令。

### 3. 状态、日志与停止

```bash
sudo bash manage.sh status          # 查看服务状态
sudo bash manage.sh logs api        # 业务日志
sudo bash manage.sh logs scorer -f  # 持续查看评分日志
sudo bash manage.sh stop            # 停止服务，保留模型与数据
```

容器数量：Ollama + 评分模式运行三个容器；百炼 + 评分模式运行两个容器。关闭评分服务后各减少一个。完整说明见 [统一 Docker 管理入口](docs/DEPLOYMENT.md)。

---

面向生产场景的 AI 内容检测与降 AI 改写服务。

[![Demo](https://img.shields.io/badge/Demo-aigcchecker.com-blue)](https://www.aigcchecker.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-API-009688)](https://fastapi.tiangolo.com/)
[![Ollama](https://img.shields.io/badge/Ollama-Local%20LLM-black)](https://ollama.com/)

在线演示站点：<https://www.aigcchecker.com/>

## 项目简介

`AIGC Checker Server` 是一个基于 `FastAPI` 构建的 AI 内容检测服务，核心目标不是依赖超长 Prompt 让模型“猜”结果，而是采用更稳定的工程化方案：

- 真实特征计算
- 文档分块检测
- 本地 Ollama 模型判别
- 规则修正
- 文档级聚合
- 可选远程复核
- 改写前后复检

项目适合：

- AI 内容检测产品后端
- SaaS 文本审核服务
- 对接 Laravel / PHP / Node / Java 等上层业务系统
- 本地化或私有化部署

## 核心特性

- 可选提供方：`.env` 的 `DETECTION_PROVIDER=ollama|bailian` 统一选择本地 Ollama 或阿里云百炼，支持判断 JSON 与表达扰动
- 真实特征：字符数、句长波动、词汇多样性、重复 n-gram、连接词密度等由 Python 实际计算
- 分块检测：长文本不是整篇一次判，而是 chunk 级别分析后再聚合
- 语言增强：区分 `zh` / `en` / `mixed`，英文采用独立的连接词、细节信号和词级 n-gram 重复率
- 会员能力分层：支持 `Free / Pro` token
- 可选二审：Pro 套餐可启用远程复核
- 兼容前端：返回结构兼容旧前端常见字段，同时保留专业字段
- 降 AI 改写：支持 `/api/reduce`，改写后自动重新检测
- 可观测 Demo：快速体验与接口测试双模式，支持查看脱敏任务轨迹
- 持久化任务日志：SQLite 记录模型路由、切词分块、评分与耗时，不保存用户原文

## 当前执行策略

### `/api/scan`

当前 `scan` 实际执行策略固定为：

- 检测提供方由 `DETECTION_PROVIDER` 选择 Ollama 或百炼
- Ollama 模型由 `OLLAMA_MODEL` 配置，百炼模型由 `BAILIAN_MODEL` 配置
- 请求中的 `api_source` 保留为兼容参数
- Pro 用户在低置信度或边界结果时可触发 Azure 复核；OpenRouter 不参与复核

### `/api/reduce`

当前 `reduce` 执行链路为：

1. 先对原文执行完整检测
2. 执行语义改写
3. 执行本地表达扰动
4. 对改写结果再次检测
5. 返回改写前后 AI 风格风险分和质量分

## 系统架构

```text
Client / Web / Laravel
        │
        ▼
   FastAPI Router
        │
        ▼
 services/checker.py
        ├── preprocess.py   文本清洗 / 语言识别 / 文体识别 / 分块
        ├── features.py     真实特征工程
        ├── judges.py       Qwen 判别 / 启发式回退
        ├── model_client.py Ollama / 百炼统一选择
        ├── bailian_client.py 百炼 JSON 与文本生成
        ├── aggregate.py    chunk 评分融合 / 文档聚合
        ├── ollama_client.py
        ├── prompts.py
        └── calibration.py
```

## 目录结构

```text
api/
  detect.py

services/
  aggregate.py
  calibration.py
  checker.py
  features.py
  judges.py
  ollama_client.py
  preprocess.py
  prompts.py
  schemas.py
  task_log.py

main.py
web/demo.html
start.sh
disploy.sh
Dockerfile
compose.yaml
requirements.txt
```

## `/api/scan` 检测流程

### 1. 请求入口

路由：`POST /api/scan`

入口文件：[api/detect.py](./api/detect.py)

处理内容：

- Bearer Token 鉴权
- 套餐识别：`free / pro`
- 文本长度校验
- 进入 `services/checker.py::run_check`

### 2. 主检测链路

入口方法：[services/checker.py](./services/checker.py) 中的 `run_check`

执行步骤：

1. `clean_text`
2. `detect_language`
3. `detect_genre`
4. `extract_document_features`
5. `chunk_text`
6. 遍历 chunk：
   - `extract_chunk_features`
   - `judge_chunk_with_qwen`
   - `score_chunk`
7. `aggregate_document`
8. Pro 用户按条件触发远程复核
9. 输出最终结果

### 3. 本地模型调用策略

判别模型由 `.env` 配置：`DETECTION_PROVIDER=ollama` 使用 `OLLAMA_MODEL`；`bailian` 使用 `BAILIAN_MODEL`。表达扰动也跟随该提供方。完整配置见 [统一管理说明](docs/DEPLOYMENT.md)。

并不是所有 chunk 都一定调用本地模型。

以下场景可能直接走启发式而跳过本地 Qwen：

- 极短 chunk
- 单句 chunk
- 强结构化列表块
- 细节很多的正式文本块

因此实际本地模型调用次数通常小于 chunk 数。

## 特征工程

特征实现位于：[services/features.py](./services/features.py)

当前真实计算的指标包括：

- `char_count`
- `sentence_count`
- `avg_sentence_length`
- `sentence_length_std`
- `burstiness`
- `lexical_diversity`
- `repeated_ngram_ratio`
- `punctuation_density`
- `connector_density`
- `paragraph_length_variance`
- `detail_signal_count`
- `digit_density`
- `list_line_ratio`

### 语言感知特征

当前已支持语言感知：

- `detect_language()`：区分 `zh` / `en` / `mixed`
- 英文连接词单独统计
- 英文 `detail_signal_count` 使用独立规则
- 英文 `repeated_ngram_ratio` 使用词级 3-gram
- `mixed` 文本同时参考中英文信号

## 评分逻辑

chunk 级风险分融合模型判断和统计特征：

```text
final_chunk_score = 0.70 * qwen_score + 0.30 * feature_score
# 模型跳过/失败：只使用 feature_score；style_score 仅解释，不重复计权。
```

之后再叠加：

- 文体修正
- 短文本收缩
- 置信度缩放

文档级 `ai_probability` 为：

- 所有 chunk `ai_score` 按 chunk 长度加权平均
- 不再重复应用文体和短文档降分；低置信度标记证据不足
- 最终格式化为字符串，例如 `"63.40"`

## 会员体系

支持两类 token：

| 套餐 | `/api/scan` | `/api/reduce` | 文本上限 | 远程复核 |
|---|---|---|---:|---|
| Free | 支持 | 不支持 | `FREE_MAX_CHARS` | 不启用 |
| Pro | 支持 | 支持 | `PRO_MAX_CHARS` | 可启用 |

说明：

- 当前 `Free` 与 `Pro` 在 `/api/scan` 返回字段上保持一致
- 两者主要区别是权限、长度上限、是否允许远程复核

## API 说明

### 认证方式

所有接口都需要：

```http
Authorization: Bearer <your_token>
Content-Type: application/json
```

### `POST /api/scan`

请求示例：

```json
{
  "content": "Your text here",
  "model": "qwen3.5:9b",
  "api_source": "ollama",
  "request_source": "api"
}
```

请求字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `content` | string | 是 | 待检测文本 |
| `model` | string | 否 | 请求模型名 |
| `api_source` | string | 否 | 兼容字段：`ollama / azure / openrouter` |
| `request_source` | string | 否 | 任务来源：`demo / test / api`，默认 `api` |

返回字段核心包括：

| 字段 | 说明 |
|---|---|
| `label` | `AI Generated / Mixed / Human Written` |
| `ai_probability` | 兼容旧客户端的风险分字符串，非校准概率 |
| `confidence` | `low / medium / high` |
| `document_features` | 文档级特征 |
| `chunks` | chunk 级结果 |
| `analysis` | `genre / language / high_risk_chunks` |
| `summary` | 摘要信息 |
| `metrics` | burstiness 为实测句长波动；perplexity/NLL 可由可选原文评分服务计算，未启用或失败返回 null；binoculars 未实现 |
| `review` | 远程复核元信息 |
| `plan` | 套餐标记 |
| `task_id` | 本次请求的任务 ID，可用于查询脱敏执行轨迹 |

示例请求：

```bash
curl -X POST "http://127.0.0.1:8027/api/scan" \
  -H "Authorization: Bearer your_token" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "This is a sample text for AI detection.",
    "model": "qwen3.5:9b",
    "api_source": "ollama"
  }'
```

### `POST /api/reduce`

请求体与 `/api/scan` 相同，但仅 Pro 用户可用。

典型返回：

- `reduced`
- `ai_probability`
- `ai_reduced_probability`
- `quality_score`
- `model`
- `changes`

### 任务记录接口

- `GET /api/tasks`：返回当前 Token 可见的任务摘要，最多读取当前保留上限
- `GET /api/tasks/{task_id}`：返回单个任务的完整执行步骤

`API_TOKEN` 作为管理 Token，可查看 Demo、测试页与第三方 API 的全部任务；`FREE_API_TOKENS` 和 `PRO_API_TOKENS` 仍按 Token 隔离，只能查看自己的记录。任务日志只保存步骤、模型、分块数量、统计分数、状态和耗时等运行元数据。`content`、`text`、`prompt`、改写前后文本等字段会被强制脱敏。

Demo 和接口测试页面底部支持逐次执行日志：执行中每两秒更新真实步骤，区分接口返回、模型调用成功和失败回退，并展示分块评分及完整 JSON。[使用与更新部署说明](docs/DEMO_EXECUTION_LOGS.md)。

## 环境配置

复制：

```bash
cp .env.example .env
```

关键配置如下：

```env
API_SOURCE=ollama

API_TOKEN=aigcchecker
FREE_API_TOKENS=free_token_1,free_token_2
PRO_API_TOKENS=pro_token_1,pro_token_2

TASK_LOG_DB_PATH=data/task_logs.db
TASK_LOG_MAX_RECORDS=200

FREE_MAX_CHARS=3000
PRO_MAX_CHARS=12000

OLLAMA_BASE_URL=http://218.4.33.190:26316
OLLAMA_MODEL=qwen3.5:9b
OLLAMA_TIMEOUT=120
OLLAMA_JSON_NUM_CTX=1536
OLLAMA_JSON_NUM_PREDICT=96

PRO_REVIEW_SOURCE=openrouter
PRO_REVIEW_MODEL=qwen/qwen-plus
OPENROUTER_API_KEY=replace_with_openrouter_api_key
```

### Token 生成

建议使用高熵随机 token：

```bash
openssl rand -hex 24
```

示例：

```env
FREE_API_TOKENS=token_a,token_b
PRO_API_TOKENS=token_c,token_d
```

## 快速启动

### 1. 创建虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

### 4. 启动服务

开发模式：

```bash
./start.sh
```

后台启动：

```bash
IS_PROD=y ./start.sh
```

默认地址：

- 快速体验：<http://127.0.0.1:8027/demo>
- 接口测试：<http://127.0.0.1:8027/test>
- 任务记录：<http://127.0.0.1:8027/tasks>
- Swagger 文档：<http://127.0.0.1:8027/docs>

`/docs` 的接口版本为 `2.1.0`，包含当前服务器提供方、套餐权限、检测/改写流程、任务轮询、响应字段及错误码。`risk_score` / `ai_probability` 表示未校准的风险分；PPL 为可选实测指标，当前不参与评分，Binoculars 尚未实现。

更新接口文档代码后执行 `sudo bash manage.sh update api`，再刷新 `/docs`；仅执行 `reload api` 不会加载源码变化。

未配置 `API_TOKEN` 时默认 Token 为 `aigcchecker`。生产环境请在 `.env` 中替换为高熵随机 Token。

## 线上 Docker 部署

已有业务 API、Ollama 并希望启用原文概率评分时，使用 [统一 Docker 管理入口](docs/DEPLOYMENT.md)：

```bash
sudo bash manage.sh up
sudo bash manage.sh check --score
```

入口统一管理业务、Ollama 和可选评分容器；复用已有模型及 Token，按 Docker 资源自动分配配额。日常只改 `.env` 用 `reload api`（不构建），改业务代码用 `update api`（仅构建业务镜像），状态/日志用 `status`、`logs api`。配置集中在 `.env` 和 `deploy.env`。已下载 `models/` 中的 GGUF 时无需重复下载。PPL 为实测指标，当前不参与风险评分。

以下为只更新业务 API 的兼容入口。

本地仍使用 `./start.sh`。线上在服务器的项目目录执行 `./disploy.sh`，需要 Docker Engine 20.10+；优先使用 Docker Compose 插件，未安装插件或只有旧版 `docker-compose` v1 时自动使用纯 Docker。

首次部署：

```bash
# 在服务器上克隆或上传项目后，进入项目目录。
cp .env.example .env
# 编辑 .env，填写生产 Token、Ollama 地址和所需的远程模型密钥。
./disploy.sh
```

后续更新再次执行 `./disploy.sh`。脚本自动执行 `git pull --ff-only`、构建镜像、重建容器和健康检查。构建失败不会停止原容器；重建期间会短暂中断服务。如果代码由上传方式更新，或需要直接部署当前目录代码：

```bash
SKIP_GIT_PULL=1 ./disploy.sh
```

- 容器名和镜像名：`aigc-checker-server`、`aigc-checker-server:latest`。
- 端口：宿主机 `8027` 映射容器 `8027`，访问 `http://服务器地址:8027/demo`，API 文档为 `/docs`。
- 生产容器直接运行 Uvicorn，不启用 `--reload`，退出后由 Docker 自动重启。
- `.env` 仅在运行时加载，不打进镜像。为兼容纯 Docker 模式，使用 `KEY=value`，不要加外层引号、`export`、行尾注释或变量引用。
- SQLite 固定保存到容器 `/app/data/task_logs.db`，挂载服务器项目下的 `data/`。重建容器保留任务记录；如原服务使用自定义数据库路径，请先将数据库备份迁移到 `data/task_logs.db`。
- Ollama 单独运行，本镜像只部署 API 服务。`OLLAMA_BASE_URL` 必须能从容器访问；如果 Ollama 在同一宿主机上，可使用 `http://host.docker.internal:11434`，并让 Ollama 监听容器可访问的地址。容器中的 `127.0.0.1` 指向容器自身。
- 健康检查使用 `/health`，只验证 API 进程响应，不验证 Ollama 或远程模型可用性。
- 如果服务器之前通过 `start.sh` 启动了服务，先停止原进程，释放 `8027` 端口再部署。

常用维护命令（两种部署模式通用）：

```bash
docker ps --filter name=aigc-checker-server
docker logs --tail 100 -f aigc-checker-server
docker restart aigc-checker-server
docker stop aigc-checker-server
curl --fail http://127.0.0.1:8027/health
```

## 与上层业务系统对接

该项目非常适合做独立检测后端，被以下系统调用：

- Laravel
- PHP
- Node.js
- Java / Spring
- Python 其他服务

推荐接入方式：

1. 上层业务系统只调用 FastAPI 服务
2. 不要直接调用 Ollama
3. 根据用户套餐选择 `Free / Pro` token
4. 普通版只调用 `/api/scan`
5. 会员版可调用 `/api/reduce`

## 适用场景

- AIGC 内容检测平台
- 文档原创度辅助判断
- SEO 内容审核
- 教育 / 论文初筛
- 企业内部文稿风险筛查
- AI 改写前后效果验证

## 项目目标

本项目优先关注：

- 稳定性
- 可解释性
- 可维护性
- 本地优先部署
- 兼容现有前端系统

它不是一个单纯依赖 Prompt 的“演示型检测器”，而是一个面向真实业务落地的服务端检测流水线。

## 后续方向

- 更强的英文 AI 模板特征
- 更稳定的真实 perplexity / pseudo-perplexity
- 更精细的句子级高亮
- 更多语言支持
- 基于标注数据的校准层增强

## 贡献

欢迎提交：

- Issue
- Bug report
- Feature request
- Pull Request

如果你在生产环境中使用了这个项目，欢迎附上你的使用场景和优化建议。


## Ubuntu CPU 部署：8 核 / 32GB

已经部署本项目时，优先使用 [统一 Docker 管理入口](docs/DEPLOYMENT.md)，支持当前 4 核 / 16GB 机器及独立评分服务。只需要 Ollama 时，也可使用 [独立 Ollama Docker 部署说明](docs/OLLAMA_DOCKER.md)。`disploy.sh` 支持配置 `OLLAMA_DOCKER_NETWORK`，重建 API 容器后自动恢复模型网络连接。

从零搭建整套 CPU 环境时，可选 [CPU 四步部署说明](docs/CPU_DEPLOYMENT.md)，同时部署 Qwen4B 量化判别和真实原文 PPL 服务。PPL 先作为实测指标返回，未训练校准前不改变现有风险分。

## 检测引擎 2.0 行为与迁移

- 修复英文句号断句，保护常见称谓、首字母、小数、版本号与域名；句长统计保留短句。
- 分块目标 320 字符、最小目标 180、硬上限 480；无标点长文同样拆分。`start/end` 是清洗后文本的 Python 字符偏移，非字节偏移。字符上限不等同模型 token 上限。
- 普通英文单词/大写缩写不再算具体细节，重叠细节只计一次；中英文连接词分别匹配，中文重复短语按 2–4 字片段提取，不宣称语义分词。
- Qwen 输入不再包含数值特征，风格子分只作解释。暂用 70% Qwen + 30% 统计；模型跳过或失败时只用统计。数字细节不再额外减分或触发模型跳过，文体修正仅在分块层执行一次。这些权重和文体规则尚未用标注集拟合。
- 低置信度返回 `label: "Insufficient Evidence"`、`assessment_status: "insufficient_evidence"`；可继续读取 `score_label` 查看原有风险区间标签。正常结果状态为 `assessed`。消费者须支持新增标签。
- `risk_score` 为 0–100 数字；`ai_probability` 保留原字符串格式。`score_semantics: "uncalibrated_risk_score"` 和 `calibrated: false` 明确表示尚非来源概率。
- 默认未开启原文评分时 `metrics.perplexity` 为 `null`；启用 `PROBABILITY_ENABLED` 并成功调用独立评分服务后，返回文档级真实 PPL 和 NLL，算法/模型信息见 `probability`。评分失败显式告警，不伪造数值。`metrics.binoculars` 及分块代理字段仍为 `null`。消费者不可将 null 当 0。
- 推荐使用 `segments`，`sentences` 作为兼容别名保留；二者 `granularity` 均为 `chunk`。`summary.percentages` 只作历史展示权重，不代表实际内容组成比例。`vocab` 只说明重复，不能作为来源证据。
- `warnings` 记录规则降级、短文本和检测分歧。全文置信度按块长度加权；规则结果不因互相一致而提升置信度。
- 远端复核不读取本地数值分数，选择高风险、分歧和代表片段并提供全文；严重分歧时标记证据不足。远端只更新文档结论，分块分数仍是本地结果（`review.scope: "document_only"`）。
- FastAPI lifespan 复用 Ollama HTTP 连接，并用 `OLLAMA_MAX_CONCURRENCY` 限制进程内模型请求；`SCAN_CHUNK_CONCURRENCY` 限制每篇文档并发任务。默认均为 1，部署多 worker 时每个进程各有额度。只有完成 GPU 压测后再增大。
- JSON 输出最少 256 tokens，兼容旧 `.env` 的 96 配置；检测到 `done_reason=length` 后增大预算重试一次。此改动需要重启服务生效。

### 回归验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试使用合成文本和模拟模型响应，不调用远程模型；回归通过不代表已验证真实检测准确率。

### 离线评测

保存有人工标签的样本及实际 `/api/scan` 返回结果为 JSONL，每行例如：

```json
{"id":"sample-001","group_id":"source-001","split":"test","label":"human","language":"zh","genre":"general","char_count":500,"prediction":{"risk_score":25.4,"assessment_status":"assessed"}}
```

以上仅为格式示例，不能作为准确率证据。标注应覆盖 human / ai / mixed、人工润色 AI、AI 润色人工、不同生成模型、语言、文体与长度。同一原文及改写版本必须共享 `group_id`，且仅归属一个 train / calibration / test 分组。

```bash
.venv/bin/python scripts/evaluate_detection.py data/labeled_predictions.jsonl --split test --threshold 70 --output data/evaluation.json
```

脚本完全离线，检查重复 ID、跨分组来源泄漏和非法分数；输出误报率、召回率、覆盖率、各类拒判率、AUROC、风险分桶实际 AI 比例，以及语言/文体/长度切片。混合标签不参与二分类指标；证据不足保留在误报率和召回率的分母，防止通过大量拒判虚增指标。

下一步需要真实标注数据：只在训练/校准集调整权重、阈值与概率校准，在冻结的测试集验证。可选 CPU 原文评分服务现已计算真实 token 概率、PPL 和 NLL，但尚未将其映射为来源概率。Binoculars / Fast-DetectGPT 仍未实现。
