"""Generic object reviews and durable decisions, using isolated API fixtures."""
import copy
import unittest
from unittest.mock import patch
import test_metadata_mesh as fixture
from test_http import HttpCase, mesh
import reconciliation as review
import conflicts
import metadata
import inspection
import test_sync as sync_fixture

class ObjectReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.MeshTests('test_native_forgejo_milestone_and_comments_reach_both_other_providers')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.repo.update(milestones=False, comments=False)
        self.f.sync()
        self.target = str(self.f.data['fj']['issues'][0]['number'])

    def compare(self, **changes):
        body = dict(category='issues', source_key='1', target_key=self.target, target='fj')
        body.update(changes)
        return review.prepare(self.f.store, self.f.repo, body)

    def decide(self, item, choice):
        return conflicts.resolve(self.f.store, self.f.repo, {'conflict_id': item['resolution_id'], 'choice': choice}, 'admin')

    def test_ignored_milestone_is_not_changed_by_bidirectional_sync(self):
        self.f.repo['milestones']=True
        self.f.data['gh']['milestones'].append(dict(id=9,number=9,title='v1',description='Old',state='open'))
        self.f.sync()
        number=str(self.f.data['fj']['milestones'][0]['number'])
        item=review.prepare(self.f.store,self.f.repo,dict(category='milestones',target='fj',source_key='9',target_key=number))
        self.decide(item,'ignore')
        self.f.data['gh']['milestones'][0]['description']='New plan'
        self.f.sync()
        self.assertIn('Old',self.f.data['fj']['milestones'][0]['description'])
        self.assertIn('New plan',self.f.data['gl']['milestones'][0]['description'])

    def test_target_status_changes_require_review(self):
        self.f.data['fj']['issues'][0]['state']='closed'
        item = next(r for r in self.f.sync()['items'] if r.get('kind')=='object_review')
        self.assertEqual(self.f.data['fj']['issues'][0]['state'],'closed')
        self.decide(item,'ignore')
        self.f.sync()
        self.assertEqual(self.f.data['fj']['issues'][0]['state'],'closed')

    def test_one_way_deleted_source_comment_requires_review(self):
        self.f.repo.update(direction='one-way',comments=True)
        self.f.data['gh']['comments'].append(dict(id=4,body='Gone'))
        self.f.sync()
        self.f.data['gh']['comments'].clear()
        item = next(r for r in self.f.sync()['items'] if r.get('kind')=='object_review' and r['category']=='comments' and r['target']=='fj')
        self.decide(item,'delete')
        self.assertFalse(self.f.data['fj']['comments'])

    def test_target_changes_require_review_before_overwriting(self):
        self.f.data['fj']['issues'][0]['body'] = 'Local edit'
        result = self.f.sync()
        item = next(r for r in result['items'] if r.get('kind') == 'object_review')
        self.assertEqual(self.f.data['fj']['issues'][0]['body'], 'Local edit')
        self.assertIn('Local edit', item['values']['fj']['body'])
        self.decide(item, 'apply_source')
        self.f.sync()
        self.assertIn('Original', self.f.data['fj']['issues'][0]['body'])

    def test_deleted_target_can_be_restored_with_a_new_number(self):
        self.f.data['fj']['issues'].clear()
        item = next(r for r in self.f.sync()['items'] if r.get('kind') == 'object_review')
        self.assertIn('restore', item['choices'])
        self.decide(item, 'restore')
        self.assertEqual(len(self.f.data['fj']['issues']), 1)
        self.assertNotEqual(str(self.f.data['fj']['issues'][0]['number']), self.target)
        self.f.sync()
        self.assertEqual(len(self.f.data['fj']['issues']), 1)

    def test_deleted_source_is_detected_and_copies_can_be_closed(self):
        self.f.data['gh']['issues'].clear()
        item = next(r for r in self.f.sync()['items'] if r.get('kind') == 'object_review' and r['target'] == 'fj')
        self.assertEqual(item['values']['gh']['status'], 'Fehlt / geloescht')
        self.decide(item, 'close')
        self.assertEqual(self.f.data['fj']['issues'][0]['state'], 'closed')
        self.assertNotIn('delete', item['choices'])

    def test_ignore_persists_and_can_be_resumed(self):
        item = self.compare()
        self.decide(item, 'ignore')
        self.f.data['gh']['issues'][0]['title'] = 'New source'
        self.f.sync()
        self.assertEqual(self.f.data['fj']['issues'][0]['title'], 'Bug')
        hold = next(h for h in review.catalog(self.f.store)['holds'] if h['item'] == 'issues:1')
        review.resume_hold(self.f.store, self.f.repo, hold)
        self.f.sync()
        self.assertEqual(self.f.data['fj']['issues'][0]['title'], 'New source')

    def test_changed_snapshot_or_repository_prevents_all_writes(self):
        item = self.compare()
        self.f.data['fj']['issues'][0]['title'] = 'Newer'
        self.f.writes.clear()
        with self.assertRaises(inspection.StaleState):
            self.decide(item, 'apply_source')
        self.assertFalse(self.f.writes)
        with self.assertRaises(inspection.StaleState):
            conflicts.resolve(self.f.store, dict(self.f.repo, enabled=False), {'conflict_id':item['resolution_id'], 'choice':'ignore'}, 'admin')

    def test_unsupported_action_is_rejected_and_catalog_is_paginated(self):
        item = self.compare()
        self.f.writes.clear()
        with self.assertRaises(ValueError):
            self.decide(item, 'delete')
        self.assertFalse(self.f.writes)
        for n in range(52):
            conflicts.remember(self.f.store, self.f.repo, dict(kind='object_review',category='issues',item=str(n),bindings={},versions={},choices=['ignore']))
        self.assertEqual(len(review.catalog(self.f.store)['items']), 50)
        self.assertIsNotNone(review.catalog(self.f.store)['next_page'])
        self.assertGreater(len(review.catalog(self.f.store, 2)['items']), 0)

    def test_inventory_contains_actual_provider_numbers(self):
        data = review.inventory(self.f.store, self.f.repo, {'category':'issues','target':'fj'}, None)
        self.assertEqual(data['items']['gh'][0]['key'], '1')
        self.assertEqual(data['items']['fj'][0]['key'], self.target)

    def test_deleted_comment_can_be_explicitly_restored(self):
        self.f.repo['comments'] = True
        self.f.data['gh']['comments'].append(dict(id=4, body='Restore me'))
        self.f.sync()
        self.f.data['gh']['comments'].clear()
        item = next(r for r in self.f.sync()['items'] if r.get('kind') == 'comment_deletion')
        self.decide(item, 'restore')
        self.assertEqual(len(self.f.data['gh']['comments']), 1)
        self.f.sync()
        self.assertEqual(len(self.f.data['gh']['comments']), 1)

    def test_deleted_milestone_waits_for_decision_and_can_be_restored(self):
        self.f.repo['milestones'] = True
        self.f.data['gh']['milestones'].append(dict(id=9, number=9, title='Release plan',description='',state='open'))
        self.f.sync()
        self.f.data['fj']['milestones'].clear()
        item = next(r for r in self.f.sync()['items'] if r.get('kind') == 'comment_deletion')
        self.assertFalse(self.f.data['fj']['milestones'])
        self.decide(item, 'restore')
        self.assertEqual(self.f.data['fj']['milestones'][0]['title'], 'Release plan')

