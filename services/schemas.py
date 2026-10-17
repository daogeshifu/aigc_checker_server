from __future__ import annotations

from typing import Literal

import math

from pydantic import BaseModel, Field, model_validator


class QwenSignals(BaseModel):
    over_smooth: float = Field(..., ge=0, le=1)
    template_pattern: float = Field(..., ge=0, le=1)
    sentence_uniformity: float = Field(..., ge=0, le=1)
    human_detail: float = Field(..., ge=0, le=1)


class QwenJudgeResult(BaseModel):
    ai_score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)
    signals: QwenSignals
    label: Literal["human", "mixed", "ai"]


class ReduceChange(BaseModel):
    original: str
    revised: str
    reason: str


class RewriteDraftResult(BaseModel):
    reduced: str
    changes: list[ReduceChange] = Field(default_factory=list)


class ReduceRewriteResult(BaseModel):
    reduced: str
    rewrite: bool = True
    ai_probability: str = "0.00"
    ai_reduced_probability: str = "0.00"
    quality_score: float = Field(default=0, ge=0, le=100)
    model: Literal["light", "moderate", "deep"] = "moderate"
    changes: list[ReduceChange] = Field(default_factory=list)


class ChunkSpan(BaseModel):
    chunk_id: int
    start: int
    end: int
    text: str


class RemoteReviewResult(BaseModel):
    ai_score: float = Field(..., ge=0, le=100)
    confidence: float = Field(..., ge=0, le=1)
    label: Literal["human", "mixed", "ai"]


class TokenProbabilityResult(BaseModel):
    """Measured original-text likelihood, never an authorship probability."""

    method: Literal["causal_token_likelihood_v1"]
    model: str = Field(min_length=1)
    model_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime: str = Field(min_length=1)
    context_tokens: int = Field(ge=4)
    stride_tokens: int = Field(ge=1)
    token_count: int = Field(ge=2)
    scored_tokens: int = Field(ge=1)
    first_token_policy: Literal["tokenizer_add_bos_then_skip_index_0"]
    nll: float = Field(ge=0, allow_inf_nan=False)
    perplexity: float = Field(ge=1, allow_inf_nan=False)
    nll_std: float = Field(ge=0, allow_inf_nan=False)
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_measurement(self):
        if self.scored_tokens != self.token_count - 1:
            raise ValueError("All tokens after index zero must be scored exactly once")
        if self.stride_tokens >= self.context_tokens:
            raise ValueError("Stride must leave at least one token of context")
        if not math.isclose(math.log(self.perplexity), self.nll, abs_tol=1e-5):
            raise ValueError("Perplexity must equal exp(mean token NLL)")
        return self
