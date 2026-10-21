import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from catalog import CATALOG, build_command
import overview
import settings
import terminal
import web


class CatalogueTests(unittest.TestCase):
    def test_every_native_top_level_function_is_available(self):
        expected = set('init setup add create remote push pull fetch sync status branch tag conflict daemon doctor cc log switch stash undo amend changelog stats merge backup mirror session dashboard template hooks alias commit diff show reset revert cherry-pick rebase blame grep version'.split())
        actual = {item['argv'][0] for item in CATALOG.values() if item['argv'][0] and not item['argv'][0].startswith('-')}
        self.assertEqual(actual, expected)

    def test_arguments_preserve_quoting_and_are_not_shell_commands(self):
        argv, writing = build_command('commit', '--message "a message; $(echo hello)"')
        self.assertEqual(argv, ['commit', '--message', 'a message; $(echo hello)'])
        self.assertTrue(writing)
        self.assertFalse(build_command('reset', '--help')[1])
        self.assertTrue(build_command('diff', '--output file')[1])
        with self.assertRaises(ValueError):
            build_command('git', '-c arbitrary')
        with self.assertRaises(ValueError):
            build_command('missing', '')


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        settings.git(self.repo, 'init')
        settings.git(self.repo, 'config', 'user.name', 'Test User')
        settings.git(self.repo, 'config', 'user.email', 'test@example.com')
        self.global_patch = patch.object(settings, 'global_config', return_value={})
        self.global_patch.start()
        self.addCleanup(self.global_patch.stop)

    def test_status_handles_empty_repo_then_commits_and_changes(self):
        data = overview.overview(self.repo)
        self.assertTrue(data['clean'])
        self.assertEqual(data['commits'], [])
        (self.repo / 'a file.txt').write_text('hello')
        settings.git(self.repo, 'add', '--', 'a file.txt')
        settings.git(self.repo, 'commit', '-m', 'First commit')
        settings.git(self.repo, 'tag', 'v1')
        settings.git(self.repo, 'remote', 'add', 'origin', 'https://example.com/repo.git')
        (self.repo / 'a file.txt').write_text('changed')
        data = overview.overview(self.repo)
        self.assertFalse(data['clean'])
        self.assertEqual(data['files'][0]['path'], 'a file.txt')
        self.assertEqual(data['commits'][0]['subject'], 'First commit')
        self.assertEqual(data['tags'], ['v1'])
        self.assertIsNone(data['remotes'][0]['behind'])

    def test_full_config_preserves_comments_and_detects_conflicting_edits(self):
        before = settings.config_document(self.repo, 'repo')
        text = '# keep this comment\n[settings]\nmax_parallel = 8\n[sync]\nstrategy = "rebase"\n[security]\nauth_backend = "environment"\nallow_env_tokens = true\n[daemon]\ninterval_seconds = 600\n'
        saved = settings.save_document(self.repo, dict(scope='repo', text=text, revision=before['revision']))
        self.assertEqual(saved['text'], text)
        with self.assertRaisesRegex(ValueError, 'inzwischen'):
            settings.save_document(self.repo, dict(scope='repo', text='', revision=before['revision']))
        for invalid in ('[settings]\nmax_parallel = true', '[sync]\nstrategy = "invalid"', '[daemon]\nenabled = true\ninterval_seconds = 0'):
            with self.assertRaises(ValueError):
                settings.save_document(self.repo, dict(scope='repo', text=invalid, revision=saved['revision']))
        self.assertEqual(settings.config_document(self.repo, 'repo')['text'], text)

    def test_global_config_is_separate_and_backed_up(self):
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.repo / 'global')}):
            before = settings.config_document(self.repo, 'global')
            first = settings.save_document(self.repo, dict(scope='global', text='[settings]\nverbosity=2\n', revision=before['revision']))
            settings.save_document(self.repo, dict(scope='global', text='[settings]\nverbosity=3\n', revision=first['revision']))
            self.assertTrue((self.repo / 'global/multigit/config.toml.bak').exists())
            self.assertFalse((self.repo / '.multigit/config.toml').exists())

    def handler(self, route, payload):
        handler = object.__new__(web.Handler)
        handler.authenticated = Mock(return_value=True)
        handler.reply = Mock()
        handler.path = route
        data = json.dumps(payload).encode()
        handler.headers = {'Content-Length': str(len(data)), 'X-MultiGit': '1'}
        handler.rfile = io.BytesIO(data)
        return handler

    def test_terminal_api_requires_explicit_confirmation(self):
        handler = self.handler('/api/terminal/start', dict(repo='test', operation='reset', arguments='--hard'))
        with patch.object(web, 'repository', return_value=self.repo), patch.object(web.SESSIONS, 'start') as start:
            handler.do_POST()
            start.assert_not_called()
            self.assertEqual(handler.reply.call_args.args[0], 400)

    def test_writing_settings_is_blocked_during_running_command(self):
        handler = self.handler('/api/settings', dict(repo='test', branch='main', max_parallel=4, verbosity=1, primary_source='origin'))
        with patch.object(web, 'repository', return_value=self.repo), patch.object(web.SESSIONS, 'busy', return_value=True):
            handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0], 409)
        self.assertFalse((self.repo / '.multigit').exists())

    def test_terminal_keys_are_not_blocked_by_status_lock(self):
        handler = self.handler('/api/terminal/input', dict(id='session', data='hello\r'))
        item = Mock()
        web.LOCK.acquire()
        try:
            with patch.object(web.SESSIONS, 'get', return_value=item):
                handler.do_POST()
        finally:
            web.LOCK.release()
        item.write.assert_called_once_with('hello\r')
        self.assertEqual(handler.reply.call_args.args[0], 200)

    def test_global_cli_options_are_forwarded_before_subcommand(self):
        handler = self.handler('/api/terminal/start', dict(repo='test', operation='version', options=dict(json=True,no_color=True,verbosity=4)))
        with patch.object(web, 'repository', return_value=self.repo), patch.object(web.SESSIONS, 'start', return_value=Mock(id='session')) as start:
            handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0], 200)
        self.assertEqual(start.call_args.args[1], ['--json','--no-color','-vvvv','version'])

    def test_disabled_native_default_daemon_config_can_be_saved(self):
        original = settings.config_document(self.repo, 'repo')
        text = '[daemon]\nenabled = false\ninterval_seconds = 0\nbackground = false\n'
        saved = settings.save_document(self.repo,dict(scope='repo',text=text,revision=original['revision']))
        self.assertEqual(saved['text'],text)

    def test_repository_targets_cannot_escape_or_overwrite(self):
        with patch.object(web, 'ROOT', self.repo):
            for invalid in ('../escape', '/absolute', '..', 'bad/name', 'bad name'):
                with self.assertRaises(ValueError):
                    web.new_repository(invalid)
            (self.repo / 'exists').mkdir()
            with self.assertRaises(ValueError):
                web.new_repository('exists')
            self.assertEqual(web.new_repository('new-project'), self.repo / 'new-project')


