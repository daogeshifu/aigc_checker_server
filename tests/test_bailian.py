"""Bailian compatibility, provider isolation, Azure-only review. No live API calls."""
import asyncio
from contextlib import ExitStack
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from services import bailian_client as client, model_client, checker, judges, task_log, ollama_client
from services.features import extract_document_features


REPLY = {"ai_score": 45, "confidence": 0.8, "label": "mixed", "signals": {
    "over_smooth": 0.5, "template_pattern": 0.4, "sentence_uniformity": 0.5, "human_detail": 0.6}}
TEXT = ("今天下午我在办公室整理了上周的工作记录，先核对客户反馈，再把遗漏的信息补进表格。"
        "同事下班前发来了修改意见，有两处数字需要再确认，所以这份材料还没有发给客户。") * 4


class BailianTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def config(self, http):
        stack = ExitStack()
        stack.enter_context(patch.object(client, '_http_client', http))
        stack.enter_context(patch.object(client, 'BAILIAN_API_KEY', 'fixture-key'))
        stack.enter_context(patch.object(client, 'BAILIAN_BASE_URL', 'https://example.invalid/compatible-mode/v1'))
        stack.enter_context(patch.object(client, '_request_gate', asyncio.Semaphore(2)))
        return stack

    async def test_json_fields_thinking_and_auth_match_contract(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(REPLY)}, 'finish_reason': 'stop'}]})
        async with self.transport(handler) as http:
            with self.config(http):
                result = await client.generate_json('system', 'user', judges._QWEN_JUDGE_SCHEMA)
        self.assertEqual(judges._normalize_qwen_result(result), REPLY)
        payload = json.loads(requests[0].content)
        self.assertFalse(payload['enable_thinking'])
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertIn('JSON Schema', payload['messages'][0]['content'])
        self.assertEqual(requests[0].headers['authorization'], 'Bearer fixture-key')
        self.assertTrue(str(requests[0].url).endswith('/compatible-mode/v1/chat/completions'))

    async def test_truncated_json_retry_increases_budget(self):
        payloads = []
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'message': {'content': '{' if len(payloads) == 1 else '{}'},
                                                         'finish_reason': 'length' if len(payloads) == 1 else 'stop'}]})
        async with self.transport(handler) as http:
            with self.config(http):
                self.assertEqual(await client.generate_json('system', 'user', {}), {})
        self.assertEqual(payloads[1]['max_tokens'], payloads[0]['max_tokens'] * 2)

    async def test_auth_error_not_retried_and_provider_body_not_exposed(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(401, json={'error': {'message': 'sensitive-provider-body'}})
        async with self.transport(handler) as http:
            with self.config(http), self.assertRaisesRegex(RuntimeError, 'HTTP 401') as raised:
                await client.generate_json('system', 'user', {})
        self.assertNotIn('sensitive-provider-body', str(raised.exception))
        self.assertEqual(len(requests), 1)

    async def test_empty_thinking_only_output_is_rejected(self):
        async with self.transport(lambda r: httpx.Response(200, json={
                'choices': [{'message': {'content': '', 'reasoning_content': 'not final'}, 'finish_reason': 'stop'}]})) as http:
            with self.config(http), self.assertRaises(ValueError):
                await client.generate_json('system', 'user', {})

    async def test_shared_concurrency_gate(self):
        active = maximum = 0
        async def handler(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.001)
            active -= 1
            return httpx.Response(200, json={'choices': [{'message': {'content': '改写文本'}, 'finish_reason': 'stop'}]})
        async with self.transport(handler) as http:
            with self.config(http):
                await asyncio.gather(*(client.generate_text('system', 'user') for _ in range(6)))
        self.assertEqual(maximum, 2)

    async def test_bailian_scan_and_expression_rewrite_never_call_ollama(self):
        payloads = []
        def handler(request):
            payload = json.loads(request.content)
            payloads.append(payload)
            content = json.dumps(REPLY) if 'response_format' in payload else '表达已调整。原有事实予以保留。'
            return httpx.Response(200, json={'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]})
        async with self.transport(handler) as http:
            with self.config(http), patch.object(model_client, 'DETECTION_PROVIDER', 'bailian'), \
                 patch.object(judges, 'DETECTION_PROVIDER', 'bailian'), patch.object(checker, 'SCAN_API_SOURCE', 'bailian'), \
                 patch.object(ollama_client, 'generate_json', AsyncMock()) as local_json, \
                 patch.object(ollama_client, 'generate_text', AsyncMock()) as local_text, \
                 patch.object(checker, 'measure_probability', AsyncMock(return_value={'status': 'disabled'})):
                result = await checker.run_check(TEXT, api_source='bailian')
                rewritten = await checker._perturb_expression_step(TEXT, TEXT, result, None, 'bailian')
        self.assertEqual(result['engine']['scan_api_source'], 'bailian')
        self.assertTrue(any(c['judge_mode'] == 'qwen' for c in result['chunks']))
        self.assertTrue(all(c['judge_provider'] == 'bailian' for c in result['chunks']))
        self.assertTrue(rewritten['applied'])
        self.assertEqual(rewritten['provider'], 'bailian')
        self.assertTrue(any('response_format' not in p and p['max_tokens'] == checker.OLLAMA_REWRITE_NUM_PREDICT for p in payloads))
        local_json.assert_not_awaited()
        local_text.assert_not_awaited()

    async def test_invalid_judgment_falls_back_to_same_statistics(self):
        with patch.object(judges, 'DETECTION_PROVIDER', 'bailian'), \
             patch.object(judges, 'generate_json', AsyncMock(return_value={'ai_score': 80})):
            result = await judges.judge_chunk_with_qwen(TEXT, 'general', extract_document_features(TEXT))
        self.assertEqual(result['judge_mode'], 'heuristic_fallback')

    async def test_review_uses_azure_only_even_with_legacy_source(self):
        answer = {'ai_score': 40, 'confidence': 0.8, 'label': 'mixed'}
        with patch.object(checker, 'PRO_REVIEW_SOURCE', 'openrouter'), \
             patch.object(checker, '_call_azure_json', AsyncMock(return_value=answer)) as azure, \
             patch.object(checker, '_call_openrouter_json', AsyncMock()) as router:
            result = await checker._run_remote_review(TEXT, {'chunks': []})
        self.assertEqual(result, answer)
        azure.assert_awaited_once()
        self.assertEqual(azure.await_args.kwargs['purpose'], 'remote_review')
        router.assert_not_awaited()

    async def test_azure_review_failure_does_not_fallback_to_openrouter(self):
        with patch.object(checker, '_call_azure_json', AsyncMock(side_effect=RuntimeError('offline'))), \
             patch.object(checker, '_call_openrouter_json', AsyncMock()) as router:
            self.assertIsNone(await checker._run_remote_review(TEXT, {'chunks': []}))
        router.assert_not_awaited()


class BailianTraceTests(unittest.TestCase):
    def test_demo_displays_server_selected_provider_without_exposing_key(self):
        import main
        from fastapi.testclient import TestClient
        with patch.object(main, 'DETECTION_PROVIDER', 'bailian'), \
             patch.object(main, 'default_model', return_value='qwen-plus'):
            response = TestClient(main.app).get('/demo')
        self.assertEqual(response.status_code, 200)
        self.assertIn("apiSource: 'bailian'", response.text)
        self.assertIn('<select id="apiSource" disabled>', response.text)
        self.assertNotIn('__BAILIAN_DEFAULT_MODEL__', response.text)
        self.assertNotIn('BAILIAN_API_KEY=', response.text)

    def test_summary_identifies_cloud_judge_without_mislabeling_ollama(self):
        events = [
            {'step': 'scan.start', 'details': {'scan_source': 'bailian', 'scan_model': 'qwen-plus'}},
            {'step': 'judge.route.qwen', 'details': {'provider': 'bailian', 'model': 'qwen-plus'}},
            {'step': 'judge.qwen.finish', 'details': {'provider': 'bailian', 'model': 'qwen-plus'}},
            {'step': 'provider.azure.call', 'details': {'purpose': 'remote_review', 'deployment': 'azure-deployment'}},
            {'step': 'provider.azure.success', 'details': {'purpose': 'remote_review', 'deployment': 'azure-deployment'}},
        ]
        result = task_log._execution_summary(events)
        self.assertEqual(result['local']['provider'], 'bailian')
        self.assertEqual(result['local']['successes'], 1)
        self.assertIn('百炼', result['local']['display_name'])
        self.assertEqual(result['third_party'][0]['provider'], 'azure')
        self.assertNotIn('OpenRouter', result['detection_modes'][-1]['description'])
