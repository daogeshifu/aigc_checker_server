from __future__ import annotations

import asyncio
import copy
import json
import re
import unittest
from unittest.mock import AsyncMock, patch

from services import checker, ollama_client
from services.aggregate import _feature_score, aggregate_document, score_chunk
from services.features import extract_document_features, _detail_signal_count, _count_connectors
from services.judges import _heuristic_fallback, _normalize_qwen_result, _should_skip_qwen, judge_chunk_with_qwen
from services.preprocess import chunk_text, clean_text, split_sentences
from scripts.evaluate_detection import evaluate

SAMPLE = 'The garden was quiet after the rain. Birds returned to the trees. People walked along the river and watched the clouds.'
JUDGE = {"ai_score": 50, "confidence": 0.8, "label": "mixed", "signals": {
    "over_smooth": 0.5, "template_pattern": 0.5, "sentence_uniformity": 0.5, "human_detail": 0.5}, "judge_mode": "qwen"}


def document(text=SAMPLE * 4, judge=None, genre='general'):
    f = extract_document_features(text)
    q = judge or JUDGE
    c = {"chunk_id": 0, "text": text, **score_chunk(f, q, genre), "judge_mode": q['judge_mode']}
    return aggregate_document([c], genre, f)


class PreprocessTests(unittest.TestCase):
    def test_english_sentence_statistics(self):
        f = extract_document_features(SAMPLE)
        self.assertEqual(f['sentence_count'], 3)
        self.assertGreater(f['burstiness'], 0)
        self.assertEqual(f['detail_signal_count'], 0)

    def test_short_sentences_are_not_merged_for_statistics(self):
        self.assertEqual(split_sentences('Hi. Yes! 好的。知道了。'), ['Hi.', 'Yes!', '好的。', '知道了。'])

    def test_abbreviations_numbers_urls_quotes(self):
        text = 'Dr. Smith paid $3.14 at example.com. "Done." Next comes v1.2.3. J. Smith agreed.'
        self.assertEqual(split_sentences(text), ['Dr. Smith paid $3.14 at example.com.', '"Done."', 'Next comes v1.2.3.', 'J. Smith agreed.'])

    def test_chunk_limits_offsets_and_content_coverage(self):
        for text in [SAMPLE * 30, '长文本没有标点' * 900, 'a' * 12000, ('word ' * 2500), '短。\n\n' + SAMPLE * 10, '  测试\r\n\r\n' + '长' * 500]:
            cleaned = clean_text(text)
            chunks = chunk_text(text)
            previous_end = 0
            for index, chunk in enumerate(chunks):
                self.assertEqual(chunk['chunk_id'], index)
                self.assertLessEqual(len(chunk['text']), 480)
                self.assertEqual(chunk['text'], cleaned[chunk['start']:chunk['end']])
                self.assertFalse(cleaned[previous_end:chunk['start']].strip())
                previous_end = chunk['end']
            self.assertFalse(cleaned[previous_end:].strip())
            self.assertEqual(re.sub(r'\s', '', ''.join(c['text'] for c in chunks)), re.sub(r'\s', '', cleaned))

    def test_empty_and_invalid_limits(self):
        self.assertEqual(chunk_text('  '), [])
        with self.assertRaises(ValueError):
            chunk_text('hello', max_size=100)


class FeatureTests(unittest.TestCase):
    def test_detail_patterns_count_facts_without_overlap(self):
        self.assertEqual(_detail_signal_count('2026-09-16 09:30 15% $200', 'en'), 4)
        self.assertEqual(_detail_signal_count('上线日期2026年9月16日，价格￥200。', 'zh'), 3)
        self.assertEqual(_detail_signal_count('hello ordinary lowercase words NASA', 'en'), 0)
        self.assertEqual(_detail_signal_count('发布项目 ABC-123 on 2026-09-16', 'mixed'), 2)

    def test_english_connector_boundaries(self):
        self.assertEqual(_count_connectors('howevermore finallyx', 'en'), 0)
        self.assertEqual(_count_connectors('However, finally we finished.', 'en'), 2)

    def test_chinese_repeated_phrases(self):
        words = extract_document_features('人工智能帮助处理文本，人工智能也用于分析。', 'zh')['suspicious_terms']
        self.assertIn('人工智能', words)

    def test_details_are_not_an_authorship_discount(self):
        f = extract_document_features(SAMPLE * 4)
        other = {**f, 'detail_signal_count': 99}
        self.assertEqual(score_chunk(f, JUDGE, 'general')['ai_score'], score_chunk(other, JUDGE, 'general')['ai_score'])
        self.assertFalse(_should_skip_qwen(other, 'academic', 'en')[0])


