"""Integration tests use temporary local repositories, never configured servers."""
import sys
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
spec = importlib.util.spec_from_file_location('gitmesh', Path(__file__).parents[1] / 'gitmesh.py')
mesh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mesh)
GIT = os.environ.get('GITMESH_TEST_GIT', shutil.which('git'))


@unittest.skipUnless(GIT, 'Git executable required')
class SyncFixture(unittest.TestCase):
    def command(self, path, *args, check=True):
        result = subprocess.run([GIT, '-c', 'protocol.file.allow=always', '-C', str(path), *args],
                                text=True, capture_output=True, timeout=30)
        if check and result.returncode:
            self.fail(result.stderr)
        return result

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.command(self.work, 'init', '-b', 'main')
        self.command(self.work, 'config', 'user.name', 'GitMesh Test')
        self.command(self.work, 'config', 'user.email', 'test@example.invalid')
        self.commit('initial')
        self.remotes = {}
        for name in ('github', 'gitlab', 'forgejo'):
            remote = self.root / name
            remote.mkdir()
            self.command(remote, 'init', '--bare')
            self.remotes[name] = str(remote)
        self.repo = {'name': 'test', 'remotes': self.remotes}
        self.addCleanup(patch.stopall)
        patch.object(mesh, 'DATA', self.root / 'data').start()
        # Production forbids file transport; tests substitute the Git runner only.
        patch.object(mesh, 'git', self.command).start()
        self.command(self.work, 'push', self.remotes['github'], 'main')

    def commit(self, text):
        (self.work / 'content.txt').write_text(text)
        self.command(self.work, 'add', '.')
        self.command(self.work, 'commit', '-m', text)
        return self.command(self.work, 'rev-parse', 'HEAD').stdout.strip()

    def tip(self, remote, ref='refs/heads/main'):
        return self.command(Path(self.remotes[remote]), 'rev-parse', ref).stdout.strip()


class SyncTests(SyncFixture):
    def test_empty_targets_receive_source_default_before_alphabetical_beta(self):
        self.command(self.work, 'checkout', '-b', 'beta')
        self.commit('newest push is on beta')
        self.command(self.work, 'push', self.remotes['github'], 'beta')
        self.command(self.work, 'checkout', 'main')
        self.command(Path(self.remotes['github']), 'symbolic-ref', 'HEAD', 'refs/heads/main')
        pushes = []
        def runner(path, *args, check=True):
            if args[0] == 'push':
                pushes.append((args[-2], args[-1].split(':', 1)[1]))
            return self.command(path, *args, check=check)
        with patch.object(mesh, 'git', runner):
            self.assertEqual(mesh.synchronize(self.repo)[0], 'success')
        for target in ('gitlab', 'forgejo'):
            self.assertEqual([ref for remote, ref in pushes if remote == target], ['refs/heads/main', 'refs/heads/beta'])

    def test_existing_target_default_branch_is_preserved(self):
        mesh.synchronize(self.repo)
        self.command(self.work, 'branch', 'beta')
        self.command(self.work, 'push', self.remotes['github'], 'beta')
        self.command(Path(self.remotes['github']), 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.command(Path(self.remotes['forgejo']), 'symbolic-ref', 'HEAD', 'refs/heads/main')
        mesh.synchronize(self.repo)
        self.assertEqual(self.command(Path(self.remotes['forgejo']), 'symbolic-ref', 'HEAD').stdout.strip(), 'refs/heads/main')

    def test_three_way_sync_and_repeat_are_idempotent(self):
        status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'success')
        self.assertEqual(len(details['updated']), 2)
        self.assertEqual(len({self.tip(r) for r in self.remotes}), 1)
        self.assertEqual(mesh.synchronize(self.repo)[1]['updated'], [])

    def test_updates_from_each_provider_propagate(self):
        mesh.synchronize(self.repo)
        for name in self.remotes:
            tip = self.commit('change from ' + name)
            self.command(self.work, 'push', self.remotes[name], 'main')
            self.assertEqual(mesh.synchronize(self.repo)[0], 'success')
            self.assertTrue(all(self.tip(r) == tip for r in self.remotes))

    def test_divergence_preserves_all_tips(self):
        mesh.synchronize(self.repo)
        base = self.tip('github')
        self.commit('left')
        self.command(self.work, 'push', self.remotes['github'], 'main')
        self.command(self.work, 'reset', '--hard', base)
        self.commit('right')
        self.command(self.work, 'push', self.remotes['forgejo'], 'main')
        before = {r: self.tip(r) for r in self.remotes}
        status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'conflict')
        self.assertIn('heads/main', details['conflicts'])
        self.assertEqual(before, {r: self.tip(r) for r in self.remotes})

    def test_tag_conflict_is_not_overwritten(self):
        mesh.synchronize(self.repo)
        self.command(self.work, 'tag', '-a', 'v1', '-m', 'first tag')
        self.command(self.work, 'push', self.remotes['github'], 'refs/tags/v1')
        mesh.synchronize(self.repo)
        self.command(self.work, 'tag', '-f', '-a', 'v1', '-m', 'different tag')
        self.command(self.work, 'push', '--force', self.remotes['forgejo'], 'refs/tags/v1')
        before = {r: self.tip(r, 'refs/tags/v1') for r in self.remotes}
        self.assertEqual(mesh.synchronize(self.repo)[0], 'conflict')
        self.assertEqual(before, {r: self.tip(r, 'refs/tags/v1') for r in self.remotes})

    def test_failed_fetch_prevents_all_pushes(self):
        mesh.synchronize(self.repo)
        before = self.tip('gitlab')
        self.commit('new change')
        self.command(self.work, 'push', self.remotes['github'], 'main')
        self.repo['remotes']['forgejo'] = str(self.root / 'missing')
        with self.assertRaises(AssertionError):
            mesh.synchronize(self.repo)
        self.assertEqual(self.tip('gitlab'), before)


if __name__ == '__main__':
    unittest.main()
