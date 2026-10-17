"""Optional measured likelihood. Uncalibrated measurements never alter risk scores."""
from __future__ import annotations

import logging
import os

import httpx

from services.schemas import TokenProbabilityResult
from services.task_log import record_task_event


logger = logging.getLogger(__name__)
PROBABILITY_ENABLED = os.getenv("PROBABILITY_ENABLED", "false").lower() in {"true", "1", "yes"}
PROBABILITY_BASE_URL = os.getenv("PROBABILITY_BASE_URL", "http://127.0.0.1:8091").rstrip("/")
PROBABILITY_TIMEOUT = float(os.getenv("PROBABILITY_TIMEOUT", "600"))


async def measure_probability(text: str, *, task_id: str | None = None) -> dict:
    if not PROBABILITY_ENABLED:
        return {"status": "disabled", "used_in_risk_score": False}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(PROBABILITY_TIMEOUT, connect=5)) as client:
            response = await client.post(f"{PROBABILITY_BASE_URL}/score", json={"text": text})
            response.raise_for_status()
            measured = TokenProbabilityResult.model_validate(response.json()).model_dump()
        record_task_event(task_id, "scan.probability.finish", status="success", details={
            "model": measured["model"], "scored_tokens": measured["scored_tokens"],
            "nll": measured["nll"], "perplexity": measured["perplexity"],
            "elapsed_seconds": measured["elapsed_seconds"], "used_in_risk_score": False,
        })
        return {"status": "measured", "used_in_risk_score": False, **measured}
    except Exception as exc:
        # Never log source text or invent a substitute PPL when unavailable/busy.
        logger.warning("task=%s original_text_scoring_failed error_type=%s", task_id or "-", type(exc).__name__)
        record_task_event(task_id, "scan.probability.failure", status="failed",
                          details={"error_type": type(exc).__name__})
        return {"status": "unavailable", "used_in_risk_score": False, "error_type": type(exc).__name__}