class CapabilityReviewTests(unittest.TestCase):
    def setUp(self):
        ObjectReviewTests.setUp(self)
    decide = ObjectReviewTests.decide

    def test_repository_details_copy_website_and_topics(self):
        info = {'gh':dict(id=10,description='Source',homepage='https://example.org',topics=['alpha']),
                'gl':dict(id=11,description='Old',topics=[]),
                'fj':dict(id=12,description='Old',website='',topics=[])}
        original = self.f.api
        def api(p,path,query=None,*,method='GET',body=None):
            root = '/projects/team%2Frepo' if p['kind']=='gitlab' else '/repos/team/repo'
            if path==root:
                if method!='GET': info[p['id']].update(body)
                return copy.deepcopy(info[p['id']])
            if path==root+'/topics':
                info[p['id']]['topics']=body.get('names',body.get('topics'))
                return {}
            return original(p,path,query,method=method,body=body)
        self.f.store.api.side_effect=api
        item=review.prepare(self.f.store,self.f.repo,dict(category='repository_details',target='fj'))
        self.decide(item,'apply_source')
        self.assertEqual(info['fj']['description'],'Source')
        self.assertEqual(info['fj']['website'],'https://example.org')
        self.assertEqual(info['fj']['topics'],['alpha'])

    def test_pr_restore_checks_exact_target_head_before_writing(self):
        from types import SimpleNamespace
        self.f.repo.update(issues=False,pull_requests=True)
        self.f.data['gh']['issues'][0].update(head={'ref':'feature','sha':'commit-1'},base={'ref':'main'})
        self.f.data['fj']['issues'].clear()
        item=review.prepare(self.f.store,self.f.repo,dict(category='pull_requests',target='fj',source_key='1',target_key='999'))
        tools=SimpleNamespace(fetch=lambda *a,**kw:(None,{'fj':{'heads/main':'base','heads/feature':'wrong'}},[],[]))
        self.f.writes.clear()
        with self.assertRaises(ValueError):
            conflicts.resolve(self.f.store,self.f.repo,dict(conflict_id=item['resolution_id'],choice='restore'),'admin',tools=tools)
        self.assertFalse(self.f.writes)
        tools.fetch=lambda *a,**kw:(None,{'fj':{'heads/main':'base','heads/feature':'commit-1'}},[],[])
        self.assertEqual(conflicts.resolve(self.f.store,self.f.repo,dict(conflict_id=item['resolution_id'],choice='restore'),'admin',tools=tools)['status'],'success')
        self.assertEqual(len(self.f.data['fj']['issues']),1)

    def test_asset_replacement_requires_review_and_delete_requires_mapping(self):
        assets={'gh':[dict(id=3,name='app.zip',size=1)],'fj':[dict(id=90,name='app.zip',size=2)]}
        original=self.f.api
        def api(p,path,query=None,*,method='GET',body=None):
            if '/releases' not in path: return original(p,path,query,method=method,body=body)
            if path.endswith('/releases/tags/v1'): return dict(id=5,tag_name='v1',name='Release')
            key=path.split('/')[-1]
            row=next((r for r in assets[p['id']] if str(r['id'])==key),None)
            if row is None: raise fixture.management.ProviderError('Missing',404)
            if method=='DELETE': assets[p['id']].remove(row); return {}
            return copy.deepcopy(row)
        self.f.store.api.side_effect=api
        request=dict(category='assets',target='fj',source_key='3',target_key='90',tag='v1')
        item=review.prepare(self.f.store,self.f.repo,request)
        self.assertNotIn('delete',item['choices'])
        with patch('releases.ReleaseSync.copy_asset',return_value='updated') as copy_asset:
            self.decide(item,'apply_source')
            copy_asset.assert_called_once()
        item=review.prepare(self.f.store,self.f.repo,request)
        self.assertIn('delete',item['choices'])
        self.decide(item,'delete')
        self.assertFalse(assets['fj'])
        self.assertEqual(len(assets['gh']),1)


