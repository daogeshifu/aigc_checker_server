"""Deterministic worker scheduling tests; no real model or Docker calls."""
import asyncio
import copy
import os
import unittest
from unittest.mock import AsyncMock, patch

from services import checker, model_client, ollama_client, bailian_client
from services.concurrency import bounded_concurrency

TEXT = 'The garden was quiet after the rain. Birds returned to the trees. People walked along the river and watched the clouds. ' * 3
JUDGE = {'ai_score': 45, 'confidence': .8, 'label': 'mixed', 'judge_mode': 'qwen', 'signals': {
    'over_smooth': .4, 'template_pattern': .4, 'sentence_uniformity': .4, 'human_detail': .6}}


class ConcurrencyConfigTests(unittest.TestCase):
    def test_default_and_hard_ceiling(self):
        for value, expected in ((None, 10), ('', 10), ('3', 3), ('10', 10), ('100', 10), ('0', 1), (0, 1), ('-1', 1)):
            with self.subTest(value=value):
                self.assertEqual(bounded_concurrency(value), expected)

    def test_actual_provider_selects_shared_capacity(self):
        with patch.object(ollama_client, 'OLLAMA_MAX_CONCURRENCY', 10), \
             patch.object(bailian_client, 'BAILIAN_MAX_CONCURRENCY', 4):
            for provider, expected in (('ollama', 10), ('bailian', 4)):
                with patch.object(model_client, 'DETECTION_PROVIDER', provider):
                    self.assertEqual(model_client.request_concurrency_limit(), expected)


class ChunkWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def run_scan(self, count, judge, configured=None, provider_limit=10):
        chunks = [{'chunk_id': index, 'text': TEXT, 'start': index * len(TEXT), 'end': (index + 1) * len(TEXT)} for index in range(count)]
        with patch.dict(os.environ), patch.object(checker, 'chunk_text', return_value=chunks), \
             patch.object(checker, 'judge_chunk_with_qwen', side_effect=judge), \
             patch.object(checker, 'request_concurrency_limit', return_value=provider_limit), \
             patch.object(checker, 'measure_probability', AsyncMock(return_value={'status': 'disabled'})):
            os.environ.pop('SCAN_CHUNK_CONCURRENCY', None)
            if configured is not None:
                os.environ['SCAN_CHUNK_CONCURRENCY'] = configured
            return await checker.run_check(TEXT * count, can_remote_review=False)

    async def measure_capacity(self, count, expected, configured=None, provider_limit=10):
        active = maximum = 0
        ready, release = asyncio.Event(), asyncio.Event()

        async def judge(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == expected:
                ready.set()
            try:
                await release.wait()
                await asyncio.sleep(0)
                return copy.deepcopy(JUDGE)
            finally:
                active -= 1

        task = asyncio.create_task(self.run_scan(count, judge, configured, provider_limit))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            self.assertEqual(active, expected)
        finally:
            release.set()
            result = await asyncio.wait_for(task, 2)
        self.assertEqual(maximum, expected)
        self.assertEqual([chunk['chunk_id'] for chunk in result['chunks']], list(range(count)))
        return result

    async def test_dynamic_parallelism_for_one_three_ten_and_twenty_three_chunks(self):
        for count in (1, 3, 10, 23):
            with self.subTest(count=count):
                await self.measure_capacity(count, min(count, 10))

    async def test_configuration_cannot_exceed_ten(self):
        await self.measure_capacity(23, 10, configured='99')

    async def test_lower_application_and_provider_limits_are_respected(self):
        await self.measure_capacity(8, 3, configured='3')
        await self.measure_capacity(8, 2, provider_limit=2)

    async def test_next_chunk_starts_before_slowest_chunk_finishes(self):
        release_slow, third_started = asyncio.Event(), asyncio.Event()
        completed = []

        async def judge(*args, chunk_id, **kwargs):
            if chunk_id == 0:
                await release_slow.wait()
            if chunk_id == 2:
                third_started.set()
            completed.append(chunk_id)
            return copy.deepcopy(JUDGE)

        task = asyncio.create_task(self.run_scan(3, judge, configured='2'))
        try:
            await asyncio.wait_for(third_started.wait(), 2)
            self.assertNotIn(0, completed)
            self.assertFalse(task.done())
        finally:
            release_slow.set()
            result = await asyncio.wait_for(task, 2)
        self.assertEqual([chunk['chunk_id'] for chunk in result['chunks']], [0, 1, 2])
        self.assertEqual(completed, [1, 2, 0])

    async def test_worker_failure_cancels_and_waits_for_other_workers(self):
        sibling_started, sibling_stopped = asyncio.Event(), asyncio.Event()

        async def judge(*args, chunk_id, **kwargs):
            if chunk_id == 0:
                sibling_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    sibling_stopped.set()
            await sibling_started.wait()
            raise RuntimeError('fixture processing failure')

        with self.assertRaisesRegex(RuntimeError, 'fixture processing failure'):
            await asyncio.wait_for(self.run_scan(2, judge), 2)
        self.assertTrue(sibling_stopped.is_set())

    async def test_cancelling_document_stops_all_its_workers(self):
        started, stopped, active = asyncio.Event(), [], 0

        async def judge(*args, chunk_id, **kwargs):
            nonlocal active
            active += 1
            if active == 3:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(chunk_id)

        task = asyncio.create_task(self.run_scan(3, judge))
        try:
            await asyncio.wait_for(started.wait(), 2)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(sorted(stopped), [0, 1, 2])

    async def test_chunk_log_reports_selected_parallelism(self):
        with patch.object(checker, '_log_task') as log:
            await self.run_scan(12, AsyncMock(return_value=JUDGE))
        entry = next(call for call in log.call_args_list if call.args[1] == 'scan.chunking.finish')
        self.assertEqual(entry.kwargs['chunks'], 12)
        self.assertEqual(entry.kwargs['parallelism'], 10)
        self.assertEqual(entry.kwargs['concurrency_limit'], 10)


if __name__ == '__main__':
    unittest.main()
