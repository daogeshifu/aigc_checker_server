import hashlib
import logging
import os
import sqlite3
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from api.contracts import ScanResponse, ReduceResponse, TaskListResponse, TaskDetailResponse, error_responses

from services.preprocess import clean_text
from services.checker import API_SOURCE, new_task_id, run_check, run_reduce
from services.task_log import fail_task, finish_task, get_task, list_tasks, record_task_event, start_task
from services.model_client import DETECTION_PROVIDER, default_model
from services.probability_client import PROBABILITY_ENABLED

logger = logging.getLogger(__name__)

router = APIRouter()

_security = HTTPBearer(description="填写本项目访问 Token，不是百炼/Azure API Key。Free 仅检测；Pro 支持检测、改写及条件复核；API_TOKEN 管理员还可查看全部任务。")
_API_TOKEN = os.getenv("API_TOKEN") or "aigcchecker"
_FREE_API_TOKENS = {token.strip() for token in os.getenv("FREE_API_TOKENS", "").split(",") if token.strip()}
_PRO_API_TOKENS = {token.strip() for token in os.getenv("PRO_API_TOKENS", "").split(",") if token.strip()}
_FREE_MAX_CHARS = int(os.getenv("FREE_MAX_CHARS", "3000"))
_PRO_MAX_CHARS = int(os.getenv("PRO_MAX_CHARS", "12000"))


def _client_id(plan: str, token: str) -> str:
    """用不可逆摘要隔离不同 Token 的任务记录，不把 Token 本身写入数据库。"""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return f"{plan}:{digest}"


def _token_context(token: str) -> dict:
    """根据 token 值返回对应的权限上下文（套餐、字数上限、功能开关等）。

    优先匹配 PRO 套餐 Token，其次是 FREE 套餐 Token，最后兼容旧版单一 API_TOKEN。
    未匹配到任何有效 Token 时抛出 401 异常。
    """
    if token in _PRO_API_TOKENS:
        # Pro 套餐：支持降 AI 改写和远端二审，字数上限更高
        return {
            "plan": "pro",
            "can_reduce": True,
            "can_remote_review": True,
            "max_chars": _PRO_MAX_CHARS,
            "client_id": _client_id("pro", token),
            "can_view_all_tasks": False,
        }
    if token in _FREE_API_TOKENS:
        # Free 套餐：不支持改写和二审，字数有限制
        return {
            "plan": "free",
            "can_reduce": False,
            "can_remote_review": False,
            "max_chars": _FREE_MAX_CHARS,
            "client_id": _client_id("free", token),
            "can_view_all_tasks": False,
        }
    if _API_TOKEN and token == _API_TOKEN:
        # 旧版单一 Token，向下兼容，权限等同 Pro
        return {
            "plan": "pro",
            "can_reduce": True,
            "can_remote_review": True,
            "max_chars": _PRO_MAX_CHARS,
            "client_id": _client_id("legacy", token),
            "can_view_all_tasks": True,
        }
    raise HTTPException(status_code=401, detail="Token 无效，拒绝访问")


def _verify_token(credentials: HTTPAuthorizationCredentials = Security(_security)) -> dict:
    """FastAPI 安全依赖：从请求头 Bearer Token 中提取并校验权限上下文。

    未配置 API_TOKEN 时使用 Demo 默认值 aigcchecker；Token 无效时返回 401。
    """
    return _token_context(credentials.credentials)


class ScanRequest(BaseModel):
    content: str = Field(description="待检测/改写原文。清洗后至少 50 个字符；最大长度按 strip 后文本及套餐配置检查。")
    model: Optional[str] = Field(default=None, description="可选 Qwen 模型名；仅 api_source 与服务器实际 DETECTION_PROVIDER 一致时覆盖默认模型。不会选择 Azure 复核模型。")
    api_source: str = Field(default=API_SOURCE, description="兼容参数，不能切换实际提供方。服务器通过 DETECTION_PROVIDER 选择 ollama/bailian；填写 azure/openrouter 也不会改变主检测路由。省略 model 可使用服务器默认模型。")
    request_source: Literal["demo", "test", "api"] = Field(default="api", description="仅用于任务来源标记，不改变权限或检测流程。")
    # Lets the caller follow its exact task while the synchronous POST is running.
    request_id: UUID | None = Field(default=None, description="可选唯一 UUID，便于在同步 POST 执行中轮询任务。task_id=scan-/reduce-加 UUID 的32位hex；重复使用返回409，不覆盖旧任务。")

    model_config = {"json_schema_extra": {"examples": [{"content": (
        "今天下午我整理了上周的工作记录，先核对客户反馈，再把遗漏的信息补进表格。"
        "同事发来修改意见后，我发现两处数字需要重新确认，因此决定明天核实后再发给客户。"
    ), "request_source": "api"}]}}


