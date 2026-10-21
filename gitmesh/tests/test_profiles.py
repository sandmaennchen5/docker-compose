from unittest.mock import patch
from test_sync import SyncFixture, mesh


class ProfileSyncTests(SyncFixture):
    def test_one_way_never_imports_target_only_branches(self):
        mesh.synchronize(self.repo)
        self.command(self.work, 'branch', 'target-only')
        self.command(self.work, 'push', self.remotes['gitlab'], 'target-only')
        self.repo.update(direction='one-way', source='github')
        mesh.synchronize(self.repo)
        self.assertNotEqual(self.command(self.root / 'github', 'show-ref', '--verify', 'refs/heads/target-only', check=False).returncode, 0)
        self.assertNotEqual(self.command(self.root / 'forgejo', 'show-ref', '--verify', 'refs/heads/target-only', check=False).returncode, 0)

    def test_branch_filters_and_disabled_tags(self):
        self.command(self.work, 'branch', 'develop')
        self.command(self.work, 'tag', '-a', 'v1', '-m', 'release')
        self.command(self.work, 'push', self.remotes['github'], 'main', 'develop', 'refs/tags/v1')
        self.repo.update(branches=['main'], tags=False)
        self.assertEqual(mesh.synchronize(self.repo)[0], 'success')
        for name in ('gitlab', 'forgejo'):
            self.tip(name)
            for ref in ('refs/heads/develop', 'refs/tags/v1'):
                self.assertNotEqual(self.command(self.root / name, 'show-ref', '--verify', ref, check=False).returncode, 0)

    def divergent(self):
        mesh.synchronize(self.repo)
        base = self.tip('github')
        left = self.commit('left')
        self.command(self.work, 'push', self.remotes['github'], 'main')
        self.command(self.work, 'branch', 'clean')
        self.command(self.work, 'push', self.remotes['github'], 'clean')
        self.command(self.work, 'reset', '--hard', base)
        right = self.commit('right')
        self.command(self.work, 'push', self.remotes['forgejo'], 'main')
        return left, right

    def test_reject_policy_has_no_partial_updates(self):
        self.divergent()
        before = {r: self.tip(r) for r in self.remotes}
        self.repo['policy'] = 'reject'
        status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'conflict')
        self.assertEqual(details['updated'], [])
        self.assertEqual(before, {r: self.tip(r) for r in self.remotes})
        self.assertNotEqual(self.command(self.root / 'gitlab', 'show-ref', '--verify', 'refs/heads/clean', check=False).returncode, 0)

    def test_one_way_ff_only_preserves_diverged_target(self):
        self.divergent()
        target = self.tip('forgejo')
        source = self.tip('github')
        self.repo.update(direction='one-way', source='github')
        self.assertEqual(mesh.synchronize(self.repo)[0], 'conflict')
        self.assertEqual(self.tip('forgejo'), target)
        self.assertEqual(self.tip('github'), source)

    def test_explicit_one_way_force_with_lease(self):
        left, right = self.divergent()
        self.repo.update(direction='one-way', source='github', policy='force-with-lease', force_confirmed=True)
        self.assertEqual(mesh.synchronize(self.repo)[0], 'success')
        self.assertTrue(all(self.tip(r) == left for r in self.remotes))

    def test_force_lease_rejects_target_changed_after_fetch(self):
        left, right = self.divergent()
        concurrent = self.commit('concurrent change')
        self.repo.update(direction='one-way', source='github', policy='force-with-lease', force_confirmed=True)
        injected = []
        def command(path, *args, check=True):
            if args[0] == 'push' and 'gitlab' in args and not injected:
                injected.append(True)
                self.command(self.work, 'push', self.remotes['gitlab'], concurrent + ':refs/heads/main')
            return self.command(path, *args, check=check)
        with patch.object(mesh, 'git', command):
            with self.assertRaises(AssertionError):
                mesh.synchronize(self.repo)
        self.assertTrue(injected)
        self.assertEqual(self.tip('gitlab'), concurrent)
        self.assertEqual(self.tip('github'), left)
