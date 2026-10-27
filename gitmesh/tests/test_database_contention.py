"""Regression coverage for worker failures caused by SQLite write contention."""
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from test_sync import mesh
import management
import decisions
import runtime

class DatabaseContentionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=management.Control(self.temp.name)
        decisions.schema(self.store)
        self.connect=sqlite3.connect

    def short_connection(self,*args,**kwargs):
        kwargs['timeout']=0.05
        return self.connect(*args,**kwargs)

    def test_readers_and_pending_do_not_need_writer_lock(self):
        writer=self.connect(self.store.data/'history.db'); self.addCleanup(writer.close)
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("UPDATE worker_state SET mode='paused' WHERE id=1")
        with patch.object(management.sqlite3,'connect',side_effect=self.short_connection):
            self.assertEqual(management.Control(self.temp.name).mode(),'running')
            self.assertFalse(decisions.pending(management.Control(self.temp.name)))
        writer.rollback()
        self.assertEqual(self.store.mode(),'running')

    def test_heartbeat_miss_does_not_crash_and_recovers_after_unlock(self):
        self.assertTrue(runtime.publish(self.store,{}))
        writer=self.connect(self.store.data/'history.db'); self.addCleanup(writer.close)
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("UPDATE worker_state SET mode='paused' WHERE id=1")
        with patch.object(management.sqlite3,'connect',side_effect=self.short_connection):
            self.assertFalse(runtime.publish(self.store,{},'waiting'))
        writer.rollback()
        self.assertTrue(runtime.publish(self.store,{},'recovered'))
        self.assertTrue(runtime.status(self.store)['online'])
        self.assertEqual(runtime.status(self.store)['notice'],'recovered')

    def test_initialization_failure_always_closes_connection(self):
        db=MagicMock(); db.execute.side_effect=sqlite3.OperationalError('database is locked')
        with patch.object(management.sqlite3,'connect',return_value=db):
            with self.assertRaises(sqlite3.OperationalError):
                with management.Control(self.temp.name).db(): pass
        db.close.assert_called_once()

    def test_legacy_job_schema_is_migrated_without_resetting_state(self):
        self.store.mode('paused')
        with self.store.db() as db:
            sql=db.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0]
            db.execute('DROP TABLE jobs')
            db.execute(sql.replace(' AUTOINCREMENT',''))
            db.execute("INSERT INTO jobs(id,profile,origin,status,requested) VALUES(17,'old','manual','success',1)")
        other=management.Control(self.temp.name)
        with other.db() as db:
            self.assertIn('AUTOINCREMENT',db.execute("SELECT sql FROM sqlite_master WHERE name='jobs'").fetchone()[0])
            self.assertEqual(db.execute('SELECT profile FROM jobs WHERE id=17').fetchone()[0],'old')
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(other.mode(),'paused')
        self.assertEqual(other.enqueue(['new'])[0],18)

    def test_non_lock_errors_are_not_silenced(self):
        with patch.object(self.store,'db',side_effect=sqlite3.OperationalError('database disk image is malformed')):
            with self.assertRaises(sqlite3.OperationalError): runtime.publish(self.store,{})