class ReviewHttpTests(HttpCase):
    def test_catalog_requires_login_and_actions_require_admin(self):
        self.assertEqual(self.request('GET','/api/conflicts')[0],401)
        for route in ('compare','inventory','resume'):
            with patch.object(mesh.Handler,'actor',return_value={'name':'reader','role':'readonly'}):
                self.assertEqual(self.request('POST','/api/conflicts/'+route,b'{}')[0],403)
        with patch.object(mesh.Handler,'actor',return_value={'name':'reader','role':'readonly'}):
            self.assertEqual(self.request('GET','/api/conflicts')[0],200)



class GitReviewTests(sync_fixture.SyncFixture):
    def setUp(self):
        super().setUp()
        self.repo['source'] = 'github'
        self.store = mesh.control()
        self.tools = mesh.repository_tools()

    def test_restore_delete_and_ignore_preserve_annotated_tag_objects(self):
        self.command(self.work, '-c', 'tag.gpgsign=false', 'tag', '-a', 'v1', '-m', 'signed object shape')
        self.command(self.work, 'push', self.remotes['github'], 'refs/tags/v1')
        item = review.prepare_git(self.store,self.repo,dict(category='tags',target='forgejo',source_key='v1'),self.tools)
        conflicts.resolve(self.store,self.repo,dict(conflict_id=item['resolution_id'],choice='restore'),'admin',tools=self.tools)
        original = self.tip('github','refs/tags/v1')
        self.assertEqual(self.tip('forgejo','refs/tags/v1'), original)
        item = review.prepare_git(self.store,self.repo,dict(category='tags',target='forgejo',source_key='v1'),self.tools)
        conflicts.resolve(self.store,self.repo,dict(conflict_id=item['resolution_id'],choice='delete'),'admin',tools=self.tools)
        self.assertNotEqual(self.command(self.root/'forgejo','rev-parse','--verify','refs/tags/v1',check=False).returncode,0)
        mesh.synchronize(self.repo)
        self.assertNotEqual(self.command(self.root/'forgejo','rev-parse','--verify','refs/tags/v1',check=False).returncode,0)
        hold = next(h for h in review.catalog(self.store)['holds'] if h['item']=='tags/v1')
        review.resume_hold(self.store,self.repo,hold)
        mesh.synchronize(self.repo)
        self.assertEqual(self.tip('forgejo','refs/tags/v1'),original)

    def test_deleted_synced_branch_requires_review_instead_of_recreation(self):
        mesh.synchronize(self.repo)
        self.command(self.work,'push',self.remotes['forgejo'],':refs/heads/main')
        status, result = mesh.synchronize(self.repo)
        self.assertEqual(status,'conflict')
        self.assertTrue(result['git_reviews'])
        self.assertNotEqual(self.command(self.root/'forgejo','rev-parse','--verify','refs/heads/main',check=False).returncode,0)
        item = result['git_reviews'][0]
        conflicts.resolve(self.store,self.repo,dict(conflict_id=item['resolution_id'],choice='restore'),'admin',tools=self.tools)
        self.assertEqual(mesh.synchronize(self.repo)[0],'success')

    def test_git_restore_rejects_a_target_changed_after_comparison(self):
        item = review.prepare_git(self.store,self.repo,dict(category='branches',target='forgejo',source_key='main'),self.tools)
        newer = self.commit('Target changed')
        self.command(self.work,'push',self.remotes['forgejo'],'main')
        with self.assertRaises(inspection.StaleState):
            conflicts.resolve(self.store,self.repo,dict(conflict_id=item['resolution_id'],choice='restore'),'admin',tools=self.tools)
        self.assertEqual(self.tip('forgejo'),newer)

