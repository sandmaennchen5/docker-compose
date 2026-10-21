"""Automatic provisioning uses mocked provider APIs and temporary local Git repositories."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock
from test_sync import mesh, SyncFixture
from test_http import HttpCase
import management


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = management.Control(self.temp.name)

    def provider(self, kind='forgejo'):
        self.store.save_provider({'id': 'target', 'name': 'Target', 'kind': kind,
                                  'url': 'https://target.example', 'token': 'test-token'})
        return self.store.provider('target', True)

    def test_existing_repository_is_never_changed(self):
        self.provider()
        with patch.object(self.store, 'api', return_value={'private': False}) as api:
            self.assertFalse(self.store.ensure_repository('target', 'https://target.example/team/repo.git', True))
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[1], '/repos/team/repo')
        self.assertNotIn('method', api.call_args.kwargs)

    def test_forgejo_own_account_and_organization_creation(self):
        for owner, endpoint in [('alice', '/user/repos'), ('team', '/orgs/team/repos')]:
            with self.subTest(owner=owner):
                self.provider()
                with patch.object(self.store, 'api', side_effect=[
                    management.ProviderError('not found', 404), {'login': 'alice'}, {'full_name': owner + '/repo'}
                ]) as api:
                    self.assertTrue(self.store.ensure_repository('target', 'https://target.example/' + owner + '/repo.git'))
                self.assertEqual(api.call_args.args[1], endpoint)
                self.assertEqual(api.call_args.kwargs, {'method': 'POST', 'body': {'name': 'repo', 'private': True, 'auto_init': False}})

    def test_github_creation_can_explicitly_be_public(self):
        self.provider('github')
        with patch.object(self.store, 'api', side_effect=[management.ProviderError('404', 404), {'login': 'alice'}, {}]) as api:
            self.assertTrue(self.store.ensure_repository('target', 'https://target.example/alice/repo.git', False))
        self.assertFalse(api.call_args.kwargs['body']['private'])
        self.assertFalse(api.call_args.kwargs['body']['auto_init'])

    def test_gitlab_nested_namespace_resolves_exact_id(self):
        self.provider('gitlab')
        with patch.object(self.store, 'api', side_effect=[
            management.ProviderError('404', 404), {'full_path': 'group/sub', 'id': 42}, {}
        ]) as api:
            self.assertTrue(self.store.ensure_repository('target', 'https://target.example/group/sub/repo.git'))
        self.assertEqual(api.call_args_list[0].args[1], '/projects/group%2Fsub%2Frepo')
        self.assertEqual(api.call_args_list[1].args[1], '/namespaces/group%2Fsub')
        self.assertEqual(api.call_args.kwargs['body'], {'name': 'repo', 'path': 'repo', 'namespace_id': 42,
                                                       'visibility': 'private', 'initialize_with_readme': False})

    def test_no_creation_on_auth_or_server_errors(self):
        self.provider()
        for status in (401, 403, 500, None):
            with self.subTest(status=status):
                with patch.object(self.store, 'api', side_effect=management.ProviderError('failed', status)) as api:
                    with self.assertRaises(management.ProviderError):
                        self.store.ensure_repository('target', 'https://target.example/team/repo.git')
                self.assertEqual(api.call_count, 1)

    def test_concurrent_creation_is_rechecked(self):
        self.provider()
        with patch.object(self.store, 'api', side_effect=[management.ProviderError('404', 404),
                              {'login': 'alice'}, management.ProviderError('conflict', 409), {}]) as api:
            self.assertFalse(self.store.ensure_repository('target', 'https://target.example/team/repo.git'))
        self.assertEqual(api.call_args.args[1], '/repos/team/repo')

    def test_unrelated_post_error_is_not_hidden(self):
        self.provider()
        with patch.object(self.store, 'api', side_effect=[management.ProviderError('404', 404),
                              {'login': 'alice'}, management.ProviderError('invalid', 422), management.ProviderError('404', 404)]):
            with self.assertRaises(management.ProviderError) as caught:
                self.store.ensure_repository('target', 'https://target.example/team/repo.git')
        self.assertEqual(caught.exception.status, 422)

    def test_target_must_belong_to_configured_host_and_safe_namespace(self):
        self.provider()
        for url in ('https://other.example/team/repo.git', 'https://target.example/team/../repo.git',
                    'https://target.example/team/%2e%2e/repo.git', 'https://target.example/team/repo.git?x=1',
                    'https://target.example/repo.git'):
            with self.subTest(url=url):
                with patch.object(self.store, 'api') as api:
                    with self.assertRaises(ValueError):
                        self.store.ensure_repository('target', url)
                api.assert_not_called()

    def test_no_implicit_tokenless_creation(self):
        self.store.save_provider({'id': 'target', 'name': 'Target', 'kind': 'forgejo', 'url': 'https://target.example'})
        with patch.object(self.store, 'api') as api:
            with self.assertRaises(management.ProviderError):
                self.store.ensure_repository('target', 'https://target.example/team/repo.git')
        api.assert_not_called()

    def test_provider_subpath_and_ssh_clone_path(self):
        provider = {'kind': 'forgejo', 'url': 'https://target.example/forge'}
        self.assertEqual(management.Control.repository_path(provider, 'https://target.example/forge/team/repo.git'), ('team', 'repo'))
        self.assertEqual(management.Control.repository_path(provider, 'ssh://git@target.example:2222/team/repo.git'), ('team', 'repo'))
        with self.assertRaises(ValueError):
            management.Control.repository_path(provider, 'https://target.example/other/team/repo.git')

    def test_api_post_sends_json_and_preserves_http_status(self):
        provider = self.provider()
        response, opener = MagicMock(), MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        opener.open.return_value = response
        with patch.object(management, 'build_opener', return_value=opener):
            self.store.api(provider, '/user/repos', method='POST', body={'name': 'repo', 'auto_init': False})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(json.loads(request.data), {'name': 'repo', 'auto_init': False})
        self.assertEqual(request.get_header('Content-type'), 'application/json')


class ProvisioningSettingsTests(HttpCase):
    def test_setting_requires_source_and_target_provider(self):
        config = mesh.load_config()
        config['repositories'][0]['create_missing'] = True
        with self.assertRaises(ValueError):
            mesh.validate_config(config)
        config['repositories'][0]['source'] = 'a'
        with self.assertRaises(ValueError):
            mesh.validate_config(config)
        mesh.control().save_provider({'id': 'target', 'name': 'Target', 'kind': 'forgejo',
                                      'url': 'https://example.org', 'token': 'test-token'})
        config['repositories'][0]['remote_providers'] = {'b': 'target'}
        config['repositories'][0]['remotes']['b'] = 'https://example.org/team/b.git'
        self.assertTrue(mesh.save_config(config))
        self.assertTrue(mesh.load_config()['repositories'][0]['create_missing'])

    def test_creation_options_must_be_boolean(self):
        config = mesh.load_config()
        config['repositories'][0]['create_private'] = 'false'
        with self.assertRaises(ValueError):
            mesh.validate_config(config)


class ProvisioningOrderTests(SyncFixture):
    def configure(self):
        self.repo.update(create_missing=True, source='github',
                         remote_providers={'gitlab': 'gl', 'forgejo': 'fj'})
        # The source is deliberately not first in the configuration.
        self.repo['remotes'] = {name: self.remotes[name] for name in ('forgejo', 'github', 'gitlab')}

    def test_source_fetched_before_target_creation_and_initial_push(self):
        self.configure()
        events = []
        def command(path, *args, check=True):
            if args[0] == 'fetch':
                events.append(('fetch', args[3] if args[1] == '--prune' else args[1]))
            return self.command(path, *args, check=check)
        store = MagicMock()
        store.db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = None
        store.ensure_repository.side_effect = lambda identifier, url, private: events.append(('create', identifier)) or True
        with patch.object(mesh, 'git', command), patch.object(mesh, 'control', return_value=store):
            status, details = mesh.synchronize(self.repo)
        self.assertEqual(events[0], ('fetch', 'github'))
        self.assertEqual(status, 'success')
        self.assertEqual(set(details['created_repositories']), {'forgejo', 'gitlab'})
        self.assertEqual(len({self.tip(remote) for remote in self.remotes}), 1)

    def test_source_fetch_failure_never_creates_targets(self):
        self.configure()
        def command(path, *args, check=True):
            if args[0] == 'fetch':
                raise mesh.GitFailure('fetch', 'github')
            return self.command(path, *args, check=check)
        store = MagicMock()
        with patch.object(mesh, 'git', command), patch.object(mesh, 'control', return_value=store):
            with self.assertRaises(mesh.GitFailure):
                mesh.synchronize(self.repo)
        store.ensure_repository.assert_not_called()


class ProvisioningRetryTests(unittest.TestCase):
    def test_created_targets_remain_visible_after_successful_retry(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            store = management.Control(directory)
            error = management.ProviderError('temporary failure', 503)
            error.details['created_repositories'] = ['forgejo']
            execute = MagicMock(side_effect=[error, ('success', {'created_repositories': [], 'updated': []})])
            worker = management.Worker(store, lambda: {}, execute, threading.Lock())
            try:
                store.enqueue(['profile'])
                with patch.object(worker.stop_event, 'wait', return_value=False):
                    worker.work(store.claim(), {'name': 'profile'})
                self.assertEqual(store.jobs()[0]['status'], 'success')
                self.assertEqual(store.jobs()[0]['details']['created_repositories'], ['forgejo'])
            finally:
                worker.close()
