import hashlib
import logging
import os
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from services.preprocess import clean_text
from services.checker import API_SOURCE, new_task_id, run_check, run_reduce
from services.task_log import fail_task, finish_task, get_task, list_tasks, record_task_event, start_task

logger = logging.getLogger(__name__)

router = APIRouter()

_security = HTTPBearer()
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
    content: str
    model: Optional[str] = None
    api_source: str = API_SOURCE
    request_source: Literal["demo", "test", "api"] = "api"


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


@router.post("/scan")
async def scan_document(request: ScanRequest, auth_context: dict = Security(_verify_token)):
    """AI 内容检测接口，对输入文本进行多维度分析并返回 AI 概率及详细报告。

    - 文本长度需在 50 ~ max_chars 之间，否则返回 400
    - ValueError 对应引擎返回格式异常，统一映射为 502
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
    task_id = new_task_id("scan")
    start_task(
        task_id=task_id,
        client_id=auth_context["client_id"],
        task_type="scan",
        source=request.request_source,
        plan=auth_context["plan"],
        requested_api_source=request.api_source,
        requested_model=request.model,
        char_count=len(text),
    )
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
        raise HTTPException(status_code=502, detail="底层引擎返回了无法解析的格式，请重试。")
    except Exception as exc:
        logger.exception("task=%s step=api.scan.error error=%s", task_id, exc)
        record_task_event(task_id, "api.scan.error", status="failed", details={"error_type": type(exc).__name__})
        fail_task(task_id, "engine_error")
        raise HTTPException(status_code=500, detail=f"检测引擎服务异常: {str(exc)}")


@router.post("/reduce")
async def reduce_document(request: ScanRequest, auth_context: dict = Security(_verify_token)):
    """降 AI 改写接口，对高 AI 概率文本进行改写并返回改写前后的概率对比。

    - 仅 Pro 套餐可用（can_reduce=True），Free 套餐返回 403
    - 文本长度限制同 /scan 接口
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
    task_id = new_task_id("reduce")
    start_task(
        task_id=task_id,
        client_id=auth_context["client_id"],
        task_type="reduce",
        source=request.request_source,
        plan=auth_context["plan"],
        requested_api_source=request.api_source,
        requested_model=request.model,
        char_count=len(text),
    )
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
        raise HTTPException(status_code=502, detail="底层引擎返回了无法解析的格式，请重试。")
    except Exception as exc:
        logger.exception("task=%s step=api.reduce.error error=%s", task_id, exc)
        record_task_event(task_id, "api.reduce.error", status="failed", details={"error_type": type(exc).__name__})
        fail_task(task_id, "engine_error")
        raise HTTPException(status_code=500, detail=f"降 AI 引擎服务异常: {str(exc)}")


@router.get("/tasks")
async def task_history(
    limit: int = Query(default=200, ge=1, le=200),
    _auth_context: dict = Security(_verify_token),
):
    """返回最近任务的脱敏摘要，不包含任何用户原文或改写内容。"""
    client_id = None if _auth_context["can_view_all_tasks"] else _auth_context["client_id"]
    return {"items": list_tasks(client_id=client_id, limit=limit)}


@router.get("/tasks/{task_id}")
async def task_detail(task_id: str, _auth_context: dict = Security(_verify_token)):
    """返回单个任务的完整执行轨迹；每一步仅暴露运行元数据。"""
    client_id = None if _auth_context["can_view_all_tasks"] else _auth_context["client_id"]
    task = get_task(task_id, client_id=client_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务记录不存在或已过期")
    return task