def api_description() -> str:
    return f"""文本 AI 风格检测与改写接口。

### 当前服务配置
- 主检测与表达扰动（由 DETECTION_PROVIDER 配置）：**{DETECTION_PROVIDER}**，默认模型 **{default_model()}**。请求中的 api_source 不能切换服务器路由。
- 原文概率测量：**{'已启用' if PROBABILITY_ENABLED else '未启用'}**；是否成功看 probability.status。
- Free 最大 {_FREE_MAX_CHARS} 字符；Pro/管理员最大 {_PRO_MAX_CHARS} 字符；清洗后至少 50 字符。

### 分数与测量值
- risk_score 为 0～100 的未校准 AI 风格风险分。ai_probability 是相同分数的历史字符串字段，**不是 AI 来源概率或 AI 内容占比**。
- 分块通常采用 70% Qwen 判断 + 30% 文本统计，再做文体/短文本修正，全文按分块长度加权。
- 短文本/列表可能跳过模型；模型失败后统计回退。HTTP 200 不保证模型成功，检查 judge_mode、warnings 和任务轨迹。
- 独立评分服务计算原文 NLL/PPL；disabled/unavailable 时为 null，当前不参与风险分。Burstiness 是句长波动统计；**Binoculars 尚未实现**。
- Pro 条件复核只调用 Azure；OpenRouter 仅保留在语义改写兜底链路。

### 调用方式
点击 Authorize 填写本项目访问 Token。POST /scan、/reduce 为同步接口；长文本/CPU 评分可能耗时较长。
可提供唯一 request_id，同时 GET /api/tasks/{{task_id}} 轮询脱敏进度。任务按 Token 隔离，管理员可查看全部任务。
"""


def _start_request_task(kind: str, request: ScanRequest, auth: dict, char_count: int) -> str:
    task_id = f"{kind}-{request.request_id.hex}" if request.request_id else new_task_id(kind)
    try:
        start_task(
            task_id=task_id, client_id=auth["client_id"], task_type=kind,
            source=request.request_source, plan=auth["plan"],
            requested_api_source=request.api_source, requested_model=request.model,
            char_count=char_count,
        )
    except sqlite3.IntegrityError as exc:
        # Never replace a task belonging to this or another caller on ID reuse.
        raise HTTPException(status_code=409, detail="请求标识已使用，请重新发起任务。") from exc
    return task_id


def _log(tag: str, task_id: str, request: ScanRequest, text_len: int) -> None:
    """记录请求日志，包含操作标签、模型名、API 来源和文本长度，便于问题排查。"""
    logger.info(
        "task=%s step=api.%s.accept requested_model=%s requested_api_source=%s chars=%d",
        task_id,
        tag,
        request.model or "default",
        request.api_source,
        text_len,
    )


@router.post("/scan", tags=["检测"], summary="检测文本 AI 风格风险",
             responses={200: {"model": ScanResponse, "description": "检测已返回；模型是否成功调用、是否证据不足需查看对应字段。"}, **error_responses(task=True)})
