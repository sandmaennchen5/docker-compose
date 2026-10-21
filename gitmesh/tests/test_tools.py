"""Repository tools run against isolated Git repositories; webhook APIs are mocked."""
import json
import tempfile
import threading
import hashlib
import hmac
import unittest
from unittest.mock import patch
from test_sync import SyncFixture, mesh
from test_http import HttpCase
import inspection
import management


class RepositoryTests(SyncFixture):
    def test_preview_never_pushes_and_lists_commits(self):
        status, result = mesh.synchronize(self.repo, dry_run=True)
        self.assertEqual(status, 'success')
        self.assertEqual(len(result['plan']), 2)
        self.assertEqual(result['plan'][0]['commits'][0]['subject'], 'initial')
        self.assertNotEqual(self.command(self.root / 'gitlab', 'show-ref', check=False).returncode, 0)

    def test_create_delete_and_stale_guard(self):
        tools = mesh.repository_tools()
        body = dict(remote='github', kind='heads', name='feature/test', action='create', from_remote='github', from_ref='heads/main')
        result = tools.change_ref(self.repo, body)
        self.assertEqual(self.tip('github', 'refs/heads/feature/test'), result['oid'])
        with self.assertRaises(inspection.StaleState):
            tools.change_ref(self.repo, body)
        body.update(action='delete', confirm_delete=True, expected_oid='0' * 40)
        with self.assertRaises(inspection.StaleState):
            tools.change_ref(self.repo, body)
        body['expected_oid'] = result['oid']
        self.assertEqual(tools.change_ref(self.repo, body)['status'], 'deleted')
        body.update(action='create', kind='tags', name='v-test')
        self.assertEqual(tools.change_ref(self.repo, body)['status'], 'created')
        self.assertEqual(self.command(self.root / 'github', 'cat-file', '-t', 'refs/tags/v-test').stdout.strip(), 'commit')

    def divergent(self, conflict=False):
        mesh.synchronize(self.repo)
        base = self.tip('github')
        if conflict:
            self.commit('left')
        else:
            (self.work / 'left.txt').write_text('left')
            self.command(self.work, 'add', '.')
            self.command(self.work, 'commit', '-m', 'left')
        self.command(self.work, 'push', self.remotes['github'], 'main')
        self.command(self.work, 'reset', '--hard', base)
        if conflict:
            self.commit('right')
        else:
            (self.work / 'right.txt').write_text('right')
            self.command(self.work, 'add', '.')
            self.command(self.work, 'commit', '-m', 'right')
        self.command(self.work, 'push', self.remotes['forgejo'], 'main')
        return dict(left='github', right='forgejo', target='forgejo', ref='heads/main', expected_left=self.tip('github'), expected_right=self.tip('forgejo'), confirm_merge=True)

    def test_comparison_and_clean_merge(self):
        body = self.divergent()
        tools = mesh.repository_tools()
        compare = tools.compare(self.repo, body)
        self.assertEqual((compare['left_only'], compare['right_only']), (1, 1))
        self.assertIn('left.txt', compare['patch'])
        result = tools.merge(self.repo, body)
        self.assertEqual(result['status'], 'merged')
        self.assertEqual(self.tip('forgejo'), result['oid'])
        self.assertEqual(self.tip('github'), body['expected_left'])
        parents = self.command(self.root / 'forgejo', 'rev-list', '--parents', '-n', '1', result['oid']).stdout.split()
        self.assertEqual(len(parents), 3)

    def test_content_conflicts_and_changed_snapshot_preserve_remotes(self):
        body = self.divergent(conflict=True)
        tools = mesh.repository_tools()
        before = {r: self.tip(r) for r in self.remotes}
        result = tools.merge(self.repo, body)
        self.assertEqual(result['status'], 'conflict')
        self.assertIn('content.txt', result['files'])
        self.assertEqual(before, {r: self.tip(r) for r in self.remotes})
        body['expected_right'] = '0' * 40
        with self.assertRaises(inspection.StaleState):
            tools.merge(self.repo, body)

    def test_ref_push_race_does_not_overwrite_new_target(self):
        tools = mesh.repository_tools()
        original = self.command
        concurrent_tip = self.commit('concurrent target')
        body = dict(remote='github', kind='heads', name='race', action='create', from_remote='github', from_ref='heads/main')
        def concurrent(path, *args, check=True):
            if args[0] == 'push' and args[-1].endswith(':refs/heads/race'):
                original(self.work, 'push', self.remotes['github'], 'main:refs/heads/race')
                result = original(path, *args, check=False)
                self.assertNotEqual(result.returncode, 0)
                raise inspection.StaleState('Concurrent creation rejected')
            return original(path, *args, check=check)
        tools.git = concurrent
        with self.assertRaises(inspection.StaleState):
            tools.change_ref(self.repo, body)
        self.assertEqual(self.tip('github', 'refs/heads/race'), concurrent_tip)

    def test_preview_missing_target_does_not_provision(self):
        self.repo.update(create_missing=True, source='github', remote_providers={'gitlab': 'p', 'forgejo': 'p'})
        with patch.object(mesh, 'control') as factory:
            factory.return_value.db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = None
            factory.return_value.repository_exists.return_value = False
            result = mesh.synchronize(self.repo, dry_run=True)[1]
            self.assertEqual(set(result['missing_repositories']), {'gitlab', 'forgejo'})
            factory.return_value.ensure_repository.assert_not_called()


