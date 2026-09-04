# AIGC Checker Server 业务逻辑梳理

> 基于当前代码（`main.py` / `api/detect.py` / `services/*`）的完整业务逻辑梳理。
> 生成时间：2025-08-19。若代码有更新，请同步维护本文档。

---

## 1. 系统定位

面向生产场景的 **AI 内容检测 + 降 AI 改写** 后端服务，供上层业务系统（Laravel / PHP / Node / Java 等）通过 HTTP API 调用。

核心设计原则：

- **本地优先**：`/api/scan` 检测主链路固定走本地 Ollama（`qwen3.5:9b`）
- **工程化而非纯 Prompt**：真实统计特征计算 + 分块检测 + 规则修正 + 文档级聚合
- **可降级**：任何 LLM 调用失败都有启发式/回退兜底，不影响主流程
- **会员分层**：Free / Pro 两档 token，控制功能权限和文本上限

---

## 2. 总体架构

```text
                        ┌──────────────────────────┐
  Client / Web / Laravel │       FastAPI (main.py)  │
      │  Bearer Token    │  /api/scan  /api/reduce  │
      ▼                 │  / (内部测试页)           │
 ┌───────────────┐      └───────────┬──────────────┘
 │ api/detect.py │ 鉴权/套餐/长度校验 │
 └───────────────┘                  ▼
 ┌──────────────────────────────────────────────┐
 │ services/checker.py  run_check / run_reduce  │
 │  ├─ preprocess.py   清洗/语言/文体/分块       │
 │  ├─ features.py     真实统计特征工程          │
 │  ├─ judges.py       本地 Qwen 判别 + 启发式   │
 │  ├─ aggregate.py    chunk 评分融合/文档聚合   │
 │  ├─ calibration.py  校准层（当前 identity）   │
 │  ├─ ollama_client.py Ollama 调用             │
 │  ├─ prompts.py      Prompt 模板              │
 │  └─ schemas.py      Pydantic 校验模型         │
 └──────────────┬─────────────────────────────┘
                │
   ┌────────────┼──────────────┬───────────────┐
   ▼            ▼              ▼               ▼
 Ollama(本地)  Azure(GPT-4o)  OpenRouter      内部代理
 /api/generate JSON模式       qwen/qwen-plus   (可选转发)
```

模块职责：

| 模块 | 职责 |
|---|---|
| `main.py` | 应用入口，挂载路由，提供内部测试 HTML 页 |
| `api/detect.py` | HTTP 层：鉴权、套餐识别、长度校验、错误映射 |
| `services/checker.py` | 业务编排：检测主流程、降 AI 改写流水线、远端二审 |
| `services/preprocess.py` | 文本清洗、句子切分、语言识别、文体识别、分块 |
| `services/features.py` | 真实统计特征提取（句长、词汇多样性、n-gram 等） |
| `services/judges.py` | 本地 Qwen 风格判别，含跳过策略和启发式兜底 |
| `services/aggregate.py` | chunk 三路评分融合、文档级聚合、置信度计算 |
| `services/calibration.py` | 校准层，当前为 identity（预留） |
| `services/ollama_client.py` | Ollama `/api/generate` 调用（JSON 模式 / 文本模式） |
| `services/prompts.py` | 判别/改写/复核各环节 Prompt |
| `services/schemas.py` | Pydantic 校验模型 |

---

## 3. 认证与会员体系

### 3.1 Token 校验（`api/detect.py::_token_context`）

匹配优先级（命中即返回，不匹配则 401）：

| 优先级 | Token 类型 | 套餐 | can_reduce | can_remote_review | 文本上限 |
|---|---|---|---|---|---|
| 1 | `PRO_API_TOKENS` 集合 | pro | ✅ | ✅ | `PRO_MAX_CHARS`(12000) |
| 2 | `FREE_API_TOKENS` 集合 | free | ❌ | ❌ | `FREE_MAX_CHARS`(3000) |
| 3 | 旧版单一 `API_TOKEN` | pro（兼容） | ✅ | ✅ | `PRO_MAX_CHARS` |
| — | 无任何配置 | — | 返回 500「服务端未配置 API Token」 | | |

### 3.2 接口权限差异

| 接口 | Free | Pro | 说明 |
|---|---|---|---|
| `POST /api/scan` | ✅ | ✅ | 返回字段一致，不按套餐裁剪 |
| `POST /api/reduce` | ❌ 403 | ✅ | Free 直接拒绝 |

### 3.3 文本长度校验（两个接口一致）

- `< 50` 字符 → 400「文本太短」
- `> 套餐 max_chars` → 400「当前套餐最多支持 N 个字符」

