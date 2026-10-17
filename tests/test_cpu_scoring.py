from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
import threading
import unittest

from fastapi.testclient import TestClient

try:
    import numpy as np
    from cpu_scoring.app import create_app
    from cpu_scoring.engine import CpuScorer, token_logprobs
except ModuleNotFoundError as exc:
    if exc.name != "numpy":
        raise
    np = None


class MarkovModel:
    """Known two-token source: switching token has probability 3/4."""
    def __init__(self, tokens):
        self.tokens = tokens
        self.windows = []

    def tokenize(self, raw, *, add_bos, special):
        assert add_bos and not special
        return self.tokens

    def reset(self):
        pass

    def eval(self, tokens):
        self.windows.append(tokens)
        self.scores = np.array([[1000 + (math.log(3) if candidate != previous else 0)
                                 for candidate in [0, 1]] for previous in tokens])


def make_scorer(tokens=None, **kwargs):
    return CpuScorer(MarkovModel(tokens or [0, 1, 1, 0, 0, 1, 0, 1, 1, 0, 1]),
                     model_name="fixture.gguf", model_sha256="a" * 64, runtime="fixture/1",
                     context_tokens=4, stride_tokens=2, **kwargs)


@unittest.skipIf(np is None, "Optional CPU tests require numpy")
class ScoringTests(unittest.TestCase):
    def test_rolling_windows_score_every_target_once_with_correct_shift(self):
        scorer = make_scorer()
        result = scorer.score("raw text", include_token_logprobs=True)
        tokens = scorer.model.tokens
        expected = [math.log(0.75 if left != right else 0.25) for left, right in zip(tokens, tokens[1:])]
        np.testing.assert_allclose(result["token_logprobs"], expected, atol=1e-10)
        self.assertEqual(result["scored_tokens"], len(tokens) - 1)
        self.assertAlmostEqual(result["nll"], -np.mean(expected))
        self.assertAlmostEqual(result["perplexity"], math.exp(-np.mean(expected)))
        self.assertTrue(all(len(window) < 4 for window in scorer.model.windows))

    def test_limits_reject_instead_of_truncating(self):
        for scorer in [make_scorer(max_tokens=4), make_scorer(tokens=[0])]:
            with self.assertRaises(ValueError):
                scorer.score("raw text")
            self.assertEqual(scorer.model.windows, [])

    def test_invalid_logits_are_not_fabricated_into_measurements(self):
        for logits, targets in [(np.array([[math.nan, 0]]), [0]),
                                (np.zeros((2, 2)), [0]), (np.zeros((1, 2)), [2])]:
            with self.assertRaises(ValueError):
                token_logprobs(logits, targets)

    def test_service_rejects_empty_and_oversized_input(self):
        with TestClient(create_app(make_scorer)) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            for text in [" ", "", "x" * 12001]:
                self.assertEqual(client.post("/score", json={"text": text}).status_code, 422)
            result = client.post("/score", json={"text": "real input"})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertNotIn("token_logprobs", result.json())

    def test_service_does_not_run_two_model_evaluations_at_once(self):
        entered, release = threading.Event(), threading.Event()
        scorer = make_scorer()
        original_score = scorer.score
        def blocking_score(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timed out")
            return original_score(*args, **kwargs)
        scorer.score = blocking_score
        with TestClient(create_app(lambda: scorer)) as client, ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(client.post, "/score", json={"text": "first"})
            try:
                self.assertTrue(entered.wait(3))
                second = client.post("/score", json={"text": "second"})
                self.assertEqual(second.status_code, 429)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=3).status_code, 200)


if __name__ == "__main__":
    unittest.main()
