"""Task history pagination, return timestamps and caller isolation."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from api import detect
from api.contracts import TaskListResponse, TaskDetailResponse
from main import app
from services import task_log


class TaskHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for patcher in (
            patch.object(task_log, 'TASK_LOG_DB_PATH', Path(directory.name) / 'history.db'),
            patch.object(task_log, '_INITIALIZED', False),
            patch.object(task_log, 'TASK_LOG_MAX_RECORDS', 300),
            patch.object(detect, 'TASK_LOG_MAX_RECORDS', 300),
            patch.object(detect, '_API_TOKEN', 'history-admin'),
            patch.object(detect, '_FREE_API_TOKENS', {'history-a', 'history-b'}),
            patch.object(detect, '_PRO_API_TOKENS', set()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app)

    def headers(self, token='history-admin'):
        return {'Authorization': 'Bearer ' + token}

    def seed(self, identifier, token='history-a', source='api', kind='scan', status='success'):
        auth = detect._token_context(token)
        task_log.start_task(identifier, auth['client_id'], kind, source, auth['plan'], 'ollama', None, 100)
        task_log.record_task_event(identifier, f'api.{kind}.accept', details={'source': source})
        if status == 'success':
            task_log.finish_task(identifier, {'ai_probability': '42.00', 'label': 'Mixed', 'confidence': 'high'})
        elif status == 'failed':
            task_log.record_task_event(identifier, f'api.{kind}.error', status='failed', details={'error_type': 'RuntimeError'})
            task_log.fail_task(identifier, 'engine_error')

    def test_admin_lists_all_sources_and_other_callers_with_return_times(self):
        self.seed('scan-demo', source='demo')
        self.seed('scan-test', source='test', status='failed')
        self.seed('scan-external', token='history-b', status='running')
        response = self.client.get('/api/tasks', headers=self.headers())
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        TaskListResponse.model_validate(payload)
        self.assertEqual(payload['total'], 3)
        self.assertTrue(payload['can_view_all_tasks'])
        self.assertEqual(payload['retention_limit'], 300)
        tasks = {item['task_id']: item for item in payload['items']}
        self.assertIsNone(tasks['scan-external']['returned_at'])
        self.assertIsNotNone(tasks['scan-test']['returned_at'])
        self.assertEqual(tasks['scan-demo']['returned_at'], tasks['scan-demo']['updated_at'])
        self.assertTrue(all('client_id' not in task for task in tasks.values()))
        detail = self.client.get('/api/tasks/scan-test', headers=self.headers()).json()
        self.assertEqual(detail['events'][-1]['status'], 'failed')
        self.assertEqual(detail['error_code'], 'engine_error')

    def test_filters_search_and_total_apply_before_pagination(self):
        for identifier, source, kind, status in (
            ('scan-api-success', 'api', 'scan', 'success'),
            ('scan-api-failed', 'api', 'scan', 'failed'),
            ('reduce-api-success', 'api', 'reduce', 'success'),
            ('scan-demo-success', 'demo', 'scan', 'success'),
        ):
            self.seed(identifier, source=source, kind=kind, status=status)
        response = self.client.get('/api/tasks?source=api&task_type=scan&status=success&search=API&limit=1', headers=self.headers())
        payload = response.json()
        self.assertEqual(payload['total'], 1)
        self.assertEqual([task['task_id'] for task in payload['items']], ['scan-api-success'])
        self.assertEqual(self.client.get('/api/tasks?search=%25', headers=self.headers()).json()['total'], 0)

    def test_non_admin_cannot_expand_list_or_detail_scope(self):
        self.seed('scan-mine')
        self.seed('scan-other', token='history-b')
        for query in ('', '?source=api', '?search=other', '?offset=1'):
            payload = self.client.get('/api/tasks' + query, headers=self.headers('history-a')).json()
            self.assertFalse(payload['can_view_all_tasks'])
            self.assertTrue(all(task['task_id'] == 'scan-mine' for task in payload['items']))
        self.assertEqual(self.client.get('/api/tasks/scan-other', headers=self.headers('history-a')).status_code, 404)
        self.assertEqual(self.client.get('/api/tasks', headers=self.headers('invalid')).status_code, 401)

    def test_pagination_reaches_history_beyond_first_200_tasks(self):
        for index in range(205):
            self.seed(f'scan-{index:03d}')
        first = self.client.get('/api/tasks?limit=200', headers=self.headers()).json()
        second = self.client.get('/api/tasks?limit=200&offset=200', headers=self.headers()).json()
        self.assertEqual(first['total'], 205)
        self.assertEqual(second['total'], 205)
        identifiers = [task['task_id'] for task in first['items'] + second['items']]
        self.assertEqual(len(identifiers), 205)
        self.assertEqual(len(set(identifiers)), 205)

    def test_return_time_records_server_completion_for_success_and_failure(self):
        started = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
        for status in ('success', 'failed'):
            with patch.object(task_log, '_now', return_value=started):
                self.seed('scan-' + status, status='running')
            with patch.object(task_log, '_now', return_value=started + timedelta(milliseconds=1500)):
                if status == 'success':
                    task_log.finish_task('scan-' + status, {'label': 'Mixed'})
                else:
                    task_log.fail_task('scan-' + status, 'engine_error')
            item = task_log.get_task('scan-' + status, client_id=None)
            self.assertEqual(item['returned_at'], '2026-10-17T10:00:01.500Z')
            self.assertEqual(item['duration_ms'], 1500)

    def test_invalid_filters_and_offsets_are_rejected(self):
        for query in ('offset=-1', 'source=unknown', 'status=done', 'task_type=other', 'limit=201'):
            self.assertEqual(self.client.get('/api/tasks?' + query, headers=self.headers()).status_code, 422)

    def test_reduce_history_keeps_compact_result_and_evidence_status(self):
        result = {'rewrite': False, 'ai_probability': '42.00', 'ai_reduced_probability': '42.00',
                  'quality_score': 60, 'reduced': 'private rewritten content',
                  'detection_status_before': 'insufficient_evidence', 'detection_status_after': 'assessed'}
        with patch('api.detect.run_reduce', AsyncMock(return_value=result)):
            response = self.client.post('/api/reduce', headers=self.headers(), json={'content': 'sample content. ' * 10})
        self.assertEqual(response.status_code, 200)
        item = self.client.get('/api/tasks', headers=self.headers()).json()['items'][0]
        self.assertEqual(item['result']['detection_status_before'], 'insufficient_evidence')
        self.assertNotIn('reduced', item['result'])
        self.assertIsNotNone(item['returned_at'])

    def test_polling_keeps_same_envelope_for_accepted_running_success_and_failure(self):
        self.seed('scan-live', status='running')
        def poll():
            response = self.client.get('/api/tasks/scan-live', headers=self.headers('history-a'))
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            TaskDetailResponse.model_validate(payload)
            return payload

        accepted = poll()
        self.assertEqual(accepted['result']['stage'], 'accepted')
        self.assertEqual(accepted['result']['progress'], 0)
        task_log.record_task_event('scan-live', 'scan.chunking.finish', details={'chunks': 2})
        task_log.record_task_event('scan-live', 'scan.chunk.score', details={'chunk_id': 1, 'text': 'private input'})
        running = poll()
        self.assertEqual(running['result']['stage'], 'detecting')
        self.assertEqual(running['result']['completed_chunks'], 1)
        self.assertNotIn('private input', str(running))
        self.assertEqual(poll(), running)
        task_log.finish_task('scan-live', {'label': 'Mixed', 'ai_probability': '42.00'})
        success = poll()
        self.assertEqual(success['result']['progress'], 100)
        self.assertEqual(success['result']['ai_probability'], '42.00')
        # The list endpoint still returns compact result summaries.
        listing = self.client.get('/api/tasks', headers=self.headers('history-a')).json()['items'][0]
        self.assertEqual(listing['result'], {'label': 'Mixed', 'ai_probability': '42.00'})
        self.seed('scan-failed', status='failed')
        failed = self.client.get('/api/tasks/scan-failed', headers=self.headers('history-a')).json()
        self.assertEqual(failed['result']['stage'], 'failed')
        self.assertTrue(failed['result']['message'])
        for payload in (running, success, failed):
            self.assertEqual(set(payload), set(accepted))


if __name__ == '__main__':
    unittest.main()
