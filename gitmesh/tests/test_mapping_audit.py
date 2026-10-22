"""Read-only inventories, ambiguity reporting and explicit local-only decisions."""
import copy
import unittest
from unittest.mock import patch
import test_metadata_mesh as fixture
import mapping_audit as audit
import inspection


class MappingAuditTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.MeshTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.data['gh']['milestones'].append(dict(id=2,number=2,title='v1',description='Plan',state='open'))
        self.f.data['gh']['comments'].append(dict(id=3,body='Discussion'))
        self.f.sync()
        self.original=self.f.api
        def api(provider,path,query=None,*,method='GET',body=None):
            if path.endswith(('/releases','/labels','/pulls','/merge_requests')): return []
            return self.original(provider,path,query,method=method,body=body)
        self.f.store.api.side_effect=api
        self.f.writes.clear()

    def run_audit(self):
        state=audit.start(self.f.store,self.f.repo)
        for _ in range(200):
            if state['done']: return state
            state=audit.step(self.f.store,self.f.repo,{'audit_id':state['id']})
        self.fail('audit did not finish')

    def test_inventory_is_read_only_and_report_has_no_credentials(self):
        result=self.run_audit()
        self.assertEqual(self.f.writes,[])
        self.assertTrue(result['done'])
        self.assertGreater(result['summary']['ok'],0)
        self.assertFalse(result['errors'],result)
        self.assertEqual(result['summary']['conflict'],0,result)
        text=__import__('json').dumps(result)
        self.assertNotIn('isolated',text)
        self.assertNotIn('token',text)

    def test_orphan_and_duplicate_markers_are_reported(self):
        item=copy.deepcopy(self.f.data['fj']['milestones'][0]); item.update(id=900,number=900)
        self.f.data['fj']['milestones'].append(item)
        result=self.run_audit()
        reasons=[r['reason'] for r in result['results']]
        self.assertIn('Kopiermarkierung ohne gespeicherte Zuordnung',reasons)
        self.assertIn('Kopiermarkierung mehrfach vorhanden',reasons)
        self.assertEqual(self.f.writes,[])

    def test_config_changes_reject_resume_and_repair(self):
        result=self.run_audit()
        self.f.repo['name']='changed'
        with self.assertRaises(ValueError): audit.step(self.f.store,self.f.repo,{'audit_id':result['id']})
        self.f.repo['name']='demo'; self.f.repo['issues']=False
        with self.assertRaises(inspection.StaleState): audit.step(self.f.store,self.f.repo,{'audit_id':result['id']})

    def test_unlink_is_explicit_local_and_stale_mapping_does_not_add_hold(self):
        result=self.run_audit()
        index=next(i for i,r in enumerate(result['results']) if r.get('item','').startswith('issues:'))
        row=result['results'][index]
        with self.f.store.db() as db:
            db.execute('UPDATE metadata_links SET target=? WHERE scope=? AND item=?',('999',row['scope'],row['item']))
        with self.assertRaises(inspection.StaleState):
            audit.repair(self.f.store,self.f.repo,dict(audit_id=result['id'],index=index,action='unlink'),'admin')
        with self.f.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM object_holds').fetchone()[0],0)
            db.execute('UPDATE metadata_links SET target=? WHERE scope=? AND item=?',(row['target_key'],row['scope'],row['item']))
        audit.repair(self.f.store,self.f.repo,dict(audit_id=result['id'],index=index,action='unlink'),'admin')
        with self.f.store.db() as db:
            self.assertIsNone(db.execute('SELECT * FROM metadata_links WHERE scope=? AND item=?',(row['scope'],row['item'])).fetchone())
        self.assertEqual(self.f.writes,[])
        self.f.sync()
        with self.f.store.db() as db:
            self.assertIsNone(db.execute('SELECT * FROM metadata_links WHERE scope=? AND item=?',(row['scope'],row['item'])).fetchone())

    def test_pair_preview_and_apply_recheck_objects_and_never_write_remotes(self):
        # An untracked copy can be explicitly linked after inspecting both objects.
        with self.f.store.db() as db:
            db.execute("DELETE FROM metadata_mesh WHERE category='milestones'")
            db.execute("DELETE FROM metadata_links WHERE item LIKE 'milestones:%'")
        result=self.run_audit()
        body=dict(audit_id=result['id'],category='milestones',source='gh',target='fj',source_key='2',target_key=str(self.f.data['fj']['milestones'][0]['number']))
        preview=audit.pair(self.f.store,self.f.repo,body)
        self.assertIn('confirmation',preview)
        self.f.data['fj']['milestones'][0]['state']='closed'
        with self.assertRaises(inspection.StaleState): audit.pair(self.f.store,self.f.repo,dict(body,confirmation=preview['confirmation']),apply=True)
        self.f.data['fj']['milestones'][0]['state']='open'
        audit.pair(self.f.store,self.f.repo,dict(body,confirmation=preview['confirmation']),apply=True)
        self.assertEqual(self.f.writes,[])
        with self.f.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM metadata_mesh WHERE category='milestones'").fetchone()[0],2)

    def test_issue_pair_adoption_preserves_remote_contents(self):
        with self.f.store.db() as db: db.execute("DELETE FROM metadata_links WHERE item LIKE 'issues:%'")
        result=self.run_audit()
        target=self.f.data['fj']['issues'][0]
        body=dict(audit_id=result['id'],category='issues',source='gh',target='fj',source_key='1',target_key=str(target['number']))
        preview=audit.pair(self.f.store,self.f.repo,body)
        audit.pair(self.f.store,self.f.repo,dict(body,confirmation=preview['confirmation']),apply=True)
        self.assertEqual(self.f.writes,[])
        with self.f.store.db() as db:
            self.assertEqual(db.execute("SELECT target FROM metadata_links WHERE item='issues:1'").fetchone()[0],str(target['number']))

    def test_git_inventory_failure_does_not_block_api_audit(self):
        from unittest.mock import Mock
        state=audit.start(self.f.store,self.f.repo)
        result=audit.step(self.f.store,self.f.repo,dict(audit_id=state['id'],git=True),Mock(inventory=Mock(side_effect=RuntimeError('private-token'))))
        self.assertEqual(result['pending'],state['pending'])
        self.assertEqual(result['results'][0]['status'],'not_checked')
        self.assertNotIn('private-token',str(result))

    def test_rate_limit_keeps_current_page_for_resume(self):
        state=audit.start(self.f.store,self.f.repo)
        with patch.object(self.f.store,'api',side_effect=fixture.management.RateLimited('pause',429)):
            result=audit.step(self.f.store,self.f.repo,{'audit_id':state['id']})
        self.assertTrue(result['paused']); self.assertEqual(result['pending'],state['pending'])
        self.assertFalse(self.f.writes)


from test_http import HttpCase
from test_sync import mesh

class MappingHttpTests(HttpCase):
    def test_every_mapping_route_requires_admin(self):
        for route in ('start','step','report','repair','pair-preview','pair-apply'):
            self.assertEqual(self.request('POST','/api/mappings/'+route,b'{}')[0],401)
            with patch.object(mesh.Handler,'actor',return_value={'name':'reader','role':'readonly'}):
                self.assertEqual(self.request('POST','/api/mappings/'+route,b'{}')[0],403)

if __name__=='__main__': unittest.main()
