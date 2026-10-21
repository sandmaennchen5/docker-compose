"""CI: verify the built Linux image, Secret Service and actual MultiGit PTY."""
import base64
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request


def published_address(name):
    # Docker can assign a different ephemeral host port on container restart.
    binding = subprocess.check_output(['docker', 'port', name, '8080/tcp'], text=True).strip()
    if not binding or len(binding.splitlines()) != 1:
        raise RuntimeError('Expected one published localhost port for the smoke container')
    return 'http://' + binding


def main(image):
    name = 'multigit-smoke-' + secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    base = ''
    def api(path, payload=None):
        headers = {'Authorization': 'Basic ' + base64.b64encode(('admin:' + password).encode()).decode()}
        data = None
        if payload is not None:
            headers.update({'Content-Type': 'application/json', 'X-MultiGit': '1'})
            data = json.dumps(payload).encode()
        request = urllib.request.Request(base + path, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    def wait_session(key):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = api('/api/terminal?id=' + key)
            if state['done']:
                assert state['code'] == 0, state['output']
                return state['output']
            time.sleep(.2)
        raise RuntimeError('Terminal session did not complete')
    try:
        subprocess.run(['docker', 'run', '-d', '--name', name, '--init',
                        '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                        '-e', 'WEB_PASSWORD=' + password, '-p', '127.0.0.1::8080', image],
                       check=True, capture_output=True, text=True)
        base = published_address(name)
        deadline = time.monotonic() + 90
        while True:
            try:
                api('/health')
                break
            except (OSError, urllib.error.URLError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        catalog = api('/api/catalog')
        assert catalog['terminal'] and catalog['keyring'], catalog
        api('/api/repos/create', dict(name='smoke', confirmed=True))
        result = api('/api/terminal/start', dict(repo='smoke', operation='version', arguments=''))
        assert 'multigit' in wait_session(result['id']).lower()
        result = api('/api/terminal/start', dict(repo='smoke', operation='help', arguments=''))
        assert 'Commands:' in wait_session(result['id'])
        status = api('/api/overview?repo=smoke')
        assert status['clean'] and status['commits'] == []
        subprocess.run(['docker', 'restart', name], check=True, capture_output=True)
        base = published_address(name)
        deadline = time.monotonic() + 90
        while True:
            try:
                assert api('/api/catalog')['keyring']
                break
            except (OSError, urllib.error.URLError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        print('Image smoke passed: startup, private keyring, repository API, native PTY/version/help and status')
    except Exception:
        subprocess.run(['docker', 'logs', '--tail', '80', name], check=False)
        raise
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, check=False)


if __name__ == '__main__':
    main(sys.argv[1])
