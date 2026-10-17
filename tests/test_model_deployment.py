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
if args[:2] == ['network', 'inspect']:
    if '--format' in args and (state / 'connected').exists():
        print('aigc-checker-server')
elif args[:2] == ['network', 'connect']:
    (state / 'connected').touch()
elif args[:2] == ['container', 'inspect'] or args[:1] == ['inspect']:
    if 'aigc-ollama' in args:
        raise SystemExit(1)
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
elif args[:1] in (['info'], ['build'], ['exec'], ['start'], ['logs'], ['ps']):
    pass
else:
    raise SystemExit('Unexpected Docker command: ' + repr(args))
'''


class ModelDeploymentTests(unittest.TestCase):
    def run_script(self, script_name, *, network=None, compose=True):
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
                   'SKIP_GIT_PULL': '1', 'OLLAMA_DOCKER_NETWORK': ''}
            result = subprocess.run(['bash', str(target)], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
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


if __name__ == '__main__':
    unittest.main()
