import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from test_sync import mesh


class HttpCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'repositories': [{'name': 'demo', 'remotes': {'a': 'https://example.org/a.git', 'b': 'https://example.org/b.git'}, 'webhook_secret_env': 'TEST_HOOK'}]}))
        self.addCleanup(patch.stopall)
        patch.object(mesh, 'DATA', self.root / 'data').start()
        patch.object(mesh, 'CONFIG', self.config).start()
        patch.dict(os.environ, {'GITMESH_ADMIN_TOKEN': 'a' * 32, 'TEST_HOOK': 'secret'}).start()
        self.run = patch.object(mesh, 'run_all', return_value=[]).start()
        self.server = mesh.ThreadingHTTPServer(('127.0.0.1', 0), mesh.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=b'', headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            conn.request(method, path, body, headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()


class HttpTests(HttpCase):
    def test_dashboard_and_health_available(self):
        self.assertEqual(self.request('GET', '/')[0], 200)
        self.assertEqual(self.request('GET', '/healthz')[0], 200)

    def test_admin_operations_require_token(self):
        self.assertEqual(self.request('GET', '/api/status')[0], 401)
        self.assertEqual(self.request('POST', '/api/sync')[0], 401)
        code, body = self.request('GET', '/api/status', headers={'Authorization': 'Bearer ' + 'a' * 32})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['repositories'][0]['name'], 'demo')
        self.run.assert_not_called()

    def test_webhooks_require_signature_and_known_mapping(self):
        self.assertEqual(self.request('POST', '/hooks/github/demo', b'{}')[0], 403)
        self.assertEqual(self.request('POST', '/hooks/gitlab/missing', b'{}', {'X-Gitlab-Token': 'secret'})[0], 403)
        self.run.assert_not_called()

    def test_all_provider_signatures(self):
        body = b'{"ref":"refs/heads/main"}'
        signature = hmac.new(b'secret', body, hashlib.sha256).hexdigest()
        cases = [('github', {'X-Hub-Signature-256': 'sha256=' + signature}),
                 ('forgejo', {'X-Forgejo-Signature': signature}),
                 ('gitlab', {'X-Gitlab-Token': 'secret'})]
        for provider, headers in cases:
            with self.subTest(provider=provider):
                self.assertEqual(self.request('POST', '/hooks/' + provider + '/demo', body, headers)[0], 202)
        self.assertEqual(self.run.call_count, 3)

    def test_tampered_body_rejected(self):
        signature = hmac.new(b'secret', b'original', hashlib.sha256).hexdigest()
        self.assertEqual(self.request('POST', '/hooks/github/demo', b'tampered', {'X-Hub-Signature-256': 'sha256=' + signature})[0], 403)
        self.run.assert_not_called()

    def test_configuration_rejects_local_transport(self):
        self.config.write_text(json.dumps({'repositories': [{'name': 'demo', 'remotes': {'a': '/tmp/a', 'b': 'https://example.org/b'}}]}))
        with self.assertRaises(ValueError):
            mesh.load_config()


class SettingsTests(HttpCase):
    def auth(self):
        return {'Authorization': 'Bearer ' + 'a' * 32}

    def test_settings_require_authentication(self):
        self.assertEqual(self.request('GET', '/api/settings')[0], 401)
        self.assertEqual(self.request('POST', '/api/settings', b'{}')[0], 401)

    def test_settings_roundtrip_and_persistence(self):
        config = {'interval': 45, 'repositories': [{'name': 'new-net', 'enabled': False,
                  'webhook_secret_env': 'TEST_HOOK', 'remotes': {'github': 'https://example.org/a.git',
                  'forgejo': 'ssh://git@example.org/b.git'}}]}
        self.assertEqual(self.request('POST', '/api/settings', json.dumps(config).encode(), self.auth())[0], 200)
        self.assertEqual(json.loads(self.config.read_text()), config)
        self.assertEqual(json.loads(self.request('GET', '/api/settings', headers=self.auth())[1]), config)
        self.assertEqual(mesh.load_config(), config)

    def test_invalid_settings_preserve_previous_config(self):
        before = self.config.read_bytes()
        for config in [{'interval': 0, 'repositories': []}, {'repositories': [{'name': '../escape', 'remotes': {}}]},
                       {'repositories': 'invalid'}, {'repositories': [{'name': 'x', 'remotes': {'--bad': 'https://example.org/a', 'b': 'https://example.org/b'}}]}]:
            self.assertEqual(self.request('POST', '/api/settings', json.dumps(config).encode(), self.auth())[0], 400)
            self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.request('POST', '/api/settings', b'{invalid', self.auth())[0], 400)

    def test_running_sync_prevents_settings_change(self):
        before = self.config.read_bytes()
        with mesh.LOCK:
            self.assertEqual(self.request('POST', '/api/settings', b'{"repositories":[]}', self.auth())[0], 409)
        self.assertEqual(self.config.read_bytes(), before)

    def test_empty_config_is_valid_and_saved(self):
        self.assertTrue(mesh.save_config({'interval': 300, 'repositories': []}))
        self.assertEqual(mesh.load_config()['repositories'], [])
