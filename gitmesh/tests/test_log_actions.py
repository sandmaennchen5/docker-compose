"""Log deletion and acknowledgement preserve live jobs and original sync results."""
import json
import sqlite3
import time
import unittest
from test_http import HttpCase, mesh
import management

class LogActionsTests(HttpCase):
    def setUp(self):
        super().setUp()
        self.store = mesh.control()
        self.store.record_action('demo', 'sync', {'conflicts': ['heads/main']}, 'admin', 'conflict')
        self.conflict = self.store.jobs()[0]['id']
        self.store.record_action('other', 'sync', {'message': 'network failed'}, 'admin', 'error')
        self.error = self.store.jobs()[0]['id']
        self.store.record_action('demo', 'sync', {'updated': ['main']}, 'admin', 'success')
        self.success = self.store.jobs()[0]['id']
        self.running = self.store.enqueue(['active'])[0]
        self.store.claim()
        self.queued = self.store.enqueue(['waiting'])[0]
        self.before = time.time()

    def api(self, path, body, token=None):
        headers = {'Authorization': 'Bearer ' + ('a' * 32 if token is None else token), 'Content-Type': 'application/json'}
        code, data = self.request('POST', '/api/jobs/' + path, json.dumps(body).encode(), headers)
        return code, json.loads(data)

    def test_reset_preserves_original_result_and_live_queue_across_restart(self):
        code, result = self.api('reset', {'id': self.conflict})
        self.assertEqual(code, 200); self.assertEqual(result['changed'], 1)
        jobs = {j['id']: j for j in management.Control(self.store.data).jobs()}
        self.assertEqual(jobs[self.conflict]['status'], 'conflict')
        self.assertEqual(jobs[self.conflict]['details']['conflicts'], ['heads/main'])
        self.assertTrue(jobs[self.conflict]['reset_at'])
        self.assertTrue(jobs[self.conflict]['reset_by'])
        self.assertEqual(jobs[self.running]['status'], 'running')
        self.assertEqual(jobs[self.queued]['status'], 'queued')
        self.assertEqual(self.api('reset', {'id': self.conflict})[1]['changed'], 0)
        self.assertEqual(self.api('reset', {'before': self.before})[1]['changed'], 1)
        self.store.finish(self.running, 'error', {'message': 'new failure'})
        self.assertIsNone(next(j for j in self.store.jobs() if j['id'] == self.running)['reset_at'])

    def test_bulk_profile_and_snapshot_cannot_reset_newer_results(self):
        self.assertEqual(self.api('reset', {'before': self.before, 'profile': 'demo'})[1]['changed'], 1)
        self.assertIsNone(next(j for j in self.store.jobs() if j['id'] == self.error)['reset_at'])
        self.store.record_action('demo', 'sync', {'message': 'new failure'}, 'admin', 'error')
        newer = self.store.jobs()[0]['id']
        self.assertEqual(self.api('reset', {'before': self.before})[1]['changed'], 1)
        self.assertIsNone(next(j for j in self.store.jobs() if j['id'] == newer)['reset_at'])

    def test_delete_only_completed_logs_and_remove_reset_records_and_legacy_runs(self):
        self.api('reset', {'id': self.conflict})
        with mesh.database() as db:
            db.execute('INSERT INTO runs(time,repository,status,details) VALUES(?,?,?,?)', (self.before - 1, 'demo', 'error', '{}'))
            db.execute('INSERT INTO runs(time,repository,status,details) VALUES(?,?,?,?)', (time.time(), 'active', 'error', '{}'))
        code, result = self.api('delete', {'before': self.before})
        self.assertEqual(code, 200); self.assertEqual(result['changed'], 3)
        self.assertEqual({j['id'] for j in self.store.jobs()}, {self.running, self.queued})
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_resets').fetchone()[0], 0)
            self.assertEqual([r[0] for r in db.execute('SELECT repository FROM runs')], ['active'])

    def test_single_delete_erases_only_its_legacy_log_and_bulk_keeps_later_completion(self):
        job = next(j for j in self.store.jobs() if j['id'] == self.success)
        with mesh.database() as db:
            db.execute('INSERT INTO runs(time,repository,status,details) VALUES(?,?,?,?)', (job['finished'], job['profile'], 'success', '{"message":"erase me"}'))
            db.execute('INSERT INTO runs(time,repository,status,details) VALUES(?,?,?,?)', (job['started'] - 1, job['profile'], 'error', '{"message":"keep me"}'))
        self.assertEqual(self.api('delete', {'id': self.success})[1]['changed'], 1)
        with mesh.database() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT details FROM runs')], ['{"message":"keep me"}'])
        self.store.finish(self.running, 'error', {'message': 'finished after snapshot'})
        self.api('delete', {'before': self.before})
        self.assertIn(self.running, {j['id'] for j in self.store.jobs()})

    def test_single_delete_and_ids_are_not_reused(self):
        self.store.record_action('demo', 'sync', {}, 'admin', 'error')
        highest = self.store.jobs()[0]['id']
        self.assertEqual(self.api('delete', {'id': highest})[1]['changed'], 1)
        next_id = self.store.enqueue(['fresh'])[0]
        self.assertGreater(next_id, highest)

    def test_live_jobs_and_invalid_parameters_are_rejected(self):
        for action in ('delete', 'reset'):
            for body in ({'id': self.running}, {'id': self.queued}, {'id': True}, {'id': -1}, {'id': 999999}, {}, {'before': time.time()+1000}, {'before': float('nan')}, {'before': self.before, 'profile': '../demo'}, {'id': self.success, 'before': self.before}):
                with self.subTest(action=action, body=body): self.assertEqual(self.api(action, body)[0], 400)
        self.assertEqual(len(self.store.jobs()), 5)

    def test_readers_and_unauthenticated_cannot_mutate_logs(self):
        self.store.save_user({'name': 'reader', 'role': 'readonly', 'password': 'reader-test-password', 'enabled': True})
        token = self.store.login('reader', 'reader-test-password', 'isolated-test')['token']
        for action in ('delete', 'reset'):
            self.assertEqual(self.api(action, {'id': self.conflict}, token=token)[0], 403)
            self.assertEqual(self.api(action, {'id': self.conflict}, token='invalid')[0], 401)
        self.assertEqual(len(self.store.jobs()), 5)

class OldDatabaseTests(unittest.TestCase):
    def test_old_database_is_migrated_without_losing_queue_or_deduplication(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(Path(directory) / 'history.db') as db:
                db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, profile TEXT NOT NULL, origin TEXT NOT NULL, status TEXT NOT NULL, requested REAL NOT NULL, started REAL, finished REAL, attempts INTEGER NOT NULL DEFAULT 0, details TEXT NOT NULL DEFAULT '{}')")
                db.execute("INSERT INTO jobs(id,profile,origin,status,requested) VALUES(42,'demo','manual','queued',1)")
            db.close()
            store = management.Control(directory)
            self.assertEqual(store.jobs()[0]['id'], 42)
            self.assertEqual(store.enqueue(['demo']), [])
            store.finish(42, 'error', {})
            store.manage_logs('delete', {'id': 42}, 'test')
            self.assertGreater(store.enqueue(['demo'])[0], 42)

if __name__ == '__main__': unittest.main()
