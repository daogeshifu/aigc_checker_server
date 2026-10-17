"""OpenAPI response descriptions, used via responses=, not runtime response filtering."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from services.schemas import ReduceChange, RemoteReviewResult, TokenProbabilityResult


class DocumentedResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class ProbabilityDisabled(DocumentedResponse):
    status: Literal["disabled"]
    used_in_risk_score: Literal[False]


class ProbabilityUnavailable(DocumentedResponse):
    status: Literal["unavailable"]
    used_in_risk_score: Literal[False]
    error_type: str = Field(description="错误类型，不包含原文或密钥；不使用伪造 PPL 兜底。")


class ProbabilityMeasured(TokenProbabilityResult):
    status: Literal["measured"]
    used_in_risk_score: Literal[False] = Field(description="当前测量结果不参与风险分。")


class DetectionMetrics(DocumentedResponse):
    perplexity: float | None = Field(description="独立评分服务实测的原文困惑度；关闭或失败时为 null，不是模型生成的估计值。")
    nll: float | None = Field(description="原文 token 平均负对数概率；perplexity=exp(nll)。")
    burstiness: float = Field(description="全文句长标准差/平均句长，属于文本统计指标。")
    binoculars: None = Field(description="Binoculars 尚未实现，始终为 null。")


class ChunkBreakdown(DocumentedResponse):
    feature_score: float = Field(description="统计特征分，0～100。")
    qwen_score: float = Field(description="Qwen 原始分；heuristic 模式下为兼容字段，不代表实际调用模型。是否调用以 judge_mode 为准。")
    style_score: float | None = Field(description="模型风格信号的解释性汇总；回退时 null。")
    style_used_in_score: Literal[False]


class DetectionChunk(DocumentedResponse):
    chunk_id: int
    start: int = Field(description="清洗后文本中的起始字符偏移。")
    end: int = Field(description="清洗后文本中的结束字符偏移。")
    text: str = Field(description="分块原文，存在检测响应中；服务端任务日志不保存此内容。")
    ai_score: float = Field(ge=0, le=100, description="融合及修正后的分块风险分。")
    label: str
    confidence: float = Field(ge=0, le=1)
    warnings: list[str]
    score_breakdown: ChunkBreakdown
    features: dict[str, Any] = Field(description="字符/句子/段落数、句长波动、词汇多样性、重复率、连接词/标点密度等实算特征；统计 token_count 不等同于模型分词器 token 数。")
    signals: dict[str, float] = Field(description="Qwen 生成的风格信号：over_smooth/template_pattern/sentence_uniformity/human_detail；统计回退时为空。")
    judge_mode: Literal["qwen", "heuristic_skip", "heuristic_fallback"]
    judge_provider: Literal["ollama", "bailian"]
    judge_label: str | None
    judge_skip_reason: str | None
    perplexity_proxy: None
    binoculars_score: None


class AzureReview(DocumentedResponse):
    enabled: bool
    used: bool = Field(description="仅在 Azure 返回有效复核结果并成功融合后为 true。")
    provider: Literal["azure"] | None
    model: str | None = Field(description="Azure 实际部署名称；Free 套餐为 null。")
    reason: str | None = Field(default=None, description="low_confidence/borderline_score/conservative_genre/not_needed/plan_disabled/review_disabled。")
    result: RemoteReviewResult | None = Field(default=None, description="成功复核时返回；置信度是模型自报值。")
    local_risk_score: float | None = Field(default=None, description="复核融合前的文档风险分。")
    score_disagreement: float | None = None
    scope: Literal["document_only"] | None = Field(default=None, description="复核仅调整文档结果，不重写各分块分数。")


class DetectionEngine(DocumentedResponse):
    checker: str
    version: str
    score_semantics: Literal["uncalibrated_risk_score"]
    requested_api_source: str
    requested_model: str | None
    scan_api_source: Literal["ollama", "bailian"] = Field(description="实际检测提供方，由服务器 DETECTION_PROVIDER 决定。")
    qwen_model: str = Field(description="实际使用的 Qwen 模型名；兼容字段名，百炼模式也使用此字段。")


class ScanResponse(DocumentedResponse):
    task_id: str = Field(description="用于 GET /api/tasks/{task_id} 查询执行轨迹。")
    label: str = Field(description="AI Generated / Mixed / Human Written；低置信度返回 Insufficient Evidence。")
    score_label: str = Field(description="按分数区间映射的标签，即使证据不足仍保留。")
    assessment_status: Literal["assessed", "insufficient_evidence"]
    risk_score: float = Field(ge=0, le=100, description="尚未校准的 AI 风格风险分，不是 AI 来源概率或 AI 内容占比。")
    ai_probability: str = Field(description="兼容旧字段，以两位小数字符串返回，与 risk_score 对应；名称含 probability 但不是实际概率。")
    score_semantics: Literal["uncalibrated_risk_score"]
    calibrated: Literal[False]
    confidence: Literal["low", "medium", "high"] = Field(description="规则调整后的置信度等级，尚未做统计校准。")
    warnings: list[str]
    plan: Literal["free", "pro"]
    document_features: dict[str, Any] = Field(description="全文实算统计特征；pseudo_perplexity 是保留空接口，返回 null。")
    chunks: list[DetectionChunk]
    analysis: dict[str, Any] = Field(description="genre、language、high_risk_chunks（分块风险分>=65 的 ID）。")
    metrics: DetectionMetrics
    probability: ProbabilityDisabled | ProbabilityUnavailable | ProbabilityMeasured = Field(
        discriminator="status", description="独立原文概率服务的状态及实测数据；PPL 不参与当前风险评分。")
    review: AzureReview
    engine: DetectionEngine
    summary: dict[str, Any] = Field(description="confidence_label、percentages 等；ai/mixed/human 百分比仅为启发式展示权重，不是原文内容比例。")
    segments: list[dict[str, Any]] = Field(description="按分块粒度展示的文本及风险分。")
    sentences: list[dict[str, Any]] = Field(deprecated=True, description="segments 的历史别名，实际粒度仍是分块，不是逐句检测。")
    granularity: Literal["chunk"]
    vocab: dict[str, Any] = Field(description="重复词/短语展示，不代表作者身份的证据。")


class ReduceResponse(DocumentedResponse):
    task_id: str
    reduced: str = Field(description="最终保留的改写文本；改写失败或未改善时为清洗后的原文。")
    rewrite: bool = Field(description="是否最终保留了改写结果。")
    ai_probability: str = Field(description="原文 AI 风格风险分，两位小数字符串，非来源概率。")
    ai_reduced_probability: str = Field(description="最终保留文本的风险分，回滚时等于原文分数。")
    score_semantics: Literal["uncalibrated_risk_score"]
    calibrated: Literal[False]
    detection_status_before: Literal["assessed", "insufficient_evidence"]
    detection_status_after: Literal["assessed", "insufficient_evidence"]
    quality_score: float = Field(ge=0, le=100, description="基于词汇重叠、长度和分数下降的启发式质量分，不是事实准确率。")
    model: Literal["light", "moderate", "deep"] = Field(description="改写处理等级，不是实际大模型名称。")
    changes: list[ReduceChange]


class TaskSummary(DocumentedResponse):
    task_id: str
    task_type: Literal["scan", "reduce"]
    source: Literal["demo", "test", "api"]
    status: Literal["running", "success", "failed"] = Field(description="任务生命周期状态；详情中 result.stage/progress/message 随真实执行阶段变化，success/failed 为终态。")
    plan: Literal["free", "pro"]
    requested_api_source: str | None
    requested_model: str | None
    char_count: int
    result: dict[str, Any] | None = Field(description="脱敏结果摘要，不含原文、完整检测报告或改写文本。")
    error_code: str | None
    created_at: str
    updated_at: str
    returned_at: str | None = Field(description="服务端完成并准备返回结果/错误的时间；执行中为 null。")
    duration_ms: int | None


class TaskListResponse(DocumentedResponse):
    items: list[TaskSummary]
    total: int = Field(description="当前权限及筛选条件下的全部保留任务数。")
    can_view_all_tasks: bool = Field(description="是否使用管理员权限查看所有调用方的任务。")
    retention_limit: int = Field(description="服务端配置的任务保留数量上限。")


class TaskEvent(DocumentedResponse):
    sequence: int
    step: str
    status: str
    details: dict[str, Any] = Field(description="脱敏执行元数据，不保存 Token、原文或提示词。")
    created_at: str
    elapsed_ms: int = Field(description="从任务开始到该事件的累计时间，不是单步耗时。")


class TaskProgressResult(DocumentedResponse):
    stage: Literal["accepted", "preprocessing", "detecting", "probability", "aggregating", "reviewing",
                   "rewriting", "perturbing", "rule_adjusting", "evaluating", "finalizing", "completed", "failed"]
    progress: int = Field(ge=0, le=100, description="按真实执行阶段和已完成分块估算的整体进度，单调不减；运行中最多99，成功为100，失败保留失败前进度。不是耗时百分比。")
    message: str = Field(description="当前阶段的业务展示文案，可直接用于加载动画。")
    scan_phase: Literal["primary_scan", "original_scan", "reduced_scan"] | None = Field(description="当前检测轮次；改写/质量评估阶段为 null。")
    completed_chunks: int = Field(ge=0, description="当前检测轮次已完成的分块数，按 chunk_id 去重。")
    total_chunks: int = Field(ge=0, description="当前检测轮次分块总数，尚未分块或不在检测轮次时为0。")


class TaskDetailResponse(TaskSummary):
    result: TaskProgressResult = Field(description="各状态均返回进度和展示文案；成功时同时保留原有脱敏业务结果摘要。不包含完整检测报告或改写文本。")
    events: list[TaskEvent]
    execution_summary: dict[str, Any] = Field(description="local 为历史字段名，表示主 Qwen 判别器；provider 可为 bailian/ollama。third_party 为 Azure 复核/语义改写等辅助调用汇总；detection_modes 区分判别、统计及回退。")


class ErrorResponse(BaseModel):
    detail: str


def error_responses(*, task=False, forbidden=False, missing=False):
    responses = {
        401: {"model": ErrorResponse, "description": "缺少有效 Bearer Token 或 Token 无效；请使用业务访问 Token，而非模型 API Key。"},
    }
    if forbidden:
        responses[403] = {"model": ErrorResponse, "description": "当前套餐不支持改写，Free 套餐需要 Pro 权限。"}
    if task:
        header = {"X-Task-ID": {"description": "已经创建的任务 ID，可继续查询脱敏执行轨迹。", "schema": {"type": "string"}}}
        responses.update({
            400: {"model": ErrorResponse, "description": "文本不足 50 个清洗后字符，或超过套餐字数上限。"},
            409: {"model": ErrorResponse, "description": "request_id 已使用；原任务不会被覆盖。"},
            500: {"model": ErrorResponse, "description": "检测/改写主流程异常。", "headers": header},
            502: {"model": ErrorResponse, "description": "引擎结果无法解析。", "headers": header},
        })
    if missing:
        responses[404] = {"model": ErrorResponse, "description": "任务不存在、已过期，或不属于当前 Token。"}
    return responses
