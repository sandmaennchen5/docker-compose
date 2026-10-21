"""Credential transport tests never contact a provider or use real credentials."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.request import Request, build_opener
from unittest.mock import MagicMock, patch
from test_sync import mesh
import management


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = management.Control(self.temp.name)

    def test_provider_headers_and_api_roots(self):
        for kind, url, expected, header in [
            ('github', 'https://github.com', 'https://api.github.com/user', 'Authorization'),
            ('github', 'https://enterprise.example', 'https://enterprise.example/api/v3/user', 'Authorization'),
            ('gitlab', 'https://gitlab.example', 'https://gitlab.example/api/v4/user', 'Private-token'),
            ('forgejo', 'https://forge.example', 'https://forge.example/api/v1/user', 'Authorization'),
        ]:
            with self.subTest(kind=kind, url=url):
                response = MagicMock()
                response.__enter__.return_value.read.return_value = b'{"login":"test"}'
                opener = MagicMock()
                opener.open.return_value = response
                with patch.object(management, 'build_opener', return_value=opener):
                    self.store.api({'kind': kind, 'url': url, 'token': 'transport-test-secret'}, '/user')
                request = opener.open.call_args.args[0]
                self.assertEqual(request.full_url, expected)
                self.assertNotIn('transport-test-secret', request.full_url)
                self.assertIn('transport-test-secret', request.get_header(header))

    def test_redirect_does_not_forward_authorization(self):
        hits = []
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                hits.append(self.path)
                self.send_response(302 if self.path == '/start' else 200)
                if self.path == '/start':
                    self.send_header('Location', '/destination')
                self.end_headers()
        server = HTTPServer(('127.0.0.1', 0), Redirect)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        request = Request('http://127.0.0.1:' + str(server.server_port) + '/start',
                          headers={'Authorization': 'Bearer isolated-test-secret'})
        with self.assertRaises(HTTPError) as captured:
            build_opener(management.NoRedirect()).open(request, timeout=5)
        captured.exception.close()
        self.assertEqual(hits, ['/start'])

    def test_git_token_stays_out_of_arguments_and_remote_url(self):
        provider = {'id': 'github', 'name': 'GitHub', 'kind': 'github', 'url': 'https://github.com',
                    'token': 'git-transport-test-secret', 'username': 'alice'}
        self.store.save_provider(provider)
        repo = {'remote_providers': {'origin': 'github'}, 'remotes': {'origin': 'https://github.com/team/repo.git'}}
        with patch.object(mesh, 'DATA', Path(self.temp.name)):
            credentials = mesh.prepare_credentials(repo)
        self.assertEqual(credentials['origin'], ('alice', 'git-transport-test-secret'))
        mesh.GIT_CONTEXT.credentials = credentials
        self.addCleanup(setattr, mesh.GIT_CONTEXT, 'credentials', {})
        with patch.object(mesh.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            mesh.git(Path(self.temp.name), 'push', 'origin', 'abc:refs/heads/main')
        self.assertNotIn('git-transport-test-secret', str(run.call_args.args))
        self.assertNotIn('git-transport-test-secret', repo['remotes']['origin'])
        self.assertEqual(run.call_args.kwargs['env']['GITMESH_GIT_TOKEN'], 'git-transport-test-secret')
        self.assertIn('http.followRedirects=false', run.call_args.args[0])

    def test_askpass_distinguishes_username_in_password_prompt(self):
        helper = Path(__file__).parents[1] / 'askpass.py'
        env = dict(os.environ, GITMESH_GIT_USERNAME='username', GITMESH_GIT_TOKEN='test-password')
        for prompt, expected in [("Username for 'https://example.invalid':", 'username'),
                                 ("Password for 'https://username@example.invalid':", 'test-password')]:
            result = subprocess.run([sys.executable, str(helper), prompt], env=env,
                                    capture_output=True, text=True, timeout=5, check=True)
            self.assertEqual(result.stdout.strip(), expected)