class ScoringTests(unittest.TestCase):
    def test_style_is_explanation_only(self):
        f = extract_document_features(SAMPLE * 4)
        alternate = {**JUDGE, 'signals': {k: 1.0 for k in JUDGE['signals']}}
        self.assertEqual(score_chunk(f, JUDGE, 'general')['ai_score'], score_chunk(f, alternate, 'general')['ai_score'])

    def test_heuristic_stays_low_and_metrics_unavailable(self):
        text = SAMPLE * 4
        f = extract_document_features(text)
        result = document(text, _heuristic_fallback(f, 'general'))
        self.assertEqual(result['confidence'], 'low')
        self.assertEqual(result['label'], 'Insufficient Evidence')
        self.assertIsNone(result['metrics']['perplexity'])
        self.assertIsNone(result['metrics']['binoculars'])
        self.assertFalse(result['calibrated'])
        self.assertIn('statistics_only', result['warnings'])

    def test_low_model_confidence_not_inflated(self):
        result = document(judge={**JUDGE, 'confidence': 0.2})
        self.assertEqual(result['confidence'], 'low')

    def test_short_document_abstains(self):
        result = document('A short sample with very little evidence. Just a few words.')
        self.assertEqual(result['assessment_status'], 'insufficient_evidence')

    def test_formal_correction_applied_once(self):
        f = extract_document_features(SAMPLE * 4)
        q = _heuristic_fallback(f, 'academic')
        self.assertAlmostEqual(score_chunk(f, q, 'academic')['ai_score'], max(0, _feature_score(f) - 8), places=2)
        result = document(SAMPLE * 4, q, 'academic')
        self.assertEqual(result['risk_score'], result['chunks'][0]['ai_score'])

    def test_output_granularity_and_aliases(self):
        result = document()
        self.assertEqual(float(result['ai_probability']), result['risk_score'])
        self.assertEqual(result['sentences'], result['segments'])
        self.assertEqual(result['granularity'], 'chunk')
        self.assertEqual(sum(result['summary']['percentages'].values()), 100)

    def test_missing_and_nonfinite_model_fields_rejected(self):
        for response in [{'label': 'human'}, {**JUDGE, 'ai_score': float('nan')}, {**JUDGE, 'confidence': float('inf')}]:
            with self.assertRaises(ValueError):
                _normalize_qwen_result(response)

    def test_empty_document_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_document([], 'general', {})

    def test_conflicting_review_abstains_and_preserves_local_score(self):
        result = document(judge={**JUDGE, 'ai_score': 20})
        original = result['risk_score']
        reviewed = checker._merge_review_result(result, {'ai_score': 99, 'confidence': 0.99, 'label': 'ai'}, {})
        self.assertEqual(reviewed['label'], 'Insufficient Evidence')
        self.assertEqual(reviewed['confidence'], 'low')
        self.assertEqual(reviewed['review']['local_risk_score'], original)
        self.assertEqual(reviewed['risk_score'], float(reviewed['ai_probability']))
        self.assertIn('review_disagreement', reviewed['warnings'])

    def test_short_text_not_rescued_by_self_reported_review(self):
        result = document('A short sample has limited evidence. Another small sentence.')
        reviewed = checker._merge_review_result(result, {'ai_score': result['risk_score'], 'confidence': 1, 'label': 'mixed'}, {})
        self.assertEqual(reviewed['confidence'], 'low')

    def test_blind_review_selects_late_high_risk_chunk(self):
        chunks = [{'chunk_id': i, 'text': str(i), 'ai_score': i * 10, 'score_breakdown': {'qwen_score': i * 10, 'feature_score': 20}} for i in range(10)]
        prompt = json.loads(checker._build_remote_review_prompt('full text', {'chunks': chunks}))
        self.assertIn(9, [c['chunk_id'] for c in prompt['representative_chunks']])
        self.assertNotIn('ai_score', json.dumps(prompt))


class AsyncDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_failure_is_explicit(self):
        with patch('services.judges.generate_json', AsyncMock(side_effect=RuntimeError('offline'))):
            q = await judge_chunk_with_qwen(SAMPLE * 4, 'general', extract_document_features(SAMPLE * 4))
        self.assertEqual(q['judge_mode'], 'heuristic_fallback')
        self.assertEqual(q['judge_skip_reason'], 'qwen_provider_error')

    async def test_successful_pipeline_order_and_no_external_calls(self):
        active, maximum = 0, 0
        async def judge(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.001)
            active -= 1
            return copy.deepcopy(JUDGE)
        with patch('services.checker.judge_chunk_with_qwen', side_effect=judge), patch.dict('os.environ', {'SCAN_CHUNK_CONCURRENCY': '2'}):
            result = await checker.run_check(SAMPLE * 12)
        self.assertEqual(maximum, 2)
        self.assertEqual([c['chunk_id'] for c in result['chunks']], list(range(len(result['chunks']))))
        self.assertFalse(result['review']['used'])
        json.dumps(result, allow_nan=False)

    async def test_truncation_retry_increases_budget(self):
        responses = AsyncMock(side_effect=[ollama_client.TruncatedResponseError('limit'), '{"ok":true}'])
        with patch('services.ollama_client._post_generate', responses):
            result = await ollama_client.generate_json('system', 'user', {})
        self.assertTrue(result['ok'])
        first, second = [c.args[0] for c in responses.await_args_list]
        self.assertGreater(second['options']['num_predict'], first['options']['num_predict'])

    async def test_shared_http_lifecycle_and_concurrency(self):
        import httpx
        active, maximum = 0, 0
        async def handler(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.001)
            active -= 1
            return httpx.Response(200, json={'response': '{}', 'done_reason': 'stop'})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(ollama_client, '_http_client', client), patch.object(ollama_client, '_request_gate', asyncio.Semaphore(2)):
            await asyncio.gather(*(ollama_client._post_generate({'format': 'json'}) for _ in range(6)))
            await ollama_client.close_ollama_client()
            self.assertTrue(client.is_closed)
        self.assertEqual(maximum, 2)

    async def test_http_length_stop_rejected(self):
        import httpx
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'response': '{}', 'done_reason': 'length'}))) as client:
            with patch.object(ollama_client, '_http_client', client):
                with self.assertRaises(ollama_client.TruncatedResponseError):
                    await ollama_client._post_generate({'format': 'json'})


class EvaluationTests(unittest.TestCase):
    def rows(self):
        return [{'id': str(i), 'group_id': str(i), 'split': 'test', 'label': label, 'prediction': {'risk_score': score, 'assessment_status': status}}
                for i, (label, score, status) in enumerate([('human', 90, 'assessed'), ('human', 10, 'assessed'), ('ai', 90, 'assessed'), ('ai', 90, 'insufficient_evidence'), ('mixed', 50, 'assessed')])]

    def test_metrics_include_abstention_and_tied_ranks(self):
        overall = evaluate(self.rows())['overall']
        self.assertEqual(overall['human_false_positive_rate'], 0.5)
        self.assertEqual(overall['ai_recall'], 0.5)
        self.assertEqual(overall['coverage'], 0.8)
        self.assertEqual(overall['binary_ranking_auroc'], 0.75)
        self.assertEqual(overall['mixed_count'], 1)

    def test_cross_split_group_leakage_rejected(self):
        rows = self.rows()
        rows.append({**rows[0], 'id': 'new', 'split': 'calibration'})
        with self.assertRaises(ValueError):
            evaluate(rows)

    def test_empty_and_invalid_input(self):
        for rows in [[], [{**self.rows()[0], 'prediction': {'risk_score': float('nan')}}], self.rows() + [self.rows()[0]]]:
            with self.assertRaises(ValueError):
                evaluate(rows)


if __name__ == '__main__':
    unittest.main()
