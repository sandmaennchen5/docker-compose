import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import settings
import web


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        settings.git(self.repo, 'init')
        self.global_patch = patch.object(settings, 'global_config', return_value={})
        self.global_patch.start()
        self.addCleanup(self.global_patch.stop)

    def save(self, **changes):
        payload = dict(branch='main', max_parallel=4, verbosity=1, primary_source='origin')
        payload.update(changes)
        settings.save_settings(self.repo, payload)

    def remote(self, **changes):
        payload = dict(name='backup', url='git@example.com:user/project.git',
                       provider='gitea', username='user', api_url='https://example.com',
                       enabled=True, use_ssh=True)
        payload.update(changes)
        settings.save_remote(self.repo, payload)

    def test_settings_preserve_other_sections_and_backup(self):
        folder = self.repo / '.multigit'
        folder.mkdir()
        original = '[security]\naudit_log = true\n[custom]\nmessage = "hello"\n'
        (folder / 'config.toml').write_text(original, encoding='utf-8')
        self.save(branch='feature/web-settings', max_parallel=8)
        data = settings.read_config(folder / 'config.toml')
        self.assertTrue(data['security']['audit_log'])
        self.assertEqual(data['custom']['message'], 'hello')
        self.assertEqual(settings.load_settings(self.repo)['max_parallel'], 8)
        self.assertEqual((folder / 'config.toml.bak').read_text(), original)
        self.assertEqual(settings.git(self.repo, 'status', '--porcelain').stdout, '')

    def test_remote_updates_git_and_multigit_without_network(self):
        self.remote()
        self.assertEqual(settings.git(self.repo, 'remote', 'get-url', 'backup').stdout.strip(),
                         'git@example.com:user/project.git')
        self.remote(url='https://example.com/user/project.git', use_ssh=False, enabled=False)
        item = settings.load_settings(self.repo)['remotes'][0]
        self.assertFalse(item['enabled'])
        self.assertFalse(item['use_ssh'])
        self.assertEqual(item['url'], 'https://example.com/user/project.git')

    def test_git_url_rolls_back_if_saving_config_fails(self):
        self.remote()
        with patch.object(settings, 'write_config', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.remote(url='https://example.com/new.git')
        self.assertEqual(settings.git(self.repo, 'remote', 'get-url', 'backup').stdout.strip(),
                         'git@example.com:user/project.git')

    def test_invalid_settings_do_not_change_file(self):
        self.save()
        original = settings.config_path(self.repo).read_bytes()
        for changes in ({'max_parallel': 0}, {'max_parallel': True}, {'branch': '--help'},
                        {'branch': 'bad branch'}, {'verbosity': 8}, {'primary_source': '../bad'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.save(**changes)
        self.assertEqual(settings.config_path(self.repo).read_bytes(), original)

    def test_rejects_credential_urls_and_local_transports(self):
        for url in ('https://user:secret@example.com/repo.git',
                    'https://example.com/repo.git?token=secret',
                    'file:///etc/passwd', 'ext::danger', '-option', '/tmp/repo'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.remote(url=url)
        self.assertEqual(settings.git(self.repo, 'remote').stdout, '')

    def test_existing_credentials_are_not_returned(self):
        settings.git(self.repo, 'remote', 'add', 'old', 'https://user:secret@example.com/repo.git')
        data = json.dumps(settings.load_settings(self.repo))
        self.assertNotIn('secret', data)

    def test_command_uses_saved_branch_and_pull_source(self):
        self.save(branch='develop', primary_source='backup')
        self.assertEqual(settings.command_for(self.repo, 'preview', ['sync', '--dry-run']),
                         ['sync', '--dry-run', '--branch', 'develop'])
        self.assertEqual(settings.command_for(self.repo, 'pull', ['pull']),
                         ['pull', '--from', 'backup'])
        self.assertEqual(settings.command_for(self.repo, 'status', ['status']), ['status'])

    def test_invalid_existing_config_is_not_overwritten(self):
        folder = self.repo / '.multigit'
        folder.mkdir()
        target = folder / 'config.toml'
        target.write_text('invalid [ TOML')
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(target.read_text(), 'invalid [ TOML')

    def test_web_auth_and_custom_header_protect_settings(self):
        handler = object.__new__(web.Handler)
        handler.authenticated = Mock(return_value=False)
        handler.reply = Mock()
        handler.path = '/api/settings'
        handler.headers = {}
        handler.do_POST()
        handler.reply.assert_not_called()
        handler.authenticated.return_value = True
        handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0], 403)

    def test_settings_api_round_trip(self):
        handler = object.__new__(web.Handler)
        handler.authenticated = Mock(return_value=True)
        handler.reply = Mock()
        handler.path = '/api/settings'
        payload = dict(repo='project', branch='develop', max_parallel=6,
                       verbosity=2, primary_source='backup')
        body = json.dumps(payload).encode()
        handler.headers = {'Content-Length': str(len(body)), 'X-MultiGit': '1'}
        handler.rfile = io.BytesIO(body)
        with patch.object(web, 'repository', return_value=self.repo):
            handler.do_POST()
        code, data = handler.reply.call_args.args
        self.assertEqual(code, 200)
        self.assertEqual(data['branch'], 'develop')
        handler.path = '/api/settings?repo=project'
        with patch.object(web, 'repository', return_value=self.repo):
            handler.do_GET()
        self.assertEqual(handler.reply.call_args.args[1]['primary_source'], 'backup')


if __name__ == '__main__':
    unittest.main()
