from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path

import numpy as np

from services.schemas import TokenProbabilityResult


def token_logprobs(logits: np.ndarray, targets: list[int]) -> np.ndarray:
    """Stable log-softmax/gather, with bounded temporary vocabulary matrices."""
    if logits.ndim != 2 or logits.shape[0] != len(targets):
        raise ValueError("Logits and target tokens must align")
    scores = []
    for offset in range(0, len(targets), 32):
        rows = np.asarray(logits[offset:offset + 32], dtype=np.float64)
        if not np.isfinite(rows).all():
            raise ValueError("Model produced nonfinite logits")
        ids = np.asarray(targets[offset:offset + len(rows)], dtype=np.int64)
        if np.any(ids < 0) or np.any(ids >= rows.shape[1]):
            raise ValueError("Target token is outside the vocabulary")
        maximum = rows.max(axis=1)
        log_normalizer = maximum + np.log(np.exp(rows - maximum[:, None]).sum(axis=1))
        scores.extend(rows[np.arange(len(rows)), ids] - log_normalizer)
    return np.minimum(np.asarray(scores, dtype=np.float64), 0.0)


class CpuScorer:
    def __init__(self, model, *, model_name: str, model_sha256: str, runtime: str,
                 context_tokens: int = 1024, stride_tokens: int = 512,
                 max_tokens: int = 16000):
        if context_tokens < 4 or not 0 < stride_tokens < context_tokens:
            raise ValueError("Require context >= 4 and 0 < stride < context")
        if max_tokens < 2:
            raise ValueError("max_tokens must be at least two")
        self.model = model
        self.model_name = model_name
        self.model_sha256 = model_sha256
        self.runtime = runtime
        self.context_tokens = context_tokens
        self.stride_tokens = stride_tokens
        self.max_tokens = max_tokens

    @classmethod
    def from_file(cls, path: str, *, threads: int = 4, context_tokens: int = 1024,
                  stride_tokens: int = 512, max_tokens: int = 16000):
        # Import only in the optional service, never in the normal API process.
        import llama_cpp

        model_path = Path(path)
        with model_path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        model = llama_cpp.Llama(
            model_path=str(model_path), n_gpu_layers=0, n_ctx=context_tokens,
            n_threads=threads, n_threads_batch=threads, n_batch=min(128, context_tokens),
            logits_all=True, verbose=False,
        )
        return cls(model, model_name=model_path.name, model_sha256=digest,
                   runtime=f"llama-cpp-python/{llama_cpp.__version__}",
                   context_tokens=context_tokens, stride_tokens=stride_tokens,
                   max_tokens=max_tokens)

    def score(self, text: str, *, include_token_logprobs: bool = False) -> dict:
        started = time.monotonic()
        # No chat template, instructions, or generated continuation are involved.
        tokens = self.model.tokenize(text.encode("utf-8"), add_bos=True, special=False)
        if len(tokens) < 2:
            raise ValueError("Text has fewer than two tokens")
        if len(tokens) > self.max_tokens:
            raise ValueError("Text exceeds the scorer token limit; nothing was truncated")
        values = []
        try:
            for target_start in range(1, len(tokens), self.stride_tokens):
                target_end = min(target_start + self.stride_tokens, len(tokens))
                context_start = max(0, target_end - self.context_tokens)
                window = tokens[context_start:target_end]
                self.model.reset()
                self.model.eval(window[:-1])
                row_start = target_start - context_start - 1
                logits = self.model.scores[row_start:len(window) - 1]
                values.extend(token_logprobs(logits, tokens[target_start:target_end]))
        finally:
            self.model.reset()
        nlls = -np.asarray(values, dtype=np.float64)
        nll = float(nlls.mean())
        # Overflow/invalid results must be explicit failures, never capped proxies.
        perplexity = math.exp(nll)
        result = TokenProbabilityResult(
            method="causal_token_likelihood_v1", model=self.model_name,
            model_sha256=self.model_sha256, runtime=self.runtime,
            context_tokens=self.context_tokens, stride_tokens=self.stride_tokens,
            token_count=len(tokens), scored_tokens=len(values),
            first_token_policy="tokenizer_add_bos_then_skip_index_0",
            nll=nll, perplexity=perplexity, nll_std=float(nlls.std()),
            elapsed_seconds=time.monotonic() - started,
        ).model_dump()
        if include_token_logprobs:
            result["token_logprobs"] = [float(value) for value in values]
        return result
