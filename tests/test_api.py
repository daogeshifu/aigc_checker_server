"""HTTP contract tests use temporary task storage and mocked model responses."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

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


if __name__ == '__main__':
    unittest.main()
