"""Metadata resolution decisions are bound to current API snapshots."""
import copy
import unittest
from unittest.mock import patch
import test_metadata as label_fixture
import test_metadata_mesh as mesh_fixture
from test_http import HttpCase, mesh
import conflicts
import metadata
import inspection


class LabelResolutionTests(unittest.TestCase):
    def setUp(self):
        self.f = label_fixture.MetadataTests('test_foreign_existing_label_is_not_modified')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.setup_api('github', 'forgejo')
        self.f.repo.update(milestones=False, issues=False, comments=False)
        self.f.values['labels'].append({'id': 77, 'name': 'bug', 'color': '000000', 'description': 'Existing'})
        self.original = self.f.store.api.side_effect
        self.changed = False
        def api(provider, path, query=None, **kw):
            if provider['id'] == 'source' and path.endswith('/labels/bug'):
                return {'id': 1, 'name': 'bug', 'color': 'ff0000', 'description': 'Changed' if self.changed else 'Bug label'}
            return self.original(provider, path, query, **kw)
        self.source_api = api
        self.f.store.api.side_effect = api

    def conflict(self):
        result = metadata.MetadataSync(self.f.store).sync(self.f.repo, {})
        return next(r for r in result['items'] if r['status'] == 'conflict')

    def test_labels_include_names_values_and_adopt_keeps_target(self):
        item = self.conflict()
        self.assertEqual(item['name'], 'bug')
        self.assertEqual(item['values']['target']['color'], '000000')
        self.f.writes.clear()
        result = conflicts.resolve(self.f.store, self.f.repo, {'conflict_id': item['resolution_id'], 'choice': 'adopt'}, 'admin')
        self.assertEqual(result['status'], 'success')
        self.assertEqual(self.f.writes, [])
        self.assertIn(item['resolution_id'], conflicts.resolved(self.f.store))
        self.assertEqual(self.f.values['labels'][0]['color'], '000000')
        self.assertFalse(metadata.MetadataSync(self.f.store).sync(self.f.repo, {})['counts'].get('conflict'))

    def test_source_choice_updates_owned_values_and_does_not_duplicate(self):
        item = self.conflict()
        result = conflicts.resolve(self.f.store, self.f.repo, {'conflict_id': item['resolution_id'], 'choice': 'source'}, 'admin')
        self.assertEqual(result['status'], 'success')
        self.assertEqual(self.f.values['labels'][0]['color'], 'ff0000')
        self.assertEqual(len(self.f.values['labels']), 1)
        self.assertFalse(metadata.MetadataSync(self.f.store).sync(self.f.repo, {})['counts'].get('conflict'))

    def test_changed_source_or_config_rejects_resolution_without_writes(self):
        item = self.conflict()
        self.f.writes.clear()
        self.changed = True
        with self.assertRaises(inspection.StaleState):
            conflicts.resolve(self.f.store, self.f.repo, {'conflict_id': item['resolution_id'], 'choice': 'source'}, 'admin')
        self.assertFalse(self.f.writes)
        self.changed = False
        with self.assertRaises(inspection.StaleState):
            conflicts.resolve(self.f.store, dict(self.f.repo, enabled=False), {'conflict_id': item['resolution_id'], 'choice': 'adopt'}, 'admin')

    def test_source_values_can_be_applied_on_all_target_provider_kinds(self):
        self.conflict()
        for kind in ('github', 'gitlab', 'forgejo'):
            with self.subTest(kind=kind):
                self.f.api.stop()
                self.f.setup_api('github', kind)
                self.original = self.f.store.api.side_effect
                self.f.store.api.side_effect = self.source_api
                self.f.values['labels'].append({'id': 77, 'name': 'bug', 'color': '000000', 'description': 'Existing'})
                with self.f.store.db() as db:
                    db.execute('DELETE FROM metadata_links')
                item = self.conflict()
                result = conflicts.resolve(self.f.store, self.f.repo, {'conflict_id': item['resolution_id'], 'choice': 'source'}, 'admin')
                self.assertEqual(result['status'], 'success')
                self.assertEqual(self.f.values['labels'][0]['color'].lstrip('#'), 'ff0000')
                self.assertFalse(metadata.MetadataSync(self.f.store).sync(self.f.repo, {})['counts'].get('conflict'))

    def test_target_change_and_missing_item_are_stale(self):
        item = self.conflict()
        self.f.values['labels'][0]['description'] = 'New target edit'
        self.f.writes.clear()
        with self.assertRaises(inspection.StaleState):
            conflicts.resolve(self.f.store, self.f.repo, {'conflict_id': item['resolution_id'], 'choice': 'source'}, 'admin')
        self.assertFalse(self.f.writes)
        self.f.values['labels'].clear()
        with self.assertRaises(inspection.StaleState):
            conflicts.resolve(self.f.store, self.f.repo, {'conflict_id': item['resolution_id'], 'choice': 'source'}, 'admin')
        self.assertFalse(self.f.writes)

    def test_identical_foreign_labels_are_adopted_without_api_writes(self):
        self.f.values['labels'][0].update(color='FF0000', description='Bug label')
        result = metadata.MetadataSync(self.f.store).sync(self.f.repo, {})
        self.assertFalse(result['counts'].get('conflict'))
        self.assertEqual(result['counts']['adopted'], 1)
        self.assertFalse(self.f.writes)


