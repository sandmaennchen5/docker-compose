"""Concrete Git diagnostics must be useful without persisting credentials."""
import base64
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote
from test_sync import mesh
import management


class GitDiagnosticsTests(unittest.TestCase):
    def tearDown(self):
        mesh.GIT_CONTEXT.credentials = {}

    def test_failed_git_reports_operation_remote_and_redacts_secrets(self):
        secret = 'private:test/token+value'
        encoded = base64.b64encode(('alice:' + secret).encode()).decode()
        mesh.GIT_CONTEXT.credentials = {'origin': ('alice', secret)}
        stderr = ('fatal: Authentication failed for https://alice:' + quote(secret, safe='') +
                  '@example.invalid/team/repo.git\nAuthorization: Basic ' + encoded +
                  '\nremote: ' + secret + '\nremote: ' + encoded)
        result = subprocess.CompletedProcess([], 128, '', stderr)
        with patch.object(mesh.subprocess, 'run', return_value=result):
            with self.assertRaises(mesh.GitFailure) as captured:
                mesh.git('.', 'fetch', 'origin')
        details = captured.exception.details
        self.assertEqual(details['operation'], 'fetch')
        self.assertEqual(details['remote'], 'origin')
        self.assertEqual(details['exit_code'], 128)
        self.assertIn('Authentication failed', details['git_error'])
        for value in (secret, quote(secret, safe=''), encoded):
            self.assertNotIn(value, str(details))
        self.assertIn('Provider', details['hint'])

    def test_timeout_does_not_expose_subprocess_stderr(self):
        mesh.GIT_CONTEXT.credentials = {'target': ('alice', 'timeout-secret')}
        with patch.object(mesh.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['git'], 180, stderr=b'timeout-secret')):
            with self.assertRaises(mesh.GitFailure) as captured:
                mesh.git('.', 'push', '--force-with-lease=refs/heads/main:abc', 'target', 'abc:refs/heads/main')
        self.assertEqual(captured.exception.details['remote'], 'target')
        self.assertNotIn('timeout-secret', str(captured.exception.details))
        self.assertFalse(captured.exception.retryable)

    def test_missing_branch_checks_do_not_raise(self):
        with patch.object(mesh.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'not found')):
            self.assertEqual(mesh.git('.', 'merge-base', '--is-ancestor', 'a', 'b', check=False).returncode, 1)

    def test_worker_persists_diagnostic_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            store = management.Control(directory)
            def execute(repo):
                raise mesh.GitFailure('push', 'forgejo', 1, 'remote: protected branch hook declined')
            import threading
            worker = management.Worker(store, lambda: {}, execute, threading.Lock())
            try:
                store.enqueue(['profile'])
                worker.work(store.claim(), {'name': 'profile'})
                details = store.jobs()[0]['details']
                self.assertEqual(details['operation'], 'push')
                self.assertEqual(details['remote'], 'forgejo')
                self.assertIn('protected branch', details['git_error'])
            finally:
                worker.close()
