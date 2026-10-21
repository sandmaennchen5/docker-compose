"""Isolated provider matrix, persistent idempotence, selection and transfer ordering."""
import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import unquote
from test_sync import mesh, SyncFixture
import metadata
import management

class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = management.Control(self.temp.name)
        self.repo = {'name': 'demo', 'source': 'source', 'direction': 'one-way', 'remotes': {'source': 'https://source.example/team/repo.git', 'target': 'https://target.example/team/repo.git'}, 'remote_providers': {'source': 'source', 'target': 'target'}, 'labels': True, 'milestones': True, 'issues': True, 'comments': True}
        self.issue = {'id': 3, 'number': 3, 'iid': 3, 'title': 'Bug', 'body': 'Initial', 'description': 'Initial', 'state': 'open', 'labels': [{'name': 'bug'}], 'milestone': {'id': 2}, 'user': {'login': 'alice'}, 'html_url': 'https://source.example/team/repo/issues/3'}
        self.note = {'id': 4, 'body': 'Comment', 'user': {'login': 'bob'}}
        self.pr = {'id': 5, 'number': 5, 'iid': 5, 'title': 'Feature', 'body': 'PR', 'state': 'open', 'head': {'ref': 'feature', 'sha': 'abc'}, 'base': {'ref': 'main'}, 'source_branch': 'feature', 'target_branch': 'main', 'sha': 'abc'}
        self.refs = {'target': {'heads/main': 'base', 'heads/feature': 'abc'}}

    def setup_api(self, source_kind, target_kind):
        for name, kind in [('source', source_kind), ('target', target_kind)]:
            self.store.save_provider({'id': name, 'name': name, 'kind': kind, 'url': 'https://' + name + '.example', 'token': 'isolated'})
        self.values = {'labels': [], 'milestones': [], 'issues': [], 'pulls': [], 'merge_requests': [], 'comments': [], 'notes': []}
        self.writes = []
        def api(provider, path, query=None, *, method='GET', body=None):
            kind = provider['kind']
            root = '/projects/team%2Frepo' if kind == 'gitlab' else '/repos/team/repo'
            suffix = unquote(path.removeprefix(root)).strip('/')
            if not suffix:
                return {'id': 10 if provider['id'] == 'source' else 20, 'description': 'Repository'}
            parts = suffix.split('/')
            if provider['id'] == 'source':
                if parts[-1] in ('comments', 'notes'):
                    return [copy.deepcopy(self.note)]
                return copy.deepcopy({'labels': [{'id': 1, 'name': 'bug', 'color': 'ff0000', 'description': 'Bug label'}], 'milestones': [{'id': 2, 'number': 2, 'title': 'v1', 'state': 'closed', 'due_on': '2027-01-01T00:00:00Z'}], 'issues': [self.issue], 'pulls': [self.pr], 'merge_requests': [self.pr]}[parts[0]])
            category = parts[-1] if parts[-1] in ('comments', 'notes') else ('comments' if 'comments' in parts else 'notes' if 'notes' in parts else parts[0])
            rows = self.values[category]
            if method == 'GET':
                if parts[-1] in ('labels', 'milestones', 'issues', 'pulls', 'merge_requests', 'comments', 'notes'):
                    return copy.deepcopy(rows)
                item = next((x for x in rows if str(x.get('number', x['id'])) == parts[-1] or x.get('name') == parts[-1]), None)
                if item is None:
                    raise management.ProviderError('not found', 404)
                return copy.deepcopy(item)
            self.writes.append((path, method, copy.deepcopy(body)))
            if parts[-1] == 'labels' and 'issues' in parts:
                item = next(x for x in self.values['issues'] + self.values['pulls'] if str(x['number']) == parts[1])
                item['labels'] = [x for x in self.values['labels'] if x['id'] in body['labels']]
                return item['labels']
            if method == 'POST':
                index = 100 + sum(len(x) for x in self.values.values())
                item = dict(body, id=index)
                if category in ('issues', 'pulls', 'merge_requests'):
                    item.update(number=index, iid=index, state='open', labels=[])
                if category == 'milestones':
                    item.update(state='open')
                    if kind == 'github': item['number'] = index
                rows.append(item)
            else:
                item = next(x for x in rows if str(x.get('number', x['id'])) == parts[-1] or x.get('name') == parts[-1])
                if category == 'labels':
                    self.assertNotIn('name', body) if kind in ('github', 'gitlab') else None
                item.update(body)
                milestone = body.get('milestone', body.get('milestone_id'))
                if milestone is not None:
                    item['milestone'] = next((x for x in self.values['milestones'] if x.get('number', x['id']) == milestone), None)
                if 'new_name' in body: item['name'] = body['new_name']
                if 'state_event' in body: item['state'] = 'closed' if body['state_event'] == 'close' else 'open'
            return copy.deepcopy(item)
        self.api = patch.object(self.store, 'api', side_effect=api)
        self.api.start()
        self.addCleanup(self.api.stop)

    def test_all_provider_pairs_create_repeat_edit_comment_and_close(self):
        for source in ('github', 'forgejo', 'gitlab'):
            for target in ('github', 'forgejo', 'gitlab'):
                with self.subTest(source=source, target=target), tempfile.TemporaryDirectory() as directory:
                    self.store = management.Control(directory)
                    self.issue['state'] = 'open'; self.issue['body'] = self.issue['description'] = 'Initial'
                    self.note['body'] = 'Comment'
                    self.issue['labels'] = ['bug'] if source == 'gitlab' else [{'name': 'bug'}]
                    self.setup_api(source, target)
                    result = metadata.MetadataSync(self.store).sync(self.repo, self.refs)
                    self.assertEqual(result['errors'], [])
                    self.assertEqual(len(self.values['issues']), 1)
                    comments = self.values['notes' if target == 'gitlab' else 'comments']
                    self.assertEqual(len(comments), 1)
                    self.assertIn('alice', self.values['issues'][0].get('body', self.values['issues'][0].get('description')))
                    self.writes.clear()
                    result = metadata.MetadataSync(self.store).sync(self.repo, self.refs)
                    self.assertEqual(result['errors'], [])
                    # Relation adapters may repeat assignments in this deliberately minimal API fixture.
                    self.assertFalse(any(method == 'POST' for _, method, _ in self.writes))
                    self.issue['state'] = 'closed'; self.issue['body'] = self.issue['description'] = 'Changed'
                    self.note['body'] = 'Updated comment'
                    result = metadata.MetadataSync(self.store).sync(self.repo, self.refs)
                    self.assertEqual(result['errors'], [])
                    self.assertEqual(self.values['issues'][0]['state'], 'closed')
                    self.assertIn('Updated comment', comments[0]['body'])
                    self.assertEqual(len(comments), 1)
                    self.api.stop()

    def test_interrupted_mapping_is_recovered_without_duplicate_issues_or_comments(self):
        self.setup_api('github', 'forgejo')
        engine = metadata.MetadataSync(self.store)
        self.assertEqual(engine.sync(self.repo, self.refs)['errors'], [])
        with self.store.db() as db:
            db.execute("DELETE FROM metadata_links WHERE item LIKE 'issues:%' OR item LIKE 'comments:%'")
        self.writes.clear()
        self.assertEqual(metadata.MetadataSync(self.store).sync(self.repo, self.refs)['errors'], [])
        self.assertFalse(any(method == 'POST' for path, method, body in self.writes))
        self.assertEqual(len(self.values['issues']), 1)
        self.assertEqual(len(self.values['comments']), 1)

    def test_repository_details_topics_and_repeat_do_not_change_permissions(self):
        for kind in ('github', 'forgejo', 'gitlab'):
            with self.subTest(kind=kind):
                self.setup_api('github', kind)
                self.repo.update(labels=False, milestones=False, issues=False, comments=False, repository_details=True)
                source = {'id': 1, 'description': 'Source description', 'homepage': 'https://docs.example', 'topics': ['gitmesh']}
                target = {'id': 2, 'description': 'Old', 'topics': []}
                writes = []
                def api(provider, path, query=None, *, method='GET', body=None):
                    if method == 'GET': return dict(source if provider['id'] == 'source' else target)
                    writes.append((method, path, body))
                    self.assertFalse(set(body) & {'private', 'visibility', 'default_branch', 'permissions'})
                    if path.endswith('/topics'):
                        self.assertEqual(method, 'PUT')
                        target['topics'] = body['names' if kind == 'github' else 'topics']
                    else: target.update(body)
                    return dict(target)
                with patch.object(self.store, 'api', side_effect=api):
                    self.assertEqual(metadata.MetadataSync(self.store).sync(self.repo, self.refs)['errors'], [])
                    self.assertEqual(target['topics'], ['gitmesh'])
                    self.assertTrue(writes); writes.clear()
                    self.assertEqual(metadata.MetadataSync(self.store).sync(self.repo, self.refs)['errors'], [])
                    self.assertEqual(writes, [])
                self.api.stop()

    def test_foreign_existing_label_is_not_modified(self):
        self.setup_api('github', 'forgejo')
        self.values['labels'].append({'id': 77, 'name': 'bug', 'color': '000000'})
        result = metadata.MetadataSync(self.store).sync(self.repo, self.refs)
        self.assertGreater(result['counts']['conflict'], 0)
        self.assertEqual(self.values['labels'][0]['color'], '000000')

    def test_pr_only_created_for_exact_available_head_and_not_merged(self):
        self.setup_api('github', 'forgejo')
        self.repo.update(labels=False, milestones=False, issues=False, comments=False, pull_requests=True)
        result = metadata.MetadataSync(self.store).sync(self.repo, {'target': {'heads/main': 'base', 'heads/feature': 'wrong'}})
        self.assertTrue(result['warnings']); self.assertFalse(self.values['pulls'])
        result = metadata.MetadataSync(self.store).sync(self.repo, self.refs)
        self.assertEqual(result['errors'], []); self.assertEqual(len(self.values['pulls']), 1)
        self.pr.update(state='closed', merged_at='2026-10-21')
        result = metadata.MetadataSync(self.store).sync(self.repo, self.refs)
        self.assertEqual(self.values['pulls'][0]['state'], 'closed')
        self.assertFalse(any('/merge' in path for path, method, body in self.writes))

    def test_confidential_issue_is_not_published_on_other_provider(self):
        self.setup_api('gitlab', 'forgejo'); self.issue['confidential'] = True
        result = metadata.MetadataSync(self.store).sync(self.repo, self.refs)
        self.assertTrue(result['warnings']); self.assertFalse(self.values['issues'])

    def test_selected_webhook_events_are_added_only_on_source(self):
        for kind in ('github', 'forgejo', 'gitlab'):
            with self.subTest(kind=kind):
                self.setup_api(kind, kind)
                repo = dict(self.repo, wiki=True, pull_requests=True)
                with patch.object(self.store, 'api', side_effect=lambda p, path, query=None, **kw: [] if kw.get('method', 'GET') == 'GET' else {'id': 1}) as api:
                    self.store.register_hooks(repo, 'https://mesh.example', 'test-secret', refresh_secret=True)
                posts = [call for call in api.call_args_list if call.kwargs.get('method') == 'POST']
                self.assertEqual(len(posts), 2)
                first, second = [call.kwargs['body'] for call in posts]
                if kind == 'gitlab':
                    self.assertTrue(first['issues_events']); self.assertTrue(first['note_events']); self.assertTrue(first['merge_requests_events']); self.assertTrue(first['wiki_page_events'])
                    self.assertNotIn('issues_events', second)
                else:
                    self.assertIn('issues', first['events']); self.assertIn('pull_request', first['events']); self.assertIn('gollum' if kind == 'github' else 'wiki', first['events'])
                    self.assertEqual(second['events'], ['push'])
                self.api.stop()

    def test_validation_and_shared_options(self):
        mesh.validate_config({'repositories': [self.repo]})
        for changes in ({'source': ''}, {'remote_providers': {}}, {'issues': 'true'}, {'sync_branches': 1}, {'issues': False, 'pull_requests': False}):
            with self.assertRaises(ValueError): mesh.validate_config({'repositories': [dict(self.repo, **changes)]})
        config = {'profiles': {'all': {'issues': True, 'sync_branches': False}}, 'repositories': [dict(self.repo, profile='all')]}
        self.assertFalse(mesh.resolve_config(config)['repositories'][0]['sync_branches'])

