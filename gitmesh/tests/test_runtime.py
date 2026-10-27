"""Process isolation, worker groups, shared profiles, and encrypted SSH transport."""
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock
from urllib.request import urlopen
from test_sync import mesh
import management
import runtime
import transport


class GroupTests(unittest.TestCase):
    def test_shared_rules_expand_without_changing_saved_mapping(self):
        repo = {'name': 'one', 'profile': 'team', 'remotes': {'source': 'https://github.com/team/one.git', 'target': 'https://forge.example/team/one.git'}}
        second = dict(repo, name='two', remotes={'source': 'https://github.com/team/two.git', 'target': 'https://forge.example/team/two.git'})
        config = {'repositories': [repo, second], 'profiles': {'team': {'direction': 'one-way', 'source': 'source', 'interval': 60, 'worker_set': 'slow'}}, 'worker_sets': {'slow': {'parallelism': 1}}}
        mesh.validate_config(config)
        resolved = mesh.resolve_config(config)
        self.assertTrue(all(r['interval'] == 60 and r['worker_set'] == 'slow' for r in resolved['repositories']))
        self.assertNotIn('interval', repo)
        with self.assertRaises(ValueError):
            mesh.validate_config(dict(config, worker_sets={}))
        with self.assertRaises(ValueError):
            mesh.validate_config(dict(config, profiles={}))

    def test_group_modes_and_filtered_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            store = management.Control(directory)
            store.enqueue(['one', 'two'])
            self.assertEqual(store.claim(['two'])['profile'], 'two')
            self.assertIsNone(store.claim([]))
            self.assertEqual(store.group_mode('slow', 'paused'), 'paused')
            self.assertEqual(management.Control(directory).group_mode('slow'), 'paused')
            self.assertEqual(store.mode(), 'running')

    def test_resource_settings_and_retry_classification(self):
        self.assertEqual(runtime.limits({'worker': {'parallelism': 2, 'min_free_mb': 128}, 'worker_sets': {'a': {'parallelism': 1}}}, 'a'), {'parallelism': 1, 'min_free_mb': 128})
        self.assertTrue(runtime.retryable_push(mesh.GitFailure('push', stderr='Connection reset by peer')))
        self.assertFalse(runtime.retryable_push(mesh.GitFailure('push', stderr='protected branch rejected')))
        self.assertFalse(runtime.retryable_push(mesh.GitFailure('push', stderr='stale info')))

    def test_transient_push_retries_fresh_execution_but_rejection_stops(self):
        for error, expected in [(mesh.GitFailure('push', stderr='Connection reset by peer'), 2), (mesh.GitFailure('push', stderr='protected branch rejected'), 1)]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                store = management.Control(directory)
                store.enqueue(['one'])
                job = store.claim()
                fake = MagicMock()
                fake.control.return_value = store
                fake.load_config.return_value = {'repositories': [{'name': 'one'}]}
                fake.execute_profile.side_effect = [error, ('success', {'updated': []})]
                with patch.object(runtime.time, 'sleep'):
                    runtime.run_child(fake, job['id'])
                self.assertEqual(fake.execute_profile.call_count, expected)
                self.assertEqual(store.jobs()[0]['attempts'], expected)
                self.assertEqual(store.jobs()[0]['status'], 'success' if expected == 2 else 'error')

    def test_cross_process_gate_is_released_after_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            with runtime.file_lock(directory):
                code = "import runtime; runtime.file_lock(" + repr(directory) + ").__enter__()"
                result = subprocess.run([sys.executable, '-c', code], cwd=Path(mesh.__file__).parent, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
            with runtime.file_lock(directory):
                pass


class SshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = management.Control(self.temp.name)
        self.store.save_provider({'id': 'provider', 'name': 'Test', 'kind': 'forgejo', 'url': 'https://forge.example', 'token': 'token'})

    def test_protocol_conversion_and_public_settings(self):
        transport.save(self.store, {'id': 'provider', 'protocol': 'ssh', 'ssh_host': 'ssh.forge.example', 'ssh_port': 2222, 'ssh_user': 'git'})
        repo = {'remotes': {'origin': 'https://forge.example/team/repo.git'}, 'remote_providers': {'origin': 'provider'}}
        self.assertEqual(transport.remote_url(self.store, repo, 'origin'), 'ssh://git@ssh.forge.example:2222/team/repo.git')
        self.assertNotIn('key', transport.settings(self.store, 'provider'))
        self.assertNotIn('key', self.store.providers()[0])

    @unittest.skipUnless(shutil.which('ssh-keygen'), 'ssh-keygen required')
    def test_private_key_encrypted_not_returned_and_materialized_temporarily(self):
        path = Path(self.temp.name) / 'generated'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)], check=True, capture_output=True, timeout=10)
        key = path.read_text()
        public = Path(str(path) + '.pub').read_text().split()
        known = 'forge.example ' + ' '.join(public[:2])
        result = transport.save(self.store, {'id': 'provider', 'protocol': 'ssh', 'key': key, 'known_hosts': known})
        self.assertTrue(result['has_key'])
        self.assertNotIn(key, json.dumps(result))
        with self.store.db() as db:
            self.assertNotIn(key, db.execute('SELECT details FROM provider_transport').fetchone()[0])
        self.assertEqual(transport.settings(self.store, 'provider', reveal=True)['key'], key)
        repo = {'remotes': {'origin': 'https://forge.example/team/repo.git'}, 'remote_providers': {'origin': 'provider'}}
        with transport.ssh_environment(self.store, repo, 'origin') as env:
            self.assertIn('StrictHostKeyChecking=yes', env['GIT_SSH_COMMAND'])
            self.assertIn('IdentitiesOnly=yes', env['GIT_SSH_COMMAND'])
            self.assertNotIn(key, env['GIT_SSH_COMMAND'])
        self.assertTrue(transport.save(self.store, {'id': 'provider', 'protocol': 'ssh', 'known_hosts': known})['has_key'])
        with self.assertRaises(ValueError):
            transport.save(self.store, {'id': 'provider', 'ssh_host': 'other.example'})
        with self.assertRaises(ValueError):
            self.store.save_provider({'id': 'provider', 'name': 'Test', 'kind': 'forgejo', 'url': 'https://other.example', 'token': 'new-token'})
        self.assertFalse(transport.save(self.store, {'id': 'provider', 'clear_key': True})['has_key'])

    def test_invalid_keys_hosts_and_ports_are_rejected(self):
        for change in ({'ssh_port': 0}, {'ssh_host': 'host -oProxyCommand=bad'}, {'known_hosts': 'bad'}, {'key': 'not a key'}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    transport.save(self.store, dict(id='provider', **change))


class ProcessTests(unittest.TestCase):
    def wait_for(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail('Process condition timed out')

    def test_worker_survives_temporary_pending_database_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config=root/'config.json'
            config.write_text(json.dumps({'interval':300,'worker':{'min_free_mb':0},'repositories':[]}))
            fixture=root/'contention_fixture.py'
            fixture.write_text("import sys,sqlite3\nfrom pathlib import Path\nsys.path.insert(0,"+repr(str(Path(mesh.__file__).parent))+")\nimport gitmesh as mesh, runtime, decisions\noriginal=decisions.pending\ncalls=0\ndef pending(store):\n global calls\n calls+=1\n if calls==1: raise sqlite3.OperationalError('database is locked')\n if calls>=3: (mesh.DATA/'recovered').write_text('ok')\n return original(store)\ndecisions.pending=pending\nruntime.supervisor(mesh)\n")
            env=dict(os.environ,GITMESH_DATA=str(root),GITMESH_CONFIG=str(config),GITMESH_ADMIN_TOKEN='a'*32,PYTHONDONTWRITEBYTECODE='1')
            worker=subprocess.Popen([sys.executable,str(fixture)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
            try:
                self.wait_for(lambda:(root/'recovered').exists())
                self.assertIsNone(worker.poll(),'Temporary SQLite lock must not exit the supervisor')
                store=management.Control(root)
                self.wait_for(lambda:runtime.status(store)['online'])
            finally:
                worker.terminate(); worker.wait(timeout=10)

    def test_web_restart_keeps_worker_job_and_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text(json.dumps({'interval': 300, 'worker': {'min_free_mb': 0, 'parallelism': 1}, 'worker_sets': {'team': {'parallelism': 1}}, 'repositories': [
                {'name': 'one', 'worker_set': 'team', 'interval': 0, 'remotes': {'a': 'https://a.example/one.git', 'b': 'https://b.example/one.git'}},
                {'name': 'two', 'worker_set': 'team', 'interval': 0, 'remotes': {'a': 'https://a.example/two.git', 'b': 'https://b.example/two.git'}}]}))
            fixture = root / 'fixture.py'
            fixture.write_text("import sys,time\nfrom pathlib import Path\nsys.path.insert(0," + repr(str(Path(mesh.__file__).parent)) + ")\nimport gitmesh as mesh\nimport runtime\ndef execute(repo):\n Path(mesh.DATA / ('started-' + repo['name'])).write_text('started')\n while not (mesh.DATA / 'release').exists(): time.sleep(.1)\n return 'success', {'updated': []}\nmesh.execute_profile=execute\nmesh.__file__=__file__\nif '--job' in sys.argv: runtime.run_child(mesh,int(sys.argv[-1]))\nelse: runtime.supervisor(mesh)\n")
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            env = dict(os.environ, GITMESH_DATA=str(root), GITMESH_CONFIG=str(config), GITMESH_ROLE='web', GITMESH_ADMIN_TOKEN='a' * 32, GITMESH_HOST='127.0.0.1', GITMESH_PORT=str(port), PYTHONDONTWRITEBYTECODE='1')
            worker = subprocess.Popen([sys.executable, str(fixture)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            web = None
            try:
                def start_web():
                    return subprocess.Popen([sys.executable, str(Path(mesh.__file__))], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                web = start_web()
                def healthy():
                    try:
                        with urlopen('http://127.0.0.1:' + str(port) + '/healthz', timeout=1) as response:
                            return response.status == 200
                    except OSError:
                        return False
                self.wait_for(healthy)
                store = management.Control(root)
                store.enqueue(['one', 'two'])
                self.wait_for(lambda: (root / 'started-one').exists())
                self.assertFalse((root / 'started-two').exists())
                web.terminate(); web.wait(timeout=10)
                self.assertIsNone(worker.poll())
                self.assertEqual(sorted(j['status'] for j in store.jobs()), ['queued', 'running'])
                web = start_web(); self.wait_for(healthy)
                self.assertEqual(sorted(j['status'] for j in store.jobs()), ['queued', 'running'])
                (root / 'release').touch()
                self.wait_for(lambda: all(j['status'] == 'success' for j in store.jobs()))
                self.assertTrue((root / 'started-two').exists())
                if os.name != 'nt':
                    # Linux Docker must run two child processes in parallel, while respecting group pause.
                    (root / 'release').unlink()
                    for name in ('one', 'two'):
                        (root / ('started-' + name)).unlink()
                    updated = json.loads(config.read_text())
                    updated['worker_sets']['team']['parallelism'] = 2
                    replacement = root / 'updated.json'
                    replacement.write_text(json.dumps(updated))
                    replacement.replace(config)
                    store.group_mode('team', 'paused')
                    store.enqueue(['one', 'two'])
                    time.sleep(1.2)
                    self.assertFalse((root / 'started-one').exists())
                    self.assertFalse((root / 'started-two').exists())
                    store.group_mode('team', 'running')
                    self.wait_for(lambda: all((root / ('started-' + name)).exists() for name in ('one', 'two')))
                    self.assertEqual(sum(j['status'] == 'running' for j in store.jobs()), 2)
                    (root / 'release').touch()
                    self.wait_for(lambda: all(j['status'] == 'success' for j in store.jobs()))
            finally:
                (root / 'release').touch()
                if web and web.poll() is None:
                    web.terminate(); web.wait(timeout=10)
                if worker.poll() is None:
                    worker.terminate(); worker.wait(timeout=10)
