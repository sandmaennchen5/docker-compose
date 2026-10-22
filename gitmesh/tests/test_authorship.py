"""Verified account routing, provenance, legacy authors, and credential isolation."""
import copy
import json
import unittest
from unittest.mock import patch
import test_metadata_mesh as fixture
from test_http import HttpCase, mesh
import authorship
import management


class AuthorRoutingTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.MeshTests('test_native_forgejo_milestone_and_comments_reach_both_other_providers')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.personal = {'gh': 11, 'gl': 22, 'fj': 33}
        self.bots = {'gh': 911, 'gl': 922, 'fj': 933}
        for name, kind in self.f.kinds.items():
            self.f.store.save_provider(dict(id=name, name=name, kind=kind, url='https://' + name + '.example', token='personal-' + name))
            self.f.store.save_provider(dict(id=name + '_bot', name='GitMesh-Bot ' + name, kind=kind, url='https://' + name + '.example', token='bot-secret-' + name))
        self.original = self.f.store.api.side_effect
        self.requests = []
        self.f.store.api.side_effect = self.api
        for name in self.f.kinds:
            authorship.save(self.f.store, {'id': name, 'enabled': True, 'bot_provider': name + '_bot'})
        self.f.data['gh']['issues'][0]['user'] = {'id': self.personal['gh'], 'login': 'michael-gh'}

    def api(self, provider, path, query=None, *, method='GET', body=None):
        main = provider['id'].removesuffix('_bot')
        bot = provider['token'].startswith('bot-')
        author = {'id': (self.bots if bot else self.personal)[main], 'login': ('gitmesh-bot-' if bot else 'michael-') + main}
        if path == '/user':
            return dict(author, username=author['login'])
        if method != 'GET':
            self.requests.append((provider['id'], provider['token'], path, method, copy.deepcopy(body)))
        result = self.original(dict(provider, id=main), path, query, method=method, body=body)
        if method == 'POST' and not path.endswith('/milestones'):
            category = 'comments' if path.endswith(('/comments', '/notes')) else 'issues'
            self.f.data[main][category][-1]['user'] = author
            result['user'] = author
        return result

    def test_deleted_foreign_comment_is_not_recreated_by_bot(self):
        self.f.sync()
        self.f.data['fj']['comments'].append(dict(id=50, body='Foreign comment', user={'id': 777, 'login': 'external'}))
        self.f.sync()
        self.assertEqual(self.f.data['gh']['comments'][0]['user']['id'], self.bots['gh'])
        self.f.data['gh']['comments'].clear()
        self.requests.clear()
        self.f.sync()
        self.f.sync()
        self.assertFalse(self.f.data['gh']['comments'])
        self.assertFalse(self.requests)

    def test_delete_decision_uses_original_bot_writer_for_copies(self):
        import conflicts
        self.f.data['gh']['comments'].append(dict(id=50, body='Foreign', user={'id': 777, 'login': 'external'}))
        self.f.sync()
        self.f.data['gh']['comments'].clear()
        item = next(r for r in self.f.sync()['items'] if r.get('kind') == 'comment_deletion')
        self.requests.clear()
        result = conflicts.resolve(self.f.store, self.f.repo, {'conflict_id': item['resolution_id'], 'choice': 'delete_copies'}, 'admin')
        self.assertEqual(result['status'], 'success')
        deletes = [(provider, method) for provider, _, _, method, _ in self.requests]
        self.assertEqual(set(deletes), {('gl_bot', 'DELETE'), ('fj_bot', 'DELETE')})

    def test_own_issue_and_comment_use_personal_accounts(self):
        self.f.data['gh']['comments'].append(dict(id=50, body='My comment', user={'id': 11, 'login': 'michael-gh'}))
        self.f.sync()
        for name in ('gl', 'fj'):
            self.assertEqual(self.f.data[name]['issues'][0]['user']['id'], self.personal[name])
            self.assertEqual(self.f.data[name]['comments'][0]['user']['id'], self.personal[name])
        self.assertFalse(any('_bot' in identifier for identifier, _, _, _, _ in self.requests))

    def test_foreign_issue_and_comments_use_bots_not_same_username(self):
        self.f.data['gh']['issues'][0]['user'] = {'id': 555, 'login': 'NikoMuffin'}
        self.f.data['gh']['comments'].append(dict(id=50, body='Foreign', user={'id': 777, 'login': 'michael-gh'}))
        self.f.sync()
        for name in ('gl', 'fj'):
            self.assertEqual(self.f.data[name]['issues'][0]['user']['id'], self.bots[name])
            self.assertEqual(self.f.data[name]['comments'][0]['user']['id'], self.bots[name])
        posts = [r for r in self.requests if r[3] == 'POST']
        self.assertTrue(posts)
        self.assertTrue(all(identifier.endswith('_bot') and secret.startswith('bot-') for identifier, secret, _, _, _ in posts))
        self.requests.clear()
        self.f.sync()
        self.assertFalse([r for r in self.requests if r[3] == 'POST'])

    def test_reverse_own_and_foreign_forgejo_comments(self):
        self.f.sync()
        self.f.data['fj']['comments'].extend([
            dict(id=50, body='My Forgejo comment', user={'id': 33, 'login': 'michael-fj'}),
            dict(id=51, body='Another author', user={'id': 999, 'login': 'NikoMuffin'})])
        self.f.sync()
        for name in ('gh', 'gl'):
            own = next(r for r in self.f.data[name]['comments'] if r['body'].startswith('My Forgejo comment'))
            other = next(r for r in self.f.data[name]['comments'] if r['body'].startswith('Another author'))
            self.assertEqual(own['user']['id'], self.personal[name])
            self.assertEqual(other['user']['id'], self.bots[name])

    def test_updates_keep_original_writer_even_when_routing_disabled(self):
        self.f.data['gh']['comments'].append(dict(id=50, body='Foreign', user={'id': 999, 'login': 'NikoMuffin'}))
        self.f.sync()
        authorship.save(self.f.store, {'id': 'gl', 'enabled': False})
        self.f.data['gh']['comments'][0]['body'] = 'Foreign edited'
        self.requests.clear()
        self.f.sync()
        writes = [r for r in self.requests if r[3] in ('PUT', 'PATCH') and ('/notes/' in r[2] or '/comments/' in r[2])]
        self.assertTrue(writes)
        self.assertTrue(all(r[0].endswith('_bot') for r in writes))
        self.assertEqual(self.f.data['gl']['comments'][0]['user']['id'], 922)

    def test_new_remote_uses_original_author_after_origin_remote_removed(self):
        self.f.repo['remotes'].pop('gl'); self.f.repo['remote_providers'].pop('gl')
        self.f.sync()
        self.f.data['fj']['comments'].append(dict(id=50, body='Foreign original', user={'id': 999, 'login': 'NikoMuffin'}))
        self.f.sync()
        self.f.repo['remotes'].pop('fj'); self.f.repo['remote_providers'].pop('fj')
        self.f.repo['remotes']['gl'] = 'https://gl.example/team/repo.git'; self.f.repo['remote_providers']['gl'] = 'gl'
        self.f.sync()
        self.assertEqual(self.f.data['gl']['comments'][0]['user']['id'], 922)
        self.assertIn('Autor: NikoMuffin', self.f.data['gl']['comments'][0]['body'])

    def test_one_way_comments_use_matching_accounts(self):
        self.f.repo.update(direction='one-way')
        self.f.data['gh']['comments'].extend([
            dict(id=50, body='Own', user={'id': 11, 'login': 'michael-gh'}),
            dict(id=51, body='Foreign', user={'id': 999, 'login': 'NikoMuffin'})])
        self.f.sync()
        for name in ('gl', 'fj'):
            own = next(r for r in self.f.data[name]['comments'] if r['body'].startswith('Own'))
            other = next(r for r in self.f.data[name]['comments'] if r['body'].startswith('Foreign'))
            self.assertEqual(own['user']['id'], self.personal[name])
            self.assertEqual(other['user']['id'], self.bots[name])

    def test_legacy_copies_keep_real_author_and_are_not_recreated(self):
        for name in self.f.kinds:
            authorship.save(self.f.store, {'id': name, 'enabled': False})
        self.f.data['gh']['comments'].append(dict(id=50, body='Foreign', user={'id': 999, 'login': 'NikoMuffin'}))
        self.f.sync()
        for name in self.f.kinds:
            authorship.save(self.f.store, {'id': name, 'enabled': True, 'bot_provider': name + '_bot'})
        self.requests.clear()
        self.f.sync()
        self.assertFalse([r for r in self.requests if r[3] == 'POST'])
        self.f.data['gh']['comments'][0]['body'] = 'Updated foreign'
        self.f.sync()
        for name in ('gl', 'fj'):
            self.assertEqual(len(self.f.data[name]['comments']), 1)
            self.assertEqual(self.f.data[name]['comments'][0]['user']['id'], self.personal[name])

    def test_foreign_pull_requests_are_created_by_bot_accounts(self):
        self.f.repo.update(issues=False, milestones=False, pull_requests=True)
        self.f.data['gh']['issues'][0].update(user={'id': 999, 'login': 'NikoMuffin'}, head={'ref': 'feature', 'sha': 'abc'}, base={'ref': 'main'})
        refs = {name: {'heads/main': 'base', 'heads/feature': 'abc'} for name in self.f.kinds}
        result = mesh.metadata.MetadataSync(self.f.store).sync(self.f.repo, refs)
        self.assertEqual(result['errors'], [])
        for name in ('gl', 'fj'):
            self.assertEqual(self.f.data[name]['issues'][0]['user']['id'], self.bots[name])

    def test_bot_failure_never_falls_back_to_personal_identity(self):
        self.f.data['gh']['comments'].append(dict(id=50, body='Foreign', user={'id': 999, 'login': 'NikoMuffin'}))
        attempts = []
        def failing(provider, path, query=None, *, method='GET', body=None):
            if method == 'POST' and path.endswith(('/notes', '/comments')):
                attempts.append(provider['id'])
                raise management.ProviderError('Permission denied', 403)
            return self.api(provider, path, query, method=method, body=body)
        with patch.object(self.f.store, 'api', side_effect=failing):
            result = mesh.metadata.MetadataSync(self.f.store).sync(self.f.repo, {})
        self.assertTrue(result['errors'])
        self.assertEqual(set(attempts), {'gl_bot', 'fj_bot'})
        self.assertFalse(self.f.data['gl']['comments'])
        self.assertFalse(self.f.data['fj']['comments'])

    def test_lost_bot_create_response_recovers_writer_without_duplicate(self):
        self.f.data['gh']['comments'].append(dict(id=50, body='Foreign', user={'id': 999, 'login': 'NikoMuffin'}))
        def lost(provider, path, query=None, *, method='GET', body=None):
            response = self.api(provider, path, query, method=method, body=body)
            if provider['id'] == 'gl_bot' and method == 'POST' and path.endswith('/notes'):
                raise management.ProviderError('Response lost', 503)
            return response
        with patch.object(self.f.store, 'api', side_effect=lost):
            self.assertTrue(mesh.metadata.MetadataSync(self.f.store).sync(self.f.repo, {})['errors'])
        self.f.sync()
        self.assertEqual(len(self.f.data['gl']['comments']), 1)
        self.f.data['gh']['comments'][0]['body'] = 'Edited after recovery'
        self.requests.clear()
        self.f.sync()
        edits = [r for r in self.requests if '/notes/' in r[2] and r[3] == 'PUT']
        self.assertEqual([r[0] for r in edits], ['gl_bot'])

    def test_same_account_and_different_host_cannot_be_bots(self):
        with self.assertRaises(ValueError):
            authorship.save(self.f.store, {'id': 'gh', 'enabled': True, 'bot_provider': 'gl_bot'})
        with self.assertRaises(ValueError):
            authorship.save(self.f.store, {'id': 'gh', 'enabled': True, 'bot_provider': 'gh'})
        self.f.store.save_provider(dict(id='gh_bot', name='Not a bot', kind='github', url='https://gh.example', token='personal-gh'))
        with self.assertRaises(ValueError):
            authorship.save(self.f.store, {'id': 'gh', 'enabled': True, 'bot_provider': 'gh_bot'})

    def test_bot_dependencies_and_secrets_are_protected(self):
        public = json.dumps(authorship.settings(self.f.store, 'gh'))
        self.assertIn('michael-gh', public)
        self.assertIn('gitmesh-bot-gh', public)
        self.assertNotIn('personal-gh', public)
        self.assertFalse(b'bot-secret-gh' in (self.f.store.data / 'history.db').read_bytes(), 'Bot credential must remain encrypted')
        with self.assertRaises(ValueError):
            self.f.store.delete_provider('gh_bot', {'repositories': [self.f.repo]})
        with self.assertRaises(ValueError):
            self.f.store.save_provider(dict(id='gh_bot', name='Moved bot', kind='github', url='https://other.example', token='new'))
        self.f.data['gh']['comments'].append(dict(id=50, body='Foreign', user={'id': 999, 'login': 'NikoMuffin'}))
        self.f.sync()
        authorship.save(self.f.store, {'id': 'gl', 'enabled': False})
        with self.assertRaises(ValueError):
            self.f.store.delete_provider('gl_bot', {'repositories': [self.f.repo]})


