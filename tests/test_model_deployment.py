"""Exercise deployment lifecycle/network recovery using a fake Docker executable."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAKE_DOCKER = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ['FAKE_DOCKER_STATE'])
with (state / 'commands.jsonl').open('a') as out:
    out.write(json.dumps(args) + '\n')
if args[:2] == ['compose', 'version']:
    raise SystemExit(0 if os.environ.get('FAKE_COMPOSE', '1') == '1' else 1)
if args[:1] == ['info']:
    if '--format' in args:
        print(os.environ.get('FAKE_CPUS', '8'), os.environ.get('FAKE_MEMORY_BYTES', str(32 * 1024**3)))
elif args[:2] == ['network', 'inspect']:
    if '--format' in args and (state / 'connected').exists():
        print('aigc-checker-server')
elif args[:2] == ['network', 'connect']:
    (state / 'connected').touch()
elif args[:2] == ['container', 'inspect'] or args[:1] == ['inspect']:
    if 'aigc-ollama' in args:
        raise SystemExit(0 if os.environ.get('FAKE_OLLAMA_EXISTS') == '1' else 1)
    expression = args[args.index('--format') + 1] if '--format' in args else ''
    if '.State.Health' in expression:
        print('healthy')
    elif '.State.Status' in expression:
        print('running')
    elif 'com.docker.compose.project' in expression:
        print('aigc-checker-server')
elif args[:1] == ['run'] or args[:1] == ['compose'] and 'up' in args:
    # Container replacement loses any manually added extra network membership.
    (state / 'connected').unlink(missing_ok=True)
elif args[:1] == ['compose'] or args[:2] in (['container', 'rm'], ['container', 'ps']):
    pass
elif args[:1] in (['build'], ['exec'], ['start'], ['logs'], ['ps'], ['update']):
    pass
else:
    raise SystemExit('Unexpected Docker command: ' + repr(args))
'''


class ModelDeploymentTests(unittest.TestCase):
    def run_script(self, script_name, *, network=None, compose=True, resource_env=None, expected_code=0):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            binaries = temporary / 'bin'
            binaries.mkdir()
            docker = binaries / 'docker'
            docker.write_text(f'#!{sys.executable}\n' + FAKE_DOCKER)
            docker.chmod(0o755)
            (temporary / '.env').write_text('API_TOKEN=fixture\n' + (
                f'OLLAMA_DOCKER_NETWORK="{network}" # local model network\n' if network else ''))
            target = temporary / script_name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(ROOT / script_name, target)
            env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
                   'FAKE_DOCKER_STATE': str(temporary), 'FAKE_COMPOSE': '1' if compose else '0',
                   'SKIP_GIT_PULL': '1', 'OLLAMA_DOCKER_NETWORK': '',
                   'OLLAMA_DEPLOY_CPUS': '', 'OLLAMA_DEPLOY_MEMORY_GB': ''}
            env.update(resource_env or {})
            result = subprocess.run(['bash', str(target)], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
            return [json.loads(line) for line in (temporary / 'commands.jsonl').read_text().splitlines()]

    def test_compose_update_restores_model_network_after_recreation(self):
        commands = self.run_script('disploy.sh', network='aigc-models')
        mutation = next(i for i, args in enumerate(commands) if args[0] == 'compose' and 'up' in args)
        connection = commands.index(['network', 'connect', 'aigc-models', 'aigc-checker-server'])
        self.assertGreater(connection, mutation)

    def test_plain_docker_update_also_restores_model_network(self):
        commands = self.run_script('disploy.sh', network='aigc-models', compose=False)
        mutation = next(i for i, args in enumerate(commands) if args[0] == 'run')
        connection = commands.index(['network', 'connect', 'aigc-models', 'aigc-checker-server'])
        self.assertGreater(connection, mutation)

    def test_skip_build_never_builds_or_implicitly_pulls_api_image(self):
        for compose in (True, False):
            with self.subTest(compose=compose):
                commands = self.run_script('disploy.sh', network='aigc-models', compose=compose,
                                           resource_env={'SKIP_DOCKER_BUILD': '1'})
                self.assertFalse(any(args[0] == 'build' or args[0] == 'compose' and 'build' in args for args in commands))
                start = next(args for args in commands if args[0] == 'run' or args[0] == 'compose' and 'up' in args)
                self.assertEqual(start[start.index('--pull') + 1], 'never')
                if compose:
                    self.assertIn('--no-build', start)

    def test_remote_model_deployment_does_not_connect_extra_network(self):
        commands = self.run_script('disploy.sh')
        self.assertFalse(any(args[:1] == ['network'] for args in commands))

    def test_standalone_ollama_deployment_never_restarts_api(self):
        commands = self.run_script('scripts/deploy_ollama.sh')
        runs = [args for args in commands if args[0] == 'run']
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][runs[0].index('--name') + 1], 'aigc-ollama')
        self.assertIn('127.0.0.1:11434:11434', runs[0])
        self.assertIn(['exec', 'aigc-ollama', 'ollama', 'pull', 'qwen3:4b'], commands)
        self.assertFalse(any('aigc-checker-server' in args for args in commands))

    def test_four_core_machine_leaves_cpu_and_memory_for_api(self):
        commands = self.run_script('scripts/deploy_ollama.sh', resource_env={
            'FAKE_CPUS': '4', 'FAKE_MEMORY_BYTES': str(16 * 1024**3)})
        run = next(args for args in commands if args[0] == 'run')
        self.assertEqual(run[run.index('--cpus') + 1], '3')
        self.assertEqual(run[run.index('--memory') + 1], '8g')

    def test_resource_request_above_daemon_limit_fails_before_mutations(self):
        for overrides in ({'OLLAMA_DEPLOY_CPUS': '6'}, {'OLLAMA_DEPLOY_MEMORY_GB': '17'},
                          {'OLLAMA_DEPLOY_CPUS': 'invalid'}):
            with self.subTest(overrides=overrides):
                commands = self.run_script('scripts/deploy_ollama.sh', expected_code=1, resource_env={
                    'FAKE_CPUS': '4', 'FAKE_MEMORY_BYTES': str(16 * 1024**3), **overrides})
                self.assertTrue(all(args[0] == 'info' for args in commands))

    def test_existing_container_resource_limits_are_repaired_without_recreation(self):
        commands = self.run_script('scripts/deploy_ollama.sh', resource_env={
            'FAKE_CPUS': '4', 'FAKE_MEMORY_BYTES': str(16 * 1024**3), 'FAKE_OLLAMA_EXISTS': '1',
            'OLLAMA_DEPLOY_CPUS': '3', 'OLLAMA_DEPLOY_MEMORY_GB': '8'})
        self.assertIn(['update', '--cpus', '3', '--memory', '8g', '--memory-swap', '16g', 'aigc-ollama'], commands)
        self.assertFalse(any(args[0] in {'run', 'rm'} or args[:2] == ['container', 'rm'] for args in commands))


if __name__ == '__main__':
    unittest.main()