---

## 4. `/api/scan` 检测主流程（`checker.py::run_check`）

### 4.1 流程步骤

```text
1. 解析请求参数
   - _normalize_api_source：规范化来源，未知值回退 API_SOURCE
   - _resolve_scan_model：仅当请求 api_source=ollama 且带了 model 时才允许覆盖
     本地模型，否则固定用 OLLAMA_DEFAULT_MODEL（qwen3.5:9b）
2. clean_text 清洗
3. detect_language：zh / en / mixed
4. detect_genre：文体识别（影响后续评分保守程度）
5. extract_document_features：文档级特征
6. chunk_text：按句子分块（目标 320 字/块，最小 180 字）
7. 逐 chunk：
   a. chunk 级语言识别（仅文档为 mixed 时逐块判断）
   b. extract_chunk_features
   c. judge_chunk_with_qwen（可能跳过 LLM 走启发式）
   d. score_chunk：三路评分融合 + 规则修正
8. aggregate_document：文档级聚合
9. _should_trigger_remote_review：判断是否触发 Pro 远端二审，必要时融合结果
10. _trim_result_by_plan：补充 plan / review 元数据后返回
```

### 4.2 语言识别（`preprocess.py::detect_language`）

- 中文 ≥ 8 字且英文 ≥ 8 词（或拉丁字符 ≥ 40）→ 计算中英占比，占比接近（≥0.18）为 `mixed`，否则取占优方
- 单语种按字符量判定 `zh` / `en`
- 影响：连接词统计、细节信号规则、n-gram 重复率算法、分块跳过阈值

### 4.3 文体识别（`preprocess.py::detect_genre`）

按优先级从上到下命中即返回：

| 文体 | 判定条件 | 业务影响 |
|---|---|---|
| `list_or_table` | 列表行/表格行 ≥ 40% | 评分 -15，触发保守策略 |
| `academic` | 学术关键词 ≥ 3 或含 `[数字]` | 评分 -8，保守 |
| `business_doc` | PRD/KPI/OKR 等 ≥ 3 | 评分 -8，保守 |
| `seo_blog` | SEO 关键词 ≥ 2 | 无特殊修正 |
| `marketing` | 营销词 ≥ 3 | 评分 +3（激进） |
| `translation_like` | 翻译标记词 ≥ 1 或中英混排 | 评分 -6 |
| `general` | 兜底 | 无 |

### 4.4 分块策略（`preprocess.py::chunk_text`）

- 按句末标点/双换行切句，短段（中文<20字 且 英文<15词 且 紧凑<35）自动并入相邻段
- 累积句子直到接近 target_size(320) 输出一块；末块 < min_size(180) 时并入上一块
- 重新编号保证 chunk_id 连续

### 4.5 分块判别（`judges.py::judge_chunk_with_qwen`）

**跳过 LLM 的条件**（走 `_heuristic_fallback`，减少本地模型调用）：

| 条件 | 说明 |
|---|---|
| 单句且 ≤ 阈值字符 | 短句块，en 阈值 110，其他 170 |
| `list_or_table` 文体或列表行占比 ≥ 阈值 | 结构化块，en 阈值 0.75，其他 0.55 |
| 非英文且字符 ≤ 170 | 短块 |
| business/academic 且细节 ≥ 阈值 且 n-gram 重复率 < 0.035 | 细节密集正式文本，en 阈值 8，其他 5 |

**LLM 调用失败**（超时/格式异常）→ `_heuristic_fallback`：
- 纯统计信号加权计算 AI 分，置信度固定 **0.38**（提示结果仅供参考）

**Qwen 输出归一化**（`_normalize_qwen_result`）：label/score/confidence/signals/perplexity_proxy/binoculars_score 缺字段时按 label 默认值补全。

### 4.6 chunk 评分（`aggregate.py::score_chunk`）

```text
final_chunk_score = 0.55 * qwen_score   (LLM 判别分)
                  + 0.30 * feature_score (统计特征分)
                  + 0.15 * style_score   (Qwen signals 风格分)
```

随后规则修正：

| 修正 | 规则 |
|---|---|
| 文体 | business_doc/academic -8；list_or_table -15；translation_like -6；marketing +3 |
| 细节信号 | 细节 ≥ 3 时每个 -2.5，最多 -14 |
| 列表占比 | > 0.35 时 -8 |
| 短文本 | < 120 字符向 50 收缩 ×0.7，置信度 ×0.72 |
| 单句 | 置信度 ×0.85 |

置信度：

```text
confidence = (0.45 * qwen_confidence + 0.25 * min(chars/260, 1) + 0.30 * agreement) * scale
agreement = 1 - |qwen_score - feature_score| / 100   # LLM 与统计分一致性
```

