"""Complete SQLite artifacts through the existing maintenance-gated kernel."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from fastapi.testclient import TestClient
import app
import database_backup
import main
import store
import test_model_review as fixture
from sandbox import KernelError, maintenance_access
from tool_router import Tool, ToolRouter


class DatabaseBackupTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ModelReviewTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.review()
        self.h = self.f.f.h

    def create(self):
        with maintenance_access():
            return main.dispatch('runtime.database.backup', {'confirmed_global': True}, self.h.context['context_token'])

    def verify(self, backup_id):
        with maintenance_access():
            return self.h.call('runtime.database.backup.check', {'backup_id': backup_id})

    def read(self, result, sql, args=()):
        path = Path(result['directory']) / 'database.sqlite3'
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def root(self):
        return Path(store.DB_PATH).resolve().parent / '.jobkit-backups'

    def change_manifest(self, result, change):
        path = Path(result['directory']) / 'manifest.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        change(value)
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_all_scopes_tasks_model_calls_reviews_and_traces_are_copied(self):
        with store.runtime_db() as c:
            original_calls = [tuple(row) for row in c.execute('SELECT * FROM runtime_model_calls')]
            original_reviews = [tuple(row) for row in c.execute('SELECT * FROM runtime_model_reviews')]
        self.h.switch(role='applicant', account='other')
        self.h.save({'city': 'private fictional applicant city'})
        self.h.submit('你在深圳吗？', key='applicant')
        self.h.switch()
        result = self.create()['data']
        self.assertEqual(self.verify(result['backup_id']), result)
        self.assertEqual(self.read(result, 'SELECT * FROM runtime_model_calls'), original_calls)
        self.assertEqual(self.read(result, 'SELECT * FROM runtime_model_reviews'), original_reviews)
        self.assertGreaterEqual(self.read(result, 'SELECT count(*) FROM runtime_scopes')[0][0], 3)
        self.assertIn('private fictional applicant city', str(self.read(result, 'SELECT content FROM runtime_documents')))
        self.assertTrue(self.read(result, 'SELECT 1 FROM runtime_events LIMIT 1'))
        self.assertEqual(self.read(result, 'SELECT status FROM runtime_tasks WHERE task_id=?', (result['backup_id'],)), [('executing',)])
        self.assertNotIn('private fictional applicant city', json.dumps(result))
        self.assertFalse(result['automatic_restore'])
        self.assertFalse(result['authenticity_verified'])
        self.assertTrue(result['contains_all_workspaces'])
        self.assertEqual(self.f.f.factory.call_count, 1)

    def test_snapshot_is_standalone_and_later_writes_do_not_change_it(self):
        result = self.create()['data']
        self.h.save({'location': 'later confirmed place', 'source': 'later source'}, revision=1)
        self.assertNotIn('later confirmed place', str(self.read(result, 'SELECT content FROM runtime_documents')))
        self.assertEqual(self.verify(result['backup_id']), result)
        self.assertEqual(sorted(path.name for path in Path(result['directory']).iterdir()), ['database.sqlite3', 'manifest.json'])

    def test_committed_wal_pages_are_included(self):
        connection = store.connect()
        try:
            self.assertEqual(connection.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            connection.execute('CREATE TABLE backup_wal_fixture(value TEXT)')
            connection.execute("INSERT INTO backup_wal_fixture VALUES ('committed WAL marker')")
            connection.commit()
            self.assertGreater(Path(str(store.DB_PATH) + '-wal').stat().st_size, 0)
            result = self.create()['data']
            self.assertEqual(self.read(result, 'SELECT value FROM backup_wal_fixture'), [('committed WAL marker',)])
            self.assertEqual(self.verify(result['backup_id']), result)
        finally:
            connection.close()

    def test_concurrent_commits_produce_a_consistent_snapshot(self):
        with store.runtime_db() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE backup_pair(a INTEGER,b INTEGER)')
            c.execute('INSERT INTO backup_pair VALUES (0,0)')
            c.execute('CREATE TABLE backup_padding(value TEXT)')
            c.executemany('INSERT INTO backup_padding VALUES (?)', [('x'*4096,)]*600)
        stop, ready = threading.Event(), threading.Event()
        errors = []
        def write():
            connection = sqlite3.connect(store.DB_PATH)
            try:
                for value in range(100):
                    connection.execute('UPDATE backup_pair SET a=?,b=?', (value, value))
                    connection.commit()
                    ready.set()
                    if stop.wait(.02):
                        break
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                connection.close()
        thread = threading.Thread(target=write, daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(3))
            result = self.create()['data']
        finally:
            stop.set()
            thread.join(timeout=3)
        self.assertFalse(errors)
        pair = self.read(result, 'SELECT a,b FROM backup_pair')[0]
        self.assertEqual(pair[0], pair[1])
        self.assertEqual(self.read(result, 'SELECT count(*) FROM backup_padding'), [(600,)])

    def test_workspace_access_cannot_mint_maintenance_permission(self):
        for name, payload in [('runtime.database.backup', {'confirmed_global': True}),
                              ('runtime.database.backup.check', {'backup_id': '0'*32})]:
            with self.assertRaises(KernelError) as caught:
                self.h.call(name, payload)
            self.assertEqual(caught.exception.code, 'MAINTENANCE_REQUIRED')
            self.assertTrue(caught.exception.trace_id)
        with maintenance_access(), self.assertRaises(KernelError) as direct:
            database_backup.create()
        self.assertEqual(direct.exception.code, 'KERNEL_REQUIRED')
        self.assertFalse(self.root().exists())

    def test_http_cannot_submit_global_backup_or_download_artifacts(self):
        with TestClient(app.app, base_url='http://127.0.0.1:18770') as client:
            client.get('/workspace')
            headers = {'X-Jobkit-Context': self.h.context['context_token']}
            response = client.post('/api/runtime/tasks', headers=headers, json={'tool': 'runtime.database.backup',
                'input': {'confirmed_global': True}, 'idempotency_key': 'http-backup'})
            self.assertEqual(response.json()['code'], 'INVALID_TOOL_KIND')
            self.assertEqual(client.get('/.jobkit-backups/test/database.sqlite3').status_code, 404)
        self.assertFalse(self.root().exists())

    def test_explicit_global_confirmation_and_no_extra_paths(self):
        with maintenance_access():
            for payload in [{'confirmed_global': False}, {'confirmed_global': 1},
                            {'confirmed_global': True, 'path': 'arbitrary.sqlite3'}]:
                with self.assertRaises(KernelError) as caught:
                    self.h.call('runtime.database.backup', payload)
                self.assertEqual(caught.exception.code, 'GLOBAL_BACKUP_CONFIRMATION_REQUIRED')
        self.assertFalse(self.root().exists())

    def test_verify_rejects_paths_instead_of_ids(self):
        with maintenance_access():
            for value in ('../outside', '/absolute/file', 'a'*33, 'https://example.invalid/file'):
                with self.assertRaises(KernelError) as caught:
                    self.h.call('runtime.database.backup.check', {'backup_id': value})
                self.assertEqual(caught.exception.code, 'INVALID_BACKUP_ID')

    def test_missing_backup_is_explicit(self):
        with self.assertRaises(KernelError) as caught:
            self.verify('0'*32)
        self.assertEqual(caught.exception.code, 'BACKUP_NOT_FOUND')

    def test_manifest_fields_and_duplicate_keys_are_strict(self):
        result = self.create()['data']
        path = Path(result['directory']) / 'manifest.json'
        original = path.read_bytes()
        changes = [lambda v: v.update(format_version=True), lambda v: v.update(database_file='../../outside.db'),
                   lambda v: v.update(backup_id='0'*32), lambda v: v.update(encrypted=True),
                   lambda v: v.update(automatic_restore=True), lambda v: v.update(unknown=True),
                   lambda v: v.update(created_at='no date')]
        for change in changes:
            path.write_bytes(original)
            self.change_manifest(result, change)
            with self.assertRaises(KernelError) as caught:
                self.verify(result['backup_id'])
            self.assertEqual(caught.exception.code, 'INVALID_BACKUP_MANIFEST')
        path.write_bytes(b'{"format":1,"format":2}')
        with self.assertRaises(KernelError) as caught:
            self.verify(result['backup_id'])
        self.assertEqual(caught.exception.code, 'INVALID_BACKUP_MANIFEST')

    def test_hash_detects_changed_database_without_modifying_source(self):
        result = self.create()['data']
        path = Path(result['directory']) / 'database.sqlite3'
        with path.open('r+b') as stream:
            stream.seek(100)
            stream.write(b'tampered')
        with self.assertRaises(KernelError) as caught:
            self.verify(result['backup_id'])
        self.assertEqual(caught.exception.code, 'BACKUP_DIGEST_MISMATCH')
        with store.runtime_db() as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchall()[0][0], 'ok')

    def test_sidecars_are_not_used_during_standalone_verification(self):
        result = self.create()['data']
        (Path(result['directory']) / 'database.sqlite3-wal').write_bytes(b'unexpected WAL')
        with self.assertRaises(KernelError) as caught:
            self.verify(result['backup_id'])
        self.assertEqual(caught.exception.code, 'INVALID_BACKUP_LAYOUT')

    def test_rehashed_corrupt_database_still_fails_integrity_check(self):
        result = self.create()['data']
        path = Path(result['directory']) / 'database.sqlite3'
        raw = b'not a sqlite database'
        path.write_bytes(raw)
        self.change_manifest(result, lambda v: v.update(database_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
        with self.assertRaises(KernelError) as caught:
            self.verify(result['backup_id'])
        self.assertEqual(caught.exception.code, 'INVALID_DATABASE_BACKUP')

    def test_valid_sqlite_with_wrong_creation_trace_is_refused(self):
        result = self.create()['data']
        path = Path(result['directory']) / 'database.sqlite3'
        connection = sqlite3.connect(path)
        try:
            connection.execute("UPDATE runtime_tasks SET tool='runtime.document.get' WHERE task_id=?", (result['backup_id'],))
            connection.commit()
        finally:
            connection.close()
        raw = path.read_bytes()
        self.change_manifest(result, lambda v: v.update(database_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
        with self.assertRaises(KernelError) as caught:
            self.verify(result['backup_id'])
        self.assertEqual(caught.exception.code, 'INVALID_DATABASE_BACKUP')

    def test_manifest_encoding_capacity_and_nonfinite_are_refused(self):
        result = self.create()['data']
        path = Path(result['directory']) / 'manifest.json'
        for raw in (b'\xff', b'{"format":NaN}', b'x'*(database_backup.MAX_MANIFEST_BYTES + 1)):
            path.write_bytes(raw)
            with self.assertRaises(KernelError) as caught:
                self.verify(result['backup_id'])
            self.assertEqual(caught.exception.code, 'INVALID_BACKUP_MANIFEST')

    def test_record_graph_corruption_is_preserved_not_silently_repaired(self):
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_documents SET content='not-json'")
        result = self.create()['data']
        self.assertEqual(self.read(result, 'SELECT content FROM runtime_documents'), [('not-json',)])
        self.assertTrue(self.verify(result['backup_id'])['integrity_checked'])

    def test_capacity_refusal_does_not_publish_partial_backup(self):
        with mock.patch.object(database_backup, 'MAX_BYTES', 1):
            with self.assertRaises(KernelError) as caught:
                self.create()
        self.assertEqual(caught.exception.code, 'BACKUP_TOO_LARGE')
        self.assertEqual(list(self.root().iterdir()), [])

    def test_deadline_failure_does_not_publish_partial_backup(self):
        with mock.patch.object(database_backup, 'TIME_LIMIT', 0):
            with self.assertRaises(KernelError):
                self.create()
        self.assertEqual(list(self.root().iterdir()), [])

    def test_existing_backup_is_never_overwritten(self):
        control = main._engine.submit('runtime.database.backup', {'confirmed_global': True}, 'collision', self.h.context, internal=True)
        final = self.root() / control['task_id']
        final.mkdir(parents=True)
        marker = final / 'untouched.txt'
        marker.write_text('keep', encoding='utf-8')
        with maintenance_access(), self.assertRaises(KernelError) as caught:
            main._engine.execute(control['task_id'], self.h.context['context_token'])
        self.assertEqual(caught.exception.code, 'BACKUP_EXISTS')
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')

    def test_reparse_points_are_refused_without_touching_target(self):
        path = self.root()
        fake = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        with mock.patch.object(Path, 'lstat', return_value=fake):
            with self.assertRaises(KernelError) as caught:
                database_backup._safe(path, directory=True)
        self.assertEqual(caught.exception.code, 'UNSAFE_BACKUP_PATH')

    def test_changes_during_verification_are_not_accepted(self):
        result = self.create()['data']
        inspect = store.runtime_database_inspect
        for filename in ('database.sqlite3', 'manifest.json'):
            path = Path(result['directory']) / filename
            def changed(*args, **kwargs):
                inspect(*args, **kwargs)
                current = path.stat()
                os.utime(path, ns=(current.st_atime_ns, current.st_mtime_ns + 1000000000))
            with mock.patch.object(store, 'runtime_database_inspect', side_effect=changed):
                with self.assertRaises(KernelError) as caught:
                    self.verify(result['backup_id'])
            self.assertEqual(caught.exception.code, 'BACKUP_CHANGED')

    def test_backup_cli_and_check_cli_use_same_kernel(self):
        env = {**os.environ, 'JOBKIT_DB': str(store.DB_PATH), 'PYTHONIOENCODING': 'utf-8'}
        created = subprocess.run([sys.executable, 'main.py', 'backup', '--confirm-all-workspaces'], env=env,
            cwd=Path(__file__).parent, capture_output=True, timeout=15, encoding='utf-8')
        self.assertEqual(created.returncode, 0, created.stderr)
        result = json.loads(created.stdout)
        checked = subprocess.run([sys.executable, 'main.py', 'backup-check', '--backup-id', result['data']['backup_id']], env=env,
            cwd=Path(__file__).parent, capture_output=True, timeout=15, encoding='utf-8')
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout)['data'], result['data'])

    def test_cli_missing_confirmation_is_traced_without_files(self):
        env = {**os.environ, 'JOBKIT_DB': str(store.DB_PATH), 'PYTHONIOENCODING': 'utf-8'}
        result = subprocess.run([sys.executable, 'main.py', 'backup'], env=env, cwd=Path(__file__).parent,
            capture_output=True, timeout=15, encoding='utf-8')
        self.assertEqual(result.returncode, 1)
        error = json.loads(result.stderr)
        self.assertEqual(error['code'], 'GLOBAL_BACKUP_CONFIRMATION_REQUIRED')
        self.assertTrue(error['trace_id'])
        self.assertFalse(self.root().exists())

    def test_file_effect_requires_maintenance_metadata(self):
        with self.assertRaises(ValueError):
            ToolRouter().register(Tool('test.file', lambda *args: None, effect='local_file'))

    def test_published_backup_fact_survives_context_switch(self):
        snapshot = store.runtime_database_snapshot
        def switched(*args, **kwargs):
            snapshot(*args, **kwargs)
            self.h.switch(role='applicant', account='new-scope')
        old = self.h.context['context_token']
        with mock.patch.object(store, 'runtime_database_snapshot', side_effect=switched), maintenance_access():
            with self.assertRaises(KernelError) as caught:
                main.dispatch('runtime.database.backup', {'confirmed_global': True}, old)
        self.assertEqual(caught.exception.code, 'STALE_CONTEXT')
        with store.runtime_db() as c:
            row = c.execute('SELECT status,result FROM runtime_tasks WHERE task_id=?', (caught.exception.trace_id,)).fetchone()
        self.assertEqual(row['status'], 'completed')
        result = json.loads(row['result'])
        self.assertTrue((Path(result['directory']) / 'database.sqlite3').is_file())

    def test_real_crash_before_and_after_publish_never_replays_backup(self):
        script = '''import os,sys
from pathlib import Path
import main
from sandbox import maintenance_access
rename=Path.rename
def checkpoint(self,target):
    if self.name.startswith('.pending-'):
        if sys.argv[3]=='before': os._exit(41)
        rename(self,target)
        os._exit(42)
    return rename(self,target)
Path.rename=checkpoint
with maintenance_access(): main._engine.execute(sys.argv[1],sys.argv[2])
'''
        for mode in ('before', 'after'):
            with self.subTest(mode=mode):
                control = main._engine.submit('runtime.database.backup', {'confirmed_global': True}, 'crash-' + mode,
                                               self.h.context, internal=True)
                env = {**os.environ, 'JOBKIT_DB': str(store.DB_PATH)}
                child = subprocess.run([sys.executable, '-c', script, control['task_id'], self.h.context['context_token'], mode],
                    env=env, cwd=Path(__file__).parent, capture_output=True, timeout=15)
                self.assertEqual(child.returncode, 41 if mode == 'before' else 42)
                recovered = self.h.call('runtime.task.recover', {'task_id': control['task_id']})
                self.assertEqual(recovered['status'], 'outcome_unknown')
                self.assertIsNone(recovered['result'])
                with maintenance_access():
                    again = main._engine.execute(control['task_id'], self.h.context['context_token'])
                self.assertEqual(again, recovered)
                if mode == 'before':
                    self.assertTrue((self.root() / ('.pending-' + control['task_id'])).is_dir())
                    with self.assertRaises(KernelError) as caught:
                        self.verify(control['task_id'])
                    self.assertEqual(caught.exception.code, 'BACKUP_NOT_FOUND')
                else:
                    self.assertTrue(self.verify(control['task_id'])['integrity_checked'])
        self.assertEqual(self.f.f.factory.call_count, 1)


if __name__ == '__main__':
    unittest.main()
