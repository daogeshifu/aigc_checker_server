"""Azure OpenAI v1 client matching wd-ranktool AIService::chatWithAzure.

No credentials are copied from other projects. Existing AZURE_* settings and
the PHP project's AZURE_OPENAI_* names are supported.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

import httpx


def azure_base_url(endpoint: str) -> str:
    parts = urlsplit(endpoint.strip())
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment or parts.username:
        raise ValueError("Azure endpoint 必须为不含查询参数的 HTTPS 资源地址或 /openai/v1/ 地址")
    path = parts.path.rstrip("/")
    if path not in {"", "/openai", "/openai/v1"}:
        raise ValueError("Azure v1 endpoint 请填写资源根地址或截至 /openai/v1/ 的地址")
    return urlunsplit((parts.scheme, parts.netloc, "/openai/v1/", "", ""))


def create_azure_client():
    key = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("AZURE_ENDPOINT")
    if not key or not endpoint:
        return None
    mode = os.getenv("AZURE_API_MODE", "v1").strip().lower()
    timeout = float(os.getenv("AZURE_OPENAI_TIMEOUT") or os.getenv("AZURE_TIMEOUT", "600"))
    connect = float(os.getenv("AZURE_OPENAI_CONNECT_TIMEOUT") or os.getenv("AZURE_CONNECT_TIMEOUT", "10"))
    if timeout <= 0 or connect <= 0:
        raise ValueError("Azure 超时时间必须大于 0")
    options = {"api_key": key, "timeout": httpx.Timeout(timeout, connect=connect)}
    if mode == "v1":
        from openai import AsyncOpenAI
        # AsyncOpenAI sends Authorization: Bearer <key>, as does the PHP client.
        return AsyncOpenAI(base_url=azure_base_url(endpoint), **options)
    if mode == "legacy":
        from openai import AsyncAzureOpenAI
        return AsyncAzureOpenAI(azure_endpoint=endpoint,
                                api_version=os.getenv("AZURE_API_VERSION", "2025-04-01-preview"), **options)
    raise ValueError("AZURE_API_MODE 只能是 v1 或 legacy")


def request_options(purpose: str) -> dict:
    """Deployment names may be arbitrary; never infer model capabilities from them."""
    effort = os.getenv("AZURE_REVIEW_REASONING_EFFORT", "").strip() if purpose == "remote_review" else ""
    options = {"response_format": {"type": "json_object"}, "stream": False}
    if effort:
        if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("AZURE_REVIEW_REASONING_EFFORT 配置不合法")
        # extra_body also works with older installed SDKs lacking the typed field.
        options["extra_body"] = {"reasoning_effort": effort}
    # v1 matches the PHP request: omit custom sampling parameters. Reasoning
    # models can reject temperature, even if hidden behind an arbitrary alias.
    if os.getenv("AZURE_API_MODE", "v1").strip().lower() == "legacy" and not effort:
        options["temperature"] = 0.2
    return options