async def scan_document(request: ScanRequest, auth_context: dict = Security(_verify_token)):
    """清洗 → 语言/文体识别 → 统计特征 → 分块 Qwen 判断 → 评分融合 → 可选原文概率测量 → 可选 Azure 复核。

    - 实际模型提供方由服务器 `DETECTION_PROVIDER=ollama|bailian` 决定。
    - Free/Pro 均可检测，字数上限由套餐决定。
    - `judge_mode=qwen` 表示有效模型判断；`heuristic_skip` 表示按规则跳过；`heuristic_fallback` 表示模型失败后回退。
    - `probability.status` 区分 `disabled`、`measured`、`unavailable`；原文 PPL 不参与风险分，Binoculars 为 null。
    - Pro 且开启远程复核时，低置信度、40～65 边界分，或符合保守文体条件可触发 Azure；失败不转 OpenRouter。
    - 本地置信度低时按60%本地+40%复核融合，其他复核按72%+28%；复核仅修改文档结论。
    - 低置信度返回 `Insufficient Evidence`，数值仍仅供风险参考。
    """
    text = request.content.strip()
    # 最小长度限制：过短的文本无法提取有效特征
    if len(clean_text(text)) < 50:
        raise HTTPException(status_code=400, detail="文本太短，无法进行有效分析，请至少输入50个字符。")
    # 最大长度限制：由套餐权限决定
    if len(text) > auth_context["max_chars"]:
        raise HTTPException(
            status_code=400,
            detail=f"当前套餐最多支持 {auth_context['max_chars']} 个字符，当前文本过长。",
        )
    task_id = _start_request_task("scan", request, auth_context, len(text))
    try:
        _log("scan", task_id, request, len(text))
        record_task_event(
            task_id,
            "api.scan.accept",
            details={
                "source": request.request_source,
                "plan": auth_context["plan"],
                "chars": len(text),
                "max_chars": auth_context["max_chars"],
            },
        )
        result = await run_check(
            content=text,
            model=request.model,
            api_source=request.api_source,
            plan=auth_context["plan"],
            can_remote_review=auth_context["can_remote_review"],
            task_id=task_id,
        )
        result["task_id"] = task_id
        record_task_event(
            task_id,
            "api.scan.return",
            status="success",
            details={
                "label": result.get("label"),
                "ai_probability": result.get("ai_probability"),
                "confidence": result.get("confidence"),
            },
        )
        finish_task(
            task_id,
            {
                "label": result.get("label"),
                "ai_probability": result.get("ai_probability"),
                "confidence": result.get("confidence"),
                "review_used": result.get("review", {}).get("used", False),
            },
        )
        return result
    except ValueError as exc:
        logger.warning("task=%s step=api.scan.parse_error error=%s", task_id, exc)
        record_task_event(task_id, "api.scan.parse_error", status="failed", details={"error_type": type(exc).__name__})
        fail_task(task_id, "engine_parse_error")
        raise HTTPException(status_code=502, detail="底层引擎返回了无法解析的格式，请重试。", headers={"X-Task-ID": task_id})
    except Exception as exc:
        logger.exception("task=%s step=api.scan.error error=%s", task_id, exc)
        record_task_event(task_id, "api.scan.error", status="failed", details={"error_type": type(exc).__name__})
        fail_task(task_id, "engine_error")
        raise HTTPException(status_code=500, detail=f"检测引擎服务异常: {str(exc)}", headers={"X-Task-ID": task_id})


@router.post("/reduce", tags=["改写"], summary="改写文本并执行前后复检",
             responses={200: {"model": ReduceResponse, "description": "返回最终保留文本及前后风险分；rewrite=false 可表示失败或回滚。"}, **error_responses(task=True, forbidden=True)})
