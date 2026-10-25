import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import test_sync
import management
import decisions
import conflicts
import inspection
import runtime

class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=management.Control(self.temp.name)
        self.repo={'name':'demo','source':'source','remotes':{'source':'https://source.example/team/repo','target':'https://target.example/team/repo'}}
        self.config={'repositories':[self.repo]}
        self.identifier=conflicts.remember(self.store,self.repo,dict(kind='label',category='labels',bindings={},versions={'source':{},'target':{}}))
        self.item=dict(action='conflicts/resolve',body=dict(profile='demo',conflict_id=self.identifier,choice='ignore'))
        self.mesh=SimpleNamespace(DATA=Path(self.temp.name),control=lambda:self.store,load_config=lambda:self.config,_repository_action=Mock(return_value={'status':'success'}))

    def enqueue(self): return decisions.enqueue(self.store,self.config,{'items':[self.item]},{'name':'bootstrap','role':'admin'})

    def test_enqueue_during_scan_is_durable_and_execution_waits(self):
        with runtime.file_lock(self.temp.name):
            queued=self.enqueue()
            self.assertFalse(decisions.run_one(self.mesh))
            self.mesh._repository_action.assert_not_called()
        other=management.Control(self.temp.name)
        self.assertTrue(decisions.pending(other))
        self.assertTrue(decisions.run_one(self.mesh))
        self.assertEqual(decisions.listing(other)['items'][0]['status'],'success')
        self.assertEqual(self.mesh._repository_action.call_count,1)
        self.assertFalse(decisions.run_one(self.mesh))

    def test_duplicate_submission_is_idempotent(self):
        self.assertEqual(self.enqueue()['ids'],self.enqueue()['ids'])
        self.assertEqual(len(decisions.listing(self.store)['items']),1)

    def test_changed_config_or_snapshot_never_executes_blindly(self):
        self.enqueue(); self.repo['source']='changed'
        decisions.run_one(self.mesh)
        self.mesh._repository_action.assert_not_called()
        self.assertEqual(decisions.listing(self.store)['items'][0]['status'],'needs_review')
        self.repo['source']='source'; self.enqueue()
        self.mesh._repository_action.side_effect=inspection.StaleState('Changed snapshot')
        decisions.run_one(self.mesh)
        self.assertEqual(decisions.listing(self.store)['items'][0]['status'],'needs_review')

    def test_invalid_choice_and_cancel(self):
        self.item['body']['choice']='unsafe'
        with self.assertRaises(inspection.StaleState): self.enqueue()
        self.item['body']['choice']='ignore'; identifier=self.enqueue()['ids'][0]
        decisions.cancel(self.store,identifier)
        self.assertFalse(decisions.pending(self.store))
        with self.assertRaises(inspection.StaleState): decisions.cancel(self.store,identifier)

    def test_interrupted_and_failed_writes_are_never_retried(self):
        self.enqueue()
        with self.store.db() as db: db.execute("UPDATE decisions SET status='running'")
        decisions.recover(self.store)
        self.assertEqual(decisions.listing(self.store)['items'][0]['status'],'needs_review')
        self.assertFalse(decisions.run_one(self.mesh))
        self.enqueue(); self.mesh._repository_action.side_effect=RuntimeError('secret-token')
        decisions.run_one(self.mesh)
        result=decisions.listing(self.store)['items'][0]
        self.assertEqual(result['status'],'error')
        self.assertNotIn('secret-token',str(result))
        self.assertFalse(decisions.run_one(self.mesh))

from test_http import HttpCase
from test_sync import mesh
from unittest.mock import patch
class DecisionHttpTests(HttpCase):
    def test_auth_and_roles(self):
        self.assertEqual(self.request('GET','/api/decisions')[0],401)
        for route in ('enqueue','cancel'):
            self.assertEqual(self.request('POST','/api/decisions/'+route,b'{}')[0],401)
            with patch.object(mesh.Handler,'actor',return_value={'name':'reader','role':'readonly'}):
                self.assertEqual(self.request('POST','/api/decisions/'+route,b'{}')[0],403)
        with patch.object(mesh.Handler,'actor',return_value={'name':'reader','role':'readonly'}):
            self.assertEqual(self.request('GET','/api/decisions')[0],403)

    def test_admin_can_load_saved_decisions(self):
        with patch.object(mesh.Handler,'actor',return_value={'name':'bootstrap','role':'admin'}):
            status,payload=self.request('GET','/api/decisions')
            self.assertEqual(status,200)
            self.assertIn('items',json.loads(payload))
