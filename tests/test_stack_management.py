"""Deployment safety and lifecycle tests; no real Docker/model/cloud calls."""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import manage


class StackManagementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'deploy.env.example').write_text((manage.ROOT / 'deploy.env.example').read_text())
        self.original = ('# production tokens\nAPI_TOKEN=fixture-admin\n'
                         'PRO_API_TOKENS=fixture-pro-a,fixture-pro-b\n'
                         'AZURE_API_KEY=fixture-secret\nREMOTE_REVIEW_ENABLED=true\n'
                         'OLLAMA_MODEL=qwen3:4b\nOLLAMA_TIMEOUT=600\n'
                         'PROBABILITY_ENABLED=false\n')
        (self.root / '.env').write_text(self.original)
        with contextlib.redirect_stdout(io.StringIO()):
            self.config = manage.deployment_config(self.root)
        self.limits = manage.resources(self.config, 4, 16 * manage.GIB)

    def test_four_core_profile_leaves_capacity_for_existing_business(self):
        self.assertEqual((self.limits['ollama_cpu'], self.limits['scorer_cpu']), (2, 1))
        self.assertEqual(self.limits['threads'], 1)
        self.assertLessEqual(self.limits['ollama_memory'] + self.limits['scorer_memory'], 12)
        # A nominal 16GB instance may report less than 16GiB to Docker.
        actual = manage.resources(self.config, 4, int(15.6 * manage.GIB))
        self.assertEqual((actual['ollama_memory'], actual['scorer_memory']), (5, 5))

    def test_overallocation_and_invalid_window_rejected(self):
        for overrides in ({'SCORER_CPUS': '5'}, {'SCORER_MEMORY_GB': '15'},
                          {'SCORER_STRIDE_TOKENS': '1024'}, {'SCORER_ENABLED': 'yes'},
                          {'SCORER_THREADS': '3'}, {'SCORER_MAX_TOKENS': '1'}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                manage.resources({**self.config, **overrides}, 4, 16 * manage.GIB)

    def test_up_preserves_tokens_review_settings_and_existing_timeout(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            manage.sync_app_env(self.root, self.config, self.limits)
        content = (self.root / '.env').read_text()
        for line in self.original.splitlines()[:6]:
            self.assertIn(line, content)
        self.assertIn('OLLAMA_TIMEOUT=600', content)
        self.assertIn('PROBABILITY_ENABLED=true', content)
        self.assertIn('PROBABILITY_BASE_URL=http://aigc-cpu-scorer:8091', content)
        self.assertEqual((self.root / '.env').stat().st_mode & 0o777, 0o600)
        backups = list(self.root.glob('.env.backup.*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertNotIn('fixture-secret', output.getvalue())
        manage.sync_app_env(self.root, self.config, self.limits)
        self.assertEqual(len(list(self.root.glob('.env.backup.*'))), 1)

    def test_duplicate_routing_keys_are_normalized_without_losing_other_lines(self):
        with (self.root / '.env').open('a') as stream:
            stream.write(' export PROBABILITY_ENABLED = false\nPROBABILITY_ENABLED=false\n')
        manage.sync_app_env(self.root, self.config, self.limits)
        content = (self.root / '.env').read_text()
        self.assertEqual(content.count('PROBABILITY_ENABLED='), 1)
        self.assertIn('PRO_API_TOKENS=fixture-pro-a,fixture-pro-b', content)

    def test_existing_model_and_deployment_config_are_reused(self):
        (self.root / 'deploy.env').unlink()
        (self.root / '.env').write_text('OLLAMA_MODEL="existing-model:4b"\n')
        config = manage.deployment_config(self.root)
        self.assertEqual(config['OLLAMA_DEPLOY_MODEL'], 'existing-model:4b')
        (self.root / '.env').write_text('OLLAMA_MODEL=changed:4b\n')
        self.assertEqual(manage.deployment_config(self.root)['OLLAMA_DEPLOY_MODEL'], 'existing-model:4b')

    def test_configuration_is_not_executed_as_shell(self):
        marker = self.root / 'should-not-exist'
        (self.root / 'deploy.env').write_text(f'OLLAMA_DEPLOY_MODEL="$(touch {marker})"\n')
        config = manage.deployment_config(self.root)
        with self.assertRaises(ValueError):
            manage.resources(config, 4, 16 * manage.GIB)
        self.assertFalse(marker.exists())

    def test_scorer_failure_restores_existing_container_without_deleting_models(self):
        calls = []
        old = {'State': {'Running': True}}
        with patch.object(manage, 'inspect', return_value=old), \
             patch.object(manage, 'docker', side_effect=lambda *args, **kw: calls.append(args)), \
             patch.object(manage, 'wait_scorer', side_effect=RuntimeError('fixture load failure')):
            with self.assertRaises(RuntimeError):
                manage.deploy_scorer(self.root, self.config, self.limits)
        backup = next(args[2] for args in calls if args[0] == 'rename')
        self.assertIn(('rename', backup, 'aigc-cpu-scorer'), calls)
        self.assertIn(('start', 'aigc-cpu-scorer'), calls)
        creation = next(args for args in calls if args[0] == 'run')
        self.assertIn(f'{self.root / "models"}:/models:ro', creation)
        self.assertIn('127.0.0.1:8091:8091', creation)
        self.assertFalse(any('volume' in args or '-v' in args and args[0] == 'rm' for args in calls))

    def test_successful_scorer_replacement_cleans_only_stopped_backup(self):
        calls = []
        with patch.object(manage, 'inspect', return_value={'State': {'Running': True}}), \
             patch.object(manage, 'docker', side_effect=lambda *args, **kw: calls.append(args)), \
             patch.object(manage, 'wait_scorer'):
            manage.deploy_scorer(self.root, self.config, self.limits)
        backup = next(args[2] for args in calls if args[0] == 'rename')
        self.assertEqual(calls[-1], ('rm', backup))

    def exercise_up(self, config=None, fail_build=False):
        events = []
        config = config or self.config
        limits = manage.resources(config, 4, 16 * manage.GIB)

        def docker(*args, **kwargs):
            events.append(('docker', args))
            if fail_build and args[0] == 'build':
                raise RuntimeError('fixture build failure')
            return subprocess.CompletedProcess(args, 0, '')

        def script(args, **kwargs):
            events.append(('script', args, kwargs['env']))

        with patch.object(manage, 'download_model', side_effect=lambda *a: events.append(('download',))), \
             patch.object(manage, 'docker', side_effect=docker), \
             patch.object(manage, 'run', side_effect=script), \
             patch.object(manage, 'inspect', return_value={'State': {'Running': True}}), \
             patch.object(manage, 'deploy_scorer', side_effect=lambda *a: events.append(('scorer',))), \
             patch.object(manage, 'ensure_network', side_effect=lambda *a: events.append(('network',))), \
             patch.object(manage, 'wait_scorer', side_effect=lambda *a: events.append(('ready',))), \
             patch.object(manage, 'verify_stack', side_effect=lambda: events.append(('verify',))):
            if fail_build:
                with self.assertRaises(RuntimeError):
                    manage.up(self.root, config, limits, 'all')
            else:
                manage.up(self.root, config, limits, 'all')
        return events

    def test_failed_build_keeps_running_services_and_original_env(self):
        events = self.exercise_up(fail_build=True)
        self.assertFalse(any(event[0] in {'script', 'scorer'} for event in events))
        self.assertEqual((self.root / '.env').read_text(), self.original)

    def test_all_images_build_before_model_replacement_and_api_starts_last(self):
        events = self.exercise_up()
        build_indexes = [i for i, event in enumerate(events) if event[0] == 'docker' and event[1][0] == 'build']
        first_script = next(i for i, event in enumerate(events) if event[0] == 'script')
        self.assertLess(max(build_indexes), first_script)
        scripts = [event for event in events if event[0] == 'script']
        self.assertEqual(scripts[0][2]['OLLAMA_DEPLOY_SKIP_PULL_IF_PRESENT'], 'true')
        self.assertEqual(scripts[-1][1], ['bash', 'disploy.sh'])
        self.assertEqual(scripts[-1][2]['SKIP_GIT_PULL'], '1')
        self.assertEqual(scripts[-1][2]['SKIP_DOCKER_BUILD'], '1')
        self.assertLess(events.index(('scorer',)), next(i for i, event in enumerate(events) if event == scripts[-1]))
        self.assertEqual(events[-1], ('verify',))

    def test_disable_updates_api_before_stopping_scorer_and_does_not_download(self):
        events = self.exercise_up({**self.config, 'SCORER_ENABLED': 'false'})
        self.assertNotIn(('download',), events)
        self.assertNotIn(('scorer',), events)
        stop = ('docker', ('stop', 'aigc-cpu-scorer'))
        self.assertGreater(events.index(stop), events.index(('verify',)))
        self.assertIn('PROBABILITY_ENABLED=false', (self.root / '.env').read_text())

    def test_health_check_runs_inside_api_and_generated_python_is_valid(self):
        for score in (False, True):
            with patch.object(manage, 'docker') as docker:
                manage.verify_stack(score=score)
            args = docker.call_args.args
            self.assertEqual(args[:4], ('exec', 'aigc-checker-server', 'python', '-c'))
            compile(args[4], '<container-health-check>', 'exec')
            self.assertIn('SCORER_ENABLED', args[4])
            self.assertNotIn('/api/scan', args[4])

    def test_bailian_mode_does_not_start_ollama_and_stops_it_after_api_verification(self):
        with (self.root / '.env').open('a') as stream:
            stream.write('DETECTION_PROVIDER=bailian\nBAILIAN_API_KEY=fixture-bailian\n'
                         'PRO_REVIEW_SOURCE=openrouter\nPRO_REVIEW_MODEL=qwen/qwen-plus\n'
                         'AZURE_DEPLOYMENT=azure-existing-deployment\n')
        events = self.exercise_up({**self.config, 'DETECTION_PROVIDER': 'bailian'})
        scripts = [event[1] for event in events if event[0] == 'script']
        self.assertEqual(scripts, [['bash', 'disploy.sh']])
        self.assertGreater(events.index(('docker', ('stop', 'aigc-ollama'))), events.index(('verify',)))
        content = (self.root / '.env').read_text()
        self.assertIn('BAILIAN_API_KEY=fixture-bailian', content)
        self.assertIn('DETECTION_PROVIDER=bailian', content)
        self.assertIn('PRO_REVIEW_SOURCE=azure', content)
        self.assertIn('AZURE_REVIEW_DEPLOYMENT=azure-existing-deployment', content)
        self.assertNotIn('PRO_REVIEW_MODEL=qwen/qwen-plus', content)

    def test_bailian_without_scorer_runs_only_business_container(self):
        with (self.root / '.env').open('a') as stream:
            stream.write('BAILIAN_API_KEY=fixture-bailian\n')
        events = self.exercise_up({**self.config, 'DETECTION_PROVIDER': 'bailian', 'SCORER_ENABLED': 'false'})
        self.assertNotIn(('download',), events)
        self.assertNotIn(('scorer',), events)
        self.assertEqual([event[1] for event in events if event[0] == 'script'], [['bash', 'disploy.sh']])

    def test_bailian_missing_key_fails_before_build_or_service_mutation(self):
        config = {**self.config, 'DETECTION_PROVIDER': 'bailian'}
        limits = manage.resources(config, 4, 16 * manage.GIB)
        with patch.object(manage, 'docker') as docker, self.assertRaisesRegex(ValueError, 'BAILIAN_API_KEY'):
            manage.up(self.root, config, limits, 'all')
        docker.assert_not_called()

    def test_bailian_provider_is_read_from_business_env_and_has_no_local_model_quota(self):
        with (self.root / '.env').open('a') as stream:
            stream.write('DETECTION_PROVIDER=bailian\n')
        config = manage.deployment_config(self.root)
        limits = manage.resources(config, 4, 16 * manage.GIB)
        self.assertFalse(limits['ollama_enabled'])
        self.assertEqual(limits['ollama_memory'], 0)
        self.assertEqual(limits['scorer_cpu'], 2)

    def test_php_azure_deployment_alias_is_used_without_changing_credentials(self):
        with (self.root / '.env').open('a') as stream:
            stream.write('AZURE_OPENAI_DEPLOYMENT=gpt-5.6-sol\nAZURE_OPENAI_API_KEY=fixture-php-key\n')
        manage.sync_app_env(self.root, self.config, self.limits)
        content = (self.root / '.env').read_text()
        self.assertIn('AZURE_REVIEW_DEPLOYMENT=gpt-5.6-sol', content)
        self.assertIn('AZURE_API_MODE=v1', content)
        self.assertIn('AZURE_OPENAI_API_KEY=fixture-php-key', content)

    def test_explicit_gpt6_review_target_and_legacy_mode_are_preserved(self):
        with (self.root / '.env').open('a') as stream:
            stream.write('AZURE_OPENAI_DEPLOYMENT=gpt-5.6-sol\n'
                         'AZURE_REVIEW_DEPLOYMENT=custom-gpt6-sol\n'
                         'AZURE_REVIEW_REASONING_EFFORT=low\nAZURE_API_MODE=legacy\n')
        manage.sync_app_env(self.root, self.config, self.limits)
        content = (self.root / '.env').read_text()
        self.assertIn('AZURE_REVIEW_DEPLOYMENT=custom-gpt6-sol', content)
        self.assertIn('AZURE_REVIEW_REASONING_EFFORT=low', content)
        self.assertIn('AZURE_API_MODE=legacy', content)


if __name__ == '__main__':
    unittest.main()
