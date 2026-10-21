"""Release metadata, binary streaming and idempotence use only isolated APIs."""
from email.parser import BytesParser
from email.policy import default
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock
from urllib.parse import urlsplit, parse_qs
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, HTTPServer
from test_sync import mesh
import management
import releases


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = management.Control(self.temp.name)
        for identifier, kind in [('source', 'github'), ('target', 'forgejo')]:
            self.store.save_provider({'id': identifier, 'name': identifier, 'kind': kind, 'url': 'https://' + identifier + '.example', 'token': 'isolated-' + identifier})
        self.engine = releases.ReleaseSync(self.store)
        self.release = {'id': 1, 'tag_name': 'v1.0', 'name': 'Version 1', 'body': 'Release notes', 'draft': False, 'prerelease': True}
        self.repo = {'name': 'demo', 'source': 'source', 'releases': True, 'remotes': {'source': 'https://source.example/team/repo.git', 'target': 'https://target.example/team/repo.git'}, 'remote_providers': {'source': 'source', 'target': 'target'}}

    def test_create_update_and_no_change_for_each_destination(self):
        for kind in ('github', 'forgejo', 'gitlab'):
            with self.subTest(kind=kind):
                provider = {'kind': kind}
                fields = self.engine.metadata(provider, self.release)
                expected = dict(fields, id=8, tag_name='v1.0')
                with patch.object(self.store, 'api', side_effect=[management.ProviderError('not found', 404), expected]) as api:
                    result, status = self.engine.upsert(provider, '/project', self.release)
                self.assertEqual(status, 'created')
                self.assertEqual(api.call_args.kwargs['method'], 'POST')
                old = dict(expected, name='Old title')
                with patch.object(self.store, 'api', side_effect=[old, expected]) as api:
                    self.assertEqual(self.engine.upsert(provider, '/project', self.release)[1], 'updated')
                    self.assertEqual(api.call_args.kwargs['method'], 'PUT' if kind == 'gitlab' else 'PATCH')
                with patch.object(self.store, 'api', return_value=expected) as api:
                    self.assertEqual(self.engine.upsert(provider, '/project', self.release)[1], 'unchanged')
                    self.assertEqual(api.call_count, 1)

    def test_missing_or_conflicting_tag_never_creates_release(self):
        self.repo['release_assets'] = False
        with patch.object(self.engine, 'pages', return_value=[self.release]), patch.object(self.engine, 'upsert') as upsert:
            result = self.engine.sync(self.repo, {'source': {'tags/v1.0': 'source'}, 'target': {'tags/v1.0': 'different'}})
            self.assertTrue(result['warnings'])
            upsert.assert_not_called()

    def test_draft_is_not_accidentally_published_on_gitlab(self):
        self.store.save_provider({'id': 'target', 'name': 'GitLab', 'kind': 'gitlab', 'url': 'https://target.example', 'token': 'isolated-target'})
        self.repo['release_assets'] = False
        with patch.object(self.engine, 'pages', return_value=[dict(self.release, draft=True)]), patch.object(self.engine, 'upsert') as upsert:
            result = self.engine.sync(self.repo, {'source': {'tags/v1.0': 'same'}, 'target': {'tags/v1.0': 'same'}})
            self.assertTrue(result['warnings'])
            upsert.assert_not_called()

    def test_release_settings_require_source_tags_and_providers(self):
        mesh.validate_config({'repositories': [self.repo]})
        for changes in ({'tags': False}, {'source': ''}, {'remote_providers': {}}, {'release_max_asset_mb': 0}, {'releases': 'true'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                mesh.validate_config({'repositories': [dict(self.repo, **changes)]})

    def test_existing_unmanaged_asset_is_never_deleted(self):
        source, target = self.store.provider('source', True), self.store.provider('target', True)
        with patch.object(releases, 'stream_download') as download, patch.object(self.engine, 'send_asset') as send:
            status = self.engine.copy_asset(self.repo, source, '/repos/team/repo', self.release, {'id': 1, 'name': 'app.zip'}, 'target', target, '/repos/team/repo', {'id': 2, 'tag_name': 'v1.0'}, {'id': 99})
        self.assertEqual(status, 'conflict')
        download.assert_not_called(); send.assert_not_called()

    def test_github_asset_upload_uses_upload_host_and_binary_body(self):
        provider = {'id': 'target', 'kind': 'github', 'url': 'https://github.com', 'token': 'token'}
        with patch.object(releases, 'upload', return_value={'id': 1}) as upload:
            self.engine.send_asset(provider, '/repos/team/repo', {'id': 9}, '/unused', 'app.zip')
        self.assertTrue(upload.call_args.args[1].startswith('https://uploads.github.com/repos/team/repo/releases/9/assets?'))
        self.assertIsNone(upload.call_args.kwargs['field'])

    def test_failed_replacement_upload_keeps_old_attachment(self):
        provider = self.store.provider('target', True)
        with patch.object(releases, 'upload', side_effect=management.ProviderError('upload failed')), patch.object(self.store, 'api') as api:
            with self.assertRaises(management.ProviderError):
                self.engine.send_asset(provider, '/repos/team/repo', {'id': 9}, '/unused', 'app.zip', {'id': 8})
            api.assert_not_called()

    def test_gitlab_upload_creates_or_updates_link_without_deletion(self):
        provider = {'id': 'target', 'kind': 'gitlab', 'url': 'https://gitlab.example', 'token': 'token'}
        for previous in (None, {'id': 3}):
            with patch.object(releases, 'upload', return_value={'full_path': '/team/repo/uploads/hash/app.zip'}), patch.object(self.store, 'api', return_value={'id': 3}) as api:
                self.engine.send_asset(provider, '/projects/team%2Frepo', {'tag_name': 'v1.0'}, '/unused', 'app.zip', previous)
                self.assertEqual(api.call_args.kwargs['method'], 'PUT' if previous else 'POST')
                self.assertEqual(api.call_args.kwargs['body']['url'], 'https://gitlab.example/team/repo/uploads/hash/app.zip')

    def test_release_enabled_webhooks_subscribe_to_release_events(self):
        for kind in ('github', 'forgejo', 'gitlab'):
            self.store.save_provider({'id': 'hook', 'name': 'Hook', 'kind': kind, 'url': 'https://hooks.example', 'token': 'isolated-token'})
            repo = {'name': 'demo', 'releases': True, 'remotes': {'origin': 'https://hooks.example/team/repo.git'}, 'remote_providers': {'origin': 'hook'}}
            with patch.object(self.store, 'api', side_effect=[[], {}]) as api:
                self.store.register_hooks(repo, 'https://mesh.example', 'isolated-hook-secret')
            body = api.call_args.kwargs['body']
            if kind == 'gitlab':
                self.assertTrue(body['release_events'])
            else:
                self.assertEqual(body['events'], ['push', 'release'])

    def test_foreign_download_hosts_and_downgrades_are_rejected(self):
        provider = self.store.provider('source', True)
        self.assertFalse(releases.trusted(provider, 'https://foreign.example/app.zip', download=True))
        self.assertFalse(releases.trusted(provider, 'http://source.example/app.zip', download=True))
        with self.assertRaises(management.ProviderError):
            releases.stream_download(provider, 'https://foreign.example/app.zip', '/unused', 100)


class ReleaseDownloadTests(unittest.TestCase):
    def test_github_redirect_does_not_forward_authorization_to_cdn(self):
        provider = {'kind': 'github', 'url': 'https://github.com', 'token': 'private-test-token'}
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {'Content-Length': '3'}
        response.read.side_effect = [b'abc', b'']
        redirect = HTTPError('https://api.github.com/asset', 302, 'Redirect', {'Location': 'https://release-assets.githubusercontent.com/asset?signature=test'}, None)
        opener = MagicMock()
        opener.open.side_effect = [redirect, response]
        with tempfile.TemporaryDirectory() as directory, patch.object(releases, 'build_opener', return_value=opener):
            digest = releases.stream_download(provider, 'https://api.github.com/asset', Path(directory) / 'asset', 100)
        self.assertEqual(digest, hashlib.sha256(b'abc').hexdigest())
        first, second = [call.args[0] for call in opener.open.call_args_list]
        self.assertIn('private-test-token', first.get_header('Authorization'))
        self.assertIsNone(second.get_header('Authorization'))

    def test_repository_scope_is_checked_before_forwarding_source_token(self):
        with patch.object(releases, 'build_opener') as opener, self.assertRaisesRegex(management.ProviderError, 'ausserhalb'):
            releases.stream_download({'kind': 'gitlab', 'url': 'https://gitlab.example', 'token': 'token'}, 'https://gitlab.example/api/v4/user', '/unused', 100, allowed_paths=('/team/repo/', '/api/v4/projects/team%2Frepo/'))
        opener.assert_not_called()
        for path in ('/team/repo/../../api/user', '/team/repo/%2e%2e/secret', '/team/repo/%252e%252e/secret'):
            self.assertFalse(releases.repository_scoped('https://gitlab.example' + path, ('/team/repo/',)))

    def test_streaming_rejects_oversized_and_truncated_files(self):
        for length, data, maximum in [('10', b'abc', 100), (None, b'abcdef', 3)]:
            with self.subTest(length=length), tempfile.TemporaryDirectory() as directory:
                response = MagicMock(); response.__enter__.return_value = response
                response.headers = {'Content-Length': length} if length else {}
                response.read.side_effect = [data, b'']
                opener = MagicMock(); opener.open.return_value = response
                with patch.object(releases, 'build_opener', return_value=opener), self.assertRaises(management.ProviderError):
                    releases.stream_download({'kind': 'forgejo', 'url': 'https://forge.example', 'token': 'token'}, 'https://forge.example/asset', Path(directory) / 'asset', maximum)


class LocalReleaseApiTests(unittest.TestCase):
    def test_binary_copy_repeat_metadata_update_and_safe_asset_replacement(self):
        state = {'release': None, 'assets': {}, 'binary': b'first binary\x00data', 'version': 1, 'uploads': 0, 'deletions': []}
        class Api(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def reply(self, status, data, binary=False):
                payload = data if binary else json.dumps(data).encode()
                self.send_response(status); self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
            def do_GET(self):
                path = urlsplit(self.path).path
                if path == '/api/v1/repos/source/repo/releases':
                    return self.reply(200, [{'id': 1, 'tag_name': 'v1', 'name': 'Title ' + str(state['version']), 'body': 'Notes', 'draft': False, 'prerelease': False}])
                if path == '/api/v1/repos/source/repo/releases/1/assets':
                    return self.reply(200, [{'id': state['version'], 'name': 'app.zip', 'size': len(state['binary']), 'updated_at': str(state['version']), 'digest': None, 'download_count': state['uploads']}])
                if path == '/source/repo/releases/download/v1/app.zip':
                    return self.reply(200, state['binary'], True)
                if path == '/api/v1/repos/target/repo/releases/tags/v1':
                    return self.reply(200, state['release']) if state['release'] else self.reply(404, {})
                if path == '/api/v1/repos/target/repo/releases/2/assets':
                    return self.reply(200, [{key: value for key, value in asset.items() if key != 'data'} for asset in state['assets'].values()])
                return self.reply(404, {})
            def do_POST(self):
                path = urlsplit(self.path).path
                raw = self.rfile.read(int(self.headers['Content-Length']))
                if path == '/api/v1/repos/target/repo/releases':
                    state['release'] = dict(json.loads(raw), id=2)
                    return self.reply(201, state['release'])
                if path == '/api/v1/repos/target/repo/releases/2/assets':
                    parsed = BytesParser(policy=default).parsebytes(('Content-Type: ' + self.headers['Content-Type'] + '\r\n\r\n').encode() + raw)
                    part = next(parsed.iter_parts())
                    assert part.get_param('name', header='content-disposition') == 'attachment'
                    state['uploads'] += 1; identifier = 100 + state['uploads']
                    asset = {'id': identifier, 'name': parse_qs(urlsplit(self.path).query)['name'][0], 'data': part.get_payload(decode=True)}
                    state['assets'][identifier] = asset
                    return self.reply(201, {'id': identifier, 'name': asset['name']})
                return self.reply(404, {})
            def do_PATCH(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/api/v1/repos/target/repo/releases/2':
                    state['release'].update(body); return self.reply(200, state['release'])
                identifier = int(self.path.rsplit('/', 1)[-1]); state['assets'][identifier].update(body)
                return self.reply(200, {key: value for key, value in state['assets'][identifier].items() if key != 'data'})
            def do_DELETE(self):
                state['deletions'].append(self.path)
                state['assets'].pop(int(self.path.rsplit('/', 1)[-1]))
                self.reply(204, {})
        server = HTTPServer(('127.0.0.1', 0), Api)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start(); self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            store = management.Control(directory)
            base = 'http://127.0.0.1:' + str(server.server_port)
            for name in ('source', 'target'):
                store.save_provider({'id': name, 'name': name, 'kind': 'forgejo', 'url': base, 'allow_http': True, 'token': 'isolated-token'})
            repo = {'name': 'demo', 'source': 'source', 'releases': True, 'remotes': {name: base + '/' + name + '/repo.git' for name in ('source', 'target')}, 'remote_providers': {name: name for name in ('source', 'target')}}
            engine = releases.ReleaseSync(store)
            snapshots = {name: {'tags/v1': 'same'} for name in ('source', 'target')}
            result = engine.sync(repo, snapshots)
            self.assertEqual(result['errors'], [])
            self.assertEqual(state['uploads'], 1)
            self.assertEqual(next(iter(state['assets'].values()))['data'], state['binary'])
            self.assertEqual(engine.sync(repo, snapshots)['assets'][0]['status'], 'unchanged')
            self.assertEqual(state['uploads'], 1)
            state['binary'] = b'updated binary'; state['version'] = 2
            result = engine.sync(repo, snapshots)
            self.assertEqual(result['errors'], [])
            self.assertEqual(result['assets'][0]['status'], 'updated')
            self.assertEqual(state['uploads'], 2)
            self.assertEqual(len(state['assets']), 1)
            self.assertEqual(next(iter(state['assets'].values()))['name'], 'app.zip')
            self.assertEqual(next(iter(state['assets'].values()))['data'], state['binary'])
            self.assertTrue(all('/assets/' in path for path in state['deletions']))


class ReleaseIntegrationTests(unittest.TestCase):
    def test_release_sync_runs_after_successful_git_push_and_preview_never_writes(self):
        repo = {'name': 'demo', 'source': 'source', 'releases': True, 'remotes': {'source': 'https://source/repo', 'target': 'https://target/repo'}}
        events = []
        def git(path, *args, **kwargs):
            if args[0] == 'push':
                events.append('push')
            return MagicMock(returncode=0, stdout='')
        snapshots = lambda: (None, {'source': {'tags/v1': 'same'}, 'target': {}}, [], [])
        def sync(repo, refs):
            events.append('releases')
            self.assertEqual(refs['target']['tags/v1'], 'same')
            return {'errors': [], 'assets': []}
        with patch.object(mesh, 'fetch_snapshots', side_effect=lambda *a, **kw: snapshots()), patch.object(mesh, 'git', side_effect=git), patch.object(mesh.releases.ReleaseSync, 'sync', side_effect=sync) as release_sync:
            mesh.synchronize(repo, dry_run=True)
            release_sync.assert_not_called()
            self.assertEqual(events, [])
            self.assertEqual(mesh.synchronize(repo)[0], 'success')
            self.assertEqual(events, ['push', 'releases'])

    def test_release_failure_preserves_successful_git_actions_in_job_details(self):
        repo = {'name': 'demo', 'source': 'source', 'releases': True, 'remotes': {'source': 'https://source/repo', 'target': 'https://target/repo'}}
        with patch.object(mesh, 'fetch_snapshots', return_value=(None, {'source': {'tags/v1': 'same'}, 'target': {}}, [], [])), patch.object(mesh, 'git'), patch.object(mesh.releases.ReleaseSync, 'sync', side_effect=management.ProviderError('Release API unavailable', 503)):
            status, details = mesh.synchronize(repo)
        self.assertEqual(status, 'error')
        self.assertEqual(len(details['updated']), 1)
        self.assertEqual(details['releases']['errors'][0]['message'], 'Release API unavailable')
