"""HTTP contract tests use temporary task storage and mocked model responses."""
import tempfile
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from main import app
from api.detect import _verify_token
from services import task_log, ollama_client

TEXT = ('The garden was quiet after the rain. Birds returned to the trees. '
        'People walked along the river and watched the clouds. ') * 4
REPLY = {'ai_score': 45, 'confidence': 0.8, 'label': 'mixed', 'signals': {
    'over_smooth': 0.4, 'template_pattern': 0.4, 'sentence_uniformity': 0.4, 'human_detail': 0.4}}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for patcher in [
            patch.object(task_log, 'TASK_LOG_DB_PATH', Path(self.directory.name) / 'tasks.db'),
            patch.object(task_log, '_INITIALIZED', False),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        app.dependency_overrides[_verify_token] = lambda: {
            'plan': 'free', 'can_remote_review': False, 'can_reduce': False,
            'max_chars': 3000, 'client_id': 'tests', 'can_view_all_tasks': False,
        }
        self.addCleanup(app.dependency_overrides.clear)

    def test_scan_contract_and_lifespan(self):
        with patch('services.judges.generate_json', AsyncMock(return_value=REPLY)):
            with TestClient(app) as client:
                self.assertIsNotNone(ollama_client._http_client)
                response = client.post('/api/scan', json={'content': TEXT})
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual(float(result['ai_probability']), result['risk_score'])
                self.assertEqual(result['score_semantics'], 'uncalibrated_risk_score')
                self.assertIsNone(result['metrics']['binoculars'])
                self.assertEqual(result['segments'], result['sentences'])
                self.assertIn('task_id', result)
                task = client.get('/api/tasks/' + result['task_id'])
                self.assertEqual(task.status_code, 200)
        self.assertIsNone(ollama_client._http_client)

    def test_provider_failure_returns_explicit_insufficient_evidence(self):
        with patch('services.judges.generate_json', AsyncMock(side_effect=RuntimeError('unavailable'))):
            with TestClient(app) as client:
                response = client.post('/api/scan', json={'content': TEXT})
                self.assertEqual(response.status_code, 200)
                result = response.json()
                self.assertEqual(result['confidence'], 'low')
                self.assertEqual(result['assessment_status'], 'insufficient_evidence')
                self.assertIn('statistics_only', result['warnings'])
                trace = client.get('/api/tasks/' + result['task_id']).json()
                scores = [event for event in trace['events'] if event['step'] == 'scan.chunk.score']
                self.assertTrue(scores)
                self.assertTrue(all(event['details']['qwen_score'] is None for event in scores))

    def test_whitespace_cannot_bypass_minimum_length(self):
        with TestClient(app) as client:
            response = client.post('/api/scan', json={'content': 'x' + ' \n' * 100 + 'y'})
            self.assertEqual(response.status_code, 400)

    def test_demo_explains_risk_score(self):
        with TestClient(app) as client:
            response = client.get('/demo')
            self.assertEqual(response.status_code, 200)
            self.assertIn('AI 风格风险分', response.text)
            self.assertIn('证据不足', response.text)

    def test_request_id_exposes_live_trace_without_replacing_another_callers_task(self):
        entered, release = threading.Event(), threading.Event()

        async def delayed_reply(**kwargs):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return REPLY

        identifier = uuid4()
        task_id = f'scan-{identifier.hex}'
        request = {'content': TEXT, 'request_id': str(identifier), 'request_source': 'demo'}
        with patch('services.judges.generate_json', delayed_reply), TestClient(app) as client:
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(client.post, '/api/scan', json=request)
                try:
                    self.assertTrue(entered.wait(3), 'The test judge was not entered')
                    trace = client.get('/api/tasks/' + task_id)
                    self.assertEqual(trace.status_code, 200)
                    self.assertEqual(trace.json()['status'], 'running')
                    self.assertIn('judge.route.qwen', [event['step'] for event in trace.json()['events']])
                    self.assertEqual(trace.json()['execution_summary']['local']['successes'], 0)
                finally:
                    release.set()
                self.assertEqual(pending.result(timeout=3).json()['task_id'], task_id)
            original = client.get('/api/tasks/' + task_id).json()
            self.assertEqual(original['status'], 'success')
            self.assertGreater(original['execution_summary']['local']['successes'], 0)
            app.dependency_overrides[_verify_token] = lambda: {
                'plan':'free', 'can_remote_review':False, 'can_reduce':False,
                'max_chars':3000, 'client_id':'another-client', 'can_view_all_tasks':False,
            }
            self.assertEqual(client.get('/api/tasks/' + task_id).status_code, 404)
            self.assertEqual(client.post('/api/scan', json=request).status_code, 409)
            self.assertEqual(task_log.get_task(task_id, client_id='tests'), original)

    def test_failed_requests_keep_their_trace_for_scan_and_reduce(self):
        app.dependency_overrides[_verify_token] = lambda: {
            'plan':'pro', 'can_remote_review':False, 'can_reduce':True,
            'max_chars':12000, 'client_id':'tests', 'can_view_all_tasks':False,
        }
        with TestClient(app) as client:
            for kind in ('scan', 'reduce'):
                for error, status in ((ValueError('invalid result'), 502), (RuntimeError('engine unavailable'), 500)):
                    with self.subTest(kind=kind, error=type(error).__name__):
                        identifier = uuid4()
                        task_id = f'{kind}-{identifier.hex}'
                        function = 'run_check' if kind == 'scan' else 'run_reduce'
                        with patch('api.detect.' + function, AsyncMock(side_effect=error)):
                            response = client.post('/api/' + kind, json={'content':TEXT, 'request_id':str(identifier)})
                        self.assertEqual(response.status_code, status)
                        self.assertEqual(response.headers['x-task-id'], task_id)
                        trace = client.get('/api/tasks/' + task_id).json()
                        self.assertEqual(trace['status'], 'failed')
                        self.assertEqual(trace['events'][-1]['status'], 'failed')
                        self.assertIsNone(trace['result'])

    def test_invalid_request_id_does_not_create_a_task(self):
        with TestClient(app) as client:
            response = client.post('/api/scan', json={'content':TEXT, 'request_id':'not-a-uuid'})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(client.get('/api/tasks').json()['items'], [])


if __name__ == '__main__':
    unittest.main()
