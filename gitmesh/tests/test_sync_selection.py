"""Explicit local false values survive shared profiles, persistence and execution."""
import copy
import json
import unittest
from unittest.mock import patch, MagicMock
from test_http import HttpCase, mesh

class SyncSelectionTests(HttpCase):
    def setUp(self):
        super().setUp()
        self.headers = {'Authorization': 'Bearer ' + 'a' * 32, 'Content-Type': 'application/json'}
        self.repo = {'name': 'demo', 'profile': 'template', 'source': 'a', 'remotes': {'a': 'https://a.example/team/repo.git', 'b': 'https://b.example/team/repo.git'}, 'remote_providers': {'a': 'a', 'b': 'b'}}
        for name in ('a', 'b'):
            mesh.control().save_provider({'id': name, 'name': name, 'kind': 'forgejo', 'url': 'https://' + name + '.example', 'token': 'isolated'})
        self.config_value = {'profiles': {'template': {'wiki': True, 'interval': 60}}, 'repositories': [self.repo]}

    def save(self, config):
        return self.request('POST', '/api/settings', json.dumps(config).encode(), self.headers)[0]

    def test_shared_wiki_true_local_false_survives_roundtrip_and_execution(self):
        self.repo['sync_overrides'] = {'wiki': False}
        self.assertEqual(self.save(self.config_value), 200)
        code, data = self.request('GET', '/api/settings', headers=self.headers)
        saved = json.loads(data)
        self.assertEqual(code, 200)
        self.assertTrue(saved['profiles']['template']['wiki'])
        self.assertIs(saved['repositories'][0]['sync_overrides']['wiki'], False)
        effective = mesh.load_config()['repositories'][0]
        self.assertIs(effective['wiki'], False)
        self.assertEqual(effective['interval'], 60)
        self.assertNotIn('wiki', mesh.selected_components(effective))
        with patch.object(mesh, 'fetch_snapshots', return_value=(self.root, {'a': {}, 'b': {}}, [], [])), patch.object(mesh, 'sync_wiki') as wiki:
            self.assertEqual(mesh.synchronize(effective)[0], 'success')
            wiki.assert_not_called()

    def test_own_rules_false_and_shared_default_inheritance(self):
        own = dict(self.repo, wiki=False)
        own.pop('profile')
        self.assertEqual(self.save({'repositories': [own]}), 200)
        self.assertIs(mesh.load_config()['repositories'][0]['wiki'], False)
        self.assertEqual(self.save(self.config_value), 200)
        self.assertIs(mesh.load_config()['repositories'][0]['wiki'], True)

    def test_component_override_cannot_override_shared_policy_or_invalid_types(self):
        for overrides in ({'source': 'b'}, {'interval': 0}, {'enabled': True}, {'wiki': 'false'}, {'wiki': None}, {'release_max_asset_mb': -1}):
            with self.subTest(overrides=overrides):
                config = copy.deepcopy(self.config_value)
                config['repositories'][0]['sync_overrides'] = overrides
                self.assertEqual(self.save(config), 400)

    def test_save_during_sync_keeps_running_snapshot_and_updates_next_job(self):
        self.assertEqual(self.save(self.config_value), 200)
        snapshot = mesh.load_config()['repositories'][0]
        changed = copy.deepcopy(self.config_value)
        changed['repositories'][0]['sync_overrides'] = {'wiki': False}
        worker = MagicMock()
        worker.active = {1: 'demo'}
        # A running child holds the operations lock for its complete sync.
        with mesh.runtime.file_lock(mesh.DATA), patch.object(mesh, 'WORKER', worker):
            self.assertEqual(self.save(changed), 200)
        self.assertTrue(snapshot['wiki'])
        self.assertFalse(mesh.load_config()['repositories'][0]['wiki'])

    def test_concurrent_configuration_writer_does_not_replace_config(self):
        self.assertEqual(self.save(self.config_value), 200)
        changed = copy.deepcopy(self.config_value)
        changed['repositories'][0]['sync_overrides'] = {'wiki': False}
        with mesh.runtime.file_lock(mesh.DATA, 'configuration'):
            self.assertEqual(self.save(changed), 409)
        self.assertTrue(mesh.load_config()['repositories'][0]['wiki'])

    def test_busy_save_does_not_claim_success_or_replace_saved_config(self):
        self.assertEqual(self.save(self.config_value), 200)
        changed = copy.deepcopy(self.config_value)
        changed['repositories'][0]['sync_overrides'] = {'wiki': False}
        with patch.object(mesh, 'MAINTENANCE', True):
            self.assertEqual(self.save(changed), 409)
        self.assertIs(mesh.load_config()['repositories'][0]['wiki'], True)
        self.assertEqual(self.save(changed), 200)
        self.assertIs(mesh.load_config()['repositories'][0]['wiki'], False)

if __name__ == '__main__': unittest.main()