class ReleaseReviewTests(unittest.TestCase):
    decide = ObjectReviewTests.decide
    # This class deliberately has its own tests only, without inheriting the
    # metadata cases: fixture APIs below add release endpoints to real storage.
    def setUp(self):
        ObjectReviewTests.setUp(self)
        self.rels = {'gh':[dict(id=5,tag_name='v1',name='Release',body='Notes',draft=False,prerelease=False)],'gl':[],'fj':[]}
        original = self.f.api
        def api(p,path,query=None,*,method='GET',body=None):
            if '/releases' not in path:
                return original(p,path,query,method=method,body=body)
            rows = self.rels[p['id']]
            suffix = path.split('/releases',1)[1].strip('/')
            if method == 'GET' and not suffix:
                return copy.deepcopy(rows)
            key = suffix.removeprefix('tags/')
            row = next((r for r in rows if r.get('tag_name')==key or str(r.get('id'))==key),None)
            if method == 'GET':
                if row is None: raise fixture.management.ProviderError('Missing',404)
                return copy.deepcopy(row)
            self.f.writes.append((p['id'],path,method,copy.deepcopy(body)))
            if method == 'POST':
                row = dict(body)
                if p['kind']!='gitlab': row['id']=55
                rows.append(row)
            elif method == 'DELETE':
                rows.remove(row); return {}
            else: row.update(body)
            return copy.deepcopy(row)
        self.f.store.api.side_effect = api
        self.f.repo.update(releases=True,release_assets=False)
        self.refs = {n:{'tags/v1':'same'} for n in self.f.kinds}
        self.engine = __import__('releases').ReleaseSync(self.f.store)

    def test_release_target_edits_and_source_deletions_require_decisions(self):
        self.assertFalse(self.engine.sync(self.f.repo,self.refs)['errors'])
        self.rels['fj'][0]['body']='Local notes'
        result = self.engine.sync(self.f.repo,self.refs)
        item = next(r for r in result['review_items'] if r['target']=='fj')
        self.assertEqual(self.rels['fj'][0]['body'],'Local notes')
        self.decide(item,'ignore')
        self.assertFalse(self.engine.sync(self.f.repo,self.refs)['review_items'])
        hold = next(h for h in review.catalog(self.f.store)['holds'] if h['item'].startswith('releases:'))
        review.resume_hold(self.f.store,self.f.repo,hold)
        item = self.engine.sync(self.f.repo,self.refs)['review_items'][0]
        self.decide(item,'apply_source')
        self.engine.sync(self.f.repo,self.refs)
        self.rels['gh'].clear()
        item = next(r for r in self.engine.sync(self.f.repo,self.refs)['review_items'] if r['target']=='fj')
        self.assertIn('delete',item['choices'])
        self.decide(item,'delete')
        self.assertFalse(self.rels['fj'])

    def test_gitlab_release_without_numeric_id_can_be_compared(self):
        self.engine.sync(self.f.repo,self.refs)
        self.assertNotIn('id',self.rels['gl'][0])
        item = review.prepare(self.f.store,self.f.repo,dict(category='releases',target='gl',source_key='v1',target_key='v1'))
        self.assertEqual(item['values']['gl']['id'],'tag:v1')
        self.decide(item,'ignore')


if __name__ == '__main__': unittest.main()
