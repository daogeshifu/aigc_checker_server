from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv


load_dotenv()  # 必须在其他业务模块 import 前执行

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from api.detect import router as detect_router
from services.checker import API_SOURCE, AZURE_DEFAULT_MODEL, OLLAMA_DEFAULT_MODEL, OPENROUTER_DEFAULT_MODEL


from services.ollama_client import start_ollama_client, close_ollama_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_ollama_client()
    try:
        yield
    finally:
        await close_ollama_client()


app = FastAPI(title="AIGC Checker Engine", version="1.2", lifespan=lifespan)
app.include_router(detect_router, prefix="/api")

_DEMO_PAGE = Path(__file__).with_name("web") / "demo.html"
_TASKS_PAGE = Path(__file__).with_name("web") / "tasks.html"


@app.get("/health", include_in_schema=False)
async def health():
    """容器存活检查，不调用模型服务。"""
    return {"status": "ok"}


def _render_demo_page(initial_view: str = "demo") -> str:
    """加载前端页面并注入非敏感运行配置；绝不把真实 Token 写进 HTML。"""
    html = _DEMO_PAGE.read_text(encoding="utf-8")
    default_model = (
        OLLAMA_DEFAULT_MODEL
        if API_SOURCE == "ollama"
        else AZURE_DEFAULT_MODEL
        if API_SOURCE == "azure"
        else OPENROUTER_DEFAULT_MODEL
    )
    replacements = {
        "__INITIAL_VIEW__": initial_view,
        "__API_SOURCE__": API_SOURCE,
        "__DEFAULT_MODEL__": default_model,
        "__OLLAMA_DEFAULT_MODEL__": OLLAMA_DEFAULT_MODEL,
        "__AZURE_DEFAULT_MODEL__": AZURE_DEFAULT_MODEL,
        "__OPENROUTER_DEFAULT_MODEL__": OPENROUTER_DEFAULT_MODEL,
    }
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)
    return html


@app.get("/", response_class=HTMLResponse)
async def get_demo_page():
    return HTMLResponse(content=_render_demo_page("demo"), headers={"Cache-Control": "no-store"})


@app.get("/demo", response_class=HTMLResponse)
async def get_demo_alias():
    return HTMLResponse(content=_render_demo_page("demo"), headers={"Cache-Control": "no-store"})


@app.get("/test", response_class=HTMLResponse)
async def get_test_page():
    return HTMLResponse(content=_render_demo_page("test"), headers={"Cache-Control": "no-store"})


@app.get("/tasks", response_class=HTMLResponse)
async def get_tasks_page():
    return HTMLResponse(content=_TASKS_PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})
