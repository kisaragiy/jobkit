"""Flat v2 backups preserve provenance through export/restore cycles."""
from copy import deepcopy
import json
import unittest
from unittest import mock
from fastapi.testclient import TestClient
import app
import restore_preflight
import runtime_tools
import store
from sandbox import KernelError
import test_restore_commit as restore_fixture


class CombinedExportTest(unittest.TestCase):
    def setUp(self):
        self.h = restore_fixture.RestoreCommitTest()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        restored = self.h.commit()['data']
        self.h.switch(restored['target_workspace'])
        self.h.call('runtime.document.save', {'content': {'location': 'latest local office', 'source': 'local fixture'}, 'confirmed': True, 'expected_revision': 2})
        self.local = self.h.call('runtime.task.submit', {'tool': 'draft.reply', 'input': {'message': '地点在哪里？', 'conversation_note': 'local D'}, 'idempotency_key': 'new-local'})['data']
        self.h.call('runtime.task.execute', {'task_id': self.local['task_id']})
        self.h.call('runtime.task.edit', {'task_id': self.local['task_id'], 'text': 'local human wording', 'confirmed': True, 'expected_manual_revision': 0, 'expected_document_revision': 3})
        self.bundle = self.h.call('runtime.workspace.export', {})['data']

    def validate(self, data):
        return restore_preflight.validate_snapshot(json.dumps(data, ensure_ascii=False), scope_id=store.runtime_scope_id,
                                                   fact_fields=runtime_tools.FACT_FIELDS, profile_fields=runtime_tools.PROFILE_FIELDS)

    def reject(self, data):
        with self.assertRaises(KernelError) as error:
            self.validate(data)
        self.assertEqual(error.exception.code, 'INVALID_RESTORE_FILE')

    def restore(self, data, name):
        text = json.dumps(data, ensure_ascii=False)
        target = {'organization_id': 'restored again', 'account_id': name}
        plan = self.h.call('runtime.workspace.restore.preflight', {'snapshot_text': text, 'target': target})['data']
        result = self.h.call('runtime.workspace.restore.commit', {'snapshot_text': text, 'target': target,
                              'expected_content_sha256': plan['content_sha256'], 'idempotency_key': name, 'confirmed': True})['data']
        self.h.switch(result['target_workspace'])
        return result

    def test_combines_local_and_history_without_mutating_source(self):
        self.assertEqual(self.bundle['format_version'], 2)
        self.assertEqual(self.bundle['counts'], {'tasks': 5, 'draft_edits': 3})
        self.assertEqual(sum(t['record_origin'] == 'local' for t in self.bundle['tasks']), 1)
        self.assertEqual(sum(t['record_origin'] == 'restored_history' for t in self.bundle['tasks']), 4)
        self.assertEqual(self.h.call('runtime.workspace.restored.get', {'view': 'source'})['data']['source_snapshot'], self.h.snapshot)
        self.validate(self.bundle)

    def test_v2_restore_maps_every_record_and_never_creates_work(self):
        self.restore(self.bundle, 'second')
        data = self.h.call('runtime.workspace.restored.get')['data']
        self.assertEqual((len(data['records']), len(data['draft_edits'])), (5, 3))
        self.assertTrue(all(t['record_origin'] == 'restored_history' and not t['executed_here'] for t in data['records']))
        self.assertFalse({t['task_id'] for t in data['records']} & {t['task_id'] for t in self.bundle['tasks']})
        with store.runtime_db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM runtime_tasks WHERE scope_id=? AND kind='work'", (self.h.context['scope_id'],)).fetchone()[0], 0)
        for task in data['records']:
            with self.assertRaises(KernelError):
                self.h.call('runtime.task.execute', {'task_id': task['task_id']})
        self.assertEqual(self.h.call('runtime.workspace.restored.get', {'view': 'source'})['data']['source_snapshot'], self.bundle)

    def test_multiple_cycles_stay_flat_and_preserve_original_sources(self):
        original_origin = deepcopy(self.bundle['origins'][0])
        data = self.bundle
        for index in range(2):
            self.restore(data, 'cycle-' + str(index))
            data = self.h.call('runtime.workspace.export', {})['data']
            self.assertEqual(data['counts'], {'tasks': 5, 'draft_edits': 3})
            self.assertEqual(len(data['origins']), index + 2)
            self.assertIn(original_origin, data['origins'])
            self.assertTrue(all(set(origin) == {'origin_id', 'workspace', 'document'} for origin in data['origins']))
            self.validate(data)

    def test_old_and_current_document_namespaces_stay_distinct(self):
        self.assertEqual(self.bundle['document']['revision'], 3)
        self.assertEqual(self.bundle['document']['content']['location'], 'latest local office')
        source = self.bundle['origins'][0]
        self.assertEqual(source['document']['revision'], 2)
        self.assertEqual(source['document']['content']['location'], 'new office')
        old = next(t for t in self.bundle['tasks'] if t['manual_revision'] == 2)
        self.assertEqual((old['origin_id'], old['document_revision']), (source['origin_id'], 1))

    def test_missing_origin_and_bad_local_provenance_are_rejected(self):
        data = deepcopy(self.bundle)
        data['origins'] = []
        self.reject(data)
        data = deepcopy(self.bundle)
        next(t for t in data['tasks'] if t['record_origin'] == 'local')['source_task_id'] = 'a' * 32
        self.reject(data)

    def test_duplicate_global_ids_and_duplicate_origin_source_ids_are_rejected(self):
        data = deepcopy(self.bundle)
        data['tasks'][1]['task_id'] = data['tasks'][0]['task_id']
        self.reject(data)
        data = deepcopy(self.bundle)
        historical = [t for t in data['tasks'] if t['record_origin'] == 'restored_history']
        historical[1]['source_task_id'] = historical[0]['source_task_id']
        self.reject(data)

    def test_links_cannot_cross_provenance_groups(self):
        data = deepcopy(self.bundle)
        local = next(t for t in data['tasks'] if t['record_origin'] == 'local')
        old = next(t for t in data['tasks'] if t['record_origin'] == 'restored_history')
        local.update(review_status='superseded', review_revision=1, superseded_by_task_id=old['task_id'])
        old['supersedes_task_id'] = local['task_id']
        self.reject(data)

    def test_origin_role_mismatch_is_rejected(self):
        data = deepcopy(self.bundle)
        origin = data['origins'][0]
        origin['workspace']['role'] = 'applicant'
        workspace = origin['workspace']
        workspace['scope_id'] = store.runtime_scope_id('applicant', workspace['organization_id'], workspace['account_id'])
        origin['document'] = {'kind': 'profile', 'revision': 2, 'content': {'city': 'fixture city'}}
        self.reject(data)

    def test_combined_counts_and_origin_caps_are_enforced_before_export(self):
        for name, maximum in (('EXPORT_MAX_TASKS', 4), ('EXPORT_MAX_EDITS', 2)):
            with mock.patch.object(runtime_tools, name, maximum):
                with self.assertRaises(KernelError) as error:
                    self.h.call('runtime.workspace.export', {})
                self.assertEqual(error.exception.code, 'EXPORT_TOO_LARGE')
        with mock.patch.object(restore_preflight, 'MAX_ORIGINS', 0):
            with self.assertRaises(KernelError) as error:
                self.validate(self.bundle)
            self.assertEqual(error.exception.code, 'RESTORE_TOO_LARGE')

    def test_corrupt_archive_prevents_partial_combined_export(self):
        with store.runtime_db() as c:
            c.execute("UPDATE runtime_workspace_imports SET task_id_map='{}' WHERE target_scope_id=?", (self.h.context['scope_id'],))
        with self.assertRaises(KernelError) as error:
            self.h.call('runtime.workspace.export', {})
        self.assertEqual(error.exception.code, 'INVALID_RESTORED_ARCHIVE')

    def test_native_workspace_still_exports_v1(self):
        self.h.switch(self.h.owner)
        exported = self.h.call('runtime.workspace.export', {})['data']
        self.assertEqual(exported['format_version'], 1)
        self.assertNotIn('origins', exported)
        self.assertTrue(all('record_origin' not in t for t in exported['tasks']))
        self.validate(exported)

    def test_http_v2_export_and_preflight(self):
        with TestClient(app.app, base_url='http://127.0.0.1:18770') as client:
            client.get('/workspace')
            headers = {'X-Jobkit-Context': self.h.context['context_token']}
            response = client.post('/api/runtime/workspace/export', headers=headers, json={})
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()['data']
            self.assertEqual((data['format_version'], data['counts']['tasks']), (2, 5))
            response = client.post('/api/runtime/workspace/restore/preflight', headers=headers,
                                   json={'snapshot_text': json.dumps(data), 'target': {'organization_id': 'another', 'account_id': 'scope'}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['data']['preflight_passed'])
