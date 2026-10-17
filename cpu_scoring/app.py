"""Single-process CPU service. The deployment exposes it on loopback only."""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from cpu_scoring.engine import CpuScorer


class ScoreRequest(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    include_token_logprobs: bool = False


def create_app(scorer_factory=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.scorer = scorer_factory() if scorer_factory else CpuScorer.from_file(
            os.environ["SCORER_MODEL_PATH"],
            threads=int(os.getenv("SCORER_THREADS", "4")),
            context_tokens=int(os.getenv("SCORER_CONTEXT_TOKENS", "1024")),
            stride_tokens=int(os.getenv("SCORER_STRIDE_TOKENS", "512")),
            max_tokens=int(os.getenv("SCORER_MAX_TOKENS", "16000")),
        )
        try:
            yield
        finally:
            close = getattr(app.state.scorer.model, "close", None)
            if close:
                close()

    app = FastAPI(title="Original-text CPU scorer", lifespan=lifespan)
    gate = threading.Lock()

    @app.get("/health")
    def health():
        scorer = app.state.scorer
        return {"status": "ok", "model": scorer.model_name,
                "model_sha256": scorer.model_sha256, "runtime": scorer.runtime}

    @app.post("/score")
    def score(request: ScoreRequest):
        if not request.text.strip():
            raise HTTPException(status_code=422, detail="Text is empty")
        if not gate.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="CPU scorer is busy", headers={"Retry-After": "5"})
        try:
            return app.state.scorer.score(request.text, include_token_logprobs=request.include_token_logprobs)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            gate.release()

    return app


app = create_app()