class MeshResolutionTests(unittest.TestCase):
    def setUp(self):
        self.f = mesh_fixture.MeshTests('test_concurrent_edits_report_conflict_without_overwriting')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.data['gh']['milestones'].append(dict(id=2, number=2, title='v1', description='Plan', state='open'))
        self.f.data['gh']['comments'].append(dict(id=3, body='Discussion'))
        self.f.sync()
        self.f.data['fj']['milestones'][0]['description'] = 'Forgejo plan'
        self.f.data['gl']['milestones'][0]['description'] = 'GitLab plan'
        self.f.data['fj']['comments'][0]['body'] = 'Forgejo discussion'
        self.f.data['gl']['comments'][0]['body'] = 'GitLab discussion'
        self.items = [r for r in self.f.sync()['items'] if r['status'] == 'conflict']

    def test_choose_remote_for_milestone_and_comment_then_repeat(self):
        self.assertEqual(len(self.items), 2)
        for item in self.items:
            result = conflicts.resolve(self.f.store, self.f.repo, {'conflict_id': item['resolution_id'], 'choice': 'fj'}, 'admin')
            self.assertEqual(result['status'], 'success', result)
        self.assertFalse(self.f.sync()['counts'].get('conflict'))
        for name in self.f.kinds:
            self.assertIn('Forgejo plan', self.f.data[name]['milestones'][0]['description'])
            self.assertIn('Forgejo discussion', self.f.data[name]['comments'][0]['body'])

    def test_stale_version_does_not_overwrite_any_remote(self):
        item = next(r for r in self.items if r['category'] == 'comments')
        self.f.data['gh']['comments'][0]['body'] = 'Newer original'
        self.f.writes.clear()
        with self.assertRaises(inspection.StaleState):
            conflicts.resolve(self.f.store, self.f.repo, {'conflict_id': item['resolution_id'], 'choice': 'fj'}, 'admin')
        self.assertFalse(self.f.writes)


class ResolutionHttpTests(HttpCase):
    def test_reader_and_anonymous_cannot_resolve(self):
        self.assertEqual(self.request('POST', '/api/conflicts/resolve', b'{}')[0], 401)
        with patch.object(mesh.Handler, 'actor', return_value={'name': 'reader', 'role': 'readonly'}), patch.object(mesh.conflict_store, 'resolve') as resolve:
            self.assertEqual(self.request('POST', '/api/conflicts/resolve', b'{}')[0], 403)
            resolve.assert_not_called()

    def test_route_requires_admin_and_uses_repository_operation_gate(self):
        actor = {'name': 'admin', 'role': 'admin'}
        with patch.object(mesh.Handler, 'actor', return_value=actor), patch.object(mesh, 'repository_action', return_value={'status': 'success'}) as action:
            self.assertEqual(self.request('POST', '/api/conflicts/resolve', b'{"profile":"demo"}')[0], 200)
            self.assertEqual(action.call_args.args[0], 'conflicts/resolve')
        with patch.object(mesh.Handler, 'actor', return_value=actor), patch.object(mesh, 'MAINTENANCE', True):
            self.assertEqual(self.request('POST', '/api/conflicts/resolve', b'{"profile":"demo"}')[0], 409)


if __name__ == '__main__': unittest.main()
