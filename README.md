# AIGC Checker Server

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

- 本地优先：`/api/scan` 主链路固定走 Ollama 本地检测
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

- 本地检测固定走 `Ollama`
- 本地模型默认使用 `qwen3.5:9b`
- 请求中的 `api_source` 保留为兼容参数
- Pro 用户在低置信度或边界结果时可触发远程复核

### `/api/reduce`

当前 `reduce` 执行链路为：

1. 先对原文执行完整检测
2. 执行语义改写
3. 执行本地表达扰动
4. 对改写结果再次检测
5. 返回改写前后 AI 概率和质量分

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
        ├── judges.py       本地 Qwen 判别 / 启发式回退
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

本地判别模型：`Ollama + qwen3.5:9b`

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

chunk 级最终分数由三部分组成：

```text
final_chunk_score =
  0.55 * qwen_score +
  0.30 * feature_score +
  0.15 * style_score
```

之后再叠加：

- 文体修正
- 细节信号修正
- 列表结构修正
- 短文本收缩
- 置信度缩放

文档级 `ai_probability` 为：

- 所有 chunk `ai_score` 按 chunk 长度加权平均
- 再经过短文档和正式文体的保守修正
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
| `ai_probability` | 文档总 AI 率 |
| `confidence` | `low / medium / high` |
| `document_features` | 文档级特征 |
| `chunks` | chunk 级结果 |
| `analysis` | `genre / language / high_risk_chunks` |
| `summary` | 摘要信息 |
| `metrics` | `perplexity / burstiness / binoculars` |
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

- `GET /api/tasks`：返回当前 Token 最近的任务摘要
- `GET /api/tasks/{task_id}`：返回单个任务的完整执行步骤

任务记录按 Token 隔离，只保存步骤、模型、分块数量、统计分数、状态和耗时等运行元数据。`content`、`text`、`prompt`、改写前后文本等字段会被强制脱敏。

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
- Swagger 文档：<http://127.0.0.1:8027/docs>

未配置 `API_TOKEN` 时默认 Token 为 `aigcchecker`。生产环境请在 `.env` 中替换为高熵随机 Token。

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
