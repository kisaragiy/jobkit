"""Atomic restore creates a new scope and immutable history, never executable work."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import app
import main
import store
from sandbox import KernelError


class RestoreCommitTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.object(store, 'DB_PATH', Path(temp.name) / 'runtime.db')
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch('runtime.context')['data']
        self.owner = dict(self.context)
        self.call('runtime.document.save', {'content': {'location': 'old office', 'source': 'fixture'}, 'confirmed': True, 'expected_revision': 0})
        tasks = self.call('runtime.task.batch.submit', {'items': [{'conversation_note': letter, 'message': '地点在哪里？'} for letter in 'ABC'], 'idempotency_key': 'fixture'})['data']['tasks']
        self.call('runtime.task.execute', {'task_id': tasks[0]['task_id']})
        for revision in range(2):
            self.call('runtime.task.edit', {'task_id': tasks[0]['task_id'], 'text': 'human ' + str(revision), 'confirmed': True, 'expected_manual_revision': revision, 'expected_document_revision': 1})
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='outcome_unknown' WHERE task_id=?", (tasks[1]['task_id'],))
        self.call('runtime.document.save', {'content': {'location': 'new office', 'source': 'fixture'}, 'confirmed': True, 'expected_revision': 1})
        self.call('runtime.task.replace', {'task_id': tasks[0]['task_id'], 'expected_review_revision': 0, 'expected_document_revision': 2})
        self.snapshot = self.call('runtime.workspace.export', {})['data']
        self.text = json.dumps(self.snapshot, ensure_ascii=False)
        self.target = {'organization_id': 'restored org', 'account_id': 'one'}
        self.plan = self.call('runtime.workspace.restore.preflight', {'snapshot_text': self.text, 'target': self.target})['data']
        self.payload = {'snapshot_text': self.text, 'target': self.target, 'expected_content_sha256': self.plan['content_sha256'], 'idempotency_key': 'request-one', 'confirmed': True}

    def call(self, tool, payload=None):
        return main.dispatch(tool, payload, self.context['context_token'])

    def commit(self, **changes):
        return self.call('runtime.workspace.restore.commit', {**self.payload, **changes})

    def switch(self, target):
        self.context = self.call('runtime.switch', {key: target[key] for key in ('role', 'organization_id', 'account_id')})['data']

    def imports(self):
        with store.runtime_db() as c:
            return c.execute('SELECT count(*) FROM runtime_workspace_imports').fetchone()[0]

    def test_atomic_document_archive_and_mapping_without_executing(self):
        with mock.patch('recruiter_reply.draft', side_effect=AssertionError('must not execute')):
            result = self.commit()['data']
        self.assertEqual(result['counts'], {'tasks': 4, 'draft_edits': 2})
        self.assertFalse(result['automatic_execution'])
        self.assertFalse(result['history_execution_verified'])
        self.assertEqual(self.call('runtime.context')['data']['scope_id'], self.owner['scope_id'])
        with store.runtime_db() as c:
            self.assertEqual(c.execute('SELECT revision FROM runtime_documents WHERE scope_id=?', (result['target_workspace']['scope_id'],)).fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_tasks WHERE scope_id=? AND kind='work'", (result['target_workspace']['scope_id'],)).fetchone()[0], 0)

    def test_remapped_bidirectional_history_is_readable_and_not_executable(self):
        result = self.commit()['data']
        self.switch(result['target_workspace'])
        data = self.call('runtime.workspace.restored.get')['data']
        records = data['records']
        self.assertEqual(len(records), 4)
        self.assertTrue(all(not record['executed_here'] and record['source_type'] == 'restored_history' for record in records))
        self.assertFalse({record['task_id'] for record in records} & {record['task_id'] for record in self.snapshot['tasks']})
        old = next(record for record in records if record['superseded_by_task_id'])
        new = next(record for record in records if record['task_id'] == old['superseded_by_task_id'])
        self.assertEqual(new['supersedes_task_id'], old['task_id'])
        self.assertEqual([edit['revision'] for edit in data['draft_edits']], [1, 2])
        self.assertTrue(all(edit['task_id'] == old['task_id'] for edit in data['draft_edits']))
        for record in records:
            with self.assertRaises(KernelError) as error:
                self.call('runtime.task.execute', {'task_id': record['task_id']})
            self.assertEqual(error.exception.code, 'TASK_NOT_FOUND')
        self.assertEqual(self.call('runtime.workspace.restored.get', {'view': 'source'})['data']['source_snapshot'], self.snapshot)

    def test_retries_reuse_same_import_without_rewriting_later_document(self):
        first = self.commit()['data']
        self.assertEqual(self.commit()['data'], first)
        self.switch(first['target_workspace'])
        self.call('runtime.document.save', {'content': {'location': 'later edit', 'source': 'fixture'}, 'confirmed': True, 'expected_revision': 2})
        self.switch(self.owner)
        self.assertEqual(self.commit()['data'], first)
        self.assertEqual(self.imports(), 1)
        with store.runtime_db() as c:
            self.assertEqual(c.execute('SELECT revision FROM runtime_documents WHERE scope_id=?', (first['target_workspace']['scope_id'],)).fetchone()[0], 3)

    def test_same_request_key_cannot_change_target_or_content(self):
        self.commit()
        with self.assertRaises(KernelError) as error:
            self.commit(target={**self.target, 'account_id': 'different'})
        self.assertEqual(error.exception.code, 'IDEMPOTENCY_CONFLICT')
        changed = deepcopy(self.snapshot)
        changed['tasks'][0]['conversation_note'] = 'changed'
        text = json.dumps(changed)
        digest = self.call('runtime.workspace.restore.preflight', {'snapshot_text': text, 'target': self.target})['data']['content_sha256']
        with self.assertRaises(KernelError) as error:
            self.commit(snapshot_text=text, expected_content_sha256=digest)
        self.assertEqual(error.exception.code, 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(self.imports(), 1)

    def test_different_request_cannot_overwrite_existing_target(self):
        self.commit()
        with self.assertRaises(KernelError) as error:
            self.commit(idempotency_key='another-request')
        self.assertEqual(error.exception.code, 'RESTORE_TARGET_EXISTS')
        self.assertEqual(self.imports(), 1)

    def test_confirmation_digest_and_extra_parameters_are_required(self):
        for changes in ({'confirmed': False}, {'expected_content_sha256': 'f' * 64}, {'extra': 'execute'}, {'idempotency_key': ''}):
            with self.assertRaises(KernelError):
                self.commit(**changes)
        self.assertEqual(self.imports(), 0)

    def test_receipt_failure_rolls_back_scope_document_archive_and_mapping(self):
        with mock.patch('runtime_tools.record_commit', side_effect=KernelError('FIXTURE', 'fixture')):
            with self.assertRaises(KernelError):
                self.commit()
        self.assertEqual(self.imports(), 0)
        with store.runtime_db() as c:
            target = self.plan['target_workspace']['scope_id']
            self.assertIsNone(c.execute('SELECT 1 FROM runtime_scopes WHERE scope_id=?', (target,)).fetchone())
            self.assertIsNone(c.execute('SELECT 1 FROM runtime_documents WHERE scope_id=?', (target,)).fetchone())

    def test_receipt_recovery_confirms_without_restoring_again(self):
        response = self.commit()
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='executing',result=NULL WHERE task_id=?", (response['trace_id'],))
        tool = main._router.resolve('runtime.workspace.restore.commit')
        with mock.patch.dict(main._router._tools, {tool.name: replace(tool, handler=mock.Mock(side_effect=AssertionError('must not restore again')))}):
            done = self.call('runtime.task.recover', {'task_id': response['trace_id']})['data']
        self.assertEqual(done['error_code'], 'RECOVERED_COMMIT')
        self.assertEqual(done['result'], response['data'])
        self.assertEqual(self.imports(), 1)

    def test_orphan_target_document_is_not_overwritten(self):
        scope = self.plan['target_workspace']['scope_id']
        with store.runtime_db() as c:
            c.execute('INSERT INTO runtime_documents VALUES (?,?,?,?)', (scope, 'facts', 9, '{}'))
        plan = self.call('runtime.workspace.restore.preflight', {'snapshot_text': self.text, 'target': self.target})['data']
        self.assertFalse(plan['preflight_passed'])
        with self.assertRaises(KernelError) as error:
            self.commit()
        self.assertEqual(error.exception.code, 'RESTORE_TARGET_EXISTS')

    def test_concurrent_same_key_reuses_one_import(self):
        outcomes, errors = [], []
        def worker():
            try:
                outcomes.append(self.commit()['data'])
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 3)
        self.assertEqual(len({outcome['import_id'] for outcome in outcomes}), 1)
        self.assertEqual(self.imports(), 1)

    def test_concurrent_different_keys_share_no_partial_target(self):
        outcomes = []
        def worker(key):
            try:
                outcomes.append(self.commit(idempotency_key=key)['data']['import_id'])
            except KernelError as error:
                outcomes.append(error.code)
        threads = [threading.Thread(target=worker, args=(str(index),)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes.count('RESTORE_TARGET_EXISTS'), 1)
        self.assertEqual(self.imports(), 1)

    def test_corrupted_mapping_is_refused_on_read_and_retry(self):
        result = self.commit()['data']
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_workspace_imports SET task_id_map='{}' WHERE import_id=?", (result['import_id'],))
        with self.assertRaises(KernelError) as error:
            self.commit()
        self.assertEqual(error.exception.code, 'INVALID_RESTORED_ARCHIVE')
        self.switch(result['target_workspace'])
        with self.assertRaises(KernelError):
            self.call('runtime.workspace.restored.get')

    def test_archives_are_scope_isolated_and_export_includes_them(self):
        result = self.commit()['data']
        with self.assertRaises(KernelError) as error:
            self.call('runtime.workspace.restored.get')
        self.assertEqual(error.exception.code, 'RESTORED_ARCHIVE_NOT_FOUND')
        self.switch(result['target_workspace'])
        exported = self.call('runtime.workspace.export', {})['data']
        self.assertEqual(exported['format_version'], 2)
        self.assertEqual(exported['counts'], self.snapshot['counts'])
        self.assertEqual(self.call('runtime.workspace.restored.get', {'view': 'source'})['data']['source_snapshot'], self.snapshot)

    def test_actual_http_commit_archive_auth_and_body_limit(self):
        with TestClient(app.app, base_url='http://127.0.0.1:18770') as client:
            client.get('/workspace')
            headers = {'X-Jobkit-Context': self.context['context_token']}
            path = '/api/runtime/workspace/restore/commit'
            response = client.post(path, headers=headers, json=self.payload)
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()['data']
            self.assertEqual(client.post(path, headers=headers, json=self.payload).json()['data'], result)
            self.switch(result['target_workspace'])
            headers['X-Jobkit-Context'] = self.context['context_token']
            archive = client.get('/api/runtime/workspace/restored', headers=headers).json()['data']
            self.assertEqual(len(archive['records']), 4)
            with mock.patch.object(app, 'RESTORE_HTTP_MAX_BYTES', 4):
                self.assertEqual(client.post(path, headers=headers, content=b'12345').status_code, 413)
        with TestClient(app.app, base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.post(path, json=self.payload).status_code, 401)

    def test_real_process_exit_before_and_after_atomic_commit(self):
        script = '''
import os,sys
from pathlib import Path
from dataclasses import replace
import store
store.DB_PATH=Path(sys.argv[1])
import main,runtime_tools
if sys.argv[4]=='before':
 original=runtime_tools.record_commit
 def checkpoint(*args):
  result=original(*args)
  if args[2]=='runtime.workspace.restore.commit':os._exit(21)
  return result
 runtime_tools.record_commit=checkpoint
else:
 original=main._router.resolve('runtime.workspace.restore.commit')
 def checkpoint(*args):
  original.handler(*args)
  os._exit(22)
 main._router._tools[original.name]=replace(original,handler=checkpoint)
main._engine.execute(sys.argv[2],sys.argv[3])
'''
        for mode in ('before', 'after'):
            payload = {**self.payload, 'target': {**self.target, 'account_id': mode}, 'idempotency_key': mode}
            control = main._engine.submit('runtime.workspace.restore.commit', payload, 'control-' + mode, self.context, internal=True)
            child = subprocess.run([sys.executable, '-c', script, str(store.DB_PATH), control['task_id'], self.context['context_token'], mode], capture_output=True, timeout=15, cwd=Path(__file__).parent)
            self.assertEqual(child.returncode, 21 if mode == 'before' else 22, child.stderr)
            recovered = self.call('runtime.task.recover', {'task_id': control['task_id']})['data']
            if mode == 'before':
                self.assertEqual(recovered['status'], 'outcome_unknown')
                self.assertEqual(self.imports(), 0)
            else:
                self.assertEqual(recovered['error_code'], 'RECOVERED_COMMIT')
                self.assertEqual(self.imports(), 1)
                self.assertEqual(self.call('runtime.workspace.restore.commit', payload)['data'], recovered['result'])