### 4.7 文档聚合（`aggregate.py::aggregate_document`）

- `ai_probability`：各 chunk `ai_score` **按字符数加权平均**
- 短文档（<120 字符）向 50 收缩 ×0.65，置信度 ×0.7
- 正式文体（business_doc/academic/list_or_table）且分数 < 70 时额外 -4
- `confidence`：chunk 平均置信度 → `≥0.72 high / ≥0.45 medium / 其余 low`
- `label`：`≥70 AI Generated / ≥40 Mixed / 其余 Human Written`
- `high_risk_chunks`：`ai_score ≥ 65` 的 chunk id 列表（用于改写优先级）
- `metrics`：perplexity（chunk 加权 ppl proxy）、burstiness、binoculars
- `sentences`：每 chunk 的热力级别（1-10）
- `vocab`：高频可疑词 Top10

### 4.8 统计特征（`features.py`）

真实计算的特征：`char_count`、`sentence_count`、`avg_sentence_length`、`sentence_length_std`、`burstiness`（变异系数）、`lexical_diversity`、`repeated_ngram_ratio`（中文 4-gram / 英文词 3-gram / mixed 取高）、`punctuation_density`、`connector_density`、`paragraph_length_variance`、`detail_signal_count`、`digit_density`、`list_line_ratio`、`suspicious_terms`。

> `pseudo_perplexity` 目前为 TODO 占位（返回 None）。

### 4.9 远端二审（Pro 专用，`checker.py`）

**触发条件**（满足任一即触发，全局开关 `REMOTE_REVIEW_ENABLED` 可关闭）：

| 原因 | 条件 |
|---|---|
| `low_confidence` | 本地置信度为 low |
| `borderline_score` | AI 概率在 [40, 65] 边界区间 |
| `conservative_genre` | 正式文体（academic/business_doc/list_or_table）且高风险块 ≤ 1 |

**二审来源**：`PRO_REVIEW_SOURCE`（openrouter 或 azure），模型 `PRO_REVIEW_MODEL`。

**分数融合**（`_merge_review_result`）：

```text
blended = 0.60 * local + 0.40 * review   (本地置信度 low 时)
          0.72 * local + 0.28 * review   (其他情况)
review_conf ≥ 0.75          → 最终置信度提升为 high
review_conf ≥ 0.45 且原为 low → 提升为 medium
```

**容错**：二审调用失败返回 None，不影响主流程结果（记录 `review.skip provider_error`）。

---

## 5. `/api/reduce` 降 AI 改写流程（`checker.py::run_reduce`）

### 5.1 流程步骤

```text
1. run_check(原文)          → 原文 AI 概率 + 高风险分块 + 统计特征目标
2. _rewrite_content(原文)   → 组合式改写流水线
3. run_check(改写后文本)    → 降 AI 后概率
4. _quality_score           → 综合质量分
5. 回滚保护：改写后概率未下降 → 回退原文
6. 返回 reduced / 前后概率 / quality_score / model 等级 / changes
```

### 5.2 改写流水线（`_rewrite_content`）

```text
Step1 semantic_rewrite  深度语义改写（Azure GPT-4o → 失败回退 OpenRouter qwen/qwen-plus）
Step2 expression_perturb 本地 Ollama Qwen 表达扰动（temperature 0.45）
Step3 rule_injection    确定性规则注入（纯代码，无 LLM）
```

**Step1 语义改写**：
- Prompt 注入：原文 AI 概率、文体、统计特征改写目标（burstiness 低插短/长句、n-gram 重复替换、词汇多样性、连接词密度等）、高风险分块（score≥40 取前 8）
- 失败回退链：Azure → OpenRouter → 兜底返回「未改写」

**Step2 表达扰动**：本地 Ollama 继续打散句长、段首、连接词节奏，保留数字/专有名词

**Step3 规则注入**（确定性）：
- `_collapse_list_style`：过齐的短句列表合并为自然段落节奏
- `_inject_detail_anchors`：从原文找回缺失的「含数字/细节」锚点句补回改写稿

**model 等级**：应用步骤 ≥ 3 → `deep`；否则 `moderate`；改写失败 → `light`

**改写失败兜底**：Step1 未应用时直接返回原文，`rewrite=false`，`quality_score=55.0`，`model=light`

### 5.3 质量分（`_quality_score`）

```text
preservation = 0.7 * 词汇重叠(Jaccard) + 0.3 * 长度比(双向取小)     # 0-100
improvement  = clamp(before - after, 0, 60) / 60 * 100             # 降 AI 幅度
quality_score = 0.6 * preservation + 0.4 * improvement
```

