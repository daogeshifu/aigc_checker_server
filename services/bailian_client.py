"""Bailian Qwen text/JSON generation; never treat generated scores as token likelihood."""
from __future__ import annotations

import asyncio
import json
import os

import httpx

from services.concurrency import bounded_concurrency


BAILIAN_BASE_URL = os.getenv("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
BAILIAN_API_KEY = os.getenv("BAILIAN_API_KEY", "")
BAILIAN_MODEL = os.getenv("BAILIAN_MODEL", "qwen-plus")
BAILIAN_REWRITE_MODEL = os.getenv("BAILIAN_REWRITE_MODEL", BAILIAN_MODEL)
BAILIAN_TIMEOUT = float(os.getenv("BAILIAN_TIMEOUT", "120"))
BAILIAN_MAX_CONCURRENCY = bounded_concurrency(os.getenv("BAILIAN_MAX_CONCURRENCY"))
BAILIAN_JSON_MAX_TOKENS = max(256, int(os.getenv("BAILIAN_JSON_MAX_TOKENS", "512")))
_http_client = None
_request_gate = None


async def start_bailian_client():
    global _http_client, _request_gate
    _request_gate = asyncio.Semaphore(BAILIAN_MAX_CONCURRENCY)
    _http_client = httpx.AsyncClient(timeout=httpx.Timeout(BAILIAN_TIMEOUT, connect=10),
                                    limits=httpx.Limits(max_connections=BAILIAN_MAX_CONCURRENCY,
                                                       max_keepalive_connections=BAILIAN_MAX_CONCURRENCY))


async def close_bailian_client():
    global _http_client, _request_gate
    if _http_client is not None:
        await _http_client.aclose()
    _http_client = _request_gate = None


class TruncatedResponseError(RuntimeError):
    pass


def validate_config():
    if not BAILIAN_API_KEY or BAILIAN_API_KEY.startswith(("replace_", "REPLACE_")):
        raise ValueError("使用百炼需要配置 BAILIAN_API_KEY")
    if not BAILIAN_BASE_URL.startswith("https://"):
        raise ValueError("BAILIAN_BASE_URL 必须是 HTTPS 的 compatible-mode/v1 地址")


async def _post(payload):
    validate_config()

    async def send():
        async def request(client):
            try:
                response = await client.post(f"{BAILIAN_BASE_URL}/chat/completions", json=payload,
                                             headers={"Authorization": f"Bearer {BAILIAN_API_KEY}"})
                if response.is_error:
                    raise RuntimeError(f"百炼请求失败（HTTP {response.status_code}）")
                data = response.json()
                choice = data["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise TruncatedResponseError("百炼输出达到 token 上限")
                content = choice.get("message", {}).get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("百炼未返回最终文本")
                return content.strip()
            except httpx.RequestError as exc:
                # Do not expose request bodies, credentials, or provider response bodies.
                raise RuntimeError(f"百炼网络请求失败（{type(exc).__name__}）") from None
        if _http_client is not None:
            return await request(_http_client)
        async with httpx.AsyncClient(timeout=httpx.Timeout(BAILIAN_TIMEOUT, connect=10)) as client:
            return await request(client)

    if _request_gate is not None:
        async with _request_gate:
            return await send()
    return await send()


async def generate_json(system_prompt, user_prompt, schema, model=None, options=None, trace_label=None):
    # json_object works across more Qwen models than strict json_schema. The caller
    # still validates all required fields/ranges using the same Pydantic schema.
    system = system_prompt + "\n只输出符合以下 JSON Schema 的 JSON 对象：\n" + json.dumps(schema, ensure_ascii=False)
    payload = {"model": model or BAILIAN_MODEL,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_prompt}],
               "stream": False, "enable_thinking": False, "temperature": 0,
               "max_tokens": BAILIAN_JSON_MAX_TOKENS, "response_format": {"type": "json_object"}}
    for attempt in range(2):
        try:
            result = json.loads(await _post(payload))
            if not isinstance(result, dict):
                raise ValueError("百炼输出必须是 JSON 对象")
            return result
        except (TruncatedResponseError, json.JSONDecodeError):
            if attempt:
                raise
            payload["max_tokens"] *= 2


async def generate_text(system_prompt, user_prompt, model=None, options=None, trace_label=None):
    options = options or {}
    return await _post({"model": model or BAILIAN_REWRITE_MODEL,
                        "messages": [{"role": "system", "content": system_prompt},
                                     {"role": "user", "content": user_prompt}],
                        "stream": False, "enable_thinking": False,
                        "temperature": options.get("temperature", 0.3),
                        "max_tokens": options.get("num_predict", 1024)})
