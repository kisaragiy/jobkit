"""Restore preflight validates untrusted snapshots without restoring or executing."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import app
import main
import restore_preflight
import store
from sandbox import KernelError


class RestorePreflightTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patch = mock.patch.object(store, 'DB_PATH', Path(temp.name) / 'runtime.db')
        patch.start()
        self.addCleanup(patch.stop)
        self.context = main.dispatch('runtime.context')['data']
        self.call('runtime.document.save', {'content': {'location': 'fictional office', 'source': 'fixture'}, 'confirmed': True, 'expected_revision': 0})
        task = self.call('runtime.task.submit', {'tool': 'draft.reply', 'input': {'message': '地点在哪里？'}, 'idempotency_key': 'fixture'})['data']
        self.task_id = task['task_id']
        self.call('runtime.task.execute', {'task_id': self.task_id})
        self.call('runtime.task.edit', {'task_id': self.task_id, 'text': 'human text', 'confirmed': True, 'expected_manual_revision': 0, 'expected_document_revision': 1})
        self.snapshot = self.call('runtime.workspace.export', {})['data']

    def call(self, name, payload=None):
        return main.dispatch(name, payload, self.context['context_token'])

    def payload(self, snapshot=None, text=None):
        return {'snapshot_text': json.dumps(self.snapshot if snapshot is None else snapshot, ensure_ascii=False) if text is None else text,
                'target': {'organization_id': 'restored', 'account_id': 'new'}}

    def preflight(self, snapshot=None, text=None):
        return self.call('runtime.workspace.restore.preflight', self.payload(snapshot, text))['data']

    def assert_invalid(self, snapshot=None, text=None):
        with self.assertRaises(KernelError) as error:
            self.preflight(snapshot, text)
        self.assertEqual(error.exception.code, 'INVALID_RESTORE_FILE')
        self.assertTrue(error.exception.trace_id)

    def test_valid_summary_digest_and_no_business_state_changes(self):
        with store.runtime_db() as c:
            before = {table: c.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in ('runtime_scopes', 'runtime_documents', 'runtime_draft_edits')}
        with mock.patch('recruiter_reply.draft', side_effect=AssertionError('must not execute')):
            summary = self.preflight()
        self.assertTrue(summary['preflight_passed'])
        self.assertTrue(summary['restore_available'])
        self.assertFalse(summary['automatic_execution'])
        self.assertEqual(summary['counts'], {'tasks': 1, 'draft_edits': 1})
        self.assertEqual(len(summary['content_sha256']), 64)
        self.assertNotIn('human text', json.dumps(summary))
        alternate = json.dumps(self.snapshot, sort_keys=True, indent=3)
        self.assertEqual(summary['content_sha256'], self.preflight(text=alternate)['content_sha256'])
        with store.runtime_db() as c:
            for table, count in before.items():
                self.assertEqual(c.execute('SELECT count(*) FROM ' + table).fetchone()[0], count)
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_tasks WHERE kind='work'").fetchone()[0], 1)

    def test_existing_target_reports_issue_without_overwrite(self):
        payload = self.payload()
        payload['target'] = {key: self.context[key] for key in ('organization_id', 'account_id')}
        result = self.call('runtime.workspace.restore.preflight', payload)['data']
        self.assertFalse(result['preflight_passed'])
        self.assertEqual(result['issues'][0]['code'], 'TARGET_EXISTS')
        self.assertEqual(self.call('runtime.document.get')['data']['revision'], 1)

    def test_bad_json_duplicate_fields_and_nonfinite_values(self):
        for text in ('{', 'null', '{"format":1,"format":2}', '{"x":NaN}', '{"x":Infinity}', '[]'):
            self.assert_invalid(text=text)

    def test_strict_top_level_schema_and_scope_identity(self):
        changes = ({'format_version': True}, {'format_version': 99}, {'restore_supported': True}, {'platform_send_evidence': True},
                   {'created_at': '2026-10-26'}, {'extra': 'execute this'})
        for change in changes:
            self.assert_invalid({**deepcopy(self.snapshot), **change})
        invalid = deepcopy(self.snapshot)
        invalid['workspace']['account_id'] = 'different'
        self.assert_invalid(invalid)
        invalid['workspace']['organization_id'] = '\ud800'
        self.assert_invalid(text=json.dumps(invalid))

    def test_invalid_task_fields_versions_results_and_counts(self):
        for key, value in (('status', []), ('status', 'failed'), ('review_status', 'handled'), ('source_type', 'platform'), ('input_state', 'unreadable'), ('document_revision', 2), ('tool', 'unregistered')):
            invalid = deepcopy(self.snapshot)
            invalid['tasks'][0][key] = value
            self.assert_invalid(invalid)
        for key, value in (('sent', True), ('text', []), ('state', 'needs_review'), ('citations', [1])):
            invalid = deepcopy(self.snapshot)
            invalid['tasks'][0]['result'][key] = value
            self.assert_invalid(invalid)
        invalid = deepcopy(self.snapshot)
        invalid['counts']['tasks'] = True
        self.assert_invalid(invalid)

    def test_full_history_and_duplicate_task_identity_are_required(self):
        invalid = deepcopy(self.snapshot)
        invalid['draft_edits'] = []
        invalid['counts']['draft_edits'] = 0
        self.assert_invalid(invalid)
        invalid = deepcopy(self.snapshot)
        invalid['tasks'].append(deepcopy(invalid['tasks'][0]))
        invalid['counts']['tasks'] = 2
        self.assert_invalid(invalid)

    def test_limits_refuse_without_partial_plan(self):
        for name, maximum in (('MAX_BYTES', 10), ('MAX_TASKS', 0), ('MAX_EDITS', 0)):
            with mock.patch.object(restore_preflight, name, maximum):
                with self.assertRaises(KernelError) as error:
                    self.preflight()
                self.assertEqual((error.exception.code, error.exception.status), ('RESTORE_TOO_LARGE', 413))

    def test_canonical_expansion_is_bounded_before_it_can_be_archived(self):
        snapshot = deepcopy(self.snapshot)
        for task in snapshot['tasks']:
            task['created_at'] = task['updated_at'] = 1e12
        text = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')).replace('1000000000000.0', '1e12')
        with mock.patch.object(restore_preflight, 'MAX_BYTES', len(text.encode('utf-8')) + 1):
            with self.assertRaises(KernelError) as error:
                self.preflight(text=text)
        self.assertEqual(error.exception.code, 'RESTORE_TOO_LARGE')

    def test_applicant_snapshot_keeps_role_and_profile(self):
        self.context = self.call('runtime.switch', {'role': 'applicant', 'organization_id': 'local', 'account_id': 'applicant'})['data']
        self.call('runtime.document.save', {'content': {'city': 'fixture city'}, 'confirmed': True, 'expected_revision': 0})
        snapshot = self.call('runtime.workspace.export', {})['data']
        result = self.preflight(snapshot)
        self.assertTrue(result['preflight_passed'])
        self.assertEqual(result['target_workspace']['role'], 'applicant')
        self.assertEqual(result['counts']['tasks'], 0)

    def test_active_and_unknown_records_are_reported_not_executed(self):
        self.call('runtime.task.submit', {'tool': 'draft.reply', 'input': {'message': '地点？'}, 'idempotency_key': 'queued'})
        unknown = self.call('runtime.task.submit', {'tool': 'draft.reply', 'input': {'message': '地点？'}, 'idempotency_key': 'unknown'})['data']
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_tasks SET status='outcome_unknown' WHERE task_id=?", (unknown['task_id'],))
        result = self.preflight(self.call('runtime.workspace.export', {})['data'])
        self.assertEqual(result['review_required'], {'queued': 1, 'executing': 0, 'outcome_unknown': 1})
        self.assertFalse(result['automatic_execution'])

    def test_stale_context_and_path_parameters_are_rejected(self):
        token = self.context['context_token']
        self.context = self.call('runtime.switch', {'role': 'applicant', 'organization_id': 'local', 'account_id': 'other'})['data']
        with self.assertRaises(KernelError) as error:
            main.dispatch('runtime.workspace.restore.preflight', self.payload(), token)
        self.assertEqual(error.exception.code, 'STALE_CONTEXT')
        with self.assertRaises(KernelError):
            self.call('runtime.workspace.restore.preflight', {'path': 'C:/somewhere'})

    def test_http_bounded_body_strict_outer_json_and_auth(self):
        with TestClient(app.app, base_url='http://127.0.0.1:18770') as client:
            client.get('/workspace')
            headers = {'X-Jobkit-Context': self.context['context_token'], 'Content-Type': 'application/json'}
            path = '/api/runtime/workspace/restore/preflight'
            response = client.post(path, headers=headers, json=self.payload())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['data']['restore_available'])
            for raw in (b'{"target":1,"target":2}', b'\xff', b'{'):
                bad = client.post(path, headers=headers, content=raw)
                self.assertEqual(bad.status_code, 400)
                self.assertTrue(bad.json()['trace_id'])
            with mock.patch.object(app, 'RESTORE_HTTP_MAX_BYTES', 16):
                large = client.post(path, headers=headers, content=b'x' * 17)
                self.assertEqual(large.status_code, 413)
                self.assertTrue(large.json()['trace_id'])
        with TestClient(app.app, base_url='http://127.0.0.1:18770') as client:
            self.assertEqual(client.post(path, json=self.payload()).status_code, 401)

    def test_invalid_input_unicode_has_trace_instead_of_sqlite_failure(self):
        with self.assertRaises(KernelError) as error:
            self.preflight(text='\ud800')
        self.assertEqual(error.exception.code, 'INVALID_INPUT')
        self.assertTrue(error.exception.trace_id)

    def test_invalid_tool_result_unicode_is_terminal_and_traced(self):
        original = main._router.resolve('runtime.context')
        with mock.patch.dict(main._router._tools, {'runtime.context': replace(original, handler=lambda *_: {'bad': '\ud800'})}):
            with self.assertRaises(KernelError) as error:
                self.call('runtime.context')
        self.assertEqual(error.exception.code, 'INVALID_TOOL_RESULT')
        with store.runtime_db() as c:
            row = c.execute('SELECT status,result FROM runtime_tasks WHERE task_id=?', (error.exception.trace_id,)).fetchone()
            self.assertEqual((row['status'], row['result']), ('failed', None))


if __name__ == '__main__':
    unittest.main()