### 5.4 回滚保护

改写成功但 `after >= before`（概率未下降）→ 回退原文：

```text
reduced_text = 原文
rewrite = false
quality = 55.0
changes = []
model = light
```

### 5.5 返回结构

| 字段 | 说明 |
|---|---|
| `reduced` | 改写后文本（失败时=原文） |
| `rewrite` | 是否成功改写 |
| `ai_probability` | 原文 AI 概率（字符串，如 "63.40"） |
| `ai_reduced_probability` | 改写后 AI 概率 |
| `quality_score` | 综合质量分 0-100 |
| `model` | light / moderate / deep |
| `changes` | 修改记录（original/revised/reason，最多 8 条） |

---

## 6. 外部 LLM 提供方与容错

| 提供方 | 用途 | 调用方式 | 失败处理 |
|---|---|---|---|
| Ollama（本地） | scan 判别、reduce 扰动 | `/api/generate`，JSON 模式用 `format=schema`，失败自动重试 `format=json` | 判别失败→启发式；扰动失败→跳过该步 |
| Azure | reduce Step1 语义重写、可选二审 | OpenAI SDK `response_format=json_object` | 回退 OpenRouter / 跳过 |
| OpenRouter | Step1 兜底、二审默认来源 | OpenAI 兼容 SDK 或内部代理（`OPENROUTER_SEND_MODE=proxy`） | 跳过该步 / 二审不触发 |

关键点：
- Ollama 请求禁用 thinking（`OLLAMA_DISABLE_THINKING=true`），`keep_alive=10m`
- 所有远端异常均被捕获，**主流程永不因 LLM 失败而中断**
- HTTP 层将引擎 `ValueError`（解析失败）映射为 502，其余异常映射为 500

---

## 7. 环境配置与业务含义

| 配置 | 默认值 | 业务含义 |
|---|---|---|
| `API_SOURCE` | ollama | 请求默认来源（兼容参数，不改变执行策略） |
| `API_TOKEN` | — | 旧版单 token，按 Pro 权限处理 |
| `FREE_API_TOKENS` / `PRO_API_TOKENS` | — | 套餐 token 集合（逗号分隔） |
| `FREE_MAX_CHARS` / `PRO_MAX_CHARS` | 3000 / 12000 | 套餐文本上限 |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | 218.4.33.190:26316 / qwen3.5:9b | 本地检测模型 |
| `OLLAMA_JSON_NUM_CTX` / `NUM_PREDICT` | 1536 / 96 | 判别 JSON 输出上下文/长度 |
| `OLLAMA_REWRITE_MODEL` / `NUM_CTX` / `NUM_PREDICT` | qwen3.5:9b / 4096 / 1024 | 扰动步骤参数 |
| `AZURE_API_KEY` / `AZURE_ENDPOINT` / `AZURE_DEPLOYMENT` | — / — / gpt-4o | Step1 语义重写主提供方 |
| `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` | — / qwen/qwen-plus | Step1 兜底 + 二审 |
| `OPENROUTER_SEND_MODE` | self | self=直连 / proxy=内部代理转发 |
| `PROXY_BASE_URL` / `PROXY_TOKEN` | — | 内部代理转发配置 |
| `PRO_REVIEW_SOURCE` / `PRO_REVIEW_MODEL` | openrouter / qwen/qwen-plus | 二审来源与模型 |
| `REMOTE_REVIEW_ENABLED` | true | 二审全局开关 |
| `LOCAL_JUDGE_SKIP_*` | 见 judges.py | 本地判别跳过阈值（分中英文） |

---

## 8. 业务边界与容错汇总

1. **过短文本**：<50 字符直接 400，不进入引擎
2. **LLM 判别失败** → 启发式兜底（低置信度 0.38）
3. **二审失败** → 跳过，保留本地结果
4. **语义改写失败** → 原文兜底，`rewrite=false`
5. **改写后概率未下降** → 回滚原文
6. **改写文本为空** → `clean_text` 后回退原文
7. **模型返回非 JSON** → 正则提取 `{...}` 兜底解析
8. **chunk 切分失败** → 整篇作为一个 chunk

## 9. 已知占位/待完善点

- `calibration.py` 为 identity mapping，校准层未实际生效
- `features.compute_pseudo_perplexity` 为 TODO，`metrics.perplexity` 实际来自 Qwen 的 `perplexity_proxy`
- 远程复核仅融合 `ai_score`，`label` 以融合分重映射
- `_split_text_units` / `_detail_score` 等改写辅助逻辑规则较硬编码，后续可参数化
