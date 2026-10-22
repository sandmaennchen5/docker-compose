"""First-copy default branches are explicit, verified and restart-safe."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_sync import mesh, SyncFixture
import management

class DefaultBranchApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = management.Control(self.temp.name)
        self.repo = {'name': 'demo', 'source': 'github', 'remotes': {'github': 'https://source.example/team/repo.git', 'target': 'https://target.example/team/repo.git'}, 'remote_providers': {'target': 'target'}}
        self.snapshots = {'target': {'heads/main': 'main', 'heads/beta': 'newer'}}

    def provider(self, kind='forgejo'):
        self.store.save_provider({'id': 'target', 'name': 'Target', 'kind': kind, 'url': 'https://target.example', 'token': 'isolated'})

    def queue(self):
        with patch.object(self.store, 'api', return_value={'id': 42, 'default_branch': 'beta'}):
            self.store.queue_initial_default(self.repo, 'target', 'main')

    def test_provider_matrix_sets_main_after_copy_and_verifies(self):
        for kind in ('forgejo', 'github', 'gitlab'):
            with self.subTest(kind=kind):
                self.provider(kind); self.queue()
                with patch.object(self.store, 'api', side_effect=[{'id': 42, 'default_branch': 'beta'}, {}, {'id': 42, 'default_branch': 'main'}]) as api:
                    result = self.store.apply_initial_defaults(self.repo, self.snapshots)
                self.assertEqual(result['errors'], [])
                self.assertEqual(result['items'][0]['branch'], 'main')
                self.assertEqual(api.call_args_list[1].kwargs, {'method': 'PUT' if kind == 'gitlab' else 'PATCH', 'body': {'default_branch': 'main'}})
                with patch.object(self.store, 'api') as api:
                    self.assertEqual(self.store.apply_initial_defaults(self.repo, self.snapshots)['items'], [])
                    api.assert_not_called()

    def test_api_failure_is_reported_and_retried_after_restart(self):
        self.provider(); self.queue()
        with patch.object(self.store, 'api', side_effect=[{'id': 42, 'default_branch': 'beta'}, management.ProviderError('API unavailable', 503)]):
            self.assertTrue(self.store.apply_initial_defaults(self.repo, self.snapshots)['errors'])
        other = management.Control(self.temp.name)
        with patch.object(other, 'api', side_effect=[{'id': 42, 'default_branch': 'beta'}, {}, {'id': 42, 'default_branch': 'main'}]):
            self.assertEqual(other.apply_initial_defaults(self.repo, self.snapshots)['items'][0]['status'], 'updated')

    def test_wrong_readback_is_an_error_and_keeps_pending_initialization(self):
        self.provider(); self.queue()
        with patch.object(self.store, 'api', return_value={'id': 42, 'default_branch': 'beta'}):
            self.assertTrue(self.store.apply_initial_defaults(self.repo, self.snapshots)['errors'])
        with patch.object(self.store, 'api', return_value={'id': 42, 'default_branch': 'main'}) as api:
            self.assertEqual(self.store.apply_initial_defaults(self.repo, self.snapshots)['items'][0]['status'], 'unchanged')
            self.assertEqual(api.call_count, 1)

    def test_filters_missing_branch_and_replaced_repository_never_patch(self):
        self.provider()
        for config, snapshots in [(dict(self.repo, branches=['beta']), self.snapshots), (dict(self.repo, sync_branches=False), self.snapshots), (self.repo, {'target': {'heads/beta': 'newer'}})]:
            self.queue()
            with patch.object(self.store, 'api') as api:
                result = self.store.apply_initial_defaults(config, snapshots)
                self.assertTrue(result['warnings']); api.assert_not_called()
        self.queue()
        with patch.object(self.store, 'api', return_value={'id': 999, 'default_branch': 'beta'}) as api:
            self.assertTrue(self.store.apply_initial_defaults(self.repo, self.snapshots)['warnings'])
            self.assertEqual(api.call_count, 1)
        with patch.object(self.store, 'api') as api:
            self.store.apply_initial_defaults(self.repo, self.snapshots); api.assert_not_called()

class DefaultBranchIntegrationTests(SyncFixture):
    def setup_forgejo(self):
        self.repo.update(source='github', remote_providers={'forgejo': 'fj'})
        self.command(self.work, 'checkout', '-b', 'beta')
        self.commit('beta is newer but is not the default')
        self.command(self.work, 'push', self.remotes['github'], 'beta')
        self.command(self.root / 'github', 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.command(self.root / 'forgejo', 'symbolic-ref', 'HEAD', 'refs/heads/beta')
        self.store = mesh.control()
        self.store.save_provider({'id': 'fj', 'name': 'Forgejo', 'kind': 'forgejo', 'url': 'https://forge.example', 'token': 'isolated'})
        self.events = []; self.fail_patch = False; self.repository_id = 42
        def api(provider, endpoint, query=None, *, method='GET', body=None):
            if method == 'PATCH':
                self.events.append(('patch', body))
                self.assertEqual(body, {'default_branch': 'main'})
                self.assertEqual(self.command(self.root / 'forgejo', 'show-ref', '--verify', 'refs/heads/main').returncode, 0)
                if self.fail_patch: raise management.ProviderError('temporary unavailable', 503)
                self.command(self.root / 'forgejo', 'symbolic-ref', 'HEAD', 'refs/heads/main')
            default = self.command(self.root / 'forgejo', 'symbolic-ref', 'HEAD').stdout.strip().removeprefix('refs/heads/')
            return {'id': self.repository_id, 'default_branch': default}
        self.addCleanup(patch.stopall)
        patch.object(self.store, 'api', side_effect=api).start()
        patch.object(self.store, 'repository_endpoint', return_value='/repos/team/repo').start()
        patch.object(mesh, 'control', return_value=self.store).start()

    def test_forgejo_main_is_explicit_even_if_beta_is_newer(self):
        self.setup_forgejo()
        status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'success')
        self.assertEqual(self.command(self.root / 'forgejo', 'symbolic-ref', 'HEAD').stdout.strip(), 'refs/heads/main')
        self.assertEqual(details['default_branch']['items'][0], {'remote': 'forgejo', 'branch': 'main', 'status': 'updated'})
        self.command(self.root / 'forgejo', 'symbolic-ref', 'HEAD', 'refs/heads/beta')
        self.events.clear()
        mesh.synchronize(self.repo)
        self.assertEqual(self.events, [], 'Established populated repositories keep their chosen default')

    def test_recreated_empty_target_with_new_id_receives_main_again(self):
        self.setup_forgejo()
        self.assertEqual(mesh.synchronize(self.repo)[0], 'success')
        target = self.root / 'forgejo'
        for ref in self.command(target, 'for-each-ref', '--format=%(refname)').stdout.splitlines():
            self.command(target, 'update-ref', '-d', ref)
        self.command(target, 'symbolic-ref', 'HEAD', 'refs/heads/beta')
        self.repository_id = 99
        status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'success')
        self.assertEqual(self.command(target, 'symbolic-ref', 'HEAD').stdout.strip(), 'refs/heads/main')
        self.assertEqual(details['default_branch']['items'][0]['branch'], 'main')

    def test_same_repository_id_with_removed_refs_still_requires_review(self):
        self.setup_forgejo()
        self.assertEqual(mesh.synchronize(self.repo)[0], 'success')
        target = self.root / 'forgejo'
        for ref in self.command(target, 'for-each-ref', '--format=%(refname)').stdout.splitlines():
            self.command(target, 'update-ref', '-d', ref)
        status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'conflict')
        self.assertTrue(details['git_reviews'])
        self.assertEqual(self.command(target, 'show-ref', check=False).stdout, '')

    def test_new_target_with_server_initialized_beta_still_uses_source_main(self):
        self.setup_forgejo()
        self.command(self.work, 'push', self.remotes['forgejo'], 'beta')
        fetch = mesh.fetch_snapshots
        def newly_created(repo, dry_run=False):
            path, snapshots, created, missing = fetch(repo, dry_run)
            return path, snapshots, ['forgejo'], missing
        with patch.object(mesh, 'fetch_snapshots', side_effect=newly_created):
            status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'success')
        self.assertEqual(self.command(self.root / 'forgejo', 'symbolic-ref', 'HEAD').stdout.strip(), 'refs/heads/main')

    def test_failed_initial_identification_keeps_target_empty_for_retry(self):
        self.setup_forgejo()
        with patch.object(self.store, 'queue_initial_default', side_effect=management.ProviderError('temporary unavailable', 503)):
            status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'error')
        self.assertEqual(self.command(self.root / 'forgejo', 'show-ref', check=False).stdout, '')
        self.assertTrue(details['default_branch']['errors'])
        self.assertEqual(mesh.synchronize(self.repo)[0], 'success')
        self.assertEqual(self.command(self.root / 'forgejo', 'symbolic-ref', 'HEAD').stdout.strip(), 'refs/heads/main')

    def test_retry_sets_default_after_refs_were_already_copied(self):
        self.setup_forgejo(); self.fail_patch = True
        status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'error'); self.assertTrue(details['updated'])
        self.assertEqual(self.tip('forgejo'), self.tip('github'))
        self.fail_patch = False
        status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'success'); self.assertEqual(details['updated'], [])
        self.assertEqual(self.command(self.root / 'forgejo', 'symbolic-ref', 'HEAD').stdout.strip(), 'refs/heads/main')

    def test_preview_and_excluded_main_never_modify_default(self):
        self.setup_forgejo()
        mesh.synchronize(self.repo, dry_run=True)
        self.assertEqual(self.events, [])
        self.repo['branches'] = ['beta']
        status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'success'); self.assertEqual(self.events, [])
        self.assertTrue(details['default_branch']['warnings'])
        self.assertEqual(self.command(self.root / 'forgejo', 'symbolic-ref', 'HEAD').stdout.strip(), 'refs/heads/beta')

if __name__ == '__main__': unittest.main()
