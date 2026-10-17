from __future__ import annotations

import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from scripts.prepare_cpu import prepare_env
from services import checker, probability_client
from services.schemas import TokenProbabilityResult


def measurement():
    return {
        "method": "causal_token_likelihood_v1", "model": "fixture.gguf", "model_sha256": "a" * 64,
        "runtime": "fixture/1", "context_tokens": 16, "stride_tokens": 8,
        "token_count": 11, "scored_tokens": 10,
        "first_token_policy": "tokenizer_add_bos_then_skip_index_0",
        "nll": math.log(4), "perplexity": 4, "nll_std": 0.2, "elapsed_seconds": 0.1,
    }


class ContractTests(unittest.TestCase):
    def test_rejects_fake_inconsistent_or_partial_measurements(self):
        for override in [{"nll": float("nan")}, {"perplexity": float("inf")},
                         {"perplexity": 8}, {"scored_tokens": 5}, {"stride_tokens": 16},
                         {"model_sha256": "unversioned"}]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                TokenProbabilityResult.model_validate({**measurement(), **override})

    def test_cpu_config_preserves_original_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = "API_TOKEN=existing-secret\nAZURE_API_KEY=keep-secret\nOLLAMA_MODEL=old\n"
            (root / ".env").write_text(original)
            target = prepare_env(root)
            self.assertEqual((root / ".env").read_text(), original)
            content = target.read_text()
            self.assertIn("API_TOKEN=existing-secret", content)
            self.assertIn("AZURE_API_KEY=keep-secret", content)
            self.assertIn("OLLAMA_MODEL=cpu-qwen4b", content)
            self.assertNotIn("OLLAMA_MODEL=old", content)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            target.write_text(content + "CUSTOM=1\n")
            prepare_env(root)
            self.assertTrue(target.read_text().endswith("CUSTOM=1\n"))

    def test_cpu_config_replaces_public_default_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.example").write_text("API_TOKEN=aigcchecker\n")
            target = prepare_env(root)
            self.assertNotIn("API_TOKEN=aigcchecker", target.read_text())


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_never_calls_provider(self):
        with patch.object(probability_client, "PROBABILITY_ENABLED", False), \
             patch.object(probability_client.httpx, "AsyncClient") as constructor:
            result = await probability_client.measure_probability("original text")
        constructor.assert_not_called()
        self.assertEqual(result["status"], "disabled")

    async def test_measured_and_failure_paths(self):
        for payload, status in [(measurement(), "measured"),
                                ({**measurement(), "nll": 0}, "unavailable"),
                                ({"ai_score": 80}, "unavailable")]:
            with self.subTest(status=status):
                client = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, json=payload)))
                with patch.object(probability_client, "PROBABILITY_ENABLED", True), \
                     patch.object(probability_client.httpx, "AsyncClient", return_value=client), \
                     patch.object(probability_client, "record_task_event") as events:
                    result = await probability_client.measure_probability("original text", task_id="scan-probability")
                self.assertEqual(result["status"], status)
                self.assertFalse(result["used_in_risk_score"])
                self.assertEqual(events.call_args_list[0].args, ("scan-probability", "scan.probability.start"))
                self.assertEqual(events.call_args_list[-1].args[1],
                                 "scan.probability.finish" if status == "measured" else "scan.probability.failure")

    async def test_busy_or_timeout_remains_unavailable(self):
        def timeout(request):
            raise httpx.ReadTimeout("scorer timeout")
        for handler in [lambda req: httpx.Response(429), timeout]:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with patch.object(probability_client, "PROBABILITY_ENABLED", True), \
                 patch.object(probability_client.httpx, "AsyncClient", return_value=client):
                result = await probability_client.measure_probability("original text")
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("perplexity", result)

    async def test_whole_document_measured_once_without_changing_risk(self):
        text = ("I walked to the river after lunch. The water was quiet. "
                "A small dog followed its owner along the path. ") * 8
        judge = {"ai_score": 50, "confidence": 0.8, "label": "mixed", "signals": {}, "judge_mode": "qwen"}
        async def detect(probability):
            mock = AsyncMock(return_value=probability)
            with patch.object(checker, "measure_probability", mock), \
                 patch.object(checker, "judge_chunk_with_qwen", AsyncMock(return_value=judge)):
                result = await checker.run_check(text, plan="free")
            mock.assert_awaited_once_with(text.strip(), task_id=None)
            return result
        baseline = await detect({"status": "disabled", "used_in_risk_score": False})
        measured = await detect({"status": "measured", "used_in_risk_score": False, **measurement()})
        failed = await detect({"status": "unavailable", "used_in_risk_score": False})
        self.assertEqual(baseline["risk_score"], measured["risk_score"])
        self.assertEqual(measured["metrics"]["perplexity"], 4)
        self.assertEqual(measured["metrics"]["nll"], math.log(4))
        self.assertIsNone(measured["metrics"]["binoculars"])
        self.assertIsNone(failed["metrics"]["perplexity"])
        self.assertIn("probability_unavailable", failed["warnings"])


if __name__ == "__main__":
    unittest.main()
