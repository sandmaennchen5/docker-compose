"""Three-provider round trips, edit conflicts, and legacy-copy adoption."""
import copy
import tempfile
import unittest
from unittest.mock import patch
from test_sync import mesh
import management
import metadata


class MeshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = management.Control(self.temp.name)
        self.kinds = {'gh': 'github', 'gl': 'gitlab', 'fj': 'forgejo'}
        self.data = {n: {'milestones': [], 'issues': [], 'comments': []} for n in self.kinds}
        self.serial = 100
        for name, kind in self.kinds.items():
            self.store.save_provider(dict(id=name, name=name, kind=kind, url='https://' + name + '.example', token='isolated'))
        self.repo = dict(name='demo', source='gh', direction='bidirectional', issues=True, comments=True, milestones=True,
                         remotes={n: 'https://' + n + '.example/team/repo.git' for n in self.kinds}, remote_providers={n: n for n in self.kinds})
        self.data['gh']['issues'].append(dict(id=1, number=1, iid=1, title='Bug', body='Original', state='open', labels=[]))
        self.writes = []
        self.patch = patch.object(self.store, 'api', side_effect=self.api)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def api(self, provider, path, query=None, *, method='GET', body=None):
        name, kind = provider['id'], provider['kind']
        root = '/projects/team%2Frepo' if kind == 'gitlab' else '/repos/team/repo'
        parts = path.removeprefix(root).strip('/').split('/')
        if parts == ['']:
            return {'id': list(self.kinds).index(name) + 10}
        category = 'comments' if parts[-1] in ('notes', 'comments') or 'notes' in parts or 'comments' in parts else parts[0]
        if category in ('pulls', 'merge_requests'):
            category = 'issues'
        rows = self.data[name][category]
        if kind == 'gitlab' and category == 'milestones' and (query or {}).get('state') not in (None, 'active', 'closed'):
            raise management.ProviderError('Invalid milestone state', 400)
        collection = parts[-1] in ('issues', 'milestones', 'notes', 'comments', 'pulls', 'merge_requests')
        if method == 'GET':
            if collection:
                return copy.deepcopy(rows)
            row = next((r for r in rows if str(r['id'] if category == 'comments' or category == 'milestones' and kind == 'gitlab' else r.get('iid', r.get('number', r['id']))) == parts[-1]), None)
            if row is None:
                raise management.ProviderError('Not found', 404)
            return copy.deepcopy(row)
        self.writes.append((name, path, method, copy.deepcopy(body)))
        if method == 'DELETE':
            row = next((r for r in rows if str(r['id']) == parts[-1]), None)
            if row is None:
                raise management.ProviderError('Not found', 404)
            rows.remove(row)
            return {}
        if method == 'POST':
            self.serial += 1
            row = dict(id=self.serial, **body)
            if category == 'issues':
                row.update(iid=self.serial + 1000, number=self.serial + 1000, state='opened' if kind == 'gitlab' else 'open', labels=[])
            if category == 'milestones':
                row.update(number=self.serial + 2000, state='active' if kind == 'gitlab' else 'open')
            rows.append(row)
        else:
            row = next(r for r in rows if str(r.get('iid', r.get('number', r['id'])) if category != 'comments' else r['id']) == parts[-1] or str(r['id']) == parts[-1])
            row.update(body)
            if 'state_event' in body:
                row['state'] = 'closed' if body['state_event'] == 'close' else 'active'
        return copy.deepcopy(row)

    def sync(self):
        result = metadata.MetadataSync(self.store).sync(self.repo, {})
        self.assertEqual(result['errors'], [], result)
        return result

    def test_github_create_omits_empty_deadline_and_updates_can_clear_it(self):
        self.data['fj']['milestones'].append(dict(id=50, number=7, title='4.1.1', description='Plan', due_on=None, state='open'))
        def strict(provider, path, query=None, *, method='GET', body=None):
            if provider['kind'] == 'github' and method == 'POST' and path.endswith('/milestones') and 'due_on' in body and body['due_on'] is None:
                raise management.ProviderError('Milestone/due_on/invalid', 422)
            return self.api(provider, path, query, method=method, body=body)
        with patch.object(self.store, 'api', side_effect=strict):
            self.sync()
            self.data['fj']['milestones'][0]['due_on'] = '2027-01-01T00:00:00Z'
            self.sync()
            self.assertEqual(self.data['gh']['milestones'][0]['due_on'], '2027-01-01T00:00:00Z')
            self.data['fj']['milestones'][0]['due_on'] = None
            self.sync()
            self.assertIsNone(self.data['gh']['milestones'][0]['due_on'])
            self.writes.clear()
            self.sync()
            self.assertEqual(self.writes, [])

    def test_one_way_forgejo_to_github_omits_empty_creation_deadline(self):
        self.repo.update(source='fj', direction='one-way', issues=False, comments=False)
        self.data['fj']['milestones'].append(dict(id=50, number=7, title='S', description='', due_on=None, state='open'))
        def strict(provider, path, query=None, *, method='GET', body=None):
            if provider['kind'] == 'github' and method == 'POST' and path.endswith('/milestones'):
                self.assertNotIn('due_on', body)
            return self.api(provider, path, query, method=method, body=body)
        with patch.object(self.store, 'api', side_effect=strict):
            self.sync()
        self.assertEqual(self.data['gh']['milestones'][0]['title'], 'S')

    def test_milestone_errors_identify_title_and_origin(self):
        self.data['fj']['milestones'].append(dict(id=50, number=7, title='4.1.1', description='', state='open'))
        def reject(provider, path, query=None, *, method='GET', body=None):
            if provider['kind'] == 'github' and method == 'POST' and path.endswith('/milestones'):
                raise management.ProviderError('Milestone/title/already_exists', 422)
            return self.api(provider, path, query, method=method, body=body)
        with patch.object(self.store, 'api', side_effect=reject):
            result = metadata.MetadataSync(self.store).sync(self.repo, {})
        self.assertEqual(result['errors'][0]['name'], '4.1.1')
        self.assertIn('fj:', result['errors'][0]['item'])

    def test_deleted_linked_comments_never_return_from_other_peers(self):
        self.data['gh']['comments'].append(dict(id=3, body='Delete me', user={'login': 'alice'}))
        self.sync()
        for remote in self.kinds:
            with self.subTest(remote=remote):
                saved = copy.deepcopy(self.data[remote]['comments'])
                self.data[remote]['comments'].clear()
                self.writes.clear()
                result = self.sync()
                self.assertFalse(self.data[remote]['comments'])
                self.assertFalse(self.writes)
                self.assertGreater(result['counts'].get('conflict', 0), 0)
                # Surviving edits and subsequent runs cannot undo deletion.
                other = next(n for n in self.kinds if n != remote)
                self.data[other]['comments'][0]['body'] = 'Edited survivor'
                self.sync()
                self.assertFalse(self.data[remote]['comments'])
                self.assertFalse(self.writes)
                self.data[remote]['comments'] = saved
                with self.store.db() as db:
                    db.execute('DELETE FROM comment_tombstones')
                # Restore matching snapshots for the next independent subcase.
                self.data[other]['comments'][0]['body'] = saved[0]['body']
                # Each subcase uses the stored links, independent of body text.

    def test_filtered_comment_is_paused_without_permanent_deletion(self):
        self.data['gh']['comments'].append(dict(id=3, body='Keep'))
        self.sync()
        self.data['fj']['comments'][0]['internal'] = True
        self.writes.clear()
        self.sync()
        self.assertFalse(self.writes)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM comment_tombstones').fetchone()[0], 0)
        self.data['fj']['comments'][0]['internal'] = False
        self.data['gh']['comments'][0]['body'] = 'Public edit'
        self.sync()
        self.assertIn('Public edit', self.data['fj']['comments'][0]['body'])

    def test_missing_comment_api_failure_does_not_establish_deletion(self):
        self.data['gh']['comments'].append(dict(id=3, body='Keep'))
        self.sync()
        self.data['fj']['comments'].clear()
        def forbidden(provider, path, query=None, *, method='GET', body=None):
            if provider['id'] == 'fj' and method == 'GET' and '/issues/comments/' in path:
                raise management.ProviderError('Forbidden', 403)
            return self.api(provider, path, query, method=method, body=body)
        self.writes.clear()
        with patch.object(self.store, 'api', side_effect=forbidden):
            result = metadata.MetadataSync(self.store).sync(self.repo, {})
        self.assertTrue(result['errors'])
        self.assertFalse(self.writes)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM comment_tombstones').fetchone()[0], 0)

    def test_comment_tombstone_survives_engine_restart_and_new_remote(self):
        self.repo['remotes'].pop('gl')
        self.repo['remote_providers'].pop('gl')
        self.data['gh']['comments'].append(dict(id=3, body='Deleted original'))
        self.sync()
        self.data['gh']['comments'].clear()
        self.sync()
        self.repo['remotes']['gl'] = 'https://gl.example/team/repo.git'
        self.repo['remote_providers']['gl'] = 'gl'
        self.writes.clear()
        self.sync()
        self.assertEqual(self.data['gh']['comments'], [])
        self.assertEqual(self.data['gl']['comments'], [])
        self.assertFalse(any('/comments' in path or '/notes' in path for _, path, _, _ in self.writes))
        # An unrelated new comment must still be distributed.
        self.data['gh']['comments'].append(dict(id=4, body='New comment'))
        self.sync()
        self.assertEqual(len(self.data['gl']['comments']), 1)
        self.assertIn('New comment', self.data['gl']['comments'][0]['body'])

    def test_native_forgejo_milestone_and_comments_reach_both_other_providers(self):
        self.sync()
        self.data['fj']['milestones'].append(dict(id=50, number=7, title='Forge milestone', description='Plan', due_on='2027-01-01T00:00:00Z', state='open'))
        self.data['fj']['comments'].append(dict(id=51, body='From Forgejo', user={'login': 'alice'}))
        self.sync()
        for name in self.kinds:
            self.assertEqual(len(self.data[name]['milestones']), 1)
            self.assertEqual(self.data[name]['milestones'][0]['title'], 'Forge milestone')
            self.assertEqual(len(self.data[name]['comments']), 1)
            self.assertIn('From Forgejo', self.data[name]['comments'][0]['body'])
        self.writes.clear()
        self.store = management.Control(self.temp.name)
        self.patch.stop()
        self.patch = patch.object(self.store, 'api', side_effect=self.api)
        self.patch.start()
        self.sync()
        self.assertEqual(self.writes, [])

    def test_duplicate_milestone_copy_never_updates_either_candidate(self):
        self.data['fj']['milestones'].append(dict(id=50, number=7, title='Native', description='Plan', state='open'))
        self.sync()
        duplicate = copy.deepcopy(self.data['gh']['milestones'][0])
        duplicate.update(id=900, number=900)
        self.data['gh']['milestones'].append(duplicate)
        self.data['fj']['milestones'][0]['state'] = 'closed'
        self.writes.clear()
        result = self.sync()
        self.assertGreater(result['counts'].get('conflict', 0), 0)
        self.assertEqual(self.writes, [])
        self.assertTrue(all(row['state'] == 'open' for row in self.data['gh']['milestones']))

    def test_forgejo_close_and_reopen_only_send_milestone_state(self):
        self.data['fj']['milestones'].append(dict(id=50, number=7, title='Forge milestone', description='Plan', due_on=None, state='open'))
        self.sync()
        for closed in (True, False):
            self.data['fj']['milestones'][0]['state'] = 'closed' if closed else 'open'
            self.writes.clear()
            self.sync()
            for name in ('gh', 'gl'):
                self.assertEqual(self.data[name]['milestones'][0]['state'], 'closed' if closed else 'active' if name == 'gl' else 'open')
                writes = [body for remote, path, method, body in self.writes if remote == name and '/milestones/' in path]
                expected = {'state_event': 'close' if closed else 'activate'} if name == 'gl' else {'state': 'closed' if closed else 'open'}
                self.assertEqual(writes, [expected])
            self.writes.clear()
            self.sync()
            self.assertEqual(self.writes, [])

    def test_single_remote_edits_and_state_changes_propagate(self):
        self.data['gh']['milestones'].append(dict(id=2, number=2, title='v1', description='Plan', state='open'))
        self.data['gh']['comments'].append(dict(id=3, body='Discussion', user={'login': 'alice'}))
        self.sync()
        self.data['fj']['milestones'][0].update(title='Renamed', description='Updated plan', state='closed')
        self.data['gl']['comments'][0]['body'] = self.data['gl']['comments'][0]['body'].replace('Discussion', 'Edited on GitLab')
        self.sync()
        for name in self.kinds:
            self.assertEqual(self.data[name]['milestones'][0]['title'], 'Renamed')
            self.assertEqual(self.data[name]['milestones'][0]['state'], 'closed')
            self.assertIn('Edited on GitLab', self.data[name]['comments'][0]['body'])
        self.writes.clear()
        self.sync()
        self.assertEqual(self.writes, [])

    def test_concurrent_edits_report_conflict_without_overwriting(self):
        self.data['gh']['milestones'].append(dict(id=2, number=2, title='v1', description='Plan', state='open'))
        self.data['gh']['comments'].append(dict(id=3, body='Discussion'))
        self.sync()
        self.data['fj']['milestones'][0]['description'] = 'Forgejo change'
        self.data['gl']['milestones'][0]['description'] = 'GitLab change'
        self.data['fj']['comments'][0]['body'] = 'Forgejo comment edit'
        self.data['gl']['comments'][0]['body'] = 'GitLab comment edit'
        self.writes.clear()
        result = self.sync()
        self.assertGreaterEqual(result['counts']['conflict'], 2)
        self.assertFalse(any(path.endswith('/milestones/2') or method == 'POST' for _, path, method, _ in self.writes))
        self.assertEqual(self.data['gh']['milestones'][0]['description'], 'Plan')
        self.assertEqual(self.data['gh']['comments'][0]['body'], 'Discussion')
        self.assertGreaterEqual(self.sync()['counts']['conflict'], 2)

    def test_every_provider_can_be_source_and_comment_origin(self):
        for source in self.kinds:
            with self.subTest(source=source):
                with self.store.db() as db:
                    db.execute('CREATE TABLE IF NOT EXISTS metadata_links (scope TEXT, item TEXT, target TEXT, signature TEXT, PRIMARY KEY(scope,item))')
                    db.execute('DELETE FROM metadata_links')
                    db.execute('CREATE TABLE IF NOT EXISTS metadata_mesh (mesh TEXT, category TEXT, origin TEXT, remote TEXT, item TEXT, snapshot TEXT, PRIMARY KEY(mesh,category,origin,remote))')
                    db.execute('DELETE FROM metadata_mesh')
                self.data = {n: {'milestones': [], 'issues': [], 'comments': []} for n in self.kinds}
                self.repo['source'] = source
                self.data[source]['issues'].append(dict(id=1, number=1, iid=1, title='Bug', body='Original', state='open', labels=[]))
                self.sync()
                for index, name in enumerate(self.kinds):
                    self.data[name]['comments'].append(dict(id=800 + index, body='Native ' + name))
                    self.data[name]['milestones'].append(dict(id=900 + index, number=900 + index, title='Native milestone ' + name, description='Plan', state='open'))
                self.sync()
                self.writes.clear()
                self.sync()
                self.assertFalse(any(method == 'POST' for _, _, method, _ in self.writes))
                for name in self.kinds:
                    self.assertEqual(len(self.data[name]['comments']), 3)
                    self.assertEqual(len(self.data[name]['milestones']), 3)

    def test_private_notes_and_confidential_target_do_not_leak(self):
        self.sync()
        self.data['gl']['issues'][0]['confidential'] = True
        self.data['gl']['comments'].append(dict(id=700, body='Secret discussion'))
        self.data['gh']['issues'][0]['body'] = 'Edited public source'
        self.data['fj']['comments'].extend([dict(id=701, body='Internal', internal=True), dict(id=702, body='System', system=True)])
        self.sync()
        self.assertTrue(self.data['gl']['issues'][0]['confidential'])
        self.assertFalse(self.data['gh']['comments'])
        self.assertEqual(len(self.data['fj']['comments']), 2)

    def test_adding_a_remote_preserves_existing_comment_and_milestone_links(self):
        self.repo['remotes'].pop('gl')
        self.repo['remote_providers'].pop('gl')
        self.data['gh']['comments'].append(dict(id=3, body='Discussion'))
        self.data['gh']['milestones'].append(dict(id=2, number=2, title='v1', description='Plan', state='open'))
        self.sync()
        self.repo['remotes']['gl'] = 'https://gl.example/team/repo.git'
        self.repo['remote_providers']['gl'] = 'gl'
        self.sync()
        for name in self.kinds:
            self.assertEqual(len(self.data[name]['comments']), 1)
            self.assertEqual(len(self.data[name]['milestones']), 1)

    def test_partial_update_retries_without_losing_the_winning_edit(self):
        self.data['gh']['milestones'].append(dict(id=2, number=2, title='v1', description='Plan', state='open'))
        self.sync()
        self.data['fj']['milestones'][0]['description'] = 'New plan'
        def failing(provider, path, query=None, *, method='GET', body=None):
            if provider['id'] == 'gl' and method == 'PUT' and '/milestones/' in path:
                raise management.ProviderError('Temporary failure', 503)
            return self.api(provider, path, query, method=method, body=body)
        with patch.object(self.store, 'api', side_effect=failing):
            result = metadata.MetadataSync(self.store).sync(self.repo, {})
        self.assertTrue(result['errors'])
        self.assertEqual(self.data['gh']['milestones'][0]['description'], 'New plan')
        result = self.sync()
        self.assertFalse(result['counts'].get('conflict'))
        for name in self.kinds:
            self.assertEqual(metadata.MetadataMesh(metadata.MetadataSync(self.store), self.repo).fields('milestones', self.data[name]['milestones'][0], name != 'gh')['description'], 'New plan')

    def test_lost_create_response_is_recovered_from_marker_without_duplicates(self):
        self.sync()
        self.data['fj']['comments'].append(dict(id=51, body='From Forgejo'))
        def lost_response(provider, path, query=None, *, method='GET', body=None):
            response = self.api(provider, path, query, method=method, body=body)
            if provider['id'] == 'gl' and method == 'POST' and path.endswith('/notes'):
                raise management.ProviderError('Response lost', 503)
            return response
        with patch.object(self.store, 'api', side_effect=lost_response):
            self.assertTrue(metadata.MetadataSync(self.store).sync(self.repo, {})['errors'])
        result = self.sync()
        self.assertFalse(result['counts'].get('conflict'))
        for name in self.kinds:
            self.assertEqual(len(self.data[name]['comments']), 1)

    def test_switch_back_to_one_way_reuses_mesh_objects(self):
        self.sync()
        self.data['fj']['comments'].append(dict(id=51, body='From Forgejo'))
        self.data['fj']['milestones'].append(dict(id=50, number=7, title='Forge milestone', description='Plan', state='open'))
        self.sync()
        self.repo['direction'] = 'one-way'
        self.writes.clear()
        self.sync()
        self.assertFalse(any(method == 'POST' for _, _, method, _ in self.writes))
        self.assertTrue(all(len(self.data[n]['comments']) == 1 and len(self.data[n]['milestones']) == 1 for n in self.kinds))

    def test_comments_on_linked_pull_requests_flow_back(self):
        self.repo.update(issues=False, milestones=False, pull_requests=True)
        self.data['gh']['issues'][0].update(head={'ref': 'feature', 'sha': 'abc'}, base={'ref': 'main'})
        refs = {name: {'heads/main': 'base', 'heads/feature': 'abc'} for name in self.kinds}
        self.assertEqual(metadata.MetadataSync(self.store).sync(self.repo, refs)['errors'], [])
        self.data['fj']['comments'].append(dict(id=51, body='PR discussion on Forgejo'))
        self.assertEqual(metadata.MetadataSync(self.store).sync(self.repo, refs)['errors'], [])
        for name in self.kinds:
            self.assertEqual(len(self.data[name]['comments']), 1)
            self.assertIn('PR discussion on Forgejo', self.data[name]['comments'][0]['body'])

    def test_one_way_remains_source_to_targets(self):
        self.repo.update(direction='one-way', milestones=False)
        self.sync()
        self.data['fj']['comments'].append(dict(id=51, body='Target only'))
        self.sync()
        self.assertEqual(self.data['gh']['comments'], [])
        self.assertEqual(self.data['gl']['comments'], [])

    def test_legacy_copies_are_adopted_without_new_duplicates(self):
        self.repo['direction'] = 'one-way'
        self.data['gh']['milestones'].append(dict(id=2, number=2, title='v1', description='Plan', state='open'))
        self.data['gh']['comments'].append(dict(id=3, body='Existing discussion', user={'login': 'alice'}))
        self.sync()
        self.repo['direction'] = 'bidirectional'
        self.writes.clear()
        self.sync()
        self.assertFalse(any(method == 'POST' for _, _, method, _ in self.writes))
        self.assertTrue(all(len(self.data[n]['comments']) == 1 and len(self.data[n]['milestones']) == 1 for n in self.kinds))


if __name__ == '__main__':
    unittest.main()
