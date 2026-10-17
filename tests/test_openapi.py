"""OpenAPI meaning and documented payload parity; all model calls are mocked."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app
from api import detect
from api.contracts import ScanResponse, ReduceResponse, TaskListResponse, TaskDetailResponse, ProbabilityMeasured
from services import task_log


TEXT = ('今天下午我整理了上周的工作记录，先核对客户反馈，再把遗漏的信息补进表格。'
        '同事发来修改意见后，我发现两处数字需要重新确认，因此决定明天核实后再发给客户。') * 4
JUDGE = {'ai_score': 45, 'confidence': 0.8, 'label': 'mixed', 'signals': {
    'over_smooth': 0.4, 'template_pattern': 0.4, 'sentence_uniformity': 0.4, 'human_detail': 0.6}}


class OpenApiTests(unittest.TestCase):
    def test_schema_documents_actual_provider_and_unimplemented_metrics(self):
        schema = app.openapi()
        description = schema['info']['description']
        self.assertIn('DETECTION_PROVIDER', description)
        self.assertIn(detect.DETECTION_PROVIDER, description)
        self.assertIn('不是 AI 来源概率', description)
        self.assertIn('Binoculars 尚未实现', description)
        self.assertEqual(schema['info']['version'], '2.1.0')
        self.assertEqual(set(schema['paths']), {'/api/scan', '/api/reduce', '/api/tasks', '/api/tasks/{task_id}'})
        self.assertIn('bailian', schema['components']['schemas']['DetectionEngine']['properties']['scan_api_source']['enum'])
        for path in ['/api/scan', '/api/reduce']:
            operation = schema['paths'][path]['post']
            self.assertIn('$ref', operation['responses']['200']['content']['application/json']['schema'])
            self.assertTrue({'400', '401', '409', '422', '500', '502'} <= set(operation['responses']))
            self.assertIn('X-Task-ID', operation['responses']['500']['headers'])
        self.assertIn('403', schema['paths']['/api/reduce']['post']['responses'])
        self.assertIn('404', schema['paths']['/api/tasks/{task_id}']['get']['responses'])
        progress = schema['components']['schemas']['TaskProgressResult']
        self.assertTrue({'stage', 'progress', 'message', 'scan_phase', 'completed_chunks', 'total_chunks'}
                        <= set(progress['required']))
        self.assertEqual(schema['components']['schemas']['TaskDetailResponse']['properties']['result']['$ref'],
                         '#/components/schemas/TaskProgressResult')

    def test_request_examples_do_not_pin_a_model_or_reuse_a_request_uuid(self):
        schema = app.openapi()['components']['schemas']['ScanRequest']
        example = schema['examples'][0]
        self.assertGreaterEqual(len(example['content']), 50)
        self.assertNotIn('request_id', example)
        self.assertNotIn('model', example)
        self.assertIn('不能切换', schema['properties']['api_source']['description'])
        self.assertIn('不是实际概率', app.openapi()['components']['schemas']['ScanResponse']['properties']['ai_probability']['description'])

    def test_documentation_models_do_not_filter_or_validate_runtime_responses(self):
        for route in detect.router.routes:
            self.assertIsNone(route.response_model)

    def test_measured_probability_documentation_preserves_metadata_contract(self):
        data = dict(method='causal_token_likelihood_v1', model='fixture.gguf', model_sha256='0' * 64,
                    runtime='fixture', context_tokens=8, stride_tokens=4, token_count=4, scored_tokens=3,
                    first_token_policy='tokenizer_add_bos_then_skip_index_0', nll=2, perplexity=math.exp(2),
                    nll_std=0.2, elapsed_seconds=0.01, status='measured', used_in_risk_score=False)
        self.assertEqual(ProbabilityMeasured.model_validate(data).perplexity, math.exp(2))

    def test_real_mocked_scan_reduce_and_task_payloads_match_documented_fields(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(task_log, 'TASK_LOG_DB_PATH', Path(directory) / 'tasks.db'), \
             patch.object(task_log, '_INITIALIZED', False), \
             patch('services.judges.generate_json', AsyncMock(side_effect=lambda **kw: copy.deepcopy(JUDGE))), \
             patch('services.checker.measure_probability', AsyncMock(return_value={'status': 'disabled', 'used_in_risk_score': False})), \
             patch('services.checker._rewrite_content', AsyncMock(return_value={
                 'reduced': TEXT + '等数字确认后再整理一遍。', 'rewrite': True,
                 'quality_score': 60, 'model': 'moderate', 'changes': []})):
            previous = app.dependency_overrides.copy()
            app.dependency_overrides[detect._verify_token] = lambda: {
                'plan': 'pro', 'can_reduce': True, 'can_remote_review': False,
                'max_chars': 12000, 'client_id': 'openapi-fixture', 'can_view_all_tasks': False}
            try:
                with TestClient(app) as client:
                    scan = client.post('/api/scan', json={'content': TEXT})
                    self.assertEqual(scan.status_code, 200, scan.text)
                    ScanResponse.model_validate(scan.json())
                    trace = client.get('/api/tasks/' + scan.json()['task_id'])
                    TaskDetailResponse.model_validate(trace.json())
                    TaskListResponse.model_validate(client.get('/api/tasks').json())
                    rewritten = client.post('/api/reduce', json={'content': TEXT})
                    self.assertEqual(rewritten.status_code, 200, rewritten.text)
                    ReduceResponse.model_validate(rewritten.json())
                    self.assertIn('changes', rewritten.json())
            finally:
                app.dependency_overrides.clear()
                app.dependency_overrides.update(previous)


if __name__ == '__main__':
    unittest.main()
