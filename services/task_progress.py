"""Business-facing polling progress derived from persisted execution events."""
from __future__ import annotations

from typing import Any


def task_progress(task: dict[str, Any]) -> dict[str, Any]:
    """Keep the task envelope intact and enrich its result with stage progress.

    Percentages represent pipeline milestones, not elapsed-time predictions.
    Replaying ordered events keeps progress stable across polls and restarts.
    """
    progress = {
        "stage": "accepted", "progress": 0, "message": "任务已受理，等待开始处理",
        "scan_phase": None, "completed_chunks": 0, "total_chunks": 0,
    }
    phase = "original_scan" if task["task_type"] == "reduce" else "primary_scan"
    chunks: dict[str, dict[str, Any]] = {}

    def advance(stage: str, percent: int, message: str, scan_phase: str | None = None) -> None:
        if percent < progress["progress"]:
            return
        counts = chunks.get(scan_phase, {})
        progress.update(
            stage=stage, progress=percent, message=message, scan_phase=scan_phase,
            completed_chunks=len(counts.get("completed", set())),
            total_chunks=counts.get("total", 0),
        )

    for event in task.get("events", []):
        step, details = event["step"], event.get("details", {})
        if step == "scan.start":
            phase = details.get("scan_phase", phase)
        event_phase = details.get("scan_phase", phase)
        counts = chunks.setdefault(event_phase, {"completed": set(), "total": 0})
        subject = {"original_scan": "原文", "reduced_scan": "改写后文本"}.get(event_phase, "文本")
        milestone = None
        if step == "scan.start":
            milestone = ("preprocessing", 5, f"正在预处理{subject}")
        elif step in {"scan.preprocess.clean", "scan.language.detect", "scan.genre.detect", "scan.features.document"}:
            milestone = ("preprocessing", 10, f"正在提取{subject}特征")
        elif step in {"scan.chunking.finish", "scan.chunk.score"}:
            if step == "scan.chunking.finish":
                counts["total"] = max(0, int(details.get("chunks", 0)))
            elif details.get("chunk_id") is not None:
                counts["completed"].add(details["chunk_id"])
            completed, total = len(counts["completed"]), counts["total"]
            percent = 15 + 55 * min(completed, total) // total if total else 15
            milestone = ("detecting", percent, f"正在分析{subject}（已完成 {completed}/{total} 个分块）")
        elif step == "scan.probability.start":
            milestone = ("probability", 72, f"正在测量{subject}概率指标")
        elif step in {"scan.probability.finish", "scan.probability.failure", "scan.aggregate.finish"}:
            milestone = ("aggregating", 85, f"正在汇总{subject}检测结果")
        elif step == "review.trigger" or (step == "review.decision" and details.get("trigger")):
            milestone = ("reviewing", 88, f"正在复核{subject}检测结果")
        elif step in {"review.merge", "review.skip"}:
            milestone = ("finalizing", 95, f"正在整理{subject}检测结果")
        elif step == "scan.finish":
            milestone = ("finalizing", 98, f"{subject}检测完成，正在整理结果")

        if milestone:
            stage, percent, message = milestone
            if task["task_type"] == "reduce":
                offset, span = (68, 26) if event_phase == "reduced_scan" else (0, 35)
                percent = offset + span * percent // 100
            advance(stage, percent, message, event_phase)
        elif step == "rewrite.pipeline.start":
            advance("rewriting", 35, "正在准备语义改写")
        elif step.startswith("rewrite.step."):
            rewrite_stage = details.get("stage")
            started = step == "rewrite.step.start"
            if rewrite_stage == "semantic_rewrite":
                advance("rewriting", 38 if step != "rewrite.step.finish" else 54,
                        "正在进行语义改写" if step != "rewrite.step.finish" else "语义改写完成，准备调整表达")
            elif rewrite_stage == "expression_perturb":
                advance("perturbing", 55 if started else 62,
                        "正在调整文本表达" if started else "表达调整已结束，继续后续处理")
            elif rewrite_stage == "rule_injection":
                advance("rule_adjusting", 64 if started else 66,
                        "正在优化文本细节" if started else "文本细节处理完成，准备复检")
        elif step == "rewrite.fallback":
            advance("rewriting", 66, "改写未生成有效变更，准备复检原文")
        elif step == "reduce.post_rewrite":
            advance("preprocessing", 68, "正在准备改写后文本复检", "reduced_scan")
        elif step in {"reduce.quality.calculate", "reduce.guard.rollback"}:
            advance("evaluating", 96, "正在评估改写质量" if step == "reduce.quality.calculate"
                    else "改写未降低风险分，正在保留原文")
        elif step == "reduce.finish":
            advance("finalizing", 98, "改写流程已完成，正在整理结果")
        elif step in {"api.scan.return", "api.reduce.return"}:
            advance("finalizing", 99, "处理已完成，正在整理返回结果", progress["scan_phase"])

    result = task.get("result") or {}
    if task["status"] == "success":
        message = "检测完成"
        if task["task_type"] == "reduce":
            message = "处理完成，已保留原文" if result.get("rewrite") is False else "改写完成"
        elif result.get("assessment_status") == "insufficient_evidence":
            message = "检测完成，当前证据不足，风险分仅供参考"
        progress.update(stage="completed", progress=100, message=message)
    elif task["status"] == "failed":
        message = {
            "engine_parse_error": "底层引擎返回了无法解析的格式，请重试",
            "engine_error": "处理引擎服务异常，请重试",
        }.get(task.get("error_code"), "任务处理失败，请重试")
        progress.update(stage="failed", message=message)
    return {**result, **progress}