class SelectionTests(SyncFixture):
    def test_metadata_failure_keeps_git_updates_and_takes_priority_over_conflict(self):
        self.repo.update(source='github', issues=True, releases=True)
        with patch.object(mesh.metadata.MetadataSync, 'sync', return_value={'errors': [{'message': 'API failure'}]}) as metadata_sync, patch.object(mesh.releases.ReleaseSync, 'sync', return_value={'conflicts': 1}):
            mesh.synchronize(self.repo, dry_run=True)
            metadata_sync.assert_not_called()
            status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'error')
        self.assertTrue(details['updated'])
        self.assertEqual(self.tip('github'), self.tip('forgejo'))
        self.assertTrue(details['metadata']['errors'])

    def test_disabled_branches_leave_targets_empty(self):
        self.repo.update(sync_branches=False, tags=False)
        status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'success'); self.assertEqual(details['updated'], [])
        self.assertEqual(self.command(self.root / 'gitlab', 'show-ref', check=False).stdout, '')

    def test_lfs_uploads_precede_git_pushes_and_preview_never_runs_lfs(self):
        self.repo.update(direction='one-way', source='github', lfs=True)
        events = []
        def run(path, *args, check=True):
            events.append(args)
            if args[0] == 'lfs':
                from subprocess import CompletedProcess
                return CompletedProcess(args, 0, '', '')
            return self.command(path, *args, check=check)
        with patch.object(mesh, 'git', run):
            mesh.synchronize(self.repo, dry_run=True)
            self.assertFalse(any(x[0] == 'lfs' for x in events)); events.clear()
            mesh.synchronize(self.repo)
        lfs = [x for x in events if x[0] == 'lfs']
        self.assertEqual(lfs, [('lfs', 'version'), ('lfs', 'fetch', '--all', 'github', 'refs/mesh/github/heads/main'), ('lfs', 'push', '--all', 'gitlab', 'refs/mesh/github/heads/main'), ('lfs', 'push', '--all', 'forgejo', 'refs/mesh/github/heads/main')])
        self.assertLess(max(i for i,x in enumerate(events) if x[0] == 'lfs'), min(i for i,x in enumerate(events) if x[0] == 'push'))

    def test_actual_lfs_binary_is_available_on_target(self):
        from test_sync import GIT
        import os
        if self.command(self.work, 'lfs', 'version', check=False).returncode:
            self.skipTest('Git LFS required')
        self.command(self.work, 'lfs', 'install', '--local')
        self.command(self.work, 'lfs', 'track', '*.bin')
        binary = b'GitMesh LFS integration fixture' * 1000
        (self.work / 'large.bin').write_bytes(binary)
        self.commit('LFS binary')
        self.command(self.work, 'remote', 'add', 'origin', Path(self.remotes['github']).as_uri())
        self.command(self.work, 'push', 'origin', 'main')
        self.repo.update(direction='one-way', source='github', lfs=True, remotes={name: Path(url).as_uri() for name, url in self.remotes.items()})
        status, details = mesh.synchronize(self.repo)
        self.assertEqual(status, 'success')
        clone = self.root / 'verify-lfs'
        self.command(self.root, 'clone', '--branch', 'main', self.remotes['forgejo'], str(clone))
        self.assertEqual((clone / 'large.bin').read_bytes(), binary)

    def test_wiki_content_is_replicated_separately(self):
        self.repo.update(source='github')
        for name, url in self.remotes.items():
            path = self.root / (name + '.wiki.git')
            path.mkdir()
            self.command(path, 'init', '--bare')
        self.command(self.work, 'push', str(self.root / 'github.wiki.git'), 'main')
        result = mesh.sync_wiki(self.repo)
        self.assertEqual(result['status'], 'success')
        for name in ('gitlab', 'forgejo'):
            actual = self.command(self.root / (name + '.wiki.git'), 'rev-parse', 'refs/heads/main').stdout
            expected = self.command(self.work, 'rev-parse', 'HEAD').stdout
            self.assertEqual(actual, expected)
        self.assertEqual(self.command(self.root / 'gitlab', 'show-ref', check=False).stdout, '')

    def test_wiki_uses_separate_cache_same_credentials_and_no_recursion(self):
        self.repo.update(source='github', wiki=True, issues=True, releases=True)
        with patch.object(mesh, 'synchronize', return_value=('success', {'updated': []})) as sync:
            result = mesh.sync_wiki(self.repo)
        wiki = sync.call_args.args[0]
        self.assertEqual(wiki['name'], 'test/wiki.git')
        self.assertTrue(all(url.endswith('.wiki.git') for url in wiki['remotes'].values()))
        self.assertFalse(wiki['wiki']); self.assertFalse(wiki['issues']); self.assertFalse(wiki['releases'])
        self.assertEqual(result['status'], 'success')

if __name__ == '__main__': unittest.main()