async def reduce_document(request: ScanRequest, auth_context: dict = Security(_verify_token)):
    """仅 Pro/管理员可用；Free 返回403，文本长度限制同检测接口。

    原文完整检测 → Azure 语义改写（失败时 OpenRouter 兜底）→ 按服务器提供方调用 Ollama/百炼表达扰动 → 本地规则调整 → 完整复检 → 质量评估及回滚保护。

    - 两次检测各自可能测量原文概率及触发 Azure 条件复核；语义改写失败时跳过后续扰动，仍执行复检。
    - 改写后风险分未下降时保留原文，`rewrite=false`。
    - 前后 `ai_probability` 字段均为风险分，不是来源概率。
    - `model=light|moderate|deep` 是改写处理等级，不是实际模型名称。
    - 当前响应返回改写和分数摘要，不包含完整 chunks/metrics/probability；过程细节通过 task_id 查询。
    """
    text = request.content.strip()
    if len(clean_text(text)) < 50:
        raise HTTPException(status_code=400, detail="文本太短，无法进行有效处理，请至少输入50个字符。")
    # 功能权限校验：Free 套餐不允许使用降 AI 改写
    if not auth_context["can_reduce"]:
        raise HTTPException(status_code=403, detail="当前套餐不支持降 AI 改写，请升级会员版。")
    if len(text) > auth_context["max_chars"]:
        raise HTTPException(
            status_code=400,
            detail=f"当前套餐最多支持 {auth_context['max_chars']} 个字符，当前文本过长。",
        )
    task_id = _start_request_task("reduce", request, auth_context, len(text))
    try:
        _log("reduce", task_id, request, len(text))
        record_task_event(
            task_id,
            "api.reduce.accept",
            details={
                "source": request.request_source,
                "plan": auth_context["plan"],
                "chars": len(text),
                "max_chars": auth_context["max_chars"],
            },
        )
        result = await run_reduce(
            content=text,
            model=request.model,
            api_source=request.api_source,
            plan=auth_context["plan"],
            can_remote_review=auth_context["can_remote_review"],
            task_id=task_id,
        )
        result["task_id"] = task_id
        record_task_event(
            task_id,
            "api.reduce.return",
            status="success",
            details={
                "rewrite": result.get("rewrite"),
                "ai_probability": result.get("ai_probability"),
                "ai_reduced_probability": result.get("ai_reduced_probability"),
                "quality_score": result.get("quality_score"),
            },
        )
        finish_task(
            task_id,
            {
                "rewrite": result.get("rewrite"),
                "ai_probability": result.get("ai_probability"),
                "ai_reduced_probability": result.get("ai_reduced_probability"),
                "quality_score": result.get("quality_score"),
            },
        )
        return result
    except ValueError as exc:
        logger.warning("task=%s step=api.reduce.parse_error error=%s", task_id, exc)
        record_task_event(task_id, "api.reduce.parse_error", status="failed", details={"error_type": type(exc).__name__})
        fail_task(task_id, "engine_parse_error")
        raise HTTPException(status_code=502, detail="底层引擎返回了无法解析的格式，请重试。", headers={"X-Task-ID": task_id})
    except Exception as exc:
        logger.exception("task=%s step=api.reduce.error error=%s", task_id, exc)
        record_task_event(task_id, "api.reduce.error", status="failed", details={"error_type": type(exc).__name__})
        fail_task(task_id, "engine_error")
        raise HTTPException(status_code=500, detail=f"降 AI 引擎服务异常: {str(exc)}", headers={"X-Task-ID": task_id})


@router.get("/tasks", tags=["任务日志"], summary="查询任务列表",
            responses={200: {"model": TaskListResponse}, **error_responses()})
async def task_history(
    limit: int = Query(default=200, ge=1, le=200, description="返回条数上限，实际条数受服务端任务保留数量限制。"),
    _auth_context: dict = Security(_verify_token),
):
    """按创建时间倒序返回最近任务的脱敏摘要，不包含原文、完整检测报告或改写内容。

    普通/会员 Token 仅能查询自己的任务；管理 API_TOKEN 可查询全部任务。
    """
    client_id = None if _auth_context["can_view_all_tasks"] else _auth_context["client_id"]
    return {"items": list_tasks(client_id=client_id, limit=limit)}


@router.get("/tasks/{task_id}", tags=["任务日志"], summary="查询任务执行轨迹与模型调用结果",
            responses={200: {"model": TaskDetailResponse}, **error_responses(missing=True)})
async def task_detail(task_id: str, _auth_context: dict = Security(_verify_token)):
    """查询进行中或已结束任务，适合轮询执行进度。权限与任务列表一致，不可见/过期任务返回404。

    - events 为脱敏步骤，elapsed_ms 是从任务开始的累计时间。
    - execution_summary.local 为主 Qwen 判别器的兼容字段，provider 可为 bailian 或 ollama，不代表一定在本地运行。
    - 调用次数区分尝试、成功、失败和待确认；一次判断内部可能有格式重试，不等同于底层 HTTP 请求数。
    - 服务端不保存原文/改写文本；任务状态 success 表示接口完成，不代表检测来源判断一定准确或所有模型调用均成功。
    """
    client_id = None if _auth_context["can_view_all_tasks"] else _auth_context["client_id"]
    task = get_task(task_id, client_id=client_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务记录不存在或已过期")
    return task