class HookTests(unittest.TestCase):
    def test_create_update_and_idempotence_for_all_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            store = management.Control(directory)
            for kind in ('github', 'gitlab', 'forgejo'):
                store.save_provider(dict(id='p', name='Test', kind=kind, url='https://git.example', token='api-token'))
                repo = dict(name='demo', remotes={'origin': 'https://git.example/team/repo.git'}, remote_providers={'origin': 'p'})
                callback = 'https://mesh.example/hooks/' + kind + '/demo'
                with patch.object(store, 'api', side_effect=[[], {}]) as api:
                    result = store.register_hooks(repo, 'https://mesh.example', 'hook-secret')
                    self.assertEqual(api.call_args.kwargs['method'], 'POST')
                    self.assertNotIn('hook-secret', json.dumps(result))
                hook = {'id': 42, 'url': callback} if kind == 'gitlab' else {'id': 42, 'config': {'url': callback}}
                with patch.object(store, 'api', side_effect=[[hook], {}]) as api:
                    store.register_hooks(repo, 'https://mesh.example', 'new-secret')
                    self.assertEqual(api.call_args.kwargs['method'], 'PUT' if kind == 'gitlab' else 'PATCH')
                    self.assertTrue(api.call_args.args[1].endswith('/42'))
                with patch.object(store, 'api', return_value=[hook]) as api:
                    self.assertEqual(store.register_hooks(repo, 'https://mesh.example', 'secret', refresh_secret=False)[0]['status'], 'exists')
                    self.assertEqual(api.call_count, 1)

    def test_all_mappings_validated_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = management.Control(directory)
            with patch.object(store, 'api') as api:
                with self.assertRaises(ValueError):
                    store.register_hooks({'name': 'demo', 'remotes': {'a': 'https://git.example/a'}}, 'https://mesh.example', 'secret')
                api.assert_not_called()


class ToolsHttpTests(HttpCase):
    def test_readonly_routes_and_mutation_permissions(self):
        actor = {'name': 'reader', 'role': 'readonly'}
        with patch.object(mesh.Handler, 'actor', return_value=actor), patch.object(mesh, 'repository_action', return_value={'ok': True}) as action:
            for route in ('preview', 'refs', 'compare'):
                self.assertEqual(self.request('POST', '/api/' + route, b'{"profile":"demo"}')[0], 200)
            for route in ('merge', 'refs/change', 'webhooks/register'):
                self.assertEqual(self.request('POST', '/api/' + route, b'{}')[0], 403)
            self.assertEqual(action.call_count, 3)

    def test_stale_snapshot_returns_conflict(self):
        with patch.object(mesh, 'repository_action', side_effect=inspection.StaleState('reload')):
            self.assertEqual(self.request('POST', '/api/preview', b'{}', {'Authorization': 'Bearer ' + 'a' * 32})[0], 409)

    def test_maintenance_rejects_changes_and_releases_on_error(self):
        with patch.object(mesh, 'MAINTENANCE', True):
            self.assertFalse(mesh.save_config({'repositories': []}))
        with self.assertRaises(ValueError):
            mesh.repository_action('preview', {'profile': 'missing'}, {'name': 'admin'})
        self.assertFalse(mesh.MAINTENANCE)

    def test_gitea_signature_is_accepted(self):
        digest = hmac.new(b'secret', b'{}', hashlib.sha256).hexdigest()
        self.assertEqual(self.request('POST', '/hooks/forgejo/demo', b'{}', {'X-Gitea-Signature': digest})[0], 202)


class WorkerGateTests(unittest.TestCase):
    def test_manual_operation_keeps_queue_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            store = management.Control(directory)
            store.enqueue(['demo'], 'test')
            worker = management.Worker(store, lambda: {'repositories': []}, lambda repo: None, threading.Lock(), blocked=lambda: True)
            try:
                worker.tick()
                self.assertEqual(store.jobs()[0]['status'], 'queued')
                self.assertEqual(worker.active, {})
            finally:
                worker.close()


class HookIntegrationTests(unittest.TestCase):
    def test_auto_hooks_run_only_for_real_sync(self):
        repo = dict(name='demo', remotes={'a': 'https://a.example/repo', 'b': 'https://b.example/repo'}, auto_webhooks=True, webhook_secret_env='HOOK_TEST')
        with patch.object(mesh, 'fetch_snapshots', return_value=(None, {'a': {}, 'b': {}}, [], [])), patch.object(mesh, 'load_config', return_value={'public_url': 'https://mesh.example'}), patch.object(mesh, 'control') as factory, patch.dict(mesh.os.environ, {'HOOK_TEST': 'secret'}):
            mesh.synchronize(repo, dry_run=True)
            factory.return_value.register_hooks.assert_not_called()
            factory.return_value.register_hooks.return_value = []
            mesh.synchronize(repo)
            factory.return_value.register_hooks.assert_called_once_with(repo, 'https://mesh.example', 'secret', refresh_secret=False)

    def test_automatic_hooks_require_valid_url_and_all_providers(self):
        repo = dict(name='demo', remotes={'a': 'https://a.example/repo', 'b': 'https://b.example/repo'}, auto_webhooks=True)
        with self.assertRaises(ValueError):
            mesh.validate_config({'repositories': [repo]})
        repo['remote_providers'] = {'a': 'one', 'b': 'two'}
        mesh.validate_config({'repositories': [repo], 'public_url': 'https://mesh.example'})
        with self.assertRaises(ValueError):
            mesh.validate_config({'repositories': [repo], 'public_url': 'http://mesh.example'})
