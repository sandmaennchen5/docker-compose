"""Discovery reports actual saved sync directions and endpoints without credentials."""
import json
import tempfile
import unittest
from unittest.mock import patch
from test_http import HttpCase
from test_sync import mesh
import management


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = management.Control(self.temp.name)
        self.provider = {'id': 'p', 'kind': 'forgejo', 'url': 'https://forge.example'}

    def test_source_target_and_bidirectional_mappings_include_all_endpoints(self):
        profiles = [{'name': 'outbound', 'direction': 'one-way', 'source': 'forgejo', 'enabled': False, 'remotes': {'forgejo': 'https://forge.example/team/one.git', 'gitlab': 'https://gitlab.example/team/one.git'}},
                    {'name': 'inbound', 'direction': 'one-way', 'source': 'github', 'remotes': {'github': 'https://github.com/team/two.git', 'forgejo': 'ssh://git@forge.example/team/two.git'}},
                    {'name': 'mesh', 'profile': 'shared', 'remotes': {'a': 'https://forge.example/team/three.git', 'b': 'https://gitlab.example/team/three.git'}}]
        index = self.store.repository_mappings({'repositories': profiles}, self.provider)
        one = index['team/one'][0]
        self.assertEqual(one['matched_remote'], 'forgejo'); self.assertEqual(one['source'], 'forgejo'); self.assertFalse(one['enabled'])
        self.assertEqual(one['remotes']['gitlab'], 'https://gitlab.example/team/one.git')
        two = index['team/two'][0]
        self.assertEqual(two['source'], 'github'); self.assertEqual(two['matched_remote'], 'forgejo')
        self.assertEqual(index['team/three'][0]['direction'], 'bidirectional')
        self.assertEqual(index['team/three'][0]['shared_profile'], 'shared')

    def test_different_host_or_http_port_is_not_same_repository(self):
        provider = {'id': 'p', 'kind': 'forgejo', 'url': 'http://172.30.10.20:40501', 'allow_http': True}
        repo = {'name': 'demo', 'remotes': {'other': 'http://172.30.10.20:40502/team/repo.git', 'remote': 'https://other.example/team/repo.git'}}
        self.assertEqual(self.store.repository_mappings({'repositories': [repo]}, provider), {})

    def test_github_names_are_matched_case_insensitively(self):
        provider = {'id': 'p', 'kind': 'github', 'url': 'https://github.com'}
        result = self.store.repository_mappings({'repositories': [{'name': 'demo', 'remotes': {'a': 'https://github.com/Team/Repo.git'}}]}, provider)
        self.assertIn('team/repo', result)


class DiscoveryHttpTests(HttpCase):
    def test_discovery_reports_effective_saved_shared_profile_rules(self):
        store = mesh.control()
        store.save_provider({'id': 'p', 'name': 'Forgejo', 'kind': 'forgejo', 'url': 'https://forge.example', 'token': 'private-discovery-token'})
        config = {'profiles': {'common': {'direction': 'one-way', 'source': 'github', 'enabled': False}}, 'repositories': [{'name': 'mapped', 'profile': 'common', 'remotes': {'github': 'https://github.com/team/repo.git', 'forgejo': 'https://forge.example/team/repo.git'}}]}
        self.config.write_text(json.dumps(config))
        with patch.object(management.Control, 'api', return_value=[{'full_name': 'team/repo', 'clone_url': 'https://forge.example/team/repo.git'}, {'full_name': 'team/new', 'clone_url': 'https://forge.example/team/new.git'}]):
            status, body = self.request('GET', '/api/discover?provider=p', headers={'Authorization': 'Bearer ' + 'a' * 32})
        self.assertEqual(status, 200)
        self.assertNotIn(b'private-discovery-token', body)
        rows = json.loads(body)['repositories']
        mapping = rows[0]['syncs'][0]
        self.assertEqual(mapping['profile'], 'mapped'); self.assertEqual(mapping['shared_profile'], 'common')
        self.assertEqual(mapping['matched_remote'], 'forgejo'); self.assertEqual(mapping['source'], 'github')
        self.assertEqual(mapping['direction'], 'one-way'); self.assertFalse(mapping['enabled'])
        self.assertEqual(rows[1]['syncs'], [])
