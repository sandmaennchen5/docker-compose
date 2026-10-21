"""Exercise the actual two-service Compose deployment in an isolated project."""
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    project = 'gitmesh-test-' + secrets.token_hex(6)
    if not re.fullmatch(r'gitmesh-test-[0-9a-f]{12}', project):
        raise RuntimeError('Unexpected test project')
    source = Path(__file__).resolve().parents[1] / 'docker-compose.ghcr.yml'
    env = dict(os.environ, GITMESH_IMAGE=os.environ.get('GITMESH_TEST_IMAGE', 'gitmesh:codex-test'),
               GITMESH_ADMIN_TOKEN=secrets.token_hex(32), GITMESH_WEBHOOK_SECRET=secrets.token_hex(32), GITMESH_VAULT_KEY='')
    with tempfile.TemporaryDirectory(prefix='gitmesh-compose-') as directory:
        compose = Path(directory) / 'compose.yml'
        compose.write_text(source.read_text(encoding='utf-8').replace('127.0.0.1:8088:8080', '127.0.0.1::8080'), encoding='utf-8')
        command = ['docker', 'compose', '-p', project, '-f', str(compose)]
        def cli(*args):
            result = subprocess.run([*command, *args], env=env, capture_output=True, text=True, timeout=100)
            if result.returncode:
                raise RuntimeError(result.stderr)
            return result.stdout.strip()
        def wait(predicate):
            until = time.monotonic() + 20
            while time.monotonic() < until:
                if predicate():
                    return
                time.sleep(0.2)
            raise RuntimeError('Compose health or state did not converge')
        try:
            helper = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', '/app/askpass.py', '-e', 'GITMESH_GIT_USERNAME=isolated-test', env['GITMESH_IMAGE'], 'Username'], capture_output=True, text=True, timeout=20)
            assert helper.returncode == 0 and helper.stdout.strip() == 'isolated-test', 'Docker HTTPS askpass is not executable'
            cli('up', '-d', '--wait', '--wait-timeout', '60')
            address = cli('port', 'gitmesh', '8080')
            base = 'http://' + address
            def api(path, body=None):
                request = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                  headers={'Authorization': 'Bearer ' + env['GITMESH_ADMIN_TOKEN'], 'Content-Type': 'application/json'})
                with urlopen(request, timeout=5) as response:
                    return json.load(response)
            wait(lambda: api('/api/status')['worker']['online'])
            worker_id = cli('ps', '-q', 'gitmesh-worker')
            heartbeat = api('/api/status')['worker']['heartbeat']
            config = api('/api/settings')
            config['worker_sets'] = {'team': {'parallelism': 1, 'min_free_mb': 0}}
            config['profiles'] = {'team-profile': {'direction': 'one-way', 'source': 'source', 'worker_set': 'team', 'interval': 0, 'enabled': False}}
            config['repositories'] = [{'name': name, 'profile': 'team-profile', 'remotes': {'source': 'https://source.invalid/team/' + name + '.git', 'target': 'https://target.invalid/team/' + name + '.git'}} for name in ('one', 'two')]
            def save():
                try:
                    api('/api/settings', config)
                    return True
                except HTTPError as exc:
                    if exc.code != 409:
                        raise
                    return False
            wait(save)
            wait(lambda: 'team' in api('/api/status')['worker']['groups'])
            api('/api/worker', {'action': 'pause', 'group': 'team'})
            wait(lambda: api('/api/status')['worker']['groups']['team']['mode'] == 'paused')
            api('/api/worker', {'action': 'resume', 'group': 'team'})
            cli('restart', 'gitmesh')
            base = 'http://' + cli('port', 'gitmesh', '8080')
            def healthy():
                try:
                    return api('/healthz')['status'] == 'ok'
                except (OSError, URLError):
                    return False
            wait(healthy)
            assert cli('ps', '-q', 'gitmesh-worker') == worker_id, 'Web restart replaced the worker'
            wait(lambda: api('/api/status')['worker']['heartbeat'] > heartbeat)
            saved = api('/api/settings')
            assert len(saved['repositories']) == 2 and saved['profiles']['team-profile']['worker_set'] == 'team'
            assert api('/api/status')['worker']['online']
            print('Docker Compose smoke: two services healthy, shared profiles, group pause/resume, independent web restart, persistent configuration: OK')
        except Exception:
            print(cli('ps', '--all'))
            print(cli('logs', '--tail', '40'))
            raise
        finally:
            cli('down', '--volumes', '--remove-orphans')


if __name__ == '__main__':
    main()
