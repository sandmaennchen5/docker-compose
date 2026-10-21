"""Explicit internal HTTP support uses isolated local servers and fake tokens."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer
from test_sync import mesh
import management
import transport


class InternalHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = management.Control(self.temp.name)
        self.body = {'id': 'local', 'name': 'Internal Forgejo', 'kind': 'forgejo', 'url': 'http://172.30.10.20:40501', 'token': 'isolated-http-token', 'allow_http': True}

    def repo(self):
        return {'name': 'local', 'remotes': {'source': 'https://github.com/team/repo.git', 'target': self.body['url'] + '/team/repo.git'}, 'remote_providers': {'target': 'local'}}

    def test_http_requires_explicit_flag_and_flag_survives_restart(self):
        for flag in (False, 'true'):
            with self.assertRaises(ValueError):
                self.store.save_provider(dict(self.body, allow_http=flag))
        self.store.save_provider(self.body)
        self.assertTrue(management.Control(self.temp.name).providers()[0]['allow_http'])
        self.assertNotIn(self.body['token'], json.dumps(self.store.providers()))
        self.assertTrue(self.store.provider('local')['allow_http'])
        self.store.delete_provider('local', {'repositories': []})
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM provider_http').fetchone()[0], 0)

    def test_http_credentials_and_repository_origin_are_strict(self):
        self.store.save_provider(self.body)
        repo = self.repo()
        mesh.validate_config({'repositories': [repo]})
        with patch.object(mesh, 'DATA', Path(self.temp.name)):
            self.assertEqual(mesh.prepare_credentials(repo)['target'][1], self.body['token'])
            repo['remotes']['target'] = 'http://172.30.10.20:40502/team/repo.git'
            with self.assertRaises(ValueError):
                mesh.prepare_credentials(repo)
        provider = self.store.provider('local')
        self.assertEqual(self.store.repository_path(provider, self.body['url'] + '/team/repo.git'), ('team', 'repo'))
        for url in ('http://172.30.10.20:40502/team/repo.git', 'https://172.30.10.20:40501/team/repo.git', 'http://user@172.30.10.20:40501/team/repo.git'):
            with self.assertRaises(ValueError):
                self.store.repository_path(provider, url)
        del repo['remote_providers']
        with self.assertRaises(ValueError):
            mesh.validate_config({'repositories': [repo]})

    def test_https_provider_cannot_silently_downgrade_to_http(self):
        self.store.save_provider(dict(self.body, url='https://172.30.10.20:40501', allow_http=False))
        with patch.object(mesh, 'DATA', Path(self.temp.name)):
            with self.assertRaises(ValueError):
                mesh.prepare_credentials(self.repo())
        with self.assertRaises(ValueError):
            transport.save(self.store, {'id': 'local', 'protocol': 'http'})

    def test_actual_local_http_api_receives_provider_auth(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                received.append((self.path, self.headers.get('Authorization')))
                payload = b'{"login":"tester"}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        server = HTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.store.save_provider(dict(self.body, url='http://127.0.0.1:' + str(server.server_port)))
        self.assertEqual(self.store.test_provider('local')['account'], 'tester')
        self.assertEqual(received, [('/api/v1/user', 'token ' + self.body['token'])])

    def test_http_discovery_and_transport_stay_on_exact_origin(self):
        self.store.save_provider(self.body)
        records = [{'full_name': 'team/repo', 'clone_url': self.body['url'] + '/team/repo.git'},
                   {'full_name': 'team/foreign', 'clone_url': 'http://other.example/team/foreign.git'}]
        with patch.object(self.store, 'api', return_value=records):
            result = self.store.discover('local')
        self.assertEqual(len(result['repositories']), 1)
        transport.save(self.store, {'id': 'local', 'protocol': 'http'})
        self.assertEqual(transport.remote_url(self.store, self.repo(), 'target'), self.body['url'] + '/team/repo.git')

    def test_http_token_only_enters_askpass_environment(self):
        mesh.GIT_CONTEXT.credentials = {'target': ('tester', self.body['token'])}
        self.addCleanup(setattr, mesh.GIT_CONTEXT, 'credentials', {})
        with patch.object(mesh.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            mesh.git(self.temp.name, 'push', 'target', 'abc:refs/heads/main')
        self.assertEqual(run.call_args.kwargs['env']['GITMESH_GIT_TOKEN'], self.body['token'])
        self.assertNotIn(self.body['token'], str(run.call_args.args))