class AuthorshipHttpTests(HttpCase):
    def test_configuration_roundtrip_exposes_no_credentials(self):
        for identifier in ('personal', 'bot'):
            mesh.control().save_provider({'id': identifier, 'name': identifier, 'kind': 'forgejo', 'url': 'https://forge.example', 'token': identifier + '-private-token'})
        headers = {'Authorization': 'Bearer ' + 'a' * 32}
        def user(_store, provider, path, *args, **kwargs):
            self.assertEqual(path, '/user')
            return {'id': 1 if provider['id'] == 'personal' else 2, 'login': 'michael' if provider['id'] == 'personal' else 'GitMesh-Bot'}
        with patch.object(management.Control, 'api', autospec=True, side_effect=user):
            status, data = self.request('POST', '/api/authorship', json.dumps({'id': 'personal', 'enabled': True, 'bot_provider': 'bot'}).encode(), headers)
        self.assertEqual(status, 200, data)
        status, data = self.request('GET', '/api/authorship?id=personal', headers=headers)
        value = json.loads(data)
        self.assertEqual(status, 200)
        self.assertEqual(value['personal_account'], 'michael')
        self.assertEqual(value['bot_account'], 'GitMesh-Bot')
        self.assertTrue(value['enabled'])
        self.assertNotIn('private-token', data.decode())

    def test_admin_only_and_operation_gate(self):
        self.assertEqual(self.request('POST', '/api/authorship', b'{}')[0], 401)
        with patch.object(mesh.Handler, 'actor', return_value={'name': 'reader', 'role': 'readonly'}), patch.object(authorship, 'save') as save:
            self.assertEqual(self.request('POST', '/api/authorship', b'{}')[0], 403)
            save.assert_not_called()
        headers = {'Authorization': 'Bearer ' + 'a' * 32}
        with patch.object(mesh, 'MAINTENANCE', True):
            self.assertEqual(self.request('POST', '/api/authorship', b'{}', headers)[0], 409)


if __name__ == '__main__': unittest.main()
