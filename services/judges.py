from __future__ import annotations

import os
import logging
import json

from services.aggregate import _feature_score
from services.ollama_client import generate_json
from services.prompts import QWEN_CHUNK_SYSTEM_PROMPT
from services.schemas import QwenJudgeResult
from services.task_log import record_task_event


logger = logging.getLogger(__name__)

LOCAL_JUDGE_SKIP_SHORT_CHARS = int(os.getenv("LOCAL_JUDGE_SKIP_SHORT_CHARS", "170"))
LOCAL_JUDGE_SKIP_LIST_RATIO = float(os.getenv("LOCAL_JUDGE_SKIP_LIST_RATIO", "0.55"))
LOCAL_JUDGE_SKIP_SHORT_CHARS_EN = int(os.getenv("LOCAL_JUDGE_SKIP_SHORT_CHARS_EN", "110"))
LOCAL_JUDGE_SKIP_LIST_RATIO_EN = float(os.getenv("LOCAL_JUDGE_SKIP_LIST_RATIO_EN", "0.75"))

_QWEN_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "ai_score": {"type": "number", "minimum": 0, "maximum": 100},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "signals": {
            "type": "object",
            "properties": {
                "over_smooth": {"type": "number", "minimum": 0, "maximum": 1},
                "template_pattern": {"type": "number", "minimum": 0, "maximum": 1},
                "sentence_uniformity": {"type": "number", "minimum": 0, "maximum": 1},
                "human_detail": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["over_smooth", "template_pattern", "sentence_uniformity", "human_detail"],
            "additionalProperties": False,
        },
        "label": {"type": "string", "enum": ["human", "mixed", "ai"]},
    },
    "required": ["ai_score", "confidence", "signals", "label"],
    "additionalProperties": False,
}

def _normalize_qwen_result(data: dict) -> dict:
    """Validate measured outputs; missing/invalid fields must trigger explicit fallback."""
    if not isinstance(data, dict):
        raise ValueError("Judge output must be an object")
    return QwenJudgeResult.model_validate(data).model_dump()


def _heuristic_fallback(features: dict, genre: str) -> dict:
    """当 LLM 调用失败时的纯统计启发式兜底方案，仅使用文本特征计算 AI 概率。

    复用 aggregate._feature_score，不做重复的文体或细节修正。
    confidence 固定为 0.38，后续聚合不得把兜底置信度抬高。
    """
    score = _feature_score(features)
    return {
        "ai_score": round(score, 2),
        "confidence": 0.38,
        "signals": {},
        "label": "ai" if score >= 70 else "mixed" if score >= 40 else "human",
        "perplexity_proxy": None,
        "binoculars_score": None,
        "judge_mode": "heuristic_fallback",
    }


def _should_skip_qwen(features: dict, genre: str, language: str) -> tuple[bool, str]:
    """对低信息量或结构化强的分块直接走启发式，减少本地模型调用。"""
    char_count = int(features.get("char_count", 0) or 0)
    sentence_count = int(features.get("sentence_count", 0) or 0)
    list_ratio = float(features.get("list_line_ratio", 0.0) or 0.0)

    short_chars_threshold = LOCAL_JUDGE_SKIP_SHORT_CHARS_EN if language == "en" else LOCAL_JUDGE_SKIP_SHORT_CHARS
    list_ratio_threshold = LOCAL_JUDGE_SKIP_LIST_RATIO_EN if language == "en" else LOCAL_JUDGE_SKIP_LIST_RATIO

    if sentence_count <= 1 and char_count <= short_chars_threshold:
        return True, "short_or_single_sentence"
    if genre == "list_or_table" or list_ratio >= list_ratio_threshold:
        return True, "structured_chunk"
    if language != "en" and char_count <= short_chars_threshold:
        return True, "short_chunk"
    return False, ""


async def judge_chunk_with_qwen(
    chunk_text: str,
    genre: str,
    features: dict,
    model: str | None = None,
    task_id: str | None = None,
    chunk_id: int | None = None,
) -> dict:
    """调用 Qwen（本地 Ollama）对单个分块进行风格判断，返回 QwenJudgeResult dict。

    仅发送文体和待分析文本；数值特征只用于路由及后续融合，减少锚定偏差。
    若 LLM 调用失败（超时/格式异常等），自动降级为 _heuristic_fallback，不影响主流程。
    """
    # Do not anchor the judge on the same numeric heuristics later used in fusion.
    user_prompt = json.dumps({"genre": genre, "text_to_analyze": chunk_text}, ensure_ascii=False)

    should_skip, skip_reason = _should_skip_qwen(features, genre, features.get("language", "mixed"))
    if should_skip:
        record_task_event(
            task_id,
            "judge.route.heuristic",
            status="skipped",
            details={
                "chunk_id": chunk_id,
                "route": "statistics_only",
                "reason": skip_reason,
                "model_called": False,
            },
        )
        heuristic = _heuristic_fallback(features, genre)
        heuristic["judge_mode"] = "heuristic_skip"
        heuristic["judge_skip_reason"] = skip_reason
        return heuristic

    try:
        record_task_event(
            task_id,
            "judge.route.qwen",
            status="running",
            details={
                "chunk_id": chunk_id,
                "route": "ollama_qwen",
                "provider": "ollama",
                "model": model or "default",
                "model_called": True,
            },
        )
        data = await generate_json(
            system_prompt=QWEN_CHUNK_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema=_QWEN_JUDGE_SCHEMA,
            model=model,
            trace_label=f"{task_id or '-'}:judge:chunk-{chunk_id if chunk_id is not None else 'na'}",
        )
        data = _normalize_qwen_result(data)
        result = data
        result["judge_mode"] = "qwen"
        record_task_event(
            task_id,
            "judge.qwen.finish",
            status="success",
            details={
                "chunk_id": chunk_id,
                "model": model or "default",
                "raw_ai_score": result.get("ai_score"),
                "raw_confidence": result.get("confidence"),
                "label": result.get("label"),
            },
        )
        return result
    except Exception as exc:
        # LLM 调用失败（超时、格式错误等），降级为启发式规则，记录警告日志
        logger.warning(
            "task=%s step=judge_chunk fallback=true chunk_id=%s model=%s error=%s",
            task_id or "-",
            chunk_id if chunk_id is not None else "na",
            model or "default",
            exc,
        )
        record_task_event(
            task_id,
            "judge.qwen.fallback",
            status="failed",
            details={
                "chunk_id": chunk_id,
                "model": model or "default",
                "error_type": type(exc).__name__,
                "fallback": "statistics_only",
            },
        )
        fallback = _heuristic_fallback(features, genre)
        fallback["judge_skip_reason"] = "qwen_provider_error"
        return fallback