class TerminalTests(unittest.TestCase):
    def test_output_buffer_and_cursor_are_bounded(self):
        session = object.__new__(terminal.Session)
        session.id='test'; session.label='Output'; session.started=0; session.background=False
        session.lock=threading.Lock(); session.output=''; session.offset=0
        session.done=False; session.code=None
        with patch.object(terminal, 'MAX_OUTPUT', 10):
            session._append('0123456789abcdef')
        snapshot = session.snapshot(0)
        self.assertEqual(snapshot['output'], '6789abcdef')
        self.assertTrue(snapshot['lost'])
        self.assertEqual(snapshot['cursor'], 16)
        self.assertEqual(session.snapshot(16)['output'], '')

    def test_one_writer_with_concurrent_readers_and_daemon_stop(self):
        manager = terminal.Sessions()
        self.addCleanup(manager.close)
        def fake(cwd, argv, label, repo_key, mutates, **options):
            item=Mock(id=str(len(manager.items)), repo_key=repo_key, mutates=mutates, background=False, touched=time.monotonic(), done=False)
            item.process.poll.return_value=None
            return item
        with patch.object(terminal, 'Session', side_effect=fake):
            manager.start('.', ['sync'], 'Sync', 'repo', True)
            with self.assertRaises(ValueError):
                manager.start('.', ['commit'], 'Commit', 'other-repo', True)
            manager.start('.', ['status'], 'Status', 'repo', False)
            manager.start('.', ['-v','daemon','stop'], 'Stop', 'repo', True)
            self.assertTrue(manager.busy())

    @unittest.skipUnless(os.name == 'posix', 'Real PTY integration runs on Linux CI')
    def test_real_pty_input_resize_and_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=Path(directory)/'worker.py'
            worker.write_text('import os,fcntl,termios,struct\nfcntl.ioctl(0,termios.TIOCSCTTY,0)\nprint("TTY="+str(os.isatty(0)),flush=True)\nvalue=input("name? ")\nsize=struct.unpack("HHHH",fcntl.ioctl(0,termios.TIOCGWINSZ,bytes(8)))\nprint("HELLO "+value+" SIZE="+str(size[:2]),flush=True)\n')
            with patch.object(terminal, 'WORKER', worker):
                item=terminal.Session(Path(directory), [], 'Test', 'repo', True)
            self.addCleanup(item.stop)
            deadline=time.monotonic()+5
            while 'name?' not in item.snapshot()['output'] and time.monotonic()<deadline:
                time.sleep(.02)
            item.resize(90,24)
            item.write('Ada\r')
            while not item.done and time.monotonic()<deadline:
                time.sleep(.02)
            data=item.snapshot()
            self.assertTrue(data['done'])
            self.assertEqual(data['code'],0)
            self.assertIn('TTY=True',data['output'])
            self.assertIn('HELLO Ada SIZE=(24, 90)',data['output'])

    @unittest.skipUnless(os.name == 'posix', 'Process-group cancellation runs on Linux CI')
    def test_stubborn_pty_process_can_be_force_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=Path(directory)/'worker.py'
            worker.write_text('import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nprint("READY",flush=True)\ntime.sleep(120)\n')
            with patch.object(terminal,'WORKER',worker):
                item=terminal.Session(Path(directory),[],'Stubborn','repo',True)
            self.addCleanup(item.stop)
            deadline=time.monotonic()+5
            while 'READY' not in item.snapshot()['output'] and time.monotonic()<deadline:
                time.sleep(.02)
            item.stop()
            deadline=time.monotonic()+6
            while not item.done and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertTrue(item.done)
            self.assertEqual(item.code,-9)


if __name__ == '__main__':
    unittest.main()
