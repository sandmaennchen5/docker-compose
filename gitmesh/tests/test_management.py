"""Management integration tests: local DB, mocked provider APIs, real HTTP authorization."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from test_sync import mesh
from test_http import HttpCase
import management


class VaultAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.store = management.Control(self.data)

    def test_validation_errors_report_fields_without_echoing_secrets(self):
        from io import BytesIO
        from urllib.error import HTTPError
        from unittest.mock import MagicMock
        provider = {'kind': 'github', 'url': 'https://github.com', 'token': 'private-token'}
        payloads = [
            (b'{"message":"private-token","errors":[{"resource":"Milestone","field":"due_on","code":"invalid","value":"private-token"}]}', 'Milestone/due_on/invalid'),
            (b'{"errors":[{"field":"private-token","message":"private-token"}]}', ''),
            (b'<html>private-token</html>', ''),
            (b'{"errors":null}', ''),
        ]
        for payload, expected in payloads:
            with self.subTest(payload=payload):
                error = HTTPError('https://api.github.com/test', 422, 'invalid', {}, BytesIO(payload))
                opener = MagicMock()
                opener.open.side_effect = error
                with patch.object(management, 'build_opener', return_value=opener):
                    with self.assertRaises(management.ProviderError) as raised:
                        self.store.api(provider, '/test')
                self.assertEqual(raised.exception.status, 422)
                self.assertNotIn('private-token', str(raised.exception))
                if expected:
                    self.assertIn(expected, str(raised.exception))
                self.assertTrue(error.fp.closed)

    def test_gitlab_budget_is_shared_across_controls_and_tokens(self):
        provider = {'kind': 'gitlab', 'url': 'https://gitlab.com', 'token': 'one'}
        other = management.Control(self.data)
        with patch.object(management.time, 'time', return_value=100), patch.object(management.time, 'sleep') as sleep:
            self.store.api_budget(provider)
            other.api_budget({**provider, 'token': 'two'})
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.9)

    def test_429_cooldown_survives_restart_without_repeating_write(self):
        from io import BytesIO
        from urllib.error import HTTPError
        from unittest.mock import MagicMock
        provider = {'kind': 'gitlab', 'url': 'https://gitlab.com', 'token': 'private'}
        opener = MagicMock()
        error = HTTPError('https://gitlab.com/test', 429, 'limit', {'Retry-After': '3600'}, BytesIO(b'private'))
        opener.open.side_effect = error
        with patch.object(management, 'build_opener', return_value=opener):
            with self.assertRaises(management.RateLimited):
                self.store.api(provider, '/test', method='POST', body={'title': 'example'})
            with self.assertRaises(management.RateLimited):
                management.Control(self.data).api(provider, '/test', method='POST', body={'title': 'example'})
        self.assertEqual(opener.open.call_count, 1)
        self.assertTrue(error.fp.closed)

    def test_retry_after_supports_http_date_and_invalid_values(self):
        with patch.object(management.time, 'time', return_value=0):
            self.assertEqual(self.store.api_retry_after({'Retry-After': 'Thu, 01 Jan 1970 00:02:00 GMT'}), 120)
        for value in ('invalid', 'nan', 'inf'):
            self.assertEqual(self.store.api_retry_after({'Retry-After': value}), 60)

    def provider(self, **changes):
        body = {'id': 'forge', 'name': 'Forgejo', 'kind': 'forgejo', 'url': 'https://forge.example',
                'username': 'alice', 'token': 'very-private-api-token'}
        body.update(changes)
        return self.store.save_provider(body)

    def test_data_directory_cannot_be_used_by_two_instances(self):
        handle = self.store.acquire_instance()
        try:
            with self.assertRaises(RuntimeError):
                management.Control(self.data).acquire_instance()
        finally:
            handle.close()
        management.Control(self.data).acquire_instance().close()

    def test_tokens_encrypted_and_preserved_across_restart(self):
        self.provider()
        self.assertNotIn(b'very-private-api-token', (self.data / 'history.db').read_bytes())
        self.assertNotIn('token', self.store.providers()[0])
        self.assertTrue(self.store.providers()[0]['has_token'])
        self.assertEqual(management.Control(self.data).provider('forge', True)['token'], 'very-private-api-token')
        self.provider(token='')
        self.assertEqual(self.store.provider('forge', True)['token'], 'very-private-api-token')
        self.provider(token='', clear_token=True)
        self.assertEqual(self.store.provider('forge', True)['token'], '')

    def test_token_cannot_move_to_another_host_implicitly(self):
        self.provider()
        with self.assertRaises(ValueError):
            self.provider(url='https://other.example', token='')
        self.assertEqual(self.store.provider('forge')['url'], 'https://forge.example')

    def test_missing_vault_key_does_not_silently_replace_it(self):
        self.provider()
        (self.data / 'vault.key').unlink()
        with self.assertRaises(management.ProviderError):
            self.store.provider('forge', True)
        self.assertFalse((self.data / 'vault.key').exists())

    def test_provider_health_and_username(self):
        self.provider(username='')
        with patch.object(self.store, 'api', return_value={'login': 'alice'}):
            self.assertEqual(self.store.test_provider('forge')['account'], 'alice')
        self.assertEqual(self.store.providers()[0]['health'], 'ok')
        self.assertEqual(self.store.providers()[0]['username'], 'alice')
        with patch.object(self.store, 'api', side_effect=management.ProviderError('HTTP 401')):
            with self.assertRaises(management.ProviderError):
                self.store.test_provider('forge')
        self.assertEqual(self.store.providers()[0]['health'], 'error')

    def test_provider_delete_rejects_live_profile_reference(self):
        self.provider()
        with self.assertRaises(ValueError):
            self.store.delete_provider('forge', {'repositories': [{'remote_providers': {'source': 'forge'}}]})
        self.store.delete_provider('forge', {'repositories': []})
        self.assertEqual(self.store.providers(), [])

    def test_discovery_pagination_and_provider_shapes(self):
        self.provider()
        data = [{'full_name': 'team/r' + str(i), 'clone_url': 'https://forge.example/team/r' + str(i) + '.git'} for i in range(50)]
        with patch.object(self.store, 'api', return_value=data) as api:
            result = self.store.discover('forge', 'my-team', 'org', 2)
        self.assertEqual(len(result['repositories']), 50)
        self.assertEqual(result['next_page'], 3)
        self.assertEqual(api.call_args.args[1], '/orgs/my-team/repos')
        self.provider(kind='gitlab', token='replacement')
        with patch.object(self.store, 'api', return_value=[{'path_with_namespace': 'group/repo', 'http_url_to_repo': 'https://forge.example/group/repo.git'}]) as api:
            result = self.store.discover('forge', 'group/sub', 'org')
        self.assertEqual(result['repositories'][0]['full_name'], 'group/repo')
        self.assertEqual(api.call_args.args[1], '/groups/group%2Fsub/projects')
        self.assertIsNone(result['next_page'])

    def test_discovery_does_not_accept_foreign_clone_hosts(self):
        self.provider()
        with patch.object(self.store, 'api', return_value=[{'full_name': 'bad', 'clone_url': 'https://attacker.invalid/r.git'}]):
            self.assertEqual(self.store.discover('forge')['repositories'], [])

    def test_passwords_hashed_sessions_expire_and_revoke(self):
        self.store.save_user({'name': 'alice', 'role': 'admin', 'password': 'a secure password'})
        session = self.store.login('alice', 'a secure password', 'test')
        self.assertEqual(self.store.actor(session['token'])['role'], 'admin')
        with self.store.db() as db:
            row = db.execute("SELECT password FROM users WHERE name='alice'").fetchone()
            self.assertNotIn('a secure password', row[0])
            self.assertNotIn(session['token'], str([tuple(r) for r in db.execute('SELECT * FROM sessions')]))
            db.execute('UPDATE sessions SET expires=0')
        self.assertIsNone(self.store.actor(session['token']))
        session = self.store.login('alice', 'a secure password', 'test')
        self.store.save_user({'name': 'alice', 'role': 'admin', 'password': 'changed password'})
        self.assertIsNone(self.store.actor(session['token']))
        self.assertIsNone(self.store.login('alice', 'a secure password', 'test'))

    def test_last_admin_and_login_throttle(self):
        self.store.save_user({'name': 'alice', 'role': 'admin', 'password': 'a secure password'})
        with self.assertRaises(ValueError):
            self.store.save_user({'name': 'alice', 'role': 'readonly'})
        with self.store.db() as db:
            db.execute('INSERT INTO login_failures VALUES(?,10,?)', ('test', time.time()))
        with self.assertRaises(management.ProviderError):
            self.store.login('alice', 'a secure password', 'test')


class ManagementHttpTests(HttpCase):
    def auth(self):
        return {'Authorization': 'Bearer ' + 'a' * 32}

    def test_login_throttle_returns_429(self):
        with mesh.control().db() as db:
            db.execute('INSERT INTO login_failures VALUES(?,10,?)', ('127.0.0.1', time.time()))
        self.assertEqual(self.request('POST', '/api/login', b'{"username":"missing","password":"wrong"}')[0], 429)

    def test_readonly_role_cannot_mutate_any_management_endpoint(self):
        store = mesh.control()
        store.save_user({'name': 'reader', 'role': 'readonly', 'password': 'reading-password'})
        code, body = self.request('POST', '/api/login', json.dumps({'username': 'reader', 'password': 'reading-password'}).encode())
        self.assertEqual(code, 200)
        token = json.loads(body)['token']
        headers = {'Authorization': 'Bearer ' + token}
        for path in ['/api/settings', '/api/sync', '/api/providers', '/api/providers/test', '/api/providers/delete',
                     '/api/users', '/api/worker', '/api/jobs/cancel']:
            with self.subTest(path=path):
                self.assertEqual(self.request('POST', path, b'{}', headers)[0], 403)
        for path in ['/api/status', '/api/settings', '/api/providers', '/api/jobs', '/api/me']:
            self.assertEqual(self.request('GET', path, headers=headers)[0], 200)
        self.assertEqual(self.request('GET', '/api/users', headers=headers)[0], 403)
        self.assertEqual(self.request('POST', '/api/logout', headers=headers)[0], 200)
        self.assertEqual(self.request('GET', '/api/me', headers=headers)[0], 401)

    def test_admin_provider_crud_never_returns_token(self):
        provider = {'id': 'test', 'name': 'GitHub', 'kind': 'github', 'url': 'https://github.com', 'token': 'never-return-me'}
        self.assertEqual(self.request('POST', '/api/providers', json.dumps(provider).encode(), self.auth())[0], 200)
        code, body = self.request('GET', '/api/providers', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertNotIn(b'never-return-me', body)
        self.assertNotIn(b'gAAAA', body)
        self.assertEqual(self.request('POST', '/api/providers/delete', b'{"id":"test"}', self.auth())[0], 200)

    def test_worker_controls_persist_state(self):
        for action, mode in [('pause', 'paused'), ('resume', 'running'), ('stop', 'stopped'), ('start', 'running')]:
            code, body = self.request('POST', '/api/worker', json.dumps({'action': action}).encode(), self.auth())
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)['mode'], mode)
            self.assertEqual(mesh.control().mode(), mode)

    def test_duplicate_reverse_mapping_is_rejected(self):
        config = mesh.load_config()
        config['repositories'].append({'name': 'duplicate', 'remotes': {'reverse': 'https://example.org/b.git', 'other': 'https://example.net/c.git'}})
        self.assertEqual(self.request('POST', '/api/settings', json.dumps(config).encode(), self.auth())[0], 400)

    def test_provider_host_must_match_credentials_mapping(self):
        mesh.control().save_provider({'id': 'github', 'name': 'GitHub', 'kind': 'github', 'url': 'https://github.com', 'token': 'secret'})
        config = mesh.load_config()
        config['repositories'][0]['remote_providers'] = {'a': 'github'}
        self.assertEqual(self.request('POST', '/api/settings', json.dumps(config).encode(), self.auth())[0], 400)

    def test_unsafe_force_configuration_rejected(self):
        config = mesh.load_config()
        config['repositories'][0].update(policy='force-with-lease', force_confirmed=True)
        self.assertEqual(self.request('POST', '/api/settings', json.dumps(config).encode(), self.auth())[0], 400)
        config['repositories'][0].update(direction='one-way', source='a', force_confirmed=False)
        self.assertEqual(self.request('POST', '/api/settings', json.dumps(config).encode(), self.auth())[0], 400)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = management.Control(self.temp.name)
        self.config = {'repositories': [{'name': 'a', 'interval': 0}, {'name': 'b', 'interval': 0}, {'name': 'c', 'interval': 0}],
                       'worker': {'parallelism': 2, 'min_free_mb': 0}}
        self.gate = threading.Lock()

    def worker(self, execute):
        worker = management.Worker(self.store, lambda: self.config, execute, self.gate)
        self.addCleanup(worker.close)
        return worker

    def wait_done(self, worker):
        deadline = time.time() + 5
        while worker.active and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(worker.active)

    def test_queue_deduplicates_and_survives_new_control(self):
        self.assertEqual(len(self.store.enqueue(['a', 'b'])), 2)
        self.assertEqual(self.store.enqueue(['a']), [])
        self.assertEqual(len(management.Control(self.temp.name).jobs()), 2)

    def test_parallelism_and_pause_stop_keep_queued_jobs(self):
        release = threading.Event()
        worker = self.worker(lambda repo: (release.wait(5) and 'success', {'updated': []}))
        self.addCleanup(release.set)
        self.store.enqueue(['a', 'b', 'c'])
        worker.tick()
        self.assertEqual(len(worker.active), 2)
        self.store.mode('paused')
        release.set()
        self.wait_done(worker)
        worker.tick()
        self.assertEqual(sum(j['status'] == 'queued' for j in self.store.jobs()), 1)
        self.store.mode('stopped')
        worker.tick()
        self.assertEqual(sum(j['status'] == 'queued' for j in self.store.jobs()), 1)
        self.store.mode('running')
        worker.tick()
        self.wait_done(worker)
        self.assertTrue(all(j['status'] == 'success' for j in self.store.jobs()))

    def test_recover_running_job_reports_interruption(self):
        self.store.enqueue(['a'])
        self.store.claim()
        self.worker(lambda r: ('success', {}))
        self.assertEqual(self.store.jobs()[0]['status'], 'error')
        self.assertIn('beendet', self.store.jobs()[0]['details']['message'])

    def test_resource_limit_keeps_queue_intact(self):
        worker = self.worker(lambda r: ('success', {}))
        self.config['worker']['min_free_mb'] = 100
        self.store.enqueue(['a'])
        with patch.object(management, 'resources', return_value={'disk_free_mb': 0, 'load': 0, 'memory_available_mb': 1000}):
            worker.tick()
        self.assertEqual(self.store.jobs()[0]['status'], 'queued')
        self.assertIn('Ressourcenlimit', worker.notice)

    def test_failed_push_is_not_automatically_retried(self):
        calls = []
        def execute(repo):
            calls.append(repo)
            raise mesh.GitFailure('push')
        worker = self.worker(execute)
        self.store.enqueue(['a'])
        worker.tick()
        self.wait_done(worker)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.jobs()[0]['attempts'], 1)

    def test_fetch_failures_are_retried(self):
        calls = []
        def execute(repo):
            calls.append(repo)
            if len(calls) < 3:
                raise mesh.GitFailure('fetch')
            return 'success', {}
        worker = self.worker(execute)
        self.store.enqueue(['a'])
        with patch.object(worker.stop_event, 'wait', return_value=False):
            worker.tick()
            self.wait_done(worker)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.store.jobs()[0]['attempts'], 3)

    def test_per_profile_schedule_and_manual_only(self):
        self.config['repositories'][0]['interval'] = 45
        self.store.schedule(self.config, 100)
        self.assertEqual(set(self.store.schedules()), {'a'})
        self.store.schedule(self.config, 200)
        self.assertEqual([j['profile'] for j in self.store.jobs()], ['a'])
        self.assertEqual(self.store.schedules()['a'], 245)
