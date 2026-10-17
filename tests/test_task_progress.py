"""Polling milestones, concurrent chunk completion and rewrite phase boundaries."""
import unittest

from api.contracts import TaskProgressResult
from services.task_progress import task_progress


class TaskProgressTests(unittest.TestCase):
    def setUp(self):
        self.task = {"task_type": "scan", "status": "running", "events": [], "result": None}
        self.snapshots = []

    def event(self, step, **details):
        self.task["events"].append({"step": step, "details": details})
        result = task_progress(self.task)
        TaskProgressResult.model_validate(result)
        self.snapshots.append(result)
        self.assertEqual([item["progress"] for item in self.snapshots],
                         sorted(item["progress"] for item in self.snapshots))
        self.assertLess(result["progress"], 100)
        return result

    def test_concurrent_chunk_results_are_deduplicated_without_progress_regression(self):
        self.event("scan.start", scan_phase="primary_scan")
        initial = self.event("scan.chunking.finish", chunks=3)
        first = self.event("scan.chunk.score", chunk_id=2)
        self.assertGreater(first["progress"], initial["progress"])
        self.assertEqual(first["completed_chunks"], 1)
        late_features = self.event("scan.chunk.features", chunk_id=0)
        self.assertEqual(first, late_features)
        duplicate = self.event("scan.chunk.score", chunk_id=2)
        self.assertEqual(first, duplicate)
        self.event("judge.qwen.fallback", chunk_id=0)
        self.event("scan.chunk.score", chunk_id=0)
        complete = self.event("scan.chunk.score", chunk_id=1)
        self.assertEqual(complete["stage"], "detecting")
        self.assertEqual((complete["completed_chunks"], complete["total_chunks"]), (3, 3))
        self.assertIn("3/3", complete["message"])
        self.assertNotIn("ai_probability", complete)

    def test_scoring_and_review_failures_continue_until_task_reaches_terminal_state(self):
        self.task["task_type"] = "reduce"
        self.event("scan.start", scan_phase="original_scan")
        self.event("scan.chunking.finish", chunks=1)
        self.event("scan.chunk.score", chunk_id=0)
        measuring = self.event("scan.probability.start")
        self.assertEqual(measuring["stage"], "probability")
        self.assertEqual(measuring["scan_phase"], "original_scan")
        self.task["events"].append({"step": "scan.probability.failure", "status": "failed"})
        aggregating = self.event("scan.aggregate.finish")
        self.assertEqual(aggregating["stage"], "aggregating")
        reviewing = self.event("review.trigger")
        self.assertEqual(reviewing["stage"], "reviewing")
        self.task["events"].append({"step": "provider.azure.failure", "status": "failed"})
        continued = self.event("review.skip", reason="provider_error")
        self.assertEqual(continued["stage"], "finalizing")
        self.assertEqual(self.task["status"], "running")

    def test_rewrite_and_rescan_reset_chunk_counts_but_keep_overall_progress(self):
        self.task["task_type"] = "reduce"
        self.event("scan.start", scan_phase="original_scan")
        self.event("scan.chunking.finish", chunks=3)
        for chunk in (2, 0, 1):
            self.event("scan.chunk.score", chunk_id=chunk)
        self.event("scan.finish")
        rewriting = self.event("rewrite.pipeline.start")
        self.assertIsNone(rewriting["scan_phase"])
        self.assertEqual(rewriting["completed_chunks"], 0)
        for stage, expected in (("semantic_rewrite", "rewriting"),
                                ("expression_perturb", "perturbing"),
                                ("rule_injection", "rule_adjusting")):
            self.assertEqual(self.event("rewrite.step.start", stage=stage)["stage"], expected)
            self.event("rewrite.step.finish", stage=stage)
        self.event("reduce.post_rewrite")
        self.event("scan.start", scan_phase="reduced_scan")
        reset = self.event("scan.chunking.finish", chunks=2)
        self.assertEqual((reset["completed_chunks"], reset["total_chunks"]), (0, 2))
        first = self.event("scan.chunk.score", scan_phase="reduced_scan", chunk_id=0)
        self.assertEqual(first["completed_chunks"], 1)
        self.assertEqual(first["scan_phase"], "reduced_scan")
        self.assertIn("改写后文本", first["message"])
        self.event("scan.chunk.score", chunk_id=1)
        self.event("scan.finish")
        self.assertEqual(self.event("reduce.quality.calculate")["stage"], "evaluating")
        rollback = self.event("reduce.guard.rollback")
        self.assertIn("保留原文", rollback["message"])
        self.event("reduce.finish")
        self.assertEqual(self.event("api.reduce.return")["progress"], 99)

    def test_semantic_rewrite_fallback_can_continue_to_rescan(self):
        self.task["task_type"] = "reduce"
        self.event("rewrite.step.start", stage="semantic_rewrite")
        self.event("rewrite.step.failure", stage="semantic_rewrite")
        fallback = self.event("rewrite.fallback")
        self.assertIn("复检原文", fallback["message"])
        self.assertEqual(self.event("reduce.post_rewrite")["scan_phase"], "reduced_scan")

    def test_failure_keeps_last_progress_and_returns_business_error_message(self):
        self.event("scan.chunking.finish", chunks=2)
        last = self.event("scan.chunk.score", chunk_id=0)
        self.task["status"] = "failed"
        for code, message in (("engine_parse_error", "无法解析"), ("engine_error", "服务异常"),
                              ("unknown_error", "处理失败")):
            with self.subTest(code=code):
                self.task["error_code"] = code
                failed = task_progress(self.task)
                self.assertEqual(failed["stage"], "failed")
                self.assertEqual(failed["progress"], last["progress"])
                self.assertIn(message, failed["message"])
                self.assertEqual(failed["completed_chunks"], 1)

    def test_terminal_results_preserve_summary_and_support_historical_tasks_without_events(self):
        for kind, summary, message in (
            ("scan", {"ai_probability": "42.00"}, "检测完成"),
            ("scan", {"assessment_status": "insufficient_evidence"}, "证据不足"),
            ("reduce", {"rewrite": True, "quality_score": 60}, "改写完成"),
            ("reduce", {"rewrite": False, "ai_reduced_probability": "42.00"}, "保留原文"),
        ):
            with self.subTest(kind=kind, summary=summary):
                result = task_progress({"task_type": kind, "status": "success", "result": summary})
                TaskProgressResult.model_validate(result)
                self.assertEqual(result["stage"], "completed")
                self.assertEqual(result["progress"], 100)
                self.assertIn(message, result["message"])
                for key, value in summary.items():
                    self.assertEqual(result[key], value)


if __name__ == "__main__":
    unittest.main()
